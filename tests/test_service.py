from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.config import (  # noqa: E402
    AppConfig,
    HermesConfig,
    LLMConfig,
    ObsidianConfig,
    OperatorConfig,
    PolicyConfig,
    ServerConfig,
)
from hermes_operator.db import LeaseFenceLost  # noqa: E402
from hermes_operator.llm import LLMResult, ScriptedLLM  # noqa: E402
from hermes_operator.models import (  # noqa: E402
    Event,
    TrustLevel,
    WorkItem,
    WorkRelation,
    WorkStatus,
)
from hermes_operator.service import OperatorService  # noqa: E402


class OperatorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        config = AppConfig(
            config_path=root / "operator.toml",
            operator=OperatorConfig(
                instance_id="service-test",
                database_path=root / "operator.db",
                data_dir=root,
                autonomy_mode="shadow",
            ),
            llm=LLMConfig(provider="command", command=[]),
            hermes=HermesConfig(enabled=False),
            obsidian=ObsidianConfig(enabled=False, discover=False),
            server=ServerConfig(enabled=False),
            policy=PolicyConfig(),
        )
        self.service = OperatorService(config)

    async def test_run_once_processes_live_event_and_exposes_next_work(self) -> None:
        event = Event(
            source="operator",
            external_id="capture-1",
            event_type="operator.request",
            payload={
                "title": "Prepare an internal analysis",
                "allow_internal_execution": True,
            },
            trust_level=TrustLevel.OPERATOR,
        )
        event_id, _ = self.service.store.enqueue_event(event)
        plan = {
            "summary": "Captured one clear task",
            "observations": [],
            "work_operations": [
                {
                    "op": "create",
                    "ref": "task",
                    "title": "Prepare an internal analysis",
                    "status": "ready",
                    "source_event_id": event_id,
                    "impact": 0.8,
                    "urgency": 0.6,
                    "acceptance_criteria": ["Analysis is supported by cited evidence"],
                }
            ],
            "questions": [],
            "dispatch": [],
            "memory_candidates": [],
            "verifications": [],
            "external_action_proposals": [],
        }
        scripted = ScriptedLLM([plan])
        self.service.llm = scripted
        self.service.supervisor.llm = scripted

        cycle = await self.service.run_once(force_reconcile=False)

        self.assertEqual(cycle.errors, {})
        self.assertIn("process_events", cycle.completed_components)
        self.assertEqual(len(scripted.calls), 1)
        version_before_read = self.service.store.list_work()[0].version
        next_items = self.service.next_work(1)
        self.assertEqual(len(next_items), 1)
        self.assertEqual(next_items[0].title, "Prepare an internal analysis")
        self.assertEqual(next_items[0].status, WorkStatus.READY)
        self.assertEqual(
            self.service.store.get_work(next_items[0].id).version,
            version_before_read,
        )
        with self.service.store.connection() as connection:
            state = connection.execute(
                "SELECT state FROM events WHERE id = ?", (event_id,)
            ).fetchone()["state"]
        self.assertEqual(state, "processed")
        health = self.service.health()
        self.assertEqual(health["cycle_count"], 1)
        self.assertEqual(health["autonomy_mode"], "shadow")
        self.assertFalse(health["hermes"]["enabled"])
        self.assertEqual(health["operational_counters"]["active_work"], 1)
        self.assertEqual(
            health["operational_counters"]["events"]["dead_letter"], 0
        )

        second = await self.service.run_once(force_reconcile=False)
        self.assertEqual(second.errors, {})
        self.assertEqual(len(scripted.calls), 1)

        independent = OperatorService(self.service.config)
        durable = independent.health()
        self.assertEqual(durable["cycle_count"], 2)
        self.assertEqual(durable["last_cycle"]["id"], second.id)
        self.assertFalse(durable["running"])

    async def test_independent_health_reads_live_durable_leader_lease(self) -> None:
        self.service._acquire_leader()
        try:
            independent = OperatorService(self.service.config)
            health = independent.health()
            self.assertTrue(health["running"])
            self.assertEqual(health["status"], "running")
            self.assertTrue(health["leader_lease"]["active"])
        finally:
            self.service.store.release_service_lease(
                self.service._leader_name,
                self.service._leader_owner,
                epoch=self.service._leader_epoch,
            )
            self.service._leader_held = False
            self.service._leader_epoch = None

    async def test_next_work_filters_dependencies_before_query_limit(self) -> None:
        blocker = WorkItem(
            title="Incomplete dependency",
            status=WorkStatus.READY,
            priority_score=1,
        )
        eligible = WorkItem(
            title="Eligible lower-ranked work",
            status=WorkStatus.READY,
            priority_score=10,
        )
        self.service.store.create_work(blocker)
        self.service.store.create_work(eligible)
        for index in range(6):
            blocked = WorkItem(
                title=f"Blocked high-ranked work {index}",
                status=WorkStatus.READY,
                priority_score=100 - index,
            )
            self.service.store.create_work(blocked)
            self.service.store.add_work_link(
                blocked.id,
                blocker.id,
                WorkRelation.DEPENDS_ON,
            )

        next_items = self.service.next_work(1)

        self.assertEqual([item.id for item in next_items], [eligible.id])

    async def test_eventless_reasoning_uses_durable_quiet_time_backoff(self) -> None:
        empty_plan = {
            "summary": "No portfolio changes needed",
            "observations": [],
            "event_dispositions": [],
            "work_operations": [],
            "questions": [],
            "dispatch": [],
            "memory_candidates": [],
            "verifications": [],
            "external_action_proposals": [],
        }
        scripted = ScriptedLLM([empty_plan])
        self.service.llm = scripted
        self.service.supervisor.llm = scripted

        first = await self.service.run_once(force_reconcile=True)
        second = await self.service.run_once(force_reconcile=True)

        self.assertEqual(first.errors, {})
        self.assertEqual(second.errors, {})
        self.assertEqual(len(scripted.calls), 1)
        state = self.service.store.get_state("supervisor.periodic_reasoning", {})
        self.assertIsInstance(state.get("completed_at_epoch"), float)

    async def test_inflight_plan_cannot_commit_after_leader_takeover(self) -> None:
        event = Event(
            source="operator",
            event_type="operator.request",
            payload={"request": "Create fenced work"},
            trust_level=TrustLevel.OPERATOR,
        )
        event_id, _ = self.service.store.enqueue_event(event)
        self.service._acquire_leader()

        class TakeoverLLM:
            async def generate_json(inner_self, *, system: str, user: str) -> LLMResult:
                del inner_self, system, user
                with self.service.store.connection() as connection:
                    connection.execute(
                        "UPDATE service_leases "
                        "SET expires_at = '2000-01-01T00:00:00Z' "
                        "WHERE name = ?",
                        (self.service._leader_name,),
                    )
                epoch = self.service.store.acquire_service_lease(
                    self.service._leader_name,
                    "replacement-owner",
                    ttl_seconds=60,
                )
                self.assertIsNotNone(epoch)
                plan = {
                    "summary": "Attempted stale work",
                    "observations": [],
                    "work_operations": [
                        {
                            "op": "create",
                            "ref": "stale",
                            "title": "Must not commit",
                            "source_event_id": event_id,
                        }
                    ],
                    "questions": [],
                    "dispatch": [],
                    "memory_candidates": [],
                    "verifications": [],
                    "external_action_proposals": [],
                }
                return LLMResult(
                    data=plan,
                    raw_text="{}",
                    usage={},
                    model="takeover-test",
                )

        self.service.supervisor.llm = TakeoverLLM()

        with self.assertRaises(LeaseFenceLost):
            await self.service.supervisor.run_pass(trigger="event")

        self.assertEqual(self.service.store.list_work(), [])

    async def test_stale_shutdown_does_not_overwrite_new_leader_state(self) -> None:
        await self.service._startup()
        with self.service.store.connection() as connection:
            connection.execute(
                "UPDATE service_leases SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE name = ?",
                (self.service._leader_name,),
            )
        replacement_epoch = self.service.store.acquire_service_lease(
            self.service._leader_name,
            "replacement-owner",
            ttl_seconds=60,
        )
        self.assertIsNotNone(replacement_epoch)
        replacement_state = {
            "instance_id": "replacement",
            "started": True,
            "leader_owner": "replacement-owner",
            "leader_epoch": replacement_epoch,
        }
        self.service.store.set_state("service", replacement_state)

        await self.service._shutdown()

        self.assertEqual(
            self.service.store.get_state("service", {}),
            replacement_state,
        )


if __name__ == "__main__":
    unittest.main()
