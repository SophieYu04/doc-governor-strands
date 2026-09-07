from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from docgov.agent import _confine_model_finding, govern
from docgov.agents import (
    ALL_SPECS,
    CONFLICT_RESOLVER,
    CONTRACT_DRAFTER,
    EVIDENCE_AUDITOR,
    FORBIDDEN_TOOL_TOKENS,
    AgentContractError,
    GraphPlan,
    PrivilegeError,
    assert_read_only,
    build_drafter,
    declared_source_payload,
    evidence_payload,
    interpret,
    parse_conflict_ruling,
    parse_draft_patch,
    parse_evidence_verdict,
    parse_json_object,
    plan_graph,
    redact_self_assessment,
    rule,
    run_graph,
)
from docgov.catalog import Catalog
from docgov.drafting import factual_tokens, validate_draft
from docgov.engine import RepositorySnapshot, analyze, apply_safe_actions, build_snapshot
from docgov.models import Evidence, Finding, GovernanceDecision


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


API_DOC = """# API Contract

Status: Current
last_verified_at: 2026-09-01

The `healthCheck` endpoint returns readiness.
"""

API_SOURCE = "export const healthCheck = () => 'ok';\nexport const sendEmail = () => true;\n"


class PrivilegeSeparationTests(unittest.TestCase):
    """The point of three agents is that each one can do less than the last (P5)."""

    def test_no_agent_declares_a_tool_that_could_mutate_or_reach_the_network(self) -> None:
        for spec in ALL_SPECS:
            with self.subTest(agent=spec.identifier):
                assert_read_only(spec)
                for name in spec.tool_names:
                    for token in FORBIDDEN_TOOL_TOKENS:
                        self.assertNotIn(token, name.lower())

    def test_each_agent_declares_exactly_the_tool_scope_the_spec_gives_it(self) -> None:
        self.assertEqual(EVIDENCE_AUDITOR.tool_names, ("evidence_for_document", "GovernanceOutput"))
        self.assertEqual(CONFLICT_RESOLVER.tool_names, ("declared_source", "GovernanceOutput"))
        self.assertEqual(CONTRACT_DRAFTER.tool_names, ("target_document", "declared_source", "GovernanceOutput"))

    def test_an_auditor_that_could_reach_a_write_tool_is_a_failure(self) -> None:
        rogue = EVIDENCE_AUDITOR.__class__(
            identifier="rogue_auditor",
            role="Rogue",
            tool_names=("evidence_for_document", "write_document"),
            max_tool_calls=1,
            document_types=(),
            system_prompt="",
        )
        with self.assertRaises(PrivilegeError):
            assert_read_only(rogue)

    def test_only_the_drafter_is_restricted_to_contract_documents(self) -> None:
        self.assertEqual(CONTRACT_DRAFTER.document_types, ("contract",))
        for document_type in ("state", "evidence", "decision", "procedure"):
            self.assertFalse(CONTRACT_DRAFTER.permits(document_type))
        self.assertTrue(CONTRACT_DRAFTER.permits("contract"))
        self.assertTrue(EVIDENCE_AUDITOR.permits("state"))

    def test_the_auditor_never_sees_a_document_s_own_assessment_of_itself(self) -> None:
        redacted = redact_self_assessment(
            "# API\n\nStatus: Current\nlast_verified_at: 2026-09-01\n"
            "<!-- docgov:supabase-inventory {\"functions\":[]} -->\n\nThe endpoint works.\n"
        )
        self.assertNotIn("Status:", redacted)
        self.assertNotIn("last_verified_at", redacted)
        self.assertNotIn("docgov:", redacted)
        self.assertIn("The endpoint works.", redacted)


class GraphPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        write(self.catalog_path, json.dumps({
            "version": 1,
            "taxonomy": {
                "contract": ["docs/architecture/**"],
                "state": ["docs/status/**"],
                "evidence": ["docs/evidence/**"],
                "decision": ["docs/decisions/**"],
                "procedure": ["docs/operations/**"],
            },
            "documents": [
                {
                    "path": "docs/architecture/API.md",
                    "type": "contract",
                    "status": "current",
                    "depends_on": ["src/**"],
                },
                {"path": "docs/status/RELEASE.md", "type": "state", "status": "current"},
                {"path": "docs/evidence/SCAN.md", "type": "evidence", "status": "current"},
                {"path": "docs/decisions/ADR-1.md", "type": "decision", "status": "current"},
            ],
            "policies": {"auto_remove_new_duplicates": True, "protected": ["docs/legal/**"]},
        }))
        write(self.root / "docs/architecture/API.md", API_DOC)
        write(self.root / "docs/status/RELEASE.md", "# Release\n\nShipped.\n")
        write(self.root / "docs/evidence/SCAN.md", "# Scan\n")
        write(self.root / "docs/decisions/ADR-1.md", "# ADR 1\n")
        write(self.root / "src/api.ts", API_SOURCE)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        self.snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        self.baseline = analyze(self.snapshot, mode="review")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_drafter_cannot_be_constructed_for_a_state_document(self) -> None:
        for path in (
            "docs/status/RELEASE.md",
            "docs/evidence/SCAN.md",
            "docs/decisions/ADR-1.md",
        ):
            with self.subTest(path=path):
                with self.assertRaises(PrivilegeError):
                    build_drafter(self.snapshot, path)

    def test_the_drafter_can_be_constructed_for_a_contract_document(self) -> None:
        node = build_drafter(self.snapshot, "docs/architecture/API.md")
        self.assertEqual(node.path, "docs/architecture/API.md")
        self.assertEqual(node.depends_on, ("src/**",))

    def test_the_drafter_is_refused_for_protected_or_human_approved_contracts(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["documents"].append({
            "path": "docs/architecture/PUBLIC.md",
            "type": "contract",
            "status": "current",
            "approval": "human",
            "depends_on": ["src/**"],
        })
        write(self.catalog_path, json.dumps(catalog))
        write(self.root / "docs/architecture/PUBLIC.md", "# Public\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "public"], cwd=self.root, check=True)
        snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)
        with self.assertRaises(PrivilegeError):
            build_drafter(snapshot, "docs/architecture/PUBLIC.md")

    def test_the_plan_bounds_how_many_documents_reach_the_model(self) -> None:
        plan = plan_graph(self.snapshot, self.baseline)
        self.assertLessEqual(len(plan.audits), 8)
        for node in plan.audits:
            self.assertNotIn("last_verified_at", node.claims)

    def test_evidence_payload_carries_hashes_but_no_previous_conclusion(self) -> None:
        payload = evidence_payload(self.snapshot, "docs/architecture/API.md")
        self.assertTrue(payload["known"])
        self.assertTrue(payload["dependency_fingerprint"])
        serialized = json.dumps(payload)
        self.assertNotIn("reason", serialized)
        self.assertNotIn("Status: Current", serialized)

    def test_declared_source_refuses_a_file_the_document_never_declared(self) -> None:
        allowed = declared_source_payload(self.snapshot, ["src/**"], "src/api.ts")
        self.assertTrue(allowed["readable"])
        refused = declared_source_payload(self.snapshot, ["src/**"], "docs/status/RELEASE.md")
        self.assertFalse(refused["readable"])
        self.assertNotIn("content", refused)


class ResponseContractTests(unittest.TestCase):
    def test_malformed_json_fails_closed(self) -> None:
        with self.assertRaises(AgentContractError):
            parse_json_object("not json at all")
        with self.assertRaises(AgentContractError):
            parse_json_object('{"unbalanced": ')

    def test_a_verdict_about_another_document_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            parse_evidence_verdict(
                {"path": "docs/status/RELEASE.md", "supported": True, "confidence": "high", "reason": "x"},
                expected_path="docs/architecture/API.md",
            )

    def test_a_verdict_missing_its_confidence_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            parse_evidence_verdict(
                {"supported": True, "reason": "x"}, expected_path="docs/architecture/API.md"
            )

    def test_a_ruling_outside_the_conflict_is_rejected(self) -> None:
        with self.assertRaises(AgentContractError):
            parse_conflict_ruling(
                {
                    "subject": "api",
                    "canonical_path": "docs/architecture/ELSEWHERE.md",
                    "superseded_paths": [],
                    "reason": "x",
                },
                allowed_paths=["docs/architecture/API.md", "docs/architecture/COPY.md"],
            )

    def test_a_drafter_declining_is_a_valid_answer(self) -> None:
        self.assertIsNone(parse_draft_patch(
            {"path": "docs/architecture/API.md", "proposed_span": "  "},
            expected_path="docs/architecture/API.md",
        ))


class DraftGroundingTests(unittest.TestCase):
    """A draft that cannot be proven is discarded whole, never partially applied (P3)."""

    def _validate(self, proposed: str, **overrides):
        kwargs = dict(
            document_path="docs/architecture/API.md",
            document_type="contract",
            declared_dependencies=["src/**"],
            current_text="# API\n\nThe old sentence.\n",
            original_span="The old sentence.",
            proposed_span=proposed,
            cited_sources={"src/api.ts": API_SOURCE},
        )
        kwargs.update(overrides)
        return validate_draft(**kwargs)

    def test_a_grounded_span_is_accepted(self) -> None:
        result = self._validate("The `healthCheck` and `sendEmail` functions are exported.")
        self.assertTrue(result.ok, result.reason)

    def test_an_invented_function_name_discards_the_whole_draft(self) -> None:
        result = self._validate("The `processPayment` function is exported.")
        self.assertFalse(result.ok)
        self.assertIn("processPayment", result.ungrounded_tokens)

    def test_a_date_is_never_derivable_from_source(self) -> None:
        self.assertFalse(self._validate("Checked on 2026-09-01 the `healthCheck` runs.").ok)

    def test_a_version_number_is_never_derivable_from_source(self) -> None:
        self.assertFalse(self._validate("Serves `healthCheck` since 2.1.0.").ok)

    def test_a_verification_claim_is_never_derivable_from_source(self) -> None:
        self.assertFalse(self._validate("The `healthCheck` path was verified in staging.").ok)

    def test_a_source_outside_depends_on_cannot_be_cited(self) -> None:
        result = self._validate(
            "The `healthCheck` function is exported.",
            cited_sources={"secrets/keys.ts": "export const healthCheck = 1;"},
        )
        self.assertFalse(result.ok)
        self.assertIn("does not declare", result.reason)

    def test_a_non_contract_document_is_refused_before_anything_else(self) -> None:
        self.assertFalse(self._validate("The `healthCheck` runs.", document_type="state").ok)

    def test_an_ambiguous_original_span_is_refused(self) -> None:
        result = self._validate(
            "The `healthCheck` runs.",
            current_text="repeat\nrepeat\n",
            original_span="repeat",
        )
        self.assertFalse(result.ok)

    def test_a_near_miss_identifier_is_not_grounded_by_a_longer_one(self) -> None:
        result = self._validate(
            "Call `get_user` to load the record.",
            cited_sources={"src/api.ts": "export function forget_user_id() {}"},
        )
        self.assertFalse(result.ok)
        self.assertIn("get_user", result.ungrounded_tokens)

    def test_invented_routes_limits_and_ports_are_factual_tokens(self) -> None:
        result = self._validate(
            "Send a POST to /refunds; the limit is 500 requests per minute and the port is 8443."
        )
        self.assertFalse(result.ok)
        self.assertIn("/refunds", result.ungrounded_tokens)
        self.assertIn("500", result.ungrounded_tokens)

    def test_token_extraction_covers_code_shaped_prose_only(self) -> None:
        tokens = factual_tokens("Call `healthCheck` at supabase/functions/health-check/index.ts now.")
        self.assertIn("healthCheck", tokens)
        self.assertIn("supabase/functions/health-check/index.ts", tokens)
        self.assertNotIn("now", tokens)


class DeterministicAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        write(self.catalog_path, json.dumps({
            "version": 1,
            "taxonomy": {"contract": ["docs/architecture/**"], "state": ["docs/status/**"]},
            "documents": [{
                "path": "docs/architecture/API.md",
                "type": "contract",
                "status": "current",
                "depends_on": ["src/**"],
            }],
            "policies": {"auto_remove_new_duplicates": True},
        }))
        write(self.root / "docs/architecture/API.md", "# API\n\nThe old sentence.\n")
        write(self.root / "src/api.ts", "export const healthCheck = () => 'ok';\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        # A source change is what puts the contract document in front of the graph.
        write(self.root / "src/api.ts", API_SOURCE)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "add sendEmail"], cwd=self.root, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        self.snapshot = build_snapshot(
            self.root, self.catalog_path, base, head, ledger_path=self.ledger_path
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fake_runner(self, responses: Dict[str, str]):
        def run(plan: GraphPlan) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
            mapped: Dict[str, str] = {}
            for node in plan.audits:
                if node.path in responses:
                    mapped[node.node_id] = responses[node.path]
            for node in plan.drafts:
                key = f"draft:{node.path}"
                if key in responses:
                    mapped[node.node_id] = responses[key]
            for node in plan.conflicts:
                key = f"conflict:{node.paths[0]}"
                if key in responses:
                    mapped[node.node_id] = responses[key]
            return mapped, [{"event": "agent_complete", "name": node.node_id} for node in plan.audits]

        return run

    def _verdict(self, path: str, supported: bool) -> str:
        return json.dumps({
            "path": path,
            "supported": supported,
            "confidence": "high",
            "unsupported_claims": [] if supported else ["The old sentence."],
            "reason": "Adversarial check.",
        })

    def test_a_grounded_draft_is_applied_by_deterministic_code(self) -> None:
        baseline = analyze(self.snapshot, mode="review")
        decision = run_graph(
            self.snapshot,
            baseline,
            model_id="test-model",
            runner=self.fake_runner({
                "docs/architecture/API.md": self._verdict("docs/architecture/API.md", False),
                "draft:docs/architecture/API.md": json.dumps({
                    "path": "docs/architecture/API.md",
                    "original_span": "The old sentence.",
                    "proposed_span": "The `healthCheck` function is exported.",
                    "cited_sources": ["src/api.ts"],
                    "factual_tokens": ["healthCheck"],
                    "reason": "Grounded in src/api.ts.",
                }),
            }),
        )
        actions = {finding.action for finding in decision.findings}
        self.assertIn("draft_contract_span", actions)

        applied = apply_safe_actions(self.snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertIn(
            "healthCheck",
            (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"),
        )
        self.assertIn("docs/architecture/API.md", applied.modified_paths)

    def test_an_ungrounded_draft_is_discarded_and_the_document_refused(self) -> None:
        baseline = analyze(self.snapshot, mode="review")
        decision = run_graph(
            self.snapshot,
            baseline,
            model_id="test-model",
            runner=self.fake_runner({
                "docs/architecture/API.md": self._verdict("docs/architecture/API.md", False),
                "draft:docs/architecture/API.md": json.dumps({
                    "path": "docs/architecture/API.md",
                    "original_span": "The old sentence.",
                    "proposed_span": "The `processPayment` function is exported.",
                    "cited_sources": ["src/api.ts"],
                    "factual_tokens": ["processPayment"],
                    "reason": "Invented.",
                }),
            }),
        )
        draft_findings = [f for f in decision.findings if f.action == "draft_contract_span"]
        self.assertEqual(draft_findings, [])
        blocking = [
            f for f in decision.findings
            if f.risk == "high" and f.action == "block" and "docs/architecture/API.md" in f.documents
        ]
        self.assertTrue(blocking)
        explained = " ".join(f"{f.reason} {f.human_decision or ''}" for f in blocking)
        self.assertIn("discarded", explained)
        self.assertIn("processPayment", explained)
        self.assertEqual(decision.result, "action_required")

        apply_safe_actions(self.snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertNotIn(
            "processPayment",
            (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"),
        )

    def test_apply_refuses_a_citation_outside_the_repository_boundary(self) -> None:
        """A `**` dependency glob matches `../secret`, so the boundary is checked on the path."""
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "secret.ts").write_text("export const secretEndpoint = 1;\n", encoding="utf-8")
            escape = f"../{outside.name}/secret.ts"
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            catalog["documents"][0]["depends_on"] = ["**/*.ts"]
            write(self.catalog_path, json.dumps(catalog))
            snapshot = build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path)

            decision = GovernanceDecision(run_id="r", mode="review", result="changed", changed=False)
            decision.findings.append(Finding(
                kind="stale",
                risk="low",
                action="draft_contract_span",
                documents=["docs/architecture/API.md"],
                reason="Cites a file outside the repository.",
                evidence=[Evidence(path=escape, kind="cited_source")],
                proposed_patch=json.dumps({
                    "original_span": "The old sentence.",
                    "proposed_span": "The `secretEndpoint` is exported.",
                }),
            ))
            applied = apply_safe_actions(snapshot, decision, self.catalog_path, self.ledger_path)
            self.assertEqual(applied.result, "action_required")
            self.assertNotIn(
                "secretEndpoint",
                (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"),
            )
        finally:
            subprocess.run(["rm", "-rf", str(outside)], check=True)

    def test_apply_reproves_the_tokens_the_model_declared_itself(self) -> None:
        """The apply-time proof must not be weaker than the one it replaces."""
        decision = GovernanceDecision(run_id="r", mode="review", result="changed", changed=False)
        decision.findings.append(Finding(
            kind="stale",
            risk="low",
            action="draft_contract_span",
            documents=["docs/architecture/API.md"],
            reason="Declares a token no regex would extract.",
            evidence=[Evidence(path="src/api.ts", kind="cited_source")],
            proposed_patch=json.dumps({
                "original_span": "The old sentence.",
                "proposed_span": "The endpoint is exported.",
                "factual_tokens": ["madeUpSymbol"],
            }),
        ))
        applied = apply_safe_actions(self.snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "action_required")
        self.assertIn(
            "The old sentence.",
            (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"),
        )

    def test_apply_revalidates_a_draft_rather_than_trusting_the_finding(self) -> None:
        """Deterministic code holds the final authority, even over its own earlier self (P4)."""
        decision = GovernanceDecision(run_id="r", mode="review", result="changed", changed=False)
        decision.findings.append(Finding(
            kind="stale",
            risk="low",
            action="draft_contract_span",
            documents=["docs/architecture/API.md"],
            reason="Forged finding.",
            proposed_patch=json.dumps({
                "original_span": "The old sentence.",
                "proposed_span": "The `processPayment` function is exported.",
            }),
        ))
        applied = apply_safe_actions(self.snapshot, decision, self.catalog_path, self.ledger_path)
        self.assertEqual(applied.result, "action_required")
        self.assertEqual(applied.findings[0].action, "block")
        self.assertNotIn(
            "processPayment",
            (self.root / "docs/architecture/API.md").read_text(encoding="utf-8"),
        )

    def test_a_deterministic_high_risk_finding_survives_a_contradicting_verdict(self) -> None:
        baseline = analyze(self.snapshot, mode="review")
        baseline.findings.append(Finding(
            kind="conflict",
            risk="high",
            action="block",
            documents=["docs/architecture/API.md"],
            reason="Protected content was modified.",
        ))
        decision = run_graph(
            self.snapshot,
            baseline,
            model_id="test-model",
            runner=self.fake_runner({
                "docs/architecture/API.md": self._verdict("docs/architecture/API.md", True),
            }),
        )
        conflict = next(f for f in decision.findings if f.kind == "conflict")
        self.assertEqual(conflict.risk, "high")
        self.assertEqual(conflict.action, "block")
        self.assertEqual(decision.result, "action_required")

    def test_an_unsupported_verdict_blocks_the_document(self) -> None:
        baseline = analyze(self.snapshot, mode="review")
        decision = run_graph(
            self.snapshot,
            baseline,
            model_id="test-model",
            runner=self.fake_runner({
                "docs/architecture/API.md": self._verdict("docs/architecture/API.md", False),
            }),
        )
        stale = next(f for f in decision.findings if f.kind == "stale" and f.risk == "high")
        self.assertEqual(stale.action, "block")

    def test_the_trace_records_identifiers_only(self) -> None:
        baseline = analyze(self.snapshot, mode="review")
        decision = run_graph(
            self.snapshot,
            baseline,
            model_id="test-model",
            runner=self.fake_runner({
                "docs/architecture/API.md": self._verdict("docs/architecture/API.md", True),
            }),
        )
        serialized = json.dumps(decision.model_trace)
        self.assertIn("model_complete", serialized)
        self.assertNotIn("The old sentence.", serialized)
        for event in decision.model_trace:
            self.assertEqual(set(event), {"event", "name"})

    def test_a_schema_violation_fails_the_whole_run_closed(self) -> None:
        def broken(_plan: GraphPlan):
            return ({node.node_id: "I could not decide." for node in _plan.audits}, [])

        decision = govern(
            self.snapshot,
            mode="review",
            enable_model=True,
            model_id="test-model",
            runner=broken,
        )
        self.assertEqual(decision.result, "blocked")
        self.assertIn("failed closed", decision.error or "")
        self.assertFalse(decision.changed)

    def test_a_runner_exception_fails_closed_without_mutation(self) -> None:
        def explode(_plan: GraphPlan):
            raise TimeoutError("timed out")

        decision = govern(
            self.snapshot, mode="review", enable_model=True, runner=explode
        )
        self.assertEqual(decision.result, "blocked")
        self.assertFalse(self.ledger_path.exists())


class ConfinementTests(unittest.TestCase):
    def test_model_only_stale_mutation_becomes_a_human_block(self) -> None:
        finding = Finding(
            kind="stale",
            risk="low",
            action="mark_stale",
            documents=["docs/status/RELEASE.md"],
            reason="Model suspects drift.",
        )
        confined = _confine_model_finding(finding)
        self.assertEqual(confined.risk, "high")
        self.assertEqual(confined.action, "block")
        self.assertIsNotNone(confined.human_decision)

    def test_model_duplicate_still_requires_deterministic_merge_boundary(self) -> None:
        finding = Finding(
            kind="duplicate",
            risk="low",
            action="merge_new_file",
            documents=["docs/architecture/COPY.md", "docs/architecture/API.md"],
            reason="Semantic duplicate.",
        )
        self.assertIs(_confine_model_finding(finding), finding)
        self.assertEqual(finding.action, "merge_new_file")

    def test_model_trace_is_part_of_the_structured_decision(self) -> None:
        decision = GovernanceDecision(
            run_id="run",
            mode="review",
            result="pass",
            changed=False,
            model_used=True,
            model_trace=[{"event": "tool_call", "name": "evidence_auditor__0:evidence_for_document"}],
        )
        self.assertEqual(
            decision.to_dict()["model_trace"],
            [{"event": "tool_call", "name": "evidence_auditor__0:evidence_for_document"}],
        )

    def test_an_empty_plan_does_not_report_the_model_as_used(self) -> None:
        snapshot = RepositorySnapshot(root=Path("."), catalog=Catalog.default())
        baseline = GovernanceDecision("run", "review", "pass", False)
        decision = run_graph(snapshot, baseline, model_id="test-model")
        self.assertFalse(decision.model_used)
        self.assertEqual(decision.result, "pass")


if __name__ == "__main__":
    unittest.main()
