from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.adapters.base import AdapterHealth  # noqa: E402
from hermes_operator.cli import _doctor, main  # noqa: E402
from hermes_operator.config import load_config  # noqa: E402
from hermes_operator.db import SQLiteStore  # noqa: E402
from hermes_operator.llm import ScriptedLLM  # noqa: E402
from hermes_operator.models import EventState, WorkStatus  # noqa: E402


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "operator.toml"

    def invoke(self, *arguments: str) -> tuple[int, dict[str, object] | None]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["--config", str(self.config_path), *arguments])
        text = stdout.getvalue().strip()
        return status, json.loads(text) if text else None

    def test_init_is_portable_and_refuses_accidental_overwrite(self) -> None:
        status, document = self.invoke("init")

        self.assertEqual(status, 0)
        self.assertEqual(Path(str(document["created"])), self.config_path.resolve())
        config = load_config(self.config_path)
        self.assertEqual(config.operator.database_path, self.root / "data" / "operator.db")
        self.assertIsNone(config.obsidian.vault_path)
        self.assertFalse(config.hermes.enabled)

        duplicate_status, duplicate_document = self.invoke("init")
        self.assertEqual(duplicate_status, 1)
        self.assertIsNone(duplicate_document)

    def test_work_add_list_and_next_share_the_canonical_database(self) -> None:
        self.invoke("init")
        status, created = self.invoke(
            "work",
            "add",
            "Prepare internal review",
            "--status",
            "ready",
            "--criterion",
            "All checks pass",
            "--impact",
            "0.9",
        )

        self.assertEqual(status, 0)
        self.assertEqual(created["status"], "ready")
        status, listed = self.invoke("work", "list", "--status", "ready")
        self.assertEqual(status, 0)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["id"], created["id"])
        status, next_items = self.invoke("next", "--limit", "1")
        self.assertEqual(status, 0)
        self.assertEqual(next_items["items"][0]["id"], created["id"])

        store = SQLiteStore(load_config(self.config_path).operator.database_path)
        self.assertEqual(store.get_work(str(created["id"])).status, WorkStatus.READY)

    def test_doctor_reports_unconfigured_model_without_network_access(self) -> None:
        self.invoke("init")
        status, document = self.invoke("doctor")

        self.assertEqual(status, 2)
        self.assertFalse(document["ok"])
        self.assertFalse(document["checks"]["llm"]["ok"])
        self.assertTrue(document["checks"]["database"]["ok"])

    def test_doctor_rejects_missing_command_executable(self) -> None:
        self.invoke("init")
        configured = self.config_path.read_text(encoding="utf-8").replace(
            'provider = "openai_compatible"',
            'provider = "command"',
            1,
        ).replace(
            "command = []",
            'command = ["/definitely/missing/hermes-operator-planner"]',
            1,
        )
        self.config_path.write_text(configured, encoding="utf-8")

        status, document = self.invoke("doctor")

        self.assertEqual(status, 2)
        self.assertFalse(document["checks"]["llm"]["ok"])
        self.assertIn("not found", document["checks"]["llm"]["detail"])

    def test_doctor_live_performs_a_real_model_readiness_probe(self) -> None:
        self.invoke("init")
        command = json.dumps(
            [
                sys.executable,
                "-c",
                (
                    "import json; "
                    "print(json.dumps({'ok': True, "
                    "'probe': 'hermes-operator-readiness'}))"
                ),
            ]
        )
        configured = self.config_path.read_text(encoding="utf-8").replace(
            'provider = "openai_compatible"',
            'provider = "command"',
            1,
        ).replace(
            "command = []",
            f"command = {command}",
            1,
        )
        self.config_path.write_text(configured, encoding="utf-8")

        status, document = self.invoke("doctor", "--live")

        self.assertEqual(status, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["checks"]["model_live"]["ok"])
        self.assertEqual(
            document["checks"]["model_live"]["detail"],
            "model probe passed",
        )

    def test_doctor_live_attests_every_effective_execution_profile(self) -> None:
        requested_profiles: list[str] = []

        class Dispatcher:
            @staticmethod
            def _policy_attestation(profile: str):
                requested_profiles.append(profile)
                return True, "fresh", {}

        healthy = AdapterHealth(True, True, "ready")
        config = SimpleNamespace(
            config_path=self.config_path,
            llm=SimpleNamespace(
                provider="command",
                command=[sys.executable],
                model="readiness-test",
            ),
            hermes=SimpleNamespace(
                enabled=True,
                profile="primary",
                default_assignee="worker",
                orchestrator_profile="orchestrator",
                allowed_profiles=["research", "worker"],
            ),
            obsidian=SimpleNamespace(enabled=False),
            native_automation=SimpleNamespace(enabled=False),
            policy=SimpleNamespace(
                external_actions_require_approval=True,
                external_action_mode="stage_only",
            ),
        )
        service = SimpleNamespace(
            config=config,
            store=SimpleNamespace(path=self.root / "operator.db"),
            hermes=SimpleNamespace(
                health=lambda: healthy,
                control_health=lambda: healthy,
            ),
            obsidian=SimpleNamespace(
                health=lambda: AdapterHealth(False, False, "disabled")
            ),
            dispatcher=Dispatcher(),
            llm=ScriptedLLM(
                [{"ok": True, "probe": "hermes-operator-readiness"}]
            ),
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            status = _doctor(service, live=True)

        document = json.loads(stdout.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(document["checks"]["policy_attestation"]["ok"])
        self.assertEqual(
            requested_profiles,
            ["orchestrator", "primary", "research", "worker"],
        )

    def test_run_once_returns_failure_when_a_component_fails(self) -> None:
        self.invoke("init")
        command = json.dumps(
            [sys.executable, "-c", "import sys; sys.exit(7)"]
        )
        configured = self.config_path.read_text(encoding="utf-8").replace(
            'provider = "openai_compatible"',
            'provider = "command"',
            1,
        ).replace(
            "command = []",
            f"command = {command}",
            1,
        )
        self.config_path.write_text(configured, encoding="utf-8")
        self.invoke(
            "ingest",
            "--type",
            "operator.request",
            "--payload",
            '{"request":"Exercise the failed planner"}',
        )

        status, document = self.invoke("run-once", "--no-reconcile")

        self.assertEqual(status, 2)
        self.assertIn("process_events", document["errors"])

    def test_audit_command_exposes_bounded_operator_history(self) -> None:
        self.invoke("init")
        _, created = self.invoke("work", "add", "Audited task")

        status, document = self.invoke(
            "audit",
            "--entity-type",
            "work",
            "--entity-id",
            str(created["id"]),
            "--limit",
            "10",
        )

        self.assertEqual(status, 0)
        self.assertGreaterEqual(document["count"], 1)
        self.assertTrue(
            all(item["entity_id"] == created["id"] for item in document["items"])
        )

    def test_work_link_connects_two_existing_items_with_version_fences(self) -> None:
        self.invoke("init")
        _, task = self.invoke("work", "add", "Dependent task")
        _, dependency = self.invoke("work", "add", "Required task")

        status, link = self.invoke(
            "work",
            "link",
            str(task["id"]),
            str(dependency["id"]),
            "--relation",
            "depends_on",
            "--from-version",
            str(task["version"]),
            "--to-version",
            str(dependency["version"]),
        )

        self.assertEqual(status, 0)
        self.assertEqual(link["from_id"], task["id"])
        store = SQLiteStore(load_config(self.config_path).operator.database_path)
        self.assertFalse(store.dependencies_satisfied(str(task["id"])))

    def test_dispatch_requires_fresh_scope_after_verification_contract_change(self) -> None:
        self.invoke("init")
        _, created = self.invoke(
            "work",
            "add",
            "Build the bounded artifact",
            "--status",
            "ready",
            "--criterion",
            "The artifact is verified",
        )
        status, displayed = self.invoke(
            "work",
            "authorization-scope",
            str(created["id"]),
        )
        self.assertEqual(status, 0)

        contract_path = self.root / "verification.json"
        contract_path.write_text(
            json.dumps({"artifacts": [], "checks": []}),
            encoding="utf-8",
        )
        status, changed = self.invoke(
            "work",
            "verification-contract",
            str(created["id"]),
            "--set",
            str(contract_path),
            "--expected-version",
            str(created["version"]),
        )
        self.assertEqual(status, 0)
        self.assertGreater(
            changed["authorization_scope_revision"],
            displayed["authorization_scope_revision"],
        )

        stale_status, _ = self.invoke(
            "work",
            "dispatch",
            str(created["id"]),
            "--expected-version",
            str(displayed["work_version"]),
            "--expected-scope-revision",
            str(displayed["authorization_scope_revision"]),
            "--expected-scope-digest",
            str(displayed["authorization_scope_digest"]),
        )
        self.assertEqual(stale_status, 1)

        status, fresh = self.invoke(
            "work",
            "authorization-scope",
            str(created["id"]),
        )
        self.assertEqual(status, 0)
        status, dispatched = self.invoke(
            "work",
            "dispatch",
            str(created["id"]),
            "--expected-version",
            str(fresh["work_version"]),
            "--expected-scope-revision",
            str(fresh["authorization_scope_revision"]),
            "--expected-scope-digest",
            str(fresh["authorization_scope_digest"]),
        )
        self.assertEqual(status, 0)
        self.assertTrue(dispatched["metadata"]["governance"]["execution_authorized"])

    def test_recurring_reminder_can_be_created_and_completed_from_cli(self) -> None:
        self.invoke("init")
        status, created = self.invoke(
            "work",
            "add",
            "Weekly planning review",
            "--kind",
            "reminder",
            "--status",
            "ready",
            "--due",
            "2020-01-06T09:00:00Z",
            "--recurrence",
            "P1W",
        )
        self.assertEqual(status, 0)
        self.assertEqual(created["recurrence_rule"], "P1W")

        status, advanced = self.invoke(
            "work",
            "reminder",
            str(created["id"]),
            "complete",
            "--expected-version",
            str(created["version"]),
        )

        self.assertEqual(status, 0)
        self.assertEqual(advanced["status"], "ready")
        self.assertNotEqual(advanced["due_at"], created["due_at"])

    def test_event_list_filters_and_replays_one_dead_letter(self) -> None:
        self.invoke("init")
        status, ingested = self.invoke(
            "ingest",
            "--source",
            "operator",
            "--type",
            "operator.test",
            "--payload",
            '{"request":"review"}',
        )
        self.assertEqual(status, 0)
        store = SQLiteStore(load_config(self.config_path).operator.database_path)
        claimed = store.claim_events("test-worker", 1, 60)
        store.fail_events(
            [str(ingested["event_id"])],
            "test failure",
            max_attempts=1,
            claim_token=claimed[0]["claim_token"],
        )

        status, listed = self.invoke(
            "event",
            "list",
            "--state",
            "dead_letter",
            "--source",
            "operator",
            "--type",
            "operator.test",
        )
        self.assertEqual(status, 0)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["event_type"], "operator.test")

        status, replayed = self.invoke(
            "event",
            "replay",
            str(ingested["event_id"]),
            "--reason",
            "Reviewed and corrected the cause",
        )
        self.assertEqual(status, 0)
        self.assertEqual(replayed["state"], EventState.PENDING.value)
        self.assertEqual(replayed["attempt_count"], 0)

    def test_environment_config_path_is_used_when_flag_is_omitted(self) -> None:
        self.invoke("init")
        stdout = io.StringIO()
        previous = os.environ.get("HERMES_OPERATOR_CONFIG")
        os.environ["HERMES_OPERATOR_CONFIG"] = str(self.config_path)
        try:
            with contextlib.redirect_stdout(stdout):
                status = main(["status"])
        finally:
            if previous is None:
                os.environ.pop("HERMES_OPERATOR_CONFIG", None)
            else:
                os.environ["HERMES_OPERATOR_CONFIG"] = previous

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["health"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
