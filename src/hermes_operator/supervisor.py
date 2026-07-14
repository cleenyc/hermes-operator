from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .config import AppConfig
from .db import LeaseFenceLost, NotFound, SQLiteStore, StateConflict
from .dispatcher import dispatch_contract_digest
from .llm import LLMResult, PlannerLLM
from .models import (
    Event,
    ExecutionMode,
    TrustLevel,
    UserQuestion,
    WorkItem,
    WorkKind,
    WorkRelation,
    WorkStatus,
    new_id,
    normalize_recurrence_rule,
    utc_now,
)
from .prioritization import PriorityEngine
from .prompts import SUPERVISOR_SYSTEM_PROMPT
from .verifier import ArtifactVerifier


logger = logging.getLogger(__name__)


_MAX_CONTEXT_BYTES = 786_432
_MAX_CONTEXT_STRING_CHARS = 4_000
_MAX_CONTEXT_COLLECTION_ITEMS = 64


_PROTECTED_METADATA_KEYS = {
    "dispatch_authorization",
    "dispatch_request",
    "hermes",
    "last_verification",
    "governance",
    "verification_contract",
}


def _bounded_context_value(value: Any, *, depth: int = 0) -> Any:
    """Bound stored evidence before placing it in a model context."""

    if isinstance(value, str):
        if len(value) <= _MAX_CONTEXT_STRING_CHARS:
            return value
        omitted = len(value) - _MAX_CONTEXT_STRING_CHARS
        return value[:_MAX_CONTEXT_STRING_CHARS] + f"...[truncated {omitted} chars]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 6:
        return "[context depth truncated]"
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {
            str(key): _bounded_context_value(item, depth=depth + 1)
            for key, item in items[:_MAX_CONTEXT_COLLECTION_ITEMS]
        }
        if len(items) > _MAX_CONTEXT_COLLECTION_ITEMS:
            bounded["__context_truncated_keys__"] = (
                len(items) - _MAX_CONTEXT_COLLECTION_ITEMS
            )
        return bounded
    if isinstance(value, (list, tuple)):
        items = list(value)
        bounded_items = [
            _bounded_context_value(item, depth=depth + 1)
            for item in items[:_MAX_CONTEXT_COLLECTION_ITEMS]
        ]
        if len(items) > _MAX_CONTEXT_COLLECTION_ITEMS:
            bounded_items.append(
                f"[context truncated {len(items) - _MAX_CONTEXT_COLLECTION_ITEMS} items]"
            )
        return bounded_items
    return _bounded_context_value(str(value), depth=depth)


class ActionStager(Protocol):
    def stage(self, proposal: dict[str, Any], *, created_by: str) -> str: ...


@dataclass(slots=True)
class PassResult:
    pass_id: str
    trigger: str
    event_ids: list[str]
    summary: str
    created_work_ids: list[str] = field(default_factory=list)
    updated_work_ids: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)
    dispatch_work_ids: list[str] = field(default_factory=list)
    memory_candidate_ids: list[str] = field(default_factory=list)
    verified_work_ids: list[str] = field(default_factory=list)
    action_intent_ids: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    event_dispositions: list[dict[str, Any]] = field(default_factory=list)
    llm_model: str = ""
    llm_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_id": self.pass_id,
            "trigger": self.trigger,
            "event_ids": self.event_ids,
            "summary": self.summary,
            "created_work_ids": self.created_work_ids,
            "updated_work_ids": self.updated_work_ids,
            "question_ids": self.question_ids,
            "dispatch_work_ids": self.dispatch_work_ids,
            "memory_candidate_ids": self.memory_candidate_ids,
            "verified_work_ids": self.verified_work_ids,
            "action_intent_ids": self.action_intent_ids,
            "observations": self.observations,
            "event_dispositions": self.event_dispositions,
            "llm_model": self.llm_model,
            "llm_usage": self.llm_usage,
        }


class PlanValidationError(ValueError):
    pass


