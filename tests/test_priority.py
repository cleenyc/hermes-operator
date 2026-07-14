from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.models import WorkItem, WorkStatus
from hermes_operator.prioritization import PriorityEngine


class PriorityEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PriorityEngine(max_contextual_adjustment=5)
        self.now = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)

    def test_due_impact_and_dependency_components_are_deterministic(self) -> None:
        item = WorkItem(
            title="High value task",
            status=WorkStatus.READY,
            impact=1,
            urgency=1,
            strategic_alignment=1,
            confidence=1,
            risk=0,
            effort_minutes=15,
            due_at=(self.now + timedelta(hours=2)).isoformat(),
            created_at=(self.now - timedelta(days=4)).isoformat(),
        )
        eligible = self.engine.score(
            item,
            now=self.now,
            dependencies_satisfied=True,
            contextual_adjustment=100,
            contextual_reason="operator focus",
        )
        blocked = self.engine.score(
            item, now=self.now, dependencies_satisfied=False
        )

        self.assertEqual(eligible.components["contextual"], 5)
        self.assertEqual(eligible.components["due"], 21)
        self.assertEqual(
            eligible.score - blocked.score,
            35,
        )
        self.assertIn("contextual adjustment: operator focus", eligible.rationale)

    def test_terminal_items_never_rank_as_next_work(self) -> None:
        terminal = WorkItem(
            title="Completed", status=WorkStatus.DONE, priority_score=10000
        )
        ready = WorkItem(
            title="Ready", status=WorkStatus.READY, priority_score=30
        )
        review = WorkItem(
            title="Review", status=WorkStatus.REVIEW, priority_score=40
        )
        running = WorkItem(
            title="Running", status=WorkStatus.RUNNING, priority_score=50
        )
        triage = WorkItem(
            title="Triage", status=WorkStatus.TRIAGE, priority_score=35
        )

        self.assertEqual(self.engine.score(terminal).score, -1000)
        self.assertEqual(
            [
                item.id
                for item in self.engine.next_best(
                    [terminal, ready, review, triage]
                )
            ],
            [review.id, triage.id, ready.id],
        )
        self.assertEqual(
            self.engine.next_best(
                [terminal, ready, review, running], limit=1, include_running=True
            )[0].id,
            running.id,
        )


if __name__ == "__main__":
    unittest.main()
