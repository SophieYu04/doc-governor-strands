"""The supply layer: an MCP server that will not hand an agent a document it cannot vouch for.

This is the enforcement point (P1). A pull-request gate protects only future
commits; documents already merged into ``main`` are already poisoned. So the
decision about whether content may be believed is made at the moment the
consuming agent asks for it.

The read path is deterministic and cheap by construction (D1):

1. look the path up in the precomputed ``.docgov/trust.json`` table,
2. recompute the SHA-256 fingerprint of the document's declared dependencies
   against the *current* working tree,
3. serve the file only if the table says usable and the fingerprint still matches.

Step 2 is what makes the server correct *between* governor runs. A developer
commits code locally and ``trust.json`` is instantly older than HEAD; without the
recheck the server would serve stale content that is marked fresh. The recheck is
pure hashing — no model, no network (P6).

The server is read-only: it exposes three tools, none of which writes, executes,
or reaches the network. It fails closed everywhere — a missing trust table, an
unknown schema version, an unreadable file and an unparseable catalog all refuse
every read rather than serving everything.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .catalog import Catalog
from .engine import (
    RepositorySnapshot,
    dependency_candidates,
    dependency_evidence,
    dependency_fingerprint,
    has_matching_coding_review,
)
from .ledger import sha256_text
from .models import DocumentRecord
from .trust_state import (
    DEFAULT_TRUST_STATE_PATH,
    TrustEntry,
    TrustStateError,
    load_trust_state,
    trust_entries,
)


SERVER_NAME = "docgov"
SERVER_INSTRUCTIONS = (
    "Doc Governor decides which documents you are allowed to believe. "
    "Call get_document instead of reading Markdown from disk: a refusal means the "
    "document's claims are no longer backed by the code it describes, and the "
    "refusal names the source files to read instead. Do not fall back to the raw "
    "file — that is the failure this server exists to prevent."
)

#: Codes a consuming agent can branch on without parsing prose.
CODE_OK = "ok"
CODE_PATH_REJECTED = "path_rejected"
CODE_NOT_GOVERNED = "not_governed"
CODE_MISSING_FILE = "missing_file"
CODE_DEPENDENCIES_CHANGED = "dependencies_changed"
CODE_CONTENT_CHANGED = "content_changed"
CODE_NOT_USABLE = "not_usable"
CODE_STATE_UNAVAILABLE = "state_unavailable"

RESOLVE_REVERIFY = (
    "Read the source locations in read_instead. An installed background worker "
    "maintains documentation and verification; this read endpoint never repairs files."
)


class PathRejected(ValueError):
    """Raised when a requested path is outside the governed read boundary."""


def resolve_governed_path(root: Path, requested: str) -> Tuple[str, Path]:
    """Return the repository-relative path and absolute file for a governed read.

    Everything about this function is a refusal: absolute paths, parent traversal,
    NUL bytes, non-Markdown suffixes, and symlinks that resolve outside the
    repository root are all rejected before any filesystem read happens.
    """
    if not isinstance(requested, str) or not requested.strip():
        raise PathRejected("A document path is required.")
    if "\x00" in requested:
        raise PathRejected("The document path contains a NUL byte.")
    normalized = requested.replace("\\", "/").strip()
    if normalized.startswith("/") or normalized.startswith("~"):
        raise PathRejected("Absolute paths are outside the governed repository.")
    # Windows drive letters ("C:/x") and any URL scheme ("file://x").
    if ":" in normalized.split("/", 1)[0]:
        raise PathRejected("Only repository-relative paths are served.")
    segments = [segment for segment in normalized.split("/") if segment not in {"", "."}]
    if any(segment == ".." for segment in segments):
        raise PathRejected("Parent-directory traversal is not permitted.")
    if not segments:
        raise PathRejected("A document path is required.")
    relative = "/".join(segments)
    if not relative.endswith(".md"):
        raise PathRejected("Only governed Markdown documents are served.")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    # `.resolve()` follows symlinks, so this also rejects a symlink inside the
    # repository that points anywhere outside it.
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise PathRejected("The resolved path escapes the repository root.")
    return relative, candidate


def _summary(content: str) -> str:
    """Return a one-line description built from a document that is safe to serve.

    Only ever called for a usable document. A refused document leaks nothing —
    not even its first line.
    """
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:160]
    return ""


@dataclass(frozen=True)
class SupplyConfig:
    root: Path
    trust_state_path: Path
    catalog_path: Path


class DocumentSupply:
    """The read path, with no MCP types in it so it can be tested on its own."""

    def __init__(self, config: SupplyConfig) -> None:
        self.config = config
        self._state_mtime: Optional[float] = None
        self._entries: Dict[str, TrustEntry] = {}
        self._state: Dict[str, Any] = {}
        self._catalog: Catalog = Catalog.default()
        self._error: Optional[str] = None
        self.reload()

    # -- state ---------------------------------------------------------

    def reload(self) -> None:
        """Reload the trust table and catalog, recording any failure as fail-closed."""
        self._catalog = Catalog.default()
        try:
            catalog = Catalog.load(self.config.catalog_path)
            self._catalog = catalog
            state = load_trust_state(self.config.trust_state_path)
            entries = trust_entries(state)
            if state.get("catalog_sha256") != sha256_text(json.dumps(catalog.to_dict(), sort_keys=True)):
                raise TrustStateError("Governance controls changed or lack a recorded fingerprint.")
        except (TrustStateError, OSError, ValueError) as exc:
            self._error = str(exc)
            self._entries = {}
            self._state = {}
            return
        self._error = None
        self._state = state
        self._entries = entries
        self._catalog = catalog
        try:
            self._state_mtime = self.config.trust_state_path.stat().st_mtime
        except OSError:
            self._state_mtime = None

    def _refresh_if_stale(self) -> None:
        """Pick up a trust table rewritten by a governor run while the server is up."""
        # Catalog and authorization can change without rewriting trust.json.
        # Re-read controls on every request; an mtime cache is not a trust proof.
        self.reload()

    # -- the deterministic recheck ---------------------------------------

    def _record(self, path: str) -> Optional[DocumentRecord]:
        return self._catalog.record_for(path)

    def current_dependencies(
        self,
        path: str,
        candidates: Optional[List[str]] = None,
    ) -> Tuple[str, List[str]]:
        """Recompute a document's dependency fingerprint against the working tree."""
        record = self._record(path)
        if record is None:
            return "", []
        snapshot = RepositorySnapshot(root=self.config.root, catalog=self._catalog)
        evidence = dependency_evidence(snapshot, record, candidates=candidates)
        return dependency_fingerprint(evidence), [item.path for item in evidence]

    def recheck(
        self,
        entry: TrustEntry,
        absolute: Path,
        *,
        candidates: Optional[List[str]] = None,
        content: Optional[str] = None,
    ) -> Optional[Tuple[str, str, List[str]]]:
        """Re-prove a table entry against the tree as it is right now.

        Returns ``None`` when the entry still holds, otherwise ``(code, reason,
        read_instead)``. Every caller that reports whether a document is usable
        must go through this: reporting the stored ``usable`` flag on its own
        would let a listing advertise — and summarize — a document that
        ``get_document`` would refuse.
        """
        if not entry.usable:
            return (CODE_NOT_USABLE, entry.reason, list(entry.source_pointers))
        current_fingerprint, current_pointers = self.current_dependencies(
            entry.path, candidates=candidates
        )
        if current_fingerprint != entry.dependency_fingerprint:
            return (
                CODE_DEPENDENCIES_CHANGED,
                "A declared dependency of this document changed after it was verified, "
                "so its claims are unverified.",
                sorted(set(entry.source_pointers) | set(current_pointers)),
            )
        if not absolute.exists() or not absolute.is_file():
            return (
                CODE_MISSING_FILE,
                "The governed document is recorded as trusted but is absent from the working tree.",
                list(entry.source_pointers),
            )
        try:
            if content is None:
                content = absolute.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return (
                CODE_MISSING_FILE,
                "The governed document could not be read as UTF-8 text.",
                list(entry.source_pointers),
            )
        if sha256_text(content) != entry.content_sha256:
            return (
                CODE_CONTENT_CHANGED,
                "The document itself changed after it was verified, so its current text "
                "carries no evidence.",
                list(entry.source_pointers),
            )
        if entry.verifier and entry.verifier.startswith("codex:"):
            try:
                catalog = Catalog.load(self.config.catalog_path)
                record = catalog.record_for(entry.path)
                live = RepositorySnapshot(root=self.config.root, catalog=catalog,
                                          files={entry.path: content})
                valid = record is not None and has_matching_coding_review(live, record)
            except (OSError, ValueError, TypeError):
                valid = False
            if not valid:
                return (CODE_NOT_USABLE, "The coding-agent review has expired, changed, or been revoked.",
                        list(entry.source_pointers))
        return None

    # -- responses -------------------------------------------------------

    def _refusal(
        self,
        path: str,
        *,
        status: str,
        code: str,
        reason: str,
        read_instead: Optional[List[str]] = None,
        canonical_path: Optional[str] = None,
        how_to_resolve: str = RESOLVE_REVERIFY,
    ) -> Dict[str, Any]:
        """Build the explanatory payload that replaces a bare error (D2).

        A bare error makes the consuming agent fall back to reading the raw file,
        which defeats the whole gate. Every refusal therefore says why, and points
        at something that is trustworthy.
        """
        return {
            "status": status,
            "code": code,
            "path": path,
            "content": None,
            "reason": reason,
            "read_instead": sorted(set(read_instead or [])),
            "canonical_path": canonical_path,
            "how_to_resolve": how_to_resolve,
        }

    def get_document(self, path: str) -> Dict[str, Any]:
        self._refresh_if_stale()
        try:
            relative, absolute = resolve_governed_path(self.config.root, path)
        except PathRejected as exc:
            return self._refusal(
                str(path),
                status="refused",
                code=CODE_PATH_REJECTED,
                reason=str(exc),
                how_to_resolve="Request a repository-relative path to a governed Markdown document.",
            )
        if self._error is not None:
            try:
                _, pointers = self.current_dependencies(relative)
            except (OSError, ValueError):
                pointers = []
            return self._refusal(
                relative,
                status="refused",
                code=CODE_STATE_UNAVAILABLE,
                reason=f"The trust state could not be loaded, so no document can be vouched for: {self._error}",
                read_instead=pointers,
            )
        entry = self._entries.get(relative)
        if entry is None:
            return self._refusal(
                relative,
                status="unknown",
                code=CODE_NOT_GOVERNED,
                reason=(
                    "This path is not a governed document, so Doc Governor has no evidence "
                    "that anything it claims is true."
                ),
                how_to_resolve=(
                    "Register the document in .docgov/catalog.yaml and run `docgov review --apply`, "
                    "or read the source files directly."
                ),
            )
        if not entry.usable:
            return self._refusal(relative, status="refused", code=CODE_NOT_USABLE,
                                 reason=entry.reason, read_instead=entry.source_pointers,
                                 canonical_path=entry.canonical_path)
        # The trust table was written by an earlier governor run; re-prove it
        # against the tree as it exists right now.
        try:
            content = absolute.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return self._refusal(relative, status="refused", code=CODE_MISSING_FILE,
                                 reason="The governed document could not be read.",
                                 read_instead=entry.source_pointers)
        failure = self.recheck(entry, absolute, content=content)
        if failure is not None:
            code, reason, read_instead = failure
            return self._refusal(
                relative,
                status="refused",
                code=code,
                reason=reason,
                read_instead=read_instead,
                canonical_path=entry.canonical_path,
            )
        return {
            "status": "ok",
            "code": CODE_OK,
            "path": relative,
            "content": content,
            "verified_at": entry.verified_at,
            "scope": entry.scope,
            "source_pointers": entry.source_pointers,
        }

    def list_documents(
        self,
        type: Optional[str] = None,
        usable_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """List governed documents, including the ones that may not be read (D6).

        Hiding unusable documents would send the consuming agent to the raw file to
        find out what it is missing, so they are listed with ``usable: false`` and
        the refusal reason. Only the binary is exposed here; the internal
        four-level scope stays in ``document_status`` and the ledger (D3).
        """
        self._refresh_if_stale()
        if self._error is not None:
            return []
        # One Git listing for the whole pass; the fingerprint recheck is per
        # document but the candidate enumeration behind it is not.
        candidates = dependency_candidates(
            RepositorySnapshot(root=self.config.root, catalog=self._catalog)
        )
        summaries: List[Dict[str, Any]] = []
        for path, entry in sorted(self._entries.items()):
            if type is not None and entry.type != type:
                continue
            usable = False
            reason = entry.reason
            try:
                _, absolute = resolve_governed_path(self.config.root, path)
            except PathRejected as exc:
                failure = (CODE_PATH_REJECTED, str(exc), [])
            else:
                try:
                    content = absolute.read_bytes().decode("utf-8")
                    failure = self.recheck(entry, absolute, candidates=candidates, content=content)
                except (OSError, UnicodeDecodeError):
                    content = ""
                    failure = (CODE_MISSING_FILE, "The governed document could not be read.", [])
            if failure is None:
                usable = True
                reason = entry.reason
            else:
                reason = failure[1]
            if usable_only and not usable:
                continue
            summary: Optional[str] = None
            if usable:
                # Only a document this server would actually serve gets
                # summarized. Reading a line out of any other one would leak
                # content from a document it has refused.
                try:
                    if absolute.is_file():
                        summary = _summary(content)
                except OSError:
                    summary = None
            summaries.append({
                "path": path,
                "type": entry.type,
                "usable": usable,
                "summary": summary,
                "reason": None if usable else reason,
                "canonical_path": entry.canonical_path,
            })
        return summaries

    def document_status(self, path: str) -> Dict[str, Any]:
        """Return the full trust record, including the internal scope, for humans."""
        self._refresh_if_stale()
        try:
            relative, absolute = resolve_governed_path(self.config.root, path)
        except PathRejected as exc:
            return {"path": str(path), "known": False, "reason": str(exc)}
        if self._error is not None:
            return {"path": relative, "known": False, "reason": self._error}
        entry = self._entries.get(relative)
        if entry is None:
            return {
                "path": relative,
                "known": False,
                "reason": "This path is not a governed document.",
            }
        current_fingerprint, current_pointers = self.current_dependencies(relative)
        failure = self.recheck(entry, absolute)
        record = entry.to_dict()
        record["known"] = True
        # `usable` here is the live verdict, so it can never disagree with what
        # `get_document` would do a moment later.
        record["usable"] = failure is None
        record["recorded_usable"] = entry.usable
        if failure is not None:
            record["reason"] = failure[1]
            record["refusal_code"] = failure[0]
        record["current_dependency_fingerprint"] = current_fingerprint
        record["dependency_fingerprint_matches"] = current_fingerprint == entry.dependency_fingerprint
        record["current_source_pointers"] = current_pointers
        return record


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_document",
        "description": (
            "Read a governed Markdown document. Returns the content only when Doc Governor "
            "can still prove the document's claims are backed by the code it describes; "
            "otherwise returns a refusal explaining why and naming what to read instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path to a Markdown document.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_documents",
        "description": (
            "List governed documents. Unusable documents are included and marked "
            "usable: false with the reason, so you never have to open a raw file to "
            "find out what is missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["contract", "state", "procedure", "evidence", "decision"],
                    "description": "Restrict the listing to one document type.",
                },
                "usable_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "Set false to include documents you may not read.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "document_status",
        "description": (
            "Return the full trust record for one document, including its internal trust "
            "scope and whether its dependency fingerprint still matches. Diagnostic only; "
            "it never returns document content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative Markdown path."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


def dispatch(supply: DocumentSupply, name: str, arguments: Dict[str, Any]) -> Any:
    """Route a tool call. There is deliberately no write, shell, or network tool."""
    if name == "get_document":
        return supply.get_document(str(arguments.get("path", "")))
    if name == "list_documents":
        document_type = arguments.get("type")
        return supply.list_documents(
            type=str(document_type) if document_type is not None else None,
            usable_only=bool(arguments.get("usable_only", True)),
        )
    if name == "document_status":
        return supply.document_status(str(arguments.get("path", "")))
    raise ValueError(f"Unknown tool: {name}")


def build_config(argv: Optional[List[str]] = None) -> SupplyConfig:
    parser = argparse.ArgumentParser(
        prog="docgov-mcp",
        description="Serve only the documentation an AI coding agent is allowed to believe.",
    )
    parser.add_argument("--root", default=".", help="Repository root to serve from")
    parser.add_argument(
        "--trust-state",
        default=None,
        help=f"Trust state path relative to root (default {DEFAULT_TRUST_STATE_PATH})",
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help="Catalog path relative to root (default .docgov/catalog.yaml)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    return SupplyConfig(
        root=root,
        trust_state_path=root / (args.trust_state or DEFAULT_TRUST_STATE_PATH),
        catalog_path=root / (args.catalog or ".docgov/catalog.yaml"),
    )


async def serve(config: SupplyConfig) -> None:  # pragma: no cover - exercised manually
    try:
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
        import mcp.types as types
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The MCP SDK is not installed. Install Doc Governor with the `mcp` extra: "
            "pip install 'doc-governor[mcp]'"
        ) from exc

    supply = DocumentSupply(config)
    server: Any = Server(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()  # type: ignore[misc]
    async def list_tools() -> List[Any]:
        return [
            types.Tool(
                name=definition["name"],
                description=definition["description"],
                inputSchema=definition["inputSchema"],
                annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
            )
            for definition in TOOL_DEFINITIONS
        ]

    @server.call_tool()  # type: ignore[misc]
    async def call_tool(name: str, arguments: Dict[str, Any]) -> List[Any]:
        payload = dispatch(supply, name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version="1",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions=SERVER_INSTRUCTIONS,
            ),
        )


def main(argv: Optional[List[str]] = None) -> int:
    """Start the stdio server, refusing to start at all if the trust table is unusable."""
    config = build_config(argv)
    try:
        # Fail loudly at startup rather than silently serving everything.
        load_trust_state(config.trust_state_path)
    except TrustStateError as exc:
        print(f"docgov-mcp: {exc}", file=sys.stderr)
        return 2
    import asyncio

    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
