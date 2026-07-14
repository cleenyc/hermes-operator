from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from .authority import execution_scope_digest, execution_scope_document
from .config import load_config
from .dispatcher import dispatch_contract_digest
from .inbound import normalize_operator_event
from .models import (
    EventState,
    ExecutionMode,
    WorkItem,
    WorkKind,
    WorkRelation,
    WorkStatus,
    utc_now,
)
from .native_automation import HermesNativeAutomationManager
from .service import OperatorService
from .verifier import (
    VERIFICATION_REQUIREMENTS,
    validate_verification_contract,
    validate_work_verification_readiness,
)


CONFIG_TEMPLATE = """[operator]
instance_id = "personal-operator"
database_path = "data/operator.db"
data_dir = "data"
timezone = "America/New_York"
autonomy_mode = "shadow"
tick_seconds = 30
reconciliation_seconds = 300
reasoning_refresh_seconds = 3600
max_events_per_pass = 25
max_parallel_work = 4
max_authorizations_per_pass = 40
event_lease_seconds = 300
event_max_attempts = 5

[llm]
provider = "openai_compatible"
model = "configure-at-deployment"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 180
temperature = 0.1
max_output_tokens = 8000
command = []
# Local command planners receive a minimal environment plus only these names.
pass_env = []

[hermes]
enabled = false
active_isolation_acknowledged = false
binary = "hermes"
profile = "operator"
board = "default"
default_assignee = "operator"
orchestrator_profile = "operator"
command_timeout_seconds = 120
goal_mode = false
default_skills = []
allowed_profiles = ["operator"]
allowed_skills = []
# Never pass control-plane or outbound credentials to Hermes.
pass_env = []
dispatch_authorization_ttl_seconds = 86400
max_execution_attempts = 3
control_base_url = ""
control_token_env = "HERMES_KANBAN_CONTROL_TOKEN"
control_timeout_seconds = 10
require_policy_attestation = true
policy_attestation_ttl_seconds = 300
allowed_plugin_versions = ["1.6.0"]
allowed_policy_versions = ["7.0.0"]
allowed_policy_digests = ["15f8e0a622abce0227c9b3f6b0168cf98bd17218933881b4d50f705edf2278d5"]

[obsidian]
enabled = false
vault_path = ""
discover = true
operator_root = "Hermes Operator"
write_mode = "projection"

[native_automation]
enabled = false
delivery = "local"
google_intake_enabled = true
google_intake_schedule = "every 10m"
reminder_delivery_enabled = true
reminder_schedule = "every 15m"
attention_redelivery_seconds = 3600
briefing_enabled = true
briefing_schedule = "0 8 * * *"
attach_to_session = true
google_skill = "google-workspace"
obsidian_skill = "obsidian"

[server]
enabled = true
host = "${HERMES_OPERATOR_BIND_HOST:-127.0.0.1}"
port = 8787
api_token_env = "HERMES_OPERATOR_API_TOKEN"
bridge_token_env = "HERMES_OPERATOR_BRIDGE_TOKEN"
bridge_proof_secret_env = "HERMES_OPERATOR_BRIDGE_PROOF_SECRET"
max_body_bytes = 1048576
allow_unsigned_webhooks = false

[server.webhook_secrets]

[policy]
external_actions_require_approval = true
external_action_mode = "stage_only"
approval_ttl_seconds = 3600
approval_secret_env = "HERMES_OPERATOR_APPROVAL_SECRET"
trusted_event_sources = ["operator", "system"]
allow_memory_auto_promotion = false
max_llm_priority_adjustment = 10.0

[verification]
enabled = true
max_artifacts = 64
max_files_per_directory = 2000
max_artifact_bytes = 268435456
max_total_artifact_bytes = 536870912

[verification.artifact_roots]
# workspace = "${HERMES_OPERATOR_WORKSPACE:-/srv/hermes/workspace}"

# [[verification.checks]]
# name = "unit-tests"
# command = ["python", "-m", "pytest", "-q"]
# cwd = "${HERMES_OPERATOR_WORKSPACE:-/srv/hermes/workspace}"
# timeout_seconds = 300
# max_output_bytes = 1048576
# pass_env = []

# Optional read-only command poller. Duplicate this table per inbound surface.
# [[inbound_connectors]]
# name = "mail-reader"
# source = "mail"
# command = ["/opt/hermes-connectors/mail-reader"]
# enabled = true
# interval_seconds = 60
# timeout_seconds = 60
# pass_env = ["MAIL_READ_TOKEN"]
# max_output_bytes = 4194304
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-operator",
        description="Portable autonomous control plane for Hermes Agent",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default="INFO")
    subcommands = parser.add_subparsers(dest="command", required=True)

    initialize = subcommands.add_parser("init", help="Create a portable configuration")
    initialize.add_argument("--force", action="store_true")

    doctor = subcommands.add_parser(
        "doctor", help="Validate configuration and integrations"
    )
    doctor.add_argument(
        "--live",
        action="store_true",
        help="Run explicit model and active-execution readiness probes",
    )
    subcommands.add_parser("run", help="Run the live autonomous service")
    once = subcommands.add_parser("run-once", help="Execute one control-plane cycle")
    once.add_argument("--no-reconcile", action="store_true")
    subcommands.add_parser("status", help="Show current operator state")

    ingest = subcommands.add_parser("ingest", help="Record an operator-trusted event")
    ingest.add_argument("--source", default="operator")
    ingest.add_argument("--type", required=True, dest="event_type")
    ingest.add_argument("--external-id")
    ingest.add_argument("--dedupe-key")
    payload = ingest.add_mutually_exclusive_group(required=True)
    payload.add_argument("--payload", help="JSON object")
    payload.add_argument("--payload-file", type=Path)

    event = subcommands.add_parser(
        "event", help="Inspect events or replay a reviewed dead letter"
    )
    event_commands = event.add_subparsers(dest="event_command", required=True)
    event_list = event_commands.add_parser("list")
    event_list.add_argument(
        "--state",
        action="append",
        choices=[value.value for value in EventState],
    )
    event_list.add_argument("--source", action="append")
    event_list.add_argument("--type", dest="event_type", action="append")
    event_list.add_argument("--limit", type=int, default=200)
    event_replay = event_commands.add_parser("replay")
    event_replay.add_argument("event_id")
    event_replay.add_argument("--reason", required=True)

    audit = subcommands.add_parser("audit", help="Inspect the append-only audit log")
    audit.add_argument("--actor", action="append")
    audit.add_argument("--event", dest="audit_event", action="append")
    audit.add_argument("--entity-type")
    audit.add_argument("--entity-id")
    audit.add_argument("--limit", type=int, default=200)

    next_command = subcommands.add_parser("next", help="Show ranked next work")
    next_command.add_argument("--limit", type=int, default=5)

    work = subcommands.add_parser("work", help="Manage goals, projects, and tasks")
    work_commands = work.add_subparsers(dest="work_command", required=True)
    work_list = work_commands.add_parser("list")
    work_list.add_argument("--status", action="append")
    work_list.add_argument("--kind", action="append")
    work_list.add_argument("--limit", type=int, default=200)
    work_show = work_commands.add_parser("show")
    work_show.add_argument("work_id")
    work_add = work_commands.add_parser("add")
    work_add.add_argument("title")
    work_add.add_argument("--kind", choices=[value.value for value in WorkKind], default="task")
    work_add.add_argument("--status", choices=[value.value for value in WorkStatus], default="triage")
    work_add.add_argument("--description", default="")
    work_add.add_argument("--parent")
    work_add.add_argument("--due")
    work_add.add_argument("--scheduled")
    work_add.add_argument(
        "--recurrence",
        help="Fixed ISO-8601 interval: PTnM, PTnH, PnD, or PnW",
    )
    work_add.add_argument("--assignee")
    work_add.add_argument("--execution", choices=[value.value for value in ExecutionMode], default="none")
    work_add.add_argument("--criterion", action="append", default=[])
    work_add.add_argument("--priority", type=int, default=0)
    work_add.add_argument("--impact", type=float, default=0.5)
    work_add.add_argument("--urgency", type=float, default=0.5)
    work_add.add_argument("--alignment", type=float, default=0.5)
    work_add.add_argument("--effort", type=int, default=30)
    work_update = work_commands.add_parser("update")
    work_update.add_argument("work_id")
    work_update.add_argument("--title")
    work_update.add_argument("--description")
    work_update.add_argument("--status", choices=[value.value for value in WorkStatus])
    work_update.add_argument("--parent")
    work_update.add_argument("--clear-parent", action="store_true")
    work_update.add_argument("--due")
    work_update.add_argument("--clear-due", action="store_true")
    work_update.add_argument("--recurrence")
    work_update.add_argument("--clear-recurrence", action="store_true")
    work_update.add_argument("--assignee")
    work_update.add_argument("--priority", type=int)
    work_update.add_argument("--execution", choices=[value.value for value in ExecutionMode])
    work_reminder = work_commands.add_parser(
        "reminder", help="Snooze, acknowledge, or complete a reminder"
    )
    work_reminder.add_argument("work_id")
    work_reminder.add_argument(
        "action", choices=["snooze", "acknowledge", "complete"]
    )
    work_reminder.add_argument("--until")
    work_reminder.add_argument("--expected-version", type=int, required=True)
    work_scope = work_commands.add_parser(
        "authorization-scope",
        help="Preview the exact execution scope that a dispatch will authorize",
    )
    work_scope.add_argument("work_id")
    work_scope.add_argument("--profile")
    work_scope.add_argument("--skill", action="append", default=[])
    work_scope.add_argument("--goal-mode", action="store_true")
    work_verification = work_commands.add_parser(
        "verification-contract",
        help="Set or clear verifier scope before a fresh authorization preview",
    )
    work_verification.add_argument("work_id")
    verification_change = work_verification.add_mutually_exclusive_group(
        required=True
    )
    verification_change.add_argument("--set", type=Path, dest="contract_file")
    verification_change.add_argument("--clear", action="store_true")
    work_verification.add_argument("--expected-version", type=int, required=True)
    work_verification.add_argument(
        "--assurance",
        choices=sorted(VERIFICATION_REQUIREMENTS),
        help="Set the protected completion assurance for this work",
    )
    work_dispatch = work_commands.add_parser("dispatch")
    work_dispatch.add_argument("work_id")
    work_dispatch.add_argument("--profile")
    work_dispatch.add_argument("--skill", action="append", default=[])
    work_dispatch.add_argument("--goal-mode", action="store_true")
    work_dispatch.add_argument("--expected-version", type=int, required=True)
    work_dispatch.add_argument(
        "--expected-scope-revision", type=int, required=True
    )
    work_dispatch.add_argument("--expected-scope-digest", required=True)
    work_link = work_commands.add_parser("link")
    work_link.add_argument("from_id")
    work_link.add_argument("to_id")
    work_link.add_argument(
        "--relation",
        choices=[value.value for value in WorkRelation],
        default=WorkRelation.DEPENDS_ON.value,
    )
    work_link.add_argument("--from-version", type=int, required=True)
    work_link.add_argument("--to-version", type=int, required=True)

    run = subcommands.add_parser("run-state", help="Inspect or resolve Hermes runs")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_list = run_commands.add_parser("list")
    run_list.add_argument("--status")
    run_list.add_argument("--limit", type=int, default=200)
    run_resolve = run_commands.add_parser("resolve")
    run_resolve.add_argument("run_id")
    run_resolve.add_argument(
        "--expected-status",
        required=True,
        choices=["queued", "lost", "legacy_conflict", "blocked", "cancel_requested"],
    )
    run_resolve.add_argument("--reason", required=True)

    question = subcommands.add_parser("question", help="Review or answer questions")
    question_commands = question.add_subparsers(dest="question_command", required=True)
    question_list = question_commands.add_parser("list")
    question_list.add_argument(
        "--status", choices=["pending", "answered", "dismissed", "all"], default="pending"
    )
    question_answer = question_commands.add_parser("answer")
    question_answer.add_argument("question_id")
    question_answer.add_argument("answer")

    approval = subcommands.add_parser("approval", help="Review exact external actions")
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_commands.add_parser("list")
    approval_list.add_argument("--status", default="pending_approval")
    approval_show = approval_commands.add_parser("show")
    approval_show.add_argument("action_id")
    approval_approve = approval_commands.add_parser("approve")
    approval_approve.add_argument("action_id")
    approval_deny = approval_commands.add_parser("deny")
    approval_deny.add_argument("action_id")
    approval_deny.add_argument("--reason", default="")

    memory = subcommands.add_parser("memory", help="Review long-term memory candidates")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_list = memory_commands.add_parser("list")
    memory_list.add_argument("--status")
    memory_list.add_argument("--limit", type=int, default=200)
    memory_show = memory_commands.add_parser("show")
    memory_show.add_argument("memory_id")
    memory_promote = memory_commands.add_parser("promote")
    memory_promote.add_argument("memory_id")
    memory_reject = memory_commands.add_parser("reject")
    memory_reject.add_argument("memory_id")

    project = subcommands.add_parser("project", help="Refresh the Obsidian projection")
    project.add_argument("--vault", type=Path)
    project.add_argument("--create", action="store_true")

    native = subcommands.add_parser(
        "native-jobs",
        help="Plan or install Hermes-native Google, reminder, and briefing jobs",
    )
    native_commands = native.add_subparsers(dest="native_command", required=True)
    native_commands.add_parser("plan")
    native_install = native_commands.add_parser("install")
    native_install.add_argument("--dry-run", action="store_true")
    native_install.add_argument(
        "--reconcile",
        action="store_true",
        help="Explicitly update existing managed jobs to the current desired prompts",
    )
    return parser


def _emit(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str))


def _load_payload(arguments: argparse.Namespace) -> dict[str, Any]:
    raw = (
        arguments.payload_file.read_text(encoding="utf-8")
        if arguments.payload_file is not None
        else arguments.payload
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Payload must be a JSON object")
    return value


def _initialize(path: Path, *, force: bool) -> int:
    target = path.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"Configuration already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    _emit({"created": str(target), "next": "Configure the model, then run doctor"})
    return 0


def _doctor(service: OperatorService, *, live: bool = False) -> int:
    llm = service.config.llm
    if llm.provider == "openai_compatible":
        llm_ok = bool(llm.model and llm.model != "configure-at-deployment" and llm.resolved_api_key())
        llm_detail = "configured" if llm_ok else "model or API key is not configured"
    else:
        executable = llm.command[0] if llm.command else ""
        resolved = shutil.which(executable) if executable else None
        if executable and ("/" in executable or "\\" in executable):
            path = Path(executable).expanduser()
            resolved = (
                str(path)
                if path.is_file() and os.access(path, os.X_OK)
                else None
            )
        llm_ok = bool(llm.command and resolved)
        llm_detail = (
            f"configured executable: {resolved}"
            if llm_ok
            else (
                "command executable was not found"
                if llm.command
                else "command is empty"
            )
        )
    hermes = (
        asdict(service.hermes.health())
        if service.hermes is not None
        else {"enabled": False, "available": False, "detail": "disabled"}
    )
    obsidian = asdict(service.obsidian.health())
    checks = {
        "configuration": {"ok": True, "path": str(service.config.config_path)},
        "database": {"ok": True, "path": str(service.store.path)},
        "llm": {"ok": llm_ok, "provider": llm.provider, "detail": llm_detail},
        "hermes": {"ok": bool(hermes.get("available")) or not service.config.hermes.enabled, **hermes},
        "obsidian": {"ok": bool(obsidian.get("available")) or not service.config.obsidian.enabled, **obsidian},
        "external_actions": {
            "ok": service.config.policy.external_actions_require_approval,
            "mode": service.config.policy.external_action_mode,
            "daemon_has_outbound_connectors": False,
        },
    }
    if live:
        model_probe: dict[str, object]
        if not llm_ok:
            model_probe = {
                "ok": False,
                "detail": "model configuration is not ready",
            }
        else:
            try:
                response = asyncio.run(
                    service.llm.generate_json(
                        system=(
                            "This is a read-only Hermes Operator readiness probe. "
                            "Return exactly one JSON object."
                        ),
                        user=(
                            'Return {"ok":true,'
                            '"probe":"hermes-operator-readiness"}.'
                        ),
                    )
                )
                valid = bool(
                    response.data.get("ok") is True
                    and response.data.get("probe")
                    == "hermes-operator-readiness"
                )
                model_probe = {
                    "ok": valid,
                    "detail": (
                        "model probe passed"
                        if valid
                        else "model returned an unexpected readiness object"
                    ),
                    "model": response.model,
                }
            except Exception as error:
                model_probe = {
                    "ok": False,
                    "detail": f"{type(error).__name__}: {error}"[:2000],
                }
        checks["model_live"] = model_probe

        if service.dispatcher is not None:
            profiles = sorted(
                {
                    value
                    for value in [
                        service.config.hermes.profile,
                        service.config.hermes.default_assignee,
                        service.config.hermes.orchestrator_profile,
                        *service.config.hermes.allowed_profiles,
                    ]
                    if value
                }
            )
            attestations: list[dict[str, object]] = []
            for profile in profiles:
                attested, reason, _ = service.dispatcher._policy_attestation(profile)
                attestations.append(
                    {
                        "ok": attested,
                        "profile": profile,
                        "detail": reason,
                    }
                )
            checks["policy_attestation"] = {
                "ok": bool(attestations)
                and all(bool(value["ok"]) for value in attestations),
                "profiles": attestations,
            }
            control_health = asdict(service.hermes.control_health())
            checks["run_control"] = {
                "ok": bool(control_health.get("available")),
                **control_health,
            }
        if service.config.native_automation.enabled:
            try:
                native_status = HermesNativeAutomationManager(
                    service.config
                ).status()
            except Exception as error:
                native_status = {
                    "ok": False,
                    "detail": f"{type(error).__name__}: {error}"[:2000],
                }
            native_status["provider_access"] = (
                "Google OAuth and native Obsidian vault access remain Hermes-owned "
                "deployment acceptance checks"
            )
            checks["native_automation"] = native_status
    overall = all(bool(check["ok"]) for check in checks.values())
    _emit({"ok": overall, "checks": checks})
    return 0 if overall else 2


def _handle_work(service: OperatorService, arguments: argparse.Namespace) -> int:
    if arguments.work_command == "list":
        items = service.store.list_work(
            statuses=arguments.status,
            kinds=arguments.kind,
            limit=arguments.limit,
        )
        _emit({"items": [item.to_dict() for item in items], "count": len(items)})
    elif arguments.work_command == "show":
        _emit(service.store.get_work(arguments.work_id).to_dict())
    elif arguments.work_command == "add":
        item = WorkItem(
            title=arguments.title,
            kind=WorkKind(arguments.kind),
            status=WorkStatus(arguments.status),
            description=arguments.description,
            parent_id=arguments.parent,
            due_at=arguments.due,
            scheduled_at=arguments.scheduled,
            recurrence_rule=arguments.recurrence,
            assignee=arguments.assignee,
            execution_mode=ExecutionMode(arguments.execution),
            acceptance_criteria=arguments.criterion,
            priority=arguments.priority,
            impact=arguments.impact,
            urgency=arguments.urgency,
            strategic_alignment=arguments.alignment,
            effort_minutes=arguments.effort,
            provenance={"source": "operator-cli", "trust_level": "operator"},
            metadata={
                "governance": {
                    "source_trust": "operator",
                    "creation_authorized": True,
                    "execution_authorized": True,
                }
            },
        )
        score = service.priority.score(
            item,
            dependencies_satisfied=service.store.dependencies_satisfied(item.id),
        )
        item.priority_score = score.score
        item.priority_rationale = score.rationale
        service.store.create_work(item, actor="operator-cli")
        _emit(item.to_dict())
    elif arguments.work_command == "authorization-scope":
        item = service.store.get_work(arguments.work_id)
        profile = (
            arguments.profile
            or item.assignee
            or service.config.hermes.default_assignee
        )
        digest = execution_scope_digest(
            item,
            profile=profile,
            skills=list(arguments.skill),
            default_skills=service.config.hermes.default_skills,
            goal_mode=arguments.goal_mode,
        )
        _emit(
            {
                "work_id": item.id,
                "work_version": item.version,
                "status": item.status.value,
                "authorizable": item.status
                not in {
                    WorkStatus.DONE,
                    WorkStatus.CANCELLED,
                    WorkStatus.ARCHIVED,
                },
                "authorization_scope_revision": (
                    item.authorization_scope_revision
                ),
                "authorization_scope_digest": digest,
                "profile": profile,
                "skills": list(arguments.skill),
                "default_skills": list(service.config.hermes.default_skills),
                "goal_mode": arguments.goal_mode,
                "scope": execution_scope_document(
                    item,
                    profile=profile,
                    skills=list(arguments.skill),
                    default_skills=service.config.hermes.default_skills,
                    goal_mode=arguments.goal_mode,
                ),
            }
        )
    elif arguments.work_command == "reminder":
        updated = service.store.resolve_reminder(
            arguments.work_id,
            action=arguments.action,
            expected_version=arguments.expected_version,
            until=arguments.until,
            actor="operator-cli",
        )
        _emit(updated.to_dict())
    elif arguments.work_command == "verification-contract":
        item = service.store.get_work(arguments.work_id)
        metadata = dict(item.metadata)
        if arguments.clear:
            metadata.pop("verification_contract", None)
        else:
            raw_contract = arguments.contract_file.read_bytes()
            if len(raw_contract) > 256_000:
                raise ValueError("Verification contract is too large")
            try:
                contract_value = json.loads(raw_contract.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "Verification contract must be UTF-8 JSON"
                ) from error
            metadata["verification_contract"] = validate_verification_contract(
                contract_value,
                service.config.verification,
            )
        if arguments.assurance is not None:
            metadata["verification_requirement"] = arguments.assurance
        if metadata.get("verification_requirement") == "deterministic_required":
            contract = metadata.get("verification_contract")
            if not isinstance(contract, dict) or not contract.get("checks"):
                raise ValueError(
                    "deterministic_required work needs at least one deployment-owned named check"
                )
        updated = service.store.update_work(
            item.id,
            {"metadata": metadata},
            actor="operator-cli",
            expected_version=arguments.expected_version,
        )
        _emit(updated.to_dict())
    elif arguments.work_command == "link":
        link_id = service.store.add_work_link(
            arguments.from_id,
            arguments.to_id,
            WorkRelation(arguments.relation),
            actor="operator-cli",
            expected_from_version=arguments.from_version,
            expected_to_version=arguments.to_version,
        )
        _emit(
            {
                "id": link_id,
                "from_id": arguments.from_id,
                "to_id": arguments.to_id,
                "relation": arguments.relation,
            }
        )
    elif arguments.work_command == "dispatch":
        item = service.store.get_work(arguments.work_id)
        validate_work_verification_readiness(
            item,
            service.config.verification,
        )
        if item.status in {
            WorkStatus.DONE,
            WorkStatus.CANCELLED,
            WorkStatus.ARCHIVED,
        }:
            raise ValueError(
                f"Terminal work cannot be dispatched: {item.status.value}"
            )
        if not item.acceptance_criteria:
            raise ValueError("Work needs acceptance criteria before dispatch")
        profile = arguments.profile or item.assignee or service.config.hermes.default_assignee
        allowed_profiles = set(service.config.hermes.allowed_profiles)
        allowed_profiles.update(
            value
            for value in (
                service.config.hermes.profile,
                service.config.hermes.default_assignee,
                service.config.hermes.orchestrator_profile,
            )
            if value
        )
        if profile not in allowed_profiles:
            raise ValueError(f"Hermes profile is not allowed: {profile}")
        allowed_skills = set(service.config.hermes.allowed_skills)
        allowed_skills.update(service.config.hermes.default_skills)
        unknown_skills = set(arguments.skill) - allowed_skills
        if unknown_skills:
            raise ValueError(f"Hermes skills are not allowed: {sorted(unknown_skills)}")
        if item.version != arguments.expected_version:
            raise ValueError("Work item changed after its scope was displayed")
        if (
            item.authorization_scope_revision
            != arguments.expected_scope_revision
        ):
            raise ValueError(
                "Work authorization scope changed after it was displayed"
            )
        displayed_digest = execution_scope_digest(
            item,
            profile=profile,
            skills=list(arguments.skill),
            default_skills=service.config.hermes.default_skills,
            goal_mode=arguments.goal_mode,
        )
        if displayed_digest != arguments.expected_scope_digest:
            raise ValueError(
                "Dispatch shape does not match the displayed authorization scope"
            )
        if item.assignee != profile:
            # Executor assignment is itself an authorization-scope transition.
            # Apply it first so any prior authority is revoked and the fresh
            # dispatch authorization below is bound to the advanced revision.
            item = service.store.update_work(
                item.id,
                {"assignee": profile},
                actor="operator-cli",
                expected_version=item.version,
            )
        metadata = dict(item.metadata)
        metadata["governance"] = {
            "source_trust": "operator",
            "creation_authorized": True,
            "execution_authorized": True,
        }
        metadata["dispatch_request"] = {
            "profile": profile,
            "skills": list(arguments.skill),
            "goal_mode": arguments.goal_mode,
            "reason": "Explicit operator CLI dispatch",
            "shadow": service.config.operator.autonomy_mode == "shadow",
        }
        # The verification contract is part of the exact dispatch digest.
        item.metadata = metadata
        contract_digest_value = dispatch_contract_digest(
            item,
            profile=profile,
            skills=list(arguments.skill),
            default_skills=service.config.hermes.default_skills,
            goal_mode=arguments.goal_mode,
        )
        authorization_scope_digest_value = execution_scope_digest(
            item,
            profile=profile,
            skills=list(arguments.skill),
            default_skills=service.config.hermes.default_skills,
            goal_mode=arguments.goal_mode,
        )
        authorization_root = hashlib.sha256(
            json.dumps(
                ["operator-cli", item.id, contract_digest_value, utc_now()],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        metadata["dispatch_authorization"] = {
            "work_id": item.id,
            "profile": profile,
            "skills": list(arguments.skill),
            "shadow": service.config.operator.autonomy_mode == "shadow",
            "issued_by": "operator-cli",
            "issued_at": utc_now(),
            "not_before": item.scheduled_at,
            "expires_at": None,
            "lifetime": "until_consumed_or_contract_change",
            "review_after": (
                datetime.now(UTC)
                + timedelta(
                    seconds=service.config.hermes.dispatch_authorization_ttl_seconds
                )
            ).isoformat().replace("+00:00", "Z"),
            "trust": "operator",
            "authorization_root": authorization_root,
            "max_attempts": service.config.hermes.max_execution_attempts,
            "authorization_kind": "operator_direct",
            "contract_digest": contract_digest_value,
            "authorization_scope_digest": authorization_scope_digest_value,
        }
        updated = service.store.update_work(
            item.id,
            {
                "status": WorkStatus.READY.value,
                "execution_mode": ExecutionMode.HERMES.value,
                "assignee": profile,
                "metadata": metadata,
            },
            actor="operator-cli",
            expected_version=item.version,
            allow_transition_override=True,
        )
        _emit(updated.to_dict())
    else:
        changes = {
            key: value
            for key, value in {
                "title": arguments.title,
                "description": arguments.description,
                "status": arguments.status,
                "due_at": arguments.due,
                "recurrence_rule": arguments.recurrence,
                "assignee": arguments.assignee,
                "priority": arguments.priority,
                "execution_mode": arguments.execution,
            }.items()
            if value is not None
        }
        if arguments.parent is not None:
            changes["parent_id"] = arguments.parent
        if arguments.clear_parent:
            changes["parent_id"] = None
        if arguments.clear_due:
            changes["due_at"] = None
        if arguments.clear_recurrence:
            changes["recurrence_rule"] = None
        updated = service.store.update_work(
            arguments.work_id,
            changes,
            actor="operator-cli",
            allow_transition_override=True,
        )
        _emit(updated.to_dict())
    return 0


def execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "init":
        configured = arguments.config or os.environ.get(
            "HERMES_OPERATOR_CONFIG", "operator.toml"
        )
        return _initialize(Path(configured), force=arguments.force)
    config = load_config(arguments.config)
    service = OperatorService(config)
    if arguments.command == "doctor":
        return _doctor(service, live=arguments.live)
    if arguments.command == "run":
        asyncio.run(service.run())
        return 0
    if arguments.command == "run-once":
        cycle = asyncio.run(
            service.run_once(force_reconcile=not arguments.no_reconcile)
        )
        _emit(cycle)
        return 2 if cycle.errors else 0
    if arguments.command == "status":
        _emit({"health": service.health(), "state": service.store.snapshot()})
        return 0
    if arguments.command == "ingest":
        envelope = {
            "source": arguments.source,
            "event_type": arguments.event_type,
            "external_id": arguments.external_id,
            "dedupe_key": arguments.dedupe_key,
            "payload": _load_payload(arguments),
        }
        normalized = normalize_operator_event(envelope, actor="operator-cli")
        event_id, created = service.store.enqueue_event(
            normalized.event, actor="operator-cli"
        )
        _emit({"event_id": event_id, "created": created})
        return 0
    if arguments.command == "event":
        if arguments.event_command == "list":
            events = service.store.list_events(
                states=arguments.state,
                sources=arguments.source,
                event_types=arguments.event_type,
                limit=arguments.limit,
            )
            _emit({"items": events, "count": len(events)})
        else:
            _emit(
                service.store.replay_dead_letter_event(
                    arguments.event_id,
                    reason=arguments.reason,
                    actor="operator-cli",
                )
            )
        return 0
    if arguments.command == "audit":
        items = service.store.list_audit(
            actors=arguments.actor,
            events=arguments.audit_event,
            entity_type=arguments.entity_type,
            entity_id=arguments.entity_id,
            limit=arguments.limit,
        )
        _emit({"items": items, "count": len(items)})
        return 0
    if arguments.command == "next":
        items = service.next_work(max(1, arguments.limit))
        _emit({"items": [item.to_dict() for item in items], "count": len(items)})
        return 0
    if arguments.command == "work":
        return _handle_work(service, arguments)
    if arguments.command == "run-state":
        if arguments.run_command == "list":
            runs = service.store.list_runs(
                status=arguments.status,
                limit=arguments.limit,
            )
            _emit({"items": runs, "count": len(runs)})
        else:
            _emit(
                service.store.resolve_run(
                    arguments.run_id,
                    expected_status=arguments.expected_status,
                    reason=arguments.reason,
                    actor="operator-cli",
                )
            )
        return 0
    if arguments.command == "question":
        if arguments.question_command == "list":
            status = None if arguments.status == "all" else arguments.status
            questions = service.store.list_questions(status=status)
            _emit({"items": questions, "count": len(questions)})
        else:
            _emit(
                service.store.answer_question(
                    arguments.question_id, arguments.answer, actor="operator-cli"
                )
            )
        return 0
    if arguments.command == "approval":
        if arguments.approval_command == "list":
            actions = service.actions.list(status=arguments.status)
            _emit({"items": [value.to_dict() for value in actions], "count": len(actions)})
        elif arguments.approval_command == "show":
            _emit(service.actions.get(arguments.action_id).to_dict())
        elif arguments.approval_command == "approve":
            grant = service.actions.approve(
                arguments.action_id, approved_by="operator-cli"
            )
            _emit(
                {
                    "action_id": arguments.action_id,
                    "status": "approved",
                    "grant_id": grant.grant_id,
                    "expires_at": grant.expires_at,
                }
            )
        else:
            service.actions.deny(
                arguments.action_id,
                denied_by="operator-cli",
                reason=arguments.reason,
            )
            _emit({"action_id": arguments.action_id, "status": "denied"})
        return 0
    if arguments.command == "memory":
        if arguments.memory_command == "list":
            memories = service.store.list_memory(
                status=arguments.status, limit=arguments.limit
            )
            _emit({"items": memories, "count": len(memories)})
        elif arguments.memory_command == "show":
            _emit(service.store.get_memory(arguments.memory_id))
        else:
            decision = (
                "promoted" if arguments.memory_command == "promote" else "rejected"
            )
            _emit(
                service.store.review_memory(
                    arguments.memory_id,
                    decision=decision,
                    actor="operator-cli",
                )
            )
        return 0
    if arguments.command == "project":
        if arguments.vault is not None:
            if not service.obsidian.configure(arguments.vault, create=arguments.create):
                raise ValueError(service.obsidian.disabled_reason)
        _emit(asdict(service.projector.project()))
        return 0
    if arguments.command == "native-jobs":
        manager = HermesNativeAutomationManager(config)
        if arguments.native_command == "plan":
            _emit(manager.plan())
        else:
            _emit(
                manager.install(
                    dry_run=arguments.dry_run,
                    reconcile=arguments.reconcile,
                )
            )
        return 0
    raise ValueError(f"Unsupported command: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(arguments.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return execute(arguments)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.getLogger(__name__).error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
