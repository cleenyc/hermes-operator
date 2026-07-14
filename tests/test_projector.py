from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.adapters import ObsidianAdapter  # noqa: E402
from hermes_operator.approvals import ExternalActionStager  # noqa: E402
from hermes_operator.db import SQLiteStore  # noqa: E402
from hermes_operator.models import (  # noqa: E402
    ExecutionMode,
    UserQuestion,
    WorkItem,
    WorkStatus,
)
from hermes_operator.prioritization import PriorityEngine  # noqa: E402
from hermes_operator.projector import KnowledgeProjector  # noqa: E402


class KnowledgeProjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SQLiteStore(self.root / "operator.db")
        self.store.initialize()

    def test_disabled_projection_is_an_explicit_noop(self) -> None:
        projector = KnowledgeProjector(
            store=self.store,
            obsidian=ObsidianAdapter(
                None, env={}, include_default_candidates=False
            ),
            priority_engine=PriorityEngine(),
        )

        summary = projector.project()

        self.assertFalse(summary.enabled)
        self.assertEqual(summary.created, 0)
        self.assertIn("No Obsidian vault", summary.reason)

    def test_dashboard_and_active_work_are_projected_without_owning_state(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()
        active = WorkItem(
            title="Prepare internal brief",
            description="Draft for review only.",
            status=WorkStatus.READY,
            execution_mode=ExecutionMode.HERMES,
            priority_score=72.5,
            priority_rationale="impact +22.0",
            acceptance_criteria=["Checklist passes"],
        )
        waiting = WorkItem(
            title="Confirm audience",
            status=WorkStatus.WAITING_INPUT,
            parent_id=active.id,
        )
        terminal = WorkItem(title="Already done", status=WorkStatus.DONE)
        for item in (active, waiting, terminal):
            self.store.create_work(item)
        question = UserQuestion(
            question="Which audience should be used?",
            blocking_work_ids=[waiting.id],
        )
        self.store.create_question(question)
        actions = ExternalActionStager(self.store)
        action_id = actions.stage(
            {
                "action_type": "email.send",
                "integration": "mail",
                "target": {"recipients": ["person@example.com"]},
                "content": "Private exact content",
                "reason": "Await final approval",
            },
            created_by="supervisor",
        )
        projector = KnowledgeProjector(
            store=self.store,
            obsidian=ObsidianAdapter(vault),
            priority_engine=PriorityEngine(),
            actions=actions,
        )

        first = projector.project()

        self.assertTrue(first.enabled)
        self.assertEqual(first.created, 4)
        dashboard = (vault / "Hermes Operator" / "Dashboard.md").read_text()
        work_path = vault / "Hermes Operator" / "Work" / f"{active.id}.md"
        work_note = work_path.read_text()
        self.assertIn("Prepare internal brief", dashboard)
        self.assertIn("Which audience should be used?", dashboard)
        self.assertIn(action_id, dashboard)
        self.assertNotIn("Private exact content", dashboard)
        self.assertIn("Checklist passes", work_note)
        terminal_note = (
            vault / "Hermes Operator" / "Work" / f"{terminal.id}.md"
        )
        self.assertTrue(terminal_note.exists())
        self.assertIn("Already done", terminal_note.read_text())

        work_path.write_text(work_note + "\nOperator annotation stays.\n", encoding="utf-8")
        self.store.update_work(active.id, {"title": "Prepare revised internal brief"})
        second = projector.project()

        self.assertGreaterEqual(second.updated, 1)
        revised = work_path.read_text()
        self.assertIn("Prepare revised internal brief", revised)
        self.assertIn("Operator annotation stays.", revised)
        self.assertEqual(self.store.get_work(active.id).title, "Prepare revised internal brief")


if __name__ == "__main__":
    unittest.main()
