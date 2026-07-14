"""Portable desired-state adapter for Hermes-native skills and cron jobs."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from .adapters.base import AdapterCommandError, AdapterUnavailableError
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
                    "attention list with each work or question ID, "
                    "title, due time, and the smallest useful next action. Also call "
                    "operator_next_work and include only one high-value next task when useful. "
                    "Tell the operator they can reply with the work ID to mark it "
                    "done or snooze it; use operator_update_work when they do. Do not contact "
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
    targets are deployment-owned. Existing jobs with the stable managed names
    are left untouched; operators use native ``hermes cron edit`` for changes.
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

    def install(self, *, dry_run: bool = False) -> dict[str, Any]:
        if not self.config.native_automation.enabled:
            raise ValueError("native_automation.enabled must be true before installation")
        help_text = self._run([*self._prefix(), "cron", "--help"])
        if "create" not in help_text or "list" not in help_text:
            raise AdapterUnavailableError(
                "Hermes Cron does not expose the required create and list commands"
            )
        existing = self._run([*self._prefix(), "cron", "list"])
        installed: list[str] = []
        skipped: list[str] = []
        commands: list[list[str]] = []
        for job in desired_native_jobs(self.config):
            argv = self._create_argv(job)
            commands.append(argv)
            if job.name in existing:
                skipped.append(job.name)
                continue
            if not dry_run:
                self._run(argv)
            installed.append(job.name)
        return {
            "dry_run": dry_run,
            "installed": installed,
            "skipped": skipped,
            "commands": commands,
            "note": (
                "Enable Hermes cron mirror delivery globally or edit jobs with "
                "attach_to_session when continuable replies are desired."
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
