from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


_FIXED_RECURRENCE_PATTERN = re.compile(
    r"^P(?:(?P<weeks>[1-9][0-9]*)W|(?P<days>[1-9][0-9]*)D|"
    r"T(?:(?P<hours>[1-9][0-9]*)H|(?P<minutes>[1-9][0-9]*)M))$"
)


def recurrence_interval(rule: str) -> timedelta:
    """Parse the supported fixed-duration ISO-8601 recurrence subset.

    Calendar months and years are intentionally excluded because their length
    depends on timezone and calendar policy. Fixed durations make roll-forward
    deterministic on every host without another scheduling dependency.
    """

    if not isinstance(rule, str):
        raise ValueError("recurrence_rule must be a string or null")
    match = _FIXED_RECURRENCE_PATTERN.fullmatch(rule.strip().upper())
    if match is None:
        raise ValueError(
            "recurrence_rule must be PTnM, PTnH, PnD, or PnW with a positive integer"
        )
    values = {
        name: int(value) if value is not None else 0
        for name, value in match.groupdict().items()
    }
    interval = timedelta(
        weeks=values["weeks"],
        days=values["days"],
        hours=values["hours"],
        minutes=values["minutes"],
    )
    if interval > timedelta(days=3650):
        raise ValueError("recurrence_rule cannot exceed 3650 days")
    return interval


def normalize_recurrence_rule(rule: str | None) -> str | None:
    if rule is None:
        return None
    normalized = rule.strip().upper()
    recurrence_interval(normalized)
    return normalized


def next_recurrence_due(
    anchor: str,
    rule: str,
    *,
    after: datetime,
) -> str:
    """Return the first recurrence after both the anchor and ``after``."""

    try:
        parsed = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("A recurring reminder requires a valid due_at timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A recurring reminder due_at must include a timezone")
    interval = recurrence_interval(rule)
    anchor_utc = parsed.astimezone(UTC)
    after_utc = after.astimezone(UTC)
    candidate = anchor_utc + interval
    if candidate <= after_utc:
        elapsed = after_utc - candidate
        candidate += interval * (elapsed // interval + 1)
    return candidate.isoformat().replace("+00:00", "Z")


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    AUTHENTICATED_UNTRUSTED = "authenticated_untrusted"
    OPERATOR = "operator"
    SYSTEM = "system"


class EventState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class WorkKind(StrEnum):
    AREA = "area"
    GOAL = "goal"
    PROJECT = "project"
    MILESTONE = "milestone"
    TASK = "task"
    TODO = "todo"
    REMINDER = "reminder"
    DECISION = "decision"


class WorkStatus(StrEnum):
    INBOX = "inbox"
    TRIAGE = "triage"
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class ExecutionMode(StrEnum):
    NONE = "none"
    HERMES = "hermes"


TERMINAL_WORK_STATUSES = {
    WorkStatus.DONE,
    WorkStatus.CANCELLED,
    WorkStatus.ARCHIVED,
}


ALLOWED_WORK_TRANSITIONS: dict[WorkStatus, set[WorkStatus]] = {
    WorkStatus.INBOX: {WorkStatus.TRIAGE, WorkStatus.PLANNED, WorkStatus.READY, WorkStatus.CANCELLED},
    WorkStatus.TRIAGE: {WorkStatus.PLANNED, WorkStatus.READY, WorkStatus.WAITING_INPUT, WorkStatus.CANCELLED},
    WorkStatus.PLANNED: {WorkStatus.READY, WorkStatus.BLOCKED, WorkStatus.WAITING_INPUT, WorkStatus.CANCELLED},
    WorkStatus.READY: {WorkStatus.RUNNING, WorkStatus.BLOCKED, WorkStatus.WAITING_INPUT, WorkStatus.CANCELLED},
    WorkStatus.RUNNING: {WorkStatus.REVIEW, WorkStatus.DONE, WorkStatus.BLOCKED, WorkStatus.WAITING_INPUT, WorkStatus.READY, WorkStatus.CANCELLED},
    WorkStatus.WAITING_INPUT: {WorkStatus.TRIAGE, WorkStatus.PLANNED, WorkStatus.READY, WorkStatus.BLOCKED, WorkStatus.CANCELLED},
    WorkStatus.BLOCKED: {WorkStatus.PLANNED, WorkStatus.READY, WorkStatus.WAITING_INPUT, WorkStatus.CANCELLED},
    WorkStatus.REVIEW: {WorkStatus.DONE, WorkStatus.READY, WorkStatus.BLOCKED, WorkStatus.WAITING_INPUT, WorkStatus.CANCELLED},
    WorkStatus.DONE: {WorkStatus.ARCHIVED, WorkStatus.READY},
    WorkStatus.CANCELLED: {WorkStatus.ARCHIVED, WorkStatus.TRIAGE},
    WorkStatus.ARCHIVED: {WorkStatus.TRIAGE},
}


class WorkRelation(StrEnum):
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    RELATED_TO = "related_to"
    DUPLICATES = "duplicates"
    DERIVED_FROM = "derived_from"


class QuestionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    DISMISSED = "dismissed"


@dataclass(slots=True)
class Provenance:
    source: str
    external_id: str | None = None
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    received_at: str = field(default_factory=utc_now)
    actor: str | None = None
    content_digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["trust_level"] = self.trust_level.value
        return result


@dataclass(slots=True)
class Event:
    source: str
    event_type: str
    payload: dict[str, Any]
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    external_id: str | None = None
    dedupe_key: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("evt"))
    state: EventState = EventState.PENDING
    created_at: str = field(default_factory=utc_now)
    available_at: str = field(default_factory=utc_now)
    attempt_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["trust_level"] = self.trust_level.value
        result["state"] = self.state.value
        return result


