"""Portable desired-state adapter for Hermes-native skills and cron jobs."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from .adapters.base import (
    AdapterCommandError,
    AdapterResponseError,
    AdapterUnavailableError,
)
from .config import AppConfig


_SAFE_ENV_NAMES = {
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERNAME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_JOB_STATE = re.compile(r"\[(active|paused|completed|disabled)\]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NativeJobSpec:
    name: str
    schedule: str
    prompt: str
    skills: tuple[str, ...]
    delivery: str
    continuable: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["skills"] = list(self.skills)
        return value


def desired_native_jobs(config: AppConfig) -> list[NativeJobSpec]:
    """Return the environment-independent Hermes cron contract.

    The jobs deliberately use Hermes' bundled Google Workspace and Obsidian
    skills. OAuth, vault discovery, gateway delivery, and scheduling remain
    native Hermes concerns. The Operator supplies durable work state and the
    narrow bridge tools referenced by each prompt.
    """

    native = config.native_automation
    jobs: list[NativeJobSpec] = []
    if native.google_intake_enabled:
        jobs.append(
            NativeJobSpec(
                name="Hermes Operator: Google intake",
                schedule=native.google_intake_schedule,
                skills=(native.google_skill,),
                delivery=native.delivery,
                prompt=(
                    "Act as the read-only intake sensor for Hermes Operator. Call "
                    "operator_status first; if the control plane is unavailable, report "
                    "that privately and stop. Check Google "
                    "Workspace authentication, then inspect recent Gmail messages and "
                    "upcoming or changed Calendar meetings for requests, commitments, "
                    "deadlines, decisions, follow-ups, and preparation work. Fetch full "
                    "content only when needed for accurate triage. When meeting notes or "
                    "transcripts are available in Drive, capture their action-bearing facts "
                    "as google.meeting evidence. Submit bounded batches through "
                    "operator_ingest_inbound using source google.gmail, google.calendar, "
                    "or google.meeting, stable provider IDs, and provider revisions when "
                    "available. Re-reading a time window is expected because the Operator "
                    "deduplicates revisions durably. Never send or reply to email, change "
                    "labels, RSVP, create or edit calendar events, share files, or modify "
                    "Google data. Return only [SILENT] after a successful poll with no "
                    "operator-facing problem."
                ),
            )
        )
    if native.reminder_delivery_enabled:
        jobs.append(
            NativeJobSpec(
                name="Hermes Operator: due reminders",
                schedule=native.reminder_schedule,
                skills=(),
                delivery=native.delivery,
                continuable=native.attach_to_session,
                prompt=(
                    "Call operator_status first; if the control plane is unavailable, "
                    "report that privately and stop. Call operator_claim_attention once. "
                    "If it returns no reminders or "
                    "questions, return only [SILENT]. Otherwise produce a concise private "
                    "attention list with each work or question ID, the current work "
                    "version when present, title, due time, and the smallest useful next "
                    "action. Also call "
                    "operator_next_work and include only one high-value next task when useful. "
                    "Tell the operator they can reply with the work ID to complete, "
                    "acknowledge, or snooze it. After the operator chooses, use "
                    "operator_resolve_reminder with the exact returned work version; snooze "
                    "sets only a temporary reminder override and never changes due_at. Do not contact "
                    "any third party and do not invoke messaging tools. The final response "
                    "is the entire reminder delivery."
                ),
            )
        )
    if native.briefing_enabled:
        jobs.append(
            NativeJobSpec(
                name="Hermes Operator: daily briefing",
                schedule=native.briefing_schedule,
                skills=(native.obsidian_skill,),
                delivery=native.delivery,
                continuable=native.attach_to_session,
                prompt=(
                    "Prepare the operator's private daily briefing. Call operator_status "
                    "first and surface any unhealthy operational counters. Call "
                    "operator_next_work "
                    "for the ranked work queue, operator_open_questions for pending context, "
                    "and operator_due_reminders for overdue commitments. Use the Obsidian "
                    "skill only when vault notes materially clarify current projects or "
                    "preferences; resolve OBSIDIAN_VAULT_PATH natively and do not build a "
                    "second index. Summarize what changed, the three best next actions, "
                    "blockers, due reminders, and at most three high-value questions. Do not "
                    "send messages or publish anything. The final response is the briefing "
                    "that Hermes Cron delivers to the configured private target."
                ),
            )
        )
    return jobs


class HermesNativeAutomationManager:
    """Install the desired jobs through the documented Hermes CLI surface.

    This adapter never edits Hermes' job files and never imports Hermes Python
    modules. An explicit install command is required because OAuth and delivery
    targets are deployment-owned. Existing jobs are changed only when the
    operator explicitly requests reconciliation.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config = config
        binary = config.hermes.binary
        self.binary = (binary,) if isinstance(binary, str) else tuple(binary)
        self.profile = config.hermes.profile
        self.timeout = config.hermes.command_timeout_seconds
        self.env = {
            name: os.environ[name]
            for name in config.hermes.pass_env
            if name in os.environ
        }
        self._runner = runner

    def plan(self) -> dict[str, Any]:
        jobs = desired_native_jobs(self.config)
        return {
            "enabled": self.config.native_automation.enabled,
            "profile": self.profile,
            "jobs": [job.to_dict() for job in jobs],
            "requirements": {
                "google_oauth": any(
                    self.config.native_automation.google_skill in job.skills
                    for job in jobs
                ),
                "gateway_scheduler": bool(jobs),
                "operator_plugin_on_cron_profile": bool(jobs),
                "private_delivery_target": self.config.native_automation.delivery,
                "continuable_delivery": any(job.continuable for job in jobs),
            },
        }

    def status(self) -> dict[str, Any]:
        """Read whether every desired managed job and scheduler are active."""

        if not self.config.native_automation.enabled:
            return {
                "ok": True,
                "enabled": False,
                "installed": [],
                "missing": [],
                "private_delivery": False,
                "detail": "Hermes native automation is disabled",
            }
        help_text = self._run([*self._prefix(), "cron", "--help"])
        if "list" not in help_text or "status" not in help_text:
            raise AdapterUnavailableError(
                "Hermes Cron does not expose the required list and status commands"
            )
        existing = self._run([*self._prefix(), "cron", "list", "--all"])
        scheduler = self._run([*self._prefix(), "cron", "status"])
        records = self._job_records(existing)
        jobs = desired_native_jobs(self.config)
        installed = [job.name for job in jobs if job.name in records]
        missing = [job.name for job in jobs if job.name not in records]
        duplicates = [
            job.name
            for job in jobs
            if int(records.get(job.name, {}).get("instances", 0)) > 1
        ]
        inactive = [
            job.name
            for job in jobs
            if records.get(job.name, {}).get("state") != "active"
            and job.name in records
        ]
        delivery_mismatch = [
            job.name
            for job in jobs
            if job.name in records
            and job.delivery not in records[job.name].get("delivery", ())
        ]
        delivery = self.config.native_automation.delivery.strip().lower()
        private_delivery = bool(delivery and delivery != "local")
        normalized_scheduler = _ANSI_ESCAPE.sub("", scheduler).lower()
        scheduler_ok = (
            "cron jobs will fire automatically" in normalized_scheduler
            or "jobs fire via the managed scheduler" in normalized_scheduler
        ) and not any(
            marker in normalized_scheduler
            for marker in (
                "will not fire",
                "won't fire",
                "stalled",
                "may not be firing",
                "ticks may be failing",
            )
        )
        ok = (
            not missing
            and not duplicates
            and not inactive
            and not delivery_mismatch
            and private_delivery
            and scheduler_ok
        )
        detail = (
            "All managed jobs, private delivery, and Hermes Cron are active"
            if ok
            else "Managed jobs, private delivery, or Hermes Cron are not ready"
        )
        return {
            "ok": ok,
            "enabled": True,
            "installed": installed,
            "missing": missing,
            "duplicates": duplicates,
            "inactive": inactive,
            "delivery_mismatch": delivery_mismatch,
            "private_delivery": private_delivery,
            "delivery": self.config.native_automation.delivery,
            "scheduler_ok": scheduler_ok,
            "detail": detail,
        }

    @staticmethod
    def _job_records(output: str) -> dict[str, dict[str, Any]]:
        """Parse the stable human-readable fields emitted by ``cron list --all``."""

        records: dict[str, dict[str, Any]] = {}
        current: dict[str, Any] | None = None

        def store_current() -> None:
            if current is None or not current.get("name"):
                return
            name = str(current["name"])
            if name in records:
                records[name]["instances"] = int(
                    records[name].get("instances", 1)
                ) + 1
                return
            current["instances"] = 1
            records[name] = current

        for raw_line in _ANSI_ESCAPE.sub("", output).splitlines():
            line = raw_line.strip()
            state = _JOB_STATE.search(line)
            if state is not None:
                store_current()
                current = {
                    "state": state.group(1).lower(),
                    "delivery": (),
                }
                continue
            if current is None:
                continue
            if line.startswith("Name:"):
                current["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Deliver:"):
                current["delivery"] = tuple(
                    value.strip()
                    for value in line.split(":", 1)[1].split(",")
                    if value.strip()
                )
        store_current()
        return records

    def install(
        self,
        *,
        dry_run: bool = False,
        reconcile: bool = False,
    ) -> dict[str, Any]:
        if not self.config.native_automation.enabled:
            raise ValueError("native_automation.enabled must be true before installation")
        help_text = self._run([*self._prefix(), "cron", "--help"])
        required_commands = {"create", "list"}
        if reconcile:
            required_commands.add("edit")
        if any(command not in help_text for command in required_commands):
            raise AdapterUnavailableError(
                "Hermes Cron does not expose required commands: "
                + ", ".join(sorted(required_commands))
            )
        # --all is required so a paused or disabled older managed job is
        # reconciled instead of being silently duplicated.
        existing = self._run([*self._prefix(), "cron", "list", "--all"])
        records = self._job_records(existing)
        duplicate_names = sorted(
            job.name
            for job in desired_native_jobs(self.config)
            if int(records.get(job.name, {}).get("instances", 0)) > 1
        )
        if duplicate_names:
            raise AdapterResponseError(
                "Hermes Cron has duplicate managed job names; resolve them before "
                "installation: " + ", ".join(duplicate_names)
            )
        installed: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        commands: list[list[str]] = []
        for job in desired_native_jobs(self.config):
            if job.name in records:
                if reconcile:
                    argv = self._edit_argv(job)
                    commands.append(argv)
                    if not dry_run:
                        self._run(argv)
                    updated.append(job.name)
                    continue
                skipped.append(job.name)
                continue
            argv = self._create_argv(job)
            commands.append(argv)
            if not dry_run:
                self._run(argv)
            installed.append(job.name)
        return {
            "dry_run": dry_run,
            "reconcile": reconcile,
            "installed": installed,
            "updated": updated,
            "skipped": skipped,
            "commands": commands,
            "note": (
                "Enable Hermes cron mirror delivery globally or edit jobs with "
                "attach_to_session when continuable replies are desired. Use "
                "--reconcile only after reviewing the desired managed prompts."
            ),
        }

    def _prefix(self) -> list[str]:
        argv = list(self.binary)
        if self.profile:
            argv.extend(("-p", self.profile))
        return argv

    def _create_argv(self, job: NativeJobSpec) -> list[str]:
        argv = [
            *self._prefix(),
            "cron",
            "create",
            job.schedule,
            job.prompt,
            "--name",
            job.name,
            "--deliver",
            job.delivery,
        ]
        for skill in job.skills:
            argv.extend(("--skill", skill))
        return argv

    def _edit_argv(self, job: NativeJobSpec) -> list[str]:
        argv = [
            *self._prefix(),
            "cron",
            "edit",
            job.name,
            "--schedule",
            job.schedule,
            "--prompt",
            job.prompt,
            "--name",
            job.name,
            "--deliver",
            job.delivery,
        ]
        if job.skills:
            for skill in job.skills:
                argv.extend(("--skill", skill))
        else:
            argv.append("--clear-skills")
        return argv

    def _run(self, argv: Sequence[str]) -> str:
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_ENV_NAMES or key.startswith("LC_")
        }
        child_env.update(self.env)
        try:
            result = self._runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                shell=False,
                env=child_env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
            raise AdapterUnavailableError(f"Hermes Cron is unavailable: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no error output").strip()
            raise AdapterCommandError(
                f"Hermes Cron command failed with exit code {result.returncode}: {detail}",
                argv=argv,
                returncode=result.returncode,
                stderr=(result.stderr or "").strip(),
            )
        return (result.stdout or "").strip()
