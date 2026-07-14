from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.adapters import InMemoryHermesAdapter
from hermes_operator.authority import execution_scope_binding
from hermes_operator.config import (
    AppConfig,
    HermesConfig,
    LLMConfig,
    ObsidianConfig,
    OperatorConfig,
    PolicyConfig,
    ServerConfig,
)
from hermes_operator.db import LeaseFenceLost, SQLiteStore, StateConflict
from hermes_operator.dispatcher import HermesDispatcher, dispatch_contract_digest
from hermes_operator.models import (
    ExecutionMode,
    RunRecord,
    UserQuestion,
    WorkItem,
    WorkRelation,
    WorkStatus,
    utc_now,
)


class MutableHermesAdapter(InMemoryHermesAdapter):
    def set_status(self, task_id: str, status: str) -> None:
        with self._lock:
            task = self._get_mutable(task_id)
            task.status = status
            task.updated_at = datetime.now(UTC).isoformat()


class ReservationProbeAdapter(MutableHermesAdapter):
    def __init__(
        self,
        store: SQLiteStore,
        work_id: str,
        dependency_id: str | None = None,
    ):
        super().__init__()
        self.store = store
        self.work_id = work_id
        self.dependency_id = dependency_id
        self.edit_was_blocked = False
        self.dependency_was_blocked = False

    def create_task(self, **kwargs):
        from hermes_operator.db import StateConflict

        try:
            self.store.update_work(
                self.work_id,
                {"title": "Changed after authorization"},
            )
        except StateConflict:
            self.edit_was_blocked = True
        if self.dependency_id:
            try:
                self.store.add_work_link(
                    self.work_id,
                    self.dependency_id,
                    WorkRelation.DEPENDS_ON,
                )
            except StateConflict:
                self.dependency_was_blocked = True
        return super().create_task(**kwargs)


