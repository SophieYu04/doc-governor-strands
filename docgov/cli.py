from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from .agent import govern
from .agents import AgentContractError
from .catalog import Catalog
from .engine import analyze_trust, apply_safe_actions, build_snapshot, record_baseline
from .git_tools import current_sha, tracked_paths
from .ledger import utc_now
from .models import DocumentRecord, GovernanceDecision
from .repair import build_repair_prompt, repair_candidates
from .repair_agents import RepairPlanError
from .supabase_remote import (
    DEFAULT_EVIDENCE_DIR,
    PROMOTION_ACTION,
    EnvironmentFingerprint,
    SupabaseAdvisorError,
    collect_advisor_evidence,
    compare_environments,
    environment_fingerprint,
    parse_projects,
)
from .ledger import Ledger
from .trust_state import (
    DEFAULT_TRUST_STATE_PATH,
    DRIFT_ACTION,
    DRIFT_CLEARED_ACTION,
    ENVIRONMENT_DOCUMENT_PREFIX,
    build_trust_state,
    documents_for_environment,
    write_trust_state,
)


def _catalog_path(root: Path, value: str | None) -> Path:
    return root / (value or ".docgov/catalog.yaml")


def _ledger_path(root: Path, value: str | None) -> Path:
    return root / (value or ".docgov/ledger.jsonl")


def _trust_state_path(root: Path, value: str | None) -> Path:
    return root / (value or DEFAULT_TRUST_STATE_PATH)


def _refresh_trust_state(
    root: Path,
    catalog_path: Path,
    ledger_path: Path,
    trust_state_path: Path,
    decision: GovernanceDecision,
) -> GovernanceDecision:
    """Recompute and commit the trust table the MCP supply layer reads (D1).

    The snapshot is rebuilt from the working tree because safe corrections have
    already been written to disk; serializing the pre-correction view would ship
    a table that describes documents that no longer exist.
    """
    try:
        applied = build_snapshot(root, catalog_path, ledger_path=ledger_path)
        state = build_trust_state(decision, applied, ledger_path=ledger_path)
        changed = write_trust_state(trust_state_path, state)
    except Exception as exc:  # a trust table that cannot be trusted must not be written
        # Safe corrections may already be on disk at this point. Leaving the old
        # table in place is still fail-closed at the read path: a corrected
        # document no longer matches its recorded content hash, so the MCP layer
        # refuses it until a governor run regenerates the table.
        decision.result = "blocked"
        decision.error = (
            f"Trust state generation failed closed: {exc}. Any safe correction already written "
            "stays on disk, and the stale trust state keeps the MCP layer refusing it."
        )
        return decision
    if changed:
        decision.changed = True
        decision.modified_paths = sorted(
            set(decision.modified_paths) | {trust_state_path.relative_to(root).as_posix()}
        )
        if decision.result == "pass":
            decision.result = "changed"
    return decision


def _latest_evidence_fingerprints(
    root: Path,
    evidence_dir: str,
    environments: List[str],
) -> Dict[str, EnvironmentFingerprint]:
    """Read the newest committed Advisor snapshot per environment. No network."""
    directory = root / evidence_dir
    newest: Dict[str, Dict[str, Any]] = {}
    if directory.exists():
        for candidate in sorted(directory.rglob("*.json")):
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or value.get("kind") != "supabase_advisor_snapshot":
                continue
            environment = str(value.get("environment", ""))
            if environments and environment not in environments:
                continue
            previous = newest.get(environment)
            if previous is None or str(value.get("observed_at", "")) >= str(previous.get("observed_at", "")):
                newest[environment] = value
    return {
        environment: environment_fingerprint(snapshot)
        for environment, snapshot in newest.items()
    }


