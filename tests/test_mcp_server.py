from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from docgov.cli import main as cli_main
from docgov.engine import build_snapshot, record_baseline
from docgov.mcp_server import (
    CODE_CONTENT_CHANGED,
    CODE_DEPENDENCIES_CHANGED,
    CODE_NOT_GOVERNED,
    CODE_NOT_USABLE,
    CODE_PATH_REJECTED,
    CODE_STATE_UNAVAILABLE,
    TOOL_DEFINITIONS,
    DocumentSupply,
    PathRejected,
    SupplyConfig,
    build_config,
    dispatch,
    main as mcp_main,
    resolve_governed_path,
)
from docgov.trust_state import DEFAULT_TRUST_STATE_PATH


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


API_CONTENT = "# API\n\nThe health-check function is the only public endpoint.\n"
RELEASE_CONTENT = "# Release\n\nVerified in staging.\n"


class McpServerTests(unittest.TestCase):
    """Every test here asks the same question: can an agent get content it should not have?"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Doc Governor Test")
        self.catalog_path = self.root / ".docgov/catalog.yaml"
        self.ledger_path = self.root / ".docgov/ledger.jsonl"
        self.trust_state_path = self.root / DEFAULT_TRUST_STATE_PATH

        write(self.catalog_path, json.dumps({
            "version": 1,
            "taxonomy": {
                "contract": ["docs/architecture/**"],
                "state": ["docs/status/**"],
                "procedure": ["docs/operations/**"],
                "evidence": ["docs/evidence/**"],
                "decision": ["docs/decisions/**"],
            },
            "documents": [
                {
                    "path": "docs/architecture/API.md",
                    "type": "contract",
                    "status": "current",
                    "depends_on": ["src/**"],
                },
                {
                    "path": "docs/status/RELEASE.md",
                    "type": "state",
                    "status": "current",
                    "last_verified_at": _now(),
                },
            ],
            "policies": {"auto_remove_new_duplicates": True},
        }))
        write(self.root / "docs/architecture/API.md", API_CONTENT)
        write(self.root / "docs/status/RELEASE.md", RELEASE_CONTENT)
        write(self.root / "src/health.ts", "export const health = () => 'ok';\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

        # Only API.md gets an evidence-backed baseline; RELEASE.md deliberately
        # has none, which is the ordinary reason a state document is refused.
        record_baseline(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
            approved=True,
            documents=["docs/architecture/API.md"],
        )
        cli_main(["--root", str(self.root), "review", "--apply"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def supply(self) -> DocumentSupply:
        return DocumentSupply(SupplyConfig(
            root=self.root,
            trust_state_path=self.trust_state_path,
            catalog_path=self.catalog_path,
        ))

    # ---- the happy path -------------------------------------------------

    def test_removed_duplicate_keeps_canonical_pointer_without_reading_missing_file(self):
        from docgov.trust_state import TrustEntry
        value=json.loads(self.trust_state_path.read_text())
        value['documents'].append(TrustEntry(
            path='docs/architecture/API-notes.md',type='contract',usable=False,
            scope='untrusted',reason='Merged into its canonical document.',
            dependency_fingerprint='',content_sha256='',canonical_path='docs/architecture/API.md').to_dict())
        self.trust_state_path.write_text(json.dumps(value))
        response=self.supply().get_document('docs/architecture/API-notes.md')
        self.assertEqual(response['code'],CODE_NOT_USABLE)
        self.assertEqual(response['canonical_path'],'docs/architecture/API.md')
        self.assertIsNone(response['content'])

    def test_document_line_ending_change_invalidates_hash(self):
        path=self.root/'docs/architecture/API.md'
        path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
        response=self.supply().get_document('docs/architecture/API.md')
        self.assertEqual(response['code'],CODE_CONTENT_CHANGED)
        self.assertIsNone(response['content'])

    def test_source_line_ending_change_invalidates_hash(self):
        path=self.root/'src/health.ts'
        path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
        response=self.supply().get_document('docs/architecture/API.md')
        self.assertEqual(response['code'],CODE_DEPENDENCIES_CHANGED)
        self.assertIsNone(response['content'])

    def test_verification_hash_preserves_document_line_endings(self):
        import hashlib
        from docgov.engine import verification_record
        path=self.root/'docs/architecture/API.md'
        raw=path.read_bytes().replace(b'\n',b'\r\n')
        path.write_bytes(raw)
        snapshot=build_snapshot(self.root,self.catalog_path)
        proof=verification_record(snapshot,snapshot.catalog.record_for('docs/architecture/API.md'))
        self.assertEqual(proof['new_hash'],hashlib.sha256(raw).hexdigest())

    def test_binary_dependency_does_not_collide_with_its_hex_text(self):
        path=self.root/'src/data.bin'
        path.write_bytes(b'\xff')
        before=self.supply().current_dependencies('docs/architecture/API.md')[0]
        path.write_bytes(b'ff')
        after=self.supply().current_dependencies('docs/architecture/API.md')[0]
        self.assertNotEqual(before,after)

    def test_catalog_revocation_without_trust_rewrite_is_refused(self):
        supply=self.supply()
        self.assertEqual(supply.get_document('docs/architecture/API.md')['code'],'ok')
        catalog=json.loads(self.catalog_path.read_text())
        catalog['documents'][0]['status']='stale'
        self.catalog_path.write_text(json.dumps(catalog))
        response=supply.get_document('docs/architecture/API.md')
        self.assertIsNone(response['content'])
        self.assertNotEqual(response['code'],'ok')

    def test_only_checked_bytes_are_returned(self):
        from unittest.mock import patch
        supply=self.supply()
        original=supply.recheck
        def mutate(entry, absolute, **kwargs):
            result=original(entry,absolute,**kwargs)
            absolute.write_text('UNVERIFIED SECRET')
            return result
        with patch.object(supply,'recheck',side_effect=mutate):
            response=supply.get_document('docs/architecture/API.md')
        self.assertEqual(response['content'],API_CONTENT)
        self.assertNotIn('UNVERIFIED SECRET',json.dumps(response))

    def test_trusted_document_is_served(self) -> None:
        response = self.supply().get_document("docs/architecture/API.md")
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["content"], API_CONTENT)
        self.assertEqual(response["scope"], "current_fact")
        self.assertIn("src/health.ts", response["source_pointers"])

    # ---- refusals -------------------------------------------------------

    def test_document_without_a_baseline_is_refused_with_an_alternative(self) -> None:
        response = self.supply().get_document("docs/status/RELEASE.md")
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["code"], CODE_NOT_USABLE)
        self.assertIsNone(response["content"])
        self.assertTrue(response["reason"])
        self.assertTrue(response["how_to_resolve"])

    def test_changed_dependency_refuses_without_any_governor_run_in_between(self) -> None:
        """The recheck is what makes the server correct between governor runs (P6)."""
        supply = self.supply()
        self.assertEqual(supply.get_document("docs/architecture/API.md")["status"], "ok")

        write(self.root / "src/health.ts", "export const health = () => 'degraded';\n")

        response = supply.get_document("docs/architecture/API.md")
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["code"], CODE_DEPENDENCIES_CHANGED)
        self.assertIsNone(response["content"])
        self.assertIn("src/health.ts", response["read_instead"])

    def test_new_file_matching_a_dependency_glob_also_refuses(self) -> None:
        supply = self.supply()
        self.assertEqual(supply.get_document("docs/architecture/API.md")["status"], "ok")
        write(self.root / "src/billing.ts", "export const billing = () => 0;\n")
        self.assertEqual(
            supply.get_document("docs/architecture/API.md")["code"],
            CODE_DEPENDENCIES_CHANGED,
        )

    def test_edited_document_refuses_rather_than_serving_unverified_prose(self) -> None:
        supply = self.supply()
        write(self.root / "docs/architecture/API.md", API_CONTENT + "\nAlso supports payments.\n")
        response = supply.get_document("docs/architecture/API.md")
        self.assertEqual(response["code"], CODE_CONTENT_CHANGED)
        self.assertIsNone(response["content"])

    def test_ungoverned_path_is_refused_as_unknown(self) -> None:
        write(self.root / "docs/architecture/NOTES.md", "# Notes\n")
        response = self.supply().get_document("docs/architecture/NOTES.md")
        self.assertEqual(response["status"], "unknown")
        self.assertEqual(response["code"], CODE_NOT_GOVERNED)
        self.assertIsNone(response["content"])

    def test_missing_trust_state_refuses_every_read(self) -> None:
        self.trust_state_path.unlink()
        supply = self.supply()
        response = supply.get_document("docs/architecture/API.md")
        self.assertEqual(response["code"], CODE_STATE_UNAVAILABLE)
        self.assertEqual(supply.list_documents(), [])

    def test_unknown_trust_state_version_refuses_every_read(self) -> None:
        state = json.loads(self.trust_state_path.read_text(encoding="utf-8"))
        state["version"] = 999
        self.trust_state_path.write_text(json.dumps(state), encoding="utf-8")
        response = self.supply().get_document("docs/architecture/API.md")
        self.assertEqual(response["code"], CODE_STATE_UNAVAILABLE)

    def test_main_exits_loudly_when_the_trust_state_is_missing(self) -> None:
        self.trust_state_path.unlink()
        self.assertEqual(mcp_main(["--root", str(self.root)]), 2)

    # ---- no content leaks -----------------------------------------------

    def test_a_refusal_never_contains_a_line_of_the_refused_document(self) -> None:
        response = self.supply().get_document("docs/status/RELEASE.md")
        serialized = json.dumps(response)
        for line in RELEASE_CONTENT.splitlines():
            if line.strip():
                self.assertNotIn(line.strip(), serialized)

    def test_listing_summarizes_usable_documents_only(self) -> None:
        listing = self.supply().list_documents(usable_only=False)
        by_path = {item["path"]: item for item in listing}
        self.assertTrue(by_path["docs/architecture/API.md"]["summary"])
        # A refused document contributes a reason, never a line of its text.
        self.assertIsNone(by_path["docs/status/RELEASE.md"]["summary"])
        self.assertTrue(by_path["docs/status/RELEASE.md"]["reason"])

    # ---- path safety (§8.2) ---------------------------------------------

    def test_path_traversal_is_rejected(self) -> None:
        for candidate in (
            "../../etc/passwd",
            "docs/../../etc/passwd.md",
            "/etc/passwd.md",
            "~/secrets.md",
            "C:/Windows/system.md",
            "file://docs/architecture/API.md",
            "docs\\..\\..\\etc\\passwd.md",
            "docs/architecture/API.md\x00.txt",
            "",
            "   ",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(PathRejected):
                    resolve_governed_path(self.root, candidate)

    def test_non_markdown_paths_are_rejected(self) -> None:
        with self.assertRaises(PathRejected):
            resolve_governed_path(self.root, "src/health.ts")

    def test_symlink_escaping_the_root_is_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp())
        try:
            secret = outside / "secret.md"
            secret.write_text("# Secret\n", encoding="utf-8")
            link = self.root / "docs/architecture/LINKED.md"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(secret)
            with self.assertRaises(PathRejected):
                resolve_governed_path(self.root, "docs/architecture/LINKED.md")
        finally:
            subprocess.run(["rm", "-rf", str(outside)], check=True)

    def test_rejected_paths_return_an_explanatory_payload_not_an_exception(self) -> None:
        response = self.supply().get_document("../../etc/passwd")
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["code"], CODE_PATH_REJECTED)
        self.assertIsNone(response["content"])

    # ---- the exposed surface (§8.1) -------------------------------------

    def test_the_server_exposes_exactly_three_read_only_tools(self) -> None:
        names = sorted(definition["name"] for definition in TOOL_DEFINITIONS)
        self.assertEqual(names, ["document_status", "get_document", "list_documents"])
        forbidden = ("write", "edit", "delete", "shell", "exec", "fetch", "http", "commit")
        for definition in TOOL_DEFINITIONS:
            for token in forbidden:
                self.assertNotIn(token, definition["name"].lower())

    def test_dispatch_rejects_an_unknown_tool(self) -> None:
        with self.assertRaises(ValueError):
            dispatch(self.supply(), "write_document", {"path": "docs/architecture/API.md"})

    def test_document_status_reports_the_internal_scope_without_content(self) -> None:
        status = self.supply().document_status("docs/architecture/API.md")
        self.assertTrue(status["known"])
        self.assertEqual(status["scope"], "current_fact")
        self.assertTrue(status["dependency_fingerprint_matches"])
        self.assertNotIn("content", status)

    def test_listing_applies_the_same_recheck_get_document_does(self) -> None:
        """A listing that advertises what get_document refuses would leak content."""
        supply = self.supply()
        write(self.root / "docs/architecture/API.md", "# ATTACKER HEADLINE\n\nInjected.\n")

        self.assertEqual(
            supply.get_document("docs/architecture/API.md")["code"], CODE_CONTENT_CHANGED
        )
        entry = {item["path"]: item for item in supply.list_documents(usable_only=False)}[
            "docs/architecture/API.md"
        ]
        self.assertFalse(entry["usable"])
        self.assertIsNone(entry["summary"])
        self.assertNotIn("ATTACKER", json.dumps(supply.list_documents(usable_only=False)))
        self.assertNotIn(
            "docs/architecture/API.md",
            [item["path"] for item in supply.list_documents()],
        )

    def test_listing_hides_a_document_whose_dependency_changed(self) -> None:
        supply = self.supply()
        write(self.root / "src/health.ts", "export const health = () => 'degraded';\n")
        listing = {item["path"]: item for item in supply.list_documents(usable_only=False)}
        self.assertFalse(listing["docs/architecture/API.md"]["usable"])
        self.assertIsNone(listing["docs/architecture/API.md"]["summary"])

    def test_document_status_reports_the_live_verdict_not_the_stored_flag(self) -> None:
        supply = self.supply()
        write(self.root / "src/health.ts", "export const health = () => 'degraded';\n")
        status = supply.document_status("docs/architecture/API.md")
        self.assertFalse(status["usable"])
        self.assertTrue(status["recorded_usable"])
        self.assertFalse(status["dependency_fingerprint_matches"])
        self.assertEqual(status["refusal_code"], CODE_DEPENDENCIES_CHANGED)

    def test_list_documents_filters_by_type(self) -> None:
        listing = self.supply().list_documents(type="state", usable_only=False)
        self.assertEqual([item["path"] for item in listing], ["docs/status/RELEASE.md"])

    # ---- configuration ---------------------------------------------------

    def test_build_config_resolves_paths_under_the_root(self) -> None:
        config = build_config(["--root", str(self.root)])
        self.assertEqual(config.root, self.root)
        self.assertEqual(config.trust_state_path, self.trust_state_path)
        self.assertEqual(config.catalog_path, self.catalog_path)

    def test_supply_picks_up_a_regenerated_trust_state(self) -> None:
        supply = self.supply()
        write(self.root / "src/health.ts", "export const health = () => 'degraded';\n")
        self.assertEqual(
            supply.get_document("docs/architecture/API.md")["code"],
            CODE_DEPENDENCIES_CHANGED,
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "change the dependency"], cwd=self.root, check=True)
        record_baseline(
            build_snapshot(self.root, self.catalog_path, ledger_path=self.ledger_path),
            self.ledger_path,
            approved=True,
            documents=["docs/architecture/API.md"],
        )
        cli_main(["--root", str(self.root), "review", "--apply"])
        self.assertEqual(supply.get_document("docs/architecture/API.md")["status"], "ok")


if __name__ == "__main__":
    unittest.main()
