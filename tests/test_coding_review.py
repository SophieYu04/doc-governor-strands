import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docgov.coding_review import review_documents
from docgov.engine import analyze_trust, build_snapshot


class CodingReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        files = {
            "docs/API.md": "# API\nVersion 1.\n",
            "src/version.txt": "1\n",
            ".docgov/ledger.jsonl": "",
            ".docgov/catalog.yaml": json.dumps({"version": 1, "documents": [{
                "path": "docs/API.md", "type": "contract", "status": "current",
                "depends_on": ["src/**"]}]}),
            ".docgov/coding-agent-review-policy.json": json.dumps({"version": 1,
                "reviewer": "coding_agent", "status": "enabled", "documents": ["docs/API.md"]}),
        }
        for name, value in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.binary = Path(self.temp.name) / "codex"
        self.binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
mode = os.environ.get('TEST_REVIEW_MODE', '')
if mode == 'failed': sys.exit(1)
args = sys.argv
schema = json.loads(Path(args[args.index('--output-schema')+1]).read_text())
snapshot = schema['properties']['snapshot_id']['enum'][0]
item = {'path':'docs/API.md','verdict':'trusted','evidence_paths':['src/version.txt'],'unresolved_claims':[]}
if mode == 'untrusted': item['verdict']='untrusted'; item['unresolved_claims']=['unsupported']
if mode == 'empty_evidence': item['evidence_paths']=[]
if mode == 'escape': item['evidence_paths']=['../private.txt']
if mode == 'wrong_scope': item['path']='docs/OTHER.md'
if mode == 'mutate': Path('src/version.txt').write_text('2')
if mode == 'source_race': Path(os.environ['TEST_ORIGINAL_SOURCE']).write_text('2')
if mode == 'untracked_race': Path(os.environ['TEST_ORIGINAL_SOURCE']).with_name('new.txt').write_text('2')
if mode == 'wrong_snapshot': snapshot='wrong'
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'snapshot_id':snapshot,'documents':[item]}))
print(json.dumps({'type':'thread.started','thread_id':'test-independent-review'}))
if mode != 'empty_model': print(json.dumps({'type':'turn.completed','usage':{'output_tokens':10}}))
''')
        self.binary.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def review(self, **kwargs):
        return review_documents(self.root, ["docs/API.md"], codex_binary=str(self.binary),
                                verify_command=kwargs.get("verify_command", f"{shlex.quote(sys.executable)} -c 'pass'"))

    def test_success_records_separate_reviewer_and_preserves_prose_index(self):
        tree = self.git("write-tree")
        result = self.review()
        self.assertEqual(result.result, "changed", result.error)
        self.assertEqual(tree, self.git("write-tree"))
        self.assertEqual((self.root / "docs/API.md").read_text(), "# API\nVersion 1.\n")
        record = json.loads((self.root / ".docgov/ledger.jsonl").read_text())
        self.assertEqual(record["verifier"], "codex:test-independent-review")
        self.assertEqual(record["action"], "verify_current")

    def test_invalid_results_do_not_create_trust(self):
        for mode in ["failed", "untrusted", "empty_evidence", "escape", "wrong_scope", "mutate", "empty_model", "wrong_snapshot"]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"TEST_REVIEW_MODE": mode}):
                result = self.review()
                self.assertEqual(result.result, "blocked", mode)
                self.assertFalse(result.changed)
                self.assertEqual((self.root / ".docgov/ledger.jsonl").read_text(), "")

    def test_verifier_failure_does_not_create_trust(self):
        result = self.review(verify_command=f"{shlex.quote(sys.executable)} -c 'raise SystemExit(1)'")
        self.assertEqual(result.error, "verification_failed")

    def test_verifier_cannot_modify_sources(self):
        result = self.review(verify_command=f'''{shlex.quote(sys.executable)} -c 'from pathlib import Path; Path("src/version.txt").write_text("2")' ''')
        self.assertEqual(result.error, "verifier_modified_snapshot")

    def test_dead_reviewer_lock_file_does_not_block_recovery(self):
        (self.root/'.git/docgov-review.lock').write_text('old process')
        self.assertEqual(self.review().result,'changed')

    def test_active_reviewer_lock_is_respected(self):
        import fcntl
        with (self.root/'.git/docgov-review.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.review().error,'review_locked')
        self.assertEqual(self.review().result,'changed')

    def test_partial_staging_is_preserved(self):
        path = self.root / "docs/API.md"
        path.write_text("staged")
        self.git("add", "docs/API.md")
        path.write_text("unstaged")
        tree = self.git("write-tree")
        self.assertEqual(self.review().error, "partial_staging")
        self.assertEqual(tree, self.git("write-tree"))
        self.assertEqual(path.read_text(), "unstaged")

    def test_source_race_blocks_trust(self):
        with patch.dict(os.environ, {"TEST_REVIEW_MODE": "source_race", "TEST_ORIGINAL_SOURCE": str(self.root / "src/version.txt")}):
            self.assertEqual(self.review().error, "source_changed_during_review")

    def test_new_untracked_dependency_blocks_trust(self):
        with patch.dict(os.environ, {"TEST_REVIEW_MODE": "untracked_race", "TEST_ORIGINAL_SOURCE": str(self.root / "src/version.txt")}):
            self.assertEqual(self.review().error, "source_changed_during_review")

    def test_policy_scope_is_enforced(self):
        path = self.root / ".docgov/coding-agent-review-policy.json"
        policy = json.loads(path.read_text())
        policy["documents"] = []
        path.write_text(json.dumps(policy))
        self.assertEqual(self.review().error, "review_not_authorized")

    def test_source_change_invalidates_successful_review(self):
        self.assertEqual(self.review().result, "changed")
        def verify():
            return analyze_trust(build_snapshot(self.root, self.root / ".docgov/catalog.yaml"), self.root / ".docgov/ledger.jsonl", requested_paths=["docs/API.md"])
        self.assertEqual(verify().result, "pass")
        (self.root / "src/version.txt").write_text("2")
        self.assertEqual(verify().result, "action_required")

    def test_repeat_preserves_ledger_and_does_not_run_model(self):
        self.assertEqual(self.review().result, "changed")
        ledger = (self.root / ".docgov/ledger.jsonl").read_bytes()
        with patch.dict(os.environ, {"TEST_REVIEW_MODE": "failed"}):
            result = self.review()
        self.assertEqual(result.result, "pass", result.error)
        self.assertFalse(result.model_used)
        self.assertEqual((self.root / ".docgov/ledger.jsonl").read_bytes(), ledger)

    def test_delegation_revocation_invalidates_trust(self):
        self.assertEqual(self.review().result, "changed")
        path = self.root / ".docgov/coding-agent-review-policy.json"
        policy = json.loads(path.read_text()); policy["documents"] = []
        path.write_text(json.dumps(policy))
        result = analyze_trust(build_snapshot(self.root, self.root / ".docgov/catalog.yaml"),
                               self.root / ".docgov/ledger.jsonl", requested_paths=["docs/API.md"])
        self.assertEqual(result.result, "action_required")

    def test_policy_content_change_invalidates_version_two_receipt(self):
        self.assertEqual(self.review().result, "changed")
        receipt=json.loads(next((self.root/'.docgov/reviews').glob('*.json')).read_text())
        self.assertEqual(receipt['version'],2)
        path=self.root/'.docgov/coding-agent-review-policy.json'
        policy=json.loads(path.read_text());policy['new_constraint']='source only';path.write_text(json.dumps(policy))
        result=analyze_trust(build_snapshot(self.root,self.root/'.docgov/catalog.yaml'),
                             self.root/'.docgov/ledger.jsonl',requested_paths=['docs/API.md'])
        self.assertEqual(result.result,'action_required')

    def test_receipt_tampering_invalidates_trust(self):
        self.assertEqual(self.review().result, "changed")
        next((self.root / ".docgov/reviews").glob("*.json")).write_text('{}')
        result = analyze_trust(build_snapshot(self.root, self.root / ".docgov/catalog.yaml"),
                               self.root / ".docgov/ledger.jsonl", requested_paths=["docs/API.md"])
        self.assertEqual(result.result, "action_required")

    def test_stale_contract_is_promoted_only_after_review(self):
        document = self.root / 'docs/API.md'
        historical = '# API\nStatus: Current\nLast verified: 2000-01-01\nVersion 1.\n'
        document.write_text(historical)
        path = self.root / ".docgov/catalog.yaml"
        catalog = json.loads(path.read_text()); catalog['documents'][0]['status'] = 'stale'
        path.write_text(json.dumps(catalog))
        with patch.dict(os.environ, {"TEST_REVIEW_MODE": "untrusted"}):
            self.assertEqual(self.review().result, "blocked")
        self.assertEqual(build_snapshot(self.root, path).catalog.record_for('docs/API.md').status, 'stale')
        self.assertEqual(self.review().result, "changed")
        self.assertEqual(build_snapshot(self.root, path).catalog.record_for('docs/API.md').status, 'current')
        self.assertEqual(document.read_text(), historical)

    def test_mcp_rechecks_revocation_without_regenerating_trust_table(self):
        from docgov.trust_state import build_trust_state, write_trust_state
        from docgov.mcp_server import DocumentSupply, build_config
        decision = self.review()
        snapshot = build_snapshot(self.root, self.root / '.docgov/catalog.yaml')
        write_trust_state(self.root / '.docgov/trust.json', build_trust_state(decision, snapshot, ledger_path=self.root / '.docgov/ledger.jsonl'))
        supply = DocumentSupply(build_config(["--root", str(self.root)]))
        self.assertTrue(supply.document_status('docs/API.md')['usable'])
        policy_path = self.root / '.docgov/coding-agent-review-policy.json'
        policy = json.loads(policy_path.read_text()); policy['documents'] = []
        policy_path.write_text(json.dumps(policy))
        self.assertFalse(supply.document_status('docs/API.md')['usable'])
        self.assertIsNone(supply.get_document('docs/API.md')['content'])

    def test_graph_reuses_exact_review_but_reaudits_changed_or_revoked_evidence(self):
        from docgov.agents import plan_graph
        from docgov.engine import analyze
        def audit_paths():
            snapshot = build_snapshot(self.root, self.root / '.docgov/catalog.yaml')
            snapshot.changed = ['docs/API.md']
            return [node.path for node in plan_graph(snapshot, analyze(snapshot)).audits]
        self.assertIn('docs/API.md', audit_paths())
        self.assertEqual(self.review().result, 'changed')
        self.assertNotIn('docs/API.md', audit_paths())
        source = self.root / 'src/version.txt'
        source.write_text('2')
        self.assertIn('docs/API.md', audit_paths())
        source.write_text('1\n')
        policy = self.root / '.docgov/coding-agent-review-policy.json'
        payload = json.loads(policy.read_text()); payload['status'] = 'disabled'
        policy.write_text(json.dumps(payload))
        self.assertIn('docs/API.md', audit_paths())

    def test_control_policy_is_not_semantic_claim_but_baseline_blocks_survive(self):
        from docgov.agents import plan_graph, rule, GraphOutcome
        from docgov.engine import analyze
        catalog_path = self.root / '.docgov/catalog.yaml'
        catalog = json.loads(catalog_path.read_text())
        catalog['policies'] = {'control_documents': ['docs/API.md'], 'protected': ['docs/API.md']}
        catalog_path.write_text(json.dumps(catalog))
        snapshot = build_snapshot(self.root, catalog_path)
        snapshot.changed = ['docs/API.md']
        baseline = analyze(snapshot)
        self.assertTrue(baseline.findings)
        self.assertFalse(plan_graph(snapshot, baseline).audits)
        decision = rule(snapshot, baseline, GraphOutcome(), model_id='test')
        self.assertEqual(decision.findings, baseline.findings)

    def test_protected_change_requires_matching_delegated_review(self):
        from docgov.engine import analyze
        path = self.root / '.docgov/catalog.yaml'
        catalog = json.loads(path.read_text()); catalog['policies'] = {'protected': ['docs/API.md']}
        path.write_text(json.dumps(catalog))
        def protected_blocks():
            snapshot = build_snapshot(self.root, path); snapshot.changed = ['docs/API.md']
            return [f for f in analyze(snapshot).findings if f.reason.startswith('Protected legal')]
        self.assertTrue(protected_blocks())
        self.assertEqual(self.review().result, 'changed')
        self.assertFalse(protected_blocks())
        (self.root / 'src/version.txt').write_text('2')
        self.assertTrue(protected_blocks())


if __name__ == "__main__":
    unittest.main()