def _run_drift(args: argparse.Namespace, root: Path, catalog_path: Path, ledger_path: Path,
               trust_state_path: Path) -> GovernanceDecision:
    """Compare environments read-only and emit findings. Never deploys anything (D7)."""
    catalog = Catalog.load(catalog_path)
    environments = [item.strip() for item in str(args.environments or "").split(",") if item.strip()]
    if args.collect:
        if not args.supabase_projects:
            raise SupabaseAdvisorError("--collect requires --supabase-projects.")
        projects = parse_projects(args.supabase_projects)
        collect_advisor_evidence(
            root,
            projects,
            os.environ.get("SUPABASE_ACCESS_TOKEN", ""),
            output_dir=args.supabase_evidence_dir,
            api_base=args.supabase_api_base,
            timeout_seconds=args.supabase_timeout_seconds,
        )
        environments = environments or [environment for environment, _ in projects]
    elif not environments and args.supabase_projects:
        environments = [environment for environment, _ in parse_projects(args.supabase_projects)]
    fingerprints = _latest_evidence_fingerprints(root, args.supabase_evidence_dir, environments)
    ledger = Ledger(ledger_path)
    findings = compare_environments(
        fingerprints,
        ledger,
        production=args.production,
        reference=args.reference,
    )
    # Name the documents that describe the drifted environment: a document about
    # a production nobody can tie to a commit is not a document anyone should read.
    for finding in findings:
        finding.documents = documents_for_environment(catalog, args.production)

    run_id = f"{current_sha(root)}-drift"
    decision = GovernanceDecision(
        run_id=run_id,
        mode="drift",
        result="action_required" if findings else "pass",
        changed=False,
        findings=findings,
        head_sha=current_sha(root),
    )
    if not args.apply:
        return decision

    modified: List[str] = []
    document_key = f"{ENVIRONMENT_DOCUMENT_PREFIX}{args.production}"
    current = fingerprints.get(args.production)
    if args.record_promotion:
        promoted = fingerprints.get(args.record_promotion)
        if promoted is None:
            raise SupabaseAdvisorError(
                f"No Advisor evidence is recorded for {args.record_promotion!r}; run with --collect first."
            )
        ledger.append(
            run_id=run_id,
            document=f"{ENVIRONMENT_DOCUMENT_PREFIX}{args.record_promotion}",
            action=PROMOTION_ACTION,
            reason=f"Recorded the {args.record_promotion} Advisor state at release promotion.",
            head_sha=decision.head_sha,
            dependency_fingerprint=promoted.advisor_fingerprint,
            verifier="release_promotion",
        )
        modified.append(ledger_path.relative_to(root).as_posix())
    if current is not None:
        ledger.append(
            run_id=run_id,
            document=document_key,
            action=DRIFT_ACTION if findings else DRIFT_CLEARED_ACTION,
            reason=(
                findings[0].reason
                if findings
                else f"The {args.production} environment matches the state Git produced."
            ),
            head_sha=decision.head_sha,
            dependency_fingerprint=current.advisor_fingerprint,
            verifier="environment_drift",
        )
        modified.append(ledger_path.relative_to(root).as_posix())
    decision.modified_paths = sorted(set(modified))
    decision.changed = bool(modified)
    return _refresh_trust_state(root, catalog_path, ledger_path, trust_state_path, decision)


def _init_catalog(root: Path, path: Path) -> Dict[str, Any]:
    catalog = Catalog.default()
    markdown_paths = tracked_paths(root, suffix=".md")
    if not markdown_paths:
        markdown_paths = [path.relative_to(root).as_posix() for path in root.rglob("*.md")]
    for relative in sorted(set(markdown_paths)):
        parts = Path(relative).parts
        if any(part in {".git", ".venv", "node_modules", "vendor"} for part in parts):
            continue
        document_type = catalog.classify(relative) or _suggest_type(relative)
        catalog.documents.append(DocumentRecord(
            path=relative,
            type=document_type,
            status="review_required",
            approval="human",
        ))
    return catalog.to_dict()


def _suggest_type(relative_path: str) -> str:
    value = relative_path.lower()
    name = Path(value).name
    if "/evidence/" in f"/{value}" or "evidence" in name:
        return "evidence"
    if "/decisions/" in f"/{value}" or "/adr/" in f"/{value}" or name.startswith("adr-"):
        return "decision"
    if any(token in value for token in ("/status/", "current_release", "release_state")):
        return "state"
    if any(token in value for token in ("/operations/", "runbook", "playbook", "checklist", "testing", "setup")):
        return "procedure"
    return "contract"


def _print(decision: GovernanceDecision, as_json: bool) -> None:
    if as_json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True))
        return
    print(f"Doc Governor: {decision.result} ({len(decision.findings)} finding(s))")
    for finding in decision.findings:
        print(f"- [{finding.risk}] {finding.kind}: {finding.reason}")
        if finding.documents:
            print(f"  documents: {', '.join(finding.documents)}")