def _bounded_factor(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        raise PlanValidationError("Numeric planning factors must be finite")
    return max(0.0, min(1.0, number))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_version(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PlanValidationError(f"{field} must be a positive integer")
    try:
        version = int(value)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(
            f"{field} must be a positive integer"
        ) from error
    if version < 1:
        raise PlanValidationError(f"{field} must be a positive integer")
    return version


def _validate_timestamp(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlanValidationError(f"Invalid timestamp: {text}") from error
    if parsed.tzinfo is None:
        raise PlanValidationError(f"Timestamp needs a timezone: {text}")
    return text


def _validate_recurrence(value: Any) -> str | None:
    try:
        return normalize_recurrence_rule(value)
    except ValueError as error:
        raise PlanValidationError(str(error)) from error


def _sanitized_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) not in _PROTECTED_METADATA_KEYS
    }


def _trusted_event(event: dict[str, Any] | None) -> bool:
    return bool(
        event
        and event.get("trust_level")
        in {TrustLevel.OPERATOR.value, TrustLevel.SYSTEM.value}
    )


def _completion_event(event: dict[str, Any] | None) -> bool:
    """Return whether an event claims to carry Hermes completion evidence.

    Completion evidence is not privileged input, but it can lead to a terminal
    verification transition. Treating it as an authority-bearing context class
    keeps unrelated inbound prose out of the same reasoning pass. The stricter
    provenance, card, run, and fingerprint checks remain in verification.
    """

    return bool(
        event
        and event.get("source") == "hermes"
        and event.get("event_type") == "execution.completed"
    )


def _task_like_event(event: dict[str, Any]) -> bool:
    """Conservatively identify events that cannot be dismissed as FYI."""

    event_type = str(event.get("event_type", "")).casefold()
    if any(
        marker in event_type
        for marker in (
            "request",
            "task",
            "todo",
            "reminder",
            "assign",
            "action_required",
        )
    ):
        return True
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return False
    if any(
        key in payload and payload.get(key) not in (None, False, "", [], {})
        for key in (
            "action_required",
            "requires_action",
            "task",
            "todo",
            "due_at",
            "deadline",
        )
    ):
        return True
    command_starts = (
        "prepare ",
        "review ",
        "send ",
        "schedule ",
        "create ",
        "complete ",
        "finish ",
        "follow up ",
        "respond ",
        "update ",
        "fix ",
        "draft ",
        "deliver ",
        "submit ",
    )
    for field in ("subject", "title", "body", "text", "summary"):
        text = payload.get(field)
        if isinstance(text, str) and text.strip().casefold().startswith(command_starts):
            return True
    return False


def _event_authorizes(
    event: dict[str, Any] | None,
    capability: str,
    *,
    work_id: str | None = None,
) -> bool:
    if not _trusted_event(event):
        return False
    payload = event.get("payload", {}) if event else {}
    if not isinstance(payload, dict):
        return False
    event_type = str(event.get("event_type", "")) if event else ""
    if event_type == "operator.request":
        if capability == "create":
            return True
        if capability == "execute_new":
            return payload.get("allow_internal_execution") is True
        return False
    if event_type in {"operator.work_authorized", "operator.work_updated"}:
        if str(payload.get("work_id", "")) != str(work_id or ""):
            return False
        raw_capabilities = payload.get("capabilities", [])
        return isinstance(raw_capabilities, list) and capability in raw_capabilities
    if event_type == "question.answered" and capability in {"update", "dispatch"}:
        blocking = payload.get("blocking_work_ids", [])
        return isinstance(blocking, list) and str(work_id or "") in map(str, blocking)
    if event_type.startswith("system."):
        authorized_ids = payload.get("authorized_work_ids", [])
        return (
            isinstance(authorized_ids, list)
            and str(work_id or "") in map(str, authorized_ids)
            and capability in {"update", "dispatch"}
        )
    return False


class Supervisor:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        llm: PlannerLLM,
        priority_engine: PriorityEngine,
        action_stager: ActionStager | None = None,
        leadership_guard: Callable[[], None] | None = None,
    ):
        self.config = config
        self.store = store
        self.llm = llm
        self.priority_engine = priority_engine
        self.action_stager = action_stager
        self.leadership_guard = leadership_guard or (lambda: None)
        self.worker_id = f"supervisor:{config.operator.instance_id}"
        self.verifier = ArtifactVerifier(config.verification)

    async def run_pass(
        self,
        *,
        trigger: str = "event",
        events: list[dict[str, Any]] | None = None,
        force_without_events: bool = False,
    ) -> PassResult | None:
        claimed_here = events is None
        if events is None:
            events = self.store.claim_events(
                self.worker_id,
                self.config.operator.max_events_per_pass,
                self.config.operator.event_lease_seconds,
            )
        if not events and not force_without_events:
            return None
        event_ids = [str(event["id"]) for event in events]
        if event_ids:
            fingerprint = hashlib.sha256("|".join(sorted(event_ids)).encode()).hexdigest()
            pass_id = f"pass_{fingerprint[:24]}"
        else:
            pass_id = new_id("pass")
        claim_token: str | None = None
        try:
            privileged_events = [event for event in events if _trusted_event(event)]
            if privileged_events and len(events) != 1:
                raise PlanValidationError(
                    "Privileged events must be processed in an isolated supervisor pass"
                )
            completion_events = [event for event in events if _completion_event(event)]
            if completion_events and len(events) != 1:
                raise PlanValidationError(
                    "Hermes completion evidence must be processed in an isolated supervisor pass"
                )
            claim_tokens = {
                str(event.get("claim_token"))
                for event in events
                if event.get("claim_token")
            }
            claim_token = next(iter(claim_tokens)) if len(claim_tokens) == 1 else None
            if claimed_here and event_ids and claim_token is None:
                raise StateConflict("Claimed event batch has no unique lease token")
            with self.store.transaction():
                self.leadership_guard()
                self.priority_engine.rescore_store(self.store)
            self._preflight_completion_events(events)
            snapshot = self._snapshot_for_events(
                self.store.snapshot(work_limit=150), events
            )
            user_prompt = self._build_context(trigger, events, snapshot)
            llm_result = await self.llm.generate_json(system=SUPERVISOR_SYSTEM_PROMPT, user=user_prompt)
            plan = self._validate_plan(llm_result.data, event_ids)
            plan_digest = hashlib.sha256(
                json.dumps(
                    plan,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if (
                len(plan["dispatch"])
                > self.config.operator.max_authorizations_per_pass
            ):
                raise PlanValidationError(
                    "Plan exceeds the configured dispatch-authorization limit"
                )
            if claimed_here and event_ids:
                # A worker whose lease expired while the model was running
                # must not apply stale output after another worker reclaimed
                # the same events.
                self.store.renew_event_claim(
                    event_ids,
                    claim_token=str(claim_token),
                    lease_seconds=self.config.operator.event_lease_seconds,
                )
            with self.store.transaction():
                # The guard reads the lease from this same immediate SQLite
                # transaction. A takeover therefore cannot interleave between
                # the fence check and application of authority-bearing output.
                self.leadership_guard()
                result = self._apply_plan(
                    pass_id,
                    plan_digest,
                    trigger,
                    events,
                    plan,
                    llm_result,
                )
                if claimed_here and event_ids:
                    self.store.finalize_supervisor_pass(
                        pass_id,
                        plan_digest,
                        event_ids,
                        result.event_dispositions,
                        claim_token=str(claim_token),
                        actor=self.worker_id,
                    )
                else:
                    self.store.set_state(
                        f"supervisor.pass:{pass_id}",
                        {
                            "pass_id": pass_id,
                            "plan_digest": plan_digest,
                            "event_ids": sorted(event_ids),
                            "finalized": True,
                            "finalized_at": utc_now(),
                        },
                    )
                self.store.set_state("supervisor.last_pass", result.to_dict())
                self.store.audit(
                    self.worker_id,
                    "supervisor.pass_completed",
                    entity_type="supervisor_pass",
                    entity_id=pass_id,
                    data=result.to_dict(),
                )
            return result
        except LeaseFenceLost:
            # Do not let a retired leader mutate queue or audit state after a
            # takeover. The event lease expires before the service lease and
            # can then be reclaimed by the current leader.
            raise
        except Exception as error:
            if claimed_here:
                try:
                    self.store.fail_events(
                        event_ids,
                        str(error),
                        max_attempts=self.config.operator.event_max_attempts,
                        claim_token=str(claim_token),
                    )
                except StateConflict:
                    logger.error("Event lease was lost while handling supervisor failure")
            self.store.audit(
                self.worker_id,
                "supervisor.pass_failed",
                entity_type="supervisor_pass",
                entity_id=pass_id,
                data={"trigger": trigger, "event_ids": event_ids, "error": str(error)[:2000]},
            )
            raise

    def _preflight_completion_events(self, events: list[dict[str, Any]]) -> None:
        """Attach a fresh deterministic report before the model assesses evidence.

        This report is advisory context at planning time.  The same verifier is
        run again after the event, card, fingerprint, and canonical run bindings
        have been validated, and only that later result controls completion.
        """

        for event in events:
            if not _completion_event(event):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            work_id = str(payload.get("work_id", ""))
            evidence = payload.get("execution_evidence", {})
            if not work_id or not isinstance(evidence, Mapping):
                continue
            try:
                work = self.store.get_work(work_id)
                report = self.verifier.verify(
                    work=work,
                    completion=evidence,
                ).to_dict()
            except Exception as error:
                report = {
                    "schema_version": 1,
                    "applicable": True,
                    "passed": False,
                    "artifacts": [],
                    "checks": [],
                    "errors": [f"deterministic verifier error: {str(error)[:1000]}"],
                    "verified_at": utc_now(),
                }
            payload["deterministic_verification"] = report

    @staticmethod
    def _snapshot_for_events(
        snapshot: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Remove untrusted prose from the context of an authority-bearing pass.

        Work IDs, state, hierarchy, and numeric scheduling signals remain
        available for coordination. Free-form text supplied by email, meeting,
        webhook, or model output is withheld so it cannot borrow a trusted
        event's authority through prompt injection.
        """

        if not any(
            _trusted_event(event) or _completion_event(event) for event in events
        ):
            return snapshot

        redacted = dict(snapshot)
        def safe_work_item(raw: Any) -> dict[str, Any]:
            item = dict(raw) if isinstance(raw, dict) else {}
            metadata = item.get("metadata", {})
            governance = (
                metadata.get("governance", {})
                if isinstance(metadata, dict)
                else {}
            )
            source_trust = (
                governance.get("source_trust")
                if isinstance(governance, dict)
                else None
            )
            trusted_semantics = bool(
                source_trust
                in {TrustLevel.OPERATOR.value, TrustLevel.SYSTEM.value}
                and isinstance(governance, dict)
                and governance.get("creation_authorized") is True
            )
            safe_item = {
                key: item.get(key)
                for key in (
                    "id",
                    "kind",
                    "status",
                    "parent_id",
                    "source_event_id",
                    "priority",
                    "priority_score",
                    "impact",
                    "urgency",
                    "strategic_alignment",
                    "unlock_value",
                    "risk",
                    "confidence",
                    "effort_minutes",
                    "due_at",
                    "scheduled_at",
                    "assignee",
                    "execution_mode",
                    "hermes_task_id",
                    "version",
                    "created_at",
                    "updated_at",
                    "completed_at",
                    "rollup",
                )
            }
            if trusted_semantics:
                # Preserve operator-authored task semantics, but never expose
                # the open-ended metadata container. Dispatcher results,
                # verifier summaries, and remote comments are mixed-trust even
                # when the original work item was operator-created.
                safe_item.update(
                    {
                        "title": item.get("title"),
                        "description": item.get("description"),
                        "acceptance_criteria": item.get("acceptance_criteria", []),
                        "governance": {
                            "source_trust": source_trust,
                            "creation_authorized": governance.get(
                                "creation_authorized"
                            ),
                            "execution_authorized": governance.get(
                                "execution_authorized"
                            ),
                        },
                        "mixed_trust_metadata_redacted": True,
                    }
                )
            else:
                safe_item["untrusted_text_redacted"] = True
            return safe_item

        redacted["work"] = [
            safe_work_item(raw) for raw in snapshot.get("work", [])
        ]
        redacted["completed_work"] = [
            safe_work_item(raw) for raw in snapshot.get("completed_work", [])
        ]
        redacted["questions"] = [
            {
                key: question.get(key)
                for key in (
                    "id",
                    "status",
                    "urgency",
                    "blocking_work_ids",
                    "created_at",
                )
            }
            | {"untrusted_text_redacted": True}
            for question in snapshot.get("questions", [])
            if isinstance(question, dict)
        ]
        redacted["promoted_memory"] = [
            {
                key: memory.get(key)
                for key in (
                    "id",
                    "category",
                    "status",
                    "trust_level",
                    "confidence",
                    "created_at",
                )
            }
            | {"untrusted_text_redacted": True}
            for memory in snapshot.get("promoted_memory", [])
            if isinstance(memory, dict)
            and memory.get("trust_level")
            not in {TrustLevel.OPERATOR.value, TrustLevel.SYSTEM.value}
        ] + [
            dict(memory)
            for memory in snapshot.get("promoted_memory", [])
            if isinstance(memory, dict)
            and memory.get("trust_level")
            in {TrustLevel.OPERATOR.value, TrustLevel.SYSTEM.value}
        ]
        redacted["active_runs"] = [
            {
                key: run.get(key)
                for key in (
                    "id",
                    "work_item_id",
                    "runner",
                    "external_run_id",
                    "status",
                    "attempt",
                    "started_at",
                    "heartbeat_at",
                    "finished_at",
                )
            }
            | {"mixed_trust_result_redacted": True}
            for run in snapshot.get("active_runs", [])
            if isinstance(run, dict)
        ]
        return redacted

    def _build_context(
        self,
        trigger: str,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> str:
        safe_events = []
        for event in events:
            payload_text = json.dumps(event.get("payload", {}), ensure_ascii=False)
            if len(payload_text) > 24_000:
                payload_text = payload_text[:24_000] + "...[truncated]"
            safe_events.append(
                {
                    "id": event["id"],
                    "source": event["source"],
                    "external_id": event.get("external_id"),
                    "event_type": event["event_type"],
                    "trust_level": event["trust_level"],
                    "received_at": event["created_at"],
                    "provenance": event.get("provenance", {}),
                    "payload_as_untrusted_evidence": payload_text,
                }
            )
        context = {
            "trigger": trigger,
            "current_time": utc_now(),
            "operator_timezone": self.config.operator.timezone,
            "autonomy_mode": self.config.operator.autonomy_mode,
            "external_action_mode": self.config.policy.external_action_mode,
            "new_events": safe_events,
            "operational_state": _bounded_context_value(snapshot),
            "constraints": {
                "max_new_work_items": 40,
                "max_questions": 10,
                "max_concurrent_executions": self.config.operator.max_parallel_work,
                "max_dispatch_authorizations": (
                    self.config.operator.max_authorizations_per_pass
                ),
                "Hermes_enabled": self.config.hermes.enabled,
                "Hermes_default_profile": self.config.hermes.default_assignee,
                "Hermes_orchestrator_profile": self.config.hermes.orchestrator_profile,
                "Hermes_allowed_profiles": sorted(
                    {
                        value
                        for value in (
                            self.config.hermes.profile,
                            self.config.hermes.default_assignee,
                            self.config.hermes.orchestrator_profile,
                            *self.config.hermes.allowed_profiles,
                        )
                        if value
                    }
                ),
                "Hermes_allowed_skills": sorted(
                    set(self.config.hermes.default_skills)
                    | set(self.config.hermes.allowed_skills)
                ),
                "Obsidian_enabled": self.config.obsidian.enabled,
                "shadow_mode_dispatches_are_recorded_but_not_executed": True,
            },
        }
        def serialize() -> str:
            return json.dumps(
                context,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )

        rendered = serialize()
        if len(rendered.encode("utf-8")) <= _MAX_CONTEXT_BYTES:
            return rendered

        truncation: dict[str, Any] = {
            "applied": True,
            "maximum_bytes": _MAX_CONTEXT_BYTES,
            "omitted_by_section": {},
        }
        context["context_truncation"] = truncation
        state = context["operational_state"]
        assert isinstance(state, dict)
        # Remove least-critical historical context first. Work and questions
        # are already ordered by operational value, so removals come from the
        # tail and preserve the highest-ranked entries.
        for section in (
            "promoted_memory",
            "completed_work",
            "work_links",
            "active_runs",
            "questions",
            "work",
        ):
            values = state.get(section)
            if not isinstance(values, list):
                continue
            omitted = 0
            while values and len(serialize().encode("utf-8")) > _MAX_CONTEXT_BYTES:
                values.pop()
                omitted += 1
            if omitted:
                truncation["omitted_by_section"][section] = omitted
            if len(serialize().encode("utf-8")) <= _MAX_CONTEXT_BYTES:
                return serialize()

        # Event bodies have already been individually capped. Reduce their
        # evidence previews if the aggregate event batch still exceeds budget.
        for event in context["new_events"]:
            evidence = str(event.get("payload_as_untrusted_evidence", ""))
            if len(evidence) > 2_000:
                event["payload_as_untrusted_evidence"] = (
                    evidence[:2_000] + "...[aggregate context truncated]"
                )
        rendered = serialize()
        if len(rendered.encode("utf-8")) <= _MAX_CONTEXT_BYTES:
            return rendered

        # This final fallback retains counts and new-event identities, rather
        # than failing every future pass because old state grew too large.
        context["operational_state"] = {
            "context_truncated": True,
            "work_counts": state.get("work_counts", {}),
            "event_counts": state.get("event_counts", {}),
            "memory_review_counts": state.get("memory_review_counts", {}),
        }
        truncation["operational_state_replaced"] = True
        return serialize()

    def _validate_plan(
        self, plan: dict[str, Any], event_ids: list[str]
    ) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise PlanValidationError("Plan must be an object")
        raw_dispositions = plan.get("event_dispositions")
        if raw_dispositions is None:
            # Compatibility for pre-v8 planners: a disposition may be derived
            # only from an explicit event-bound effect. An empty plan is never
            # inferred as non-actionable, which closes the silent-consumption
            # failure while allowing an in-flight model upgrade to converge.
            inferred: list[dict[str, Any]] = []
            for event_id in event_ids:
                related_ids: list[str] = []
                related_refs: list[str] = []
                outcome: str | None = None
                for operation in plan.get("work_operations", []):
                    if str(operation.get("source_event_id", "")) != event_id:
                        continue
                    outcome = "work_recorded"
                    if operation.get("work_id"):
                        related_ids.append(str(operation["work_id"]))
                    if operation.get("ref"):
                        related_refs.append(str(operation["ref"]))
                for dispatch in plan.get("dispatch", []):
                    if str(dispatch.get("source_event_id", "")) != event_id:
                        continue
                    outcome = outcome or "work_recorded"
                    if dispatch.get("work_id"):
                        related_ids.append(str(dispatch["work_id"]))
                    if dispatch.get("work_ref"):
                        related_refs.append(str(dispatch["work_ref"]))
                if any(
                    str(question.get("source_event_id", "")) == event_id
                    for question in plan.get("questions", [])
                ):
                    outcome = outcome or "question_requested"
                if any(
                    str(candidate.get("source_event_id", "")) == event_id
                    for candidate in plan.get("memory_candidates", [])
                ):
                    outcome = outcome or "memory_recorded"
                if any(
                    str(proposal.get("source_event_id", "")) == event_id
                    for proposal in plan.get("external_action_proposals", [])
                ):
                    outcome = outcome or "external_action_proposed"
                if len(event_ids) == 1 and plan.get("verifications"):
                    outcome = outcome or "execution_reconciled"
                if outcome:
                    inferred.append(
                        {
                            "event_id": event_id,
                            "disposition": outcome,
                            "reason": (
                                "Deterministically inferred from an explicit "
                                "event-bound effect in a legacy planner response"
                            ),
                            "related_work_ids": related_ids,
                            "related_work_refs": related_refs,
                            "_compatibility_inferred": True,
                        }
                    )
            raw_dispositions = inferred
        normalized: dict[str, Any] = {
            "summary": str(plan.get("summary", "")).strip(),
            "observations": [str(item) for item in plan.get("observations", [])][:50],
            "event_dispositions": list(raw_dispositions or []),
            "work_operations": list(plan.get("work_operations", [])),
            "questions": list(plan.get("questions", [])),
            "dispatch": list(plan.get("dispatch", [])),
            "memory_candidates": list(plan.get("memory_candidates", [])),
            "verifications": list(plan.get("verifications", [])),
            "external_action_proposals": list(plan.get("external_action_proposals", [])),
        }
        limits = {
            "event_dispositions": max(len(event_ids), 1),
            "work_operations": 80,
            "questions": 10,
            "dispatch": self.config.operator.max_authorizations_per_pass,
            "memory_candidates": 30,
            "verifications": 20,
            "external_action_proposals": 20,
        }
        for key, maximum in limits.items():
            if len(normalized[key]) > maximum:
                raise PlanValidationError(f"Plan contains too many {key}")
            if not all(isinstance(item, dict) for item in normalized[key]):
                raise PlanValidationError(f"Every {key} entry must be an object")
        disposition_ids: list[str] = []
        allowed_dispositions = {
            "work_recorded",
            "question_requested",
            "execution_reconciled",
            "memory_recorded",
            "external_action_proposed",
            "duplicate",
            "non_actionable",
            "quarantined",
        }
        for disposition in normalized["event_dispositions"]:
            event_id = _optional_text(disposition.get("event_id"))
            disposition_value = _optional_text(disposition.get("disposition"))
            reason = _optional_text(disposition.get("reason"))
            if not event_id or event_id not in event_ids:
                raise PlanValidationError(
                    f"Event disposition has an unknown event_id: {event_id}"
                )
            if disposition_value not in allowed_dispositions:
                raise PlanValidationError(
                    f"Unsupported event disposition: {disposition_value}"
                )
            if not reason:
                raise PlanValidationError(
                    f"Event disposition for {event_id} needs a reason"
                )
            if len(reason) > 4000:
                raise PlanValidationError("Event disposition reason is too long")
            for field in ("related_work_ids", "related_work_refs"):
                values = disposition.get(field, [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) and value.strip() for value in values
                ):
                    raise PlanValidationError(
                        f"Event disposition {field} must be a list of IDs"
                    )
                disposition[field] = list(dict.fromkeys(map(str, values)))[:100]
            disposition_ids.append(event_id)
        refs: set[str] = set()
        creates = 0
        for operation in normalized["work_operations"]:
            op = operation.get("op")
            if op not in {"create", "update", "link"}:
                raise PlanValidationError(f"Unsupported work operation: {op}")
            if op == "create":
                creates += 1
                ref = _optional_text(operation.get("ref"))
                if not ref or ref in refs:
                    raise PlanValidationError("Every create operation needs a unique ref")
                refs.add(ref)
                if not _optional_text(operation.get("title")):
                    raise PlanValidationError(f"Work ref {ref} has no title")
                if _optional_text(operation.get("parent_id")):
                    operation["parent_version"] = _required_version(
                        operation.get("parent_version"),
                        f"Work ref {ref} parent_version",
                    )
                source_event = _optional_text(operation.get("source_event_id"))
                if source_event and source_event not in event_ids:
                    raise PlanValidationError(f"Unknown source_event_id: {source_event}")
            elif op == "update":
                work_id = _optional_text(operation.get("work_id"))
                if not work_id:
                    raise PlanValidationError("Update operation needs a work_id")
                operation["expected_version"] = _required_version(
                    operation.get("expected_version"),
                    f"Update {work_id} expected_version",
                )
                source_event = _optional_text(operation.get("source_event_id"))
                if source_event and source_event not in event_ids:
                    raise PlanValidationError(f"Unknown source_event_id: {source_event}")
            elif op == "link":
                source_event = _optional_text(operation.get("source_event_id"))
                if source_event and source_event not in event_ids:
                    raise PlanValidationError(
                        f"Unknown source_event_id: {source_event}"
                    )
                if _optional_text(operation.get("from_id")):
                    operation["expected_from_version"] = _required_version(
                        operation.get("expected_from_version"),
                        "Link expected_from_version",
                    )
                if _optional_text(operation.get("to_id")):
                    operation["expected_to_version"] = _required_version(
                        operation.get("expected_to_version"),
                        "Link expected_to_version",
                    )
        if creates > 40:
            raise PlanValidationError("Plan creates more than 40 work items")
        for dispatch in normalized["dispatch"]:
            work_id = _optional_text(dispatch.get("work_id"))
            if work_id:
                dispatch["expected_version"] = _required_version(
                    dispatch.get("expected_version"),
                    f"Dispatch {work_id} expected_version",
                )
        for verification in normalized["verifications"]:
            work_id = _optional_text(verification.get("work_id"))
            if not work_id:
                raise PlanValidationError("Verification needs a work_id")
            verification["expected_version"] = _required_version(
                verification.get("expected_version"),
                f"Verification {work_id} expected_version",
            )
        for question in normalized["questions"]:
            versions = question.get("blocking_work_versions", {})
            if not isinstance(versions, dict):
                raise PlanValidationError(
                    "Question blocking_work_versions must be an object"
                )
            question["blocking_work_versions"] = {
                str(work_id): _required_version(
                    version, f"Question blocking version for {work_id}"
                )
                for work_id, version in versions.items()
            }
        for proposal in normalized["external_action_proposals"]:
            source_event = _optional_text(proposal.get("source_event_id"))
            if not source_event or source_event not in event_ids:
                raise PlanValidationError(
                    "Every external action proposal needs a source_event_id from this pass"
                )
        if (
            len(disposition_ids) != len(event_ids)
            or len(set(disposition_ids)) != len(disposition_ids)
            or set(disposition_ids) != set(event_ids)
        ):
            raise PlanValidationError(
                "Every event needs exactly one explicit disposition"
            )
        return normalized

    def _resolve_event_dispositions(
        self,
        *,
        plan: dict[str, Any],
        events: list[dict[str, Any]],
        references: dict[str, str],
        result: PassResult,
    ) -> list[dict[str, Any]]:
        """Bind model dispositions to effects that actually survived policy."""

        event_by_id = {str(event["id"]): event for event in events}
        work_effects: dict[str, set[str]] = {event_id: set() for event_id in event_by_id}
        question_effects: dict[str, list[str]] = {
            event_id: [] for event_id in event_by_id
        }
        memory_effects: set[str] = set()
        external_effects: set[str] = set()
        verification_effects: set[str] = set()

        for operation in plan["work_operations"]:
            source_event_id = _optional_text(operation.get("source_event_id"))
            if not source_event_id or source_event_id not in work_effects:
                continue
            if operation["op"] == "create":
                resolved = references.get(str(operation.get("ref", "")))
                if resolved:
                    work_effects[source_event_id].add(resolved)
            elif operation["op"] == "update":
                work_id = str(operation.get("work_id", ""))
                if work_id in result.updated_work_ids:
                    work_effects[source_event_id].add(work_id)
            elif operation["op"] == "link":
                from_id = _optional_text(operation.get("from_id")) or references.get(
                    str(operation.get("from_ref", ""))
                )
                to_id = _optional_text(operation.get("to_id")) or references.get(
                    str(operation.get("to_ref", ""))
                )
                if from_id and to_id:
                    relation = str(
                        operation.get("relation", WorkRelation.RELATED_TO.value)
                    )
                    with self.store.connection() as connection:
                        linked = connection.execute(
                            "SELECT 1 FROM work_links WHERE from_id = ? "
                            "AND to_id = ? AND relation = ?",
                            (from_id, to_id, relation),
                        ).fetchone()
                    if linked is not None:
                        work_effects[source_event_id].update((from_id, to_id))

        for dispatch in plan["dispatch"]:
            source_event_id = _optional_text(dispatch.get("source_event_id"))
            if not source_event_id or source_event_id not in work_effects:
                continue
            work_id = _optional_text(dispatch.get("work_id")) or references.get(
                str(dispatch.get("work_ref", ""))
            )
            if work_id and work_id in result.dispatch_work_ids:
                work_effects[source_event_id].add(work_id)

        for proposal in plan["questions"]:
            source_event_id = _optional_text(proposal.get("source_event_id"))
            if not source_event_id or source_event_id not in question_effects:
                continue
            blocking = [
                references.get(str(value), str(value))
                for value in proposal.get("blocking_work_ids", [])
            ]
            identity = json.dumps(
                {
                    "question": str(proposal.get("question", "")).strip(),
                    "context": str(proposal.get("context", "")).strip(),
                    "blocking_work_ids": sorted(map(str, blocking)),
                    "source_event_id": source_event_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            question_id = "qst_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            if question_id in result.question_ids:
                question_effects[source_event_id].append(question_id)

        for candidate in plan["memory_candidates"]:
            source_event_id = _optional_text(candidate.get("source_event_id"))
            if source_event_id:
                memory_effects.add(source_event_id)
        for proposal in plan["external_action_proposals"]:
            source_event_id = _optional_text(proposal.get("source_event_id"))
            if source_event_id:
                external_effects.add(source_event_id)
        for event_id, event in event_by_id.items():
            payload = event.get("payload", {})
            work_id = str(payload.get("work_id", "")) if isinstance(payload, dict) else ""
            if any(
                str(verification.get("work_id", "")) == work_id
                for verification in plan["verifications"]
            ):
                verification_effects.add(event_id)

        resolved_dispositions: list[dict[str, Any]] = []
        for disposition in plan["event_dispositions"]:
            event_id = str(disposition["event_id"])
            outcome = str(disposition["disposition"])
            related_work_ids = set(
                map(str, disposition.get("related_work_ids", []))
            )
            related_work_ids.update(
                references[ref]
                for ref in disposition.get("related_work_refs", [])
                if ref in references
            )
            related_work_ids.update(work_effects[event_id])
            for work_id in related_work_ids:
                try:
                    self.store.get_work(work_id)
                except NotFound as error:
                    raise PlanValidationError(
                        f"Disposition for {event_id} references missing work {work_id}"
                    ) from error
            related_question_ids = question_effects[event_id]
            if (
                disposition.get("_compatibility_inferred") is True
                and outcome == "work_recorded"
                and not work_effects[event_id]
            ):
                outcome = "quarantined"
            if outcome == "work_recorded" and not work_effects[event_id]:
                raise PlanValidationError(
                    f"Work disposition for {event_id} has no applied work effect"
                )
            if outcome == "question_requested" and not related_question_ids:
                raise PlanValidationError(
                    f"Question disposition for {event_id} has no created question"
                )
            if outcome == "execution_reconciled" and event_id not in verification_effects:
                raise PlanValidationError(
                    f"Execution disposition for {event_id} has no verification"
                )
            if outcome == "memory_recorded" and event_id not in memory_effects:
                raise PlanValidationError(
                    f"Memory disposition for {event_id} has no memory candidate"
                )
            if outcome == "external_action_proposed" and event_id not in external_effects:
                raise PlanValidationError(
                    f"External-action disposition for {event_id} has no proposal"
                )
            if outcome == "duplicate" and not related_work_ids:
                raise PlanValidationError(
                    f"Duplicate disposition for {event_id} needs related work"
                )
            event = event_by_id[event_id]
            if outcome == "non_actionable" and _task_like_event(event):
                raise PlanValidationError(
                    f"Task-like event {event_id} cannot be dismissed as non-actionable"
                )
            resolved_dispositions.append(
                {
                    "event_id": event_id,
                    "disposition": outcome,
                    "reason": str(disposition["reason"]).strip(),
                    "related_work_ids": sorted(related_work_ids),
                    "related_question_ids": related_question_ids,
                    "metadata": {
                        "source": event.get("source"),
                        "event_type": event.get("event_type"),
                        "trust_level": event.get("trust_level"),
                    },
                }
            )
        return resolved_dispositions

    def _apply_plan(
        self,
        pass_id: str,
        plan_digest: str,
        trigger: str,
        events: list[dict[str, Any]],
        plan: dict[str, Any],
        llm_result: LLMResult,
    ) -> PassResult:
        event_ids = [str(event["id"]) for event in events]
        event_by_id = {str(event["id"]): event for event in events}
        result = PassResult(
            pass_id=pass_id,
            trigger=trigger,
            event_ids=event_ids,
            summary=plan["summary"],
            observations=plan["observations"],
            llm_model=llm_result.model,
            llm_usage=llm_result.usage,
        )
        references: dict[str, str] = {}
        reference_update_authority: dict[str, bool] = {}
        reference_execution_authority: dict[str, bool] = {}
        reference_reused: dict[str, bool] = {}
        version_lineage: dict[str, tuple[int, int]] = {}

        def effective_version(work_id: str, planned_version: int) -> int:
            lineage = version_lineage.get(work_id)
            if lineage is None:
                return planned_version
            initial_version, current_version = lineage
            if planned_version != initial_version:
                raise StateConflict(
                    f"Plan contains inconsistent snapshot versions for {work_id}"
                )
            return current_version

        def record_version(
            work_id: str,
            planned_version: int,
            current_version: int,
        ) -> None:
            initial_version = version_lineage.get(
                work_id,
                (planned_version, planned_version),
            )[0]
            version_lineage[work_id] = (initial_version, current_version)

        creates = [op for op in plan["work_operations"] if op["op"] == "create"]
        pending = list(enumerate(creates))
        while pending:
            progressed = False
            for position, operation in pending[:]:
                parent_ref = _optional_text(operation.get("parent_ref"))
                if parent_ref and parent_ref not in references:
                    continue
                ref = str(operation["ref"])
                supplied_key = _optional_text(operation.get("idempotency_key"))
                fallback_material = {
                    "kind": operation.get("kind", WorkKind.TASK.value),
                    "title": str(operation.get("title", "")).strip().casefold(),
                    "parent_id": operation.get("parent_id"),
                    "parent_ref": operation.get("parent_ref"),
                    "source_event_id": operation.get("source_event_id") if events else None,
                }
                semantic_key = supplied_key or json.dumps(
                    fallback_material,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if len(semantic_key) > 512:
                    raise PlanValidationError("Work idempotency key is too long")
                deterministic = hashlib.sha256(
                    f"work:{semantic_key}".encode()
                ).hexdigest()[:24]
                work_id = f"wrk_{deterministic}"
                source_event_id = _optional_text(operation.get("source_event_id"))
                source_event = event_by_id.get(source_event_id or "")
                trust = (
                    source_event.get("trust_level")
                    if source_event
                    else TrustLevel.UNTRUSTED.value
                )
                source_can_create = (
                    _event_authorizes(source_event, "create") if events else False
                )
                source_can_execute = (
                    _event_authorizes(source_event, "execute_new") if events else False
                )
                parent_id = _optional_text(operation.get("parent_id")) or (
                    references.get(parent_ref) if parent_ref else None
                )
                if _optional_text(operation.get("parent_id")):
                    parent = self.store.get_work(str(parent_id))
                    if parent.version != int(operation["parent_version"]):
                        raise StateConflict(
                            f"Parent changed while work was being planned: {parent_id}"
                        )
                if (
                    events
                    and _optional_text(operation.get("parent_id"))
                    and not source_can_create
                    and not _event_authorizes(
                        source_event, "update", work_id=str(parent_id)
                    )
                ):
                    parent_id = None
                if parent_ref and reference_reused.get(parent_ref, False):
                    parent_version = operation.get("parent_version")
                    parent_authorized = reference_update_authority.get(
                        parent_ref,
                        False,
                    )
                    if not parent_authorized or parent_version is None:
                        parent_id = None
                    else:
                        expected_parent_version = _required_version(
                            parent_version,
                            f"Work ref {ref} reused parent_version",
                        )
                        parent = self.store.get_work(str(parent_id))
                        if parent.version != expected_parent_version:
                            raise StateConflict(
                                f"Parent changed while work was being planned: {parent_id}"
                            )
                provenance = {
                    "supervisor_pass": pass_id,
                    "source_event_id": source_event_id,
                    "source": source_event.get("source") if source_event else "supervisor",
                    "trust_level": trust,
                }
                criteria = [
                    str(value).strip()
                    for value in operation.get("acceptance_criteria", [])
                    if str(value).strip()
                ]
                requested_status = WorkStatus(
                    operation.get("status", WorkStatus.TRIAGE.value)
                )
                if requested_status not in {
                    WorkStatus.INBOX,
                    WorkStatus.TRIAGE,
                    WorkStatus.PLANNED,
                    WorkStatus.READY,
                    WorkStatus.WAITING_INPUT,
                    WorkStatus.BLOCKED,
                }:
                    raise PlanValidationError(
                        f"New work cannot start in {requested_status.value}"
                    )
                requested_execution = ExecutionMode(
                    operation.get("execution_mode", ExecutionMode.NONE.value)
                )
                effective_status = (
                    requested_status if source_can_create else WorkStatus.TRIAGE
                )
                effective_execution = (
                    requested_execution
                    if source_can_execute
                    else ExecutionMode.NONE
                )
                effective_assignee = (
                    _optional_text(operation.get("assignee"))
                    if source_can_execute
                    else None
                )
                kind = WorkKind(operation.get("kind", WorkKind.TASK.value))
                title = str(operation["title"]).strip()
                description = str(operation.get("description", ""))
                due_at = _validate_timestamp(operation.get("due_at"))
                scheduled_at = _validate_timestamp(operation.get("scheduled_at"))
                recurrence_rule = _validate_recurrence(
                    operation.get("recurrence_rule")
                )
                if recurrence_rule is not None and (
                    kind != WorkKind.REMINDER or due_at is None
                ):
                    raise PlanValidationError(
                        "recurrence_rule requires reminder work with due_at"
                    )
                impact = _bounded_factor(operation.get("impact"))
                urgency = _bounded_factor(operation.get("urgency"))
                alignment = _bounded_factor(operation.get("strategic_alignment"))
                unlock_value = _bounded_factor(operation.get("unlock_value"), 0.0)
                risk = _bounded_factor(operation.get("risk"), 0.0)
                confidence = _bounded_factor(operation.get("confidence"))
                effort_minutes = max(
                    0,
                    min(100_000, int(operation.get("effort_minutes", 30))),
                )
                creation_identity = {
                    "idempotency_key": semantic_key,
                    "plan_ref": ref,
                    "source_event_id": source_event_id,
                    "kind": kind.value,
                    "title": title,
                    "description": description,
                    "parent_id": parent_id,
                    "status": effective_status.value,
                    "execution_mode": effective_execution.value,
                    "assignee": effective_assignee,
                    "acceptance_criteria": criteria,
                    "due_at": due_at,
                    "scheduled_at": scheduled_at,
                    "recurrence_rule": recurrence_rule,
                    "impact": impact,
                    "urgency": urgency,
                    "strategic_alignment": alignment,
                    "unlock_value": unlock_value,
                    "risk": risk,
                    "confidence": confidence,
                    "effort_minutes": effort_minutes,
                }
                creation_identity_digest = hashlib.sha256(
                    json.dumps(
                        creation_identity,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                semantic_identity_digest = hashlib.sha256(
                    json.dumps(
                        {
                            "idempotency_key": semantic_key,
                            "kind": kind.value,
                            "title": title,
                            "parent_id": parent_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                try:
                    item = self.store.get_work(work_id)
                except NotFound:
                    model_metadata = _sanitized_metadata(operation.get("metadata", {}))
                    item = WorkItem(
                        id=work_id,
                        title=title,
                        kind=kind,
                        description=description,
                        status=effective_status,
                        parent_id=parent_id,
                        source_event_id=source_event_id,
                        provenance=provenance,
                        impact=impact,
                        urgency=urgency,
                        strategic_alignment=alignment,
                        unlock_value=unlock_value,
                        risk=risk,
                        confidence=confidence,
                        effort_minutes=effort_minutes,
                        due_at=due_at,
                        scheduled_at=scheduled_at,
                        recurrence_rule=recurrence_rule,
                        assignee=effective_assignee,
                        execution_mode=effective_execution,
                        acceptance_criteria=criteria,
                        metadata={
                            **model_metadata,
                            "supervisor_pass": pass_id,
                            "plan_ref": ref,
                            "idempotency_key": semantic_key,
                            "creation_identity_digest": creation_identity_digest,
                            "semantic_identity_digest": semantic_identity_digest,
                            "governance": {
                                "source_trust": trust,
                                "creation_authorized": source_can_create,
                                "execution_authorized": source_can_execute,
                            },
                        },
                    )
                    priority = self.priority_engine.score(item)
                    item.priority_score = priority.score
                    item.priority_rationale = priority.rationale
                    self.store.create_work(item, actor=self.worker_id)
                    result.created_work_ids.append(item.id)
                metadata = item.metadata
                same_creation = (
                    item.kind == kind
                    and item.source_event_id == source_event_id
                    and metadata.get("supervisor_pass") == pass_id
                    and metadata.get("plan_ref") == ref
                    and metadata.get("idempotency_key") == semantic_key
                    and metadata.get("creation_identity_digest")
                    == creation_identity_digest
                )
                semantic_reuse = (
                    item.kind == kind
                    and metadata.get("idempotency_key") == semantic_key
                    and metadata.get("semantic_identity_digest")
                    == semantic_identity_digest
                )
                if not same_creation and not semantic_reuse:
                    raise PlanValidationError(
                        f"Idempotency key for {ref} conflicts with existing work identity"
                    )
                references[ref] = item.id
                governance = item.metadata.get("governance", {})
                reference_update_authority[ref] = bool(
                    (same_creation and source_can_create)
                    or (
                        semantic_reuse
                        and _event_authorizes(
                            source_event,
                            "update",
                            work_id=item.id,
                        )
                    )
                )
                reference_execution_authority[ref] = bool(
                    same_creation
                    and source_can_execute
                    and isinstance(governance, dict)
                    and governance.get("execution_authorized") is True
                )
                reference_reused[ref] = not same_creation
                pending.remove((position, operation))
                progressed = True
            if not progressed:
                unresolved = [str(op.get("parent_ref")) for _, op in pending]
                raise PlanValidationError(f"Unresolved or cyclic parent refs: {unresolved}")

        for operation in plan["work_operations"]:
            if operation["op"] == "update":
                work_id = str(operation.get("work_id", ""))
                changes = dict(operation.get("changes", {}))
                protected = {"id", "provenance", "source_event_id", "hermes_task_id"}
                for key in protected:
                    changes.pop(key, None)
                work_before = self.store.get_work(work_id)
                planned_version = int(operation["expected_version"])
                expected_version = effective_version(work_id, planned_version)
                source_event_id = _optional_text(operation.get("source_event_id"))
                source_event = event_by_id.get(source_event_id or "")
                operation_trusted = _event_authorizes(
                    source_event, "update", work_id=work_id
                )
                trusted_source_authorization = _event_authorizes(
                    source_event, "dispatch", work_id=work_id
                )
                governance = work_before.metadata.get("governance", {})
                execution_authorized = bool(
                    isinstance(governance, dict)
                    and governance.get("execution_authorized") is True
                )
                if (
                    source_event
                    and source_event.get("event_type") == "question.answered"
                    and not execution_authorized
                ):
                    # An answer can resume previously authorized work. It does
                    # not turn a planning-only item into executable work.
                    trusted_source_authorization = False
                if events and not operation_trusted:
                    self.store.audit(
                        self.worker_id,
                        "work.update_quarantined",
                        entity_type="work",
                        entity_id=work_id,
                        data={"source_event_id": source_event_id},
                    )
                    continue
                if not events:
                    safe_reconciliation_fields = {
                        "priority",
                        "impact",
                        "urgency",
                        "strategic_alignment",
                        "unlock_value",
                        "risk",
                        "confidence",
                        "effort_minutes",
                    }
                    forbidden = set(changes) - safe_reconciliation_fields
                    if forbidden:
                        self.store.audit(
                            self.worker_id,
                            "work.reconciliation_update_quarantined",
                            entity_type="work",
                            entity_id=work_id,
                            data={"forbidden_fields": sorted(forbidden)},
                        )
                        continue
                if (
                    not execution_authorized
                    and not trusted_source_authorization
                    and (
                        changes.get("status") in {
                            WorkStatus.RUNNING.value,
                            WorkStatus.REVIEW.value,
                        }
                        or changes.get("execution_mode")
                        == ExecutionMode.HERMES.value
                    )
                ):
                    raise PlanValidationError(
                        f"Work {work_id} needs trusted operator authorization before execution"
                    )
                model_metadata = _sanitized_metadata(changes.get("metadata", {}))
                if "metadata" in changes:
                    merged_metadata = dict(work_before.metadata)
                    merged_metadata.update(model_metadata)
                    changes["metadata"] = merged_metadata
                if trusted_source_authorization and (
                    changes.get("status") == WorkStatus.READY.value
                    or changes.get("execution_mode") == ExecutionMode.HERMES.value
                ):
                    merged_metadata = dict(changes.get("metadata", work_before.metadata))
                    merged_metadata["governance"] = {
                        "source_trust": (
                            source_event.get("trust_level")
                            if source_event
                            else governance.get("source_trust", TrustLevel.SYSTEM.value)
                            if isinstance(governance, dict)
                            else TrustLevel.SYSTEM.value
                        ),
                        "creation_authorized": bool(
                            governance.get("creation_authorized") is True
                            or trusted_source_authorization
                        ),
                        "execution_authorized": True,
                    }
                    changes["metadata"] = merged_metadata
                target_status = changes.get("status")
                trusted_terminal_event = any(
                    _event_authorizes(event, "update", work_id=work_id)
                    and str(event.get("payload", {}).get("work_id", "")) == work_id
                    for event in events
                )
                if target_status in {
                    WorkStatus.DONE.value,
                    WorkStatus.CANCELLED.value,
                    WorkStatus.ARCHIVED.value,
                } and not trusted_terminal_event:
                    raise PlanValidationError(
                        f"Terminal transition for {work_id} needs trusted operator or verification evidence"
                    )
                updated = self.store.update_work(
                    work_id,
                    changes,
                    actor=self.worker_id,
                    expected_version=expected_version,
                )
                record_version(work_id, planned_version, updated.version)
                result.updated_work_ids.append(updated.id)
            elif operation["op"] == "link":
                if not events:
                    self.store.audit(
                        self.worker_id,
                        "work.reconciliation_link_quarantined",
                        entity_type="supervisor_pass",
                        entity_id=pass_id,
                    )
                    continue
                from_id = _optional_text(operation.get("from_id")) or references.get(
                    str(operation.get("from_ref", ""))
                )
                to_id = _optional_text(operation.get("to_id")) or references.get(
                    str(operation.get("to_ref", ""))
                )
                if not from_id or not to_id:
                    raise PlanValidationError("Link operation contains an unresolved work reference")
                relation = WorkRelation(
                    operation.get("relation", WorkRelation.RELATED_TO.value)
                )
                from_ref = str(operation.get("from_ref", ""))
                to_ref = str(operation.get("to_ref", ""))
                link_source_id = _optional_text(operation.get("source_event_id"))
                link_source = event_by_id.get(link_source_id or "")
                from_authorized = (
                    reference_update_authority.get(from_ref, False)
                    if from_ref
                    else _event_authorizes(link_source, "update", work_id=from_id)
                )
                to_authorized = (
                    reference_update_authority.get(to_ref, False)
                    if to_ref
                    else _event_authorizes(link_source, "update", work_id=to_id)
                )
                if not (from_authorized and to_authorized):
                    self.store.audit(
                        self.worker_id,
                        "work.link_quarantined",
                        entity_type="work",
                        entity_id=from_id,
                        data={
                            "to_id": to_id,
                            "relation": relation.value,
                            "source_event_id": link_source_id,
                        },
                    )
                    continue
                self.store.add_work_link(
                    from_id,
                    to_id,
                    relation,
                    actor=self.worker_id,
                    expected_from_version=(
                        effective_version(
                            from_id,
                            int(operation["expected_from_version"]),
                        )
                        if _optional_text(operation.get("from_id"))
                        else None
                    ),
                    expected_to_version=(
                        effective_version(
                            to_id,
                            int(operation["expected_to_version"]),
                        )
                        if _optional_text(operation.get("to_id"))
                        else None
                    ),
                )

        for index, proposal in enumerate(plan["questions"]):
            blocking = [references.get(value, value) for value in proposal.get("blocking_work_ids", [])]
            question_source_id = _optional_text(proposal.get("source_event_id"))
            question_identity = json.dumps(
                {
                    "question": str(proposal.get("question", "")).strip(),
                    "context": str(proposal.get("context", "")).strip(),
                    "blocking_work_ids": sorted(map(str, blocking)),
                    "source_event_id": question_source_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            deterministic = hashlib.sha256(question_identity.encode()).hexdigest()[:24]
            question_id = f"qst_{deterministic}"
            question_source = event_by_id.get(question_source_id or "")
            blocking_versions: dict[str, int | None] = {}
            if events:
                allowed_blocking: list[str] = []
                for raw, work_id in zip(
                    proposal.get("blocking_work_ids", []), blocking
                ):
                    raw_text = str(raw)
                    authorized = reference_update_authority.get(raw_text, False) or _event_authorizes(
                        question_source, "update", work_id=str(work_id)
                    )
                    if not authorized:
                        continue
                    if raw_text in references:
                        blocking_versions[str(work_id)] = None
                        allowed_blocking.append(str(work_id))
                        continue
                    expected = proposal["blocking_work_versions"].get(
                        str(work_id)
                    )
                    if expected is None:
                        self.store.audit(
                            self.worker_id,
                            "question.blocking_work_missing_version",
                            entity_type="work",
                            entity_id=str(work_id),
                        )
                        continue
                    blocking_versions[str(work_id)] = effective_version(
                        str(work_id),
                        int(expected),
                    )
                    allowed_blocking.append(str(work_id))
                blocking = allowed_blocking
            else:
                blocking = []
            try:
                try:
                    self.store.get_question(question_id)
                    result.question_ids.append(question_id)
                    continue
                except NotFound:
                    pass
                question = UserQuestion(
                    id=question_id,
                    question=str(proposal.get("question", "")).strip(),
                    context=str(proposal.get("context", "")),
                    urgency=_bounded_factor(proposal.get("urgency")),
                    blocking_work_ids=blocking,
                )
                if not question.question:
                    continue
                self.store.create_question(question, actor=self.worker_id)
                result.question_ids.append(question.id)
                for work_id in blocking:
                    try:
                        work = self.store.get_work(work_id)
                        if work.status not in {WorkStatus.DONE, WorkStatus.CANCELLED, WorkStatus.ARCHIVED}:
                            updated = self.store.update_work(
                                work_id,
                                {"status": WorkStatus.WAITING_INPUT.value},
                                actor=self.worker_id,
                                expected_version=(
                                    blocking_versions.get(str(work_id))
                                    or work.version
                                ),
                                allow_transition_override=True,
                            )
                            planned_version = int(
                                proposal["blocking_work_versions"].get(
                                    str(work_id),
                                    work.version,
                                )
                            )
                            record_version(
                                str(work_id),
                                planned_version,
                                updated.version,
                            )
                    except NotFound:
                        logger.warning("Question referenced missing work item %s", work_id)
            except Exception:
                logger.exception("Could not create question %s", question_id)
                raise

        for candidate_index, candidate in enumerate(plan["memory_candidates"]):
            source_event_id = _optional_text(candidate.get("source_event_id"))
            source = event_by_id.get(source_event_id or "")
            source_trust = TrustLevel(source["trust_level"]) if source else TrustLevel.UNTRUSTED
            claimed_trust = TrustLevel(candidate.get("trust_level", TrustLevel.UNTRUSTED.value))
            trust_order = {
                TrustLevel.UNTRUSTED: 0,
                TrustLevel.AUTHENTICATED_UNTRUSTED: 1,
                TrustLevel.OPERATOR: 2,
                TrustLevel.SYSTEM: 3,
            }
            effective_trust = min(
                (source_trust, claimed_trust), key=lambda value: trust_order[value]
            )
            status = "pending" if effective_trust in {TrustLevel.OPERATOR, TrustLevel.SYSTEM} else "quarantined"
            if not self.config.policy.allow_memory_auto_promotion:
                status = "quarantined" if effective_trust not in {TrustLevel.OPERATOR, TrustLevel.SYSTEM} else "pending"
            category = str(candidate.get("category", "fact"))
            content = str(candidate.get("content", "")).strip()
            memory_identity = json.dumps(
                {
                    "category": category,
                    "content": content,
                    "source_event_id": source_event_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            candidate_id = self.store.save_memory_candidate(
                category=category,
                content=content,
                trust_level=effective_trust,
                provenance={"source_event_id": source_event_id, "supervisor_pass": pass_id},
                confidence=_bounded_factor(candidate.get("confidence")),
                status=status,
                actor=self.worker_id,
                candidate_id="mem_"
                + hashlib.sha256(memory_identity.encode()).hexdigest()[:24],
            )
            result.memory_candidate_ids.append(candidate_id)

        completed_event_work_ids = {
            str(event.get("payload", {}).get("work_id", ""))
            for event in events
            if event.get("event_type") == "execution.completed"
            and event.get("source") == "hermes"
            and event.get("trust_level") == TrustLevel.AUTHENTICATED_UNTRUSTED.value
            and event.get("provenance", {}).get("adapter") == "hermes-kanban"
        }
        for verification in plan["verifications"]:
            work_id = str(verification.get("work_id", ""))
            if not work_id or work_id not in completed_event_work_ids:
                raise PlanValidationError(
                    "Verification needs a matching execution.completed event"
                )
            work = self.store.get_work(work_id)
            prior_verification = work.metadata.get("last_verification", {})
            if (
                isinstance(prior_verification, dict)
                and prior_verification.get("supervisor_pass") == pass_id
                and prior_verification.get("plan_digest") == plan_digest
            ):
                if prior_verification.get("verdict") == "passed":
                    result.verified_work_ids.append(work_id)
                continue
            if work.version != int(verification["expected_version"]):
                raise StateConflict(
                    f"Work changed while verification was being planned: {work_id}"
                )
            matching_events = [
                event
                for event in events
                if event.get("event_type") == "execution.completed"
                and event.get("source") == "hermes"
                and event.get("provenance", {}).get("adapter") == "hermes-kanban"
                and str(event.get("payload", {}).get("work_id", "")) == work_id
                and str(event.get("payload", {}).get("hermes_task_id", ""))
                == str(work.hermes_task_id or "")
            ]
            if not matching_events or not work.hermes_task_id:
                raise PlanValidationError(
                    f"Verification evidence is not bound to Hermes card for {work_id}"
                )
            completion_payload = matching_events[0].get("payload", {})
            if not isinstance(completion_payload, dict):
                raise PlanValidationError(
                    f"Verification evidence payload is invalid for {work_id}"
                )
            evidence_fingerprint = str(
                completion_payload.get("evidence_fingerprint", "")
            )
            completion_run_id = str(completion_payload.get("run_id", ""))
            completion_attempt = completion_payload.get("attempt")
            if (
                not completion_run_id
                or isinstance(completion_attempt, bool)
                or not isinstance(completion_attempt, int)
                or completion_attempt < 1
            ):
                raise PlanValidationError(
                    f"Verification evidence has no canonical run binding for {work_id}"
                )
            hermes_metadata = work.metadata.get("hermes", {})
            if not evidence_fingerprint or not (
                isinstance(hermes_metadata, dict)
                and hermes_metadata.get("completion_fingerprint")
                == evidence_fingerprint
                and hermes_metadata.get("completion_run_id") == completion_run_id
                and hermes_metadata.get("completion_attempt") == completion_attempt
            ):
                raise PlanValidationError(
                    f"Verification evidence fingerprint does not match {work_id}"
                )
            with self.store.connection() as connection:
                completed_run = connection.execute(
                    "SELECT result_json FROM runs WHERE id = ? AND work_item_id = ? "
                    "AND external_run_id = ? AND attempt = ? "
                    "AND status = 'completed' LIMIT 1",
                    (
                        completion_run_id,
                        work_id,
                        work.hermes_task_id,
                        completion_attempt,
                    ),
                ).fetchone()
            if completed_run is None:
                raise PlanValidationError(
                    f"Verification evidence has no completed Hermes run for {work_id}"
                )
            try:
                completed_result = json.loads(completed_run["result_json"] or "{}")
            except (json.JSONDecodeError, TypeError) as error:
                raise PlanValidationError(
                    f"Verification evidence has an invalid canonical run result for {work_id}"
                ) from error
            if not isinstance(completed_result, dict):
                raise PlanValidationError(
                    f"Verification evidence has an invalid canonical run result for {work_id}"
                )
            canonical_fingerprint = str(completed_result.get("updated_at") or "")
            if not canonical_fingerprint:
                canonical_fingerprint = hashlib.sha256(
                    json.dumps(
                        completed_result,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
            if canonical_fingerprint != evidence_fingerprint:
                raise PlanValidationError(
                    f"Verification fingerprint does not match canonical run evidence for {work_id}"
                )
            if work.status != WorkStatus.REVIEW:
                raise PlanValidationError(f"Work {work_id} is not in review")
            requested_verdict = str(verification.get("verdict", ""))
            if requested_verdict not in {"passed", "failed", "needs_input"}:
                raise PlanValidationError(
                    f"Invalid verification verdict: {requested_verdict}"
                )
            deterministic = self.verifier.verify(
                work=work,
                completion=completed_result,
            ).to_dict()
            verdict = requested_verdict
            if (
                requested_verdict == "passed"
                and deterministic.get("applicable") is True
                and deterministic.get("passed") is not True
            ):
                verdict = "failed"
            criteria_results = verification.get("criteria_results", [])
            if not isinstance(criteria_results, list):
                raise PlanValidationError("criteria_results must be a list")
            by_criterion = {
                str(value.get("criterion", "")).strip(): value
                for value in criteria_results
                if isinstance(value, dict)
            }
            expected = {value.strip() for value in work.acceptance_criteria if value.strip()}
            if set(by_criterion) != expected:
                raise PlanValidationError(
                    f"Verification for {work_id} must assess every acceptance criterion exactly"
                )
            confidence = _bounded_factor(verification.get("confidence"), 0.0)
            all_passed = all(
                value.get("passed") is True
                and bool(str(value.get("evidence", "")).strip())
                for value in by_criterion.values()
            )
            metadata = dict(work.metadata)
            metadata["last_verification"] = {
                "verdict": verdict,
                "requested_verdict": requested_verdict,
                "criteria_results": criteria_results,
                "confidence": confidence,
                "summary": str(verification.get("summary", "")),
                "verified_at": utc_now(),
                "supervisor_pass": pass_id,
                "plan_digest": plan_digest,
                "evidence_fingerprint": evidence_fingerprint,
                "deterministic": deterministic,
            }
            if verdict == "passed":
                if not all_passed or confidence < 0.75:
                    raise PlanValidationError(
                        f"Passed verification for {work_id} lacks evidence or confidence"
                    )
                target_status = WorkStatus.DONE
                result.verified_work_ids.append(work_id)
            elif verdict == "needs_input":
                target_status = WorkStatus.WAITING_INPUT
            else:
                target_status = WorkStatus.BLOCKED
            verified = self.store.update_work(
                work_id,
                {"status": target_status.value, "metadata": metadata},
                actor=self.worker_id,
                expected_version=work.version,
                allow_transition_override=True,
            )
            record_version(
                work_id,
                int(verification["expected_version"]),
                verified.version,
            )
            self.store.audit(
                self.worker_id,
                f"verification.{verdict}",
                entity_type="work",
                entity_id=work_id,
                data=metadata["last_verification"],
            )

        for proposal in plan["external_action_proposals"]:
            if not events:
                self.store.audit(
                    self.worker_id,
                    "external_action.reconciliation_proposal_quarantined",
                    entity_type="supervisor_pass",
                    entity_id=pass_id,
                )
                continue
            if self.config.policy.external_action_mode == "disabled":
                self.store.audit(
                    self.worker_id,
                    "external_action.proposal_rejected_by_policy",
                    entity_type="supervisor_pass",
                    entity_id=pass_id,
                    data={"action_type": proposal.get("action_type")},
                )
                continue
            if self.action_stager is None:
                self.store.audit(
                    self.worker_id,
                    "external_action.proposal_not_staged",
                    entity_type="supervisor_pass",
                    entity_id=pass_id,
                    data={"reason": "approval broker unavailable", "proposal": proposal},
                )
                continue
            intent_id = self.action_stager.stage(proposal, created_by=self.worker_id)
            result.action_intent_ids.append(intent_id)

        for dispatch in plan["dispatch"]:
            work_id = _optional_text(dispatch.get("work_id")) or references.get(
                str(dispatch.get("work_ref", ""))
            )
            if not work_id:
                raise PlanValidationError("Dispatch contains an unresolved work reference")
            work = self.store.get_work(work_id)
            planned_dispatch_version = (
                int(dispatch["expected_version"])
                if _optional_text(dispatch.get("work_id"))
                else None
            )
            expected_dispatch_version = (
                effective_version(work_id, planned_dispatch_version)
                if planned_dispatch_version is not None
                else None
            )
            if (
                expected_dispatch_version is not None
                and work.version != expected_dispatch_version
            ):
                raise StateConflict(
                    f"Work changed while dispatch was being planned: {work_id}"
                )
            if work.status == WorkStatus.WAITING_INPUT:
                self.store.audit(
                    self.worker_id,
                    "dispatch.rejected_waiting_for_input",
                    entity_type="work",
                    entity_id=work_id,
                )
                continue
            dispatch_source_id = _optional_text(dispatch.get("source_event_id"))
            dispatch_source = event_by_id.get(dispatch_source_id or "")
            candidate_profile = (
                _optional_text(dispatch.get("profile"))
                or work.assignee
                or self.config.hermes.default_assignee
            )
            candidate_skills = [
                str(value).strip()
                for value in dispatch.get("skills", [])
                if str(value).strip()
            ]
            candidate_goal_mode = bool(
                dispatch.get("goal_mode", self.config.hermes.goal_mode)
            )
            prior_authorization = work.metadata.get("dispatch_authorization", {})
            last_verification = work.metadata.get("last_verification", {})
            dispatch_payload = (
                dispatch_source.get("payload", {})
                if isinstance(dispatch_source, dict)
                else {}
            )
            if not isinstance(dispatch_payload, dict):
                dispatch_payload = {}
            prior_skills = (
                prior_authorization.get("skills", [])
                if isinstance(prior_authorization, dict)
                else []
            )
            retry_authorized = False
            if (
                isinstance(dispatch_source, dict)
                and dispatch_source.get("source") == "hermes"
                and dispatch_source.get("event_type") == "execution.completed"
                and dispatch_source.get("trust_level")
                == TrustLevel.AUTHENTICATED_UNTRUSTED.value
                and dispatch_source.get("provenance", {}).get("adapter")
                == "hermes-kanban"
                and isinstance(last_verification, dict)
                and last_verification.get("supervisor_pass") == pass_id
                and last_verification.get("verdict") == "failed"
                and isinstance(prior_authorization, dict)
                and prior_authorization.get("lifetime")
                == "until_consumed_or_contract_change"
                and isinstance(prior_authorization.get("authorization_root"), str)
                and prior_authorization.get("authorization_root")
                and dispatch_payload.get("work_id") == work_id
                and dispatch_payload.get("hermes_task_id") == work.hermes_task_id
                and prior_authorization.get("consumed_run_id")
                == dispatch_payload.get("run_id")
                and prior_authorization.get("consumed_external_run_id")
                == work.hermes_task_id
                and prior_authorization.get("profile") == candidate_profile
                and isinstance(prior_skills, list)
                and sorted(map(str, prior_skills))
                == sorted(candidate_skills)
                and prior_authorization.get("contract_digest")
                == dispatch_contract_digest(
                    work,
                    profile=str(candidate_profile or ""),
                    skills=candidate_skills,
                    default_skills=self.config.hermes.default_skills,
                    goal_mode=candidate_goal_mode,
                )
            ):
                configured_attempts = prior_authorization.get("max_attempts")
                if (
                    isinstance(configured_attempts, int)
                    and not isinstance(configured_attempts, bool)
                    and 1 <= configured_attempts
                    <= self.config.hermes.max_execution_attempts
                ):
                    with self.store.connection() as connection:
                        latest_attempt = int(
                            connection.execute(
                                "SELECT COALESCE(MAX(attempt), 0) FROM runs "
                                "WHERE work_item_id = ?",
                                (work_id,),
                            ).fetchone()[0]
                        )
                    retry_authorized = latest_attempt < configured_attempts
            if events and not (
                _event_authorizes(dispatch_source, "dispatch", work_id=work_id)
                or reference_execution_authority.get(
                    str(dispatch.get("work_ref", "")), False
                )
                or retry_authorized
            ):
                self.store.audit(
                    self.worker_id,
                    "dispatch.quarantined_untrusted_trigger",
                    entity_type="work",
                    entity_id=work_id,
                    data={"source_event_id": dispatch_source_id},
                )
                continue
            if not events:
                self.store.audit(
                    self.worker_id,
                    "dispatch.reconciliation_request_quarantined",
                    entity_type="work",
                    entity_id=work_id,
                )
                continue
            governance = work.metadata.get("governance", {})
            if not (
                isinstance(governance, dict)
                and governance.get("execution_authorized") is True
            ):
                self.store.audit(
                    self.worker_id,
                    "dispatch.rejected_missing_operator_authorization",
                    entity_type="work",
                    entity_id=work_id,
                )
                continue
            if not work.acceptance_criteria:
                self.store.update_work(
                    work_id,
                    {"status": WorkStatus.TRIAGE.value},
                    actor=self.worker_id,
                    allow_transition_override=True,
                )
                self.store.audit(
                    self.worker_id,
                    "dispatch.rejected_missing_acceptance_criteria",
                    entity_type="work",
                    entity_id=work_id,
                )
                continue
            requested_profile = candidate_profile
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
            if requested_profile not in allowed_profiles:
                raise PlanValidationError(
                    f"Hermes profile is not allowed: {requested_profile}"
                )
            requested_skills = candidate_skills
            allowed_skills = set(self.config.hermes.allowed_skills)
            allowed_skills.update(self.config.hermes.default_skills)
            unknown_skills = set(requested_skills) - allowed_skills
            if unknown_skills:
                raise PlanValidationError(
                    f"Hermes skills are not allowed: {sorted(unknown_skills)}"
                )
            goal_mode = candidate_goal_mode
            contract_digest_value = dispatch_contract_digest(
                work,
                profile=requested_profile,
                skills=requested_skills,
                default_skills=self.config.hermes.default_skills,
                goal_mode=goal_mode,
            )
            authorization_root = (
                str(prior_authorization.get("authorization_root"))
                if retry_authorized and isinstance(prior_authorization, dict)
                else hashlib.sha256(
                    json.dumps(
                        [pass_id, work_id, contract_digest_value, dispatch_source_id],
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest()
            )
            max_attempts = (
                int(prior_authorization["max_attempts"])
                if retry_authorized and isinstance(prior_authorization, dict)
                else self.config.hermes.max_execution_attempts
            )
            metadata = dict(work.metadata)
            metadata["dispatch_request"] = {
                "profile": requested_profile,
                "goal_mode": goal_mode,
                "skills": requested_skills,
                "reason": str(dispatch.get("reason", "")),
                "requested_at": utc_now(),
                "shadow": self.config.operator.autonomy_mode == "shadow",
            }
            metadata["dispatch_authorization"] = {
                "work_id": work_id,
                "profile": requested_profile,
                "skills": requested_skills,
                "shadow": self.config.operator.autonomy_mode == "shadow",
                "issued_by": self.worker_id,
                "issued_at": utc_now(),
                "not_before": work.scheduled_at,
                "expires_at": None,
                "lifetime": "until_consumed_or_contract_change",
                "review_after": (
                    datetime.now(UTC)
                    + timedelta(
                        seconds=self.config.hermes.dispatch_authorization_ttl_seconds
                    )
                ).isoformat().replace("+00:00", "Z"),
                "trust": "system",
                "authorization_root": authorization_root,
                "max_attempts": max_attempts,
                "authorization_kind": (
                    "bounded_verification_retry"
                    if retry_authorized
                    else "trusted_scope"
                ),
                "retry_of_run_id": (
                    dispatch_payload.get("run_id")
                    if retry_authorized
                    else None
                ),
                "supervisor_pass": pass_id,
                "plan_digest": plan_digest,
                "contract_digest": contract_digest_value,
            }
            changes: dict[str, Any] = {
                "execution_mode": ExecutionMode.HERMES.value,
                "assignee": metadata["dispatch_request"]["profile"],
                "metadata": metadata,
            }
            if work.status not in {WorkStatus.READY, WorkStatus.RUNNING, WorkStatus.REVIEW}:
                changes["status"] = WorkStatus.READY.value
            self.store.update_work(
                work_id,
                changes,
                actor=self.worker_id,
                expected_version=work.version,
                allow_transition_override=True,
            )
            result.dispatch_work_ids.append(work_id)

        result.event_dispositions = self._resolve_event_dispositions(
            plan=plan,
            events=events,
            references=references,
            result=result,
        )
        self.priority_engine.rescore_store(self.store)
        return result
