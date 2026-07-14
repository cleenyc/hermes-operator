from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.runtime import AutonomousRuntime, RuntimeCallbacks  # noqa: E402


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Condition was not reached before timeout")
        await asyncio.sleep(0.005)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_callback_receives_completed_result(self) -> None:
        persisted: list[dict[str, object]] = []
        runtime = AutonomousRuntime(
            RuntimeCallbacks(process_events=lambda: None),
            on_cycle=lambda result: persisted.append(result.to_dict()),
        )

        result = await runtime.run_once(reason="persist-health")

        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["id"], result.id)
        self.assertIsNotNone(persisted[0]["finished_at"])
        self.assertEqual(persisted[0]["errors"], {})

    async def test_observation_runs_before_reconciliation_and_event_processing(self) -> None:
        calls: list[str] = []
        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                observe=lambda: calls.append("observe"),
                reconcile=lambda: calls.append("reconcile"),
                process_events=lambda: calls.append("process"),
                dispatch=lambda: calls.append("dispatch"),
                project=lambda: calls.append("project"),
            )
        )

        result = await runtime.run_once(force_reconcile=True, reason="order")

        self.assertEqual(
            calls,
            ["observe", "reconcile", "process", "dispatch", "project"],
        )
        self.assertEqual(result.errors, {})

    async def test_systemic_observation_failure_prevents_dispatch(self) -> None:
        calls: list[str] = []

        def broken_observation() -> None:
            calls.append("observe")
            raise RuntimeError("leader fence lost")

        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                observe=broken_observation,
                process_events=lambda: calls.append("process"),
                dispatch=lambda: calls.append("dispatch"),
                project=lambda: calls.append("project"),
            )
        )

        result = await runtime.run_once(reason="observation-failure")

        self.assertEqual(calls, ["observe", "process", "project"])
        self.assertEqual(
            result.errors["dispatch"],
            "skipped because observe failed",
        )

    async def test_wake_from_another_thread_triggers_immediate_cycle(self) -> None:
        calls: list[str] = []
        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                reconcile=lambda: calls.append("reconcile"),
                process_events=lambda: calls.append("process"),
                dispatch=lambda: calls.append("dispatch"),
                project=lambda: calls.append("project"),
            ),
            tick_seconds=10,
            reconciliation_seconds=10,
        )
        task = asyncio.create_task(runtime.run())
        await wait_until(lambda: runtime.health()["cycle_count"] >= 1)
        initial_cycles = runtime.health()["cycle_count"]

        thread = threading.Thread(target=lambda: runtime.wake("http-event"))
        thread.start()
        thread.join()
        await wait_until(lambda: runtime.health()["cycle_count"] > initial_cycles)

        runtime.stop()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(runtime.health()["status"], "stopped")
        self.assertEqual(
            calls[:4], ["reconcile", "process", "dispatch", "project"]
        )
        self.assertEqual(runtime.health()["last_cycle"]["reason"], "http-event")

    async def test_timer_is_recovery_fallback_and_reconciliation_is_periodic(self) -> None:
        process_count = 0
        reconcile_count = 0

        def process() -> None:
            nonlocal process_count
            process_count += 1

        def reconcile() -> None:
            nonlocal reconcile_count
            reconcile_count += 1

        runtime = AutonomousRuntime(
            RuntimeCallbacks(process_events=process, reconcile=reconcile),
            tick_seconds=0.02,
            reconciliation_seconds=0.045,
        )
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.13)
        runtime.stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertGreaterEqual(process_count, 4)
        self.assertGreaterEqual(reconcile_count, 2)
        self.assertGreaterEqual(runtime.health()["cycle_count"], process_count)

    async def test_hermes_telemetry_does_not_force_full_reconciliation(self) -> None:
        reconcile_count = 0

        def reconcile() -> None:
            nonlocal reconcile_count
            reconcile_count += 1

        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                reconcile=reconcile,
                process_events=lambda: None,
            ),
            tick_seconds=10,
            reconciliation_seconds=10,
        )
        task = asyncio.create_task(runtime.run())
        await wait_until(lambda: runtime.health()["cycle_count"] >= 1)
        initial_reconciliations = reconcile_count

        runtime.wake("event:hermes")
        await wait_until(lambda: runtime.health()["cycle_count"] >= 2)
        self.assertEqual(reconcile_count, initial_reconciliations)

        runtime.wake("hermes-state")
        await wait_until(lambda: runtime.health()["cycle_count"] >= 3)
        self.assertEqual(reconcile_count, initial_reconciliations + 1)
        runtime.stop()
        await asyncio.wait_for(task, timeout=1)

    async def test_component_errors_are_isolated_and_reported(self) -> None:
        completed: list[str] = []
        reported: list[tuple[str, str]] = []

        def broken() -> None:
            raise RuntimeError("adapter unavailable")

        async def process() -> None:
            completed.append("process")

        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                reconcile=broken,
                process_events=process,
                dispatch=lambda: completed.append("dispatch"),
                project=lambda: completed.append("project"),
            ),
            on_error=lambda name, error: reported.append((name, str(error))),
        )
        result = await runtime.run_once(force_reconcile=True, reason="test")

        self.assertIn("reconcile", result.errors)
        self.assertEqual(completed, ["process", "project"])
        self.assertEqual(reported, [("reconcile", "adapter unavailable")])
        self.assertEqual(
            result.errors["dispatch"], "skipped because reconcile failed"
        )
        self.assertEqual(runtime.health()["status"], "degraded")

    async def test_failed_event_pass_prevents_dispatch_in_the_same_cycle(self) -> None:
        calls: list[str] = []

        def broken_events() -> None:
            calls.append("process")
            raise RuntimeError("plan application failed")

        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                process_events=broken_events,
                dispatch=lambda: calls.append("dispatch"),
                project=lambda: calls.append("project"),
            )
        )

        result = await runtime.run_once(reason="failure-boundary")

        self.assertEqual(calls, ["process", "project"])
        self.assertIn("process_events", result.errors)
        self.assertEqual(
            result.errors["dispatch"],
            "skipped because process_events failed",
        )

    async def test_startup_and_shutdown_callbacks_wrap_graceful_run(self) -> None:
        lifecycle: list[str] = []
        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                startup=lambda: lifecycle.append("startup"),
                process_events=lambda: lifecycle.append("cycle"),
                shutdown=lambda: lifecycle.append("shutdown"),
            ),
            tick_seconds=10,
            reconciliation_seconds=10,
        )
        runtime.wake("before-start")
        task = asyncio.create_task(runtime.run())
        await wait_until(lambda: runtime.health()["cycle_count"] >= 1)
        runtime.stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(lifecycle, ["startup", "cycle", "shutdown"])
        self.assertEqual(runtime.health()["last_cycle"]["reason"], "before-start")
        self.assertIsNotNone(runtime.health()["stopped_at"])

    async def test_stop_before_run_is_safe(self) -> None:
        invoked = False

        def process() -> None:
            nonlocal invoked
            invoked = True

        runtime = AutonomousRuntime(RuntimeCallbacks(process_events=process))
        runtime.stop()
        await runtime.run()

        self.assertFalse(invoked)
        self.assertEqual(runtime.health()["status"], "stopped")
        self.assertTrue(runtime.health()["stop_requested"])

    async def test_stop_during_cycle_skips_remaining_components(self) -> None:
        calls: list[str] = []
        runtime: AutonomousRuntime

        async def process() -> None:
            calls.append("process")
            runtime.stop()

        runtime = AutonomousRuntime(
            RuntimeCallbacks(
                process_events=process,
                dispatch=lambda: calls.append("dispatch"),
                project=lambda: calls.append("project"),
            )
        )

        await runtime.run_once(reason="stop-test")

        self.assertEqual(calls, ["process"])


if __name__ == "__main__":
    unittest.main()