def _exit_code(decision: GovernanceDecision) -> int:
    return 2 if decision.result in {"action_required", "blocked"} else 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docgov")
    parser.add_argument("--root", default=".")
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument(
        "--trust-state",
        default=None,
        help=f"Trust state path relative to root (default {DEFAULT_TRUST_STATE_PATH})",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--apply", action="store_true")
    init_parser.add_argument("--json", action="store_true", dest="sub_json")

    for command in ("review", "audit", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--base", default=None)
        command_parser.add_argument("--head", default=None)
        command_parser.add_argument("--apply", action="store_true")
        command_parser.add_argument("--json", action="store_true", dest="sub_json")
        command_parser.add_argument(
            "--approved",
            action="store_true",
            help="Apply the narrow action authorized by the docgov-approved label",
        )
        command_parser.add_argument(
            "--enable-model",
            action="store_true",
            default=os.environ.get("DOCGOV_ENABLE_MODEL", "").lower() in {"1", "true", "yes"},
        )
        command_parser.add_argument("--model-id", default=os.environ.get("DOCGOV_MODEL_ID"))

    audit_parser = subparsers.choices["audit"]
    audit_parser.add_argument(
        "--supabase-projects",
        default=None,
        help="Comma or newline separated environment=project_ref pairs for read-only Advisor evidence",
    )
    audit_parser.add_argument(
        "--supabase-evidence-dir",
        default=DEFAULT_EVIDENCE_DIR,
        help="Repository-relative immutable evidence destination",
    )
    audit_parser.add_argument(
        "--supabase-api-base",
        default=os.environ.get("DOCGOV_SUPABASE_API_BASE", "https://api.supabase.com"),
        help=argparse.SUPPRESS,
    )
    audit_parser.add_argument(
        "--supabase-timeout-seconds",
        type=float,
        default=20,
        help="Timeout for each read-only Supabase Advisor request",
    )

    verify_parser = subparsers.choices["verify"]
    verify_parser.add_argument("--strict", action="store_true")
    verify_parser.add_argument("--ref", default=None, help="Read governed content from this Git ref")
    verify_parser.add_argument("paths", nargs="*", help="Tracked Markdown paths to verify")

    drift_parser = subparsers.add_parser(
        "drift",
        help="Detect read-only cross-environment drift between staging and production",
    )
    drift_parser.add_argument(
        "--supabase-projects",
        default=None,
        help="Comma or newline separated environment=project_ref pairs",
    )
    drift_parser.add_argument(
        "--collect",
        action="store_true",
        help="Fetch fresh read-only Advisor evidence first (needs SUPABASE_ACCESS_TOKEN, read scope only)",
    )
    drift_parser.add_argument("--supabase-evidence-dir", default=DEFAULT_EVIDENCE_DIR)
    drift_parser.add_argument(
        "--supabase-api-base",
        default=os.environ.get("DOCGOV_SUPABASE_API_BASE", "https://api.supabase.com"),
        help=argparse.SUPPRESS,
    )
    drift_parser.add_argument("--supabase-timeout-seconds", type=float, default=20)
    drift_parser.add_argument(
        "--environments",
        default=None,
        help="Comma separated environment names to compare when not collecting",
    )
    drift_parser.add_argument("--production", default="production")
    drift_parser.add_argument("--reference", default="staging")
    drift_parser.add_argument(
        "--apply",
        action="store_true",
        help="Record the drift verdict in the ledger and refresh the trust state",
    )
    drift_parser.add_argument(
        "--record-promotion",
        default=None,
        metavar="ENVIRONMENT",
        help="Record the current Advisor state as the baseline a release promoted to this environment",
    )
    drift_parser.add_argument("--json", action="store_true", dest="sub_json")

    baseline_parser = subparsers.add_parser("baseline")
    baseline_parser.add_argument("--approved", action="store_true", required=True)
    baseline_parser.add_argument("--ref", default=None)
    baseline_parser.add_argument("--json", action="store_true", dest="sub_json")
    baseline_parser.add_argument("paths", nargs="*")

    repair_parser = subparsers.add_parser(
        "repair-prompt",
        help="Use Strands to plan repairs and emit a prompt for the configured coding agent",
    )
    repair_parser.add_argument("--json", action="store_true", dest="sub_json")
    repair_parser.add_argument(
        "--enable-model",
        action="store_true",
        default=os.environ.get("DOCGOV_ENABLE_MODEL", "").lower() in {"1", "true", "yes"},
    )
    repair_parser.add_argument("--model-id", default=os.environ.get("DOCGOV_MODEL_ID"))

    args = parser.parse_args(argv)
    args.as_json = bool(args.as_json or getattr(args, "sub_json", False))
    root = Path(args.root).resolve()
    catalog_path = _catalog_path(root, args.catalog)
    ledger_path = _ledger_path(root, args.ledger)
    trust_state_path = _trust_state_path(root, args.trust_state)

    if args.command == "drift":
        try:
            decision = _run_drift(args, root, catalog_path, ledger_path, trust_state_path)
        except (SupabaseAdvisorError, OSError, ValueError) as exc:
            decision = GovernanceDecision(
                run_id="unknown-drift",
                mode="drift",
                result="blocked",
                changed=False,
                error=f"Cross-environment drift detection failed closed: {exc}",
            )
        _print(decision, args.as_json)
        return _exit_code(decision)

    if args.command == "init":
        value = _init_catalog(root, catalog_path)
        changed = False
        if args.apply:
            # Initialization is safe to run repeatedly: never overwrite a
            # reviewed Catalog that already exists.
            if not catalog_path.exists():
                catalog_path.parent.mkdir(parents=True, exist_ok=True)
                Catalog(value).save(catalog_path)
                changed = True
        print(json.dumps({
            "result": "changed" if changed else "pass",
            "changed": changed,
            "finding_count": 0,
            "proposal_only": not args.apply,
            "catalog": value,
        }, ensure_ascii=False, sort_keys=True))
        return 0

    mode = "review" if args.command == "review" else "audit"
    if args.command == "verify":
        mode = "review"
    remote_evidence_paths: List[str] = []
    if args.command == "audit" and args.supabase_projects:
        if not args.apply:
            decision = GovernanceDecision(
                run_id="unknown-audit",
                mode="audit",
                result="blocked",
                changed=False,
                error="Read-only Supabase Advisor collection requires --apply to persist immutable evidence.",
            )
            _print(decision, args.as_json)
            return _exit_code(decision)
        try:
            # Validate the governance controls before making any evidence write.
            Catalog.load(catalog_path)
            projects = parse_projects(args.supabase_projects)
            writes = collect_advisor_evidence(
                root,
                projects,
                os.environ.get("SUPABASE_ACCESS_TOKEN", ""),
                output_dir=args.supabase_evidence_dir,
                api_base=args.supabase_api_base,
                timeout_seconds=args.supabase_timeout_seconds,
            )
            remote_evidence_paths = [
                item.path for item in writes if item.changed and item.path is not None
            ]
        except (SupabaseAdvisorError, OSError, ValueError) as exc:
            decision = GovernanceDecision(
                run_id="unknown-audit",
                mode="audit",
                result="blocked",
                changed=False,
                error=f"Supabase Advisor evidence collection failed closed: {exc}",
            )
            _print(decision, args.as_json)
            return _exit_code(decision)
    try:
        snapshot = build_snapshot(
            root,
            catalog_path,
            base=getattr(args, "base", None),
            head=getattr(args, "head", None),
            ledger_path=ledger_path,
            source_ref=getattr(args, "ref", None),
        )
    except Exception as exc:
        decision = GovernanceDecision(
            run_id=f"unknown-{mode}",
            mode=mode,
            result="blocked",
            changed=False,
            error=f"Unable to load governance inputs: {exc}",
        )
        _print(decision, args.as_json)
        return _exit_code(decision)
    if args.command == "repair-prompt":
        try:
            prompt = build_repair_prompt(
                snapshot,
                enable_model=args.enable_model,
                model_id=args.model_id,
            )
        except (RepairPlanError, AgentContractError, OSError, RuntimeError) as exc:
            if args.as_json:
                print(json.dumps({
                    "documents": [record.path for record in repair_candidates(snapshot)],
                    "error": f"Strands repair planning failed closed: {exc}",
                    "model_used": bool(args.enable_model),
                    "required": True,
                    "result": "blocked",
                }, ensure_ascii=False))
            else:
                print(f"Strands repair planning failed closed: {exc}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps({
                "documents": [record.path for record in repair_candidates(snapshot)],
                "model_used": bool(args.enable_model and prompt),
                "planner": "strands" if args.enable_model and prompt else "deterministic",
                "prompt": prompt,
                "required": bool(prompt),
                "result": "pass",
            }, ensure_ascii=False))
        else:
            print(prompt, end="")
        return 0
    if args.command == "baseline":
        decision = record_baseline(
            snapshot,
            ledger_path,
            approved=args.approved,
            documents=args.paths or None,
        )
        if decision.changed:
            decision = _refresh_trust_state(
                root, catalog_path, ledger_path, trust_state_path, decision
            )
        _print(decision, args.as_json)
        return _exit_code(decision)

    if args.command == "verify" and args.strict:
        decision = analyze_trust(
            snapshot,
            ledger_path,
            requested_paths=args.paths or None,
        )
        _print(decision, args.as_json)
        return _exit_code(decision)

    try:
        decision = govern(
            snapshot,
            mode=mode,
            run_id=f"{snapshot.head_sha}-{mode}",
            enable_model=args.enable_model and args.command != "verify",
            model_id=args.model_id,
        )
    except Exception as exc:  # model failures must never mutate a repository
        decision = GovernanceDecision(
            run_id=f"{snapshot.head_sha}-{mode}",
            mode=mode,
            result="blocked",
            changed=False,
            head_sha=snapshot.head_sha,
            error=str(exc),
        )
    if args.apply and args.command != "verify" and decision.result != "blocked":
        decision = apply_safe_actions(
            snapshot,
            decision,
            catalog_path,
            ledger_path,
            approved=args.approved,
        )
        decision = _refresh_trust_state(
            root, catalog_path, ledger_path, trust_state_path, decision
        )
    if remote_evidence_paths:
        decision.changed = True
        decision.modified_paths = sorted(set(decision.modified_paths) | set(remote_evidence_paths))
        if decision.result == "pass":
            decision.result = "changed"
    _print(decision, args.as_json)
    return _exit_code(decision)


if __name__ == "__main__":
    raise SystemExit(main())
