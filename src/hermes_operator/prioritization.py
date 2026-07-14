from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from .db import SQLiteStore
from .models import TERMINAL_WORK_STATUSES, WorkItem, WorkStatus


@dataclass(frozen=True, slots=True)
class PriorityResult:
    score: float
    rationale: str
    components: dict[str, float]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class PriorityEngine:
    """Deterministic base ranking with bounded contextual adjustment."""

    def __init__(self, *, max_contextual_adjustment: float = 10.0):
        self.max_contextual_adjustment = max(0.0, max_contextual_adjustment)

    def score(
        self,
        item: WorkItem,
        *,
        now: datetime | None = None,
        dependencies_satisfied: bool = True,
        contextual_adjustment: float = 0.0,
        contextual_reason: str = "",
    ) -> PriorityResult:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if item.status in TERMINAL_WORK_STATUSES:
            return PriorityResult(-1000.0, "terminal work item", {"terminal": -1000.0})

        due = _parse_time(item.due_at)
        due_component = 0.0
        if due is not None:
            hours = (due - now).total_seconds() / 3600
            if hours <= 0:
                due_component = 24.0
            elif hours <= 4:
                due_component = 21.0
            elif hours <= 24:
                due_component = 17.0
            elif hours <= 72:
                due_component = 12.0
            elif hours <= 168:
                due_component = 7.0
            else:
                due_component = max(0.0, 5.0 - math.log10(max(hours / 168, 1)) * 2)

        created = _parse_time(item.created_at) or now
        age_days = max(0.0, (now - created).total_seconds() / 86400)
        age_component = min(8.0, math.log1p(age_days) * 2.5)

        effort = max(0, item.effort_minutes)
        quick_win_component = 8.0 / (1.0 + effort / 45.0)
        status_component = {
            WorkStatus.INBOX: -4.0,
            WorkStatus.TRIAGE: -2.0,
            WorkStatus.PLANNED: 0.0,
            WorkStatus.READY: 8.0,
            WorkStatus.RUNNING: 5.0,
            WorkStatus.WAITING_INPUT: -18.0,
            WorkStatus.BLOCKED: -25.0,
            WorkStatus.REVIEW: 10.0,
        }.get(item.status, 0.0)
        dependency_component = 0.0 if dependencies_satisfied else -30.0
        risk_component = -8.0 * item.risk
        confidence_component = 5.0 * item.confidence
        manual_priority_component = max(-10.0, min(10.0, item.priority * 2.0))
        contextual = max(
            -self.max_contextual_adjustment,
            min(self.max_contextual_adjustment, contextual_adjustment),
        )

        components = {
            "impact": 22.0 * item.impact,
            "urgency": 15.0 * item.urgency,
            "strategic_alignment": 17.0 * item.strategic_alignment,
            "unlock_value": 13.0 * item.unlock_value,
            "due": due_component,
            "age": age_component,
            "quick_win": quick_win_component,
            "confidence": confidence_component,
            "risk": risk_component,
            "status": status_component,
            "dependencies": dependency_component,
            "manual_priority": manual_priority_component,
            "contextual": contextual,
        }
        score = round(sum(components.values()), 3)
        leaders = sorted(components.items(), key=lambda pair: abs(pair[1]), reverse=True)[:5]
        rationale = ", ".join(f"{name} {value:+.1f}" for name, value in leaders)
        if contextual_reason and contextual:
            rationale += f"; contextual adjustment: {contextual_reason}"
        return PriorityResult(score, rationale, components)

    def rescore_store(self, store: SQLiteStore) -> list[PriorityResult]:
        items = store.list_work(limit=5000, order_by="created")
        results: list[PriorityResult] = []
        for item in items:
            result = self.score(item, dependencies_satisfied=store.dependencies_satisfied(item.id))
            store.update_priority(item.id, result.score, result.rationale)
            results.append(result)
        return results

    def next_best(
        self,
        items: Iterable[WorkItem],
        *,
        limit: int = 5,
        include_running: bool = False,
    ) -> list[WorkItem]:
        # Triage candidates are surfaced for operator review without granting
        # them execution authority or changing their canonical status.
        allowed = {WorkStatus.TRIAGE, WorkStatus.READY, WorkStatus.REVIEW}
        if include_running:
            allowed.add(WorkStatus.RUNNING)
        candidates = [item for item in items if item.status in allowed]
        return sorted(
            candidates,
            key=lambda item: (item.priority_score, item.priority, -item.effort_minutes),
            reverse=True,
        )[:limit]
