"""Hermes native bridge for the autonomous operator control plane."""

from __future__ import annotations

import atexit
from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
from pathlib import Path
import shlex
from threading import Lock
from typing import Any

from .client import OperatorClient
from .config import ConfigurationError, PluginConfig
from .compatibility import (
    bridge_activation_blockers,
    detect_delegate_mode,
    diagnose_host,
)
from .hooks import LifecycleEmitter, PolicyAttestationRefresher, build_hooks
from .policy import (
    POLICY_DIGEST,
    POLICY_MODE,
    POLICY_VERSION,
    TaskScopedPolicyGuard,
    guard_external_side_effects,
)
from . import schemas, tools

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "1.6.0"

_active_refresher: PolicyAttestationRefresher | None = None
_active_refresher_lock = Lock()


@lru_cache(maxsize=1)
def _client() -> OperatorClient:
    return OperatorClient(PluginConfig.from_env())


@lru_cache(maxsize=1)
def _emitter() -> LifecycleEmitter:
    return LifecycleEmitter(_client())


def _activate_refresher(refresher: PolicyAttestationRefresher) -> None:
    """Keep exactly one heartbeat alive for this loaded plugin module."""

    global _active_refresher
    with _active_refresher_lock:
        previous = _active_refresher
        if previous is refresher:
            return
        if previous is not None:
            previous.stop()
        _active_refresher = None
        refresher.start()
        _active_refresher = refresher


def _stop_active_refresher() -> None:
    global _active_refresher
    with _active_refresher_lock:
        refresher = _active_refresher
        _active_refresher = None
    if refresher is not None:
        refresher.stop()


atexit.register(_stop_active_refresher)


def _command(raw_args: str) -> str:
    """Implement direct-user Operator slash commands."""

    try:
        parts = shlex.split(raw_args or "")
    except ValueError as exc:
        return json.dumps({"success": False, "error": f"Invalid arguments: {exc}"})
    command = parts[0].lower() if parts else "status"
    tail = parts[1:]
    if command == "status":
        return tools.status(_client(), {})
    if command in {"next", "priorities"}:
        return tools.next_work(_client(), {"limit": 8})
    if command in {"questions", "input"}:
        return tools.open_questions(_client(), {"limit": 20})
    if command in {"reminders", "due"}:
        return tools.due_reminders(_client(), {"limit": 50})
    if command in {"diagnostics", "doctor"}:
        return tools.diagnostics(_diagnostics, {})
    if command in {"add", "task"} and tail:
        return tools.create_work(_client(), {"title": " ".join(tail), "kind": "task"})
    if command == "remind" and len(tail) >= 2:
        return tools.create_work(
            _client(),
            {
                "title": " ".join(tail[1:]),
                "kind": "reminder",
                "due_at": tail[0],
            },
        )
    if command == "answer" and len(tail) >= 2:
        return tools.answer_question(
            _client(),
            {"question_id": tail[0], "answer": " ".join(tail[1:])},
        )
    if command in {"scope", "authorization-scope"} and len(tail) == 1:
        return tools.authorization_scope(_client(), {"work_id": tail[0]})
    if command == "authorize" and len(tail) >= 4:
        try:
            version = int(tail[1])
            scope_revision = int(tail[2])
        except ValueError:
            version = 0
            scope_revision = 0
        return tools.authorize_work(
            _client(),
            {
                "work_id": tail[0],
                "expected_version": version,
                "expected_scope_revision": scope_revision,
                "expected_scope_digest": tail[3],
                "reason": " ".join(tail[4:]),
            },
        )
    if command == "done" and len(tail) == 2:
        try:
            version = int(tail[1])
        except ValueError:
            version = 0
        return tools.update_work(
            _client(),
            {
                "work_id": tail[0],
                "expected_version": version,
                "changes": {"status": "done"},
            },
        )
    if command == "snooze" and len(tail) == 3:
        try:
            version = int(tail[1])
        except ValueError:
            version = 0
        return tools.resolve_reminder(
            _client(),
            {
                "work_id": tail[0],
                "expected_version": version,
                "action": "snooze",
                "until": tail[2],
            },
        )
    return json.dumps(
        {
            "success": False,
            "error": (
                "Usage: /operator status|next|questions|reminders|diagnostics|"
                "add <title>|remind <ISO-time> <title>|answer <question-id> <answer>|"
                "scope <work-id>|"
                "authorize <work-id> <version> <scope-revision> <scope-digest> [reason]|"
                "done <work-id> <version>|"
                "snooze <work-id> <version> <ISO-time>"
            ),
        }
    )


