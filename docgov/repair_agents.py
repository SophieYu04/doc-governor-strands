"""Strands-first planning for required-document repair.

The graph produces bounded instructions for a separate coding agent. It cannot
write files, and deterministic validation rejects plans that cite undeclared
sources or answer about a different document.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agents import (
    DEFAULT_GRAPH_TIMEOUT_SECONDS,
    DEFAULT_MODEL_ID,
    DEFAULT_NODE_TIMEOUT_SECONDS,
    MAX_DOCUMENT_CHARS,
    AgentContractError,
    AgentSpec,
    _ToolBudget,
    _clip,
    _node_prompt,
    _result_text,
    assert_read_only,
    declared_source_payload,
    parse_json_object,
)
from .engine import RepositorySnapshot, changed_dependency_evidence
from .models import DocumentRecord


REPAIR_PLANNER = AgentSpec(
    identifier="repair_planner",
    role="Repair Planner",
    tool_names=("target_document", "declared_source"),
    max_tool_calls=8,
    document_types=("contract", "procedure"),
    system_prompt=(
        "You are the Repair Planner. Determine the smallest documentation correction required by "
        "changed source evidence. Read the target document and relevant declared sources. Produce "
        "instructions for a coding agent, not replacement prose. Every evidence path must be one "
        "of the declared source files you actually read. Do not claim deployment, testing, approval, "
        "or verification. If the evidence is insufficient, set needs_human to true instead of guessing.\n"
        "Reply with one JSON object and nothing else. Schema: "
        '{"path": str, "instructions": [str], "evidence_paths": [str], "needs_human": bool, "reason": str}'
    ),
)
assert_read_only(REPAIR_PLANNER)


class RepairPlanError(RuntimeError):
    """Raised when Strands cannot produce a bounded, grounded repair plan."""


@dataclass(frozen=True)
class RepairNode:
    node_id: str
    path: str
    document_type: str
    depends_on: Tuple[str, ...]
    changed_sources: Tuple[str, ...]


@dataclass(frozen=True)
class RepairGraphPlan:
    nodes: Tuple[RepairNode, ...]


@dataclass(frozen=True)
class RepairInstruction:
    path: str
    instructions: Tuple[str, ...]
    evidence_paths: Tuple[str, ...]
    reason: str


RepairRunner = Callable[[RepairGraphPlan], Tuple[Dict[str, str], List[Dict[str, str]]]]


def plan_repairs(snapshot: RepositorySnapshot, candidates: List[DocumentRecord]) -> RepairGraphPlan:
    nodes: List[RepairNode] = []
    for index, record in enumerate(candidates):
        if not REPAIR_PLANNER.permits(record.type):
            raise RepairPlanError(f"Strands Repair Planner may not plan a {record.type} document: {record.path}")
        changed = tuple(item.path for item in changed_dependency_evidence(snapshot, record))
        nodes.append(RepairNode(
            node_id=f"{REPAIR_PLANNER.identifier}__{index}",
            path=record.path,
            document_type=record.type,
            depends_on=tuple(record.depends_on),
            changed_sources=changed,
        ))
    return RepairGraphPlan(tuple(nodes))


def _parse_instruction(payload: Dict[str, Any], node: RepairNode) -> RepairInstruction:
    if payload.get("path") != node.path:
        raise AgentContractError("The Repair Planner answered about a document it was not assigned.")
    instructions = payload.get("instructions")
    if not isinstance(instructions, list) or not instructions or not all(
        isinstance(item, str) and item.strip() for item in instructions
    ):
        raise AgentContractError("Repair instructions must be a non-empty array of strings.")
    evidence_paths = payload.get("evidence_paths")
    if not isinstance(evidence_paths, list) or not evidence_paths or not all(
        isinstance(item, str) and item in node.changed_sources for item in evidence_paths
    ):
        raise AgentContractError("Repair evidence must name changed, declared source files only.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise AgentContractError("Repair reason must be a non-empty string.")
    if payload.get("needs_human") is not False:
        raise RepairPlanError(f"Strands requires human review for {node.path}: {reason}")
    return RepairInstruction(
        path=node.path,
        instructions=tuple(item.strip() for item in instructions),
        evidence_paths=tuple(evidence_paths),
        reason=reason.strip(),
    )


def run_repair_graph(
    snapshot: RepositorySnapshot,
    candidates: List[DocumentRecord],
    *,
    model_id: Optional[str] = None,
    runner: Optional[RepairRunner] = None,
) -> Tuple[List[RepairInstruction], List[Dict[str, str]]]:
    plan = plan_repairs(snapshot, candidates)
    if not plan.nodes:
        return [], []
    execute = runner or strands_repair_runner(snapshot, model_id=model_id or DEFAULT_MODEL_ID)
    responses, trace = execute(plan)
    instructions: List[RepairInstruction] = []
    for node in plan.nodes:
        raw = responses.get(node.node_id)
        if raw is None:
            raise RepairPlanError(f"Strands returned no repair plan for {node.path}.")
        instructions.append(_parse_instruction(parse_json_object(raw), node))
    return instructions, trace


def strands_repair_runner(snapshot: RepositorySnapshot, *, model_id: str) -> RepairRunner:
    """Build a read-only Strands graph with one isolated planner per document."""

    def execute(plan: RepairGraphPlan) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
        from strands import Agent, tool
        from strands.models import BedrockModel
        from strands.multiagent import GraphBuilder

        trace: List[Dict[str, str]] = []
        model = BedrockModel(
            model_id=model_id,
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
        builder = GraphBuilder()
        builder.set_execution_timeout(float(
            os.environ.get("DOCGOV_GRAPH_TIMEOUT_SECONDS", DEFAULT_GRAPH_TIMEOUT_SECONDS)
        ))
        builder.set_node_timeout(float(
            os.environ.get("DOCGOV_NODE_TIMEOUT_SECONDS")
            or os.environ.get("DOCGOV_MODEL_TIMEOUT_SECONDS")
            or DEFAULT_NODE_TIMEOUT_SECONDS
        ))

        def make_source_tool(allowed: Tuple[str, ...], changed: Tuple[str, ...]) -> Any:
            @tool(name="declared_source")
            def declared_source(path: str) -> str:
                """Read a changed source file declared by the assigned document."""
                normalized = path.replace("\\", "/")
                if normalized not in changed:
                    return json.dumps({
                        "path": normalized,
                        "readable": False,
                        "reason": "Only changed declared sources may be read during repair planning.",
                    })
                return json.dumps(declared_source_payload(snapshot, allowed, normalized), ensure_ascii=False)

            return declared_source

        def make_target_tool(path: str) -> Any:
            @tool(name="target_document")
            def target_document() -> str:
                """Return the current text of the assigned required document."""
                return json.dumps({
                    "path": path,
                    "content": _clip(snapshot.files.get(path, ""), MAX_DOCUMENT_CHARS),
                }, ensure_ascii=False)

            return target_document

        for node in plan.nodes:
            body = (
                f"Required document: {node.path}\n"
                f"Type: {node.document_type}\n"
                f"Declared dependencies: {', '.join(node.depends_on)}\n"
                f"Changed declared sources: {', '.join(node.changed_sources)}\n"
            )
            agent = Agent(
                model=model,
                tools=[
                    make_target_tool(node.path),
                    make_source_tool(node.depends_on, node.changed_sources),
                ],
                system_prompt=_node_prompt(REPAIR_PLANNER, body),
                hooks=[_ToolBudget(REPAIR_PLANNER, node.node_id, trace)],
                callback_handler=None,
            )
            builder.add_node(agent, node.node_id)
            builder.set_entry_point(node.node_id)

        result = builder.build()(
            "Plan the smallest evidence-backed repair for your assigned document and return the required JSON."
        )
        responses: Dict[str, str] = {}
        for node_id, node_result in result.results.items():
            texts = [_result_text(item) for item in node_result.get_agent_results()]
            if texts:
                responses[str(node_id)] = texts[-1]
            trace.append({"event": "agent_complete", "name": str(node_id)})
        return responses, trace

    return execute
