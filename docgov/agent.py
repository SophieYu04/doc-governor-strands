"""Entry point for the judgement layer, and the invariant that guards it.

``govern`` is the only place a model result can enter a Doc Governor run, and it
is deliberately narrow: deterministic analysis runs first and unconditionally,
the graph may only add to it, and any failure at all — an import error, a
timeout, a schema violation, a privilege error — returns the deterministic
findings with ``result="blocked"`` and changes nothing (P4, §8.6).

The multi-agent graph itself lives in :mod:`docgov.agents`.
"""

from __future__ import annotations

from typing import Optional

from .agents import (
    DEFAULT_MODEL_ID,
    GraphRunner,
    confine_model_finding,
    plan_graph,
    run_graph,
)
from .engine import RepositorySnapshot, analyze
from .models import GovernanceDecision
from .model_errors import model_error_code


# Preserved under its original private name: the invariant it encodes (model
# output may add context but never authorize an unproven mutation) is referenced
# by name in AGENTS.md and in the security invariant list.
_confine_model_finding = confine_model_finding

__all__ = [
    "DEFAULT_MODEL_ID",
    "govern",
    "plan_graph",
    "run_graph",
    "_confine_model_finding",
]


def govern(
    snapshot: RepositorySnapshot,
    mode: str,
    run_id: str | None = None,
    enable_model: bool = False,
    model_id: str | None = None,
    runner: Optional[GraphRunner] = None,
) -> GovernanceDecision:
    baseline = analyze(snapshot, mode=mode, run_id=run_id)
    if not enable_model:
        return baseline
    try:
        decision = run_graph(snapshot, baseline, model_id=model_id, runner=runner)
        decision.model_requested = True
        return decision
    except Exception as exc:
        # Fail closed: keep every deterministic finding, add nothing, mutate nothing.
        return GovernanceDecision(
            run_id=baseline.run_id,
            mode=baseline.mode,
            result="blocked",
            changed=False,
            findings=baseline.findings,
            head_sha=baseline.head_sha,
            model_used=False,
            model_requested=True,
            error=f"Strands graph failed closed: {model_error_code(exc)}",
        )
