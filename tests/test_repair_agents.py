from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from docgov.engine import build_snapshot
from docgov.repair import build_repair_prompt, repair_candidates
from docgov.repair_agents import RepairGraphPlan, RepairPlanError, run_repair_graph


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class StrandsRepairPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        self.catalog = self.root / ".docgov/catalog.yaml"
        write(self.catalog, json.dumps({
            "version": 1,
            "taxonomy": {"procedure": ["AGENTS.md"]},
            "documents": [{
                "path": "AGENTS.md",
                "type": "procedure",
                "status": "current",
                "depends_on": ["src/**"],
            }],
            "policies": {"auto_repair_documents": ["AGENTS.md"]},
        }))
        write(self.root / "AGENTS.md", "# Instructions\n\nUse interface version 1.\n")
        write(self.root / "src/interface.py", "VERSION = 1\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        write(self.root / "src/interface.py", "VERSION = 2\n")
        subprocess.run(["git", "add", "src/interface.py"], cwd=self.root, check=True)
        self.snapshot = build_snapshot(self.root, self.catalog)
        self.candidates = repair_candidates(self.snapshot)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def runner(self, payload: Dict[str, object]):
        def execute(plan: RepairGraphPlan) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
            return {plan.nodes[0].node_id: json.dumps(payload)}, [{"event": "agent_complete", "name": plan.nodes[0].node_id}]
        return execute

    def test_strands_plan_is_embedded_in_the_coding_agent_prompt(self) -> None:
        prompt = build_repair_prompt(
            self.snapshot,
            enable_model=True,
            runner=self.runner({
                "path": "AGENTS.md",
                "instructions": ["Replace the interface version claim with the value in source."],
                "evidence_paths": ["src/interface.py"],
                "needs_human": False,
                "reason": "The declared interface source changed.",
            }),
        )
        self.assertIn("Amazon Bedrock and a read-only Strands graph", prompt)
        self.assertIn("Replace the interface version claim", prompt)
        self.assertIn("src/interface.py", prompt)

    def test_plan_cannot_cite_an_unchanged_or_undeclared_file(self) -> None:
        with self.assertRaises(ValueError):
            run_repair_graph(
                self.snapshot,
                self.candidates,
                runner=self.runner({
                    "path": "AGENTS.md",
                    "instructions": ["Rewrite it."],
                    "evidence_paths": ["secrets.txt"],
                    "needs_human": False,
                    "reason": "A reason.",
                }),
            )

    def test_needs_human_blocks_the_repair_pipeline(self) -> None:
        with self.assertRaises(RepairPlanError):
            run_repair_graph(
                self.snapshot,
                self.candidates,
                runner=self.runner({
                    "path": "AGENTS.md",
                    "instructions": ["Review the ambiguous claim."],
                    "evidence_paths": ["src/interface.py"],
                    "needs_human": True,
                    "reason": "The source does not settle the policy wording.",
                }),
            )


if __name__ == "__main__":
    unittest.main()
