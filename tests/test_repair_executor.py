import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docgov.repair_executor import repair_staged
from docgov.agent import govern
from docgov.catalog import Catalog
from docgov.engine import RepositorySnapshot


class IsolatedRepairTests(unittest.TestCase):
    def setUp(self):
        # Each fixture is an independent repository, even when the suite is a verifier child.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("DOCGOV_REPAIR_ACTIVE", None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        for name, content in {
            ".gitignore": "__pycache__/\n",
            ".docgov/catalog.yaml": json.dumps({"version": 1, "documents": [{
                "path": "docs/API.md", "type": "contract", "status": "stale", "depends_on": ["src/**"]}],
                "policies": {"auto_repair_documents": ["docs/API.md"]}}),
            ".docgov/ledger.jsonl": "",
            "docs/API.md": "# API\nStatus: stale\n\nVersion 1.\n",
            "src/version.txt": "1\n",
            "unrelated.txt": "original\n",
        }.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        (self.root / "src/version.txt").write_text("2\n")
        self.git("add", "src/version.txt")
        self.script = Path(self.temp.name) / "executor.py"
        self.script.write_text("from pathlib import Path\np=Path('docs/API.md')\np.write_text(p.read_text().replace('Version 1.', 'Version '+Path('src/version.txt').read_text().strip()+'.'))\n")
        self.command = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.script))}"

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def planner(self, plan):
        return {node.node_id: json.dumps({"path": node.path,
                  "instructions": ["Match the interface version to source."],
                  "evidence_paths": list(node.changed_sources), "needs_human": False,
                  "reason": "The source version changed."}) for node in plan.nodes}, [
                      {"event": "agent_complete", "name": node.node_id} for node in plan.nodes]

    def run_repair(self, **kwargs):
        args = dict(enable_model=True, planner_runner=self.planner, executor_command=self.command,
                    verify_command=f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(0)' ")
        args.update(kwargs)
        return repair_staged(self.root, **args)

    def unchanged_after(self, **kwargs):
        tree = self.git("write-tree")
        content = (self.root / "docs/API.md").read_bytes()
        result = self.run_repair(**kwargs)
        self.assertEqual(result["result"], "blocked", result)
        self.assertEqual(self.git("write-tree"), tree)
        self.assertEqual((self.root / "docs/API.md").read_bytes(), content)
        return result

    def test_recursive_repair_stops_before_execution(self):
        with patch.dict(os.environ, {"DOCGOV_REPAIR_ACTIVE": "1"}):
            result = self.run_repair(planner_runner=lambda _: self.fail("recursive planner"))
        self.assertEqual(result["error_code"], "recursive_repair")

    def test_staged_source_only_and_preserves_other_work(self):
        (self.root / "src/version.txt").write_text("3\n")
        (self.root / "unrelated.txt").write_text("user work\n")
        result = self.run_repair()
        self.assertEqual(result["result"], "changed", result)
        self.assertTrue(result["model_used"])
        self.assertIn("Version 2.", self.git("show", ":docs/API.md"))
        self.assertEqual((self.root / "src/version.txt").read_text(), "3\n")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "user work\n")
        self.assertEqual(self.git("show", ":.docgov/ledger.jsonl"), "")
        self.assertFalse((self.root / ".docgov/trust.json").exists())

    def test_repeat_does_not_stage_extra_changes(self):
        self.assertEqual(self.run_repair()["result"], "changed")
        tree = self.git("write-tree")
        self.assertEqual(self.run_repair()["result"], "pass")
        self.assertEqual(self.git("write-tree"), tree)

    def test_empty_diff_does_not_call_model(self):
        self.git("reset", "--hard", "HEAD")
        result = self.run_repair(planner_runner=lambda _: self.fail("unexpected model call"))
        self.assertEqual(result["result"], "pass")
        self.assertTrue(result["model_requested"])
        self.assertFalse(result["model_used"])

    def test_partially_staged_document_blocks(self):
        (self.root / "docs/API.md").write_text("unfinished user edit\n")
        self.assertEqual(self.unchanged_after()["error_code"], "target_has_unstaged_changes")

    def test_executor_outside_allowlist_blocks(self):
        self.script.write_text("from pathlib import Path\nPath('src/version.txt').write_text('999')\n")
        self.assertEqual(self.unchanged_after()["error_code"], "executor_modified_outside_targets")

    def test_executor_cannot_change_verification_metadata(self):
        self.script.write_text("from pathlib import Path\np=Path('docs/API.md');p.write_text(p.read_text().replace('stale','current'))\n")
        self.assertEqual(self.unchanged_after()["error_code"], "verification_metadata_modified")

    def test_bold_verification_date_cannot_be_refreshed(self):
        p = self.root / "docs/API.md"
        p.write_text(p.read_text() + "\n**Last verified:** 2026-01-01\n")
        self.git("add", "docs/API.md")
        self.script.write_text("from pathlib import Path\np=Path('docs/API.md');p.write_text(p.read_text().replace('2026-01-01','2026-09-07'))\n")
        self.assertEqual(self.unchanged_after()["error_code"], "verification_metadata_modified")

    def test_executor_failure_blocks_without_leaking_output(self):
        self.script.write_text("import sys\nprint('PRIVATE_DOCUMENT_TEXT')\nsys.exit(1)\n")
        result = self.unchanged_after()
        self.assertEqual(result["error_code"], "executor_failed")
        self.assertNotIn("PRIVATE_DOCUMENT_TEXT", json.dumps(result))

    def test_verification_failure_never_publishes(self):
        self.assertEqual(self.unchanged_after(verify_command=f"{sys.executable} -c 'exit(1)'")["error_code"], "verification_failed")

    def test_verifier_cannot_stage_a_repair(self):
        result = self.unchanged_after(verify_command="git add docs/API.md")
        self.assertEqual(result["error_code"], "verification_modified_git_state")

    def test_source_modified_during_planning_blocks(self):
        def planner(plan):
            (self.root / "src/version.txt").write_text("concurrent work\n")
            return self.planner(plan)
        self.assertEqual(self.unchanged_after(planner_runner=planner)["error_code"], "source_changed_during_repair")
        self.assertEqual((self.root / "src/version.txt").read_text(), "concurrent work\n")

    def test_aws_verification_failure_has_actionable_code(self):
        def denied(_):
            raise RuntimeError("Your account is currently being verified.")
        result = self.unchanged_after(planner_runner=denied)
        self.assertEqual(result["error_code"], "aws_account_verification_pending")
        self.assertFalse(result["model_used"])

    def test_rejected_plan_never_calls_executor(self):
        def bad(plan):
            responses, trace = self.planner(plan)
            for name, raw in responses.items():
                value = json.loads(raw); value["evidence_paths"] = ["unrelated.txt"]
                responses[name] = json.dumps(value)
            return responses, trace
        self.assertEqual(self.unchanged_after(planner_runner=bad)["error_code"], "model_plan_failed")

    def test_protected_document_is_not_a_repair_candidate(self):
        p = self.root / ".docgov/catalog.yaml"
        value = json.loads(p.read_text()); value["policies"]["protected"] = ["docs/API.md"]
        p.write_text(json.dumps(value)); self.git("add", str(p))
        result = self.run_repair(planner_runner=lambda _: self.fail("protected planner"))
        self.assertEqual(result["result"], "pass")
        self.assertFalse(result["model_used"])

    def test_model_disabled_cannot_silently_repair(self):
        self.assertEqual(self.unchanged_after(enable_model=False)["error_code"], "model_required")

    def test_index_lock_is_preserved(self):
        lock = self.root / ".git/index.lock"
        # Acquire after planning, not before Git reads the initial index.
        def planner(plan):
            lock.write_text("other writer")
            return self.planner(plan)
        result = self.run_repair(planner_runner=planner)
        self.assertEqual(result["result"], "blocked")
        self.assertEqual(lock.read_text(), "other writer")

    def test_governance_failure_does_not_claim_successful_model_use(self):
        snapshot = RepositorySnapshot(root=self.root, catalog=Catalog.default())
        with patch("docgov.agent.run_graph", side_effect=RuntimeError("denied")):
            result = govern(snapshot, mode="review", enable_model=True)
        self.assertEqual(result.result, "blocked")
        self.assertTrue(result.model_requested)
        self.assertFalse(result.model_used)


if __name__ == "__main__":
    unittest.main()
