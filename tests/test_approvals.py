from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.approvals import (
    ApprovalStateError,
    ExternalActionStager,
    OutboundBroker,
)
from hermes_operator.db import SQLiteStore


def proposal(*, body: str = "Ready for review") -> dict[str, object]:
    return {
        "action_type": "email.send",
        "integration": "mail",
        "target": {"recipients": ["person@example.com"], "mailbox": "primary"},
        "content": body,
        "attributes": {"subject": "Status"},
        "reason": "Send an approved update",
        "risk": "medium",
    }


class RecordingConnector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, action):
        self.calls.append(action.id)
        return {"ok": True, "provider_id": "out-1"}


class ApprovalPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "operator.db")
        self.store.initialize()
        self.stager = ExternalActionStager(self.store, ttl_seconds=300)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_stage_is_idempotent_and_preserves_exact_payload(self) -> None:
        first = self.stager.stage(proposal(), created_by="supervisor")
        second = self.stager.stage(proposal(), created_by="supervisor")

        self.assertEqual(first, second)
        action = self.stager.get(first)
        self.assertEqual(action.intent.recipients, ("person@example.com",))
        self.assertEqual(action.intent.content, "Ready for review")
        self.assertEqual(action.status, "pending_approval")

    def test_unknown_action_is_rejected(self) -> None:
        value = proposal()
        value["action_type"] = "plugin.unknown"
        with self.assertRaises(ValueError):
            self.stager.stage(value, created_by="supervisor")

    def test_risk_is_derived_from_action_type_not_model_text(self) -> None:
        value = proposal()
        value["action_type"] = "data.delete"
        value["risk"] = "low"

        action_id = self.stager.stage(value, created_by="supervisor")

        self.assertEqual(self.stager.get(action_id).risk, "high")

    def test_approval_is_bound_and_broker_consumes_once(self) -> None:
        action_id = self.stager.stage(proposal(), created_by="supervisor")
        grant = self.stager.approve(action_id, approved_by="operator:chris")
        connector = RecordingConnector()
        broker = OutboundBroker(
            self.stager,
            connectors={"mail": connector},
            enabled=True,
            max_grant_lifetime_seconds=300,
        )

        result = broker.execute(action_id, grant_id=grant.grant_id, actor="operator")

        self.assertTrue(result["ok"])
        self.assertEqual(connector.calls, [action_id])
        self.assertEqual(self.stager.get(action_id).status, "executed")
        with self.assertRaises(ApprovalStateError):
            broker.execute(action_id, grant_id=grant.grant_id, actor="operator")

    def test_disabled_broker_never_consumes_approval(self) -> None:
        action_id = self.stager.stage(proposal(), created_by="supervisor")
        grant = self.stager.approve(action_id, approved_by="operator:chris")
        broker = OutboundBroker(self.stager, enabled=False)

        with self.assertRaises(ApprovalStateError):
            broker.execute(action_id, grant_id=grant.grant_id, actor="operator")

        connector = RecordingConnector()
        enabled = OutboundBroker(
            self.stager,
            connectors={"mail": connector},
            enabled=True,
            max_grant_lifetime_seconds=300,
        )
        enabled.execute(action_id, grant_id=grant.grant_id, actor="operator")
        self.assertEqual(connector.calls, [action_id])

    def test_changed_staged_payload_fails_digest_check(self) -> None:
        action_id = self.stager.stage(proposal(), created_by="supervisor")
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT canonical_json FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
            changed = str(row["canonical_json"]).replace(
                "Ready for review", "Changed after staging"
            )
            connection.execute(
                "UPDATE action_intents SET canonical_json = ? WHERE id = ?",
                (changed, action_id),
            )
        with self.assertRaises(ApprovalStateError):
            self.stager.get(action_id)

    def test_concurrent_approval_creates_only_one_grant(self) -> None:
        action_id = self.stager.stage(proposal(), created_by="supervisor")
        successes: list[str] = []
        failures: list[Exception] = []

        def approve() -> None:
            try:
                successes.append(
                    self.stager.approve(
                        action_id, approved_by="operator:chris"
                    ).grant_id
                )
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM approval_grants WHERE intent_id = ?",
                (action_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
