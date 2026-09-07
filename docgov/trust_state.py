"""Deterministic, committed trust state consumed by the MCP supply layer.

`.docgov/trust.json` is the table the MCP server looks documents up in. It is
produced only by deterministic code (P4) at pull-request and daily-audit time
(D1), so the read path never needs a model, a network call, or a Git history
walk.

Two properties are load-bearing:

* **Determinism.** Re-running the governor against an unchanged working tree
  must produce a byte-identical file, otherwise the correction-commit machinery
  would churn a commit on every run. Every timestamp and commit SHA recorded
  here therefore describes the *verification* that established the trust record,
  never the run that serialized it.
* **Fail-closed defaults.** A document the governor cannot positively prove is
  written as ``usable: false`` with a reason, never omitted (D6). Silence would
  send the consuming agent to the raw file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .catalog import Catalog
from .engine import (
    RepositorySnapshot,
    analyze_trust,
    dependency_evidence,
    dependency_fingerprint,
    expired,
)
from .ledger import Ledger, sha256_text
from .models import DocumentRecord, Finding, GovernanceDecision, TrustResult
from .patterns import matches_repo_glob


TRUST_STATE_VERSION = 1
DEFAULT_TRUST_STATE_PATH = ".docgov/trust.json"

#: Ledger actions that record the outcome of a cross-environment drift check.
DRIFT_ACTION = "environment_drift"
DRIFT_CLEARED_ACTION = "environment_drift_cleared"
#: Synthetic ledger document key for an environment-level record.
ENVIRONMENT_DOCUMENT_PREFIX = "supabase:"


class TrustStateError(RuntimeError):
    """Raised when a trust state file cannot be trusted to describe this repository."""


@dataclass(frozen=True)
class TrustEntry:
    """One document's precomputed trust record."""

    path: str
    type: str
    usable: bool
    scope: str
    reason: str
    dependency_fingerprint: str
    content_sha256: str
    canonical_path: Optional[str] = None
    source_pointers: List[str] = field(default_factory=list)
    verified_at: Optional[str] = None
    head_sha: Optional[str] = None
    verifier: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if self.verifier is None:
            value.pop("verifier")
        return value

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TrustEntry":
        return cls(
            path=str(value["path"]),
            type=str(value.get("type", "unknown")),
            usable=bool(value.get("usable", False)),
            scope=str(value.get("scope", "untrusted")),
            reason=str(value.get("reason", "")),
            dependency_fingerprint=str(value.get("dependency_fingerprint", "")),
            content_sha256=str(value.get("content_sha256", "")),
            canonical_path=(
                str(value["canonical_path"]) if value.get("canonical_path") else None
            ),
            source_pointers=[str(item) for item in value.get("source_pointers", [])],
            verified_at=(str(value["verified_at"]) if value.get("verified_at") else None),
            head_sha=(str(value["head_sha"]) if value.get("head_sha") else None),
            verifier=(str(value["verifier"]) if value.get("verifier") else None),
        )


def _control_documents(catalog: Catalog) -> set[str]:
    return {str(item) for item in catalog.policies.get("control_documents", ["AGENTS.md"])}


def _latest_baseline(ledger_entries: Iterable[Dict[str, Any]], path: str) -> Optional[Dict[str, Any]]:
    baseline: Optional[Dict[str, Any]] = None
    for entry in ledger_entries:
        if entry.get("document") == path and entry.get("action") == "verify_current":
            baseline = entry
    return baseline


def unresolved_drift_environments(ledger_entries: Iterable[Dict[str, Any]]) -> set[str]:
    """Return environments whose most recent drift check is still unresolved.

    The ledger is append-only, so "resolved" is expressed as a later
    ``environment_drift_cleared`` entry rather than by mutating the drift entry.
    """
    latest: Dict[str, str] = {}
    for entry in ledger_entries:
        document = str(entry.get("document", ""))
        action = str(entry.get("action", ""))
        if not document.startswith(ENVIRONMENT_DOCUMENT_PREFIX):
            continue
        if action in {DRIFT_ACTION, DRIFT_CLEARED_ACTION}:
            latest[document[len(ENVIRONMENT_DOCUMENT_PREFIX) :]] = action
    return {name for name, action in latest.items() if action == DRIFT_ACTION}


def drift_environments_from_findings(findings: Iterable[Finding]) -> set[str]:
    """Return the environments named by ``environment_drift`` findings."""
    environments: set[str] = set()
    for finding in findings:
        if finding.kind != DRIFT_ACTION:
            continue
        for item in finding.evidence:
            if item.kind == "environment" and item.path:
                environments.add(item.path)
    return environments


def record_depends_on_environment(record: DocumentRecord, environment: str) -> bool:
    """Return whether a catalog record declares a dependency on a named environment.

    Two declarations are honoured: an explicit ``environments`` list on the
    record, and an environment name appearing as a path segment in one of the
    record's ``depends_on`` patterns (which is how Supabase Advisor evidence is
    laid out on disk).
    """
    if environment in record.environments:
        return True
    for pattern in record.depends_on:
        segments = pattern.replace("\\", "/").split("/")
        if environment in segments:
            return True
    return False


