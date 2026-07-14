from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .adapters import ObsidianAdapter
from .approvals import ExternalActionStager
from .db import SQLiteStore
from .models import WorkItem, WorkStatus, utc_now
from .prioritization import PriorityEngine


@dataclass(frozen=True, slots=True)
class ProjectionSummary:
    enabled: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    reason: str = ""


def _safe_segment(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    result = "".join(character if character in allowed else "-" for character in value)
    while "--" in result:
        result = result.replace("--", "-")
    return result.strip("-") or "item"


class KnowledgeProjector:
    """Projects canonical operator state into human-readable Obsidian notes."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        obsidian: ObsidianAdapter,
        priority_engine: PriorityEngine,
        operator_root: str = "Hermes Operator",
        actions: ExternalActionStager | None = None,
    ) -> None:
        self.store = store
        self.obsidian = obsidian
        self.priority_engine = priority_engine
        self.operator_root = operator_root.strip("/ ") or "Hermes Operator"
        self.actions = actions

    def project(self) -> ProjectionSummary:
        if not self.obsidian.enabled:
            health = self.obsidian.health()
            return ProjectionSummary(False, reason=health.detail)
        active = self.store.list_work(
            statuses=[
                WorkStatus.INBOX,
                WorkStatus.TRIAGE,
                WorkStatus.PLANNED,
                WorkStatus.READY,
                WorkStatus.RUNNING,
                WorkStatus.WAITING_INPUT,
                WorkStatus.BLOCKED,
                WorkStatus.REVIEW,
            ],
            limit=5000,
        )
        terminal = self.store.list_work(
            statuses=[WorkStatus.DONE, WorkStatus.CANCELLED, WorkStatus.ARCHIVED],
            limit=5000,
            order_by="updated",
        )
        questions = self.store.list_questions()
        next_items = self.priority_engine.next_best(active, limit=10, include_running=True)
        approvals = self.actions.list(status="pending_approval") if self.actions else []
        memories = self.store.list_memory(status="promoted", limit=5000)
        memory_review = self.store.list_memory(status="pending", limit=1000)
        memory_review.extend(self.store.list_memory(status="quarantined", limit=1000))
        results = [
            self._project_dashboard(
                active, next_items, questions, approvals, len(memory_review)
            )
        ]
        results.extend(self._project_work(item) for item in active)
        results.extend(self._project_work(item) for item in terminal)
        results.extend(self._project_memory(memory) for memory in memories)
        created = sum(1 for result in results if result.created)
        updated = sum(1 for result in results if result.updated)
        return ProjectionSummary(
            True,
            created=created,
            updated=updated,
            unchanged=len(results) - created - updated,
        )

    def _project_dashboard(
        self, active, next_items, questions, approvals, memory_review_count
    ):
        status_counts: dict[str, int] = {}
        for item in active:
            status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
        lines = [
            "# Autonomous Operator",
            "",
            f"Last projected: {utc_now()}",
            "",
            "## Next best work",
            "",
        ]
        lines.extend(self._work_links(next_items) or ["No ready work."])
        lines.extend(["", "## Questions", ""])
        lines.extend(
            [
                f"- {question['question']} (`{question['id']}`)"
                for question in questions
            ]
            or ["No open questions."]
        )
        lines.extend(["", "## Approval queue", ""])
        lines.extend(
            [
                f"- `{action.intent.action_type_value}` for "
                f"{', '.join(action.intent.recipients) or action.intent.target or 'external target'} "
                f"(`{action.id}`)"
                for action in approvals
            ]
            or ["No external actions waiting for approval."]
        )
        lines.extend(["", "## Active status", ""])
        lines.extend(
            [f"- {status}: {count}" for status, count in sorted(status_counts.items())]
            or ["No active work."]
        )
        lines.extend(
            [
                "",
                "## Memory review",
                "",
                f"Candidates waiting for operator review: {memory_review_count}",
            ]
        )
        return self.obsidian.project_note(
            {
                "id": "operator-dashboard",
                "title": "Autonomous Operator",
                "updated_at": utc_now(),
                "active_work_count": len(active),
                "open_question_count": len(questions),
                "pending_approval_count": len(approvals),
                "memory_review_count": memory_review_count,
            },
            body="\n".join(lines),
            relative_path=f"{self.operator_root}/Dashboard.md",
        )

    def _project_memory(self, memory: dict):
        category = _safe_segment(str(memory["category"]))
        body = "\n".join(
            [
                f"# {memory['category'].title()}",
                "",
                str(memory["content"]),
                "",
                "## Provenance",
                "",
                f"- Trust: `{memory['trust_level']}`",
                f"- Confidence: {float(memory['confidence']):.2f}",
                f"- Created: {memory['created_at']}",
                f"- Promoted: {memory['promoted_at'] or ''}",
            ]
        )
        return self.obsidian.project_note(
            {
                "id": memory["id"],
                "title": str(memory["category"]).title(),
                "memory_category": memory["category"],
                "status": memory["status"],
                "trust_level": memory["trust_level"],
                "confidence": memory["confidence"],
                "promoted_at": memory["promoted_at"] or "",
            },
            body=body,
            relative_path=(
                f"{self.operator_root}/Memory/{category}/"
                f"{_safe_segment(str(memory['id']))}.md"
            ),
        )

    def _project_work(self, item: WorkItem):
        lines = [f"# {item.title}", "", item.description.strip()]
        lines.extend(["", "## Status", "", f"- Status: `{item.status.value}`"])
        lines.append(f"- Priority score: {item.priority_score:.3f}")
        lines.append(f"- Execution: `{item.execution_mode.value}`")
        if item.due_at:
            lines.append(f"- Due: {item.due_at}")
        if item.assignee:
            lines.append(f"- Assignee: {item.assignee}")
        if item.parent_id:
            lines.append(f"- Parent: [[{_safe_segment(item.parent_id)}]]")
        if item.hermes_task_id:
            lines.append(f"- Hermes card: `{item.hermes_task_id}`")
        lines.extend(["", "## Acceptance criteria", ""])
        lines.extend([f"- [ ] {criterion}" for criterion in item.acceptance_criteria] or ["None recorded."])
        lines.extend(["", "## Priority rationale", "", item.priority_rationale or "Not scored."])
        return self.obsidian.project_project(
            {
                "id": item.id,
                "title": item.title,
                "work_kind": item.kind.value,
                "status": item.status.value,
                "parent_id": item.parent_id or "",
                "priority_score": item.priority_score,
                "due_at": item.due_at or "",
                "hermes_task_id": item.hermes_task_id or "",
                "updated_at": item.updated_at,
            },
            body="\n".join(lines),
            relative_path=f"{self.operator_root}/Work/{_safe_segment(item.id)}.md",
        )

    def _work_links(self, items: Iterable[WorkItem]) -> list[str]:
        return [
            f"- [[Work/{_safe_segment(item.id)}|{item.title}]] "
            f"({item.status.value}, {item.priority_score:.1f})"
            for item in items
        ]
