from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from .models import utc_now


ComponentCallback = Callable[[], Any | Awaitable[Any]]
ErrorCallback = Callable[[str, BaseException], None]
CycleCallback = Callable[["CycleResult"], Any | Awaitable[Any]]


@dataclass(slots=True)
class RuntimeCallbacks:
    """Functions run by the live autonomy loop.

    Each callback may be synchronous or asynchronous. Observation polls
    bounded inbound readers, reconciliation checks execution state, processing
    performs the supervisor pass, dispatch starts authorized internal work,
    and projection refreshes human-readable views. The runtime itself performs
    no outbound communication.
    """

    observe: ComponentCallback | None = None
    process_events: ComponentCallback | None = None
    reconcile: ComponentCallback | None = None
    dispatch: ComponentCallback | None = None
    project: ComponentCallback | None = None
    startup: ComponentCallback | None = None
    shutdown: ComponentCallback | None = None


@dataclass(slots=True)
class CycleResult:
    id: str
    reason: str
    started_at: str
    finished_at: str | None = None
    reconciled: bool = False
    completed_components: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutonomousRuntime:
    """Event-driven autonomy supervisor with timer-based recovery checks."""

    def __init__(
        self,
        callbacks: RuntimeCallbacks,
        *,
        tick_seconds: float = 30.0,
        reconciliation_seconds: float = 300.0,
        on_error: ErrorCallback | None = None,
        on_cycle: CycleCallback | None = None,
    ):
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if reconciliation_seconds <= 0:
            raise ValueError("reconciliation_seconds must be positive")
        self.callbacks = callbacks
        self.tick_seconds = float(tick_seconds)
        self.reconciliation_seconds = float(reconciliation_seconds)
        self.on_error = on_error
        self.on_cycle = on_cycle

        self._state_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_reasons: deque[str] = deque(maxlen=32)
        self._stop_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._cycle_lock: asyncio.Lock | None = None
        self._running = False
        self._cycle_count = 0
        self._last_cycle: CycleResult | None = None
        self._started_at: str | None = None
        self._stopped_at: str | None = None

    def wake(self, reason: str = "external") -> None:
        """Wake the loop from an asyncio task, HTTP thread, or connector thread."""

        normalized = reason.strip()[:128] if isinstance(reason, str) else "external"
        normalized = normalized or "external"
        with self._pending_lock:
            self._pending_reasons.append(normalized)
        loop = self._loop
        event = self._wake_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def stop(self) -> None:
        """Request graceful shutdown and interrupt the current idle wait."""

        self._stop_requested.set()
        loop = self._loop
        event = self._wake_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    async def run(self) -> None:
        """Run until ``stop`` is requested.

        A boot cycle runs immediately. Subsequent cycles are caused by wake
        notifications or the lightweight recovery tick. Full reconciliation
        runs on its own monotonic interval inside the same loop.
        """

        with self._state_lock:
            if self._running:
                raise RuntimeError("Runtime is already running")
            if self._stop_requested.is_set():
                self._stopped_at = utc_now()
                return
            self._running = True
            self._started_at = utc_now()
            self._stopped_at = None

        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        next_tick = self._loop.time()
        next_reconciliation = self._loop.time()

        try:
            if self.callbacks.startup is not None:
                await _invoke(self.callbacks.startup)
            while not self._stop_requested.is_set():
                now = self._loop.time()
                due_at = min(next_tick, next_reconciliation)
                pending = self._has_pending_reasons()
                if not pending and due_at > now:
                    assert self._wake_event is not None
                    try:
                        await asyncio.wait_for(
                            self._wake_event.wait(),
                            timeout=due_at - now,
                        )
                    except TimeoutError:
                        pass
                    self._wake_event.clear()
                if self._stop_requested.is_set():
                    break

                now = self._loop.time()
                reasons = self._drain_reasons()
                tick_due = now >= next_tick
                reconcile_due = now >= next_reconciliation
                if not reasons:
                    reasons = ["reconciliation" if reconcile_due else "recovery-tick"]
                event_reconcile = any(
                    reason == "hermes-state"
                    for reason in reasons
                )
                await self.run_once(
                    force_reconcile=reconcile_due or event_reconcile,
                    reason="+".join(dict.fromkeys(reasons)),
                )
                after = self._loop.time()
                if tick_due or after >= next_tick:
                    next_tick = after + self.tick_seconds
                if reconcile_due:
                    next_reconciliation = after + self.reconciliation_seconds
        finally:
            await self._run_lifecycle_callback("shutdown", self.callbacks.shutdown)
            with self._state_lock:
                self._running = False
                self._stopped_at = utc_now()
            self._wake_event = None
            self._loop = None
            self._cycle_lock = None

    async def run_once(
        self,
        *,
        force_reconcile: bool = False,
        reason: str = "manual",
    ) -> CycleResult:
        """Execute one ordered control-plane cycle."""

        lock = self._cycle_lock
        if lock is None:
            lock = asyncio.Lock()
        async with lock:
            result = CycleResult(
                id=f"cycle_{uuid.uuid4().hex}",
                reason=(reason.strip() or "manual")[:512],
                started_at=utc_now(),
                reconciled=force_reconcile,
            )
            components: list[tuple[str, ComponentCallback | None]] = []
            components.append(("observe", self.callbacks.observe))
            if force_reconcile:
                components.append(("reconcile", self.callbacks.reconcile))
            components.extend(
                [
                    ("process_events", self.callbacks.process_events),
                    ("dispatch", self.callbacks.dispatch),
                    ("project", self.callbacks.project),
                ]
            )
            for name, callback in components:
                if self._stop_requested.is_set():
                    break
                if callback is None:
                    continue
                if name == "dispatch" and (
                    "observe" in result.errors
                    or "process_events" in result.errors
                    or "reconcile" in result.errors
                ):
                    # Dispatch must never run on a cycle whose control-plane
                    # event pass failed partway through. A later recovery
                    # cycle can safely retry after the event lease is reset.
                    if "observe" in result.errors:
                        failed = "observe"
                    elif "process_events" in result.errors:
                        failed = "process_events"
                    else:
                        failed = "reconcile"
                    result.errors[name] = f"skipped because {failed} failed"
                    continue
                try:
                    await _invoke(callback)
                    result.completed_components.append(name)
                except Exception as error:
                    result.errors[name] = f"{type(error).__name__}: {error}"[:2000]
                    if self.on_error is not None:
                        try:
                            self.on_error(name, error)
                        except Exception:
                            pass
            result.finished_at = utc_now()
            if self.on_cycle is not None:
                try:
                    await _invoke(self.on_cycle, result)
                except Exception as error:
                    result.errors["cycle_state"] = (
                        f"{type(error).__name__}: {error}"
                    )[:2000]
                    if self.on_error is not None:
                        try:
                            self.on_error("cycle_state", error)
                        except Exception:
                            pass
            with self._state_lock:
                self._cycle_count += 1
                self._last_cycle = result
            return result

    def health(self) -> dict[str, Any]:
        """Return a thread-safe, JSON-compatible runtime status snapshot."""

        with self._state_lock:
            running = self._running
            last = self._last_cycle.to_dict() if self._last_cycle else None
            degraded = bool(self._last_cycle and self._last_cycle.errors)
            return {
                "status": "degraded" if degraded else ("running" if running else "stopped"),
                "running": running,
                "stop_requested": self._stop_requested.is_set(),
                "cycle_count": self._cycle_count,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_cycle": last,
            }

    def _has_pending_reasons(self) -> bool:
        with self._pending_lock:
            return bool(self._pending_reasons)

    def _drain_reasons(self) -> list[str]:
        with self._pending_lock:
            reasons = list(self._pending_reasons)
            self._pending_reasons.clear()
            return reasons

    async def _run_lifecycle_callback(
        self,
        name: str,
        callback: ComponentCallback | None,
    ) -> None:
        if callback is None:
            return
        try:
            await _invoke(callback)
        except Exception as error:
            if self.on_error is not None:
                try:
                    self.on_error(name, error)
                except Exception:
                    pass


async def _invoke(callback: Callable[..., Any], *args: Any) -> Any:
    value = callback(*args)
    if inspect.isawaitable(value):
        return await value
    return value
