"""Drive the real Strands Graph with a stub model instead of Amazon Bedrock.

Every other test in this suite replaces :func:`docgov.agents.strands_runner`
with a fake, which proves the ruling logic but proves nothing about the Strands
wiring. This module does the opposite: the real ``GraphBuilder``, the real
``Agent`` nodes, the real ``@tool`` decorators, the real ``BeforeToolCallEvent``
hook and the real edge conditions all run. Only the network call is replaced, by
a ``Model`` implementation that yields canned stream events.

So an API change in Strands — a renamed builder method, a moved hook event, a
different ``GraphResult`` shape — fails here in CI rather than the first time
someone points the action at Bedrock.

It does not prove that a real model returns a schema-valid answer. Nothing short
of a live Bedrock call proves that.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterable, Dict, List, Optional
from unittest.mock import patch

from docgov.agent import govern
from docgov.agents import plan_graph, strands_runner
from docgov.engine import analyze, build_snapshot

try:  # the graph only exists when the bedrock extra is installed
    from strands.models.model import Model

    STRANDS_AVAILABLE = True
except ImportError:  # pragma: no cover - deterministic-only installs
    Model = object  # type: ignore[assignment,misc]
    STRANDS_AVAILABLE = False


API_DOC = """# API Contract

Status: Current

