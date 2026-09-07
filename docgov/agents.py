"""The judgement layer: a Strands Graph of three deliberately under-privileged agents.

The point of splitting one agent into three is not parallelism. It is privilege
separation (P5). Each agent receives strictly less than the previous single agent
did — a narrower tool set, a narrower slice of the repository, and in Agent A's
case deliberately less information than is available:

* **Evidence Auditor (A)** — decides whether the evidence supports a document's
  claims. It is never shown the document's own assertion of its status, its
  ``last_verified_at`` field, or any prior Doc Governor conclusion, because a
  document that says "verified" is trying to answer the question being asked.
  One invocation per document, fanned out.
* **Conflict Resolver (B)** — decides which of several contradictory documents is
  current. Sees only the conflicting documents and A's verdicts, and can read only
  the source files those documents already declare.
* **Contract Drafter (C)** — may re-draft a span of a ``contract`` document. It is
  structurally impossible to construct this agent for a ``state``, ``evidence``, or
  ``decision`` document: the restriction lives in :func:`plan_graph`, not in a
  prompt. Its output is a proposal that must survive deterministic grounding
  validation before anything is written (see :mod:`docgov.drafting`).

None of the three can write, execute, or reach the network. Deterministic code
plans the graph, validates every structured response, and holds the final ruling
(P4); a model failure, timeout, or schema violation fails closed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Literal

from .catalog import Catalog
from .drafting import DraftValidation, validate_draft
from .engine import (
    RepositorySnapshot,
    dependency_evidence,
    dependency_fingerprint,
    dependency_summary,
    repository_path,
)
from .ledger import Ledger
from .models import DocumentRecord, Evidence, Finding, GovernanceDecision
from .patterns import matches_repo_glob


DEFAULT_MODEL_ID = "us.amazon.nova-lite-v1:0"
DEFAULT_GRAPH_TIMEOUT_SECONDS = 240.0
DEFAULT_NODE_TIMEOUT_SECONDS = 90.0
MAX_AUDIT_DOCUMENTS = 8
MAX_DOCUMENT_CHARS = 12_000
MAX_SOURCE_CHARS = 8_000

#: Substrings that must never appear in a tool exposed to any Doc Governor agent.
#: No agent has both model access and infrastructure write access (§8.5).
FORBIDDEN_TOOL_TOKENS = (
    "write", "edit", "patch", "apply", "commit", "push", "delete", "remove",
    "shell", "exec", "run_", "bash", "http", "fetch", "request", "deploy",
    "upload", "network",
)

#: Lines a document uses to describe its own trustworthiness. Agent A never sees them.
SELF_ASSESSMENT_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(status|last[_ ]verified(?:[_ ]at)?|last[_ ]updated|verified[_ ]by|owner|狀態|最後驗證|最後更新)"
    r"(?:\*\*)?\s*[:：=].*$",
    re.IGNORECASE | re.MULTILINE,
)
DOCGOV_MARKER_PATTERN = re.compile(r"<!--\s*docgov:.*?-->", re.DOTALL)
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)


class AgentContractError(ValueError):
    """Raised when an agent response does not satisfy its declared schema."""


class PrivilegeError(RuntimeError):
    """Raised when an agent would be constructed outside its declared privilege."""


# ---------------------------------------------------------------------------
# Privilege declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """What one agent is allowed to do. Enforced in code, asserted in tests."""

    identifier: str
    role: str
    tool_names: Tuple[str, ...]
    max_tool_calls: int
    document_types: Tuple[str, ...]  # empty means "any governed type"
    system_prompt: str

    def permits(self, document_type: str) -> bool:
        return not self.document_types or document_type in self.document_types


_JSON_ONLY = "Reply with a single JSON object and nothing else. No prose, no code fence."

EVIDENCE_AUDITOR = AgentSpec(
    identifier="evidence_auditor",
    role="Evidence Auditor",
    tool_names=("evidence_for_document", "GovernanceOutput"),
    max_tool_calls=2,
    document_types=(),
    system_prompt=(
        "You are the Evidence Auditor. You are given the claims a documentation file makes and "
        "the evidence recorded for it. Your stance is adversarial: try to disprove the claims. "
        "A claim is supported only if the evidence you can see positively establishes it. "
        "Absence of contradicting evidence is not support. You cannot see the document's own "
        "status field or any previous conclusion about it, and you must not guess at them. "
        "Quote unsupported claims verbatim from the text you were given; never paraphrase and "
        "never invent a claim that is not in the text.\n"
        "Call evidence_for_document exactly once, then answer.\n"
        + _JSON_ONLY
        + ' Schema: {"path": str, "supported": bool, "confidence": "high"|"medium"|"low", '
        '"unsupported_claims": [str], "reason": str}'
    ),
)

CONFLICT_RESOLVER = AgentSpec(
    identifier="conflict_resolver",
    role="Conflict Resolver",
    tool_names=("declared_source", "GovernanceOutput"),
    max_tool_calls=6,
    document_types=(),
    system_prompt=(
        "You are the Conflict Resolver. Several documents make contradictory statements about "
        "the same subject. Decide which one reflects current truth and which are superseded, "
        "using only the documents shown to you, the Evidence Auditor verdicts, and the source "
        "files those documents declare as dependencies (read them with declared_source). "
        "If the evidence you can see does not settle it, say so by setting needs_human to true "
        "rather than guessing — a wrong ruling is worse than no ruling.\n"
        + _JSON_ONLY
        + ' Schema: {"subject": str, "canonical_path": str, "superseded_paths": [str], '
        '"reason": str, "needs_human": bool}'
    ),
)

CONTRACT_DRAFTER = AgentSpec(
    identifier="contract_drafter",
    role="Contract Drafter",
    tool_names=("target_document", "declared_source", "GovernanceOutput"),
    max_tool_calls=8,
    document_types=("contract",),
    system_prompt=(
        "You are the Contract Drafter. A contract document describes an interface whose source "
        "has changed, and the deterministic inventory sync could not repair the prose. Propose a "
        "replacement for the smallest span that is wrong.\n"
        "Every identifier, function name, endpoint path, file path, config key and type name you "
        "write must appear literally in a source file you cite, and you may cite only files the "
        "document already declares as dependencies. Do not write a date, a version number, or any "
        "claim that something was verified, tested, or approved — none of those are recoverable "
        "from source, and a draft containing one is discarded. If you cannot ground the whole "
        "span, return an empty proposed_span; the document will be marked stale instead, which is "
        "the safe outcome.\n"
        "original_span must be copied byte for byte from the current document.\n"
        + _JSON_ONLY
        + ' Schema: {"path": str, "original_span": str, "proposed_span": str, '
        '"cited_sources": [str], "factual_tokens": [str], "reason": str}'
    ),
)

ALL_SPECS = (EVIDENCE_AUDITOR, CONFLICT_RESOLVER, CONTRACT_DRAFTER)


def assert_read_only(spec: AgentSpec) -> None:
    """Fail loudly if a spec ever names a tool that could mutate or reach the network."""
    for name in spec.tool_names:
        lowered = name.lower()
        for token in FORBIDDEN_TOOL_TOKENS:
            if token in lowered:
                raise PrivilegeError(
                    f"Agent {spec.identifier!r} declares tool {name!r}, which crosses the read-only boundary."
                )


for _spec in ALL_SPECS:
    assert_read_only(_spec)


# ---------------------------------------------------------------------------
# Input shaping
# ---------------------------------------------------------------------------


def redact_self_assessment(text: str) -> str:
    """Strip a document's own claims about its trustworthiness.

    Feeding the Evidence Auditor a document that says "Status: Current, last
    verified 2026-09-01" contaminates the judgement it is being asked to make, so
    those lines and every Doc Governor annotation are removed before the document
    reaches it. This is a design decision, not an oversight.
    """
    without_markers = DOCGOV_MARKER_PATTERN.sub("", text)
    without_comments = HTML_COMMENT_PATTERN.sub("", without_markers)
    without_status = SELF_ASSESSMENT_PATTERN.sub("", without_comments)
    return re.sub(r"\n{3,}", "\n\n", without_status).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


# ---------------------------------------------------------------------------
# Deterministic graph planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditNode:
    node_id: str
    path: str
    claims: str


@dataclass(frozen=True)
class ConflictNode:
    node_id: str
    subject: str
    paths: Tuple[str, ...]
    documents: Dict[str, str]
    depends_on: Tuple[str, ...]


@dataclass(frozen=True)
class DraftNode:
    node_id: str
    path: str
    depends_on: Tuple[str, ...]


@dataclass(frozen=True)
class GraphPlan:
    audits: Tuple[AuditNode, ...] = ()
    conflicts: Tuple[ConflictNode, ...] = ()
    drafts: Tuple[DraftNode, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.audits or self.conflicts or self.drafts)


def _audit_candidates(snapshot: RepositorySnapshot, baseline: GovernanceDecision) -> List[str]:
    """Documents whose claims are worth auditing this run, deterministically chosen."""
    candidates: List[str] = []
    for finding in baseline.findings:
        for path in finding.documents:
            if path in snapshot.files and path not in candidates:
                candidates.append(path)
    for path in snapshot.changed + snapshot.added:
        if path.endswith(".md") and path in snapshot.files and path not in candidates:
            if snapshot.catalog.record_for(path) is not None:
                candidates.append(path)
    return candidates[:MAX_AUDIT_DOCUMENTS]


def _conflict_groups(snapshot: RepositorySnapshot, baseline: GovernanceDecision) -> List[Tuple[str, List[str]]]:
    groups: List[Tuple[str, List[str]]] = []
    seen: set[Tuple[str, ...]] = set()
    for key, records in snapshot.catalog.duplicate_canonical_records().items():
        paths = sorted(record.path for record in records if record.path in snapshot.files)
        if len(paths) >= 2 and tuple(paths) not in seen:
            seen.add(tuple(paths))
            groups.append((key, paths))
    for finding in baseline.findings:
        if finding.kind not in {"conflict", "duplicate"}:
            continue
        paths = sorted({path for path in finding.documents if path in snapshot.files})
        if len(paths) >= 2 and tuple(paths) not in seen:
            seen.add(tuple(paths))
            groups.append((finding.reason[:80], paths))
    return groups


def _drafter_eligible(snapshot: RepositorySnapshot, path: str) -> bool:
    """Agent C exists only for a contract document the governor is allowed to rewrite.

    This is the structural half of P2: a state, evidence or decision document can
    never reach the Drafter because no Drafter node is ever built for it.
    """
    record = snapshot.catalog.record_for(path)
    if record is None or record.type != "contract":
        return False
    if not CONTRACT_DRAFTER.permits(record.type):
        return False
    if snapshot.catalog.is_protected(path):
        return False
    if record.approval == "human":
        return False
    return bool(record.depends_on) and path in snapshot.files


def plan_graph(snapshot: RepositorySnapshot, baseline: GovernanceDecision) -> GraphPlan:
    """Decide the graph's shape before any model runs. Deterministic and testable."""
    audits: List[AuditNode] = []
    for index, path in enumerate(_audit_candidates(snapshot, baseline)):
        audits.append(AuditNode(
            node_id=f"{EVIDENCE_AUDITOR.identifier}__{index}",
            path=path,
            claims=_clip(redact_self_assessment(snapshot.files[path]), MAX_DOCUMENT_CHARS),
        ))

    conflicts: List[ConflictNode] = []
    for index, (subject, paths) in enumerate(_conflict_groups(snapshot, baseline)):
        depends: List[str] = []
        for path in paths:
            record = snapshot.catalog.record_for(path)
            if record:
                depends.extend(record.depends_on)
        conflicts.append(ConflictNode(
            node_id=f"{CONFLICT_RESOLVER.identifier}__{index}",
            subject=subject,
            paths=tuple(paths),
            documents={path: _clip(snapshot.files[path], MAX_DOCUMENT_CHARS) for path in paths},
            depends_on=tuple(sorted(set(depends))),
        ))

    drafts: List[DraftNode] = []
    audited = {node.path for node in audits}
    for index, path in enumerate(sorted(path for path in audited if _drafter_eligible(snapshot, path))):
        record = snapshot.catalog.record_for(path)
        assert record is not None  # _drafter_eligible already proved it
        drafts.append(DraftNode(
            node_id=f"{CONTRACT_DRAFTER.identifier}__{index}",
            path=path,
            depends_on=tuple(record.depends_on),
        ))
    return GraphPlan(tuple(audits), tuple(conflicts), tuple(drafts))


