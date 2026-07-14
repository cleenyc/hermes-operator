"""Durable Hermes Kanban dispatch and reconciliation.

The operator database owns work intent and governance. Hermes owns execution
state after dispatch. This module links the two systems without importing any
Hermes implementation details, so the control plane can run in another Python
environment, container, or host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from .adapters.base import HermesAdapter, HermesTask
from .config import AppConfig
from .db import LeaseFenceLost, NotFound, SQLiteStore, StateConflict
from .models import (
    Event,
    ExecutionMode,
    RunRecord,
    TERMINAL_WORK_STATUSES,
    TrustLevel,
    WorkItem,
    WorkStatus,
    utc_now,
)
from .verifier import ArtifactVerifier


logger = logging.getLogger(__name__)


_COMPLETE_STATES = {"complete", "completed", "done", "closed", "resolved", "success"}
_BLOCKED_STATES = {"blocked", "stalled", "waiting", "waiting_input", "needs_input"}
_RUNNING_STATES = {
    "active",
    "doing",
    "executing",
    "in_progress",
    "inprogress",
    "running",
    "started",
    "working",
}
_READY_STATES = {"backlog", "open", "planned", "ready", "todo", "triage", "queued"}
_CANCELLED_STATES = {"archived", "canceled", "cancelled", "discarded"}


def dispatch_contract_digest(
    item: WorkItem,
    *,
    profile: str,
    skills: list[str],
    default_skills: list[str],
    goal_mode: bool,
) -> str:
    effective_skills = sorted(
        {
            normalized
            for value in [*default_skills, *skills]
            if (normalized := str(value).strip())
        }
    )
    payload = {
        "work_id": item.id,
        "kind": item.kind.value,
        "title": item.title,
        "description": item.description,
        "parent_id": item.parent_id,
        "acceptance_criteria": list(item.acceptance_criteria),
        "due_at": item.due_at,
        "scheduled_at": item.scheduled_at,
        "recurrence_rule": item.recurrence_rule,
        "priority": item.priority,
        "profile": profile,
        "effective_skills": effective_skills,
        "goal_mode": bool(goal_mode),
        "verification_contract": item.metadata.get("verification_contract"),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class DispatchReport:
    """Summary of one dispatcher operation."""

    dispatched_work_ids: list[str] = field(default_factory=list)
    reconciled_work_ids: list[str] = field(default_factory=list)
    skipped_work_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def merge(self, other: "DispatchReport") -> "DispatchReport":
        self.dispatched_work_ids.extend(other.dispatched_work_ids)
        self.reconciled_work_ids.extend(other.reconciled_work_ids)
        self.skipped_work_ids.extend(other.skipped_work_ids)
        self.event_ids.extend(other.event_ids)
        self.errors.update(other.errors)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatched_work_ids": self.dispatched_work_ids,
            "reconciled_work_ids": self.reconciled_work_ids,
            "skipped_work_ids": self.skipped_work_ids,
            "event_ids": self.event_ids,
            "errors": self.errors,
        }


class HermesDispatcher:
    """Create durable Hermes cards and reconcile their execution state.

    ``shadow`` mode never creates or mutates Hermes tasks. ``internal`` and
    ``active`` modes may dispatch execution work, but neither mode authorizes
    external-facing communication. That boundary is enforced separately by
    the approval broker.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        adapter: HermesAdapter,
        should_stop: Callable[[], bool] | None = None,
        leadership_guard: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.adapter = adapter
        self.should_stop = should_stop or (lambda: False)
        self.leadership_guard = leadership_guard or (lambda: None)
        self.actor = f"dispatcher:{config.operator.instance_id}"
        self.verifier = ArtifactVerifier(config.verification)

    def cycle(self) -> DispatchReport:
        """Reconcile linked cards, then fill available execution capacity."""

        report = self.reconcile()
        report.merge(self.dispatch_ready())
        with self.store.transaction():
            self.leadership_guard()
            self.store.set_state("dispatcher.last_cycle", report.to_dict())
        return report

    def execution_contract(self, task_id: str) -> dict[str, Any]:
        """Return the narrow live authorization used by the native worker guard."""

        self.leadership_guard()
        item = self.store.find_work_by_hermes_id(task_id)
        if item is None:
            return {"authorized": False, "task_id": task_id}
        authorized, reason = self._dispatch_authorized(
            item,
            ongoing_external_run_id=task_id,
        )
        authorization = item.metadata.get("dispatch_authorization", {})
        run_id = (
            str(authorization.get("consumed_run_id", ""))
            if isinstance(authorization, Mapping)
            else ""
        )
        run = None
        if run_id:
            try:
                run = self.store.get_run(run_id)
            except NotFound:
                run = None
        live = bool(
            authorized
            and item.status == WorkStatus.RUNNING
            and run
            and run.get("work_item_id") == item.id
            and run.get("external_run_id") == task_id
            and run.get("status") == "running"
        )
        if not live:
            return {
                "authorized": False,
                "task_id": task_id,
                "reason": reason if not authorized else "canonical_run_not_active",
            }
        self.leadership_guard()
        return {
            "authorized": True,
            "task_id": task_id,
            "work_id": item.id,
            "profile": str(item.assignee or ""),
            "contract_digest": str(authorization.get("contract_digest", "")),
            "run_id": run_id,
            "internal_capabilities": [
                "delegate_task",
                "local_read",
                "local_write",
                "local_test",
                "local_build",
            ],
        }

    def claim_delegation_batch(
        self,
        task_id: str,
        requested_children: int,
    ) -> dict[str, Any]:
        """Atomically consume the one bounded subagent batch for a live run."""

        if (
            not isinstance(requested_children, int)
            or isinstance(requested_children, bool)
            or not 1 <= requested_children <= 3
        ):
            raise ValueError("requested_children must be an integer from 1 through 3")
        with self.store.transaction() as connection:
            self.leadership_guard()
            contract = self.execution_contract(task_id)
            run_id = str(contract.get("run_id", ""))
            contract_digest = str(contract.get("contract_digest", ""))
            base = {
                "task_id": task_id,
                "run_id": run_id,
                "contract_digest": contract_digest,
                "requested_children": requested_children,
            }
            if contract.get("authorized") is not True or (
                "delegate_task"
                not in contract.get("internal_capabilities", [])
            ):
                return {
                    **base,
                    "claimed": False,
                    "reason": "live_delegation_contract_required",
                }
            state_key = f"hermes.delegation_claim:{run_id}"
            existing = connection.execute(
                "SELECT 1 FROM system_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if existing is not None:
                return {
                    **base,
                    "claimed": False,
                    "reason": "delegation_batch_already_claimed",
                }
            claimed_at = utc_now()
            value = {
                **base,
                "work_id": contract["work_id"],
                "profile": contract["profile"],
                "claimed_at": claimed_at,
            }
            connection.execute(
                "INSERT INTO system_state(key, value_json, updated_at) VALUES(?, ?, ?)",
                (
                    state_key,
                    json.dumps(
                        value,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    claimed_at,
                ),
            )
            self.store.audit(
                self.actor,
                "execution.delegation_batch_claimed",
                entity_type="run",
                entity_id=run_id,
                data=value,
                connection=connection,
            )
            return {
                **base,
                "claimed": True,
                "reason": "claimed",
            }

    def dispatch_ready(self) -> DispatchReport:
        """Dispatch eligible ready work up to the configured concurrency cap."""

        report = DispatchReport()
        if not self.config.hermes.enabled:
            return report

        candidates = [
            item
            for item in self.store.list_work(
                statuses=[WorkStatus.READY],
                limit=1000,
                order_by="priority",
            )
            if item.execution_mode == ExecutionMode.HERMES
        ]
        if self.config.operator.autonomy_mode == "shadow":
            report.skipped_work_ids.extend(item.id for item in candidates)
            if candidates:
                with self.store.transaction():
                    self.leadership_guard()
                    self.store.audit(
                        self.actor,
                        "dispatcher.shadow_dispatch_skipped",
                        entity_type="dispatcher",
                        entity_id=self.config.operator.instance_id,
                        data={"work_ids": report.skipped_work_ids},
                    )
            return report

        for item in candidates:
            if self.should_stop():
                break
            if not self._scheduled_time_is_due(item.scheduled_at):
                report.skipped_work_ids.append(item.id)
                self._audit_skip(item, "scheduled_for_future")
                continue
            authorized, authorization_reason = self._dispatch_authorized(item)
            if not authorized:
                report.skipped_work_ids.append(item.id)
                self._audit_skip(item, authorization_reason)
                continue
            if not self._has_acceptance_criteria(item):
                report.skipped_work_ids.append(item.id)
                self._audit_skip(item, "missing_acceptance_criteria")
                continue
            if not self.store.dependencies_satisfied(item.id):
                report.skipped_work_ids.append(item.id)
                self._audit_skip(item, "dependencies_not_satisfied")
                continue

            authorization = item.metadata.get("dispatch_authorization", {})
            contract_digest = (
                str(authorization.get("contract_digest", ""))
                if isinstance(authorization, dict)
                else ""
            )
            attested, attestation_reason, attestation_digest = (
                self._policy_attestation(str(item.assignee or ""))
            )
            if not attested:
                report.skipped_work_ids.append(item.id)
                self._audit_skip(item, attestation_reason)
                continue
            attestation_key = (
                f"hermes.policy_attestation:{item.assignee}"
                if self.config.hermes.require_policy_attestation
                else None
            )
            with self.store.transaction():
                self.leadership_guard()
                reserved_run = self.store.reserve_run_slot(
                    item.id,
                    runner="hermes-kanban",
                    max_active=self.config.operator.max_parallel_work,
                    stale_queue_seconds=int(
                        self.config.hermes.command_timeout_seconds + 60
                    ),
                    expected_work_version=item.version,
                    contract_digest=contract_digest,
                    required_state_key=attestation_key,
                    required_state_digest=(
                        attestation_digest if attestation_key else None
                    ),
                    actor=self.actor,
                )
            if reserved_run is None:
                report.skipped_work_ids.append(item.id)
                continue
            try:
                # Re-read after the atomic reservation. While the run remains
                # queued, SQLiteStore rejects edits to every field covered by
                # the dispatch contract, closing the check/create TOCTOU.
                current = self.store.get_work(item.id)
                current_authorized, current_reason = self._dispatch_authorized(current)
                reservation = reserved_run.get("result", {}).get("reservation", {})
                if (
                    not current_authorized
                    or current.version != item.version
                    or reservation.get("work_version") != current.version
                    or reservation.get("contract_digest") != contract_digest
                ):
                    with self.store.transaction():
                        self.leadership_guard()
                        self.store.update_run(
                            str(reserved_run["id"]),
                            actor=self.actor,
                            status="failed",
                            error=f"dispatch reservation invalid: {current_reason}",
                            finished_at=utc_now(),
                        )
                    report.skipped_work_ids.append(item.id)
                    self._audit_skip(item, "dispatch_reservation_invalid")
                    continue
                self._dispatch_one(current, active_run=reserved_run)
                report.dispatched_work_ids.append(item.id)
            except Exception as error:  # One failed card must not stop other work.
                if isinstance(error, LeaseFenceLost):
                    raise
                with self.store.transaction():
                    self.leadership_guard()
                    message = self._record_dispatch_error(item, error, reserved_run)
                report.errors[item.id] = message
                logger.warning("Hermes dispatch failed for %s: %s", item.id, message)
        return report

    def reconcile(self) -> DispatchReport:
        """Pull linked Hermes card state into the operator control plane."""

        report = DispatchReport()
        if not self.config.hermes.enabled:
            return report

        self._recover_orphaned_runs(report)

        if self.should_stop():
            return report

        linked = [
            item
            for item in self.store.list_work(
                statuses=[
                    WorkStatus.INBOX,
                    WorkStatus.TRIAGE,
                    WorkStatus.PLANNED,
                    WorkStatus.READY,
                    WorkStatus.RUNNING,
                    WorkStatus.WAITING_INPUT,
                    WorkStatus.BLOCKED,
                    WorkStatus.REVIEW,
                    WorkStatus.DONE,
                    WorkStatus.CANCELLED,
                    WorkStatus.ARCHIVED,
                ],
                limit=5000,
                order_by="updated",
            )
            if item.execution_mode == ExecutionMode.HERMES and item.hermes_task_id
        ]
        for item in linked:
            if self.should_stop():
                break
            try:
                task = self.adapter.show_task(str(item.hermes_task_id))
                request_stop = False
                stop_reason = ""
                with self.store.transaction():
                    self.leadership_guard()
                    current_item = self.store.get_work(item.id)
                    if current_item.version != item.version:
                        raise StateConflict(
                            "Work changed during Hermes reconciliation"
                        )
                    remote_state = self._normalize_state(task.status)
                    current_authorized, authorization_reason = self._dispatch_authorized(
                        current_item,
                        ongoing_external_run_id=task.id,
                    )
                    canonical_running = current_item.status == WorkStatus.RUNNING
                    request_stop = bool(
                        (
                            current_item.status in TERMINAL_WORK_STATUSES
                            or not current_authorized
                            or not canonical_running
                        )
                        and remote_state
                        not in (_COMPLETE_STATES | _CANCELLED_STATES)
                    )
                    if current_item.status in TERMINAL_WORK_STATUSES:
                        stop_reason = (
                            "Canonical work is terminal. Stop the worker and keep this "
                            "Hermes task blocked."
                        )
                    elif not current_authorized:
                        stop_reason = (
                            "Execution authorization is no longer valid "
                            f"({authorization_reason}). Stop work and await operator review."
                        )
                    elif not canonical_running:
                        stop_reason = (
                            "Canonical work is not in the running state "
                            f"({current_item.status.value}). Stop work before waiting, "
                            "review, or rescheduling."
                        )
                    changed, event_id = self._apply_task_state(current_item, task)
                if request_stop:
                    stopped_task = self._request_remote_stop(task.id, stop_reason)
                    if stopped_task is not None:
                        with self.store.transaction():
                            self.leadership_guard()
                            self._finish_active_run(
                                item.id,
                                task.id,
                                "cancelled",
                                {
                                    **self._task_payload(stopped_task),
                                    "native_stop_acknowledged": True,
                                    "stop_reason": stop_reason,
                                },
                            )
                        changed = True
                if changed:
                    report.reconciled_work_ids.append(item.id)
                if event_id:
                    report.event_ids.append(event_id)
            except Exception as error:  # A missing or malformed card is isolated.
                if isinstance(error, LeaseFenceLost):
                    raise
                message = self._error_text(error)
                report.errors[item.id] = message
                with self.store.transaction():
                    self.leadership_guard()
                    changed, event_id = self._record_reconciliation_failure(
                        item, error
                    )
                    if changed:
                        report.reconciled_work_ids.append(item.id)
                    if event_id:
                        report.event_ids.append(event_id)
                    self.store.audit(
                        self.actor,
                        "dispatcher.reconcile_failed",
                        entity_type="work",
                        entity_id=item.id,
                        data={
                            "hermes_task_id": item.hermes_task_id,
                            "error_type": type(error).__name__,
                            "error": message,
                        },
                    )
                logger.warning("Hermes reconciliation failed for %s: %s", item.id, message)
        return report

    def _recover_orphaned_runs(self, report: DispatchReport) -> None:
        for run in self.store.list_active_runs():
            if self.should_stop():
                return
            external_id = run.get("external_run_id")
            try:
                item = self.store.get_work(str(run["work_item_id"]))
                if item.hermes_task_id and run.get("status") != "queued":
                    continue
                reservation = run.get("result", {}).get("reservation", {})
                authorized, reason = self._dispatch_authorized(item)
                authorization = item.metadata.get("dispatch_authorization", {})
                current_digest = (
                    authorization.get("contract_digest")
                    if isinstance(authorization, Mapping)
                    else None
                )
                reservation_valid = bool(
                    isinstance(reservation, Mapping)
                    and reservation.get("work_version") == item.version
                    and reservation.get("contract_digest") == current_digest
                )
                if authorized and not reservation_valid:
                    reason = "dispatch_reservation_contract_mismatch"
                if (
                    not authorized
                    or not reservation_valid
                ):
                    if not external_id:
                        with self.store.transaction():
                            self.leadership_guard()
                            self.store.audit(
                                self.actor,
                                "dispatcher.queued_recovery_requires_operator",
                                entity_type="run",
                                entity_id=str(run["id"]),
                                data={
                                    "work_item_id": item.id,
                                    "reason": reason,
                                },
                            )
                        report.errors[item.id] = (
                            "queued Hermes reservation cannot be safely recovered: "
                            + reason
                        )
                        continue
                    task = self.adapter.show_task(str(external_id))
                    request_stop = bool(
                        self._normalize_state(task.status)
                        not in (_COMPLETE_STATES | _CANCELLED_STATES)
                        and run.get("status") != "cancel_requested"
                    )
                    with self.store.transaction():
                        self.leadership_guard()
                        current_item = self.store.get_work(item.id)
                        if current_item.version != item.version:
                            raise StateConflict(
                                "Orphaned work changed before quarantine"
                            )
                        self._quarantine_orphaned_run(
                            run,
                            current_item,
                            reason,
                            task,
                        )
                    if request_stop:
                        stopped_task = self._request_remote_stop(
                            task.id,
                            "Execution authorization is invalid. "
                            "Stop work and await operator review.",
                        )
                        if stopped_task is not None:
                            with self.store.transaction():
                                self.leadership_guard()
                                self.store.update_run(
                                    str(run["id"]),
                                    actor=self.actor,
                                    status="cancelled",
                                    result={
                                        **self._task_payload(stopped_task),
                                        "native_stop_acknowledged": True,
                                    },
                                    heartbeat_at=utc_now(),
                                    finished_at=utc_now(),
                                )
                    report.errors[item.id] = (
                        "orphaned Hermes run failed authorization revalidation"
                    )
                    continue
                if not external_id:
                    self._dispatch_one(item, active_run=run)
                    report.reconciled_work_ids.append(item.id)
                    continue
                task = self.adapter.show_task(str(external_id))
                metadata = self._hermes_metadata(item, task, phase="recovered")
                with self.store.transaction():
                    self.leadership_guard()
                    self.store.commit_dispatch_reservation(
                        str(run["id"]),
                        item.id,
                        expected_work_version=item.version,
                        contract_digest=str(current_digest),
                        external_run_id=task.id,
                        metadata=metadata,
                        result={"dispatch": self._task_payload(task), "recovered": True},
                        actor=self.actor,
                    )
                report.reconciled_work_ids.append(item.id)
            except Exception as error:
                if isinstance(error, LeaseFenceLost):
                    raise
                work_id = str(run["work_item_id"])
                report.errors[work_id] = self._error_text(error)
                if run.get("status") == "queued" and not external_id:
                    # A failed create call may have reached Hermes before its
                    # response was lost. Keep the reservation capacity-active
                    # for idempotent recovery or explicit operator resolution.
                    continue
                try:
                    with self.store.transaction():
                        self.leadership_guard()
                        failed_item = self.store.get_work(work_id)
                        changed, event_id = self._record_reconciliation_failure(
                            failed_item, error
                        )
                        if changed:
                            report.reconciled_work_ids.append(work_id)
                        if event_id:
                            report.event_ids.append(event_id)
                except LeaseFenceLost:
                    raise
                except Exception:
                    logger.warning(
                        "Could not persist orphaned run failure for %s", work_id
                    )

    def _record_reconciliation_failure(
        self,
        item: WorkItem,
        error: Exception,
    ) -> tuple[bool, str | None]:
        active = self._active_run(item.id)
        if active is None:
            return False, None
        result = dict(active.get("result", {}))
        failures = int(result.get("reconciliation_failures", 0)) + 1
        result["reconciliation_failures"] = failures
        result["last_reconciliation_error"] = self._error_text(error)
        result["last_reconciliation_failure_at"] = utc_now()
        if active.get("status") == "lost":
            self.store.update_run(
                str(active["id"]),
                actor=self.actor,
                result=result,
                error=self._error_text(error),
                finished_at=None,
            )
            return False, None
        heartbeat_text = active.get("heartbeat_at") or active.get("started_at")
        stale = False
        if heartbeat_text:
            try:
                heartbeat = datetime.fromisoformat(
                    str(heartbeat_text).replace("Z", "+00:00")
                )
                threshold = timedelta(
                    seconds=max(
                        int(self.config.operator.reconciliation_seconds * 3),
                        int(self.config.hermes.command_timeout_seconds * 3),
                        300,
                    )
                )
                stale = (
                    heartbeat.tzinfo is None
                    or datetime.now(UTC) - heartbeat.astimezone(UTC) > threshold
                )
            except ValueError:
                stale = True
        terminal = stale or failures >= 3
        if not terminal:
            self.store.update_run(
                str(active["id"]),
                actor=self.actor,
                result=result,
                error=self._error_text(error),
            )
            return False, None

        self.store.update_run(
            str(active["id"]),
            actor=self.actor,
            status="lost",
            result=result,
            error=self._error_text(error),
            finished_at=None,
        )
        # A lost reconciliation channel is not proof that remote execution
        # stopped. The run deliberately remains capacity-active and continues
        # to be polled until Hermes reports a terminal state. Clearing a
        # permanently missing execution requires explicit operator recovery.
        changed = False
        if item.status not in TERMINAL_WORK_STATUSES:
            metadata = dict(item.metadata)
            metadata["execution_lost"] = {
                "hermes_task_id": item.hermes_task_id,
                "failure_count": failures,
                "reason": self._error_text(error),
                "recorded_at": utc_now(),
            }
            self.store.update_work(
                item.id,
                {"status": WorkStatus.BLOCKED, "metadata": metadata},
                actor=self.actor,
                expected_version=item.version,
                allow_transition_override=True,
            )
            changed = True
        event = Event(
            source="hermes",
            event_type="execution.lost",
            external_id=str(item.hermes_task_id or active.get("external_run_id") or item.id),
            dedupe_key=hashlib.sha256(
                f"execution.lost|{item.id}|{active['id']}".encode()
            ).hexdigest(),
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            payload={
                "work_id": item.id,
                "hermes_task_id": item.hermes_task_id,
                "run_id": active["id"],
                "failure_count": failures,
                "capacity_slot_held": True,
                "requires_operator_review": True,
            },
            provenance={"adapter": "hermes-kanban"},
        )
        event_id, _created = self.store.enqueue_event(event, actor=self.actor)
        self.store.audit(
            self.actor,
            "dispatcher.execution_tracking_lost",
            entity_type="run",
            entity_id=str(active["id"]),
            data={
                "work_id": item.id,
                "capacity_slot_held": True,
                "operator_recovery_required_if_permanent": True,
            },
        )
        return changed, event_id

    def _quarantine_orphaned_run(
        self,
        run: Mapping[str, Any],
        item: WorkItem,
        reason: str,
        task: HermesTask,
    ) -> None:
        external_id = str(run.get("external_run_id") or "")
        remote_state = self._normalize_state(task.status)
        result = dict(run.get("result", {}))
        result["dispatch"] = self._task_payload(task)
        result["cancellation_requested"] = remote_state not in (
            _COMPLETE_STATES | _CANCELLED_STATES
        )
        if remote_state in _COMPLETE_STATES | _CANCELLED_STATES:
            terminal_status = (
                "quarantined"
                if remote_state in _COMPLETE_STATES
                else "cancelled"
            )
            self.store.update_run(
                str(run["id"]),
                actor=self.actor,
                status=terminal_status,
                result=result,
                error=f"orphaned dispatch authorization invalid: {reason}",
                heartbeat_at=utc_now(),
                finished_at=utc_now(),
            )
        else:
            self.store.update_run(
                str(run["id"]),
                actor=self.actor,
                status="cancel_requested",
                result=result,
                error=f"orphaned dispatch authorization invalid: {reason}",
                heartbeat_at=utc_now(),
                finished_at=None,
            )
        metadata = dict(item.metadata)
        metadata["orphaned_dispatch"] = {
            "external_run_id": external_id,
            "quarantined_at": utc_now(),
            "reason": reason,
        }
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            actor=self.actor,
            expected_version=item.version,
            allow_transition_override=True,
        )

    def _request_remote_stop(
        self, task_id: str, message: str
    ) -> HermesTask | None:
        """Terminate native compute and block the card from being redispatched."""

        self.leadership_guard()
        try:
            self.adapter.terminate_task(task_id)
            return self.adapter.block_task(task_id, message[:2000])
        except Exception as error:
            try:
                self.adapter.comment_task(
                    task_id,
                    "STOP REQUIRED: " + message[:1900],
                )
            except Exception:
                pass
            logger.warning(
                "Could not stop and block Hermes task %s: %s", task_id, error
            )
            return None

    def _dispatch_one(
        self,
        item: WorkItem,
        *,
        active_run: Mapping[str, Any] | None,
    ) -> None:
        run = active_run
        if run is None:
            raise RuntimeError("Hermes dispatch requires an atomic reservation")

        task: HermesTask | None = None
        try:
            self.leadership_guard()
            attempt = int(run.get("attempt", 1))
            previous = self._previous_run(item.id, before_attempt=attempt)
            can_resume = bool(
                item.hermes_task_id
                and previous
                and previous.get("status") == "blocked"
                and previous.get("external_run_id") == item.hermes_task_id
            )
            if can_resume:
                existing = self.adapter.show_task(str(item.hermes_task_id))
                existing_state = self._normalize_state(existing.status)
                if existing_state in _BLOCKED_STATES:
                    marker = f"[hermes-operator resume {run['id']}]"
                    if not any(
                        marker in str(comment.get("body", ""))
                        for comment in existing.comments
                        if isinstance(comment, Mapping)
                    ):
                        self.adapter.comment_task(
                            existing.id,
                            self._resume_comment(item, str(run["id"]), marker),
                        )
                    task = self.adapter.unblock_task(existing.id)
                elif existing_state in (_READY_STATES | _RUNNING_STATES):
                    # Recovery after an unblock response was lost. The queued
                    # canonical reservation still owns this exact attempt.
                    task = existing
                elif existing_state not in (_COMPLETE_STATES | _CANCELLED_STATES):
                    raise RuntimeError(
                        f"Hermes task cannot be safely resumed from {existing.status}"
                    )
            if task is None:
                task = self.adapter.create_task(
                    title=item.title,
                    description=self._task_description(item),
                    priority=item.priority if item.priority else None,
                    assignee=item.assignee or self.config.hermes.default_assignee or None,
                    # Canonical hierarchy is organizational context, not a
                    # Hermes dependency edge. Core depends_on links are
                    # already enforced before this call.
                    parent_id=None,
                    idempotency_key=(
                        f"hermes-operator:{item.id}:attempt:{attempt}"
                    ),
                    scheduled_at=item.scheduled_at,
                    metadata=self._dispatch_metadata(
                        item,
                        run_id=str(run["id"]),
                        attempt=attempt,
                    ),
                )

            metadata = self._hermes_metadata(item, task, phase="dispatched")
            reservation = run.get("result", {}).get("reservation", {})
            with self.store.transaction():
                self.leadership_guard()
                self.store.commit_dispatch_reservation(
                    str(run["id"]),
                    item.id,
                    expected_work_version=item.version,
                    contract_digest=str(reservation.get("contract_digest", "")),
                    external_run_id=task.id,
                    metadata=metadata,
                    result={"dispatch": self._task_payload(task)},
                    actor=self.actor,
                )
        except Exception as error:
            if isinstance(error, LeaseFenceLost):
                # Leave the immutable reservation for the new leader. Hermes
                # card creation is idempotent on the canonical attempt, so recovery can
                # safely rediscover a card created during the lost call.
                raise
            with self.store.transaction():
                self.leadership_guard()
                if task is None:
                    self.store.update_run(
                        str(run["id"]),
                        actor=self.actor,
                        status="queued",
                        error=self._error_text(error),
                        heartbeat_at=utc_now(),
                        finished_at=None,
                    )
                else:
                    # Keep the immutable reservation queued. Recovery revalidates
                    # its exact contract before linking the already-created card.
                    stored_result = dict(run.get("result", {}))
                    stored_result["dispatch"] = self._task_payload(task)
                    self.store.update_run(
                        str(run["id"]),
                        actor=self.actor,
                        external_run_id=task.id,
                        error=self._error_text(error),
                        heartbeat_at=utc_now(),
                        result=stored_result,
                    )
            raise

    def _apply_task_state(self, item: WorkItem, task: HermesTask) -> tuple[bool, str | None]:
        state = self._normalize_state(task.status)
        latest = self._latest_run(item.id)
        if latest and latest.get("external_run_id") == task.id:
            latest_status = str(latest.get("status", ""))
            if state in _COMPLETE_STATES and latest_status == "completed":
                return self._record_completion(item, task)
            if state in _BLOCKED_STATES and latest_status in {
                "blocked",
                "cancelled",
                "quarantined",
            }:
                return False, None
            if state in _CANCELLED_STATES and latest_status in {
                "cancelled",
                "quarantined",
            }:
                return False, None
        if item.status not in TERMINAL_WORK_STATUSES and state not in _CANCELLED_STATES:
            if (
                item.status != WorkStatus.RUNNING
                and state not in (_COMPLETE_STATES | _BLOCKED_STATES)
            ):
                return self._quarantine_linked_execution(
                    item,
                    task,
                    reason=f"canonical_work_not_running:{item.status.value}",
                    hermes_state=state,
                )
            authorized, reason = self._dispatch_authorized(
                item,
                ongoing_external_run_id=task.id,
            )
            if not authorized:
                return self._quarantine_linked_execution(
                    item, task, reason=reason, hermes_state=state
                )
        if state in _COMPLETE_STATES:
            return self._record_completion(item, task)
        if state in _BLOCKED_STATES:
            return self._record_blocked(item, task)
        if state in _CANCELLED_STATES:
            return self._record_cancelled(item, task)
        if state in _RUNNING_STATES or state in _READY_STATES:
            return self._record_active(item, task), None

        self.store.audit(
            self.actor,
            "dispatcher.unknown_hermes_status",
            entity_type="work",
            entity_id=item.id,
            data={"hermes_task_id": task.id, "status": task.status},
        )
        self._keep_active_run(item.id, task, status="running")
        return False, None

    def _quarantine_linked_execution(
        self,
        item: WorkItem,
        task: HermesTask,
        *,
        reason: str,
        hermes_state: str,
    ) -> tuple[bool, str | None]:
        payload = self._task_payload(task)
        if hermes_state in (_COMPLETE_STATES | _BLOCKED_STATES | _CANCELLED_STATES):
            self._finish_active_run(item.id, task.id, "quarantined", payload)
        else:
            self._keep_active_run(
                item.id,
                task,
                status="cancel_requested",
            )
        prior = item.metadata.get("execution_quarantine", {})
        target_status = (
            item.status
            if item.status
            in {WorkStatus.BLOCKED, WorkStatus.REVIEW, WorkStatus.WAITING_INPUT}
            else WorkStatus.BLOCKED
        )
        already_recorded = (
            isinstance(prior, Mapping)
            and prior.get("hermes_task_id") == task.id
            and prior.get("reason") == reason
            and item.status == target_status
        )
        metadata = dict(item.metadata)
        metadata["execution_quarantine"] = {
            "hermes_task_id": task.id,
            "hermes_state": hermes_state,
            "reason": reason,
            "cancel_requested": hermes_state
            not in (_COMPLETE_STATES | _BLOCKED_STATES | _CANCELLED_STATES),
            "recorded_at": (
                prior.get("recorded_at")
                if already_recorded and isinstance(prior, Mapping)
                else utc_now()
            ),
        }
        if not already_recorded:
            self.store.update_work(
                item.id,
                {"status": target_status, "metadata": metadata},
                actor=self.actor,
                expected_version=item.version,
                allow_transition_override=True,
            )
        event = Event(
            source="hermes",
            event_type="execution.quarantined",
            external_id=task.id,
            dedupe_key=hashlib.sha256(
                f"execution.quarantined|{item.id}|{task.id}|{reason}|{hermes_state}".encode()
            ).hexdigest(),
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            payload={
                "work_id": item.id,
                "hermes_task_id": task.id,
                "reason": reason,
                "hermes_state": hermes_state,
                "cancel_requested": hermes_state
                not in (_COMPLETE_STATES | _BLOCKED_STATES | _CANCELLED_STATES),
            },
            provenance={"adapter": "hermes-kanban", "hermes_task_id": task.id},
        )
        event_id, _created = self.store.enqueue_event(event, actor=self.actor)
        return not already_recorded, event_id

    def _record_completion(self, item: WorkItem, task: HermesTask) -> tuple[bool, str | None]:
        payload = self._task_payload(task)
        completed_run = self._finish_active_run(
            item.id, task.id, "completed", payload
        )

        if item.status in TERMINAL_WORK_STATUSES:
            return False, None

        fingerprint = task.updated_at or self._payload_digest(payload)
        existing_hermes = item.metadata.get("hermes", {})
        prior_verification = item.metadata.get("last_verification", {})
        if (
            isinstance(existing_hermes, Mapping)
            and existing_hermes.get("completion_run_id") == completed_run["id"]
        ):
            self.store.audit(
                self.actor,
                "execution.completed_reobservation_ignored",
                entity_type="run",
                entity_id=str(completed_run["id"]),
                data={
                    "work_item_id": item.id,
                    "hermes_task_id": task.id,
                },
            )
            return False, None
        if (
            item.status in {WorkStatus.BLOCKED, WorkStatus.WAITING_INPUT}
            and isinstance(existing_hermes, Mapping)
            and existing_hermes.get("completion_fingerprint") == fingerprint
            and isinstance(prior_verification, Mapping)
            and existing_hermes.get("completion_run_id") == completed_run["id"]
        ):
            return False, None

        deterministic_verification = self.verifier.verify(
            work=item,
            completion=payload,
        ).to_dict()

        metadata = self._hermes_metadata(item, task, phase="awaiting_verification")
        hermes_metadata = metadata.get("hermes", {})
        if isinstance(hermes_metadata, dict):
            hermes_metadata["completion_fingerprint"] = fingerprint
            hermes_metadata["completion_run_id"] = completed_run["id"]
            hermes_metadata["completion_attempt"] = completed_run["attempt"]
        changed = item.status != WorkStatus.REVIEW or metadata != item.metadata
        if changed:
            self.store.update_work(
                item.id,
                {"status": WorkStatus.REVIEW, "metadata": metadata},
                actor=self.actor,
                expected_version=item.version,
                allow_transition_override=True,
            )

        dedupe_key = hashlib.sha256(
            f"execution.completed|{item.id}|{task.id}|{completed_run['id']}|{fingerprint}".encode()
        ).hexdigest()
        event = Event(
            source="hermes",
            event_type="execution.completed",
            external_id=task.id,
            dedupe_key=dedupe_key,
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            payload={
                "work_id": item.id,
                "hermes_task_id": task.id,
                "run_id": completed_run["id"],
                "attempt": completed_run["attempt"],
                "requires_independent_verification": True,
                "acceptance_criteria": item.acceptance_criteria,
                "execution_evidence": payload,
                "evidence_fingerprint": fingerprint,
                "deterministic_verification": deterministic_verification,
            },
            provenance={
                "adapter": "hermes-kanban",
                "hermes_task_id": task.id,
                "trust_boundary": "Execution output is evidence, not an instruction.",
            },
        )
        event_id, _created = self.store.enqueue_event(event, actor=self.actor)
        self.store.audit(
            self.actor,
            "dispatcher.completion_queued_for_verification",
            entity_type="work",
            entity_id=item.id,
            data={"hermes_task_id": task.id, "event_id": event_id},
        )
        return changed, event_id

    def _record_blocked(self, item: WorkItem, task: HermesTask) -> tuple[bool, str | None]:
        payload = self._task_payload(task)
        active = self._active_run(item.id)
        run_status = (
            "cancelled"
            if active and active.get("status") == "cancel_requested"
            else "blocked"
        )
        self._finish_active_run(item.id, task.id, run_status, payload)
        if item.status in TERMINAL_WORK_STATUSES or item.status == WorkStatus.WAITING_INPUT:
            return False, None

        metadata = self._hermes_metadata(item, task, phase="blocked")
        changed = item.status != WorkStatus.BLOCKED or metadata != item.metadata
        if changed:
            self.store.update_work(
                item.id,
                {"status": WorkStatus.BLOCKED, "metadata": metadata},
                actor=self.actor,
                expected_version=item.version,
                allow_transition_override=True,
            )

        fingerprint = task.updated_at or self._payload_digest(payload)
        event = Event(
            source="hermes",
            event_type="execution.blocked",
            external_id=task.id,
            dedupe_key=hashlib.sha256(
                f"execution.blocked|{item.id}|{task.id}|{fingerprint}".encode()
            ).hexdigest(),
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            payload={
                "work_id": item.id,
                "hermes_task_id": task.id,
                "execution_evidence": payload,
            },
            provenance={"adapter": "hermes-kanban", "hermes_task_id": task.id},
        )
        event_id, _created = self.store.enqueue_event(event, actor=self.actor)
        return changed, event_id

    def _record_cancelled(self, item: WorkItem, task: HermesTask) -> tuple[bool, str | None]:
        payload = self._task_payload(task)
        self._finish_active_run(item.id, task.id, "cancelled", payload)
        if item.status in TERMINAL_WORK_STATUSES:
            return False, None
        metadata = self._hermes_metadata(item, task, phase="cancelled")
        self.store.update_work(
            item.id,
            {"status": WorkStatus.BLOCKED, "metadata": metadata},
            actor=self.actor,
            expected_version=item.version,
            allow_transition_override=True,
        )
        fingerprint = task.updated_at or self._payload_digest(payload)
        event = Event(
            source="hermes",
            event_type="execution.cancelled",
            external_id=task.id,
            dedupe_key=hashlib.sha256(
                f"execution.cancelled|{item.id}|{task.id}|{fingerprint}".encode()
            ).hexdigest(),
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            payload={
                "work_id": item.id,
                "hermes_task_id": task.id,
                "execution_evidence": payload,
                "requires_operator_or_supervisor_decision": True,
            },
            provenance={"adapter": "hermes-kanban", "hermes_task_id": task.id},
        )
        event_id, _created = self.store.enqueue_event(event, actor=self.actor)
        return True, event_id

    def _record_active(self, item: WorkItem, task: HermesTask) -> bool:
        self._ensure_active_run(item.id, task)
        if item.status in TERMINAL_WORK_STATUSES or item.status in {
            WorkStatus.REVIEW,
            WorkStatus.BLOCKED,
            WorkStatus.WAITING_INPUT,
        }:
            return False
        metadata = self._hermes_metadata(item, task, phase="executing")
        changed = item.status != WorkStatus.RUNNING or metadata != item.metadata
        if changed:
            self.store.update_work(
                item.id,
                {"status": WorkStatus.RUNNING, "metadata": metadata},
                actor=self.actor,
                expected_version=item.version,
                allow_transition_override=True,
            )
        return changed

    def _ensure_active_run(self, work_id: str, task: HermesTask) -> None:
        active = self._active_run(work_id)
        if active:
            status = (
                "cancel_requested"
                if active.get("status") == "cancel_requested"
                else "running"
            )
            self.store.update_run(
                str(active["id"]),
                actor=self.actor,
                external_run_id=task.id,
                status=status,
                heartbeat_at=utc_now(),
                finished_at=None,
            )
            return
        self.store.create_run(
            RunRecord(
                work_item_id=work_id,
                runner="hermes-kanban",
                external_run_id=task.id,
                status="running",
                attempt=self._next_attempt(work_id),
                result={"recovered_from_reconciliation": True},
                started_at=utc_now(),
                heartbeat_at=utc_now(),
            ),
            actor=self.actor,
        )

    def _keep_active_run(
        self,
        work_id: str,
        task: HermesTask,
        *,
        status: str,
    ) -> None:
        payload = self._task_payload(task)
        active = self._active_run(work_id)
        if active:
            self.store.update_run(
                str(active["id"]),
                actor=self.actor,
                external_run_id=task.id,
                status=status,
                result=payload,
                error=None,
                heartbeat_at=utc_now(),
                finished_at=None,
            )
            return
        self.store.create_run(
            RunRecord(
                work_item_id=work_id,
                runner="hermes-kanban",
                external_run_id=task.id,
                status=status,
                attempt=self._next_attempt(work_id),
                result=payload,
                started_at=utc_now(),
                heartbeat_at=utc_now(),
            ),
            actor=self.actor,
        )

    def _finish_active_run(
        self,
        work_id: str,
        external_run_id: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        active = self._active_run(work_id)
        if active:
            self.store.update_run(
                str(active["id"]),
                actor=self.actor,
                external_run_id=external_run_id,
                status=status,
                result=result,
                error=None,
                heartbeat_at=utc_now(),
                finished_at=utc_now(),
            )
            return self.store.get_run(str(active["id"]))
        latest = self._latest_run(work_id)
        if latest and latest["status"] == status and latest["external_run_id"] == external_run_id:
            return latest
        created = RunRecord(
                work_item_id=work_id,
                runner="hermes-kanban",
                external_run_id=external_run_id,
                status=status,
                attempt=self._next_attempt(work_id),
                result=result,
                finished_at=utc_now(),
            )
        self.store.create_run(created, actor=self.actor)
        return self.store.get_run(created.id)

    def _active_run(self, work_id: str) -> dict[str, Any] | None:
        runs = [
            run
            for run in self.store.list_active_runs()
            if str(run["work_item_id"]) == work_id
        ]
        return runs[-1] if runs else None

    def _latest_run(self, work_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE work_item_id = ? ORDER BY attempt DESC, id DESC LIMIT 1",
                (work_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json") or "{}")
        return value

    def _next_attempt(self, work_id: str) -> int:
        latest = self._latest_run(work_id)
        return int(latest["attempt"]) + 1 if latest else 1

    def _previous_run(
        self, work_id: str, *, before_attempt: int
    ) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE work_item_id = ? AND attempt < ? "
                "ORDER BY attempt DESC, id DESC LIMIT 1",
                (work_id, before_attempt),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json") or "{}")
        return value

    def _resume_comment(self, item: WorkItem, run_id: str, marker: str) -> str:
        answers = [
            value
            for value in self.store.list_questions(status="answered", limit=1000)
            if item.id in value.get("blocking_work_ids", [])
            and value.get("answer")
        ][-5:]
        lines = [
            marker,
            "A fresh exact execution authorization resumed this task.",
            f"Canonical work item: {item.id}",
            f"Canonical run reservation: {run_id}",
        ]
        if answers:
            lines.append("Operator answers:")
            for value in answers:
                question = str(value.get("question", ""))[:500]
                answer = str(value.get("answer", ""))[:1500]
                lines.append(f"- {question}\n  Answer: {answer}")
        else:
            request = item.metadata.get("dispatch_request", {})
            reason = (
                str(request.get("reason", ""))
                if isinstance(request, Mapping)
                else ""
            )
            if reason:
                lines.append("Authorization context: " + reason[:1500])
        lines.append("Continue only within the unchanged acceptance criteria.")
        return "\n".join(lines)[:6000]

    def _task_description(self, item: WorkItem) -> str:
        sections: list[str] = []
        if item.description.strip():
            sections.append(item.description.strip())
        criteria = "\n".join(f"- {criterion.strip()}" for criterion in item.acceptance_criteria)
        sections.append(f"Acceptance criteria:\n{criteria}")
        sections.append(
            "Operator control boundary:\n"
            f"- Work item: {item.id}\n"
            "- Do not send, publish, submit, or otherwise execute external-facing actions.\n"
            "- Return external-facing drafts and proposed actions for explicit approval."
        )
        if item.parent_id:
            try:
                parent = self.store.get_work(item.parent_id)
                sections.append(
                    "Operator hierarchy context:\n"
                    f"- Parent work item: {parent.id}\n"
                    f"- Parent title: {parent.title}\n"
                    "- This is organizational context, not a Hermes dependency."
                )
            except NotFound:
                sections.append(
                    "Operator hierarchy context:\n"
                    f"- Parent work item: {item.parent_id}\n"
                    "- This is organizational context, not a Hermes dependency."
                )
        request = item.metadata.get("dispatch_request", {})
        request = request if isinstance(request, Mapping) else {}
        if bool(request.get("goal_mode", self.config.hermes.goal_mode)):
            sections.append(
                "Hermes orchestration guidance:\n"
                "- Treat this as a goal that may be decomposed into bounded subtasks.\n"
                "- Run independent subtasks with Hermes subagents in parallel when useful.\n"
                "- Keep every child inside this work item's scope and acceptance criteria.\n"
                "- Consolidate child evidence and blockers back into this Kanban task."
            )
        else:
            sections.append(
                "Hermes execution guidance:\n"
                "- Use Hermes subagents in parallel only for clearly independent subtasks.\n"
                "- Keep delegation inside this work item's scope and acceptance criteria.\n"
                "- Consolidate child evidence and blockers back into this Kanban task."
            )
        requested_skills = request.get("skills", [])
        if not isinstance(requested_skills, list):
            requested_skills = []
        skills = self._effective_skills(requested_skills)
        if skills:
            sections.append(
                "Requested Hermes skills: " + ", ".join(str(value) for value in skills)
            )
        verification_contract = item.metadata.get("verification_contract")
        if isinstance(verification_contract, Mapping):
            artifacts = verification_contract.get("artifacts", [])
            checks = verification_contract.get("checks", [])
            sections.append(
                "Deterministic verification contract:\n"
                "- Produce any required artifacts at the exact configured-root paths.\n"
                "- Do not alter or substitute the named deterministic checks.\n"
                + json.dumps(
                    {"artifacts": artifacts, "checks": checks},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )[:6_000]
            )
        return "\n\n".join(sections)

    def _dispatch_metadata(
        self,
        item: WorkItem,
        *,
        run_id: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        request = item.metadata.get("dispatch_request", {})
        request = request if isinstance(request, Mapping) else {}
        requested_skills = request.get("skills", [])
        if not isinstance(requested_skills, list):
            requested_skills = []
        skills = self._effective_skills(requested_skills)
        return {
            "operator_work_id": item.id,
            "operator_parent_work_id": item.parent_id,
            "operator_run_id": run_id,
            "operator_attempt": attempt,
            "source_event_id": item.source_event_id,
            "acceptance_criteria": list(item.acceptance_criteria),
            "autonomy_mode": self.config.operator.autonomy_mode,
            "orchestrator_profile": self.config.hermes.orchestrator_profile,
            "goal_mode": bool(request.get("goal_mode", self.config.hermes.goal_mode)),
            "skills": skills,
        }

    def _hermes_metadata(
        self,
        item: WorkItem,
        task: HermesTask,
        *,
        phase: str,
    ) -> dict[str, Any]:
        metadata = dict(item.metadata)
        existing = metadata.get("hermes")
        hermes = dict(existing) if isinstance(existing, Mapping) else {}
        observation = {
            "task_id": task.id,
            "status": task.status,
            "updated_at": task.updated_at,
            "phase": phase,
        }
        if any(hermes.get(key) != value for key, value in observation.items()):
            observation["observed_at"] = utc_now()
        else:
            observation["observed_at"] = hermes.get("observed_at") or utc_now()
        hermes.update(observation)
        metadata["hermes"] = hermes
        return metadata

    @staticmethod
    def _has_acceptance_criteria(item: WorkItem) -> bool:
        return bool(item.acceptance_criteria) and all(
            isinstance(value, str) and bool(value.strip()) for value in item.acceptance_criteria
        )

    @staticmethod
    def _scheduled_time_is_due(value: str | None) -> bool:
        if value is None:
            return True
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return False
        return parsed.astimezone(UTC) <= datetime.now(UTC)

    def _dispatch_authorized(
        self,
        item: WorkItem,
        *,
        ongoing_external_run_id: str | None = None,
    ) -> tuple[bool, str]:
        if not self.store.dependencies_satisfied(item.id):
            return False, "dependencies_not_satisfied"
        governance = item.metadata.get("governance", {})
        if not (
            isinstance(governance, Mapping)
            and governance.get("execution_authorized") is True
        ):
            return False, "missing_execution_authorization"
        authorization = item.metadata.get("dispatch_authorization", {})
        request = item.metadata.get("dispatch_request", {})
        if not isinstance(authorization, Mapping) or not isinstance(request, Mapping):
            return False, "missing_dispatch_authorization"
        if request.get("shadow") is True or authorization.get("shadow") is True:
            return False, "dispatch_authorization_shadow_only"
        if authorization.get("work_id") != item.id:
            return False, "dispatch_authorization_work_mismatch"
        if authorization.get("trust") not in {"operator", "system"}:
            return False, "dispatch_authorization_untrusted"
        consumed_at = authorization.get("consumed_at")
        if consumed_at is not None:
            consumed_run_id = str(authorization.get("consumed_run_id", ""))
            consumed_external_run_id = str(
                authorization.get("consumed_external_run_id", "")
            )
            if (
                not ongoing_external_run_id
                or consumed_external_run_id != str(ongoing_external_run_id)
                or consumed_external_run_id != str(item.hermes_task_id or "")
            ):
                return False, "dispatch_authorization_consumed"
            try:
                consumed_run = self.store.get_run(consumed_run_id)
            except NotFound:
                return False, "dispatch_authorization_run_missing"
            if (
                consumed_run.get("work_item_id") != item.id
                or consumed_run.get("external_run_id") != consumed_external_run_id
                or consumed_run.get("status") not in {"running", "lost"}
            ):
                return False, "dispatch_authorization_run_not_active"
        expires_at = authorization.get("expires_at")
        durable = authorization.get("lifetime") == "until_consumed_or_contract_change"
        if durable:
            if expires_at is not None:
                return False, "dispatch_authorization_durable_expiry_invalid"
            if authorization.get("not_before") != item.scheduled_at:
                return False, "dispatch_authorization_not_before_mismatch"
            if not isinstance(authorization.get("max_attempts"), int) or isinstance(
                authorization.get("max_attempts"), bool
            ):
                return False, "dispatch_authorization_attempt_budget_invalid"
            if not (
                1
                <= int(authorization["max_attempts"])
                <= self.config.hermes.max_execution_attempts
            ):
                return False, "dispatch_authorization_attempt_budget_invalid"
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(authorization.get("authorization_root", ""))
            ):
                return False, "dispatch_authorization_root_invalid"
        else:
            try:
                expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            except ValueError:
                return False, "dispatch_authorization_expiry_invalid"
            if expires.tzinfo is None or expires.astimezone(UTC) <= datetime.now(UTC):
                return False, "dispatch_authorization_expired"
        issuer = str(authorization.get("issued_by", ""))
        if not (issuer.startswith("supervisor:") or issuer == "operator-cli"):
            return False, "dispatch_authorization_issuer_invalid"
        if issuer.startswith("supervisor:"):
            pass_id = str(authorization.get("supervisor_pass", ""))
            plan_digest = str(authorization.get("plan_digest", ""))
            finalized = self.store.get_state(
                f"supervisor.pass:{pass_id}", {}
            )
            if not (
                pass_id
                and len(plan_digest) == 64
                and isinstance(finalized, Mapping)
                and finalized.get("finalized") is True
                and finalized.get("pass_id") == pass_id
                and finalized.get("plan_digest") == plan_digest
            ):
                return False, "supervisor_pass_not_finalized"
        profile = str(authorization.get("profile", ""))
        if not profile or profile != str(item.assignee or ""):
            return False, "dispatch_profile_mismatch"
        if profile != str(request.get("profile", "")):
            return False, "dispatch_request_profile_mismatch"
        allowed_profiles = set(self.config.hermes.allowed_profiles)
        allowed_profiles.update(
            value
            for value in (
                self.config.hermes.profile,
                self.config.hermes.default_assignee,
                self.config.hermes.orchestrator_profile,
            )
            if value
        )
        if profile not in allowed_profiles:
            return False, "dispatch_profile_not_allowed"
        authorization_skills = authorization.get("skills", [])
        request_skills = request.get("skills", [])
        if not isinstance(authorization_skills, list) or not isinstance(request_skills, list):
            return False, "dispatch_skills_invalid"
        if sorted(set(map(str, authorization_skills))) != sorted(
            set(map(str, request_skills))
        ):
            return False, "dispatch_skills_mismatch"
        allowed_skills = set(self.config.hermes.allowed_skills)
        allowed_skills.update(self.config.hermes.default_skills)
        if set(map(str, authorization_skills)) - allowed_skills:
            return False, "dispatch_skill_not_allowed"
        expected_digest = dispatch_contract_digest(
            item,
            profile=profile,
            skills=list(map(str, authorization_skills)),
            default_skills=self.config.hermes.default_skills,
            goal_mode=bool(request.get("goal_mode", False)),
        )
        if authorization.get("contract_digest") != expected_digest:
            return False, "dispatch_contract_changed"
        attested, reason, _ = self._policy_attestation(profile)
        if not attested:
            return False, reason
        return True, "authorized"

    def _effective_skills(self, requested_skills: list[Any]) -> list[str]:
        return sorted(
            {
                normalized
                for value in [
                    *self.config.hermes.default_skills,
                    *requested_skills,
                ]
                if (normalized := str(value).strip())
            }
        )

    def _policy_attestation(self, profile: str) -> tuple[bool, str, str]:
        if not self.config.hermes.require_policy_attestation:
            return True, "policy_attestation_not_required", ""
        state = self.store.get_state(
            f"hermes.policy_attestation:{profile}", None
        )
        if not isinstance(state, Mapping):
            return False, "policy_attestation_missing", ""
        if state.get("authenticated_ingress") is not True:
            return False, "policy_attestation_unauthenticated", ""
        if state.get("profile") != profile:
            return False, "policy_attestation_profile_mismatch", ""
        if state.get("guard_active") is not True or state.get("policy_mode") != "default_deny":
            return False, "policy_attestation_guard_inactive", ""
        if state.get("plugin_version") not in set(
            self.config.hermes.allowed_plugin_versions
        ):
            return False, "policy_attestation_plugin_version", ""
        if state.get("policy_version") not in set(
            self.config.hermes.allowed_policy_versions
        ):
            return False, "policy_attestation_policy_version", ""
        if state.get("policy_digest") not in set(
            self.config.hermes.allowed_policy_digests
        ):
            return False, "policy_attestation_digest", ""
        try:
            attested_at = datetime.fromisoformat(
                str(state.get("attested_at", "")).replace("Z", "+00:00")
            )
            received_at = datetime.fromisoformat(
                str(state.get("received_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            return False, "policy_attestation_timestamp_invalid", ""
        now = datetime.now(UTC)
        ttl = timedelta(
            seconds=self.config.hermes.policy_attestation_ttl_seconds
        )
        if (
            attested_at.tzinfo is None
            or received_at.tzinfo is None
            or attested_at.astimezone(UTC) > now + timedelta(seconds=60)
            or received_at.astimezone(UTC) > now + timedelta(seconds=60)
            or now - attested_at.astimezone(UTC) > ttl
            or now - received_at.astimezone(UTC) > ttl
        ):
            return False, "policy_attestation_stale", ""
        canonical = json.dumps(
            dict(state),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return True, "policy_attestation_valid", hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _normalize_state(status: str) -> str:
        return "_".join(str(status).strip().lower().replace("-", " ").split())

    @classmethod
    def _task_payload(cls, task: HermesTask) -> dict[str, Any]:
        value = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "description": task.description,
            "priority": task.priority,
            "assignee": task.assignee,
            "parent_id": task.parent_id,
            "scheduled_at": task.scheduled_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "current_run_id": task.current_run_id,
            "comments": list(task.comments),
            "raw": dict(task.raw),
        }
        # Adapter payloads are expected to be JSON, but a defensive conversion
        # prevents an integration-specific scalar from breaking reconciliation.
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))

    @staticmethod
    def _payload_digest(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _audit_skip(self, item: WorkItem, reason: str) -> None:
        with self.store.transaction():
            self.leadership_guard()
            self.store.audit(
                self.actor,
                "dispatcher.work_not_eligible",
                entity_type="work",
                entity_id=item.id,
                data={"reason": reason},
            )

    def _record_dispatch_error(
        self,
        item: WorkItem,
        error: Exception,
        active_run: Mapping[str, Any] | None,
    ) -> str:
        message = self._error_text(error)
        self.store.audit(
            self.actor,
            "dispatcher.dispatch_failed",
            entity_type="work",
            entity_id=item.id,
            data={
                "run_id": active_run.get("id") if active_run else None,
                "error_type": type(error).__name__,
                "error": message,
            },
        )
        return message

    @staticmethod
    def _error_text(error: Exception) -> str:
        return str(error)[:2000] or type(error).__name__
