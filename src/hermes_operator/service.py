from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from dataclasses import asdict
from typing import Any

from .adapters import HermesCLIAdapter, ObsidianAdapter
from .api import APIContext, APIService
from .approvals import ExternalActionStager
from .config import AppConfig
from .connectors import CommandInboundConnector, ObsidianInboxReader
from .db import LeaseFenceLost, SQLiteStore, StateConflict
from .dispatcher import HermesDispatcher
from .llm import build_llm
from .models import WorkStatus, utc_now
from .prioritization import PriorityEngine
from .projector import KnowledgeProjector
from .runtime import AutonomousRuntime, CycleResult, RuntimeCallbacks
from .supervisor import Supervisor


logger = logging.getLogger(__name__)


class OperatorService:
    """Portable composition root for the autonomous operator control plane."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = SQLiteStore(config.operator.database_path)
        self.store.initialize()
        self._leader_name = "operator-control-plane"
        self._leader_owner = (
            f"{config.operator.instance_id}:{uuid.uuid4().hex}"
        )
        self._leader_ttl_seconds = max(
            config.operator.event_lease_seconds + 60,
            config.llm.timeout_seconds + 60,
            int(config.operator.tick_seconds * 3),
        )
        self._leader_held = False
        self._leader_epoch: int | None = None
        self._service_state_owned = False
        self._leader_heartbeat_task: asyncio.Task[None] | None = None
        self._leader_heartbeat_error: BaseException | None = None
        self.priority = PriorityEngine(
            max_contextual_adjustment=config.policy.max_llm_priority_adjustment
        )
        self.actions = ExternalActionStager(
            self.store,
            ttl_seconds=config.policy.approval_ttl_seconds,
            actor_id=f"hermes-operator:{config.operator.instance_id}",
        )
        self.llm = build_llm(config.llm)
        self.supervisor = Supervisor(
            config=config,
            store=self.store,
            llm=self.llm,
            priority_engine=self.priority,
            action_stager=self.actions,
            leadership_guard=self._assert_leader,
        )

        self.hermes = None
        self.dispatcher = None
        if config.hermes.enabled:
            self.hermes = HermesCLIAdapter(
                binary=config.hermes.binary,
                profile=config.hermes.profile,
                board=config.hermes.board,
                timeout_seconds=config.hermes.command_timeout_seconds,
                env={
                    name: os.environ[name]
                    for name in config.hermes.pass_env
                    if name in os.environ
                },
                control_base_url=config.hermes.control_base_url,
                control_token=config.hermes.resolved_control_token(),
                control_timeout_seconds=config.hermes.control_timeout_seconds,
            )
            self.dispatcher = HermesDispatcher(
                config=config,
                store=self.store,
                adapter=self.hermes,
                should_stop=lambda: self.runtime.stop_requested,
                leadership_guard=self._assert_leader,
            )

        environment_vault = os.environ.get("HERMES_OPERATOR_VAULT", "").strip()
        configured_vault = environment_vault or config.obsidian.vault_path
        obsidian_enabled = config.obsidian.enabled or bool(environment_vault)
        self.obsidian = ObsidianAdapter(
            configured_vault if obsidian_enabled else None,
            env=None if obsidian_enabled else {},
            env_keys=() if not obsidian_enabled else ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_VAULT"),
            include_default_candidates=bool(obsidian_enabled and config.obsidian.discover),
        )
        self.projector = KnowledgeProjector(
            store=self.store,
            obsidian=self.obsidian,
            priority_engine=self.priority,
            operator_root=config.obsidian.operator_root,
            actions=self.actions,
        )
        self.inbound_connectors = [
            CommandInboundConnector(
                connector_config,
                self.store,
                leadership_guard=self._assert_leader,
            )
            for connector_config in config.inbound_connectors
        ]
        self.obsidian_inbox = ObsidianInboxReader(
            self.obsidian,
            self.store,
            operator_root=config.obsidian.operator_root,
            leadership_guard=self._assert_leader,
        )

        callbacks = RuntimeCallbacks(
            startup=self._startup,
            shutdown=self._shutdown,
            observe=self._observe_inbound,
            reconcile=self._reconcile,
            process_events=self._process_events,
            dispatch=self._dispatch,
            project=self._project,
        )
        self.runtime = AutonomousRuntime(
            callbacks,
            tick_seconds=config.operator.tick_seconds,
            reconciliation_seconds=config.operator.reconciliation_seconds,
            on_error=self._record_error,
            on_cycle=self._record_cycle,
        )
        self.api: APIService | None = None
        if config.server.enabled:
            context = APIContext(
                store=self.store,
                api_token=config.server.resolved_api_token(),
                bridge_token=config.server.resolved_bridge_token(),
                bridge_proof_secret=(
                    config.server.resolved_bridge_proof_secret()
                ),
                webhook_secrets=config.server.webhook_secrets,
                allow_unsigned_webhooks=config.server.allow_unsigned_webhooks,
                max_body_bytes=config.server.max_body_bytes,
                wake=self.runtime.wake,
                health_provider=self.health,
                next_provider=self.next_work,
                execution_contract_provider=(
                    self.dispatcher.execution_contract
                    if self.dispatcher is not None
                    else None
                ),
                delegation_claim_provider=(
                    self.dispatcher.claim_delegation_batch
                    if self.dispatcher is not None
                    else None
                ),
                action_stager=self.actions,
                attention_redelivery_seconds=(
                    config.native_automation.attention_redelivery_seconds
                ),
                authorization_profile=(
                    config.hermes.default_assignee or config.hermes.profile
                ),
                authorization_default_skills=tuple(
                    config.hermes.default_skills
                ),
                authorization_goal_mode=config.hermes.goal_mode,
                verification_config=config.verification,
            )
            self.api = APIService(config.server.host, config.server.port, context)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(name, self.runtime.stop)
                installed.append(name)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await self.runtime.run()
        finally:
            for name in installed:
                loop.remove_signal_handler(name)

    async def run_once(self, *, force_reconcile: bool = True):
        acquired_here = False
        if not self._leader_held:
            self._acquire_leader()
            acquired_here = True
            self._start_leader_heartbeat()
        try:
            return await self.runtime.run_once(
                force_reconcile=force_reconcile, reason="operator-run-once"
            )
        finally:
            if acquired_here:
                await self._stop_leader_heartbeat()
                self.store.release_service_lease(
                    self._leader_name,
                    self._leader_owner,
                    epoch=self._leader_epoch,
                )
                self._leader_held = False
                self._leader_epoch = None

    async def _startup(self) -> None:
        self._acquire_leader()
        self._start_leader_heartbeat()
        if self.api is not None:
            address = await asyncio.to_thread(self.api.start)
            self.store.set_state(
                "api.address", {"host": address[0], "port": address[1]}
            )
        with self.store.transaction():
            self._assert_leader()
            self.store.set_state(
                "service",
                {
                    "instance_id": self.config.operator.instance_id,
                    "autonomy_mode": self.config.operator.autonomy_mode,
                    "started": True,
                    "started_at": utc_now(),
                    "leader_owner": self._leader_owner,
                    "leader_epoch": self._leader_epoch,
                },
            )
            self._service_state_owned = True

    async def _shutdown(self) -> None:
        try:
            if self.api is not None:
                await asyncio.to_thread(self.api.stop)
            if self._service_state_owned and self._leader_held:
                try:
                    with self.store.transaction():
                        self._assert_leader()
                        state = self.store.get_state("service", {})
                        if (
                            isinstance(state, dict)
                            and state.get("leader_owner") == self._leader_owner
                            and state.get("leader_epoch") == self._leader_epoch
                        ):
                            self.store.set_state(
                                "service",
                                {
                                    **state,
                                    "started": False,
                                    "stopped_at": utc_now(),
                                },
                            )
                except StateConflict:
                    logger.warning(
                        "Skipped shutdown state update after losing the leader fence"
                    )
        finally:
            self._service_state_owned = False
            await self._stop_leader_heartbeat()
            if self._leader_held:
                self.store.release_service_lease(
                    self._leader_name,
                    self._leader_owner,
                    epoch=self._leader_epoch,
                )
                self._leader_held = False
            self._leader_epoch = None

    async def _reconcile(self) -> dict[str, Any]:
        self._renew_leader()
        dispatch_result: dict[str, Any] = {}
        if self.dispatcher is not None:
            adapter_health = await asyncio.to_thread(self.hermes.health)
            with self.store.transaction():
                self._assert_leader()
                self.store.set_state("hermes.health", asdict(adapter_health))
            report = await asyncio.to_thread(self.dispatcher.reconcile)
            dispatch_result = report.to_dict()
        pass_result = None
        reasoning_due = self._periodic_reasoning_due()
        if not self.store.has_pending_events() and reasoning_due:
            pass_result = await self.supervisor.run_pass(
                trigger="reconciliation", force_without_events=True
            )
            with self.store.transaction():
                self._assert_leader()
                self.store.set_state(
                    "supervisor.periodic_reasoning",
                    {"completed_at_epoch": time.time()},
                )
        return {
            "hermes": dispatch_result,
            "supervisor": pass_result.to_dict() if pass_result else None,
            "reasoning_due": reasoning_due,
        }

    def _periodic_reasoning_due(self) -> bool:
        state = self.store.get_state("supervisor.periodic_reasoning", {})
        raw = state.get("completed_at_epoch") if isinstance(state, dict) else None
        try:
            completed_at = float(raw)
        except (TypeError, ValueError):
            return True
        elapsed = time.time() - completed_at
        return elapsed < 0 or elapsed >= self.config.operator.reasoning_refresh_seconds

    async def _observe_inbound(self) -> dict[str, Any]:
        """Poll independent read-only sources without coupling their failures."""

        self._renew_leader()
        readers: list[tuple[str, Any]] = [
            (connector.config.name, connector)
            for connector in self.inbound_connectors
        ]
        readers.append((self.obsidian_inbox.name, self.obsidian_inbox))
        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(reader.poll)
                for _, reader in readers
            ),
            return_exceptions=True,
        )
        previous_health = self.store.get_state("inbound.health", {})
        previous_errors = (
            previous_health.get("errors", {})
            if isinstance(previous_health, dict)
            else {}
        )
        if not isinstance(previous_errors, dict):
            previous_errors = {}
        reader_names = {name for name, _ in readers}
        reports: dict[str, Any] = {}
        errors: dict[str, str] = {
            str(name): str(error)
            for name, error in previous_errors.items()
            if name in reader_names
        }
        failures: dict[str, str] = {}
        for (name, reader), outcome in zip(readers, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures[name] = (
                    f"{type(outcome).__name__}: {outcome}"
                )[:2000]
                errors[name] = failures[name]
                logger.warning(
                    "Inbound reader %s failed: %s",
                    name,
                    errors[name],
                )
            else:
                reports[name] = asdict(outcome)
                disabled = (
                    name == self.obsidian_inbox.name
                    and not self.obsidian.enabled
                ) or not getattr(
                    getattr(reader, "config", None),
                    "enabled",
                    True,
                )
                if outcome.polled or disabled:
                    errors.pop(name, None)

        with self.store.transaction():
            self._assert_leader()
            self.store.set_state(
                "inbound.health",
                {
                    "readers": reports,
                    "errors": errors,
                },
            )
            for name, error in failures.items():
                self.store.audit(
                    "runtime",
                    "connector.poll_failed",
                    entity_type="connector",
                    entity_id=name,
                    data={"error": error},
                )
        return {"readers": reports, "errors": errors}

    async def _process_events(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for _ in range(10):
            self._renew_leader()
            result = await self.supervisor.run_pass(trigger="event")
            if result is None:
                break
            results.append(result.to_dict())
        return results

    async def _dispatch(self) -> dict[str, Any]:
        self._renew_leader()
        if self.dispatcher is None:
            return {}
        # Reconcile again immediately after the supervisor pass. A question or
        # operator update can move running work into waiting or blocked state in
        # this same cycle, and native compute must be stopped without waiting for
        # the slower recovery interval.
        report = await asyncio.to_thread(self.dispatcher.cycle)
        return report.to_dict()

    async def _project(self) -> dict[str, Any]:
        self._renew_leader()
        summary = await asyncio.to_thread(self.projector.project)
        return asdict(summary)

    def _acquire_leader(self) -> None:
        epoch = self.store.acquire_service_lease(
            self._leader_name,
            self._leader_owner,
            ttl_seconds=self._leader_ttl_seconds,
        )
        if epoch is None:
            raise StateConflict(
                "Another Hermes Operator instance owns the control-plane lease"
            )
        self._leader_held = True
        self._leader_epoch = epoch
        self._leader_heartbeat_error = None

    def _renew_leader(self) -> None:
        if not self._leader_held or not self.store.renew_service_lease(
            self._leader_name,
            self._leader_owner,
            ttl_seconds=self._leader_ttl_seconds,
            epoch=self._leader_epoch,
        ):
            self._leader_held = False
            raise StateConflict("Hermes Operator lost the control-plane lease")

    def _assert_leader(self) -> None:
        if not self._leader_held or self._leader_epoch is None:
            raise LeaseFenceLost(
                "Hermes Operator does not own the control-plane lease"
            )
        self.store.assert_service_lease(
            self._leader_name,
            self._leader_owner,
            self._leader_epoch,
        )

    def _start_leader_heartbeat(self) -> None:
        task = self._leader_heartbeat_task
        if task is not None and not task.done():
            return
        self._leader_heartbeat_task = asyncio.create_task(
            self._leader_heartbeat_loop(),
            name="hermes-operator-leader-heartbeat",
        )

    async def _stop_leader_heartbeat(self) -> None:
        task = self._leader_heartbeat_task
        self._leader_heartbeat_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _leader_heartbeat_loop(self) -> None:
        interval = max(1.0, min(30.0, self._leader_ttl_seconds / 3.0))
        while self._leader_held:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if not self._leader_held:
                return
            try:
                await asyncio.to_thread(self._renew_leader)
            except Exception as error:
                self._leader_heartbeat_error = error
                self.runtime.stop()
                return

    def next_work(self, limit: int = 5):
        items = self.store.list_work(
            statuses=[
                WorkStatus.TRIAGE,
                WorkStatus.READY,
                WorkStatus.REVIEW,
                WorkStatus.RUNNING,
            ],
            dependencies_satisfied_only=True,
            running_bypasses_dependencies=True,
            limit=max(limit * 5, limit),
        )
        return self.priority.next_best(items, limit=limit, include_running=True)

    def health(self) -> dict[str, Any]:
        local = self.runtime.health()
        lease = self.store.get_service_lease(self._leader_name)
        durable = self.store.get_state("runtime.status", {})
        service_state = self.store.get_state("service", {})
        if not isinstance(durable, dict):
            durable = {}
        if not isinstance(service_state, dict):
            service_state = {}
        running = bool(lease and lease.get("active"))
        last_cycle = local.get("last_cycle") or durable.get("last_cycle")
        degraded = bool(
            isinstance(last_cycle, dict) and last_cycle.get("errors")
        )
        result = {
            "status": "degraded" if degraded else ("running" if running else "stopped"),
            "running": running,
            "stop_requested": (
                local.get("stop_requested")
                if local.get("running")
                else False
            ),
            "cycle_count": max(
                int(local.get("cycle_count", 0)),
                int(durable.get("cycle_count", 0)),
            ),
            "started_at": service_state.get("started_at") or local.get("started_at"),
            "stopped_at": service_state.get("stopped_at") or local.get("stopped_at"),
            "last_cycle": last_cycle,
            "leader_lease": lease,
        }
        result.update(
            {
                "instance_id": self.config.operator.instance_id,
                "autonomy_mode": self.config.operator.autonomy_mode,
                "external_action_mode": self.config.policy.external_action_mode,
                "database": str(self.store.path),
                "operational_counters": self.store.operational_counters(),
                "pending_approvals": len(
                    self.actions.list(status="pending_approval", limit=1000)
                ),
                "hermes": (
                    self.store.get_state(
                        "hermes.health",
                        {"enabled": True, "available": False, "detail": "Not checked yet"},
                    )
                    if self.hermes is not None
                    else {"enabled": False, "available": False}
                ),
                "inbound": self.store.get_state(
                    "inbound.health",
                    {"readers": {}, "errors": {}},
                ),
                "obsidian": asdict(self.obsidian.health()),
            }
        )
        return result

    def _record_cycle(self, result: CycleResult) -> None:
        """Persist cycle health so another process can report truthful status."""

        previous = self.store.get_state("runtime.status", {})
        prior_count = (
            int(previous.get("cycle_count", 0))
            if isinstance(previous, dict)
            else 0
        )
        with self.store.transaction():
            self._assert_leader()
            self.store.set_state(
                "runtime.status",
                {
                    "cycle_count": prior_count + 1,
                    "last_cycle": result.to_dict(),
                    "leader_owner": self._leader_owner,
                    "leader_epoch": self._leader_epoch,
                    "updated_at": utc_now(),
                },
            )

    def _record_error(self, component: str, error: BaseException) -> None:
        logger.exception("Autonomy component %s failed", component, exc_info=error)
        try:
            self.store.audit(
                "runtime",
                "component.failed",
                entity_type="component",
                entity_id=component,
                data={"error_type": type(error).__name__, "error": str(error)[:2000]},
            )
        except Exception:
            logger.exception("Could not persist component failure")