def build_drafter(snapshot: RepositorySnapshot, path: str) -> DraftNode:
    """Construct a Drafter node, refusing outright for any non-contract document."""
    record = snapshot.catalog.record_for(path)
    document_type = record.type if record else "unknown"
    if not CONTRACT_DRAFTER.permits(document_type):
        raise PrivilegeError(
            f"The Contract Drafter may not be constructed for {path!r}: it is a {document_type} document, "
            "and only contract prose is mechanically recoverable from source."
        )
    if not _drafter_eligible(snapshot, path):
        raise PrivilegeError(
            f"The Contract Drafter may not be constructed for {path!r}: it is protected, "
            "requires human approval, or declares no dependencies."
        )
    assert record is not None
    return DraftNode(node_id=f"{CONTRACT_DRAFTER.identifier}__0", path=path, depends_on=tuple(record.depends_on))


# ---------------------------------------------------------------------------
# Response contracts (validated by deterministic code, never trusted as-is)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceVerdict:
    path: str
    supported: bool
    confidence: str
    unsupported_claims: List[str]
    reason: str


@dataclass(frozen=True)
class ConflictRuling:
    subject: str
    canonical_path: str
    superseded_paths: List[str]
    reason: str
    needs_human: bool


@dataclass(frozen=True)
class DraftPatch:
    path: str
    original_span: str
    proposed_span: str
    cited_sources: List[str]
    factual_tokens: List[str]
    reason: str