def blocked_paths(findings: Iterable[Finding]) -> Dict[str, str]:
    """Return documents a high-risk finding blocked in this run, with a safe reason.

    The reason is built from the finding's *kind*, never from its prose. A model
    finding's reason can quote the document verbatim, and the trust state is
    served to the consuming agent on refusal — quoting a refused document there
    would leak the content the refusal exists to withhold (§8.3).
    """
    reasons = {
        "stale": "Doc Governor blocked this document in its latest run: its claims are not supported by evidence.",
        "conflict": "Doc Governor blocked this document in its latest run: it conflicts with another document.",
        "duplicate": "Doc Governor blocked this document in its latest run: another document is canonical for this subject.",
        "unverified_date": "Doc Governor blocked this document in its latest run: a verification date changed without evidence.",
        "misclassified": "Doc Governor blocked this document in its latest run: it is not classified as a governed type.",
        "orphan": "Doc Governor blocked this document in its latest run: it is not present in the checkout.",
        "environment_drift": "Doc Governor blocked this document in its latest run: its environment drifted from Git.",
    }
    blocked: Dict[str, str] = {}
    for finding in findings:
        if finding.risk != "high" or finding.action != "block":
            continue
        for path in finding.documents:
            blocked.setdefault(
                path,
                reasons.get(finding.kind, "Doc Governor blocked this document in its latest run."),
            )
    return blocked


def _canonical_pointers(
    catalog: Catalog,
    findings: Iterable[Finding],
) -> Dict[str, str]:
    """Map a document path to the document a reader should consult instead."""
    pointers: Dict[str, str] = {}

    # A catalog record that declares itself non-canonical names its canonical peer.
    canonical_by_key: Dict[str, str] = {
        f"{record.type}:{record.canonical_key}": record.path
        for record in catalog.documents
        if record.authority == "canonical" and record.canonical_key
    }
    for record in catalog.documents:
        if record.authority == "canonical" or not record.canonical_key:
            continue
        canonical = canonical_by_key.get(f"{record.type}:{record.canonical_key}")
        if canonical and canonical != record.path:
            pointers[record.path] = canonical

    # A duplicate finding names [new_file, canonical_file] in that order.
    for finding in findings:
        if finding.kind == "duplicate" and len(finding.documents) == 2:
            new_path, canonical_path = finding.documents
            if new_path != canonical_path:
                pointers.setdefault(new_path, canonical_path)
    return pointers