_diagnostics: dict[str, Any] = {
    "schema_version": 1,
    "status": "plugin_not_registered",
}


def _policy_attestation(profile: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "plugin_version": PLUGIN_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_digest": POLICY_DIGEST,
        "guard_active": True,
        "policy_mode": POLICY_MODE,
        "attested_at": datetime.now(timezone.utc).isoformat(),
    }


def _policy_revocation(profile: str, reason: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "plugin_version": PLUGIN_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_digest": POLICY_DIGEST,
        "guard_active": False,
        "policy_mode": POLICY_MODE,
        "attested_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason[:512],
    }


def _revoke_policy_best_effort(client: OperatorClient, reason: str) -> None:
    """Publish negative state without making host startup depend on transport."""

    try:
        client.revoke_policy(_policy_revocation(client.config.profile, reason))
    except Exception as exc:
        logger.warning(
            "Could not publish Hermes operator policy revocation (%s): %s",
            reason,
            exc,
        )


def register(ctx: Any) -> None:
    """Register against current Hermes APIs and degrade on optional surfaces."""

    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        raise RuntimeError(
            "Hermes Operator requires the documented pre_tool_call policy hook"
        )
    client: OperatorClient | None = None
    try:
        client = _client()
    except ConfigurationError as exc:
        logger.warning(
            "Hermes operator bridge unavailable; pre_tool_call policy remains active: %s",
            exc,
        )
    guard = TaskScopedPolicyGuard(
        expected_profile=client.config.profile if client is not None else "",
        delegation_mode=detect_delegate_mode(),
    )
    # Registration is the first interaction with Hermes. Missing or invalid bridge
    # configuration therefore degrades to a policy-only guard that fails closed for
    # every task-scoped capability.
    register_hook("pre_tool_call", guard)
    global _diagnostics
    _diagnostics = diagnose_host(
        ctx,
        guard,
        credentials_scrubbed=bool(
            client is not None and client.config.credentials_scrubbed
        ),
        reviewed_host_override=bool(
            client is not None and client.config.reviewed_host_override
        ),
    )
    if client is None:
        return
    activation_blockers = bridge_activation_blockers(_diagnostics)
    if activation_blockers:
        reason = "host_incompatible:" + ",".join(activation_blockers)
        logger.error(
            "Hermes operator compatibility gate refused bridge activation; "
            "pre_tool_call policy remains active in policy-only mode: %s",
            ", ".join(activation_blockers),
        )
        _revoke_policy_best_effort(client, reason)
        return

    try:
        client.attest_policy(_policy_attestation(client.config.profile))
    except Exception as exc:
        logger.error(
            "Hermes operator policy attestation failed; bridge remains disabled: %s",
            exc,
        )
        _revoke_policy_best_effort(client, "policy_attestation_failed")
        return


    if client.config.emit_lifecycle:
        try:
            client.emit_lifecycle(
                "compatibility_observed",
                _diagnostics,
                identity_parts=(
                    client.config.profile,
                    PLUGIN_VERSION,
                    str(_diagnostics.get("diagnostic_digest", "")),
                ),
            )
        except Exception as exc:
            logger.warning("Could not record Hermes compatibility diagnostics: %s", exc)

    attestation_refresher = PolicyAttestationRefresher(
        client,
        lambda: _policy_attestation(client.config.profile),
        client.config.attestation_refresh_seconds,
    )
    try:
        _activate_refresher(attestation_refresher)
    except Exception as exc:
        logger.error(
            "Hermes policy heartbeat failed to start; bridge remains disabled: %s",
            exc,
        )
        _revoke_policy_best_effort(client, "policy_heartbeat_unavailable")
        return

    guard.activate_bridge(client.execution_contract, client.claim_delegation)

    ctx.register_tool(
        name="operator_status",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_STATUS,
        handler=lambda args, **kwargs: tools.status(client, args, **kwargs),
        description="Check the internal operator control plane.",
    )
    ctx.register_tool(
        name="operator_next_work",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_NEXT_WORK,
        handler=lambda args, **kwargs: tools.next_work(client, args, **kwargs),
        description="Read next-best work suggestions from the operator.",
    )
    ctx.register_tool(
        name="operator_open_questions",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_OPEN_QUESTIONS,
        handler=lambda args, **kwargs: tools.open_questions(client, args, **kwargs),
        description="Read unresolved operator questions.",
    )
    ctx.register_tool(
        name="operator_due_reminders",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_DUE_REMINDERS,
        handler=lambda args, **kwargs: tools.due_reminders(client, args, **kwargs),
        description="Read reminders that are due for native Hermes delivery.",
    )
    ctx.register_tool(
        name="operator_resolve_reminder",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_RESOLVE_REMINDER,
        handler=lambda args, **kwargs: tools.resolve_reminder(client, args, **kwargs),
        description=(
            "Snooze, acknowledge, or complete one reminder without moving its due-time anchor."
        ),
    )
    ctx.register_tool(
        name="operator_claim_attention",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_CLAIM_ATTENTION,
        handler=lambda args, **kwargs: tools.claim_attention(client, args, **kwargs),
        description="Claim due reminders and questions for one private Cron delivery.",
    )
    ctx.register_tool(
        name="operator_create_work",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_CREATE_WORK,
        handler=lambda args, **kwargs: tools.create_work(client, args, **kwargs),
        description="Record reversible work in Operator triage.",
    )
    ctx.register_tool(
        name="operator_answer_question",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_ANSWER_QUESTION,
        handler=lambda args, **kwargs: tools.answer_question(client, args, **kwargs),
        description="Answer one exact Operator question with native human approval.",
    )
    ctx.register_tool(
        name="operator_authorization_scope",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_AUTHORIZATION_SCOPE,
        handler=lambda args, **kwargs: tools.authorization_scope(
            client, args, **kwargs
        ),
        description=(
            "Read the current dependency-fenced scope before requesting authorization."
        ),
    )
    ctx.register_tool(
        name="operator_authorize_work",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_AUTHORIZE_WORK,
        handler=lambda args, **kwargs: tools.authorize_work(client, args, **kwargs),
        description="Authorize one exact work item with native human approval.",
    )
    ctx.register_tool(
        name="operator_update_work",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_UPDATE_WORK,
        handler=lambda args, **kwargs: tools.update_work(client, args, **kwargs),
        description="Update canonical work without granting execution authority.",
    )
    ctx.register_tool(
        name="operator_ingest_inbound",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_INGEST_INBOUND,
        handler=lambda args, **kwargs: tools.ingest_inbound(client, args, **kwargs),
        description="Record provider items read by Hermes native skills.",
    )
    ctx.register_tool(
        name="operator_diagnostics",
        toolset="hermes_operator",
        schema=schemas.OPERATOR_DIAGNOSTICS,
        handler=lambda args, **kwargs: tools.diagnostics(_diagnostics, args, **kwargs),
        description="Read local Hermes compatibility diagnostics.",
    )

    hooks = build_hooks(client, _emitter(), attestation_refresher)
    _register_optional_hooks(ctx, hooks)

    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        try:
            register_command(
                "operator",
                _command,
                description="Review and manage Operator work, questions, and reminders",
            )
        except Exception as exc:
            logger.warning("Could not register /operator command: %s", exc)

    register_skill = getattr(ctx, "register_skill", None)
    skill_path = Path(__file__).parent / "skills" / "operator-workflow" / "SKILL.md"
    if callable(register_skill) and skill_path.exists():
        try:
            register_skill("operator-workflow", skill_path)
        except Exception as exc:
            logger.warning("Could not register operator workflow skill: %s", exc)


def _register_optional_hooks(ctx: Any, hooks: dict[str, Any]) -> None:
    """Register hooks individually so a version mismatch disables only one feature."""

    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        logger.warning("Hermes version does not expose plugin lifecycle hooks")
        return
    for name, callback in hooks.items():
        try:
            register_hook(name, callback)
        except Exception as exc:
            logger.warning("Hermes rejected optional hook %s: %s", name, exc)
