from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.config import (  # noqa: E402
    AppConfig,
    HermesConfig,
    LLMConfig,
    NativeAutomationConfig,
    ObsidianConfig,
    OperatorConfig,
    PolicyConfig,
    ServerConfig,
)
from hermes_operator.native_automation import (  # noqa: E402
    HermesNativeAutomationManager,
    desired_native_jobs,
)


class NativeAutomationTests(unittest.TestCase):
    def config(self, temporary: str) -> AppConfig:
        root = Path(temporary)
        return AppConfig(
            config_path=root / "operator.toml",
            operator=OperatorConfig(
                database_path=root / "operator.db",
                data_dir=root,
            ),
            llm=LLMConfig(provider="command", command=["planner"]),
            hermes=HermesConfig(
                enabled=True,
                binary=["docker", "exec", "hermes", "hermes"],
                profile="operator",
            ),
            obsidian=ObsidianConfig(),
            server=ServerConfig(enabled=False),
            policy=PolicyConfig(),
            native_automation=NativeAutomationConfig(
                enabled=True,
                delivery="telegram",
            ),
        )

    def test_plan_uses_hermes_google_cron_and_obsidian_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(temporary)
            jobs = desired_native_jobs(config)

        self.assertEqual(len(jobs), 3)
        intake = next(job for job in jobs if "Google intake" in job.name)
        briefing = next(job for job in jobs if "daily briefing" in job.name)
        reminders = next(job for job in jobs if "due reminders" in job.name)
        self.assertEqual(intake.skills, ("google-workspace",))
        self.assertIn("operator_status", intake.prompt)
        self.assertIn("operator_ingest_inbound", intake.prompt)
        self.assertIn("Never send or reply", intake.prompt)
        self.assertEqual(briefing.skills, ("obsidian",))
        self.assertIn("operator_next_work", briefing.prompt)
        self.assertIn("operator_claim_attention", reminders.prompt)
        self.assertIn("operator_status", reminders.prompt)
        self.assertIn("operator_resolve_reminder", reminders.prompt)
        self.assertNotIn("use operator_update_work", reminders.prompt)
        self.assertIn("never changes due_at", reminders.prompt)
        self.assertTrue(reminders.continuable)

    def test_explicit_install_is_idempotent_by_managed_job_name(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            del kwargs
            calls.append(list(argv))
            if argv[-2:] == ["cron", "--help"]:
                output = "commands: create list edit pause resume remove status"
            elif argv[-2:] == ["cron", "list"]:
                output = "Hermes Operator: daily briefing  0 8 * * *"
            else:
                output = "created"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as temporary:
            manager = HermesNativeAutomationManager(
                self.config(temporary),
                runner=runner,
            )
            result = manager.install()

        self.assertEqual(result["skipped"], ["Hermes Operator: daily briefing"])
        self.assertCountEqual(
            result["installed"],
            [
                "Hermes Operator: Google intake",
                "Hermes Operator: due reminders",
            ],
        )
        create_calls = [call for call in calls if "create" in call]
        self.assertEqual(len(create_calls), 2)
        self.assertTrue(
            all(
                call[:6]
                == ["docker", "exec", "hermes", "hermes", "-p", "operator"]
                for call in create_calls
            )
        )
        self.assertTrue(all("--deliver" in call for call in create_calls))
        intake_call = next(
            call
            for call in create_calls
            if any("Google intake" in value for value in call)
        )
        self.assertIn("google-workspace", intake_call)
        self.assertNotIn("--attach-to-session", intake_call)

    def test_dry_run_reports_commands_without_creating_jobs(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            del kwargs
            calls.append(list(argv))
            output = "create list" if argv[-1] == "--help" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as temporary:
            manager = HermesNativeAutomationManager(
                self.config(temporary),
                runner=runner,
            )
            result = manager.install(dry_run=True)

        self.assertEqual(len(result["commands"]), 3)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