def _usable(
    snapshot: RepositorySnapshot,
    record: Optional[DocumentRecord],
    trust_result: TrustResult,
    *,
    is_control_document: bool,
    drifted_environments: set[str],
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Collapse the internal four-level scope into the binary the MCP layer exposes (D3).

    ``analyze_trust`` is the single source of truth for the trust verdict; this
    function only adds guards that are strictly more conservative, so the MCP
    layer can never be more permissive than ``docgov verify --strict``.
    """
    if record is None:
        return False, trust_result.reason
    if trust_result.scope == "untrusted":
        return False, trust_result.reason
    if record.status == "review_required":
        return False, "The catalog record is still awaiting its first human review."
    if record.status != "current":
        return False, f"Catalog status is {record.status!r}, not current."
    for environment in sorted(drifted_environments):
        if record.type in {"state", "procedure"} and record_depends_on_environment(record, environment):
            return False, (
                f"The {environment} environment has drifted from the last state Git produced, "
                "so no document describing it can be trusted until the drift is resolved."
            )
    # `analyze_trust` only applies the TTL to state and procedure documents; a
    # catalog that sets an explicit ttl_days on another type gets the stricter
    # reading here (P3).
    if not is_control_document and expired(record, snapshot.catalog, now=now):
        return False, (
            f"The document has passed its {snapshot.catalog.ttl_for(record)}-day verification window."
        )
    return True, trust_result.reason


def _tombstones(
    snapshot: RepositorySnapshot,
    ledger_entries: Iterable[Dict[str, Any]],
    known_paths: set[str],
) -> List[Dict[str, Any]]:
    """Return records for documents the governor merged away.

    A consuming agent that still holds a link to a removed duplicate must be told
    where the content went. Dropping the path instead would return "unknown" and
    send the agent to the raw file to find out (D2, D6).
    """
    merged: Dict[str, Optional[str]] = {}
    for entry in ledger_entries:
        if entry.get("action") != "remove_new_duplicate":
            continue
        path = str(entry.get("document", ""))
        if not path or path in known_paths or path in snapshot.files:
            continue
        evidence = entry.get("evidence") or []
        canonical = None
        if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
            canonical = evidence[0].get("path")
        merged[path] = str(canonical) if canonical else None
    return [
        TrustEntry(
            path=path,
            type=snapshot.catalog.classify(path) or "unknown",
            usable=False,
            scope="untrusted",
            reason=(
                f"This document was merged into {canonical} and removed; "
                "the canonical document holds its content."
                if canonical
                else "This document was merged into its canonical peer and removed."
            ),
            dependency_fingerprint="",
            content_sha256="",
            canonical_path=canonical,
        ).to_dict()
        for path, canonical in sorted(merged.items())
    ]


def build_trust_state(
    decision: GovernanceDecision,
    snapshot: RepositorySnapshot,
    *,
    ledger_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the serializable trust state for every governed document.

    ``decision`` supplies the findings that explain *why* a document is not
    usable and where to look instead; the trust verdicts themselves are recomputed
    deterministically so the state file never depends on which subcommand produced
    the decision.
    """
    resolved_ledger_path = ledger_path or (snapshot.root / snapshot.ledger_path)
    ledger_entries = (
        snapshot.source_ledger_entries
        if snapshot.source_ledger_entries is not None
        else Ledger(resolved_ledger_path).entries()
    )
    drifted = unresolved_drift_environments(ledger_entries) | drift_environments_from_findings(
        decision.findings
    )
    trust = analyze_trust(snapshot, resolved_ledger_path, now=now)
    pointers = _canonical_pointers(snapshot.catalog, list(decision.findings) + list(trust.findings))
    blocked = blocked_paths(decision.findings)
    controls = _control_documents(snapshot.catalog)

    entries: List[Dict[str, Any]] = []
    for result in trust.trust_results:
        record = snapshot.catalog.record_for(result.path)
        usable, reason = _usable(
            snapshot,
            record,
            result,
            is_control_document=result.path in controls,
            drifted_environments=drifted,
            now=now,
        )
        if usable and result.path in blocked:
            usable, reason = False, blocked[result.path]
        dependencies = dependency_evidence(snapshot, record) if record else []
        baseline = _latest_baseline(ledger_entries, result.path)
        content = snapshot.files.get(result.path)
        entries.append(
            TrustEntry(
                path=result.path,
                type=result.type,
                usable=usable,
                scope=result.scope,
                reason=reason,
                dependency_fingerprint=dependency_fingerprint(dependencies),
                content_sha256=sha256_text(content) if content is not None else "",
                canonical_path=pointers.get(result.path),
                source_pointers=[item.path for item in dependencies],
                # Both fields describe the verification that established this
                # record, never the run that serialized the file, so an unchanged
                # tree always serializes to identical bytes.
                verified_at=(str(baseline.get("timestamp")) if baseline and baseline.get("timestamp") else None),
                head_sha=(str(baseline.get("head_sha")) if baseline and baseline.get("head_sha") else None),
                verifier=(str(baseline["verifier"]) if baseline and str(baseline.get("verifier", "")).startswith("codex:") else None),
            ).to_dict()
        )

    entries.extend(_tombstones(snapshot, ledger_entries, {item["path"] for item in entries}))
    entries.sort(key=lambda item: item["path"])
    return {
        "version": TRUST_STATE_VERSION,
        "catalog_path": snapshot.catalog_path,
        "ledger_path": snapshot.ledger_path,
        "drifted_environments": sorted(drifted),
        "documents": entries,
    }


def render_trust_state(state: Dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_trust_state(path: Path, state: Dict[str, Any]) -> bool:
    """Write the trust state, returning whether the file's bytes changed."""
    rendered = render_trust_state(state)
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return True


def load_trust_state(path: Path) -> Dict[str, Any]:
    """Load a trust state file, failing closed on anything unexpected."""
    if not path.exists():
        raise TrustStateError(
            f"No trust state at {path}. Run `docgov review --apply` to generate it."
        )
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrustStateError(f"Unable to read the trust state at {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TrustStateError(f"The trust state at {path} is not a JSON object.")
    version = parsed.get("version")
    if version != TRUST_STATE_VERSION:
        raise TrustStateError(
            f"The trust state at {path} declares version {version!r}, "
            f"but this Doc Governor understands version {TRUST_STATE_VERSION}. "
            "Regenerate it with `docgov review --apply`."
        )
    if not isinstance(parsed.get("documents"), list):
        raise TrustStateError(f"The trust state at {path} has no documents array.")
    return parsed


def trust_entries(state: Dict[str, Any]) -> Dict[str, TrustEntry]:
    """Return the state's documents keyed by path."""
    entries: Dict[str, TrustEntry] = {}
    for item in state.get("documents", []):
        if not isinstance(item, dict) or "path" not in item:
            raise TrustStateError("Every trust state document must be an object with a path.")
        entry = TrustEntry.from_dict(item)
        entries[entry.path] = entry
    return entries


def documents_for_environment(catalog: Catalog, environment: str) -> List[str]:
    """Return the state and procedure documents that describe a named environment."""
    return sorted(
        record.path
        for record in catalog.documents
        if record.type in {"state", "procedure"}
        and record_depends_on_environment(record, environment)
    )


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches_repo_glob(path, pattern) for pattern in patterns)
