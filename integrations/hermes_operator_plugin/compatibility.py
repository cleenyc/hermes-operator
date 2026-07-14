"""Hermes host diagnostics with no host mutation.

The plugin deliberately observes the installed harness instead of patching it or
reordering other plugins. Unknown or changed managed-execution semantics are reported
as ``unknown`` and keep the bridge in policy-only mode. Optional capabilities such as
delegation can still degrade independently.
"""

from __future__ import annotations

import hashlib
from importlib import import_module, metadata
import inspect
import json
import re
from typing import Any, Mapping


SUPPORTED_HERMES_VERSION = "0.18.2"
SUPPORTED_HERMES_TAG = "v2026.7.7.2"
SUPPORTED_HERMES_COMMIT = "9de9c25f620ff7f1ce0fd5457d596052d5159596"


def diagnose_host(ctx: Any, guard: Any) -> dict[str, Any]:
    """Return a bounded, JSON-compatible compatibility observation."""

    hermes_version = _distribution_version()
    active_profile = _active_profile(ctx)
    configured_profile = str(getattr(guard, "expected_profile", "") or "")
    delegate_mode = detect_delegate_mode()
    hook_semantics = _pre_tool_semantics()
    worker_identity_semantics = _managed_worker_identity_semantics()
    hook_position, hook_count = _guard_position(ctx, guard)
    artifacts = _kanban_completion_artifacts()

    warnings: list[str] = []
    if hermes_version not in {"unknown", SUPPORTED_HERMES_VERSION}:
        warnings.append(
            "installed Hermes version differs from the pinned integration-test target"
        )
    if configured_profile and active_profile not in {"", "unknown", configured_profile}:
        warnings.append("active Hermes profile differs from the configured Operator profile")
    if delegate_mode != "foreground":
        warnings.append(
            "top-level delegate_task is not proven foreground and is blocked for Operator-managed cards"
        )
    if artifacts is not False:
        warnings.append(
            "Hermes completion may transport artifacts; Operator-managed completions reject artifact fields and promotable local paths"
        )
    if hook_semantics == "first_valid" and hook_position != 1:
        warnings.append(
            "the Operator guard is not proven first; Hermes uses the first valid pre-tool directive"
        )
    elif hook_semantics == "unknown":
        warnings.append("pre_tool_call directive resolution semantics could not be identified")
    if worker_identity_semantics != "dispatcher_environment":
        warnings.append(
            "dispatcher ownership of HERMES_KANBAN_TASK could not be positively verified"
        )

    report: dict[str, Any] = {
        "schema_version": 3,
        "hermes_version": hermes_version,
        "supported_hermes_version": SUPPORTED_HERMES_VERSION,
        "supported_hermes_tag": SUPPORTED_HERMES_TAG,
        "supported_hermes_commit": SUPPORTED_HERMES_COMMIT,
        "supported_hermes_version_match": (
            None if hermes_version == "unknown" else hermes_version == SUPPORTED_HERMES_VERSION
        ),
        "active_profile": active_profile,
        "configured_profile": configured_profile,
        "configured_profile_match": (
            None
            if not configured_profile or active_profile in {"", "unknown"}
            else active_profile == configured_profile
        ),
        "delegate_mode": delegate_mode,
        "delegate_policy": (
            "foreground_contract_required"
            if delegate_mode == "foreground"
            else "blocked_non_durable"
        ),
        "kanban_completion_artifacts": artifacts,
        "operator_artifact_policy": "reject_artifact_fields_and_promotable_local_paths",
        "pre_tool_directive_semantics": hook_semantics,
        "managed_worker_identity_semantics": worker_identity_semantics,
        "guard_hook_position": hook_position,
        "guard_hook_count": hook_count,
        "warnings": warnings[:12],
    }
    canonical = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    report["diagnostic_digest"] = hashlib.sha256(canonical).hexdigest()
    return report


def bridge_activation_blockers(report: Mapping[str, Any]) -> tuple[str, ...]:
    """Return unverified or incompatible host semantics that make activation unsafe.

    Managed autonomy requires positive evidence for the profile, hook directive
    resolution, hook position, and dispatcher-controlled worker identity. The
    installed version remains diagnostic rather than an exact-version lock: a newer
    or vendor-patched Hermes build may activate when it exposes the same semantics.
    """

    blockers: list[str] = []
    profile_match = report.get("configured_profile_match")
    if profile_match is False:
        blockers.append("active_profile_mismatch")
    elif profile_match is not True:
        blockers.append("active_profile_unverified")

    hook_semantics = report.get("pre_tool_directive_semantics")
    if hook_semantics == "first_valid":
        if report.get("guard_hook_position") != 1:
            blockers.append("operator_guard_not_first")
    elif hook_semantics == "unknown" or not isinstance(hook_semantics, str):
        blockers.append("pre_tool_directive_semantics_unverified")
    else:
        blockers.append("pre_tool_directive_semantics_unsupported")

    worker_identity = report.get("managed_worker_identity_semantics")
    if worker_identity == "unknown" or not isinstance(worker_identity, str):
        blockers.append("managed_worker_identity_unverified")
    elif worker_identity != "dispatcher_environment":
        blockers.append("managed_worker_identity_unsupported")
    return tuple(blockers)


