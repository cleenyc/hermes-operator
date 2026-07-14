"""Hermes host diagnostics without patching or reordering the host.

The plugin deliberately observes the installed harness instead of patching it or
reordering other plugins. Unknown or changed managed-execution semantics are reported
as ``unknown`` and keep the bridge in policy-only mode. The plugin separately scrubs
its own bridge credentials from ``os.environ`` before managed tools can run. Optional
capabilities such as delegation can still degrade independently.
"""

from __future__ import annotations

import hashlib
from importlib import import_module, metadata
import inspect
import json
import os
import re
from typing import Any, Mapping


SUPPORTED_HERMES_VERSION = "0.18.2"
SUPPORTED_HERMES_TAG = "v2026.7.7.2"
SUPPORTED_HERMES_COMMIT = "9de9c25f620ff7f1ce0fd5457d596052d5159596"


def diagnose_host(
    ctx: Any,
    guard: Any,
    *,
    credentials_scrubbed: bool = False,
    reviewed_host_override: bool = False,
) -> dict[str, Any]:
    """Return a bounded, JSON-compatible compatibility observation."""

    hermes_version = _distribution_version()
    active_profile = _active_profile(ctx)
    configured_profile = str(getattr(guard, "expected_profile", "") or "")
    delegate_mode = detect_delegate_mode()
    hook_semantics = _pre_tool_semantics()
    worker_identity_semantics = _managed_worker_identity_semantics()
    worker_secret_semantics = _managed_subprocess_secret_semantics(
        credentials_scrubbed=credentials_scrubbed
    )
    hook_position, hook_count = _guard_position(ctx, guard)
    artifact_transport = _completion_transport_semantics()

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
    if artifact_transport in {
        "post_hook_prose_path_promotion",
        "prehook_structured_and_post_hook_prose_path_promotion",
        "prehook_structured_artifact_fields",
    }:
        warnings.append(
            "Hermes promotes completion prose paths after the pre-tool hook; Operator-managed completions reject artifact fields and promotable local paths"
        )
    elif artifact_transport not in {"none"}:
        warnings.append(
            "Hermes completion artifact transport could not be positively matched to the Operator guard"
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
    if worker_secret_semantics not in {"protected", "plugin_environment_scrubbed"}:
        warnings.append(
            "Hermes managed subprocess filtering does not positively protect Operator bridge credentials"
        )

    report: dict[str, Any] = {
        "schema_version": 4,
        "hermes_version": hermes_version,
        "supported_hermes_version": SUPPORTED_HERMES_VERSION,
        "supported_hermes_tag": SUPPORTED_HERMES_TAG,
        "supported_hermes_commit": SUPPORTED_HERMES_COMMIT,
        "supported_hermes_version_match": (
            None if hermes_version == "unknown" else hermes_version == SUPPORTED_HERMES_VERSION
        ),
        "reviewed_host_override": bool(reviewed_host_override),
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
        "kanban_completion_artifacts": (
            False if artifact_transport == "none" else (
                True
                if artifact_transport
                in {
                    "post_hook_prose_path_promotion",
                    "prehook_structured_and_post_hook_prose_path_promotion",
                    "prehook_structured_artifact_fields",
                }
                else None
            )
        ),
        "completion_artifact_transport_semantics": artifact_transport,
        "operator_artifact_policy": "reject_artifact_fields_and_promotable_local_paths",
        "pre_tool_directive_semantics": hook_semantics,
        "managed_worker_identity_semantics": worker_identity_semantics,
        "managed_subprocess_secret_semantics": worker_secret_semantics,
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

    Managed autonomy requires the pinned Hermes release (or an explicit
    deployment-owned reviewed-host override) plus positive evidence for the profile,
    hook directive resolution, hook position, dispatcher-controlled worker identity,
    child-process credential filtering, and completion artifact transport. An
    override does not bypass any semantic blocker.
    """

    blockers: list[str] = []
    version_match = report.get("supported_hermes_version_match")
    reviewed_override = report.get("reviewed_host_override") is True
    if not reviewed_override:
        if version_match is False:
            blockers.append("hermes_version_unsupported")
        elif version_match is not True:
            blockers.append("hermes_version_unverified")

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

    worker_secrets = report.get("managed_subprocess_secret_semantics")
    if worker_secrets == "unknown" or not isinstance(worker_secrets, str):
        blockers.append("managed_subprocess_secret_filter_unverified")
    elif worker_secrets not in {"protected", "plugin_environment_scrubbed"}:
        blockers.append("managed_subprocess_secret_filter_unsupported")

    artifact_transport = report.get("completion_artifact_transport_semantics")
    if artifact_transport == "unknown" or not isinstance(artifact_transport, str):
        blockers.append("completion_artifact_transport_unverified")
    elif artifact_transport not in {
        "none",
        "post_hook_prose_path_promotion",
        "prehook_structured_and_post_hook_prose_path_promotion",
        "prehook_structured_artifact_fields",
    }:
        blockers.append("completion_artifact_transport_unsupported")
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
    """Verify that the dispatcher owns both task identity and workspace root.

    The quiet CLI creates a turn UUID independently, so the hook ``task_id`` cannot
    identify the Kanban card. The stable identity is safe only when the native
    dispatcher places ``task.id`` in ``HERMES_KANBAN_TASK``, places its selected
    workspace in ``HERMES_KANBAN_WORKSPACE``, and launches the quiet worker inside
    that workspace with the same environment. Source inspection is deliberately
    semantic and version-independent; an exact package version alone is not
    sufficient evidence.
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
    has_workspace_identity = bool(
        "HERMES_KANBAN_WORKSPACE" in source
        and re.search(
            r"(?:env|environment)\s*\[\s*['\"]HERMES_KANBAN_WORKSPACE['\"]\s*\]"
            r"\s*=\s*(?:str\s*\(\s*)?workspace\b",
            source,
        )
    )
    passes_environment = bool(re.search(r"\benv\s*=\s*env\b", source))
    uses_workspace_cwd = bool(
        re.search(r"\bcwd\s*=\s*(?:str\s*\(\s*)?workspace\b", source)
    )
    if (
        has_environment_identity
        and has_workspace_identity
        and has_quiet_worker
        and passes_environment
        and uses_workspace_cwd
    ):
        return "dispatcher_environment"
    return "unsupported"


def _managed_subprocess_secret_semantics(
    *, credentials_scrubbed: bool = False
) -> str:
    """Classify whether managed project code can inherit bridge credentials.

    The bridge token and the proof secret together form the plugin's authority
    boundary.  We therefore require positive, semantic evidence that Hermes' local
    environment builder strips both names and does not allow either through its
    explicit passthrough mechanism.  Reading source and immutable configuration is
    intentionally side-effect free; an unknown implementation fails activation.
    """

    protected_names = {
        "HERMES_OPERATOR_BRIDGE_TOKEN",
        "HERMES_OPERATOR_BRIDGE_PROOF_SECRET",
    }
    if credentials_scrubbed:
        if protected_names.isdisjoint(os.environ):
            return "plugin_environment_scrubbed"
        return "exposed"
    try:
        local = import_module("tools.environments.local")
    except Exception:
        return "unknown"

    blocklists: list[set[str]] = []
    for name in ("_HERMES_PROVIDER_ENV_BLOCKLIST", "_ALWAYS_STRIP_KEYS"):
        value = getattr(local, name, None)
        if isinstance(value, (set, frozenset, list, tuple)) and all(
            isinstance(item, str) for item in value
        ):
            blocklists.append(set(value))
    # Some Hermes releases keep the universal strip list next to passthrough
    # handling rather than the local environment implementation.
    try:
        passthrough = import_module("tools.env_passthrough")
    except Exception:
        return "unknown"
    value = getattr(passthrough, "_ALWAYS_STRIP_KEYS", None)
    if isinstance(value, (set, frozenset, list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        blocklists.append(set(value))
    is_passthrough = getattr(passthrough, "is_env_passthrough", None)
    if not callable(is_passthrough) or not blocklists:
        return "unknown"

    try:
        passthrough_result = {
            name: bool(is_passthrough(name)) for name in protected_names
        }
    except Exception:
        return "unknown"

    combined = set().union(*blocklists)
    callbacks = [
        getattr(local, "_make_run_env", None),
        getattr(local, "_sanitize_subprocess_env", None),
    ]
    sources: list[str] = []
    for callback in callbacks:
        if not callable(callback):
            continue
        try:
            sources.append(inspect.getsource(callback))
        except Exception:
            continue
    if not sources:
        return "unknown"
    source = "\n".join(sources)
    uses_known_filter = any(
        marker in source
        for marker in (
            "_HERMES_PROVIDER_ENV_BLOCKLIST",
            "_ALWAYS_STRIP_KEYS",
            "is_env_passthrough",
        )
    )
    if not uses_known_filter:
        return "unknown"
    if protected_names.issubset(combined) and not any(passthrough_result.values()):
        return "protected"
    return "exposed"


def _completion_transport_semantics() -> str:
    """Classify native completion-to-Gateway artifact promotion.

    A recognized prose-path promotion is compatible because Hermes resolves the
    model-facing pre-tool directive before ``_handle_complete`` runs, and the guard
    rejects promotable paths in the completion summary. Structured artifact fields
    or any other transport are unsupported rather than silently assumed safe.
    """

    try:
        tools_module = import_module("tools.kanban_tools")
        handler = getattr(tools_module, "_handle_complete")
        handler_source = inspect.getsource(handler)
        schema = getattr(tools_module, "KANBAN_COMPLETE_SCHEMA", None)
    except Exception:
        return "unknown"
    try:
        database_module = import_module("hermes_cli.kanban_db")
        complete = getattr(database_module, "complete_task")
        complete_source = inspect.getsource(complete)
    except Exception:
        return "unknown"

    schema_properties: Mapping[str, Any] = {}
    if isinstance(schema, Mapping):
        parameters = schema.get("parameters", schema)
        if isinstance(parameters, Mapping):
            properties = parameters.get("properties", {})
            if isinstance(properties, Mapping):
                schema_properties = properties
    structured_transport = "artifacts" in schema_properties or bool(
        re.search(r"\b(?:args|arguments|payload)\s*\.get\(\s*['\"]artifacts['\"]", handler_source)
    )
    database_promotes_prose = bool(
        "_merge_completion_prose_artifacts" in complete_source
        or (
            re.search(r"\bartifacts\b", complete_source)
            and re.search(r"\bsummary\b", complete_source)
            and re.search(r"\bmetadata\b", complete_source)
        )
    )
    handler_passes_summary = bool(
        re.search(r"\bcomplete_task\s*\(", handler_source)
        and re.search(r"\bsummary\b", handler_source)
    )
    if database_promotes_prose:
        if not handler_passes_summary:
            return "unknown"
        try:
            gateway_module = import_module("gateway.kanban_watchers")
            gateway_callback = getattr(
                getattr(gateway_module, "GatewayKanbanWatchersMixin"),
                "_deliver_kanban_artifacts",
            )
            gateway_source = inspect.getsource(gateway_callback)
        except Exception:
            return "unknown"
        if not (
            "extract_local_files" in gateway_source
            or re.search(r"\bartifacts\b", gateway_source)
        ):
            return "unknown"
        return (
            "prehook_structured_and_post_hook_prose_path_promotion"
            if structured_transport
            else "post_hook_prose_path_promotion"
        )

    if structured_transport:
        return "prehook_structured_artifact_fields"

    if re.search(r"\bartifacts\b", handler_source + "\n" + complete_source):
        return "unknown"
    return "none"


def _kanban_completion_artifacts() -> bool | None:
    """Backward-compatible projection retained for diagnostics consumers."""

    semantics = _completion_transport_semantics()
    if semantics == "none":
        return False
    if semantics in {
        "post_hook_prose_path_promotion",
        "prehook_structured_and_post_hook_prose_path_promotion",
        "prehook_structured_artifact_fields",
    }:
        return True
    return None


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