The `healthCheck` endpoint returns readiness.
"""
API_SOURCE = "export const healthCheck = () => 'ok';\nexport const sendEmail = () => true;\n"

AUDIT_ANSWER = json.dumps({
    "path": "docs/architecture/API.md",
    "supported": False,
    "confidence": "high",
    "unsupported_claims": ["The `healthCheck` endpoint returns readiness."],
    "reason": "The declared source now exports a second function the document does not mention.",
})
DRAFT_ANSWER = json.dumps({
    "path": "docs/architecture/API.md",
    "original_span": "The `healthCheck` endpoint returns readiness.",
    "proposed_span": "The `healthCheck` and `sendEmail` functions are exported.",
    "cited_sources": ["src/api.ts"],
    "factual_tokens": ["healthCheck", "sendEmail"],
    "reason": "Both names appear in src/api.ts.",
})


class StubModel(Model):  # type: ignore[misc,valid-type]
    """A Strands ``Model`` that answers from a script instead of from Bedrock.

    The first invocation of a node that has tools emits a tool-use block, so the
    tool-budget hook, the tool closure and the trace recorder are all exercised;
    the next one emits the JSON answer.
    """

    def __init__(self, answers: Dict[str, str]) -> None:
        self.answers = answers
        self.tool_calls: List[str] = []
        self._served: set[str] = set()

    def get_config(self) -> Dict[str, Any]:
        return {}

    def update_config(self, **kwargs: Any) -> None:
        return None

    def _answer_for(self, system_prompt: Optional[str]) -> str:
        prompt = system_prompt or ""
        for marker, answer in self.answers.items():
            if marker in prompt:
                return answer
        return "{}"

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterable[Dict[str, Any]]:
        already_used_a_tool = any(
            isinstance(message, dict)
            and any("toolResult" in block for block in message.get("content", []) if isinstance(block, dict))
            for message in (messages or [])
        )
        if tool_specs and not already_used_a_tool:
            spec = tool_specs[0]
            name = spec["name"] if isinstance(spec, dict) else getattr(spec, "name")
            self.tool_calls.append(str(name))
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"t{len(self.tool_calls)}"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"path": "src/api.ts"})}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        if any(spec.get("name") == "GovernanceOutput" for spec in (tool_specs or [])):
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "GovernanceOutput", "toolUseId": "output"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": self._answer_for(system_prompt)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": self._answer_for(system_prompt)}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}

    async def structured_output(self, output_model: Any, prompt: Any, **kwargs: Any) -> AsyncIterable[Any]:
        yield {"output": output_model()}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@unittest.skipUnless(STRANDS_AVAILABLE, "Strands is not installed")
class StrandsGraphSmokeTests(unittest.TestCase):
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
        write(self.root / "docs/architecture/API.md", API_DOC)
        write(self.root / "src/api.ts", "export const healthCheck = () => 'ok';\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        write(self.root / "src/api.ts", API_SOURCE)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "add sendEmail"], cwd=self.root, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        self.snapshot = build_snapshot(
            self.root, self.catalog_path, base, head, ledger_path=self.ledger_path
        )
        self.baseline = analyze(self.snapshot, mode="review")
        self.model = StubModel({
            "Evidence Auditor": AUDIT_ANSWER,
            "Contract Drafter": DRAFT_ANSWER,
        })

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_real_graph(self):
        with patch("strands.models.BedrockModel", lambda **_kwargs: self.model):
            runner = strands_runner(self.snapshot, model_id="stub-model")
            return runner(plan_graph(self.snapshot, self.baseline))

    def test_private_reasoning_text_is_discarded_before_interpreting_verdicts(self):
        class ThinkingModel(StubModel):
            async def stream(self, *args, **kwargs):
                async for event in super().stream(*args, **kwargs):
                    start = event.get("contentBlockStart", {}).get("start", {}).get("toolUse", {})
                    if start.get("name") == "GovernanceOutput":
                        yield {"contentBlockDelta": {"delta": {"text": "<thinking>PRIVATE { not valid JSON</thinking>"}}}
                        yield {"contentBlockStop": {}}
                    yield event
        self.model = ThinkingModel(self.model.answers)
        responses, trace = self.run_real_graph()
        self.assertTrue(responses)
        for value in responses.values():
            json.loads(value)
            self.assertNotIn("PRIVATE", value)
        self.assertNotIn("PRIVATE", json.dumps(trace))

    def test_the_real_graph_builds_executes_and_returns_node_answers(self) -> None:
        plan = plan_graph(self.snapshot, self.baseline)
        self.assertTrue(plan.audits, "the fixture must give the graph something to audit")

        responses, trace = self.run_real_graph()

        audit_id = plan.audits[0].node_id
        self.assertIn(audit_id, responses)
        self.assertEqual(json.loads(responses[audit_id])["supported"], False)
        self.assertIn({"event": "agent_complete", "name": audit_id}, trace)

    def test_the_tool_budget_hook_records_a_real_tool_call(self) -> None:
        _, trace = self.run_real_graph()
        tool_events = [event for event in trace if event["event"] == "tool_call"]
        self.assertTrue(tool_events, "no tool call reached the BeforeToolCallEvent hook")
        for event in tool_events:
            # Identifiers only: node id and tool name, never arguments (§8.7).
            self.assertEqual(set(event), {"event", "name"})
            self.assertIn(":", event["name"])

    def test_the_drafter_edge_fires_only_when_the_auditor_faults_the_document(self) -> None:
        plan = plan_graph(self.snapshot, self.baseline)
        self.assertTrue(plan.drafts, "the fixture must give the graph a contract to redraft")
        responses, _ = self.run_real_graph()
        self.assertIn(plan.drafts[0].node_id, responses)

    def test_a_supported_verdict_leaves_the_drafter_unexecuted(self) -> None:
        self.model.answers["Evidence Auditor"] = json.dumps({
            "path": "docs/architecture/API.md",
            "supported": True,
            "confidence": "high",
            "unsupported_claims": [],
            "reason": "The source matches the document.",
        })
        plan = plan_graph(self.snapshot, self.baseline)
        responses, _ = self.run_real_graph()
        self.assertNotIn(plan.drafts[0].node_id, responses)

    def test_govern_runs_the_real_graph_end_to_end(self) -> None:
        with patch("strands.models.BedrockModel", lambda **_kwargs: self.model):
            decision = govern(
                self.snapshot, mode="review", enable_model=True, model_id="stub-model"
            )
        self.assertTrue(decision.model_used)
        self.assertIsNone(decision.error)
        self.assertIn(
            "draft_contract_span",
            {finding.action for finding in decision.findings},
        )
        serialized = json.dumps(decision.model_trace)
        self.assertNotIn("healthCheck", serialized)


if __name__ == "__main__":
    unittest.main()


class StructuredOutputBoundaryTests(unittest.TestCase):
    def test_final_output_budget_does_not_expand_read_access(self):
        from types import SimpleNamespace
        from docgov.agents import _ToolBudget, EVIDENCE_AUDITOR
        trace = []
        budget = _ToolBudget(EVIDENCE_AUDITOR, "audit", trace)
        for _ in range(EVIDENCE_AUDITOR.max_tool_calls):
            budget._before_tool_call(SimpleNamespace(tool_use={"name": "evidence_for_document"}, cancel_tool=None))
        for name in ("evidence_for_document", "write_document"):
            event = SimpleNamespace(tool_use={"name": name}, cancel_tool=None)
            budget._before_tool_call(event)
            self.assertIsNotNone(event.cancel_tool)
        for _ in range(3):
            event = SimpleNamespace(tool_use={"name": "GovernanceOutput"}, cancel_tool=None)
            budget._before_tool_call(event)
            self.assertIsNone(event.cancel_tool)
        event = SimpleNamespace(tool_use={"name": "GovernanceOutput"}, cancel_tool=None)
        budget._before_tool_call(event)
        self.assertIsNotNone(event.cancel_tool)

    def test_rejected_tool_name_cannot_leak_into_public_trace(self):
        from types import SimpleNamespace
        from docgov.agents import _ToolBudget, EVIDENCE_AUDITOR
        trace = []
        budget = _ToolBudget(EVIDENCE_AUDITOR, "audit", trace)
        private = "PRIVATE DOCUMENT TEXT"
        event = SimpleNamespace(tool_use={"name": private}, cancel_tool=None)
        budget._before_tool_call(event)
        self.assertIsNotNone(event.cancel_tool)
        self.assertNotIn(private, event.cancel_tool)
        self.assertNotIn(private, json.dumps(trace))
        self.assertEqual(trace, [{"event": "tool_denied", "name": "audit:unrecognized_tool"}])

    def test_structured_output_takes_precedence_over_untrusted_prose(self):
        from types import SimpleNamespace
        from docgov.agents import _result_text
        value = SimpleNamespace(structured_output=SimpleNamespace(model_dump_json=lambda: '{"supported":false}'),
                                message={"content": [{"text": "malformed { prose"}]})
        self.assertEqual(_result_text(value), '{"supported":false}')


@unittest.skipUnless(STRANDS_AVAILABLE, "Strands is not installed")
class ModelStreamRetryTests(unittest.TestCase):
    def test_only_transient_stream_failure_is_retryable_and_attempts_are_bounded(self):
        from docgov.agents import _model_retry_strategy
        class ResponseError(Exception):
            def __init__(self, code):
                self.response = {"Error": {"Code": code}}
        retry = _model_retry_strategy()
        self.assertEqual(retry._max_attempts, 2)
        self.assertTrue(retry.is_retryable(ResponseError("modelStreamErrorException")))
        self.assertFalse(retry.is_retryable(ResponseError("AccessDeniedException")))
        self.assertFalse(retry.is_retryable(ValueError("invalid model plan")))
