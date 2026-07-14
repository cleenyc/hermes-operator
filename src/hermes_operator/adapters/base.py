"""Portable interfaces shared by Hermes Operator integration adapters.

The adapter layer intentionally knows nothing about the operator's database or
domain models.  Core code can depend on these small dataclasses and protocols,
while deployments remain free to use the Hermes CLI, an in-memory test double,
or a future API-backed implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class AdapterError(RuntimeError):
    """Base class for integration adapter failures."""


class AdapterUnavailableError(AdapterError):
    """Raised when a configured integration cannot be reached."""


class AdapterCommandError(AdapterError):
    """Raised when an integration command exits unsuccessfully."""

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class AdapterResponseError(AdapterError):
    """Raised when an integration returns an invalid or unexpected response."""


class UnsafePathError(AdapterError, ValueError):
    """Raised when a requested vault path escapes the configured vault."""


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """A side-effect-free summary of an adapter's current availability."""

    enabled: bool
    available: bool
    detail: str
    version: str | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HermesCapabilities:
    """Capabilities detected from a Hermes installation."""

    available: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    detail: str = ""

    def supports(self, capability: str) -> bool:
        return capability in self.available

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(slots=True)
class HermesTask:
    """Stable, minimal representation of a Hermes Kanban task."""

    id: str
    title: str
    status: str = "triage"
    description: str = ""
    priority: int | float | str | None = None
    assignee: str | None = None
    parent_id: str | None = None
    scheduled_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    current_run_id: str | None = None
    comments: list[Mapping[str, Any]] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class HermesAdapter(Protocol):
    """Operations the autonomous layer needs from Hermes Kanban."""

    def health(self) -> AdapterHealth:
        """Return current integration health without mutating board state."""

    def check_capabilities(
        self, required: Sequence[str] | None = None
    ) -> HermesCapabilities:
        """Report which required Kanban operations are available."""

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        priority: int | float | str | None = None,
        assignee: str | None = None,
        parent_id: str | None = None,
        idempotency_key: str | None = None,
        scheduled_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> HermesTask:
        """Create and return a durable task."""

    def show_task(self, task_id: str) -> HermesTask:
        """Fetch one task by its Hermes identifier."""

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assignee: str | None = None,
        limit: int | None = None,
    ) -> list[HermesTask]:
        """List tasks matching supported filters."""

    def comment_task(self, task_id: str, comment: str) -> HermesTask:
        """Append a durable task comment and return current task state."""

    def unblock_task(self, task_id: str) -> HermesTask:
        """Unblock a task and return current task state."""

    def block_task(self, task_id: str, reason: str) -> HermesTask:
        """Block a task so Hermes cannot automatically redispatch it."""

    def terminate_task(self, task_id: str) -> HermesTask:
        """Terminate the task's current native run, if one exists."""
