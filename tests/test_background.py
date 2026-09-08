"""Integration tests use explicit planner/executor/reviewer doubles, never real models."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from docgov.background import Worker, atomic_json, control_hash, private_dir
from docgov.coding_review import review_documents
from docgov.engine import build_snapshot
from docgov.install import install, enqueue
from docgov.mcp_server import DocumentSupply, build_config
from docgov.repair_executor import repair_staged
from docgov.trust_state import build_trust_state, write_trust_state
from tests.test_repair_executor import IsolatedRepairTests


class BackgroundTests(unittest.TestCase):
    git = IsolatedRepairTests.git
    planner = IsolatedRepairTests.planner

    def setUp(self):
        IsolatedRepairTests.setUp(self)
        p = self.root / '.docgov/catalog.yaml'
        catalog = json.loads(p.read_text())
        catalog['documents'][0]['status'] = 'current'
        p.write_text(json.dumps(catalog))
        policy = self.root / '.docgov/coding-agent-review-policy.json'
        policy.write_text(json.dumps(dict(version=1, reviewer='coding_agent', status='enabled', documents=['docs/API.md'])))
        self.git('add', '.')
        self.git('commit', '-qm', 'source changed without docs')
        self.binary = Path(self.temp.name) / 'reviewer'
        self.binary.write_text('#!' + sys.executable + '\n' + '''
import json, sys
from pathlib import Path
schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
sid=schema['properties']['snapshot_id']['enum'][0]
# Deliberate local test double; compare the fixture's source and document.
source=Path('src/version.txt').read_text().strip()
trusted=('Version '+source+'.') in Path('docs/API.md').read_text()
result={'snapshot_id':sid,'documents':[{'path':'docs/API.md','verdict':'trusted' if trusted else 'untrusted',
'evidence_paths':['src/version.txt'],'unresolved_claims':[] if trusted else ['version mismatch']}]}
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'thread.started','thread_id':'TEST_DOUBLE'}))
print(json.dumps({'type':'turn.completed','usage':{'output_tokens':10}}))
''')
        self.binary.chmod(0o755)
        self.config = dict(documents=['docs/API.md'],
                           controls={p:control_hash(self.root,p) for p in ['.docgov/catalog.yaml','.docgov/coding-agent-review-policy.json']},
                           executor_command=self.command, verify_command=shlex.join([sys.executable,'-c','pass']),
                           codex_binary=str(self.binary))
        atomic_json(private_dir(self.root) / 'config.json', self.config)
        self.worker = Worker(self.root)
        self.addCleanup(self.worker.db.close)
        self.calls = []
        self.mock = patch.object(self.worker, 'execute', side_effect=self.execute)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def execute(self, directory, phase, timeout):
        self.calls.append(phase)
        job = json.loads((directory / 'job.json').read_text())['job']
        root = directory / 'repo'
        if phase == 'repair':
            result = repair_staged(root, enable_model=True, planner_runner=self.planner,
                                   executor_command=self.command, verify_command=self.config['verify_command'],
                                   source_paths=job['sources'], target_paths={job['target']})
        else:
            subprocess.check_call(['git','add','-A'],cwd=root)
            decision=review_documents(root,[job['target']],verify_command=self.config['verify_command'],codex_binary=str(self.binary))
            result=decision.to_dict()
            result['error_code']=decision.error
            if decision.result != 'blocked':
                write_trust_state(root/'.docgov/trust.json',build_trust_state(decision,build_snapshot(root,root/'.docgov/catalog.yaml'),ledger_path=root/'.docgov/ledger.jsonl'))
        atomic_json(directory/(phase+'.json'),result)
        return result

    def tick(self):
        self.worker.discover()
        for job in self.worker.jobs():
            job['ready_at']=0
            self.worker.save(job)
        self.worker.tick()

    def finish(self):
        self.tick()
        self.tick()
        self.assertEqual(self.worker.jobs()[-1]['state'],'complete',self.worker.jobs())

    def test_committed_source_is_repaired_reviewed_and_supplied_without_staging(self):
        tree=self.git('write-tree')
        self.finish()
        self.assertEqual(tree,self.git('write-tree'))
        self.assertIn('Version 2.',(self.root/'docs/API.md').read_text())
        supply=DocumentSupply(build_config(['--root',str(self.root)]))
        self.assertEqual(supply.get_document('docs/API.md')['code'],'ok')
        self.assertEqual(self.calls,['repair','review'])
        ledger=(self.root/'.docgov/ledger.jsonl').read_bytes()
        self.tick()
        self.assertEqual(ledger,(self.root/'.docgov/ledger.jsonl').read_bytes())
        self.assertEqual(self.calls,['repair','review'])

    def test_partial_staging_and_unrelated_work_preserved(self):
        (self.root/'src/version.txt').write_text('3\n')
        self.git('add','src/version.txt')
        (self.root/'src/version.txt').write_text('4\n')
        (self.root/'unrelated.txt').write_text('USER WORK')
        tree=self.git('write-tree')
        self.finish()
        self.assertEqual(tree,self.git('write-tree'))
        self.assertIn('Version 4.',(self.root/'docs/API.md').read_text())
        self.assertEqual((self.root/'unrelated.txt').read_text(),'USER WORK')
        self.assertEqual(self.git('show',':src/version.txt'),'3')

    def test_source_race_never_publishes(self):
        self.tick()
        (self.root/'src/version.txt').write_text('3\n')
        self.tick()
        self.assertIn('Version 1.',(self.root/'docs/API.md').read_text())
        self.tick()
        self.assertIn('Version 3.',(self.root/'docs/API.md').read_text())

    def test_document_edit_during_review_preserved(self):
        self.tick()
        (self.root/'docs/API.md').write_text('USER DOCUMENT')
        self.tick()
        self.assertEqual((self.root/'docs/API.md').read_text(),'USER DOCUMENT')
        self.assertEqual((self.root/'.docgov/ledger.jsonl').read_text(),'')

    def test_timeout_retries_only_failed_phase(self):
        self.tick()
        actual=self.execute
        def timeout(directory,phase,budget):
            return dict(result='blocked',error_code='command_timeout')
        self.worker.execute.side_effect=timeout
        self.tick()
        self.assertEqual(self.worker.jobs()[0]['attempts'],1)
        self.worker.execute.side_effect=actual
        self.tick()
        self.assertEqual(self.calls,['repair','review'])
        self.assertEqual(self.worker.jobs()[0]['state'],'complete')

    def test_persistent_failure_notifies_once_and_has_no_trust(self):
        self.worker.execute.side_effect=lambda *args:dict(result='blocked',error_code='command_timeout')
        for _ in range(5): self.tick()
        self.assertEqual(self.worker.jobs()[0]['state'],'failed')
        self.assertEqual(len(list(self.worker.home.glob('notice-*.json'))),1)
        self.assertEqual((self.root/'.docgov/ledger.jsonl').read_text(),'')

    def test_restart_retains_completed_repair(self):
        self.tick()
        self.worker.db.close()
        self.mock.stop()
        self.worker=Worker(self.root)
        self.addCleanup(self.worker.db.close)
        self.worker.execute=self.execute
        self.tick()
        self.assertEqual(self.calls,['repair','review'])
        self.assertEqual(self.worker.jobs()[0]['state'],'complete')

    def test_forged_authorization_is_refused(self):
        p=self.root/'.docgov/coding-agent-review-policy.json'
        value=json.loads(p.read_text());value['documents'].append('docs/OTHER.md');p.write_text(json.dumps(value))
        with self.assertRaisesRegex(Exception,'installed_policy_changed'): self.tick()
        self.assertEqual(self.calls,[])

    def test_precommit_stages_only_verified_outputs(self):
        self.finish()
        before=self.git('show',':unrelated.txt')
        (self.root/'unrelated.txt').write_text('UNSTAGED USER WORK')
        self.worker.stage_ready()
        self.assertIn('Version 2.',self.git('show',':docs/API.md'))
        self.assertEqual(self.git('show',':unrelated.txt'),before)
        self.assertIn('.docgov/trust.json',self.git('diff','--cached','--name-only'))

    def test_precommit_does_not_stage_when_source_is_partially_staged(self):
        (self.root/'src/version.txt').write_text('3\n')
        self.git('add','src/version.txt')
        (self.root/'src/version.txt').write_text('4\n')
        self.finish()
        before=self.git('write-tree')
        self.worker.stage_ready()
        self.assertEqual(before,self.git('write-tree'))

    def test_preexisting_unstaged_document_draft_is_not_staged(self):
        path=self.root/'docs/API.md'
        path.write_text(path.read_text()+'\nUser-owned draft paragraph.\n')
        tree=self.git('write-tree')
        self.finish()
        self.worker.stage_ready()
        self.assertEqual(tree,self.git('write-tree'))
        self.assertIn('User-owned draft paragraph.',path.read_text())

    def test_partially_staged_document_draft_is_not_staged(self):
        path=self.root/'docs/API.md'
        path.write_text(path.read_text()+'\nStaged user paragraph.\n')
        self.git('add','docs/API.md')
        path.write_text(path.read_text()+'\nUnstaged user paragraph.\n')
        tree=self.git('write-tree')
        self.finish()
        self.worker.stage_ready()
        self.assertEqual(tree,self.git('write-tree'))
        self.assertIn('Unstaged user paragraph.',path.read_text())

    def test_owned_generated_artifacts_can_be_staged_on_later_repair(self):
        self.finish()
        self.script.write_text(self.script.read_text().replace("'Version 1.'", "'Version 2.'"))
        (self.root/'src/version.txt').write_text('3\n')
        self.git('add','src/version.txt')
        self.tick()
        self.tick()
        self.worker.stage_ready()
        self.assertIn('Version 3.',self.git('show',':docs/API.md'))
        for receipt in (self.root/'.docgov/reviews').glob('*.json'):
            self.assertTrue(self.git('show',':'+receipt.relative_to(self.root).as_posix()))

    def test_precommit_does_not_stage_user_document_edits(self):
        self.finish()
        (self.root/'docs/API.md').write_text('USER EDIT')
        before=self.git('write-tree')
        self.worker.stage_ready()
        self.assertEqual(before,self.git('write-tree'))

    def test_enqueue_does_not_run_models_or_verifier(self):
        enqueue(self.root)
        self.assertTrue((self.worker.home/'wake').exists())
        self.assertEqual(self.calls,[])

    def test_installer_preserves_hook_and_codex_configuration(self):
        original=self.root/'.git/hooks/pre-commit'
        original.write_text('#!/bin/sh\nprintf old-hook > hook-ran\nexit 7\n');original.chmod(0o755)
        # No preexisting installation record in this install fixture.
        (self.worker.home/'config.json').unlink()
        with patch('docgov.install.preflight'):
            result=install(self.root,verify_command=self.config['verify_command'],start=False)
            install(self.root,verify_command=self.config['verify_command'],start=False)
        self.assertFalse(result['worker_started'])
        hook=self.worker.home/'hooks/pre-commit'
        run=subprocess.run([str(hook)],cwd=self.root)
        self.assertEqual(run.returncode,7)
        self.assertEqual((self.root/'hook-ran').read_text(),'old-hook')
        self.assertIn('exit 7',original.read_text())
        import tomllib
        config=tomllib.loads((self.root/'.codex/config.toml').read_text())
        self.assertIn('docgov',config['mcp_servers'])
        self.assertIn('get_document',config['developer_instructions'])


if __name__=='__main__': unittest.main()
