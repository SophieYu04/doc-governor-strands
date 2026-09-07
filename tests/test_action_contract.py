from __future__ import annotations

import re
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from docgov.github import MARKER, report_markdown, upsert_check_run, upsert_pr_comment
from docgov.models import GovernanceDecision


ROOT = Path(__file__).resolve().parents[1]


class ActionContractTests(unittest.TestCase):
    def test_composite_action_exposes_stable_outputs_and_commits_only_modified_paths(self) -> None:
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            set(action["outputs"]),
            {"result", "changed", "finding_count", "decision_json", "commit_sha"},
        )
        source = (ROOT / "action.yml").read_text(encoding="utf-8")
        self.assertIn('json.load(handle).get("modified_paths", [])', source)
        self.assertNotIn("git add .", source)
        self.assertIn('if [ "$result" = "action_required" ] || [ "$result" = "blocked" ]', source)
        self.assertIn("supabase_projects", action["inputs"])
        self.assertIn("supabase_evidence_dir", action["inputs"])
        self.assertIn('--supabase-projects "$DOCGOV_SUPABASE_PROJECTS"', source)
        self.assertEqual(action["inputs"]["enable_model"]["default"], "false")
        self.assertEqual(action["inputs"]["model_id"]["default"], "us.amazon.nova-lite-v1:0")

    def test_workflows_pin_actions_and_never_use_pull_request_target(self) -> None:
        workflow_paths = list((ROOT / ".github/workflows").glob("*.yml"))
        workflow_sources = [path.read_text(encoding="utf-8") for path in workflow_paths]
        combined = "\n".join(workflow_sources)
        self.assertNotIn("pull_request_target", combined)
        for path in workflow_paths:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    self.assertIsInstance(step, dict, path)
                    self.assertTrue("run" in step or "uses" in step, (path, step))
        for use in re.findall(r"uses:\s*([^\s#]+)", combined + "\n" + (ROOT / "action.yml").read_text(encoding="utf-8")):
            if use == "./":
                continue
            self.assertRegex(use, r"^[^@]+@[0-9a-f]{40}$")
        self.assertEqual(combined.count("uses: ./"), 4)

    def test_fork_model_and_oidc_steps_are_same_repository_only(self) -> None:
        source = (ROOT / ".github/workflows/docgov-review.yml").read_text(encoding="utf-8")
        same_repo = "github.event.pull_request.head.repo.full_name == github.repository"
        self.assertGreaterEqual(source.count(same_repo), 2)
        self.assertIn(f"persist-credentials: ${{{{ {same_repo} }}}}", source)
        self.assertIn("vars.DOCGOV_AWS_ROLE_ARN != ''", source)
        self.assertIn("vars.DOCGOV_ENABLE_MODEL == 'true'", source)
        self.assertIn("id-token: write", source)

    def test_daily_audit_branches_from_the_checked_out_commit(self) -> None:
        source = (ROOT / ".github/workflows/docgov-audit.yml").read_text(encoding="utf-8")
        self.assertIn("base_sha=$(git rev-parse HEAD)", source)
        self.assertIn('git checkout -B "$BRANCH" "$base_sha"', source)
        self.assertNotIn('git checkout -B "$BRANCH" "$GITHUB_SHA"', source)

    def test_source_reconciliation_is_commit_triggered_and_stages_only_safe_changes(self) -> None:
        source = (ROOT / ".github/workflows/docgov-source-reconcile.yml").read_text(encoding="utf-8")
        workflow = yaml.safe_load(source)
        self.assertEqual(workflow[True]["push"]["branches"], ["main"])
        self.assertEqual(
            workflow[True]["push"]["paths"],
            ["**/supabase/config.toml", "**/supabase/functions/**"],
        )
        self.assertIn("BRANCH: docgov/source-reconcile", source)
        self.assertIn("enable_model: false", source)
        self.assertIn('json.load(handle).get("modified_paths", [])', source)
        self.assertNotIn("git add .", source)
        self.assertIn('gh pr create --base main', source)

    def test_ci_accepts_only_approved_or_expected_pending_trust_state(self) -> None:
        source = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        self.assertIn("Verify repository trust state", source)
        self.assertIn('test "$exit_code" -eq 0 -o "$exit_code" -eq 2', source)
        self.assertIn('result["result"] == "pass" and not findings', source)
        self.assertIn('{("README.md", "block")}', source)

    def test_pre_commit_hook_delegates_to_the_atomic_repair_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            command = root / "docgov-stub"
            command.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit 2\n")
            command.chmod(0o755)
            env = os.environ.copy()
            env.pop("DOCGOV_REPAIR_ACTIVE", None)
            env["DOCGOV_BIN"] = str(command)
            result = subprocess.run(["sh", str(ROOT / ".githooks/pre-commit")], cwd=root,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout.splitlines()[-2:], ["repair", "--json"])
            self.assertFalse((root / ".docgov").exists())

    def test_aws_templates_are_repository_scoped_and_cover_profile_destinations(self) -> None:
        trust = (ROOT / "infra/aws/github-oidc-trust-policy.json").read_text(encoding="utf-8")
        policy = (ROOT / "infra/aws/bedrock-inference-policy.json").read_text(encoding="utf-8")
        self.assertIn('"<GITHUB_SUB_CLAIM_PREFIX>:*"', trust)
        self.assertNotIn("repo:SophieYu04/", trust)
        self.assertIn('"bedrock:InferenceProfileArn"', policy)
        for region in ("us-east-1", "us-east-2", "us-west-2"):
            self.assertIn(
                f"arn:aws:bedrock:{region}::foundation-model/amazon.nova-lite-v1:0",
                policy,
            )

    def test_decision_card_comment_is_updated_instead_of_duplicated(self) -> None:
        decision = GovernanceDecision("run", "review", "pass", False)
        existing = [{"id": 17, "body": MARKER, "user": {"type": "Bot"}}]
        with patch("docgov.github._request", side_effect=[existing, None]) as request:
            upsert_pr_comment(decision, "owner/repo", "3", "token")
        self.assertEqual(request.call_args_list[1].args[0], "PATCH")
        self.assertEqual(
            request.call_args_list[1].args[1],
            "https://api.github.com/repos/owner/repo/issues/comments/17",
        )

    def test_check_run_is_looked_up_by_commit_ref_before_update(self) -> None:
        decision = GovernanceDecision("run", "review", "pass", False)
        existing = {"check_runs": [{"id": 23, "name": "Doc Governor"}]}
        with patch("docgov.github._request", side_effect=[existing, None]) as request:
            upsert_check_run(decision, "owner/repo", "abc123", "token")
        self.assertEqual(request.call_args_list[0].args[:3], (
            "GET",
            "https://api.github.com/repos/owner/repo/commits/abc123/check-runs"
            "?check_name=Doc%20Governor&filter=latest&per_page=100",
            "token",
        ))
        self.assertEqual(request.call_args_list[1].args[:3], (
            "PATCH",
            "https://api.github.com/repos/owner/repo/check-runs/23",
            "token",
        ))

    def test_check_run_is_created_when_the_commit_has_no_existing_report(self) -> None:
        decision = GovernanceDecision("run", "review", "pass", False)
        with patch("docgov.github._request", side_effect=[{"check_runs": []}, None]) as request:
            upsert_check_run(decision, "owner/repo", "abc123", "token")
        self.assertEqual(request.call_args_list[1].args[:3], (
            "POST",
            "https://api.github.com/repos/owner/repo/check-runs",
            "token",
        ))

    def test_blocked_model_error_is_visible_in_decision_card(self) -> None:
        decision = GovernanceDecision("run", "review", "blocked", False, error="model timeout")
        report = report_markdown(decision)
        self.assertIn("**Error:** model timeout", report)
        self.assertNotIn("No documentation drift", report)

    def test_init_result_uses_the_stable_action_result_enum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [sys.executable, "-m", "docgov", "--root", temporary, "--json", "init"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
        payload = yaml.safe_load(completed.stdout)
        self.assertEqual(payload["result"], "pass")
        self.assertTrue(payload["proposal_only"])


if __name__ == "__main__":
    unittest.main()
