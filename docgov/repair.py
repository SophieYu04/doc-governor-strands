from __future__ import annotations

from typing import List, Optional

from .engine import RepositorySnapshot, changed_dependency_evidence
from .models import DocumentRecord
from .patterns import matches_repo_glob
from .repair_agents import RepairRunner, run_repair_graph


def repair_candidates(snapshot: RepositorySnapshot) -> List[DocumentRecord]:
    patterns = [
        str(item)
        for item in snapshot.catalog.policies.get("auto_repair_documents", [])
    ]
    return [
        record
        for record in snapshot.catalog.documents
        if record.type in {"contract", "procedure"}
        and record.approval != "human"
        and not snapshot.catalog.is_protected(record.path)
        and any(matches_repo_glob(record.path, pattern) for pattern in patterns)
        and any(
            matches_repo_glob(path, dependency)
            for path in snapshot.changed
            for dependency in record.depends_on
        )
    ]


def build_repair_prompt(
    snapshot: RepositorySnapshot,
    *,
    enable_model: bool = False,
    model_id: Optional[str] = None,
    runner: Optional[RepairRunner] = None,
    trace: Optional[List[dict]] = None,
    target_paths: Optional[set[str]] = None,
) -> str:
    candidates = repair_candidates(snapshot)
    if target_paths is not None:
        candidates = [record for record in candidates if record.path in target_paths]
    if not candidates:
        return ""
    planned = []
    if enable_model:
        planned, _trace = run_repair_graph(
            snapshot,
            candidates,
            model_id=model_id,
            runner=runner,
            trace_sink=trace,
        )
    plans_by_path = {item.path: item for item in planned}
    sections: List[str] = []
    for record in candidates:
        changed = changed_dependency_evidence(snapshot, record)
        sources = "\n".join(f"- {item.path}" for item in changed) or "- none"
        plan = plans_by_path.get(record.path)
        strands_plan = ""
        if plan is not None:
            instructions = "\n".join(f"- {item}" for item in plan.instructions)
            evidence = "\n".join(f"- {item}" for item in plan.evidence_paths)
            strands_plan = (
                f"\nStrands repair instructions:\n{instructions}\n"
                f"Strands evidence paths:\n{evidence}\n"
                f"Strands reasoning: {plan.reason}\n"
            )
        sections.append(
            f"Document: {record.path}\n"
            f"Type: {record.type}\n"
            f"Declared dependencies: {', '.join(record.depends_on)}\n"
            f"Changed evidence:\n{sources}"
            f"{strands_plan}"
        )
    documents = "\n\n".join(sections)
    planner = (
        "Amazon Bedrock and a read-only Strands graph produced the bounded repair plan below. "
        if enable_model
        else ""
    )
    return f"""You are the repository's coding agent. Repair required documentation before this commit.

{planner}Inspect the staged code diff and the declared source dependencies below. Follow the Strands repair instructions when present. Update each listed document so its factual claims match the implementation in the working tree. Preserve valid human-authored guidance and edit only what the source change makes inaccurate or incomplete. Do not claim deployment, testing, approval, or verification unless repository evidence proves it. Do not change dates merely to make a document look current. Do not commit.

Required documents:

{documents}

After editing, run the repository's documented verification command. If a claim cannot be grounded, leave the document unchanged and report the blocker instead of inventing content.
"""
