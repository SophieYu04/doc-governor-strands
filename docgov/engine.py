from __future__ import annotations

import difflib
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .catalog import Catalog
from .drafting import apply_span, validate_draft
from .git_tools import (
    bytes_at_ref,
    changed_content,
    changed_paths,
    content_at_ref,
    current_sha,
    deleted_paths,
    run_git,
    tracked_paths,
    untracked_paths,
)
from .ledger import Ledger, sha256_file, sha256_text, utc_now
from .models import DocumentRecord, Evidence, Finding, GovernanceDecision, TrustResult
from .patterns import matches_repo_glob
from .supabase import inventory as supabase_inventory


DATE_PATTERN = re.compile(
    r"(?:last_verified_at|last verified|最後驗證|最後更新)\s*[`\"]?\s*[:：=]\s*[`\"]?([0-9]{4}-[0-9]{2}-[0-9]{2})",
    re.IGNORECASE,
)
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
MARKDOWN_LINK_PATTERN = re.compile(r"(\[[^\]]+\]\()([^)#]+)(#[^)]*)?(\))")
SUPABASE_MARKER_PATTERN = re.compile(r"<!--\s*docgov:supabase-inventory\s*(\{.*?\})\s*-->", re.DOTALL)


@dataclass
class RepositorySnapshot:
    root: Path
    catalog: Catalog
    catalog_path: str = ".docgov/catalog.yaml"
    ledger_path: str = ".docgov/ledger.jsonl"
    files: Dict[str, str] = field(default_factory=dict)
    changed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    removed_by_governor: set[str] = field(default_factory=set)
    diff: str = ""
    head_sha: str = "unknown"
    source_ref: Optional[str] = None
    source_ledger_entries: Optional[List[Dict[str, object]]] = None
    supabase: Dict[str, object] = field(default_factory=dict)


def markdown_files(root: Path, ref: Optional[str] = None) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for relative in tracked_paths(root, suffix=".md", ref=ref):
        if ref:
            content = content_at_ref(root, ref, relative)
            if content is not None:
                result[relative] = content
            continue
        path = root / relative
        if path.exists() and path.is_file():
            result[relative] = path.read_bytes().decode("utf-8")
    return result


def build_snapshot(
    root: Path,
    catalog_path: Path,
    base: str | None = None,
    head: str | None = None,
    ledger_path: Path | None = None,
    source_ref: str | None = None,
) -> RepositorySnapshot:
    ledger_file = ledger_path or (root / ".docgov/ledger.jsonl")
    try:
        catalog_relative = catalog_path.relative_to(root).as_posix()
    except ValueError:
        catalog_relative = str(catalog_path)
    try:
        ledger_relative = ledger_file.relative_to(root).as_posix()
    except ValueError:
        ledger_relative = str(ledger_file)
    source_ledger_entries: Optional[List[Dict[str, object]]] = None
    if source_ref:
        catalog_text = content_at_ref(root, source_ref, catalog_relative)
        if catalog_text is None:
            raise ValueError(f"Catalog is missing at {source_ref}:{catalog_relative}")
        catalog = Catalog.loads(catalog_text, source=f"{source_ref}:{catalog_relative}")
        ledger_text = content_at_ref(root, source_ref, ledger_relative)
        source_ledger_entries = Ledger.parse(ledger_text or "")
        ledger_entries = source_ledger_entries
    else:
        catalog = Catalog.load(catalog_path)
        ledger_entries = Ledger(ledger_file).entries()
    changed, added = changed_paths(root, base, head)
    removed_by_governor = {
        str(entry.get("document"))
        for entry in ledger_entries
        if entry.get("action") == "remove_new_duplicate"
    }
    return RepositorySnapshot(
        root=root,
        catalog=catalog,
        catalog_path=catalog_relative,
        ledger_path=ledger_relative,
        files=markdown_files(root, source_ref),
        changed=changed,
        added=added,
        deleted=deleted_paths(root, base, head),
        removed_by_governor=removed_by_governor,
        diff=changed_content(root, base, head),
        head_sha=head or (
            run_git(root, "rev-parse", source_ref).strip()
            if source_ref
            else current_sha(root)
        ),
        source_ref=source_ref,
        source_ledger_entries=source_ledger_entries,
        supabase=supabase_inventory(root),
    )


def normalize_markdown(value: str) -> str:
    value = re.sub(r"^\s*(狀態|最後驗證|最後更新|status|last_verified_at)\s*[:：].*$", "", value, flags=re.IGNORECASE | re.MULTILINE)
    value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    value = re.sub(r"[`*_>#|\-]", " ", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, normalize_markdown(left), normalize_markdown(right)).ratio()


def local_links(text: str) -> List[str]:
    return [match.group(1) for match in LINK_PATTERN.finditer(text) if not match.group(1).startswith(("http://", "https://", "mailto:"))]


def _resolved_link(source_path: str, target: str) -> Optional[str]:
    value = target.strip()
    if not value or value.startswith(("http://", "https://", "mailto:", "#")):
        return None
    if value.startswith("/"):
        value = value.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_path), value))


def links_to_document(source_path: str, text: str, target_path: str) -> bool:
    return any(
        _resolved_link(source_path, match.group(2)) == target_path
        for match in MARKDOWN_LINK_PATTERN.finditer(text)
    )


def rewrite_document_links(source_path: str, text: str, old_path: str, new_path: str) -> str:
    source_directory = posixpath.dirname(source_path) or "."
    replacement = posixpath.relpath(new_path, source_directory)

    def replace(match: re.Match[str]) -> str:
        if _resolved_link(source_path, match.group(2)) != old_path:
            return match.group(0)
        return f"{match.group(1)}{replacement}{match.group(3) or ''}{match.group(4)}"

    return MARKDOWN_LINK_PATTERN.sub(replace, text)


