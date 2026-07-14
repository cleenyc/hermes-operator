from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.db import SQLiteStore  # noqa: E402
from hermes_operator.models import (  # noqa: E402
    UserQuestion,
    WorkItem,
    WorkKind,
    WorkStatus,
    next_recurrence_due,
)


class ReminderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteStore(Path(self.temporary.name) / "operator.db")
        self.store.initialize()

    def test_fixed_recurrence_rolls_forward_from_original_anchor(self) -> None:
        result = next_recurrence_due(
            "2026-07-10T09:00:00Z",
            "P1D",
            after=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )

        self.assertEqual(result, "2026-07-15T09:00:00Z")

    def test_attention_claim_is_atomic_and_honors_redelivery_window(self) -> None:
        reminder = WorkItem(
            title="Submit report",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2026-07-14T09:00:00Z",
        )
        question = UserQuestion(question="Which customer should be first?")
        self.store.create_work(reminder)
        self.store.create_question(question)
        first_time = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

        first = self.store.claim_attention(
            redelivery_seconds=3600,
            now=first_time,
        )
        suppressed = self.store.claim_attention(
            redelivery_seconds=3600,
            now=first_time + timedelta(minutes=59),
        )
        redelivered = self.store.claim_attention(
            redelivery_seconds=3600,
            now=first_time + timedelta(hours=1),
        )

        self.assertEqual([item.id for item in first["reminders"]], [reminder.id])
        self.assertEqual([item["id"] for item in first["questions"]], [question.id])
        self.assertEqual(suppressed["reminders"], [])
        self.assertEqual(suppressed["questions"], [])
        self.assertEqual(redelivered["reminders"][0].reminder_delivery_count, 2)
        self.assertEqual(redelivered["questions"][0]["delivery_count"], 2)

    def test_snooze_resets_delivery_and_requires_a_future_time(self) -> None:
        reminder = WorkItem(
            title="Call the accountant",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2020-01-01T09:00:00Z",
        )
        self.store.create_work(reminder)
        delivered = self.store.claim_due_reminders()[0]
        until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

        snoozed = self.store.resolve_reminder(
            reminder.id,
            action="snooze",
            expected_version=delivered.version,
            until=until,
        )

        self.assertEqual(snoozed.due_at, "2020-01-01T09:00:00Z")
        self.assertEqual(snoozed.reminder_snoozed_until, until)
        self.assertIsNone(snoozed.reminder_last_delivered_at)
        self.assertEqual(self.store.claim_due_reminders(), [])

    def test_snooze_does_not_shift_the_recurring_schedule(self) -> None:
        original_due = "2026-07-14T09:00:00Z"
        reminder = WorkItem(
            title="Daily review",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at=original_due,
            recurrence_rule="P1D",
        )
        self.store.create_work(reminder)
        snooze_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

        snoozed = self.store.resolve_reminder(
            reminder.id,
            action="snooze",
            expected_version=reminder.version,
            until=snooze_until,
        )
        completed = self.store.resolve_reminder(
            reminder.id,
            action="complete",
            expected_version=snoozed.version,
        )

        self.assertEqual(snoozed.due_at, original_due)
        self.assertEqual(snoozed.reminder_snoozed_until, snooze_until)
        self.assertIsNone(completed.reminder_snoozed_until)
        next_due = datetime.fromisoformat(completed.due_at.replace("Z", "+00:00"))
        self.assertEqual(next_due.hour, 9)
        self.assertGreater(next_due, datetime.now(UTC))

    def test_complete_rolls_recurring_reminder_and_finishes_one_shot(self) -> None:
        recurring = WorkItem(
            title="Weekly review",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2020-01-06T09:00:00Z",
            recurrence_rule="P1W",
        )
        one_shot = WorkItem(
            title="One time follow-up",
            kind=WorkKind.REMINDER,
            status=WorkStatus.TRIAGE,
            due_at="2020-01-01T09:00:00Z",
        )
        self.store.create_work(recurring)
        self.store.create_work(one_shot)

        advanced = self.store.resolve_reminder(
            recurring.id,
            action="complete",
            expected_version=recurring.version,
        )
        completed = self.store.resolve_reminder(
            one_shot.id,
            action="acknowledge",
            expected_version=one_shot.version,
        )

        self.assertEqual(advanced.status, WorkStatus.READY)
        self.assertGreater(
            datetime.fromisoformat(advanced.due_at.replace("Z", "+00:00")),
            datetime.now(UTC),
        )
        self.assertIsNotNone(advanced.reminder_last_acknowledged_at)
        self.assertEqual(completed.status, WorkStatus.DONE)
        self.assertIsNotNone(completed.reminder_last_acknowledged_at)

    def test_recurrence_requires_reminder_kind_and_due_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "only for reminder"):
            self.store.create_work(
                WorkItem(title="Not a reminder", recurrence_rule="P1D")
            )
        with self.assertRaisesRegex(ValueError, "requires due_at"):
            self.store.create_work(
                WorkItem(
                    title="Missing anchor",
                    kind=WorkKind.REMINDER,
                    recurrence_rule="P1D",
                )
            )


if __name__ == "__main__":
    unittest.main()