@dataclass(slots=True)
class WorkItem:
    title: str
    kind: WorkKind = WorkKind.TASK
    description: str = ""
    status: WorkStatus = WorkStatus.TRIAGE
    parent_id: str | None = None
    source_event_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    priority_score: float = 0.0
    priority_rationale: str = ""
    impact: float = 0.5
    urgency: float = 0.5
    strategic_alignment: float = 0.5
    unlock_value: float = 0.0
    risk: float = 0.0
    confidence: float = 0.5
    effort_minutes: int = 30
    due_at: str | None = None
    scheduled_at: str | None = None
    recurrence_rule: str | None = None
    reminder_snoozed_until: str | None = None
    reminder_last_delivered_at: str | None = None
    reminder_last_acknowledged_at: str | None = None
    reminder_delivery_count: int = 0
    assignee: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.NONE
    hermes_task_id: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("wrk"))
    version: int = 1
    authorization_scope_revision: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["status"] = self.status.value
        result["execution_mode"] = self.execution_mode.value
        return result

    @classmethod
    def from_row(cls, row: Any) -> "WorkItem":
        return cls(
            id=row["id"],
            title=row["title"],
            kind=WorkKind(row["kind"]),
            description=row["description"],
            status=WorkStatus(row["status"]),
            parent_id=row["parent_id"],
            source_event_id=row["source_event_id"],
            provenance=json.loads(row["provenance_json"] or "{}"),
            priority=row["priority"],
            priority_score=row["priority_score"],
            priority_rationale=row["priority_rationale"],
            impact=row["impact"],
            urgency=row["urgency"],
            strategic_alignment=row["strategic_alignment"],
            unlock_value=row["unlock_value"],
            risk=row["risk"],
            confidence=row["confidence"],
            effort_minutes=row["effort_minutes"],
            due_at=row["due_at"],
            scheduled_at=row["scheduled_at"],
            recurrence_rule=row["recurrence_rule"],
            reminder_snoozed_until=row["reminder_snoozed_until"],
            reminder_last_delivered_at=row["reminder_last_delivered_at"],
            reminder_last_acknowledged_at=row["reminder_last_acknowledged_at"],
            reminder_delivery_count=int(row["reminder_delivery_count"]),
            assignee=row["assignee"],
            execution_mode=ExecutionMode(row["execution_mode"]),
            hermes_task_id=row["hermes_task_id"],
            acceptance_criteria=json.loads(row["acceptance_criteria_json"] or "[]"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            version=row["version"],
            authorization_scope_revision=int(row["authorization_scope_revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )


@dataclass(slots=True)
class UserQuestion:
    question: str
    context: str = ""
    urgency: float = 0.5
    blocking_work_ids: list[str] = field(default_factory=list)
    blocking_work_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("qst"))
    status: QuestionStatus = QuestionStatus.PENDING
    answer: str | None = None
    created_at: str = field(default_factory=utc_now)
    answered_at: str | None = None
    last_delivered_at: str | None = None
    delivery_count: int = 0


@dataclass(slots=True)
class RunRecord:
    work_item_id: str
    runner: str
    status: str = "queued"
    external_run_id: str | None = None
    attempt: int = 1
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    id: str = field(default_factory=lambda: new_id("run"))
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