def repository_path(root: Path, relative_path: str) -> Optional[Path]:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def evidence_for_path(snapshot: RepositorySnapshot, relative_path: str) -> List[Evidence]:
    absolute = snapshot.root / relative_path
    if not absolute.exists():
        return []
    return [Evidence(path=relative_path, sha256=sha256_file(absolute))]


def impacted_records(snapshot: RepositorySnapshot) -> List[DocumentRecord]:
    impacted: List[DocumentRecord] = []
    for record in snapshot.catalog.documents:
        if record.path in snapshot.changed:
            impacted.append(record)
            continue
        for dependency in record.depends_on:
            if any(matches_repo_glob(path, dependency) for path in snapshot.changed):
                impacted.append(record)
                break
    return impacted


def changed_dependency_evidence(
    snapshot: RepositorySnapshot,
    record: DocumentRecord,
) -> List[Evidence]:
    evidence: List[Evidence] = []
    for path in snapshot.changed:
        if not any(matches_repo_glob(path, pattern) for pattern in record.depends_on):
            continue
        path_evidence = evidence_for_path(snapshot, path)
        if path_evidence:
            evidence.extend(path_evidence)
        else:
            evidence.append(Evidence(
                path=path,
                kind="deleted_dependency",
                detail="The declared dependency is absent from the working tree.",
            ))
    return evidence


