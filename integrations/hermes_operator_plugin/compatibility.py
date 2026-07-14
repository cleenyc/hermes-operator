"""Best-effort Hermes host diagnostics with no host mutation.

The plugin deliberately observes the installed harness instead of patching it or
reordering other plugins.  Unknown or changed internals are reported as ``unknown``;
policy code treats unknown delegation semantics as non-durable and fails closed.
"""

from __future__ import annotations

import hashlib
from importlib import import_module, metadata
import inspect
import json
from typing import Any, Mapping


def diagnose_host(ctx: Any, guard: Any) -> dict[str, Any]:
    """Return a bounded, JSON-compatible compatibility observation."""

    hermes_version = _distribution_version()
    active_profile = _active_profile(ctx)
    configured_profile = str(getattr(guard, "expected_profile", "") or "")
    delegate_mode = detect_delegate_mode()
    hook_semantics = _pre_tool_semantics()
    hook_position, hook_count = _guard_position(ctx, guard)
    artifacts = _kanban_completion_artifacts()

    warnings: list[str] = []
    if configured_profile and active_profile not in {"", "unknown", configured_profile}:
        warnings.append("active Hermes profile differs from the configured Operator profile")
    if delegate_mode != "foreground":
        warnings.append(
            "top-level delegate_task is not proven foreground and is blocked for Operator-managed cards"
        )
    if artifacts is not False:
        warnings.append(
            "Hermes completion may transport artifacts; Operator-managed completions reject artifact fields"
        )
    if hook_semantics == "first_valid" and hook_position not in {None, 1}:
        warnings.append(
            "another pre_tool_call hook precedes the Operator guard; Hermes uses the first valid directive"
        )
    elif hook_semantics == "unknown":
        warnings.append("pre_tool_call directive resolution semantics could not be identified")

    report: dict[str, Any] = {
        "schema_version": 1,
        "hermes_version": hermes_version,
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
        "operator_artifact_policy": "reject_artifact_fields",
        "pre_tool_directive_semantics": hook_semantics,
        "guard_hook_position": hook_position,
        "guard_hook_count": hook_count,
        "warnings": warnings[:12],
    }
    canonical = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    report["diagnostic_digest"] = hashlib.sha256(canonical).hexdigest()
    return report


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


def _kanban_completion_artifacts() -> bool | None:
    try:
        module = import_module("tools.kanban_tools")
        callback = getattr(module, "_handle_complete")
        source = inspect.getsource(callback)
    except Exception:
        return None
    return "artifacts" in source and "metadata" in source


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


__all__ = ["detect_delegate_mode", "diagnose_host"]