def _distribution_version() -> str:
    for name in ("hermes-agent", "hermes_agent", "hermes-cli"):
        try:
            value = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        except Exception:
            return "unknown"
        return str(value)[:128] or "unknown"
    return "unknown"


def _active_profile(ctx: Any) -> str:
    try:
        value = getattr(ctx, "profile_name", "unknown")
    except Exception:
        return "unknown"
    return str(value or "unknown")[:128]


def detect_delegate_mode() -> str:
    """Classify the model-facing top-level delegate path, if installed."""

    try:
        module = import_module("tools.delegate_tool")
    except Exception:
        return "unknown"

    chooser = getattr(module, "_model_background_value", None)
    if callable(chooser):
        try:
            parent = type("_OperatorDiagnosticParent", (), {"_delegate_depth": 0})()
            return "background" if bool(chooser({}, parent)) else "foreground"
        except Exception:
            pass

    schema = getattr(module, "DELEGATE_TASK_SCHEMA", None)
    if isinstance(schema, Mapping):
        try:
            background = schema["parameters"]["properties"]["background"]
            description = str(background.get("description", "")).lower()
        except (KeyError, TypeError, AttributeError):
            description = ""
        if "ignored" in description and "background" in description:
            return "background"
    return "unknown"


def _pre_tool_semantics() -> str:
    try:
        module = import_module("hermes_cli.plugins")
        callback = getattr(module, "_get_pre_tool_call_directive_details")
        source = inspect.getsource(callback)
    except Exception:
        return "unknown"
    if "for result in hook_results" in source and "return _PreToolCallDirective" in source:
        return "first_valid"
    return "unknown"


def _managed_worker_identity_semantics() -> str:
    """Verify that the native dispatcher owns the managed-card environment marker.

    The quiet CLI creates a turn UUID independently, so the hook ``task_id`` cannot
    identify the Kanban card. The stable identity is safe only when the native
    dispatcher places ``task.id`` in ``HERMES_KANBAN_TASK`` and passes that environment
    to the quiet worker subprocess. Source inspection is deliberately semantic and
    version-independent; an exact package version alone is not sufficient evidence.
    """

    try:
        module = import_module("hermes_cli.kanban_db")
        callback = getattr(module, "_default_spawn")
        source = inspect.getsource(callback)
    except Exception:
        return "unknown"
    has_environment_identity = bool(
        re.search(
            r"(?:env|environment)\s*\[\s*['\"]HERMES_KANBAN_TASK['\"]\s*\]"
            r"\s*=\s*task\.id\b",
            source,
        )
    )
    has_quiet_worker = bool(
        re.search(r"['\"]chat['\"]", source)
        and re.search(r"['\"]-q['\"]", source)
    )
    passes_environment = bool(re.search(r"\benv\s*=\s*env\b", source))
    if has_environment_identity and has_quiet_worker and passes_environment:
        return "dispatcher_environment"
    return "unsupported"


def _kanban_completion_artifacts() -> bool | None:
    observations: list[bool] = []
    try:
        module = import_module("tools.kanban_tools")
        callback = getattr(module, "_handle_complete")
        source = inspect.getsource(callback)
    except Exception:
        pass
    else:
        observations.append("artifacts" in source and "metadata" in source)
    try:
        module = import_module("hermes_cli.kanban_db")
        callback = getattr(module, "complete_task")
        source = inspect.getsource(callback)
    except Exception:
        pass
    else:
        observations.append(
            "_merge_completion_prose_artifacts" in source
            or ("artifacts" in source and "metadata" in source)
        )
    return any(observations) if observations else None


def _guard_position(ctx: Any, guard: Any) -> tuple[int | None, int | None]:
    """Observe current callback order through Hermes' context when available."""

    try:
        manager = getattr(ctx, "_manager")
        hooks = getattr(manager, "_hooks")
        callbacks = list(hooks.get("pre_tool_call", []))
    except Exception:
        return None, None
    try:
        position = callbacks.index(guard) + 1
    except ValueError:
        position = None
    return position, len(callbacks)


__all__ = [
    "SUPPORTED_HERMES_COMMIT",
    "SUPPORTED_HERMES_TAG",
    "SUPPORTED_HERMES_VERSION",
    "bridge_activation_blockers",
    "detect_delegate_mode",
    "diagnose_host",
]
