from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.api import APIContext, APIService  # noqa: E402
from hermes_operator.approvals import ExternalActionStager  # noqa: E402
from hermes_operator.db import SQLiteStore  # noqa: E402
from hermes_operator.models import (  # noqa: E402
    Event,
    RunRecord,
    UserQuestion,
    WorkItem,
    WorkKind,
    WorkStatus,
)


class APITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temporary.name) / "operator.db")
        self.store.initialize()
        self.actions = ExternalActionStager(self.store, ttl_seconds=300)
        self.wakes: list[str] = []
        self.context = APIContext(
            store=self.store,
            api_token="operator-token",
            bridge_token="bridge-token",
            webhook_secrets={"gmail": "gmail-secret"},
            allow_unsigned_webhooks=True,
            max_body_bytes=1024,
            wake=self.wakes.append,
            health_provider=lambda: {
                "status": "running",
                "running": True,
                "cycle_count": 2,
                "operational_counters": {"private": 99},
                "database": "/private/operator.db",
                "last_cycle": {"errors": {"llm": "sensitive detail"}},
            },
            action_stager=self.actions,
        )
        self.service = APIService("127.0.0.1", 0, self.context)
        self.host, self.port = self.service.start()

    def tearDown(self) -> None:
        self.service.stop()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        document: dict[str, Any] | None = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = raw if raw is not None else (
            json.dumps(document).encode("utf-8") if document is not None else None
        )
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload.decode("utf-8"))
        finally:
            connection.close()

    @staticmethod
    def operator_headers() -> dict[str, str]:
        return {"Authorization": "Bearer operator-token"}

    @staticmethod
    def bridge_headers() -> dict[str, str]:
        return {"Authorization": "Bearer bridge-token"}

    def test_health_is_public_but_operator_reads_are_protected(self) -> None:
        status, document = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["runtime"]["cycle_count"], 2)
        self.assertNotIn("database", document["runtime"])
        self.assertNotIn("last_cycle", document["runtime"])
        self.assertNotIn("operational_counters", document["runtime"])

        status, document = self.request("GET", "/v1/work")
        self.assertEqual(status, 401)
        self.assertEqual(document["error"]["code"], "unauthorized")

        status, document = self.request(
            "GET", "/v1/work", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["items"], [])

    def test_authenticated_status_exposes_content_free_operational_counters(self) -> None:
        self.store.enqueue_event(
            Event(
                source="gmail",
                event_type="email.received",
                payload={"subject": "private"},
            )
        )
        self.store.create_question(UserQuestion(question="Private question"))
        work = WorkItem(title="Private work", status=WorkStatus.RUNNING)
        self.store.create_work(work)
        self.store.create_run(
            RunRecord(
                work_item_id=work.id,
                runner="hermes",
                status="running",
            )
        )

        status, _ = self.request("GET", "/v1/status")
        self.assertEqual(status, 401)
        status, document = self.request(
            "GET", "/v1/status", headers=self.operator_headers()
        )

        self.assertEqual(status, 200)
        counters = document["operational_counters"]
        self.assertEqual(counters["events"]["pending"], 1)
        self.assertEqual(counters["events"]["processing"], 0)
        self.assertEqual(counters["events"]["failed"], 0)
        self.assertEqual(counters["events"]["dead_letter"], 0)
        self.assertEqual(counters["pending_questions"], 1)
        self.assertEqual(counters["active_work"], 1)
        self.assertEqual(counters["active_runs"], 1)
        self.assertNotIn("private", json.dumps(counters))

    def test_bridge_status_exposes_only_liveness_and_content_free_counters(self) -> None:
        self.store.enqueue_event(
            Event(
                source="gmail",
                event_type="email.received",
                payload={"subject": "private"},
            )
        )

        status, document = self.request("GET", "/v1/hermes/status")
        self.assertEqual(status, 401)
        status, document = self.request(
            "GET", "/v1/hermes/status", headers=self.bridge_headers()
        )

        self.assertEqual(status, 200)
        self.assertEqual(document["status"], "running")
        self.assertTrue(document["running"])
        self.assertEqual(document["cycle_count"], 2)
        self.assertEqual(
            document["operational_counters"]["events"]["pending"], 1
        )
        self.assertNotIn("database", document)
        self.assertNotIn("last_cycle", document)
        self.assertNotIn("private", json.dumps(document))

    def test_bridge_token_is_scoped_to_context_and_hermes_ingress(self) -> None:
        item = WorkItem(title="Visible next task", status=WorkStatus.READY)
        self.store.create_work(item)
        pending_question = UserQuestion(question="Pending operator question")
        answered_question = UserQuestion(question="Historical operator question")
        self.store.create_question(pending_question)
        self.store.create_question(answered_question)
        self.store.answer_question(
            answered_question.id,
            "Sensitive historical answer",
        )

        status, _ = self.request(
            "GET", "/v1/next", headers=self.bridge_headers()
        )
        self.assertEqual(status, 200)
        status, document = self.request(
            "GET", "/v1/questions", headers=self.bridge_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual([value["id"] for value in document["items"]], [pending_question.id])
        self.assertNotIn("answer", document["items"][0])
        status, document = self.request(
            "GET",
            "/v1/questions?status=answered",
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 403)
        self.assertEqual(document["error"]["code"], "bridge_question_scope")
        status, document = self.request(
            "GET",
            "/v1/questions?status=answered",
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["items"][0]["answer"], "Sensitive historical answer")
        status, _ = self.request(
            "GET", "/v1/work", headers=self.bridge_headers()
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST",
            "/v1/ingest",
            document={"event_type": "operator.request", "payload": {}},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 401)
        status, document = self.request(
            "POST",
            "/v1/events/hermes",
            document={
                "event_type": "turn_completed",
                "payload": {"session_id": "session-1"},
            },
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 202)
        self.assertEqual(document["trust_level"], "authenticated_untrusted")

    def test_bridge_can_read_only_the_exact_task_execution_contract(self) -> None:
        seen: list[str] = []

        def provider(task_id: str) -> dict[str, Any]:
            seen.append(task_id)
            return {
                "authorized": True,
                "task_id": task_id,
                "work_id": "wrk_1",
                "profile": "operator",
                "contract_digest": "a" * 64,
                "run_id": "run_1",
                "internal_capabilities": ["local_test"],
            }

        self.context.execution_contract_provider = provider
        status, document = self.request(
            "GET",
            "/v1/hermes/execution-contract?task_id=t_123",
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertTrue(document["authorized"])
        self.assertEqual(seen, ["t_123"])

        status, _ = self.request(
            "GET",
            "/v1/hermes/execution-contract?task_id=t_123",
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 401)
        status, document = self.request(
            "GET",
            "/v1/hermes/execution-contract?task_id=../../bad",
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_task_id")

    def test_bridge_can_atomically_claim_only_a_bounded_delegation_batch(self) -> None:
        seen: list[tuple[str, int]] = []

        def provider(task_id: str, requested_children: int) -> dict[str, Any]:
            seen.append((task_id, requested_children))
            return {
                "claimed": True,
                "task_id": task_id,
                "run_id": "run_1",
                "contract_digest": "a" * 64,
                "requested_children": requested_children,
                "reason": "claimed",
            }

        self.context.delegation_claim_provider = provider
        status, document = self.request(
            "POST",
            "/v1/hermes/delegation-claim",
            document={"task_id": "t_123", "requested_children": 3},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertTrue(document["claimed"])
        self.assertEqual(seen, [("t_123", 3)])

        status, _ = self.request(
            "POST",
            "/v1/hermes/delegation-claim",
            document={"task_id": "t_123", "requested_children": 1},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 401)
        status, document = self.request(
            "POST",
            "/v1/hermes/delegation-claim",
            document={"task_id": "t_123", "requested_children": 4},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_delegation_claim")

    def test_bridge_conversation_can_capture_answer_and_authorize_exact_work(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/hermes/work",
            document={
                "title": "Prepare the launch checklist",
                "description": "Capture this before details are lost",
                "kind": "task",
                "due_at": "2026-07-18T17:00:00-04:00",
                "parent_id": None,
            },
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 201)
        work_id = created["work"]["id"]
        item = self.store.get_work(work_id)
        self.assertEqual(item.status, WorkStatus.TRIAGE)
        self.assertEqual(item.execution_mode.value, "none")
        self.assertFalse(item.metadata["governance"]["execution_authorized"])
        self.assertIn("hermes-work-created", self.wakes)

        status, updated = self.request(
            "POST",
            f"/v1/hermes/work/{work_id}/update",
            document={
                "expected_version": item.version,
                "changes": {"due_at": "2026-07-19T09:00:00-04:00"},
            },
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["work"]["version"], item.version + 1)
        item = self.store.get_work(work_id)
        self.assertIn("hermes-work-updated", self.wakes)

        question = UserQuestion(
            question="Which launch is in scope?",
            blocking_work_ids=[work_id],
        )
        self.store.create_question(question)
        status, answered = self.request(
            "POST",
            f"/v1/hermes/questions/{question.id}/answer",
            document={"answer": "The July customer launch"},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(answered["answer"], "The July customer launch")

        status, authorized = self.request(
            "POST",
            f"/v1/hermes/work/{work_id}/authorize",
            document={
                "expected_version": item.version,
                "reason": "The operator approved internal execution",
            },
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 202)
        self.assertEqual(authorized["work_id"], work_id)
        self.assertEqual(authorized["work_version"], item.version)
        self.assertEqual(authorized["authorization_scope_revision"], 2)
        self.assertEqual(len(authorized["authorization_scope_digest"]), 64)
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT source, event_type, trust_level, payload_json FROM events "
                "WHERE id = ?",
                (authorized["event_id"],),
            ).fetchone()
        self.assertEqual(row["source"], "operator")
        self.assertEqual(row["event_type"], "operator.work_authorized")
        self.assertEqual(row["trust_level"], "operator")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["work_id"], work_id)
        self.assertEqual(payload["work_version"], item.version)
        self.assertEqual(
            payload["scope_digest"],
            authorized["authorization_scope_digest"],
        )

        status, _ = self.request(
            "POST",
            f"/v1/hermes/work/{work_id}/authorize",
            document={"expected_version": item.version},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 401)

    def test_bridge_authorization_rejects_a_revision_changed_before_capture(self) -> None:
        item = WorkItem(title="Displayed revision")
        self.store.create_work(item)
        self.store.update_work(
            item.id,
            {"description": "Changed after it was displayed"},
            expected_version=item.version,
            actor="operator-api",
        )

        status, document = self.request(
            "POST",
            f"/v1/hermes/work/{item.id}/authorize",
            document={"expected_version": item.version},
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 409)
        self.assertEqual(document["error"]["code"], "state_conflict")
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type = 'operator.work_authorized'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_bridge_google_skill_ingress_is_revision_deduplicated_untrusted_evidence(self) -> None:
        envelope = {
            "source": "google.gmail",
            "events": [
                {
                    "event_type": "email.received",
                    "external_id": "gmail-message-1",
                    "revision": "history-42",
                    "payload": {
                        "from": "manager@example.com",
                        "subject": "Prepare the review by Friday",
                    },
                }
            ],
        }
        status, first = self.request(
            "POST",
            "/v1/hermes/inbound",
            document=envelope,
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 202)
        self.assertEqual(first["created"], 1)
        status, second = self.request(
            "POST",
            "/v1/hermes/inbound",
            document=envelope,
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["items"][0]["event_id"], first["items"][0]["event_id"])
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT trust_level, provenance_json FROM events WHERE id = ?",
                (first["items"][0]["event_id"],),
            ).fetchone()
        self.assertEqual(row["trust_level"], "authenticated_untrusted")
        self.assertEqual(
            json.loads(row["provenance_json"])["ingress"],
            "hermes-google-skill",
        )
        self.assertEqual(self.wakes.count("event:google.gmail"), 1)

        status, document = self.request(
            "POST",
            "/v1/hermes/inbound",
            document={**envelope, "source": "arbitrary.connector"},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_request")

    def test_bridge_reads_then_atomically_claims_due_reminders(self) -> None:
        due = WorkItem(
            title="Submit the expense report",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2020-01-01T09:00:00Z",
        )
        future = WorkItem(
            title="Renew the passport",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2099-01-01T09:00:00Z",
        )
        snoozed = WorkItem(
            title="Review after snooze",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2020-01-01T09:00:00Z",
            reminder_snoozed_until="2099-01-02T09:00:00Z",
        )
        self.store.create_work(due)
        self.store.create_work(future)
        self.store.create_work(snoozed)

        status, document = self.request(
            "GET",
            "/v1/hermes/reminders?limit=20",
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in document["items"]], [due.id])
        self.assertEqual(document["items"][0]["delivery_state"]["delivery_count"], 0)
        self.assertEqual(self.store.get_work(due.id).status, WorkStatus.READY)

        status, preview = self.request(
            "GET",
            "/v1/hermes/reminders?limit=20",
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in preview["items"]], [due.id])

        status, attention_preview = self.request(
            "GET",
            "/v1/hermes/attention?limit=20",
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in attention_preview["reminders"]],
            [due.id],
        )

        status, claimed = self.request(
            "POST",
            "/v1/hermes/attention/claim",
            document={"limit": 20},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in claimed["reminders"]], [due.id])
        self.assertEqual(claimed["reminders"][0]["delivery_state"]["delivery_count"], 1)

        status, repeated = self.request(
            "POST",
            "/v1/hermes/attention/claim",
            document={"limit": 20},
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated["reminders"], [])

    def test_bridge_attention_claim_includes_questions_atomically(self) -> None:
        reminder = WorkItem(
            title="Review contract",
            kind=WorkKind.REMINDER,
            status=WorkStatus.READY,
            due_at="2020-01-01T09:00:00Z",
        )
        question = UserQuestion(question="Which contract version is canonical?")
        self.store.create_work(reminder)
        self.store.create_question(question)

        status, preview = self.request(
            "GET",
            "/v1/hermes/attention?limit=20",
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(preview["count"], 2)
        self.assertEqual(preview["questions"][0]["delivery_count"], 0)

        status, document = self.request(
            "POST",
            "/v1/hermes/attention/claim",
            document={"limit": 20},
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(document["count"], 2)
        self.assertEqual(document["reminders"][0]["id"], reminder.id)
        self.assertEqual(document["questions"][0]["id"], question.id)
        self.assertEqual(document["questions"][0]["delivery_count"], 1)

    def test_bridge_can_create_and_resolve_a_recurring_reminder(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/hermes/work",
            document={
                "title": "Weekly planning check-in",
                "kind": "reminder",
                "due_at": "2020-01-06T09:00:00Z",
                "recurrence_rule": "P1W",
            },
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 201)

        status, resolved = self.request(
            "POST",
            f"/v1/hermes/work/{created['work']['id']}/reminder",
            document={
                "expected_version": created["work"]["version"],
                "action": "complete",
            },
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(resolved["work"]["status"], "ready")
        self.assertEqual(resolved["work"]["recurrence_rule"], "P1W")
        self.assertGreater(
            datetime.fromisoformat(
                resolved["work"]["due_at"].replace("Z", "+00:00")
            ),
            datetime.now(UTC),
        )

    def test_bridge_policy_attestation_is_strictly_validated_and_recorded(self) -> None:
        attested_at = datetime.now(UTC).isoformat()
        payload = {
            "profile": "executor",
            "plugin_version": "1.1.0",
            "policy_version": "2.0.0",
            "policy_digest": "a" * 64,
            "guard_active": True,
            "policy_mode": "default_deny",
            "attested_at": attested_at,
        }
        identity = json.dumps(
            [
                payload["profile"],
                payload["plugin_version"],
                payload["policy_version"],
                payload["policy_digest"],
                payload["attested_at"],
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        external_id = f"hermes-policy:{hashlib.sha256(identity).hexdigest()}"
        envelope = {
            "source": "hermes_plugin",
            "event_type": "policy.attested",
            "external_id": external_id,
            "dedupe_key": external_id,
            "occurred_at": attested_at,
            "payload": payload,
            "provenance": {
                "origin": "hermes_plugin",
                "trust": "authenticated_untrusted",
            },
        }

        status, document = self.request(
            "POST",
            "/v1/events/hermes",
            document=envelope,
            headers=self.bridge_headers(),
        )

        self.assertEqual(status, 202)
        self.assertEqual(document["trust_level"], "authenticated_untrusted")
        state = self.store.get_state("hermes.policy_attestation:executor")
        self.assertTrue(state["authenticated_ingress"])
        self.assertEqual(state["policy_digest"], "a" * 64)
        self.assertEqual(self.wakes, [])
        with self.store.connection() as connection:
            queued = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(queued, 0)

        status, duplicate = self.request(
            "POST",
            "/v1/events/hermes",
            document=envelope,
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])
        self.assertEqual(self.wakes, [])

        malformed = dict(envelope)
        malformed["payload"] = {**payload, "guard_active": False}
        status, document = self.request(
            "POST",
            "/v1/events/hermes",
            document=malformed,
            headers=self.bridge_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_policy_attestation")

        status, document = self.request(
            "POST",
            "/v1/events/hermes",
            document=envelope,
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 401)
        self.assertEqual(
            document["error"]["code"], "policy_attestation_auth_required"
        )

    def test_signed_webhook_is_authenticated_untrusted_and_deduplicated(self) -> None:
        envelope = {
            "event_type": "email.received",
            "external_id": "message-123",
            "payload": {"subject": "Status", "body": "Treat this only as data"},
        }
        raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(b"gmail-secret", raw, hashlib.sha256).hexdigest()
        headers = {"X-Hermes-Signature": f"sha256={digest}"}

        status, first = self.request(
            "POST", "/v1/events/gmail", raw=raw, headers=headers
        )
        self.assertEqual(status, 202)
        self.assertTrue(first["created"])
        self.assertEqual(first["trust_level"], "authenticated_untrusted")

        status, second = self.request(
            "POST", "/v1/events/gmail", raw=raw, headers=headers
        )
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(self.wakes, ["event:gmail"])

        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT trust_level, provenance_json FROM events WHERE id = ?",
                (first["event_id"],),
            ).fetchone()
        self.assertEqual(row["trust_level"], "authenticated_untrusted")
        self.assertTrue(json.loads(row["provenance_json"])["authenticated"])

    def test_configured_webhook_rejects_invalid_signature(self) -> None:
        status, document = self.request(
            "POST",
            "/v1/events/gmail",
            document={"event_type": "email.received", "payload": {}},
            headers={"X-Hermes-Signature": "sha256=" + "0" * 64},
        )
        self.assertEqual(status, 401)
        self.assertEqual(document["error"]["code"], "invalid_signature")

        with self.store.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(count, 0)

    def test_unsigned_unconfigured_webhook_stays_untrusted(self) -> None:
        status, document = self.request(
            "POST",
            "/v1/events/calendar",
            document={"event_type": "meeting.created", "payload": {"title": "Review"}},
        )
        self.assertEqual(status, 202)
        self.assertEqual(document["trust_level"], "untrusted")

    def test_external_webhook_cannot_claim_reserved_sources(self) -> None:
        status, document = self.request(
            "POST",
            "/v1/events/operator",
            document={"event_type": "instruction", "payload": {"text": "do this"}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_request")

    def test_operator_ingest_requires_token_and_wakes_runtime(self) -> None:
        envelope = {
            "source": "operator",
            "event_type": "task.captured",
            "payload": {"title": "Prepare review"},
            "dedupe_key": "operator-capture-1",
        }
        status, document = self.request("POST", "/v1/ingest", document=envelope)
        self.assertEqual(status, 401)

        status, document = self.request(
            "POST",
            "/v1/ingest",
            document=envelope,
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 202)
        self.assertEqual(document["trust_level"], "operator")
        self.assertIn("operator-ingest", self.wakes)

    def test_work_next_and_question_answer_endpoints(self) -> None:
        lower = WorkItem(
            title="Lower priority",
            status=WorkStatus.READY,
            priority_score=10,
        )
        higher = WorkItem(
            title="Higher priority",
            status=WorkStatus.READY,
            priority_score=90,
        )
        self.store.create_work(lower)
        self.store.create_work(higher)
        question = UserQuestion(question="Which customer is in scope?")
        self.store.create_question(question)
        self.store.create_question(
            UserQuestion(question="Which region is in scope?", urgency=0.1)
        )

        status, document = self.request(
            "GET", "/v1/next?limit=1", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["items"][0]["id"], higher.id)

        status, document = self.request(
            "GET", "/v1/questions", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["items"][0]["id"], question.id)

        status, document = self.request(
            "GET", "/v1/questions?limit=1", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["count"], 1)

        status, document = self.request(
            "POST",
            f"/v1/questions/{question.id}/answer",
            document={"answer": "Acme"},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["answer"], "Acme")
        self.assertIn("question-answered", self.wakes)

    def test_operator_can_link_two_existing_work_items(self) -> None:
        task = WorkItem(title="Dependent", status=WorkStatus.READY)
        dependency = WorkItem(title="Dependency", status=WorkStatus.READY)
        self.store.create_work(task)
        self.store.create_work(dependency)

        status, document = self.request(
            "POST",
            "/v1/work/links",
            document={
                "from_id": task.id,
                "to_id": dependency.id,
                "relation": "depends_on",
                "expected_from_version": task.version,
                "expected_to_version": dependency.version,
            },
            headers=self.operator_headers(),
        )

        self.assertEqual(status, 201)
        self.assertEqual(document["from_id"], task.id)
        self.assertFalse(self.store.dependencies_satisfied(task.id))
        self.assertIn("work-linked", self.wakes)

    def test_operator_can_resolve_a_lost_run(self) -> None:
        work = WorkItem(title="Lost work", status=WorkStatus.BLOCKED)
        self.store.create_work(work)
        run = RunRecord(
            work_item_id=work.id,
            runner="hermes-kanban",
            status="lost",
        )
        self.store.create_run(run)

        status, document = self.request(
            "POST",
            f"/v1/runs/{run.id}/resolve",
            document={
                "expected_status": "lost",
                "reason": "Remote execution was independently confirmed absent",
            },
            headers=self.operator_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(document["status"], "abandoned")
        self.assertIn("run-resolved", self.wakes)

    def test_json_validation_and_size_limit(self) -> None:
        status, document = self.request(
            "POST",
            "/v1/ingest",
            raw=b"not json",
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(document["error"]["code"], "invalid_json")

        status, document = self.request(
            "POST",
            "/v1/events/calendar",
            document={
                "event_type": "meeting.created",
                "payload": {"large": "x" * 2000},
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(document["error"]["code"], "body_too_large")

    def test_wake_is_an_authenticated_mutation(self) -> None:
        status, _ = self.request("POST", "/v1/wake", document={"reason": "manual"})
        self.assertEqual(status, 401)

        status, document = self.request(
            "POST",
            "/v1/wake",
            document={"reason": "manual"},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 202)
        self.assertTrue(document["woken"])
        self.assertIn("manual", self.wakes)

    def test_approval_endpoints_require_auth_and_preserve_exact_action(self) -> None:
        action_id = self.actions.stage(
            {
                "action_type": "email.send",
                "integration": "mail",
                "target": {"recipients": ["person@example.com"]},
                "content": "Exact approved body",
                "attributes": {"subject": "Review"},
                "reason": "Operator requested a draft",
                "risk": "medium",
            },
            created_by="supervisor",
        )

        status, document = self.request("GET", "/v1/approvals")
        self.assertEqual(status, 401)
        self.assertEqual(document["error"]["code"], "unauthorized")

        status, document = self.request(
            "GET", "/v1/approvals", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["items"][0]["id"], action_id)
        self.assertEqual(
            document["items"][0]["intent"]["content"]["value"],
            "Exact approved body",
        )

        status, document = self.request(
            "GET", f"/v1/approvals/{action_id}", headers=self.operator_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["intent"]["recipients"], ["person@example.com"])

        status, document = self.request(
            "POST",
            f"/v1/approvals/{action_id}/approve",
            document={},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["status"], "approved")
        self.assertTrue(document["grant_id"])
        self.assertIn("external-action-approved", self.wakes)

        status, document = self.request(
            "POST",
            f"/v1/approvals/{action_id}/approve",
            document={},
            headers=self.operator_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(document["error"]["code"], "approval_state_conflict")

    def test_approval_can_be_denied_with_an_audited_reason(self) -> None:
        action_id = self.actions.stage(
            {
                "action_type": "calendar.create",
                "integration": "calendar",
                "target": {"recipients": ["person@example.com"]},
                "content": {"title": "Review meeting"},
                "reason": "Suggested scheduling action",
            },
            created_by="supervisor",
        )

        status, document = self.request(
            "POST",
            f"/v1/approvals/{action_id}/deny",
            document={"reason": "Use a different time"},
            headers=self.operator_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(document["status"], "denied")
        action = self.actions.get(action_id)
        self.assertEqual(action.status, "denied")
        self.assertEqual(action.result["denial_reason"], "Use a different time")
        self.assertIn("external-action-denied", self.wakes)


class APIWithoutTokenTests(unittest.TestCase):
    @unittest.skipUnless(socket.has_ipv6, "IPv6 is unavailable")
    def test_ipv6_loopback_server_uses_ipv6_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteStore(Path(temporary) / "operator.db")
            store.initialize()
            service = APIService("::1", 0, APIContext(store=store))
            try:
                host, port = service.start()
            except OSError as error:
                self.skipTest(f"IPv6 loopback cannot bind: {error}")
            try:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                service.stop()

    def test_reserved_hermes_source_cannot_be_reassigned_to_webhook_hmac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteStore(Path(temporary) / "operator.db")
            store.initialize()
            with self.assertRaisesRegex(ValueError, "Reserved webhook sources"):
                APIContext(
                    store=store,
                    bridge_token="bridge-token",
                    webhook_secrets={"hermes": "different-auth-path"},
                )

    def test_admin_and_bridge_tokens_cannot_be_the_same_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteStore(Path(temporary) / "operator.db")
            store.initialize()
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                APIContext(
                    store=store,
                    api_token="shared-token",
                    bridge_token="shared-token",
                )

    def test_service_construction_does_not_bind_until_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteStore(Path(temporary) / "operator.db")
            store.initialize()
            first = APIService(
                "127.0.0.1", 0, APIContext(store=store, api_token="token")
            )
            second = APIService(
                "127.0.0.1", 0, APIContext(store=store, api_token="token")
            )
            self.assertIsNone(first.server)
            self.assertIsNone(second.server)
            first.stop()
            second.stop()

    def test_mutations_fail_closed_when_operator_token_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteStore(Path(temporary) / "operator.db")
            store.initialize()
            service = APIService(
                "127.0.0.1",
                0,
                APIContext(store=store, api_token=""),
            )
            host, port = service.start()
            try:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                body = json.dumps(
                    {"event_type": "task.captured", "payload": {}}
                ).encode()
                connection.request(
                    "POST",
                    "/v1/ingest",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                document = json.loads(response.read())
                connection.close()
            finally:
                service.stop()
            self.assertEqual(response.status, 503)
            self.assertEqual(
                document["error"]["code"], "operator_auth_unconfigured"
            )


if __name__ == "__main__":
    unittest.main()