def expired(record: DocumentRecord, catalog: Catalog, now: Optional[datetime] = None) -> bool:
    ttl = catalog.ttl_for(record)
    if ttl is None:
        return False
    if not record.last_verified_at:
        return True
    try:
        verified = datetime.fromisoformat(record.last_verified_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return now > verified + timedelta(days=ttl)


def _content_for_path(snapshot: RepositorySnapshot, relative_path: str) -> Optional[str]:
    if snapshot.source_ref:
        return content_at_ref(snapshot.root, snapshot.source_ref, relative_path)
    absolute = snapshot.root / relative_path
    if not absolute.exists() or not absolute.is_file():
        return None
    try:
        return absolute.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return absolute.read_bytes().hex()


def dependency_candidates(snapshot: RepositorySnapshot) -> List[str]:
    """List the repository paths a dependency glob may match.

    Exposed so a caller that resolves many documents in one pass (the MCP
    listing) can compute the Git listing once instead of once per document.
    """
    candidates = tracked_paths(snapshot.root, ref=snapshot.source_ref)
    if not snapshot.source_ref:
        candidates = sorted(set(candidates) | set(untracked_paths(snapshot.root)))
    return list(candidates)


def dependency_evidence(
    snapshot: RepositorySnapshot,
    record: DocumentRecord,
    candidates: Optional[List[str]] = None,
) -> List[Evidence]:
    if candidates is None:
        candidates = dependency_candidates(snapshot)
    evidence: List[Evidence] = []
    for relative_path in sorted({
        path
        for pattern in record.depends_on
        for path in candidates
        if matches_repo_glob(path, pattern)
    }):
        if snapshot.source_ref:
            content = bytes_at_ref(snapshot.root, snapshot.source_ref, relative_path)
        else:
            path = snapshot.root / relative_path
            content = path.read_bytes() if path.is_file() else None
        if content is None:
            continue
        evidence.append(Evidence(
            path=relative_path,
            kind="dependency",
            sha256=hashlib.sha256(content).hexdigest(),
        ))
    return evidence


def dependency_fingerprint(evidence: Iterable[Evidence]) -> str:
    digest = hashlib.sha256()
    for item in sorted(evidence, key=lambda value: value.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((item.sha256 or "").encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def dependency_summary(record: DocumentRecord, evidence: Iterable[Evidence]) -> List[Evidence]:
    items = list(evidence)
    summaries: List[Evidence] = []
    for pattern in record.depends_on:
        matched = [item for item in items if matches_repo_glob(item.path, pattern)]
        summaries.append(Evidence(
            path=pattern,
            kind="dependency_fingerprint",
            sha256=dependency_fingerprint(matched),
            detail=f"{len(matched)} file(s)",
        ))
    return summaries


def verification_record(snapshot: RepositorySnapshot, record: DocumentRecord) -> Dict[str, object]:
    dependencies = dependency_evidence(snapshot, record)
    document_content = snapshot.files.get(record.path)
    return {
        "document": record.path,
        "action": "verify_current",
        "reason": "Recorded an evidence-backed documentation baseline.",
        "evidence": dependency_summary(record, dependencies),
        "dependency_count": len(dependencies),
        "new_hash": sha256_text(document_content) if document_content is not None else None,
        "dependency_fingerprint": dependency_fingerprint(dependencies),
    }


def has_matching_verification_baseline(
    snapshot: RepositorySnapshot,
    record: DocumentRecord,
) -> bool:
    """Return whether the latest ledger proof matches the checked-out document and dependencies."""
    if record.status != "current" or record.path not in snapshot.files:
        return False
    ledger_entries = (
        snapshot.source_ledger_entries
        if snapshot.source_ledger_entries is not None
        else Ledger(snapshot.root / snapshot.ledger_path).entries()
    )
    baseline = next((
        entry
        for entry in reversed(ledger_entries)
        if entry.get("document") == record.path
        and entry.get("action") == "verify_current"
    ), None)
    if baseline is None:
        return False
    current = verification_record(snapshot, record)
    return (
        baseline.get("new_hash") == current["new_hash"]
        and baseline.get("dependency_fingerprint") == current["dependency_fingerprint"]
    )


def _status_scope(record: DocumentRecord) -> Tuple[str, str]:
    if record.type == "evidence" and record.status in {"current", "immutable"}:
        return "historical_evidence", "Evidence is immutable and only proves its recorded point in time."
    if record.type == "decision" and record.status in {"current", "accepted"}:
        return "rationale_only", "The decision is accepted as rationale, not as current implementation state."
    if record.status == "current":
        return "current_fact", "The document is current and its recorded evidence still matches."
    return "untrusted", f"Catalog status is {record.status!r}, not current."


def review_authorization_fingerprint(snapshot: RepositorySnapshot, record: DocumentRecord) -> str:
    policy_path = ".docgov/coding-agent-review-policy.json"
    policy = (content_at_ref(snapshot.root, snapshot.source_ref, policy_path) if snapshot.source_ref
              else (snapshot.root / policy_path).read_bytes().decode("utf-8"))
    definition = record.to_dict()
    # Trust review can promote status, but cannot change its own grant.
    definition.pop("status", None)
    definition.pop("last_verified_at", None)
    return sha256_text(json.dumps([policy, definition, snapshot.catalog.policies], sort_keys=True))


def has_matching_coding_review(snapshot: RepositorySnapshot, record: DocumentRecord) -> bool:
    """Recognize only a scoped, hash-bound delegated review, never blanket approval."""
    if not has_matching_verification_baseline(snapshot, record):
        return False
    try:
        def read(path):
            if snapshot.source_ref:
                return content_at_ref(snapshot.root, snapshot.source_ref, path)
            target = snapshot.root.resolve() / path
            if target.is_symlink() or any(p.is_symlink() for p in target.parents if p.is_relative_to(snapshot.root.resolve())):
                raise ValueError("symlink")
            return target.read_bytes().decode("utf-8")
        policy = json.loads(read(".docgov/coding-agent-review-policy.json"))
        if (policy.get("version") != 1 or policy.get("reviewer") != "coding_agent"
                or policy.get("status") not in {"authorized_review_pending", "enabled"}
                or record.path not in policy.get("documents", []) or record.type != "contract"):
            return False
        entries = snapshot.source_ledger_entries if snapshot.source_ledger_entries is not None else Ledger(snapshot.root / snapshot.ledger_path).entries()
        baseline = next(entry for entry in reversed(entries)
                        if entry.get("document") == record.path and entry.get("action") == "verify_current")
        if not str(baseline.get("verifier", "")).startswith("codex:"):
            return False
        evidence = [item for item in baseline.get("evidence", []) if item.get("kind") == "coding_agent_review"]
        if len(evidence) != 1:
            return False
        path = evidence[0]["path"]
        if not re.fullmatch(r"\.docgov/reviews/[a-f0-9]{64}\.json", path):
            return False
        raw = read(path)
        if sha256_text(raw) != evidence[0].get("sha256"):
            return False
        receipt = json.loads(raw)
        if receipt.get("version") not in {1, 2}:
            return False
        if receipt.get("version") == 2 and receipt.get("authorization", {}).get(record.path) != review_authorization_fingerprint(snapshot, record):
            return False
        if receipt.get("model_executed") is not True or receipt.get("reviewer") != baseline.get("verifier"):
            return False
        return any(item.get("path") == record.path and item.get("sha256") == baseline.get("new_hash")
                   and item.get("dependency_fingerprint") == baseline.get("dependency_fingerprint")
                   and item.get("verdict") == "trusted" for item in receipt.get("documents", []))
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def analyze_trust(
    snapshot: RepositorySnapshot,
    ledger_path: Path,
    requested_paths: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
) -> GovernanceDecision:
    """Return a fail-closed, read-only trust decision for tracked Markdown."""
    requested = list(requested_paths or [])
    explicit_request = bool(requested)
    controls = set(snapshot.catalog.policies.get("control_documents", ["AGENTS.md"]))
    available = set(snapshot.files)
    paths = requested or sorted(available)
    trust_results: List[TrustResult] = []
    findings: List[Finding] = []
    ledger = Ledger(ledger_path)
    ledger_entries = (
        snapshot.source_ledger_entries
        if snapshot.source_ledger_entries is not None
        else ledger.entries()
    )
    require_ledger = bool(snapshot.catalog.policies.get("require_verification_ledger", True))

    configured_functions = snapshot.supabase.get("config_functions", [])
    source_functions = snapshot.supabase.get("source_functions", [])
    if not snapshot.source_ref and (configured_functions or source_functions) and configured_functions != source_functions:
        findings.append(Finding(
            "conflict", "high", "block", [],
            "Supabase Edge Function config and source inventories differ.",
            evidence_for_path(snapshot, str(snapshot.supabase.get("config_path") or "supabase/config.toml")),
        ))

    for path in paths:
        normalized = path.replace("\\", "/")
        if snapshot.catalog.ignored(normalized):
            continue
        if normalized not in available:
            reason = "The Markdown file is not tracked at the selected source ref."
            trust_results.append(TrustResult(normalized, "unknown", "missing", "untrusted", reason))
            findings.append(Finding("orphan", "high", "block", [normalized], reason))
            continue
        record = snapshot.catalog.record_for(normalized)
        if record is None:
            reason = "The tracked Markdown file has no explicit Catalog record."
            trust_results.append(TrustResult(normalized, "unknown", "unregistered", "untrusted", reason))
            findings.append(Finding("misclassified", "high", "block", [normalized], reason))
            continue

        scope, reason = _status_scope(record)
        dependencies = dependency_evidence(snapshot, record)
        summarized = dependency_summary(record, dependencies)
        if normalized in controls:
            trust_results.append(TrustResult(
                normalized, record.type, record.status, "current_fact",
                "Governance control document is readable so the trust gate can bootstrap.", summarized,
            ))
            continue
        if scope == "untrusted":
            trust_results.append(TrustResult(normalized, record.type, record.status, scope, reason, summarized))
            if explicit_request:
                findings.append(Finding("stale", "high", "block", [normalized], reason, summarized))
            continue
        if record.type in {"state", "procedure"} and expired(record, snapshot.catalog, now=now):
            reason = f"The {record.type} document has passed its {snapshot.catalog.ttl_for(record)}-day verification window."
            trust_results.append(TrustResult(normalized, record.type, record.status, "untrusted", reason, summarized))
            findings.append(Finding("stale", "high", "block", [normalized], reason, summarized))
            continue

        matching_baselines = [
            entry
            for entry in ledger_entries
            if entry.get("document") == normalized and entry.get("action") == "verify_current"
        ]
        baseline = matching_baselines[-1] if matching_baselines else None
        if require_ledger and baseline is None:
            reason = "No successful verification baseline exists in the append-only ledger."
            trust_results.append(TrustResult(normalized, record.type, record.status, "untrusted", reason, summarized))
            findings.append(Finding("stale", "high", "block", [normalized], reason, summarized))
            continue
        if baseline is not None:
            if str(baseline.get("verifier", "")).startswith("codex:") and not has_matching_coding_review(snapshot, record):
                reason = "Coding-agent review is missing, revoked, or no longer matches the document and sources."
                trust_results.append(TrustResult(normalized, record.type, record.status, "untrusted", reason, summarized))
                findings.append(Finding("stale", "high", "block", [normalized], reason, summarized))
                continue
            current_hash = sha256_text(snapshot.files[normalized])
            current_fingerprint = dependency_fingerprint(dependencies)
            if baseline.get("new_hash") != current_hash:
                reason = "Document content changed after its latest successful verification."
                trust_results.append(TrustResult(normalized, record.type, record.status, "untrusted", reason, summarized))
                findings.append(Finding("unverified_date", "high", "block", [normalized], reason, summarized))
                continue
            if baseline.get("dependency_fingerprint") != current_fingerprint:
                reason = "A declared dependency changed after the document was verified."
                trust_results.append(TrustResult(normalized, record.type, record.status, "untrusted", reason, summarized))
                findings.append(Finding("stale", "high", "block", [normalized], reason, summarized))
                continue
        trust_results.append(TrustResult(normalized, record.type, record.status, scope, reason, summarized))

    conflicts = snapshot.catalog.duplicate_canonical_records()
    for key, records in conflicts.items():
        finding = Finding(
            "conflict", "high", "block", [record.path for record in records],
            f"The Catalog declares multiple canonical documents for {key}.",
        )
        findings.append(finding)

    result = "action_required" if findings else "pass"
    return GovernanceDecision(
        run_id=f"{snapshot.head_sha}-verify-strict",
        mode="verify",
        result=result,
        changed=False,
        findings=findings,
        trust_results=trust_results,
        head_sha=snapshot.head_sha,
    )


def record_baseline(
    snapshot: RepositorySnapshot,
    ledger_path: Path,
    *,
    approved: bool,
    documents: Optional[Iterable[str]] = None,
) -> GovernanceDecision:
    """Record a one-time maintainer-approved baseline without changing documents or dates."""
    if not approved:
        return GovernanceDecision(
            run_id=f"{snapshot.head_sha}-baseline",
            mode="baseline",
            result="action_required",
            changed=False,
            findings=[Finding(
                "conflict", "high", "block", [],
                "Baseline recording requires explicit maintainer approval.",
            )],
            head_sha=snapshot.head_sha,
        )
    wanted = set(documents or [record.path for record in snapshot.catalog.documents])
    ledger = Ledger(ledger_path)
    changed = False
    modified: List[str] = []
    findings: List[Finding] = []
    events: List[Tuple[DocumentRecord, Dict[str, object]]] = []
    for record in snapshot.catalog.documents:
        if record.path not in wanted:
            continue
        if record.path not in snapshot.files:
            findings.append(Finding(
                "orphan", "high", "block", [record.path],
                "Catalog record cannot be baselined because the document is missing.",
            ))
            continue
        scope, _ = _status_scope(record)
        if scope == "untrusted":
            continue
        event = verification_record(snapshot, record)
        if record.type == "state" and not event["dependency_count"]:
            findings.append(Finding(
                "unverified_date", "high", "block", [record.path],
                "A State baseline requires read-only source or evidence dependencies.",
            ))
            continue
        events.append((record, event))
    if findings:
        return GovernanceDecision(
            run_id=f"{snapshot.head_sha}-baseline",
            mode="baseline",
            result="action_required",
            changed=False,
            findings=findings,
            head_sha=snapshot.head_sha,
        )
    for record, event in events:
        changed = ledger.append(
            run_id=f"{snapshot.head_sha}-baseline",
            document=record.path,
            action="verify_current",
            reason=str(event["reason"]),
            evidence=event["evidence"],  # type: ignore[arg-type]
            new_hash=event["new_hash"],  # type: ignore[arg-type]
            head_sha=snapshot.head_sha,
            dependency_fingerprint=str(event["dependency_fingerprint"]),
            verifier="maintainer_bootstrap",
        ) or changed
    if changed:
        modified.append(ledger_path.relative_to(snapshot.root).as_posix())
    result = "action_required" if findings else ("changed" if changed else "pass")
    return GovernanceDecision(
        run_id=f"{snapshot.head_sha}-baseline",
        mode="baseline",
        result=result,
        changed=changed,
        findings=findings,
        modified_paths=modified,
        head_sha=snapshot.head_sha,
    )


def analyze(snapshot: RepositorySnapshot, mode: str = "review", run_id: Optional[str] = None) -> GovernanceDecision:
    run_id = run_id or f"{snapshot.head_sha}-{mode}"
    findings: List[Finding] = []
    modified_paths: List[str] = []
    auto_repair_patterns = [
        str(item)
        for item in snapshot.catalog.policies.get("auto_repair_documents", [])
    ]

    configured_functions = snapshot.supabase.get("config_functions", [])
    source_functions = snapshot.supabase.get("source_functions", [])
    supabase_sync_paths: set[str] = set()
    if configured_functions or source_functions:
        if configured_functions != source_functions:
            findings.append(Finding(
                kind="conflict",
                risk="high",
                action="block",
                documents=[],
                reason="Supabase Edge Function config and source inventories differ; documentation cannot safely repair an undeclared deployment contract.",
                evidence=evidence_for_path(
                    snapshot,
                    str(snapshot.supabase.get("config_path") or "supabase/config.toml"),
                ),
                human_decision="Resolve the source/config mismatch before updating documentation.",
            ))
        else:
            inventory_keys = {
                "functions": "source_functions",
                "jwt_flags": "jwt_flags",
                "rpcs": "rpc_functions",
                "storage_buckets": "storage_buckets",
            }
            for document, marker in snapshot.supabase.get("document_markers", {}).items():
                if not isinstance(marker, dict) or marker.get("invalid"):
                    findings.append(Finding(
                        kind="conflict",
                        risk="high",
                        action="block",
                        documents=[str(document)],
                        reason="The Supabase inventory marker is not valid JSON.",
                    ))
                    continue
                desired = dict(marker)
                for marker_key, inventory_key in inventory_keys.items():
                    if marker_key in marker:
                        desired[marker_key] = snapshot.supabase.get(inventory_key, [])
                if desired != marker:
                    path = str(document)
                    record = snapshot.catalog.record_for(path)
                    if record is None:
                        continue
                    evidence = dependency_summary(record, dependency_evidence(snapshot, record))
                    findings.append(Finding(
                        kind="stale",
                        risk="low",
                        action="update",
                        documents=[path],
                        reason="The machine-readable Supabase inventory differs from aligned source and config.",
                        evidence=evidence,
                        proposed_patch=f"<!-- docgov:supabase-inventory {json.dumps(desired, ensure_ascii=False, sort_keys=True, separators=(',', ':'))} -->",
                    ))
                    supabase_sync_paths.add(path)

    for path in snapshot.changed:
        if path.endswith(".md") and path not in snapshot.files and path not in snapshot.removed_by_governor:
            findings.append(Finding(
                kind="orphan",
                risk="high",
                action="block",
                documents=[path],
                reason="The changed Markdown path is not present in the checkout.",
            ))
        if path not in snapshot.added and snapshot.catalog.classify(path) == "evidence":
            findings.append(Finding(
                kind="conflict",
                risk="high",
                action="block",
                documents=[path],
                reason="Evidence documents are immutable after creation and cannot be overwritten by a pull request.",
                human_decision="Create a new dated evidence file and leave the existing evidence unchanged.",
            ))
        if (path not in snapshot.added and snapshot.catalog.is_protected(path)
                and not (snapshot.catalog.record_for(path)
                         and has_matching_coding_review(snapshot, snapshot.catalog.record_for(path)))):
            findings.append(Finding(
                kind="conflict",
                risk="high",
                action="block",
                documents=[path],
                reason="Protected legal, public, business, or production-status content was modified by this change.",
                human_decision="Have a maintainer review the protected change and provide an explicit replacement or decision.",
            ))

    for path in snapshot.added:
        if not path.endswith(".md") or path not in snapshot.files:
            continue
        document_type = snapshot.catalog.classify(path)
        if document_type is None:
            findings.append(Finding(
                kind="misclassified",
                risk="high",
                action="block",
                documents=[path],
                reason="The new Markdown file does not map to one of the configured core document types.",
            ))
            continue
        best_path: Optional[str] = None
        best_score = 0.0
        for candidate, content in snapshot.files.items():
            if candidate == path or candidate in snapshot.added or not candidate.endswith(".md"):
                continue
            if snapshot.catalog.classify(candidate) != document_type:
                continue
            score = similarity(snapshot.files[path], content)
            if content.strip() and content.strip() in snapshot.files[path]:
                score = max(score, 0.95)
            if score > best_score:
                best_path, best_score = candidate, score
        if best_path and best_score >= 0.90:
            protected = snapshot.catalog.is_protected(path) or snapshot.catalog.is_protected(best_path)
            immutable = document_type in {"evidence", "decision"}
            action = "block" if protected or immutable else "merge_new_file"
            findings.append(Finding(
                kind="duplicate",
                risk="high" if protected or immutable else "low",
                action=action,
                documents=[path, best_path],
                reason=f"The new file is {best_score:.0%} semantically equivalent to the existing canonical document.",
                evidence=evidence_for_path(snapshot, best_path),
                human_decision=(
                    "Choose a canonical document before changing protected content."
                    if protected
                    else "Keep evidence and decision records immutable; create a new record if the information is still needed."
                    if immutable
                    else None
                ),
            ))

    if mode in {"review", "audit"}:
        for record in impacted_records(snapshot):
            if (
                record.type in {"contract", "state", "procedure"}
                and record.status == "current"
                and record.path not in snapshot.changed
                and record.path not in supabase_sync_paths
                and not has_matching_verification_baseline(snapshot, record)
            ):
                auto_repair = any(
                    matches_repo_glob(record.path, pattern)
                    for pattern in auto_repair_patterns
                )
                findings.append(Finding(
                    kind="repair_required" if auto_repair else "stale",
                    risk="high" if auto_repair else "low",
                    action="block" if auto_repair else "mark_stale",
                    documents=[record.path],
                    reason=(
                        "A required document must be repaired by the configured coding agent before commit."
                        if auto_repair
                        else "A declared dependency changed after this document was verified."
                    ),
                    evidence=changed_dependency_evidence(snapshot, record),
                ))

    for record in snapshot.catalog.documents:
        if mode == "audit" and record.type in {"state", "procedure"} and expired(record, snapshot.catalog):
            findings.append(Finding(
                kind="stale",
                risk="low",
                action="mark_stale",
                documents=[record.path],
                reason=f"The {record.type} document has passed its {snapshot.catalog.ttl_for(record)}-day verification window.",
            ))

    if DATE_PATTERN.search(snapshot.diff) and not any(
        snapshot.catalog.classify(path) == "evidence" or path == snapshot.ledger_path
        for path in snapshot.changed
    ):
        removable_duplicates = {
            finding.documents[0]
            for finding in findings
            if finding.kind == "duplicate"
            and finding.risk == "low"
            and finding.action == "merge_new_file"
            and finding.documents
        }
        changed_dates = [
            path for path in snapshot.changed
            if path not in removable_duplicates
            if (path.endswith(".md") or path == snapshot.catalog_path)
            and DATE_PATTERN.search(
                snapshot.files.get(
                    path,
                    (snapshot.root / path).read_text(encoding="utf-8")
                    if (snapshot.root / path).exists()
                    else "",
                )
            )
        ]
        if changed_dates:
            findings.append(Finding(
                kind="unverified_date",
                risk="high",
                action="block",
                documents=changed_dates,
                reason="A verification date changed without a new evidence or ledger input in this change.",
                human_decision="Provide evidence or leave the verification date unchanged.",
            ))

    conflicts = snapshot.catalog.duplicate_canonical_records()
    for document_type, records in conflicts.items():
        findings.append(Finding(
            kind="conflict",
            risk="high",
            action="block",
            documents=[record.path for record in records],
            reason=f"The catalog declares multiple canonical {document_type} documents.",
            human_decision="Select one canonical document and mark the others supporting or superseded.",
        ))

    result = "action_required" if any(item.risk == "high" for item in findings) else ("changed" if findings else "pass")
    return GovernanceDecision(
        run_id=run_id,
        mode=mode,
        result=result,
        changed=False,
        findings=findings,
        modified_paths=modified_paths,
        head_sha=snapshot.head_sha,
    )


def apply_safe_actions(
    snapshot: RepositorySnapshot,
    decision: GovernanceDecision,
    catalog_path: Path,
    ledger_path: Path,
    *,
    approved: bool = False,
) -> GovernanceDecision:
    """Apply deterministic corrections within the governed write boundary.

    A maintainer approval can authorize one exact new-file duplicate, including
    one under a protected path. It never supplies missing evidence or resolves
    conflicting canonical documents, so those findings remain blocked.
    """
    if decision.high_risk_findings:
        if approved:
            for finding in decision.high_risk_findings:
                if finding.kind == "duplicate" and finding.action == "block":
                    finding.risk = "low"
                    finding.action = "merge_new_file"
                    finding.human_decision = None
                elif (
                    finding.kind == "conflict"
                    and finding.action == "block"
                    and finding.reason.startswith("Protected legal, public, business, or production-status")
                ):
                    # Human approval acknowledges an already-reviewed protected
                    # edit; the Governor still performs no content rewrite.
                    finding.risk = "low"
                    finding.action = "noop"
                    finding.human_decision = None
    ledger = Ledger(ledger_path)
    modified_paths: List[str] = []
    removed_paths: set[str] = set()
    pending_ledger: List[Dict[str, object]] = []
    planned_writes: Dict[str, str] = {}
    verified_paths: set[str] = set()
    catalog_before = json.dumps(snapshot.catalog.to_dict(), sort_keys=True)
    for finding in decision.findings:
        if finding.action == "merge_new_file" and len(finding.documents) == 2:
            new_path, canonical_path = finding.documents
            if not snapshot.catalog.policies.get("auto_remove_new_duplicates", True):
                continue
            new_file = repository_path(snapshot.root, new_path)
            canonical_file = repository_path(snapshot.root, canonical_path)
            if (
                new_file is None
                or canonical_file is None
                or new_path not in snapshot.added
                or canonical_path in snapshot.added
                or not new_path.endswith(".md")
                or not canonical_path.endswith(".md")
            ):
                finding.risk = "high"
                finding.action = "block"
                finding.human_decision = "The proposed duplicate removal crossed the governed new-Markdown boundary."
                decision.result = "action_required"
                return decision
            if (snapshot.catalog.is_protected(new_path) or snapshot.catalog.is_protected(canonical_path)) and not approved:
                finding.risk = "high"
                finding.action = "block"
                finding.human_decision = "Protected duplicate removal requires approval for the current head SHA."
                decision.result = "action_required"
                return decision
            if not new_file.exists() or not canonical_file.exists():
                continue
            new_content = new_file.read_text(encoding="utf-8")
            canonical_content = canonical_file.read_text(encoding="utf-8")
            exact_duplicate = normalize_markdown(new_content) == normalize_markdown(canonical_content)
            additive_duplicate = bool(canonical_content.strip()) and canonical_content.strip() in new_content
            if not exact_duplicate and not additive_duplicate:
                finding.risk = "high"
                finding.action = "block"
                finding.human_decision = "Review the unique sections and choose whether to merge them manually."
                decision.result = "action_required"
                return decision
            incoming = [
                path
                for path, content in snapshot.files.items()
                if path != new_path and links_to_document(path, planned_writes.get(path, content), new_path)
            ]
            for incoming_path in incoming:
                incoming_record = snapshot.catalog.record_for(incoming_path)
                if (
                    incoming_path not in snapshot.changed
                    or snapshot.catalog.is_protected(incoming_path)
                    or (incoming_record and incoming_record.type in {"evidence", "decision"})
                ):
                    finding.risk = "high"
                    finding.action = "block"
                    finding.human_decision = "An inbound link is outside the safely changed Markdown boundary."
                    decision.result = "action_required"
                    return decision
                previous_content = planned_writes.get(incoming_path, snapshot.files[incoming_path])
                rewritten = rewrite_document_links(incoming_path, previous_content, new_path, canonical_path)
                if rewritten == previous_content:
                    finding.risk = "high"
                    finding.action = "block"
                    finding.human_decision = "The inbound link could not be rewritten deterministically."
                    decision.result = "action_required"
                    return decision
                planned_writes[incoming_path] = rewritten
                pending_ledger.append({
                    "document": incoming_path,
                    "action": "update_link",
                    "reason": f"Repointed the changed Markdown link from {new_path} to {canonical_path}.",
                    "evidence": [Evidence(path=canonical_path, sha256=sha256_text(new_content if additive_duplicate else canonical_content))],
                    "previous_hash": sha256_text(previous_content),
                    "new_hash": sha256_text(rewritten),
                })
            if additive_duplicate and new_content != canonical_content:
                planned_writes[canonical_path] = new_content
                pending_ledger.append({
                    "document": canonical_path,
                    "action": "update",
                    "reason": f"Preserved the additive content from new duplicate {new_path} in the canonical document.",
                    "evidence": [Evidence(path=new_path, sha256=sha256_text(new_content))],
                    "previous_hash": sha256_text(canonical_content),
                    "new_hash": sha256_text(new_content),
                })
            modified_paths.append(new_path)
            removed_paths.add(new_path)
            duplicate_record = snapshot.catalog.record_for(new_path)
            if duplicate_record is not None:
                snapshot.catalog.documents.remove(duplicate_record)
            pending_ledger.append({
                "document": new_path,
                "action": "remove_new_duplicate",
                "reason": f"Merged into {canonical_path}; unique content and changed-file links were preserved before removing the new file.",
                "evidence": [Evidence(path=canonical_path, sha256=sha256_text(new_content if additive_duplicate else canonical_content))],
                "previous_hash": sha256_text(snapshot.files[new_path]),
                "new_hash": None,
            })
        elif finding.action == "mark_stale":
            for document in finding.documents:
                record = snapshot.catalog.record_for(document)
                if record and record.status != "stale":
                    record.status = "stale"
                    pending_ledger.append({
                        "document": document,
                        "action": "mark_stale",
                        "reason": finding.reason,
                        "evidence": finding.evidence,
                    })
        elif finding.action == "update" and len(finding.documents) == 1 and finding.proposed_patch:
            document = finding.documents[0]
            record = snapshot.catalog.record_for(document)
            destination = repository_path(snapshot.root, document)
            if (
                destination is None
                or not document.endswith(".md")
                or not destination.exists()
                or record is None
                or record.type in {"evidence", "decision"}
                or snapshot.catalog.is_protected(document)
                or not finding.proposed_patch.startswith("<!-- docgov:supabase-inventory ")
            ):
                finding.risk = "high"
                finding.action = "block"
                finding.human_decision = "The proposed update crossed the governed generated-inventory boundary."
                decision.result = "action_required"
                return decision
            previous_content = planned_writes.get(document, destination.read_text(encoding="utf-8"))
            matches = list(SUPABASE_MARKER_PATTERN.finditer(previous_content))
            if len(matches) != 1:
                finding.risk = "high"
                finding.action = "block"
                finding.human_decision = "Exactly one machine-readable Supabase inventory marker is required."
                decision.result = "action_required"
                return decision
            updated_content = SUPABASE_MARKER_PATTERN.sub(finding.proposed_patch, previous_content, count=1)
            planned_writes[document] = updated_content
            record.status = "current"
            if record.type in {"state", "procedure"}:
                record.last_verified_at = utc_now()
            verified_paths.add(document)
            pending_ledger.append({
                "document": document,
                "action": "update",
                "reason": finding.reason,
                "evidence": finding.evidence,
                "previous_hash": sha256_text(previous_content),
                "new_hash": sha256_text(updated_content),
            })
        elif finding.action == "draft_contract_span" and len(finding.documents) == 1 and finding.proposed_patch:
            # A model proposed this prose. Deterministic code re-proves the whole
            # grounding argument here rather than trusting that it was proved
            # upstream: the authority to mutate a file lives in this function (P4).
            document = finding.documents[0]
            record = snapshot.catalog.record_for(document)
            destination = repository_path(snapshot.root, document)

            def _reject(reason: str) -> GovernanceDecision:
                finding.risk = "high"
                finding.action = "block"
                finding.proposed_patch = None
                finding.human_decision = reason
                decision.result = "action_required"
                return decision

            try:
                payload = json.loads(finding.proposed_patch)
                original_span = str(payload["original_span"])
                proposed_span = str(payload["proposed_span"])
                declared_tokens = [str(item) for item in payload.get("factual_tokens", [])]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return _reject("The proposed contract span was not a well-formed patch.")
            if (
                destination is None
                or record is None
                or not document.endswith(".md")
                or not destination.exists()
            ):
                return _reject("The proposed contract span left the governed Markdown boundary.")
            cited = sorted({item.path for item in finding.evidence if item.kind == "cited_source"})
            cited_sources: Dict[str, str] = {}
            for source_path in cited:
                # A `depends_on` glob such as `**/*.py` matches `../../secret.py`,
                # so the boundary has to be enforced on the resolved path, not on
                # the glob. Anything outside the repository is simply not read,
                # which leaves its tokens ungrounded and discards the draft.
                located = repository_path(snapshot.root, source_path)
                if located is None or not located.exists() or not located.is_file():
                    return _reject(
                        "The proposed contract span cited a source outside the repository boundary."
                    )
                content = _content_for_path(snapshot, source_path)
                if content is not None:
                    cited_sources[source_path] = content
            previous_content = planned_writes.get(
                document, destination.read_text(encoding="utf-8")
            )
            validation = validate_draft(
                document_path=document,
                document_type=record.type,
                declared_dependencies=record.depends_on,
                current_text=previous_content,
                original_span=original_span,
                proposed_span=proposed_span,
                cited_sources=cited_sources,
                declared_tokens=declared_tokens,
                protected=snapshot.catalog.is_protected(document),
                approval=record.approval,
            )
            if not validation.ok:
                return _reject(f"The proposed contract span failed grounding validation: {validation.reason}")
            updated_content = apply_span(previous_content, original_span, proposed_span)
            if updated_content is None or updated_content == previous_content:
                return _reject("The proposed contract span no longer matches the document exactly once.")
            planned_writes[document] = updated_content
            # The correction is applied, but no verification baseline is recorded:
            # rewriting prose does not prove the document as a whole, and the
            # changed content hash correctly leaves it untrusted until a
            # maintainer re-verifies it.
            pending_ledger.append({
                "document": document,
                "action": "draft_contract_span",
                "reason": finding.reason,
                "evidence": finding.evidence,
                "previous_hash": sha256_text(previous_content),
                "new_hash": sha256_text(updated_content),
            })
    for path in snapshot.added:
        if not path.endswith(".md") or path in removed_paths or path not in snapshot.files:
            continue
        document_type = snapshot.catalog.classify(path)
        if document_type and snapshot.catalog.record_for(path) is None:
            snapshot.catalog.ensure_record(path, document_type)
            pending_ledger.append({
                "document": path,
                "action": "catalog_register",
                "reason": f"Registered new {document_type} document in the central Catalog.",
                "evidence": evidence_for_path(snapshot, path),
            })
    catalog_changed = json.dumps(snapshot.catalog.to_dict(), sort_keys=True) != catalog_before
    for path, content in planned_writes.items():
        destination = repository_path(snapshot.root, path)
        if destination is None or not path.endswith(".md"):
            raise RuntimeError(f"Planned write escaped the Markdown boundary: {path}")
        destination.write_text(content, encoding="utf-8")
        modified_paths.append(path)
    for path in removed_paths:
        destination = repository_path(snapshot.root, path)
        if destination is not None and destination.exists():
            destination.unlink()
    for document in sorted(verified_paths):
        record = snapshot.catalog.record_for(document)
        if record is None:
            continue
        dependencies = dependency_evidence(snapshot, record)
        pending_ledger.append({
            "document": document,
            "action": "verify_current",
            "reason": "Deterministic Supabase inventory matched source and config after synchronization.",
            "evidence": dependency_summary(record, dependencies),
            "new_hash": sha256_text(planned_writes[document]),
            "dependency_fingerprint": dependency_fingerprint(dependencies),
            "verifier": "supabase_inventory",
        })
    if catalog_changed:
        modified_paths.append(catalog_path.relative_to(snapshot.root).as_posix())
        catalog_previous_hash = sha256_file(catalog_path) if catalog_path.exists() else None
        snapshot.catalog.save(catalog_path)
        catalog_new_hash = sha256_file(catalog_path)
    else:
        catalog_previous_hash = None
        catalog_new_hash = None
    for event in pending_ledger:
        is_catalog_event = event["action"] in {"mark_stale", "catalog_register", "remove_new_duplicate"} and catalog_changed
        ledger.append(
            run_id=decision.run_id,
            document=str(event["document"]),
            action=str(event["action"]),
            reason=str(event["reason"]),
            evidence=event.get("evidence", []),  # type: ignore[arg-type]
            previous_hash=(catalog_previous_hash if is_catalog_event else event.get("previous_hash")),  # type: ignore[arg-type]
            new_hash=(catalog_new_hash if is_catalog_event else event.get("new_hash")),  # type: ignore[arg-type]
            head_sha=decision.head_sha,
            dependency_fingerprint=event.get("dependency_fingerprint"),  # type: ignore[arg-type]
            verifier=event.get("verifier"),  # type: ignore[arg-type]
        )
    if pending_ledger:
        modified_paths.append(ledger_path.relative_to(snapshot.root).as_posix())
    changed = bool(planned_writes or removed_paths or catalog_changed)
    decision.changed = changed
    decision.modified_paths = sorted(set(modified_paths))
    if decision.high_risk_findings:
        decision.result = "action_required"
    elif changed:
        decision.result = "changed"
    elif decision.result == "action_required" and not decision.high_risk_findings:
        decision.result = "pass"
    return decision