class PausingHermesAdapter(MutableHermesAdapter):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def create_task(self, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test did not release Hermes creation")
        return super().create_task(**kwargs)


class LostCreateResponseAdapter(MutableHermesAdapter):
    """Create one idempotent card, then simulate a lost CLI response."""

    def __init__(self):
        super().__init__()
        self.lose_response = True

    def create_task(self, **kwargs):
        task = super().create_task(**kwargs)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("Hermes create response was lost")
        return task


class StopUnavailableAdapter(MutableHermesAdapter):
    def terminate_task(self, task_id: str):
        del task_id
        raise TimeoutError("native run-control API unavailable")


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.store = SQLiteStore(root / "operator.db")
        self.store.initialize()
        self.adapter = MutableHermesAdapter()
        self.config = self._config(root)

    @staticmethod
    def _config(root: Path, *, mode: str = "internal", parallel: int = 4) -> AppConfig:
        return AppConfig(
            config_path=root / "operator.toml",
            operator=OperatorConfig(
                instance_id="test",
                database_path=root / "operator.db",
                data_dir=root,
                autonomy_mode=mode,
                max_parallel_work=parallel,
            ),
            llm=LLMConfig(provider="command"),
            hermes=HermesConfig(
                enabled=True,
                default_assignee="executor",
                orchestrator_profile="operator",
                default_skills=["kanban-orchestrator"],
                require_policy_attestation=False,
            ),
            obsidian=ObsidianConfig(),
            server=ServerConfig(enabled=False),
            policy=PolicyConfig(),
        )

    def _ready(self, title: str = "Perform bounded work", **changes: object) -> WorkItem:
        values = {
            "title": title,
            "status": WorkStatus.READY,
            "execution_mode": ExecutionMode.HERMES,
            "assignee": "executor",
            "acceptance_criteria": ["The result is verified by a named check"],
            "metadata": {
                "governance": {"execution_authorized": True, "source_trust": "operator"},
                "dispatch_request": {"profile": "executor", "skills": []},
            },
        }
        values.update(changes)
        item = WorkItem(**values)
        requested_skills = list(
            item.metadata.get("dispatch_request", {}).get("skills", [])
        )
        item.metadata["dispatch_authorization"] = {
            "work_id": item.id,
            "profile": item.assignee,
            "skills": requested_skills,
            "issued_by": "operator-cli",
            "issued_at": "2026-07-13T00:00:00Z",
            "expires_at": "2099-07-13T00:00:00Z",
            "trust": "operator",
            "contract_digest": dispatch_contract_digest(
                item,
                profile=str(item.assignee),
                skills=requested_skills,
                default_skills=self.config.hermes.default_skills,
                goal_mode=bool(
                    item.metadata.get("dispatch_request", {}).get("goal_mode", False)
                ),
            ),
        }
        self.store.create_work(item)
        return item

    def _reauthorize(self, work_id: str, *, reason: str) -> WorkItem:
        item = self.store.get_work(work_id)
        metadata = dict(item.metadata)
        request = {
            "profile": str(item.assignee or "executor"),
            "skills": [],
            "goal_mode": False,
            "reason": reason,
            "shadow": False,
        }
        metadata["governance"] = {
            "execution_authorized": True,
            "source_trust": "operator",
        }
        metadata["dispatch_request"] = request
        metadata["dispatch_authorization"] = {
            "work_id": item.id,
            "profile": request["profile"],
            "skills": [],
            "issued_by": "operator-cli",
            "issued_at": utc_now(),
            "not_before": item.scheduled_at,
            "expires_at": None,
            "lifetime": "until_consumed_or_contract_change",
            "trust": "operator",
            "authorization_root": "a" * 64,
            "max_attempts": self.config.hermes.max_execution_attempts,
            "contract_digest": dispatch_contract_digest(
                item,
                profile=request["profile"],
                skills=[],
                default_skills=self.config.hermes.default_skills,
                goal_mode=False,
            ),
        }
        return self.store.update_work(
            item.id,
            {"status": WorkStatus.READY, "metadata": metadata},
            expected_version=item.version,
            allow_transition_override=True,
        )

    def _question_binding(self, item: WorkItem) -> dict[str, object]:
        request = item.metadata.get("dispatch_request", {})
        request = request if isinstance(request, dict) else {}
        skills = request.get("skills", [])
        skills = skills if isinstance(skills, list) else []
        return execution_scope_binding(
            item,
            profile=str(request.get("profile") or item.assignee or "executor"),
            skills=[str(value) for value in skills],
            default_skills=self.config.hermes.default_skills,
            goal_mode=bool(request.get("goal_mode", False)),
            execution_authorized=True,
        )

    def _dispatcher(self, config: AppConfig | None = None) -> HermesDispatcher:
        return HermesDispatcher(
            config=config or self.config,
            store=self.store,
            adapter=self.adapter,
        )

    def test_shadow_mode_never_creates_a_hermes_task(self) -> None:
        item = self._ready()
        config = self._config(Path(self.temporary_directory.name), mode="shadow")

        report = self._dispatcher(config).dispatch_ready()

        self.assertEqual(report.skipped_work_ids, [item.id])
        self.assertEqual(self.adapter.list_tasks(), [])
        self.assertEqual(self.store.list_active_runs(), [])
        self.assertEqual(self.store.get_work(item.id).status, WorkStatus.READY)

    def test_every_executable_work_mode_has_a_runner(self) -> None:
        self.assertEqual(
            {value.value for value in ExecutionMode},
            {"none", "hermes"},
        )

    def test_dispatch_creates_durable_card_link_and_running_run(self) -> None:
        parent = WorkItem(
            title="Parent project",
            status=WorkStatus.RUNNING,
            execution_mode=ExecutionMode.HERMES,
            hermes_task_id="t_parent",
        )
        self.store.create_work(parent)
        item = self._ready(
            description="Produce the implementation.",
            parent_id=parent.id,
            priority=8,
        )

        report = self._dispatcher().dispatch_ready()

        self.assertEqual(report.dispatched_work_ids, [item.id])
        linked = self.store.get_work(item.id)
        self.assertEqual(linked.status, WorkStatus.RUNNING)
        self.assertIsNotNone(linked.hermes_task_id)
        task = self.adapter.show_task(str(linked.hermes_task_id))
        self.assertIsNone(task.parent_id)
        self.assertEqual(task.assignee, "executor")
        self.assertIn("Acceptance criteria", task.description)
        self.assertIn("Do not send, publish, submit", task.description)
        self.assertIn("Do not start background subagents", task.description)
        self.assertEqual(
            task.raw["operator_metadata"]["skills"],
            ["kanban-orchestrator"],
        )
        self.assertEqual(task.raw["operator_metadata"]["operator_work_id"], item.id)
        self.assertEqual(
            task.raw["operator_metadata"]["operator_parent_work_id"],
            parent.id,
        )
        self.assertIn("organizational context, not a Hermes dependency", task.description)
        self.assertFalse(task.raw["operator_metadata"]["goal_mode"])
        active = self.store.list_active_runs()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["external_run_id"], task.id)
        self.assertEqual(active[0]["status"], "running")

    def test_worker_execution_contract_is_exact_and_live_only(self) -> None:
        item = self._ready("Bound native worker tools")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)

        contract = dispatcher.execution_contract(str(linked.hermes_task_id))

        self.assertTrue(contract["authorized"])
        self.assertEqual(contract["work_id"], item.id)
        self.assertNotIn("delegate_task", contract["internal_capabilities"])
        self.assertIn("local_test", contract["internal_capabilities"])
        self.assertEqual(
            dispatcher.execution_contract("t_unknown"),
            {"authorized": False, "task_id": "t_unknown"},
        )

        metadata = dict(linked.metadata)
        metadata["governance"] = {"execution_authorized": False}
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=linked.version,
            allow_transition_override=True,
        )
        revoked = dispatcher.execution_contract(str(linked.hermes_task_id))
        self.assertFalse(revoked["authorized"])

    def test_execution_contract_linearizes_before_concurrent_cancellation(self) -> None:
        item = self._ready("Linearize worker authorization")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)
        item_loaded = threading.Event()
        release_lookup = threading.Event()
        cancellation_started = threading.Event()
        cancellation_committed = threading.Event()
        contracts: list[dict[str, object]] = []
        errors: list[BaseException] = []
        original_lookup = self.store.find_work_by_hermes_id

        def paused_lookup(external_id: str) -> WorkItem | None:
            value = original_lookup(external_id)
            item_loaded.set()
            if not release_lookup.wait(timeout=3):
                raise TimeoutError("test did not release contract lookup")
            return value

        def read_contract() -> None:
            try:
                contracts.append(dispatcher.execution_contract(task_id))
            except BaseException as error:  # pragma: no cover - assertion path
                errors.append(error)

        def cancel_work() -> None:
            try:
                cancellation_started.set()
                current = self.store.get_work(item.id)
                self.store.update_work(
                    item.id,
                    {"status": WorkStatus.CANCELLED},
                    expected_version=current.version,
                    allow_transition_override=True,
                )
                cancellation_committed.set()
            except BaseException as error:  # pragma: no cover - assertion path
                errors.append(error)

        self.store.find_work_by_hermes_id = paused_lookup  # type: ignore[method-assign]
        self.addCleanup(
            setattr,
            self.store,
            "find_work_by_hermes_id",
            original_lookup,
        )
        reader = threading.Thread(target=read_contract)
        reader.start()
        self.assertTrue(item_loaded.wait(timeout=2))
        writer = threading.Thread(target=cancel_work)
        writer.start()
        self.assertTrue(cancellation_started.wait(timeout=2))

        # The cancellation writer cannot commit inside the capability read.
        self.assertFalse(cancellation_committed.wait(timeout=0.2))
        release_lookup.set()
        reader.join(timeout=3)
        writer.join(timeout=3)

        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(contracts[0]["authorized"])
        self.assertTrue(cancellation_committed.is_set())
        self.assertFalse(dispatcher.execution_contract(task_id)["authorized"])

    def test_running_work_rejects_new_dependency_and_contract_checks_legacy_edges(self) -> None:
        item = self._ready("Keep dependency invariant while running")
        dependency = WorkItem(
            title="Late unfinished dependency", status=WorkStatus.PLANNED
        )
        self.store.create_work(dependency)
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)

        with self.assertRaisesRegex(StateConflict, "active run"):
            self.store.add_work_link(
                item.id,
                dependency.id,
                WorkRelation.DEPENDS_ON,
                expected_from_version=linked.version,
                expected_to_version=dependency.version,
            )
        self.assertTrue(dispatcher.execution_contract(task_id)["authorized"])

        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO work_links(id, from_id, to_id, relation, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    "lnk_legacy_unmet_dependency",
                    item.id,
                    dependency.id,
                    WorkRelation.DEPENDS_ON.value,
                    utc_now(),
                ),
            )

        revoked = dispatcher.execution_contract(task_id)
        self.assertFalse(revoked["authorized"])
        self.assertEqual(revoked["reason"], "dependencies_not_satisfied")

    def test_execution_contract_fails_closed_after_leader_fence_loss(self) -> None:
        item = self._ready("Fence native worker authorization")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        task_id = str(self.store.get_work(item.id).hermes_task_id)
        calls = 0

        def stale_guard() -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise LeaseFenceLost("stale leader")

        stale_dispatcher = HermesDispatcher(
            config=self.config,
            store=self.store,
            adapter=self.adapter,
            leadership_guard=stale_guard,
        )

        with self.assertRaisesRegex(LeaseFenceLost, "stale leader"):
            stale_dispatcher.execution_contract(task_id)
        self.assertEqual(calls, 2)

    def test_background_delegation_is_not_advertised_or_claimable(self) -> None:
        item = self._ready("Keep work on the canonical card")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)

        claim = dispatcher.claim_delegation_batch(task_id, 3)

        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["reason"], "live_delegation_contract_required")
        self.assertIsNone(
            self.store.get_state(f"hermes.delegation_claim:{claim['run_id']}")
        )

    def test_dispatch_passes_native_goal_and_effective_skills(self) -> None:
        item = self._ready(
            "Parallel bounded goal",
            metadata={
                "governance": {
                    "execution_authorized": True,
                    "source_trust": "operator",
                },
                "dispatch_request": {
                    "profile": "executor",
                    "skills": ["kanban-orchestrator"],
                    "goal_mode": True,
                },
            },
        )

        report = self._dispatcher().dispatch_ready()

        self.assertEqual(report.dispatched_work_ids, [item.id])
        task = self.adapter.show_task(str(self.store.get_work(item.id).hermes_task_id))
        self.assertTrue(task.raw["operator_metadata"]["goal_mode"])
        self.assertEqual(
            task.raw["operator_metadata"]["skills"],
            ["kanban-orchestrator"],
        )

    def test_dispatch_cannot_commit_card_after_leader_fence_loss(self) -> None:
        item = self._ready("Fenced dispatch")
        calls = 0

        def guard() -> None:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise LeaseFenceLost("test leader fence lost")

        dispatcher = HermesDispatcher(
            config=self.config,
            store=self.store,
            adapter=self.adapter,
            leadership_guard=guard,
        )

        with self.assertRaises(LeaseFenceLost):
            dispatcher.dispatch_ready()

        self.assertEqual(len(self.adapter.list_tasks()), 1)
        stored = self.store.get_work(item.id)
        self.assertEqual(stored.status, WorkStatus.READY)
        self.assertIsNone(stored.hermes_task_id)
        active = self.store.list_active_runs()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "queued")

    def test_dispatch_requires_criteria_and_satisfied_dependencies(self) -> None:
        missing_criteria = self._ready("No definition of done", acceptance_criteria=[])
        dependency = WorkItem(title="Unfinished dependency", status=WorkStatus.PLANNED)
        self.store.create_work(dependency)
        blocked = self._ready("Depends on unfinished work")
        self.store.add_work_link(blocked.id, dependency.id, WorkRelation.DEPENDS_ON)

        report = self._dispatcher().dispatch_ready()

        self.assertEqual(
            set(report.skipped_work_ids),
            {missing_criteria.id, blocked.id},
        )
        self.assertEqual(self.adapter.list_tasks(), [])

    def test_dispatch_honors_global_parallelism_bound(self) -> None:
        root = Path(self.temporary_directory.name)
        running = WorkItem(
            title="Already running",
            status=WorkStatus.RUNNING,
            execution_mode=ExecutionMode.HERMES,
            acceptance_criteria=["Complete"],
        )
        self.store.create_work(running)
        self.store.create_run(
            RunRecord(
                work_item_id=running.id,
                runner="hermes-kanban",
                status="running",
            )
        )
        queued = self._ready("Wait for a slot")

        report = self._dispatcher(self._config(root, parallel=1)).dispatch_ready()

        self.assertIn(queued.id, report.skipped_work_ids)
        self.assertEqual(self.adapter.list_tasks(), [])

    def test_future_scheduled_work_does_not_consume_execution_capacity(self) -> None:
        future = self._ready(
            "Future reminder",
            priority=100,
            scheduled_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
        current = self._ready("Runnable now", priority=1)
        config = self._config(Path(self.temporary_directory.name), parallel=1)

        report = self._dispatcher(config).dispatch_ready()

        self.assertIn(future.id, report.skipped_work_ids)
        self.assertEqual(report.dispatched_work_ids, [current.id])
        self.assertEqual(self.store.get_work(future.id).status, WorkStatus.READY)
        with self.store.connection() as connection:
            future_runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE work_item_id = ?",
                (future.id,),
            ).fetchone()[0]
        self.assertEqual(future_runs, 0)

    def test_dispatch_requires_fresh_allowlisted_profile_policy_attestation(self) -> None:
        item = self._ready("Guarded candidate")
        config = self._config(Path(self.temporary_directory.name))
        config.hermes.require_policy_attestation = True
        config.hermes.policy_attestation_ttl_seconds = 300
        config.hermes.allowed_plugin_versions = ["1.1.0"]
        config.hermes.allowed_policy_versions = ["2.0.0"]
        config.hermes.allowed_policy_digests = ["a" * 64]
        dispatcher = self._dispatcher(config)

        missing = dispatcher.dispatch_ready()
        self.assertIn(item.id, missing.skipped_work_ids)
        self.assertEqual(self.adapter.list_tasks(), [])

        self.store.set_state(
            "hermes.policy_attestation:executor",
            {
                "profile": "executor",
                "plugin_version": "1.1.0",
                "policy_version": "2.0.0",
                "policy_digest": "a" * 64,
                "guard_active": True,
                "policy_mode": "default_deny",
                "attested_at": utc_now(),
                "received_at": utc_now(),
                "event_id": "evt-attested",
                "authenticated_ingress": True,
            },
        )

        accepted = dispatcher.dispatch_ready()

        self.assertEqual(accepted.dispatched_work_ids, [item.id])
        self.assertEqual(len(self.adapter.list_tasks()), 1)

    def test_dispatch_contract_is_frozen_until_card_link_is_atomic(self) -> None:
        item = self._ready("Original authorized title")
        dependency = WorkItem(
            title="Late unfinished dependency", status=WorkStatus.PLANNED
        )
        self.store.create_work(dependency)
        adapter = ReservationProbeAdapter(
            self.store, item.id, dependency.id
        )
        dispatcher = HermesDispatcher(
            config=self.config,
            store=self.store,
            adapter=adapter,
        )

        report = dispatcher.dispatch_ready()

        self.assertEqual(report.dispatched_work_ids, [item.id])
        self.assertTrue(adapter.edit_was_blocked)
        self.assertTrue(adapter.dependency_was_blocked)
        linked = self.store.get_work(item.id)
        self.assertEqual(linked.title, "Original authorized title")
        self.assertEqual(
            adapter.show_task(str(linked.hermes_task_id)).title,
            "Original authorized title",
        )

    def test_default_skill_change_invalidates_dispatch_authorization(self) -> None:
        item = self._ready("Authorized under the original defaults")
        changed_config = self._config(Path(self.temporary_directory.name))
        changed_config.hermes.default_skills.append("additional-default-skill")

        report = self._dispatcher(changed_config).dispatch_ready()

        self.assertIn(item.id, report.skipped_work_ids)
        self.assertEqual(self.adapter.list_tasks(), [])

    def test_shadow_issued_authorization_requires_live_reauthorization(self) -> None:
        item = self._ready("Authorized while shadowing")
        metadata = dict(item.metadata)
        request = dict(metadata["dispatch_request"])
        request["shadow"] = True
        metadata["dispatch_request"] = request
        authorization = dict(metadata["dispatch_authorization"])
        authorization["shadow"] = True
        metadata["dispatch_authorization"] = authorization
        self.store.update_work(
            item.id,
            {"metadata": metadata},
            expected_version=item.version,
        )

        report = self._dispatcher().dispatch_ready()

        self.assertIn(item.id, report.skipped_work_ids)
        self.assertEqual(self.adapter.list_tasks(), [])

    def test_parallel_dispatchers_share_one_atomic_capacity_limit(self) -> None:
        first = self._ready("First candidate")
        second = self._ready("Second candidate")
        adapter = PausingHermesAdapter()
        config = self._config(Path(self.temporary_directory.name), parallel=1)
        reports = []

        def dispatch() -> None:
            reports.append(
                HermesDispatcher(
                    config=config,
                    store=self.store,
                    adapter=adapter,
                ).dispatch_ready()
            )

        first_thread = threading.Thread(target=dispatch)
        first_thread.start()
        self.assertTrue(adapter.entered.wait(timeout=2))
        second_thread = threading.Thread(target=dispatch)
        second_thread.start()
        second_thread.join(timeout=2)
        adapter.release.set()
        first_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(adapter.list_tasks()), 1)
        self.assertEqual(len(self.store.list_active_runs()), 1)
        dispatched = [
            work_id
            for report in reports
            for work_id in report.dispatched_work_ids
        ]
        self.assertEqual(len(dispatched), 1)
        self.assertIn(dispatched[0], {first.id, second.id})

    def test_lost_create_response_keeps_capacity_and_recovers_idempotently(self) -> None:
        first = self._ready("Uncertain first create", priority=10)
        second = self._ready("Must not overtake uncertain create", priority=1)
        adapter = LostCreateResponseAdapter()
        config = self._config(Path(self.temporary_directory.name), parallel=1)
        dispatcher = HermesDispatcher(
            config=config,
            store=self.store,
            adapter=adapter,
        )

        initial = dispatcher.dispatch_ready()

        self.assertIn(first.id, initial.errors)
        self.assertIn(second.id, initial.skipped_work_ids)
        self.assertEqual(len(adapter.list_tasks()), 1)
        active = self.store.list_active_runs()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "queued")
        self.assertIsNone(active[0]["external_run_id"])

        recovered = dispatcher.reconcile()

        self.assertIn(first.id, recovered.reconciled_work_ids)
        linked = self.store.get_work(first.id)
        self.assertEqual(linked.status, WorkStatus.RUNNING)
        self.assertIsNotNone(linked.hermes_task_id)
        self.assertEqual(len(adapter.list_tasks()), 1)
        active = self.store.list_active_runs()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "running")
        self.assertEqual(active[0]["work_item_id"], first.id)

    def test_known_external_recovery_preserves_immutable_execution_contract(self) -> None:
        item = self._ready("Recover a persisted external task id")
        authorization = item.metadata["dispatch_authorization"]
        reserved = self.store.reserve_run_slot(
            item.id,
            runner="hermes-kanban",
            max_active=1,
            stale_queue_seconds=180,
            expected_work_version=item.version,
            contract_digest=authorization["contract_digest"],
        )
        self.assertIsNotNone(reserved)
        assert reserved is not None
        task = self.adapter.create_task(
            title=item.title,
            description="Card created before the local commit response was lost",
            idempotency_key=f"hermes-operator:{item.id}:attempt:1",
        )
        self.store.update_run(
            str(reserved["id"]),
            external_run_id=task.id,
            result=reserved["result"] | {"dispatch": {"id": task.id}},
        )

        report = self._dispatcher().reconcile()

        self.assertIn(item.id, report.reconciled_work_ids)
        recovered = self.store.get_run(str(reserved["id"]))
        contract = recovered["result"]["execution_contract"]
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(
            contract["dispatch_contract_digest"],
            authorization["contract_digest"],
        )
        self.assertEqual(
            contract["scope_revision"],
            item.authorization_scope_revision,
        )
        self.assertNotIn("contract_unavailable", contract)

    def test_orphan_recovery_revalidates_authorization_before_linking(self) -> None:
        item = self._ready("Recover only if still authorized")
        authorization = item.metadata["dispatch_authorization"]
        reserved = self.store.reserve_run_slot(
            item.id,
            runner="hermes-kanban",
            max_active=1,
            stale_queue_seconds=180,
            expected_work_version=item.version,
            contract_digest=authorization["contract_digest"],
        )
        self.assertIsNotNone(reserved)
        task = self.adapter.create_task(
            title=item.title,
            idempotency_key=f"hermes-operator:{item.id}",
        )
        self.store.update_run(
            reserved["id"],
            status="running",
            external_run_id=task.id,
            result=reserved["result"] | {"dispatch": {"id": task.id}},
        )
        current = self.store.get_work(item.id)
        metadata = dict(current.metadata)
        metadata.pop("dispatch_authorization")
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=current.version,
            allow_transition_override=True,
        )

        report = self._dispatcher().reconcile()

        recovered = self.store.get_work(item.id)
        self.assertEqual(recovered.status, WorkStatus.BLOCKED)
        self.assertIsNone(recovered.hermes_task_id)
        self.assertIn("orphaned_dispatch", recovered.metadata)
        self.assertIn(item.id, report.errors)
        active = self.store.list_active_runs()
        self.assertEqual(active, [])
        self.assertEqual(self.store.list_runs()[0]["status"], "cancelled")

    def test_repeated_dispatch_uses_existing_card_link(self) -> None:
        item = self._ready()
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)
        self.store.update_work(
            item.id,
            {"status": WorkStatus.READY},
            allow_transition_override=True,
        )

        dispatcher.dispatch_ready()

        self.assertEqual(len(self.adapter.list_tasks()), 1)
        self.assertEqual(self.store.get_work(item.id).hermes_task_id, task_id)

    def test_resolved_run_requires_fresh_dispatch_authorization(self) -> None:
        item = self._ready("Abandoned remote execution")
        run = RunRecord(
            work_item_id=item.id,
            runner="hermes-kanban",
            external_run_id="missing-task",
            status="lost",
        )
        self.store.create_run(run)
        self.store.resolve_run(
            run.id,
            expected_status="lost",
            reason="Remote execution was confirmed absent",
        )
        reset = self.store.get_work(item.id)
        self.store.update_work(
            item.id,
            {
                "status": WorkStatus.READY,
                "execution_mode": ExecutionMode.HERMES,
            },
            expected_version=reset.version,
            allow_transition_override=True,
        )

        report = self._dispatcher().dispatch_ready()

        self.assertIn(item.id, report.skipped_work_ids)
        self.assertEqual(self.adapter.list_tasks(), [])

    def test_completion_moves_to_review_and_enqueues_verification_once(self) -> None:
        item = self._ready()
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        self.adapter.set_status(str(linked.hermes_task_id), "done")

        report = dispatcher.reconcile()

        reviewed = self.store.get_work(item.id)
        self.assertEqual(reviewed.status, WorkStatus.REVIEW)
        self.assertNotEqual(reviewed.status, WorkStatus.DONE)
        self.assertEqual(report.reconciled_work_ids, [item.id])
        with self.store.connection() as connection:
            events = connection.execute(
                "SELECT event_type, trust_level, payload_json FROM events WHERE event_type = ?",
                ("execution.completed",),
            ).fetchall()
            runs = connection.execute(
                "SELECT status FROM runs WHERE work_item_id = ? ORDER BY attempt",
                (item.id,),
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trust_level"], "authenticated_untrusted")
        self.assertEqual(runs[-1]["status"], "completed")
        payload = json.loads(events[0]["payload_json"])
        run = self.store.list_runs()[0]
        contract = run["result"]["execution_contract"]
        self.assertEqual(
            payload["execution_scope_digest"],
            contract["execution_scope_digest"],
        )
        self.assertEqual(
            payload["dispatch_contract_digest"],
            contract["dispatch_contract_digest"],
        )
        self.assertIn("completion", run["result"])
        self.assertIn("reservation", run["result"])

        dispatcher.reconcile()
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = ?",
                ("execution.completed",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_completed_dispatch_authorization_cannot_launch_a_second_run(self) -> None:
        item = self._ready("One authorized execution")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        authorization = linked.metadata["dispatch_authorization"]
        self.assertEqual(
            authorization["consumed_external_run_id"],
            linked.hermes_task_id,
        )
        self.adapter.set_status(str(linked.hermes_task_id), "done")
        dispatcher.reconcile()
        reviewed = self.store.get_work(item.id)
        self.store.update_work(
            item.id,
            {"status": WorkStatus.READY},
            expected_version=reviewed.version,
            allow_transition_override=True,
            actor="operator-update-only",
        )

        report = dispatcher.dispatch_ready()

        self.assertIn(item.id, report.skipped_work_ids)
        self.assertEqual(len(self.adapter.list_tasks()), 1)
        self.assertEqual(self.store.list_active_runs(), [])
        with self.store.connection() as connection:
            attempts = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE work_item_id = ?",
                (item.id,),
            ).fetchone()[0]
        self.assertEqual(attempts, 1)

    def test_blocked_state_is_safe_and_does_not_become_waiting_input(self) -> None:
        item = self._ready()
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        self.adapter.set_status(str(linked.hermes_task_id), "blocked")

        report = dispatcher.reconcile()

        self.assertEqual(self.store.get_work(item.id).status, WorkStatus.BLOCKED)
        self.assertEqual(report.reconciled_work_ids, [item.id])
        active = self.store.list_active_runs()
        self.assertEqual(active, [])
        latest_run = self.store.list_runs()[0]
        self.assertEqual(latest_run["status"], "blocked")
        self.assertIsNotNone(latest_run["finished_at"])
        with self.store.connection() as connection:
            event_type = connection.execute(
                "SELECT event_type FROM events WHERE external_id = ?",
                (linked.hermes_task_id,),
            ).fetchone()[0]
        self.assertEqual(event_type, "execution.blocked")

    def test_out_of_band_unblock_cannot_reuse_consumed_authorization(self) -> None:
        item = self._ready("Do not restart without fresh authorization")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)
        self.adapter.set_status(task_id, "blocked")
        dispatcher.reconcile()
        contract = dispatcher.execution_contract(task_id)
        self.assertFalse(contract["authorized"])
        self.assertEqual(
            contract["reason"],
            "dispatch_authorization_run_not_active",
        )

        self.adapter.set_status(task_id, "running")
        report = dispatcher.reconcile()

        quarantined = self.store.get_work(item.id)
        self.assertEqual(quarantined.status, WorkStatus.BLOCKED)
        self.assertEqual(self.adapter.show_task(task_id).status, "blocked")
        self.assertEqual(self.store.list_active_runs(), [])
        self.assertIn(item.id, report.reconciled_work_ids)
        self.assertEqual(
            quarantined.metadata["execution_quarantine"]["reason"],
            "canonical_work_not_running:blocked",
        )

    def test_waiting_for_input_stops_native_compute_and_preserves_waiting_state(self) -> None:
        item = self._ready("Pause while the operator answers")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)
        self.adapter.set_status(task_id, "running")
        self.store.update_work(
            item.id,
            {"status": WorkStatus.WAITING_INPUT},
            expected_version=linked.version,
            allow_transition_override=True,
        )

        dispatcher.reconcile()

        waiting = self.store.get_work(item.id)
        self.assertEqual(waiting.status, WorkStatus.WAITING_INPUT)
        self.assertEqual(self.adapter.show_task(task_id).status, "blocked")
        self.assertEqual(self.store.list_active_runs(), [])
        self.assertEqual(
            waiting.metadata["execution_quarantine"]["reason"],
            "canonical_work_not_running:waiting_input",
        )

    def test_completed_run_evidence_is_immutable_after_verification_failure(self) -> None:
        item = self._ready("Do not replay changed completion evidence")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        task_id = str(linked.hermes_task_id)
        self.adapter.set_status(task_id, "done")
        dispatcher.reconcile()
        reviewed = self.store.get_work(item.id)
        metadata = dict(reviewed.metadata)
        metadata["last_verification"] = {
            "verdict": "failed",
            "summary": "The check failed",
        }
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=reviewed.version,
            allow_transition_override=True,
        )

        self.adapter.set_status(task_id, "done")
        dispatcher.reconcile()

        self.assertEqual(self.store.get_work(item.id).status, WorkStatus.BLOCKED)
        with self.store.connection() as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'execution.completed' "
                "AND external_id = ?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_answered_context_resumes_blocked_card_with_fresh_capacity(self) -> None:
        item = self._ready("Resume after operator context")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        first = self.store.get_work(item.id)
        task_id = str(first.hermes_task_id)
        self.adapter.set_status(task_id, "blocked")
        dispatcher.reconcile()
        question = UserQuestion(
            question="Which schema should the worker use?",
            blocking_work_ids=[item.id],
            blocking_work_bindings={
                item.id: self._question_binding(self.store.get_work(item.id))
            },
        )
        self.store.create_question(question)
        self.store.answer_question(question.id, "Use the 2026 schema")
        self._reauthorize(item.id, reason="Operator supplied the missing schema")

        report = dispatcher.dispatch_ready()

        self.assertIn(item.id, report.dispatched_work_ids)
        resumed = self.store.get_work(item.id)
        self.assertEqual(resumed.hermes_task_id, task_id)
        self.assertEqual(len(self.adapter.list_tasks()), 1)
        self.assertEqual(len(self.store.list_active_runs()), 1)
        runs = sorted(self.store.list_runs(), key=lambda value: value["attempt"])
        self.assertEqual([value["status"] for value in runs], ["blocked", "running"])
        comments = self.adapter.show_task(task_id).comments
        self.assertTrue(
            any("Use the 2026 schema" in str(value.get("body", "")) for value in comments)
        )

    def test_changed_execution_scope_creates_new_card_instead_of_resuming_old(self) -> None:
        item = self._ready(
            "Do scope-bound work",
            description="OLD scope description",
            acceptance_criteria=["OLD proof is present"],
        )
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        first = self.store.get_work(item.id)
        first_task_id = str(first.hermes_task_id)
        self.adapter.set_status(first_task_id, "blocked")
        dispatcher.reconcile()
        blocked = self.store.get_work(item.id)

        changed = self.store.update_work(
            item.id,
            {
                "description": "NEW scope description",
                "acceptance_criteria": ["NEW proof is present"],
            },
            expected_version=blocked.version,
        )
        self.assertGreater(
            changed.authorization_scope_revision,
            blocked.authorization_scope_revision,
        )
        self._reauthorize(item.id, reason="Authorize the NEW scope")

        report = dispatcher.dispatch_ready()

        self.assertIn(item.id, report.dispatched_work_ids)
        second = self.store.get_work(item.id)
        second_task_id = str(second.hermes_task_id)
        self.assertNotEqual(second_task_id, first_task_id)
        self.assertEqual(len(self.adapter.list_tasks()), 2)
        self.assertEqual(self.adapter.show_task(first_task_id).status, "blocked")
        second_description = self.adapter.show_task(second_task_id).description
        self.assertIn("NEW scope description", second_description)
        self.assertIn("NEW proof is present", second_description)
        self.assertNotIn("OLD scope description", second_description)

    def test_resume_comment_excludes_answers_bound_to_stale_scope(self) -> None:
        item = self._ready("Bind resume context to one scope")
        old_question = UserQuestion(
            question="What belongs to the old scope?",
            blocking_work_ids=[item.id],
            blocking_work_bindings={item.id: self._question_binding(item)},
        )
        self.store.create_question(old_question)
        self.store.answer_question(old_question.id, "STALE answer")
        changed = self.store.update_work(
            item.id,
            {"description": "The execution scope is now different"},
            expected_version=item.version,
        )
        current = self._reauthorize(
            changed.id,
            reason="Authorize the replacement scope",
        )
        current_question = UserQuestion(
            question="What belongs to the current scope?",
            blocking_work_ids=[item.id],
            blocking_work_bindings={item.id: self._question_binding(current)},
        )
        self.store.create_question(current_question)
        self.store.answer_question(current_question.id, "CURRENT answer")

        comment = self._dispatcher()._resume_comment(
            current,
            "run_current",
            "[resume marker]",
        )

        self.assertIn("CURRENT answer", comment)
        self.assertNotIn("STALE answer", comment)

    def test_failed_verification_retry_gets_distinct_card_and_event(self) -> None:
        item = self._ready("Retry a corrected implementation")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        first = self.store.get_work(item.id)
        first_task_id = str(first.hermes_task_id)
        self.adapter.set_status(first_task_id, "done")
        dispatcher.reconcile()
        reviewed = self.store.get_work(item.id)
        metadata = dict(reviewed.metadata)
        metadata["last_verification"] = {
            "verdict": "failed",
            "summary": "The named check failed",
        }
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=reviewed.version,
            allow_transition_override=True,
        )
        self._reauthorize(item.id, reason="Correct the failed named check")

        second_report = dispatcher.dispatch_ready()

        self.assertIn(item.id, second_report.dispatched_work_ids)
        second = self.store.get_work(item.id)
        second_task_id = str(second.hermes_task_id)
        self.assertNotEqual(second_task_id, first_task_id)
        self.assertEqual(len(self.adapter.list_tasks()), 2)
        self.adapter.set_status(second_task_id, "done")
        dispatcher.reconcile()
        with self.store.connection() as connection:
            events = connection.execute(
                "SELECT payload_json FROM events WHERE event_type = 'execution.completed' "
                "ORDER BY created_at, id"
            ).fetchall()
        self.assertEqual(len(events), 2)
        payloads = [json.loads(value["payload_json"]) for value in events]
        self.assertEqual({value["attempt"] for value in payloads}, {1, 2})
        self.assertEqual(len({value["run_id"] for value in payloads}), 2)

    def test_nonterminal_remote_execution_is_stopped_for_every_nonrunning_canonical_state(self) -> None:
        canonical_statuses = [
            WorkStatus.BLOCKED,
            WorkStatus.WAITING_INPUT,
            WorkStatus.REVIEW,
            WorkStatus.DONE,
            WorkStatus.CANCELLED,
            WorkStatus.ARCHIVED,
        ]
        items = [self._ready(f"Remote work for {status.value}") for status in canonical_statuses]
        dispatcher = self._dispatcher(
            self._config(
                Path(self.temporary_directory.name),
                parallel=len(canonical_statuses),
            )
        )
        dispatcher.dispatch_ready()

        for item, status in zip(items, canonical_statuses, strict=True):
            linked = self.store.get_work(item.id)
            active = next(
                run
                for run in self.store.list_active_runs()
                if run["work_item_id"] == item.id
            )
            self.store.update_run(
                active["id"],
                status="failed",
                finished_at=utc_now(),
            )
            self.store.update_work(
                item.id,
                {"status": status},
                expected_version=linked.version,
                allow_transition_override=True,
            )

        dispatcher.reconcile()

        active_by_work = {
            run["work_item_id"]: run for run in self.store.list_active_runs()
        }
        self.assertEqual(active_by_work, {})
        for item, status in zip(items, canonical_statuses, strict=True):
            self.assertEqual(self.store.get_work(item.id).status, status)
            self.assertEqual(
                self.adapter.show_task(
                    str(self.store.get_work(item.id).hermes_task_id)
                ).status,
                "blocked",
            )

    def test_reconciliation_cannot_restart_work_after_authorization_revocation(self) -> None:
        item = self._ready("Revocable work")
        dispatcher = self._dispatcher()
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        metadata = dict(linked.metadata)
        metadata["governance"] = {
            "execution_authorized": False,
            "source_trust": "operator",
        }
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=linked.version,
            allow_transition_override=True,
        )

        report = dispatcher.reconcile()

        quarantined = self.store.get_work(item.id)
        self.assertEqual(quarantined.status, WorkStatus.BLOCKED)
        self.assertEqual(
            quarantined.metadata["execution_quarantine"]["reason"],
            "canonical_work_not_running:blocked",
        )
        active = self.store.list_active_runs()
        self.assertEqual(active, [])
        with self.store.connection() as connection:
            latest_status = connection.execute(
                "SELECT status FROM runs WHERE work_item_id = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (item.id,),
            ).fetchone()["status"]
        self.assertEqual(latest_status, "cancelled")
        task = self.adapter.show_task(str(linked.hermes_task_id))
        self.assertEqual(task.status, "blocked")
        self.assertIn("authorization is no longer valid", task.raw["blocked_reason"])
        self.assertIn(item.id, report.reconciled_work_ids)

        candidate = self._ready("Use capacity after native stop acknowledgement")
        capacity_report = self._dispatcher(
            self._config(Path(self.temporary_directory.name), parallel=1)
        ).dispatch_ready()
        self.assertIn(candidate.id, capacity_report.dispatched_work_ids)

    def test_failed_native_stop_holds_capacity_fail_closed(self) -> None:
        adapter = StopUnavailableAdapter()
        dispatcher = HermesDispatcher(
            config=self._config(
                Path(self.temporary_directory.name),
                parallel=1,
            ),
            store=self.store,
            adapter=adapter,
        )
        item = self._ready("Stop must be acknowledged")
        dispatcher.dispatch_ready()
        linked = self.store.get_work(item.id)
        metadata = dict(linked.metadata)
        metadata["governance"] = {"execution_authorized": False}
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            expected_version=linked.version,
            allow_transition_override=True,
        )

        dispatcher.reconcile()

        active = self.store.list_active_runs()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "cancel_requested")
        candidate = self._ready("Cannot overtake an uncertain worker")
        report = dispatcher.dispatch_ready()
        self.assertIn(candidate.id, report.skipped_work_ids)

    def test_reconciliation_errors_are_isolated_per_card(self) -> None:
        missing = WorkItem(
            title="Missing external card",
            status=WorkStatus.RUNNING,
            execution_mode=ExecutionMode.HERMES,
            hermes_task_id="t_missing",
            acceptance_criteria=["Complete"],
        )
        self.store.create_work(missing)
        task = self.adapter.create_task(title="Valid card", idempotency_key="valid")
        valid = WorkItem(
            title="Valid linked work",
            status=WorkStatus.RUNNING,
            execution_mode=ExecutionMode.HERMES,
            assignee="executor",
            hermes_task_id=task.id,
            acceptance_criteria=["Complete"],
            metadata={
                "governance": {
                    "execution_authorized": True,
                    "source_trust": "operator",
                },
                "dispatch_request": {"profile": "executor", "skills": []},
            },
        )
        valid.metadata["dispatch_authorization"] = {
            "work_id": valid.id,
            "profile": "executor",
            "skills": [],
            "issued_by": "operator-cli",
            "issued_at": "2026-07-13T00:00:00Z",
            "expires_at": "2099-07-13T00:00:00Z",
            "trust": "operator",
            "contract_digest": dispatch_contract_digest(
                valid,
                profile="executor",
                skills=[],
                default_skills=self.config.hermes.default_skills,
                goal_mode=False,
            ),
        }
        self.store.create_work(valid)
        self.adapter.set_status(task.id, "done")

        report = self._dispatcher().reconcile()

        self.assertIn(missing.id, report.errors)
        self.assertIn(valid.id, report.reconciled_work_ids)
        self.assertEqual(self.store.get_work(valid.id).status, WorkStatus.REVIEW)
        with self.store.connection() as connection:
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event = ? AND entity_id = ?",
                ("dispatcher.reconcile_failed", missing.id),
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_stale_missing_run_stays_active_and_holds_capacity(self) -> None:
        missing = WorkItem(
            title="Lost external execution",
            status=WorkStatus.RUNNING,
            execution_mode=ExecutionMode.HERMES,
            hermes_task_id="t_gone",
            acceptance_criteria=["Complete"],
        )
        self.store.create_work(missing)
        self.store.create_run(
            RunRecord(
                work_item_id=missing.id,
                runner="hermes-kanban",
                external_run_id="t_gone",
                status="running",
                started_at="2000-01-01T00:00:00Z",
                heartbeat_at="2000-01-01T00:00:00Z",
            )
        )
        candidate = self._ready("Use released capacity")
        config = self._config(
            Path(self.temporary_directory.name), parallel=1
        )

        report = self._dispatcher(config).cycle()

        self.assertIn(missing.id, report.errors)
        self.assertIn(candidate.id, report.skipped_work_ids)
        self.assertNotIn(candidate.id, report.dispatched_work_ids)
        self.assertEqual(self.store.get_work(missing.id).status, WorkStatus.BLOCKED)
        with self.store.connection() as connection:
            run = connection.execute(
                "SELECT status, finished_at FROM runs WHERE work_item_id = ?",
                (missing.id,),
            ).fetchone()
        self.assertEqual(run["status"], "lost")
        self.assertIsNone(run["finished_at"])
        self.assertEqual(
            [value["work_item_id"] for value in self.store.list_active_runs()],
            [missing.id],
        )


if __name__ == "__main__":
    unittest.main()