def parse_json_object(value: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise AgentContractError("The agent did not return a JSON object.")
        candidate = value[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AgentContractError(f"The agent returned malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentContractError("The agent response must be a JSON object.")
    return parsed


def _string_list(payload: Dict[str, Any], key: str) -> List[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentContractError(f"{key!r} must be an array of strings.")
    return [item for item in value]


def parse_evidence_verdict(payload: Dict[str, Any], *, expected_path: str) -> EvidenceVerdict:
    if payload.get("path") not in {expected_path, None}:
        raise AgentContractError("The Evidence Auditor answered about a document it was not given.")
    if not isinstance(payload.get("supported"), bool):
        raise AgentContractError("'supported' must be a boolean.")
    confidence = payload.get("confidence")
    if confidence not in {"high", "medium", "low"}:
        raise AgentContractError("'confidence' must be high, medium, or low.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise AgentContractError("'reason' must be a non-empty string.")
    return EvidenceVerdict(
        path=expected_path,
        supported=bool(payload["supported"]),
        confidence=str(confidence),
        unsupported_claims=_string_list(payload, "unsupported_claims"),
        reason=reason,
    )


def parse_conflict_ruling(payload: Dict[str, Any], *, allowed_paths: Sequence[str]) -> ConflictRuling:
    canonical = payload.get("canonical_path")
    if canonical not in allowed_paths:
        raise AgentContractError("The Conflict Resolver named a canonical document outside the conflict.")
    superseded = _string_list(payload, "superseded_paths")
    outside = [path for path in superseded if path not in allowed_paths]
    if outside:
        raise AgentContractError("The Conflict Resolver superseded documents outside the conflict.")
    if canonical in superseded:
        raise AgentContractError("A document cannot be both canonical and superseded.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise AgentContractError("'reason' must be a non-empty string.")
    return ConflictRuling(
        subject=str(payload.get("subject", "")),
        canonical_path=str(canonical),
        superseded_paths=superseded,
        reason=reason,
        needs_human=bool(payload.get("needs_human", True)),
    )


def parse_draft_patch(payload: Dict[str, Any], *, expected_path: str) -> Optional[DraftPatch]:
    if payload.get("path") not in {expected_path, None}:
        raise AgentContractError("The Contract Drafter answered about a document it was not given.")
    proposed = payload.get("proposed_span", "")
    if not isinstance(proposed, str):
        raise AgentContractError("'proposed_span' must be a string.")
    if not proposed.strip():
        # The Drafter declining is a valid, safe answer.
        return None
    original = payload.get("original_span")
    if not isinstance(original, str) or not original.strip():
        raise AgentContractError("'original_span' must be a non-empty string.")
    return DraftPatch(
        path=expected_path,
        original_span=original,
        proposed_span=proposed,
        cited_sources=_string_list(payload, "cited_sources"),
        factual_tokens=_string_list(payload, "factual_tokens"),
        reason=str(payload.get("reason", "")),
    )


# ---------------------------------------------------------------------------
# Tool payloads (pure data; the Strands wrappers are thin)
# ---------------------------------------------------------------------------


def evidence_payload(snapshot: RepositorySnapshot, path: str) -> Dict[str, Any]:
    """Everything Agent A may see about one document — and nothing about its status."""
    record = snapshot.catalog.record_for(path)
    if record is None:
        return {"path": path, "known": False}
    dependencies = dependency_evidence(snapshot, record)
    ledger_entries = (
        snapshot.source_ledger_entries
        if snapshot.source_ledger_entries is not None
        else Ledger(snapshot.root / snapshot.ledger_path).entries()
    )
    return {
        "path": path,
        "known": True,
        "type": record.type,
        "declared_dependencies": list(record.depends_on),
        "dependency_files": [
            {"path": item.path, "sha256": item.sha256} for item in dependencies
        ],
        "dependency_fingerprint": dependency_fingerprint(dependencies),
        "dependency_summary": [item.to_dict() for item in dependency_summary(record, dependencies)],
        # Ledger actions and hashes only: no reason text that might restate a
        # previous Doc Governor conclusion about this document.
        "ledger_actions": [
            {"action": entry.get("action"), "dependency_fingerprint": entry.get("dependency_fingerprint")}
            for entry in ledger_entries
            if entry.get("document") == path
        ],
        "supabase_inventory": {
            key: value
            for key, value in snapshot.supabase.items()
            if key in {"config_functions", "source_functions", "jwt_flags", "rpc_functions", "storage_buckets"}
        },
    }


def declared_source_payload(
    snapshot: RepositorySnapshot,
    allowed_patterns: Sequence[str],
    path: str,
) -> Dict[str, Any]:
    """Read one source file, but only if a document already declared it."""
    normalized = str(path).replace("\\", "/")
    if not any(matches_repo_glob(normalized, pattern) for pattern in allowed_patterns):
        return {
            "path": normalized,
            "readable": False,
            "reason": "This file is not declared as a dependency of the documents in scope.",
        }
    absolute = repository_path(snapshot.root, normalized)
    if absolute is None or not absolute.exists() or not absolute.is_file():
        return {"path": normalized, "readable": False, "reason": "The file is absent from the working tree."}
    try:
        content = absolute.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"path": normalized, "readable": False, "reason": "The file is not readable as UTF-8 text."}
    return {"path": normalized, "readable": True, "content": _clip(content, MAX_SOURCE_CHARS)}


def cited_source_texts(
    snapshot: RepositorySnapshot,
    patterns: Sequence[str],
    cited: Iterable[str],
) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for path in cited:
        payload = declared_source_payload(snapshot, patterns, path)
        if payload.get("readable"):
            texts[str(payload["path"])] = str(payload["content"])
    return texts


# ---------------------------------------------------------------------------
# The final deterministic ruling
# ---------------------------------------------------------------------------


@dataclass
class GraphOutcome:
    verdicts: List[EvidenceVerdict] = field(default_factory=list)
    rulings: List[ConflictRuling] = field(default_factory=list)
    drafts: List[Tuple[DraftPatch, DraftValidation]] = field(default_factory=list)
    trace: List[Dict[str, str]] = field(default_factory=list)


def confine_model_finding(finding: Finding) -> Finding:
    """Model output may add findings; it may never authorize an unproven mutation.

    Two actions survive: a duplicate merge, which deterministic code re-proves by
    comparing normalized content, and a contract span draft, which deterministic
    code re-proves by re-running grounding validation at apply time. Everything
    else becomes a human block.
    """
    if finding.kind == "duplicate" and finding.action == "merge_new_file":
        return finding
    if finding.action == "draft_contract_span":
        return finding
    finding.risk = "high"
    finding.action = "block"
    finding.proposed_patch = None
    if not finding.human_decision:
        finding.human_decision = (
            "Review this semantic finding; model-only evidence cannot authorize a repository change."
        )
    return finding


def rule(
    snapshot: RepositorySnapshot,
    baseline: GovernanceDecision,
    outcome: GraphOutcome,
    *,
    model_id: str,
) -> GovernanceDecision:
    """Turn agent output into findings. Deterministic code decides, every time (P4)."""
    findings: List[Finding] = list(baseline.findings)
    # Keyed on the action as well as the kind: a deterministic low-risk
    # `mark_stale` and an adversarial `block` on the same document are different
    # statements, and collapsing them would silently drop the stronger one.
    existing: Dict[Tuple[str, str, Tuple[str, ...]], Finding] = {
        (item.kind, item.action, tuple(item.documents)): item for item in baseline.findings
    }

    def add(finding: Finding) -> Optional[Finding]:
        key = (finding.kind, finding.action, tuple(finding.documents))
        if key in existing:
            return existing[key]
        confined = confine_model_finding(finding)
        existing[(confined.kind, confined.action, tuple(confined.documents))] = confined
        findings.append(confined)
        return None

    for verdict in outcome.verdicts:
        if verdict.supported:
            continue
        add(Finding(
            kind="stale",
            risk="high",
            action="block",
            documents=[verdict.path],
            reason=(
                "The Evidence Auditor could not find evidence supporting this document's claims: "
                + verdict.reason
            ),
            evidence=[Evidence(path=verdict.path, kind="model_verdict", detail=f"confidence={verdict.confidence}")],
            human_decision=(
                "Unsupported claim(s): " + " | ".join(verdict.unsupported_claims)
                if verdict.unsupported_claims
                else None
            ),
        ))

    for ruling in outcome.rulings:
        for superseded in ruling.superseded_paths:
            add(Finding(
                kind="duplicate",
                risk="high",
                action="block",
                # documents[0] is the superseded document and documents[1] the
                # canonical one, which is the order the trust state reads to
                # produce a "read this instead" pointer.
                documents=[superseded, ruling.canonical_path],
                reason=(
                    f"The Conflict Resolver ruled {ruling.canonical_path} canonical for "
                    f"{ruling.subject or 'this subject'}: {ruling.reason}"
                ),
                human_decision=(
                    "The Conflict Resolver could not settle this from the available evidence."
                    if ruling.needs_human
                    else f"Confirm {ruling.canonical_path} as canonical and mark the others superseded."
                ),
            ))

    for draft, validation in outcome.drafts:
        if validation.ok:
            add(Finding(
                kind="stale",
                risk="low",
                action="draft_contract_span",
                documents=[draft.path],
                reason=(
                    "Every factual token in the proposed contract span is grounded in a cited source file."
                ),
                evidence=[Evidence(path=path, kind="cited_source") for path in sorted(draft.cited_sources)],
                proposed_patch=json.dumps(
                    {
                        "original_span": draft.original_span,
                        "proposed_span": draft.proposed_span,
                        # Carried through so `apply_safe_actions` re-proves the
                        # model's own declared tokens, not only the extracted ones.
                        "factual_tokens": sorted(draft.factual_tokens),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))
            continue
        # A draft that cannot be proven is discarded whole, never partially
        # applied (P3). The document is then refused rather than repaired: this
        # finding blocks the pull request, and `blocked_paths` flips the document
        # to `usable: false` so the MCP layer stops serving it either way.
        note = (
            "A proposed contract rewrite was discarded because it could not be grounded in source, "
            f"so this document is refused instead: {validation.reason}"
        )
        duplicate = add(Finding(
            kind="stale",
            risk="high",
            action="block",
            documents=[draft.path],
            reason=note,
            human_decision="Update this document by hand, or re-verify it once the source is described correctly.",
        ))
        if duplicate is not None:
            duplicate.human_decision = (
                f"{duplicate.human_decision} {note}".strip() if duplicate.human_decision else note
            )

    result = (
        "action_required"
        if any(item.risk == "high" for item in findings)
        else ("changed" if findings else "pass")
    )
    return GovernanceDecision(
        run_id=baseline.run_id,
        mode=baseline.mode,
        result=result,
        changed=False,
        findings=findings,
        head_sha=baseline.head_sha,
        model_used=True,
        model_trace=outcome.trace + [{"event": "model_complete", "name": model_id}],
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

#: A runner turns a plan into raw node responses keyed by node id.
GraphRunner = Callable[[GraphPlan], Tuple[Dict[str, str], List[Dict[str, str]]]]


def interpret(
    snapshot: RepositorySnapshot,
    plan: GraphPlan,
    responses: Dict[str, str],
) -> GraphOutcome:
    """Validate every raw response. A schema violation fails closed for the whole run."""
    outcome = GraphOutcome()
    for node in plan.audits:
        raw = responses.get(node.node_id)
        if raw is None:
            continue
        outcome.verdicts.append(
            parse_evidence_verdict(parse_json_object(raw), expected_path=node.path)
        )
    for node in plan.conflicts:
        raw = responses.get(node.node_id)
        if raw is None:
            continue
        outcome.rulings.append(
            parse_conflict_ruling(parse_json_object(raw), allowed_paths=node.paths)
        )
    unsupported = {verdict.path for verdict in outcome.verdicts if not verdict.supported}
    for node in plan.drafts:
        raw = responses.get(node.node_id)
        if raw is None:
            continue
        if node.path not in unsupported:
            # The Drafter only ever acts on a document the Auditor faulted.
            continue
        draft = parse_draft_patch(parse_json_object(raw), expected_path=node.path)
        if draft is None:
            continue
        record = snapshot.catalog.record_for(node.path)
        if record is None:
            continue
        validation = validate_draft(
            document_path=node.path,
            document_type=record.type,
            declared_dependencies=record.depends_on,
            current_text=snapshot.files.get(node.path, ""),
            original_span=draft.original_span,
            proposed_span=draft.proposed_span,
            cited_sources=cited_source_texts(snapshot, record.depends_on, draft.cited_sources),
            declared_tokens=draft.factual_tokens,
            protected=snapshot.catalog.is_protected(node.path),
            approval=record.approval,
        )
        outcome.drafts.append((draft, validation))
    return outcome


def run_graph(
    snapshot: RepositorySnapshot,
    baseline: GovernanceDecision,
    *,
    model_id: Optional[str] = None,
    runner: Optional[GraphRunner] = None,
) -> GovernanceDecision:
    """Plan the graph, execute it, validate every response, then rule deterministically."""
    resolved_model = model_id or DEFAULT_MODEL_ID
    plan = plan_graph(snapshot, baseline)
    if plan.empty:
        decision = GovernanceDecision(
            run_id=baseline.run_id,
            mode=baseline.mode,
            result=baseline.result,
            changed=False,
            findings=baseline.findings,
            head_sha=baseline.head_sha,
            model_used=False,
            model_trace=[{"event": "graph_empty", "name": "no_governed_document_changed"}],
        )
        return decision
    execute = runner or strands_runner(snapshot, model_id=resolved_model)
    responses, trace = execute(plan)
    outcome = interpret(snapshot, plan, responses)
    outcome.trace = trace
    return rule(snapshot, baseline, outcome, model_id=resolved_model)


# ---------------------------------------------------------------------------
# Strands wiring
# ---------------------------------------------------------------------------


class _ToolBudget:
    """Enforce each agent's tool budget *before* the call rather than auditing the trace after.

    Cancelling in ``BeforeToolCallEvent`` means an over-budget or out-of-scope call
    never executes, so the budget is a boundary rather than a report.
    """

    def __init__(self, spec: AgentSpec, node_id: str, trace: List[Dict[str, str]]) -> None:
        self.spec = spec
        self.node_id = node_id
        self.trace = trace
        self.calls = 0
        self.output_attempts = 0

    def register_hooks(self, registry: Any, **_: Any) -> None:  # pragma: no cover - needs strands
        from strands.hooks import BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: Any) -> None:  # pragma: no cover - needs strands
        name = str(event.tool_use.get("name", ""))
        if name not in self.spec.tool_names:
            event.cancel_tool = (
                f"{self.spec.role} is not permitted to call {name!r}."
            )
            self.trace.append({"event": "tool_denied", "name": f"{self.node_id}:{name}"})
            return
        if name == "GovernanceOutput":
            if self.output_attempts >= 3:
                event.cancel_tool = "Structured output attempt budget exhausted."
                self.trace.append({"event": "tool_budget_exceeded", "name": f"{self.node_id}:{name}"})
                return
            self.output_attempts += 1
            self.trace.append({"event": "tool_call", "name": f"{self.node_id}:{name}"})
            return
        if self.calls >= self.spec.max_tool_calls:
            event.cancel_tool = (
                f"{self.spec.role} has used its budget of {self.spec.max_tool_calls} tool calls."
            )
            self.trace.append({"event": "tool_budget_exceeded", "name": f"{self.node_id}:{name}"})
            return
        self.calls += 1
        # Identifiers only: the trace never records arguments or document text (§8.7).
        self.trace.append({"event": "tool_call", "name": f"{self.node_id}:{name}"})


def _node_prompt(spec: AgentSpec, body: str) -> str:
    return f"{spec.system_prompt}\n\n--- YOUR ASSIGNMENT ---\n{body}"


def _model_retry_strategy() -> Any:
    from strands import ModelRetryStrategy

    class BoundedStreamRetry(ModelRetryStrategy):
        def is_retryable(self, exception: Exception) -> bool:
            code = getattr(exception, "response", {}).get("Error", {}).get("Code")
            return code == "modelStreamErrorException" or super().is_retryable(exception)

    return BoundedStreamRetry(max_attempts=2, initial_delay=1, max_delay=1)


def strands_runner(
    snapshot: RepositorySnapshot,
    *,
    model_id: str,
) -> GraphRunner:  # pragma: no cover - exercised by the model-enabled demo
    """Build the real Strands Graph. Each node is an agent that can see only its own slice."""

    def execute(plan: GraphPlan) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
        from strands import Agent, tool
        from strands.models import BedrockModel
        from strands.multiagent import GraphBuilder
        from pydantic import BaseModel, ConfigDict, Field, create_model

        trace: List[Dict[str, str]] = []
        model = BedrockModel(
            model_id=model_id,
            temperature=0,
            max_tokens=4096,
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
        builder = GraphBuilder()
        builder.set_execution_timeout(
            float(os.environ.get("DOCGOV_GRAPH_TIMEOUT_SECONDS", DEFAULT_GRAPH_TIMEOUT_SECONDS))
        )
        builder.set_node_timeout(float(
            os.environ.get("DOCGOV_NODE_TIMEOUT_SECONDS")
            # Honour the action's existing single-agent timeout input.
            or os.environ.get("DOCGOV_MODEL_TIMEOUT_SECONDS")
            or DEFAULT_NODE_TIMEOUT_SECONDS
        ))

        class StrictOutput(BaseModel):
            model_config = ConfigDict(extra="forbid", strict=True)

        def output_schema(spec: AgentSpec, paths: Tuple[str, ...]) -> Any:
            if spec == EVIDENCE_AUDITOR:
                fields = dict(path=(Literal[paths[0]], ...), supported=(bool, ...),
                    confidence=(Literal["high", "medium", "low"], ...),
                    unsupported_claims=(List[str], Field(max_length=3)), reason=(str, Field(min_length=1, max_length=600)))
            elif spec == CONFLICT_RESOLVER:
                fields = dict(subject=(str, ...), canonical_path=(Literal[paths], ...),
                    superseded_paths=(List[Literal[paths]], ...), needs_human=(bool, ...),
                    reason=(str, Field(min_length=1)))
            else:
                fields = dict(path=(Literal[paths[0]], ...), original_span=(str, ...),
                    proposed_span=(str, ...), cited_sources=(List[str], ...),
                    factual_tokens=(List[str], ...), reason=(str, Field(min_length=1)))
            return create_model("GovernanceOutput", __base__=StrictOutput, **fields)

        def make_agent(spec: AgentSpec, node_id: str, body: str, tools: List[Any],
                       paths: Tuple[str, ...]) -> Any:
            assert_read_only(spec)
            return Agent(
                model=model,
                tools=tools,
                system_prompt=_node_prompt(spec, body) + "\nSubmit the final answer using GovernanceOutput. Keep reasons under 600 characters; report at most three concise unsupported claims. Do not reproduce the entire document.",
                structured_output_model=output_schema(spec, paths),
                retry_strategy=_model_retry_strategy(),
                hooks=[_ToolBudget(spec, node_id, trace)],
                callback_handler=None,
            )

        # Each tool is built by a factory so the per-node binding is a real
        # closure. Binding through a default argument instead would leak a
        # leading-underscore parameter into the tool's generated schema, which
        # Strands rejects.
        def make_evidence_tool(audited_path: str) -> Any:
            @tool(name="evidence_for_document")
            def evidence_for_document(path: str) -> str:
                """Return the recorded evidence for the document under audit."""
                if path.replace("\\", "/") != audited_path:
                    return json.dumps({
                        "path": path,
                        "known": False,
                        "reason": "You may only request evidence for the document you were assigned.",
                    })
                return json.dumps(evidence_payload(snapshot, audited_path), ensure_ascii=False)

            return evidence_for_document

        def make_source_tool(allowed: Tuple[str, ...]) -> Any:
            @tool(name="declared_source")
            def declared_source(path: str) -> str:
                """Read a source file one of the documents in scope declares as a dependency."""
                return json.dumps(declared_source_payload(snapshot, allowed, path), ensure_ascii=False)

            return declared_source

        def make_target_tool(drafted_path: str) -> Any:
            @tool(name="target_document")
            def target_document() -> str:
                """Return the current text of the contract document being re-drafted."""
                return json.dumps(
                    {
                        "path": drafted_path,
                        "content": _clip(snapshot.files.get(drafted_path, ""), MAX_DOCUMENT_CHARS),
                    },
                    ensure_ascii=False,
                )

            return target_document

        audit_ids: List[str] = []
        for node in plan.audits:
            body = (
                f"Document under audit: {node.path}\n"
                "Its self-reported status has been removed on purpose.\n"
                "--- CLAIMS ---\n"
                f"{node.claims}\n"
            )
            builder.add_node(
                make_agent(EVIDENCE_AUDITOR, node.node_id, body, [make_evidence_tool(node.path)], (node.path,)),
                node.node_id,
            )
            builder.set_entry_point(node.node_id)
            audit_ids.append(node.node_id)

        for node in plan.conflicts:
            patterns = node.depends_on
            rendered = "\n\n".join(
                f"--- {path} ---\n{text}" for path, text in sorted(node.documents.items())
            )
            body = (
                f"Conflicting documents: {', '.join(node.paths)}\n"
                f"Readable source files must match: {', '.join(patterns) or '(none declared)'}\n\n"
                f"{rendered}\n"
            )
            builder.add_node(
                make_agent(CONFLICT_RESOLVER, node.node_id, body, [make_source_tool(patterns)], node.paths),
                node.node_id,
            )
            for audit_id in audit_ids:
                builder.add_edge(audit_id, node.node_id)
            if not audit_ids:
                builder.set_entry_point(node.node_id)

        for node in plan.drafts:
            patterns = node.depends_on
            body = (
                f"Contract document: {node.path}\n"
                f"You may cite only files matching: {', '.join(patterns)}\n"
                "Re-draft the smallest wrong span, or return an empty proposed_span to decline.\n"
            )
            builder.add_node(
                make_agent(
                    CONTRACT_DRAFTER,
                    node.node_id,
                    body,
                    [make_target_tool(node.path), make_source_tool(patterns)],
                    (node.path,),
                ),
                node.node_id,
            )
            source = next(
                (audit.node_id for audit in plan.audits if audit.path == node.path),
                None,
            )
            if source is not None:
                builder.add_edge(
                    source,
                    node.node_id,
                    condition=_unsupported_condition(source),
                )
            else:  # pragma: no cover - plan_graph only drafts audited documents
                builder.set_entry_point(node.node_id)

        graph = builder.build()
        result = graph(
            "Audit the assigned documents against their evidence, then answer in the JSON schema "
            "given in your instructions. Do not answer about any document other than your own."
        )
        responses: Dict[str, str] = {}
        for node_id, node_result in result.results.items():
            texts = [_result_text(item) for item in node_result.get_agent_results()]
            if texts:
                responses[str(node_id)] = texts[-1]
            trace.append({"event": "agent_complete", "name": str(node_id)})
        return responses, trace

    return execute


def _unsupported_condition(auditor_node_id: str) -> Callable[[Any], bool]:  # pragma: no cover - needs strands
    """Only let the Drafter run when its auditor actually faulted the document."""

    def condition(state: Any) -> bool:
        node_result = getattr(state, "results", {}).get(auditor_node_id)
        if node_result is None:
            return False
        for agent_result in node_result.get_agent_results():
            try:
                payload = parse_json_object(_result_text(agent_result))
            except AgentContractError:
                return False
            if payload.get("supported") is False:
                return True
        return False

    return condition


def _result_text(result: Any) -> str:
    structured = getattr(result, "structured_output", None)
    if structured is not None:
        return structured.model_dump_json()
    if isinstance(result, str):
        return result
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content", [])
        if isinstance(content, list):
            text = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            if text:
                return text
    return str(result)
