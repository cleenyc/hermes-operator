"""Task-scoped guard for Operator-managed Hermes work.

Managed Kanban cards receive a strict execution contract and cannot perform external
communication or publication. Interactive and Cron sessions remain native Hermes
surfaces: recognizable external writes use Hermes' own approval directive. The
optional exact-action broker is a separate deployment path, not a prerequisite for
normal Hermes operation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    blocked: bool
    category: str = ""
    detail: str = ""


ALLOW = PolicyDecision(False)

AuthorizationLookup = Callable[[str], Mapping[str, Any]]
DelegationClaim = Callable[[str, int], Mapping[str, Any]]


class TaskScopedPolicyGuard:
    """Bind the fail-closed policy to one authenticated control-plane client."""

    def __init__(
        self,
        authorization_lookup: AuthorizationLookup | None = None,
        delegation_claim: DelegationClaim | None = None,
        *,
        expected_profile: str = "",
        delegation_mode: str = "unknown",
    ) -> None:
        self._authorization_lookup = authorization_lookup
        self._delegation_claim = delegation_claim
        self._expected_profile = expected_profile.strip()
        self._delegation_mode = (
            delegation_mode if delegation_mode in {"foreground", "background"} else "unknown"
        )

    @property
    def expected_profile(self) -> str:
        """Expose non-secret configured identity to compatibility diagnostics."""

        return self._expected_profile

    def activate_bridge(
        self,
        authorization_lookup: AuthorizationLookup,
        delegation_claim: DelegationClaim,
    ) -> None:
        """Enable contract-backed worker actions after host attestation gates pass.

        The guard is registered before Hermes compatibility can be observed.  Keeping
        these callbacks unset until profile and hook-order checks pass makes
        "policy-only mode" real: an incompatible host cannot use the bridge merely
        because its model-facing Operator tools were withheld.
        """

        if not callable(authorization_lookup) or not callable(delegation_claim):
            raise TypeError("bridge callbacks must be callable")
        self._authorization_lookup = authorization_lookup
        self._delegation_claim = delegation_claim

    def deactivate_bridge(self) -> None:
        """Return the already-registered guard to fail-closed policy-only mode."""

        self._authorization_lookup = None
        self._delegation_claim = None

    def __call__(
        self,
        tool_name: str = "",
        args: dict | None = None,
        task_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str] | None:
        name = _normalize_name(tool_name)
        current_task_id, identity_error = _current_task_identity(task_id)
        if identity_error:
            return _blocked_response(PolicyDecision(True, "identity", identity_error))
        if name in _HUMAN_CONFIRMATION_TOOLS:
            if current_task_id:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "an autonomous worker cannot request user authority through a conversational control tool",
                    )
                )
            return _human_confirmation(name, args)
        if name in {"operator_create_work", "operator_ingest_inbound"} and current_task_id:
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "provider intake and conversational work capture are available only outside Operator-managed worker cards",
                )
            )
        if name == "operator_update_work":
            if current_task_id:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "an autonomous worker cannot mutate canonical work through the conversational update tool",
                    )
                )
            return _work_update_confirmation(args)
        if not current_task_id:
            return _native_session_policy(name, args)
        if name == "delegate_task":
            return self._guard_delegation(args, task_id)
        return guard_external_side_effects(
            tool_name,
            args,
            task_id=task_id,
            authorization_lookup=self._authorization_lookup,
            expected_profile=self._expected_profile,
            **kwargs,
        )

    def _guard_delegation(
        self,
        args: dict | None,
        task_id: str,
    ) -> dict[str, str] | None:
        if self._delegation_mode != "foreground":
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "this Hermes host does not provide proven foreground delegation; "
                    "use Operator-managed parallel work cards instead",
                )
            )
        if args is not None and not isinstance(args, Mapping):
            return _blocked_response(
                PolicyDecision(
                    True,
                    "generic_mutation",
                    "malformed tool arguments cannot be evaluated safely",
                )
            )
        try:
            current_task_id, identity_error = _current_task_identity(task_id)
            if identity_error:
                return _blocked_response(
                    PolicyDecision(True, "identity", identity_error)
                )
            contract = _lookup_execution_contract(
                self._authorization_lookup,
                current_task_id,
                self._expected_profile,
                "delegate_task",
            )
            decision = evaluate_tool_call(
                "delegate_task",
                args or {},
                current_task_id=current_task_id,
                execution_contract=contract,
            )
            if decision.blocked:
                return _blocked_response(decision)
            if contract is None:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "parallel delegation requires a live task-scoped execution contract",
                    )
                )
            requested_children = _delegated_child_count(args or {})
            if self._delegation_claim is None:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "durable delegation claim service is unavailable",
                    )
                )
            claim = self._delegation_claim(
                current_task_id,
                requested_children,
            )
            expected_claim_keys = {
                "claimed",
                "contract_digest",
                "reason",
                "requested_children",
                "run_id",
                "task_id",
            }
            if (
                not isinstance(claim, Mapping)
                or set(claim) != expected_claim_keys
                or claim.get("claimed") is not True
                or claim.get("task_id") != current_task_id
                or claim.get("run_id") != contract.get("run_id")
                or claim.get("contract_digest") != contract.get("contract_digest")
                or claim.get("requested_children") != requested_children
                or claim.get("reason") != "claimed"
            ):
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "this canonical run cannot claim another delegation batch",
                    )
                )
            return None
        except Exception:
            return _blocked_response(
                PolicyDecision(
                    True,
                    "generic_mutation",
                    "policy evaluation failed closed",
                )
            )

POLICY_VERSION = "7.0.0"
POLICY_MODE = "default_deny"

_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CONTRACT_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_KNOWN_INTERNAL_CAPABILITIES = {
    "delegate_task",
    "local_build",
    "local_read",
    "local_test",
    "local_write",
}


def _policy_source_digest() -> str:
    """Bind an attestation to the exact policy source loaded by this process."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


POLICY_DIGEST = _policy_source_digest()

_READ_ACTIONS = {
    "check",
    "download",
    "fetch",
    "find",
    "get",
    "inspect",
    "list",
    "lookup",
    "query",
    "read",
    "search",
    "show",
    "status",
    "view",
}

_EXPLICITLY_ALLOWED_TOOLS = {
    "clarify",
    "operator_next_work",
    "operator_open_questions",
    "operator_authorization_scope",
    "operator_create_work",
    "operator_ingest_inbound",
    "operator_update_work",
    "operator_status",
    "operator_diagnostics",
    "operator_due_reminders",
    "project_list",
    "read_file",
    "read_terminal",
    "search_files",
    "session_search",
    "skill_view",
    "skills_list",
    "todo",
    "video_analyze",
    "vision_analyze",
    "web_extract",
    "web_search",
    "x_search",
}

_HUMAN_CONFIRMATION_TOOLS = {
    "operator_answer_question": "question_id",
    "operator_authorize_work": "work_id",
    "operator_resolve_reminder": "work_id",
}

_READ_ONLY_KANBAN_TOOLS = {
    "kanban_list",
    "kanban_show",
}

_CURRENT_TASK_KANBAN_TOOLS = {
    "kanban_block",
    "kanban_comment",
    "kanban_complete",
    "kanban_heartbeat",
}

_TASK_SCOPED_CAPABILITY_TOOLS = {
    "patch": "local_write",
    "write_file": "local_write",
}

_TASK_SCOPED_READ_TOOLS = {
    "read_file": "local_read",
    "read_terminal": "local_read",
    "search_files": "local_read",
}

_MANAGED_UNSCOPED_OBSERVATION_TOOLS = {
    "read_terminal",
    "session_search",
    "video_analyze",
    "vision_analyze",
}

_WORKSPACE_ENV = "HERMES_KANBAN_WORKSPACE"
_PATCH_FILE_LINE = re.compile(
    r"^\*\*\*\s+(?:Add|Delete|Update)\s+File:\s*(?P<path>.+?)\s*$"
)
_PATCH_MOVE_LINE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(?P<path>.+?)\s*$")

_COMMUNICATION_OBJECTS = {
    "chat",
    "comment",
    "dm",
    "email",
    "mail",
    "message",
    "notification",
    "reply",
    "sms",
    "sticker",
}
_COMMUNICATION_VERBS = {
    "add",
    "create",
    "deliver",
    "forward",
    "post",
    "reply",
    "send",
    "write",
}
_CALENDAR_OBJECTS = {"calendar", "event", "meeting", "schedule"}
_CALENDAR_VERBS = {
    "accept",
    "add",
    "book",
    "cancel",
    "create",
    "decline",
    "delete",
    "invite",
    "join",
    "move",
    "remove",
    "reschedule",
    "respond",
    "rsvp",
    "schedule",
    "update",
}
_FINANCIAL_OBJECTS = {
    "billing",
    "charge",
    "checkout",
    "deposit",
    "financial",
    "invoice",
    "order",
    "payment",
    "purchase",
    "refund",
    "trade",
    "transaction",
    "transfer",
    "withdrawal",
}
_FINANCIAL_VERBS = {
    "buy",
    "cancel",
    "charge",
    "create",
    "execute",
    "pay",
    "place",
    "purchase",
    "refund",
    "sell",
    "submit",
    "trade",
    "transfer",
    "update",
    "withdraw",
}
_DESTRUCTIVE = {
    "delete",
    "destroy",
    "drop",
    "erase",
    "kill",
    "purge",
    "remove",
    "shred",
    "terminate",
    "truncate",
    "wipe",
}
_PERMISSION_OBJECTS = {
    "access",
    "acl",
    "admin",
    "ban",
    "iam",
    "member",
    "membership",
    "permission",
    "policy",
    "role",
    "security",
    "timeout",
}
_PERMISSION_VERBS = {
    "add",
    "assign",
    "ban",
    "change",
    "create",
    "delete",
    "edit",
    "grant",
    "invite",
    "kick",
    "remove",
    "revoke",
    "set",
    "timeout",
    "update",
}
_REPOSITORY_OBJECTS = {
    "bitbucket",
    "branch",
    "code",
    "commit",
    "git",
    "github",
    "gitlab",
    "pr",
    "pull",
    "repository",
}
_REPOSITORY_VERBS = {
    "approve",
    "close",
    "comment",
    "create",
    "delete",
    "fork",
    "merge",
    "publish",
    "push",
    "release",
    "review",
    "submit",
    "update",
}

_HTTP_TOOLS = {
    "api_call",
    "api_request",
    "http",
    "http_request",
    "rest_request",
    "web_request",
}
_BROWSER_MUTATION_TOOLS = {
    "browser_back",
    "browser_cdp",
    "browser_click",
    "browser_dialog",
    "browser_navigate",
    "browser_press",
    "browser_scroll",
    "browser_type",
}
_BROWSER_READ_TOOLS = {
    "browser_console",
    "browser_get_images",
    "browser_snapshot",
    "browser_vision",
}

_SAFE_TERMINAL_PROGRAMS = {
    "basename",
    "cat",
    "comm",
    "cut",
    "date",
    "df",
    "diff",
    "dirname",
    "du",
    "file",
    "git",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "rg",
    "stat",
    "tail",
    "tree",
    "wc",
    "which",
}

_SHELL_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "communication",
        re.compile(
            r"(?i)(?:^|[;&|]\s*)(?:mail|mailx|mutt|sendmail|msmtp)\b|"
            r"\bhermes\s+send\b|\b(?:slack|telegram|discord|twilio)\b.*\b(?:send|post)\b"
        ),
    ),
    (
        "code_change",
        re.compile(
            r"(?i)\bgit\s+(?:push|merge)\b|\bgh\s+pr\s+(?:create|merge|review|comment)\b|"
            r"\bglab\s+mr\s+(?:create|merge|approve)\b|\bhub\s+pull-request\b"
        ),
    ),
    (
        "publication",
        re.compile(
            r"(?i)\b(?:npm|pnpm|cargo)\s+publish\b|\byarn\s+npm\s+publish\b|"
            r"\btwine\s+upload\b|\bdocker\s+push\b|\bgh\s+release\s+create\b|"
            r"\b(?:firebase|vercel|netlify|fly)\s+(?:deploy|publish)\b|"
            r"\bhelm\s+(?:install|upgrade|uninstall)\b|"
            r"\b(?:kubectl|oc)\s+(?:apply|create|delete|patch|replace|scale)\b|"
            r"\b(?:terraform|tofu)\s+(?:apply|destroy|import)\b"
        ),
    ),
    (
        "generic_mutation",
        re.compile(
            r"(?i)\bcurl\b[^\n]*(?:--request(?:\s+|=)|-X\s*)(?:POST|PUT|PATCH|DELETE)\b|"
            r"\bcurl\b[^\n]*(?:--data(?:-binary|-raw|-urlencode)?|-d|--form|-F|"
            r"--form-string|--json|--upload-file|-T)(?:\s|=)|"
            r"\bcurl\b[^\n]*(?:\s)-(?:d|F|T)\S+|"
            r"\bwget\b[^\n]*(?:--post-data|--post-file|--body-data|--body-file|"
            r"--method(?:\s+|=)(?:POST|PUT|PATCH|DELETE))|"
            r"\bhttp(?:ie)?\s+(?:POST|PUT|PATCH|DELETE)\b|"
            r"\bInvoke-(?:WebRequest|RestMethod)\b[^\n]*-Method\s+(?:Post|Put|Patch|Delete)\b"
        ),
    ),
    (
        "destructive",
        re.compile(
            r"(?i)(?:^|[;&|]\s*)(?:rm|rmdir|shred|unlink|wipefs|mkfs(?:\.\w+)?|dropdb)\b|"
            r"\bfind\b[^\n]*(?:-delete|-exec|-execdir|-ok)\b|"
            r"\bgit\s+(?:clean\b|reset\s+--hard\b|checkout\s+--\b|branch\s+-D\b)|"
            r"\bdocker\s+(?:rm\b|system\s+prune\b)|"
            r"\b(?:DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM)\b"
        ),
    ),
    (
        "security",
        re.compile(
            r"(?i)(?:^|[;&|]\s*)(?:chmod|chown|chgrp|setfacl)\b|"
            r"\baws\s+iam\b|\bgcloud\b[^\n]*\b(?:add-iam-policy-binding|set-iam-policy)\b|"
            r"\b(?:kubectl|oc)\s+(?:create|delete|patch)\s+(?:role|rolebinding|clusterrole)\b"
        ),
    ),
    (
        "financial",
        re.compile(
            r"(?i)\b(?:stripe|paypal|coinbase)\b[^\n]*\b(?:create|pay|refund|transfer|buy|sell|withdraw)\b"
        ),
    ),
)

def guard_external_side_effects(
    tool_name: str = "",
    args: dict | None = None,
    task_id: str = "",
    *,
    authorization_lookup: AuthorizationLookup | None = None,
    expected_profile: str = "",
    **kwargs: Any,
) -> dict[str, str] | None:
    """Return Hermes' documented veto shape for disallowed worker actions."""

    del kwargs
    if args is not None and not isinstance(args, Mapping):
        decision = PolicyDecision(
            True, "generic_mutation", "malformed tool arguments cannot be evaluated safely"
        )
    else:
        try:
            current_task_id, identity_error = _current_task_identity(task_id)
            if identity_error:
                decision = PolicyDecision(True, "identity", identity_error)
            else:
                required_capability = _required_capability(tool_name, args or {})
                contract: Mapping[str, Any] | None = None
                if required_capability:
                    contract = _lookup_execution_contract(
                        authorization_lookup,
                        current_task_id,
                        expected_profile,
                        required_capability,
                    )
                decision = evaluate_tool_call(
                    tool_name,
                    args or {},
                    current_task_id=current_task_id,
                    execution_contract=contract,
                )
        except Exception:
            # Hermes catches hook exceptions and would otherwise continue the tool call.
            # Convert every policy failure into an explicit veto instead.
            decision = PolicyDecision(
                True, "generic_mutation", "policy evaluation failed closed"
            )
    return _blocked_response(decision)


def _blocked_response(decision: PolicyDecision) -> dict[str, str] | None:
    if not decision.blocked:
        return None
    return {
        "action": "block",
        "message": (
            "Blocked by Hermes Operator policy "
            f"({decision.category}): {decision.detail}. "
            "An Operator-managed worker cannot perform this side effect. Return the "
            "proposal to an interactive Hermes turn for the operator's native final "
            "approval; tool arguments, prompts, and worker claims never grant approval."
        ),
    }


def _native_session_policy(
    tool_name: str, args: Mapping[str, Any] | None
) -> dict[str, str] | None:
    """Defer normal interactive and cron sessions to Hermes native policy.

    The strict default-deny worker policy is intentionally task scoped. Outside an
    Operator-managed Kanban card we add a native approval prompt only for identifiable
    external writes and scheduler mutations, while leaving reads, Obsidian operations,
    native delegation, and unknown host capabilities to Hermes itself.
    """

    if not tool_name:
        return None
    if args is not None and not isinstance(args, Mapping):
        return None
    safe_args: Mapping[str, Any] = args or {}
    action = _action(safe_args)
    tokens = set(_tokens(tool_name)) | set(_argument_operation_tokens(safe_args))

    if tool_name == "cronjob":
        if action in {"list", "show", "status"}:
            return None
        return _native_approval(
            tool_name,
            action or "mutate",
            "Confirm this Hermes scheduler change",
        )

    # Reads from Google, mail, calendar, meetings, and other native skills should
    # remain usable. Known writes receive the harness' own approval UI.
    write_tokens = (
        _COMMUNICATION_VERBS
        | _CALENDAR_VERBS
        | _REPOSITORY_VERBS
        | _PERMISSION_VERBS
        | _FINANCIAL_VERBS
        | _DESTRUCTIVE
        | {
            "deploy",
            "publish",
            "share",
            "submit",
            "tweet",
            "upload",
        }
    )
    external_objects = (
        _COMMUNICATION_OBJECTS
        | _CALENDAR_OBJECTS
        | _REPOSITORY_OBJECTS
        | _PERMISSION_OBJECTS
        | _FINANCIAL_OBJECTS
        | {
            "drive",
            "form",
            "google",
            "gmail",
            "meet",
            "outlook",
            "slack",
            "zoom",
        }
    )
    if tokens & write_tokens and tokens & external_objects:
        return _native_approval(
            tool_name,
            action or "write",
            "Confirm this external action in Hermes",
        )
    return None


def _native_approval(
    tool_name: str, operation: str, message: str
) -> dict[str, str]:
    safe_operation = _normalize_name(operation) or "action"
    return {
        "action": "approve",
        "message": f"{message}: {tool_name} ({safe_operation})",
        "rule_key": f"hermes_operator:{tool_name}:{safe_operation}",
    }


def evaluate_tool_call(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    current_task_id: str = "",
    execution_contract: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Classify a direct Hermes tool invocation without trusting its arguments."""

    name = _normalize_name(tool_name)
    if not name:
        return PolicyDecision(True, "generic_mutation", "unnamed tool call")

    if name == "terminal":
        return _terminal_policy(
            args,
            current_task_id=current_task_id,
            execution_contract=execution_contract,
        )
    if name == "execute_code":
        return _execute_code_policy(args)
    if current_task_id and name in _MANAGED_UNSCOPED_OBSERVATION_TOOLS:
        return PolicyDecision(
            True,
            "authorization",
            f"{name} has no provable current-workspace or current-run binding",
        )
    if name == "delegate_task":
        if not _contract_has_capability(
            execution_contract, current_task_id, "delegate_task"
        ):
            return PolicyDecision(
                True,
                "authorization",
                "parallel delegation requires a live task-scoped execution contract",
            )
        return _delegate_task_policy(args)
    if name == "process":
        if current_task_id:
            return PolicyDecision(
                True,
                "authorization",
                "process output has no provable current-run ownership binding",
            )
        action = _action(args)
        if action in {"kill", "terminate", "write"} or not action:
            return PolicyDecision(
                True,
                "destructive" if action in {"kill", "terminate"} else "generic_mutation",
                "interactive or destructive process control",
            )
        return ALLOW if action in {"list", "log", "poll", "wait"} else PolicyDecision(
            True, "generic_mutation", "unknown process action"
        )

    if name in _BROWSER_MUTATION_TOOLS:
        return PolicyDecision(
            True, "generic_mutation", "browser state or navigation is not worker-authorized"
        )
    if name in _BROWSER_READ_TOOLS:
        return ALLOW
    if name == "computer_use":
        action = _action(args)
        if action in {"screenshot", "list_apps"}:
            return ALLOW
        return PolicyDecision(
            True, "generic_mutation", "interactive computer action can commit external state"
        )

    if name == "cronjob":
        action = _action(args)
        return ALLOW if action in {"list", "show", "status"} else PolicyDecision(
            True, "generic_mutation", "scheduled-job mutation or execution"
        )
    if name == "ha_call_service":
        return PolicyDecision(True, "generic_mutation", "smart-home state mutation")
    if name in {"ha_get_state", "ha_list_entities", "ha_list_services"}:
        return ALLOW

    if name == "discord":
        action = _action(args)
        return ALLOW if action in {
            "fetch_channel",
            "fetch_messages",
            "list_channels",
            "search_members",
        } else PolicyDecision(True, "communication", "Discord mutation or message")
    if name == "discord_admin":
        action = _action(args)
        return ALLOW if action.startswith(("get_", "list_", "fetch_", "search_")) else PolicyDecision(
            True, "security", "Discord administration or permission mutation"
        )

    if name in {"feishu_drive_add_comment", "feishu_drive_reply_comment"}:
        return PolicyDecision(True, "communication", "external document comment")
    if name in {
        "feishu_doc_read",
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
    }:
        return ALLOW
    if name in {"yb_send_dm", "yb_send_sticker"}:
        return PolicyDecision(True, "communication", "external chat message")
    if name.startswith("yb_query_") or name == "yb_search_sticker":
        return ALLOW

    if name == "spotify_playback":
        return PolicyDecision(True, "generic_mutation", "external playback control")
    if name in {"spotify_search", "spotify_albums"}:
        return ALLOW
    if name in {"spotify_devices", "spotify_queue", "spotify_playlists", "spotify_library"}:
        action = _action(args)
        if action in _READ_ACTIONS or action.startswith(
            ("get_", "list_", "search_", "check_", "fetch_")
        ):
            return ALLOW
        return PolicyDecision(True, "generic_mutation", "Spotify account or playback mutation")

    if name == "skill_manage":
        return PolicyDecision(True, "generic_mutation", "skill mutation is not worker-authorized")

    if name in _HTTP_TOOLS or name.endswith(("_http_request", "_api_request")):
        return PolicyDecision(True, "generic_mutation", "raw HTTP is not worker-authorized")

    if name in _READ_ONLY_KANBAN_TOOLS:
        return ALLOW
    if name in _CURRENT_TASK_KANBAN_TOOLS:
        if not _contract_has_capability(execution_contract, current_task_id, ""):
            return PolicyDecision(
                True,
                "authorization",
                "Kanban lifecycle mutation requires a live task-scoped execution contract",
            )
        target_task_id, target_error = _kanban_target_task_id(args)
        if target_error:
            return PolicyDecision(True, "authorization", target_error)
        if target_task_id and target_task_id != current_task_id:
            return PolicyDecision(
                True,
                "authorization",
                "Kanban lifecycle mutation may target only the current Hermes task",
            )
        if name == "kanban_complete":
            artifact_error = _kanban_completion_artifact_error(args)
            if artifact_error:
                return PolicyDecision(True, "sharing", artifact_error)
        return ALLOW
    if name in {
        "kanban_create",
        "kanban_link",
        "kanban_start",
        "kanban_status",
        "kanban_unblock",
        "kanban_update",
    }:
        return PolicyDecision(
            True,
            "generic_mutation",
            "durable Kanban creation, linking, unblocking, and foreign mutation are control-plane owned",
        )

    capability = _TASK_SCOPED_CAPABILITY_TOOLS.get(name)
    if capability:
        if _contract_has_capability(execution_contract, current_task_id, capability):
            workspace_error = _file_tool_workspace_error(name, args)
            if workspace_error:
                return PolicyDecision(True, "authorization", workspace_error)
            return ALLOW
        return PolicyDecision(
            True,
            "authorization",
            f"{name} requires the live {capability} task capability",
        )

    capability = _TASK_SCOPED_READ_TOOLS.get(name)
    if capability:
        if not _contract_has_capability(
            execution_contract, current_task_id, capability
        ):
            return PolicyDecision(
                True,
                "authorization",
                f"{name} requires the live {capability} task capability",
            )
        if name in {"read_file", "search_files"}:
            workspace_error = _file_tool_workspace_error(name, args)
            if workspace_error:
                return PolicyDecision(True, "authorization", workspace_error)
        return ALLOW

    if name in _EXPLICITLY_ALLOWED_TOOLS:
        return ALLOW

    tokens = set(_tokens(name))
    nested = set(_argument_operation_tokens(args))
    combined = tokens | nested

    if "push" in combined or "merge" in combined:
        return PolicyDecision(True, "code_change", "code push or merge")
    if _DESTRUCTIVE & combined:
        return PolicyDecision(True, "destructive", "destructive operation")
    if {"share", "upload"} & combined or (
        "attachment" in combined and _COMMUNICATION_VERBS & combined
    ):
        return PolicyDecision(True, "sharing", "sharing or upload operation")
    if "submit" in combined or ("form" in combined and "fill" in combined):
        return PolicyDecision(True, "submission", "external submission")
    if {"publish", "tweet"} & combined or (
        "release" in combined and _REPOSITORY_VERBS & combined
    ):
        return PolicyDecision(True, "publication", "external publication")
    if _COMMUNICATION_OBJECTS & combined and _COMMUNICATION_VERBS & combined:
        return PolicyDecision(True, "communication", "external communication")
    if _CALENDAR_OBJECTS & combined and _CALENDAR_VERBS & combined:
        return PolicyDecision(True, "scheduling", "calendar or meeting mutation")
    if _FINANCIAL_OBJECTS & combined and (
        _FINANCIAL_VERBS & combined or not (_READ_ACTIONS & combined)
    ):
        return PolicyDecision(True, "financial", "financial transaction")
    if _PERMISSION_OBJECTS & combined and _PERMISSION_VERBS & combined:
        return PolicyDecision(True, "security", "account or permission mutation")
    if _REPOSITORY_OBJECTS & combined and _REPOSITORY_VERBS & combined:
        return PolicyDecision(True, "code_change", "remote repository mutation")
    if {"api", "external"} & combined and {
        "call",
        "create",
        "execute",
        "mutate",
        "set",
        "update",
        "write",
    } & combined:
        return PolicyDecision(True, "generic_mutation", "external API mutation")

    if name.startswith("mcp_"):
        return PolicyDecision(
            True, "generic_mutation", "MCP capability is not explicitly reviewed"
        )
    return PolicyDecision(
        True, "generic_mutation", "tool is not on the explicit worker allowlist"
    )


def _required_capability(tool_name: str, args: Mapping[str, Any]) -> str:
    name = _normalize_name(tool_name)
    if name == "delegate_task":
        return "delegate_task"
    if name in _CURRENT_TASK_KANBAN_TOOLS:
        return "__live_task__"
    if name in _TASK_SCOPED_CAPABILITY_TOOLS:
        return _TASK_SCOPED_CAPABILITY_TOOLS[name]
    if name in _TASK_SCOPED_READ_TOOLS:
        return _TASK_SCOPED_READ_TOOLS[name]
    if name == "terminal":
        command = _first_string(args, ("command", "cmd", "script"))
        if not command:
            return ""
        if _is_safe_terminal_command(command):
            return "local_read"
        return _internal_command_capability(command)
    return ""


def _current_task_identity(hook_task_id: str) -> tuple[str, str]:
    """Return the dispatcher-owned managed-worker identity when present.

    Current Hermes assigns every ordinary interactive and Cron turn an ephemeral UUID
    and forwards it as the hook ``task_id``.  That value isolates the turn but does not
    make it an Operator-managed Kanban worker.  The dispatcher-controlled
    ``HERMES_KANBAN_TASK`` environment marker is authoritative. Quiet Kanban workers
    also receive a fresh turn UUID, so equality with the hook identity would reject
    every real managed worker. When the marker does not exist, the turn is native
    regardless of the ordinary Hermes UUID.
    """

    del hook_task_id
    environment_identity = os.getenv("HERMES_KANBAN_TASK", "").strip()
    if not environment_identity:
        return "", ""
    if not _TASK_ID_PATTERN.fullmatch(environment_identity):
        return "", "HERMES_KANBAN_TASK is not a valid Hermes task identity"
    return environment_identity, ""


def _lookup_execution_contract(
    lookup: AuthorizationLookup | None,
    task_id: str,
    expected_profile: str,
    required_capability: str,
) -> Mapping[str, Any] | None:
    if lookup is None or not task_id:
        return None
    try:
        contract = lookup(task_id)
    except Exception:
        return None
    if not isinstance(contract, Mapping):
        return None
    required_keys = {
        "authorized",
        "contract_digest",
        "internal_capabilities",
        "profile",
        "run_id",
        "task_id",
        "work_id",
    }
    if set(contract) != required_keys:
        return None
    if contract.get("authorized") is not True or contract.get("task_id") != task_id:
        return None
    for key in ("profile", "run_id", "work_id"):
        value = contract.get(key)
        if not isinstance(value, str) or not value or len(value) > 128:
            return None
    if expected_profile and contract.get("profile") != expected_profile:
        return None
    digest = contract.get("contract_digest")
    if not isinstance(digest, str) or not _CONTRACT_DIGEST_PATTERN.fullmatch(digest):
        return None
    capabilities = contract.get("internal_capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or len(capabilities) > len(_KNOWN_INTERNAL_CAPABILITIES)
        or len(set(capabilities)) != len(capabilities)
        or any(
            not isinstance(value, str) or value not in _KNOWN_INTERNAL_CAPABILITIES
            for value in capabilities
        )
    ):
        return None
    if (
        required_capability != "__live_task__"
        and required_capability not in capabilities
    ):
        return None
    return contract


def _contract_has_capability(
    contract: Mapping[str, Any] | None,
    current_task_id: str,
    capability: str,
) -> bool:
    if (
        not contract
        or contract.get("authorized") is not True
        or not current_task_id
        or contract.get("task_id") != current_task_id
    ):
        return False
    if not capability:
        return True
    capabilities = contract.get("internal_capabilities")
    return isinstance(capabilities, list) and capability in capabilities


def _kanban_target_task_id(args: Mapping[str, Any]) -> tuple[str, str]:
    values: list[str] = []
    for key in ("task_id", "task", "id"):
        if key not in args:
            continue
        value = args.get(key)
        if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value.strip()):
            return "", f"Kanban {key} is not a valid task identity"
        values.append(value.strip())
    if len(set(values)) > 1:
        return "", "Kanban tool arguments contain conflicting task identities"
    return (values[0] if values else ""), ""


def _kanban_completion_artifact_error(args: Mapping[str, Any]) -> str:
    """Reject Hermes fields that can turn completion into file delivery.

    This check applies only after a live Operator execution contract has been
    established, so it does not alter normal Hermes Kanban behavior outside cards
    managed by this system.
    """

    artifacts = args.get("artifacts")
    if artifacts not in (None, "", [], ()):
        return "Operator-managed completion cannot attach or deliver artifact paths"
    for key in ("summary", "result"):
        value = args.get(key)
        if value is not None and not isinstance(value, str):
            return (
                "Operator-managed completion summary and result must be strings "
                "so artifact paths can be evaluated before Hermes transforms them"
            )
    metadata = args.get("metadata")
    if isinstance(metadata, Mapping):
        for key in metadata:
            normalized = _normalize_name(key)
            if normalized in {"artifact", "artifacts", "attachment", "attachments"}:
                return "Operator-managed completion metadata cannot attach or deliver files"

    # Newer Hermes hosts discover existing absolute/home-relative files named in
    # completion prose after pre_tool_call hooks have returned, then promote them
    # into notification attachments. That promotion is not confined to the worker
    # workspace: an existing file anywhere the Hermes process can read may be sent.
    # Reject every path shape the Gateway can promote, without consulting existence
    # or containment, so ``~``, ``..``, symlinks, and paths written outside the
    # workspace cannot cross the approval boundary.
    # We intentionally do not offer an allow flag: the hook receives no authenticated
    # notification-recipient identity and therefore cannot prove a configured sink
    # is the actual destination. Files can still be delivered from an interactive
    # Hermes turn after native approval.
    prose = "\n".join(
        value for key in ("summary", "result")
        if isinstance((value := args.get(key)), str) and value
    )
    if prose and _prose_path_candidates(prose):
        return (
            "Operator-managed completion prose cannot name absolute or home-relative "
            "local paths because Hermes may promote them to notification attachments"
        )
    return ""


_PROSE_PATH_PATTERN = re.compile(
    r"(?P<quoted>['\"](?P<quoted_path>(?:~[/\\]|/|[A-Za-z]:[/\\])[^'\"\r\n]+)['\"])"
    r"|(?<![A-Za-z0-9_:/\\])"
    r"(?P<bare>(?:~[/\\]|/|[A-Za-z]:[/\\])[^\s`'\"<>]+)"
)
_PROSE_MEDIA_PATH_ANCHOR_PATTERN = re.compile(
    r"(?i:\bMEDIA)\s*:\s*[`'\"]?(?:~[/\\]|/|[A-Za-z]:[/\\])"
)
_PATH_TRAILING_PUNCTUATION = "`\"',.;:)}]"


def _prose_path_candidates(prose: str) -> tuple[str, ...]:
    """Extract path shapes Hermes can later promote into native attachments."""

    candidates: list[str] = []
    candidates.extend(
        match.group(0)
        for match in _PROSE_MEDIA_PATH_ANCHOR_PATTERN.finditer(prose)
    )
    for match in _PROSE_PATH_PATTERN.finditer(prose):
        raw = match.group("quoted_path") or match.group("bare") or ""
        candidate = raw.strip().rstrip(_PATH_TRAILING_PUNCTUATION)
        if candidate:
            candidates.append(candidate)
    return tuple(candidates)


def _human_confirmation(
    tool_name: str, args: Mapping[str, Any] | None
) -> dict[str, str]:
    """Use Hermes' native human approval gate for irreversible intent changes."""

    if not isinstance(args, Mapping):
        return _blocked_response(
            PolicyDecision(
                True,
                "authorization",
                "malformed operator interaction arguments cannot be confirmed",
            )
        ) or {}
    identity_key = _HUMAN_CONFIRMATION_TOOLS[tool_name]
    identity = args.get(identity_key)
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or len(identity.strip()) > 128
        or not _TASK_ID_PATTERN.fullmatch(identity.strip())
    ):
        return _blocked_response(
            PolicyDecision(
                True,
                "authorization",
                f"{identity_key} must be one exact safe identifier",
            )
        ) or {}
    if tool_name == "operator_answer_question":
        answer = args.get("answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 20_000:
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "question approval requires one exact nonempty bounded answer",
                )
            ) or {}
        answer_digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        return {
            "action": "approve",
            "message": (
                "Confirm that you want Hermes to submit this exact answer to "
                f"Operator question {identity.strip()}"
            ),
            "rule_key": f"{tool_name}:{identity.strip()}:{answer_digest[:16]}",
        }
    if tool_name == "operator_resolve_reminder":
        expected_version = args.get("expected_version")
        action = args.get("action")
        until = args.get("until")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
            or action not in {"snooze", "acknowledge", "complete"}
        ):
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "reminder confirmation requires an exact version and lifecycle action",
                )
            ) or {}
        if action == "snooze":
            if not isinstance(until, str) or not until.strip() or len(until) > 128:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "snooze confirmation requires one bounded timezone-aware until timestamp",
                    )
                ) or {}
            try:
                parsed_until = datetime.fromisoformat(until)
            except ValueError:
                parsed_until = None
            if parsed_until is None or parsed_until.tzinfo is None:
                return _blocked_response(
                    PolicyDecision(
                        True,
                        "authorization",
                        "snooze confirmation requires one bounded timezone-aware until timestamp",
                    )
                ) or {}
            normalized_until: str | None = until
        elif until is not None:
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "until is accepted only for an exact snooze confirmation",
                )
            ) or {}
        else:
            normalized_until = None
        reminder_shape = {
            "action": action,
            "expected_version": expected_version,
            "until": normalized_until,
            "work_id": identity.strip(),
        }
        shape_digest = hashlib.sha256(
            json.dumps(
                reminder_shape,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        until_suffix = f" until {normalized_until}" if normalized_until else ""
        return {
            "action": "approve",
            "message": (
                "Confirm this exact Operator reminder action: "
                f"{action} {identity.strip()} version {expected_version}{until_suffix}"
            ),
            "rule_key": (
                f"{tool_name}:{identity.strip()}:v{expected_version}:"
                f"{shape_digest[:16]}"
            ),
        }
    if tool_name == "operator_authorize_work":
        expected_version = args.get("expected_version")
        expected_scope_revision = args.get("expected_scope_revision")
        expected_scope_digest = args.get("expected_scope_digest")
        profile = args.get("profile")
        skills = args.get("skills")
        goal_mode = args.get("goal_mode")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
            or not isinstance(expected_scope_revision, int)
            or isinstance(expected_scope_revision, bool)
            or expected_scope_revision < 1
            or not isinstance(expected_scope_digest, str)
            or not _CONTRACT_DIGEST_PATTERN.fullmatch(expected_scope_digest)
            or (
                profile is not None
                and (
                    not isinstance(profile, str)
                    or not profile.strip()
                    or len(profile) > 128
                )
            )
            or (
                skills is not None
                and (
                    not isinstance(skills, list)
                    or len(skills) > 64
                    or any(
                        not isinstance(value, str)
                        or not value.strip()
                        or len(value) > 128
                        for value in skills
                    )
                )
            )
            or (goal_mode is not None and not isinstance(goal_mode, bool))
        ):
            return _blocked_response(
                PolicyDecision(
                    True,
                    "authorization",
                    "work authorization requires an exact version, scope revision, scope digest, and valid execution parameters",
                )
            ) or {}
        execution_shape = {
            "goal_mode": goal_mode,
            "profile": profile.strip() if isinstance(profile, str) else None,
            "scope_digest": expected_scope_digest,
            "scope_revision": expected_scope_revision,
            "skills": [value.strip() for value in skills] if isinstance(skills, list) else None,
            "work_id": identity.strip(),
            "work_version": expected_version,
        }
        shape_digest = hashlib.sha256(
            json.dumps(
                execution_shape,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        overrides: list[str] = []
        if execution_shape["profile"] is not None:
            overrides.append(f"profile={execution_shape['profile']}")
        if execution_shape["skills"] is not None:
            overrides.append(f"skills={','.join(execution_shape['skills']) or 'none'}")
        if execution_shape["goal_mode"] is not None:
            overrides.append(f"goal_mode={str(execution_shape['goal_mode']).lower()}")
        suffix = f" ({'; '.join(overrides)})" if overrides else ""
        return {
            "action": "approve",
            "message": (
                "Confirm that you want Hermes to authorize this exact Operator work "
                f"scope: {identity.strip()} version {expected_version}, scope revision "
                f"{expected_scope_revision}, digest {expected_scope_digest}{suffix}"
            ),
            "rule_key": (
                f"{tool_name}:{identity.strip()}:v{expected_version}:"
                f"r{expected_scope_revision}:{expected_scope_digest}:"
                f"{shape_digest[:16]}"
            ),
        }
    return _blocked_response(
        PolicyDecision(True, "authorization", "unknown confirmation tool")
    ) or {}


def _work_update_confirmation(
    args: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if not isinstance(args, Mapping):
        return _blocked_response(
            PolicyDecision(True, "authorization", "work update arguments are malformed")
        )
    work_id = args.get("work_id")
    expected_version = args.get("expected_version")
    changes = args.get("changes")
    if (
        not isinstance(work_id, str)
        or not _TASK_ID_PATTERN.fullmatch(work_id.strip())
        or not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
        or not isinstance(changes, Mapping)
        or not changes
    ):
        return _blocked_response(
            PolicyDecision(True, "authorization", "work update identity or changes are invalid")
        )
    fields = sorted(str(key) for key in changes)
    try:
        encoded_changes = json.dumps(
            changes,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return _blocked_response(
            PolicyDecision(
                True,
                "authorization",
                "work update changes cannot be bound to an exact approval",
            )
        )
    changes_digest = hashlib.sha256(encoded_changes).hexdigest()
    return {
        "action": "approve",
        "message": (
            "Confirm this exact Operator work update: "
            f"{work_id.strip()} version {expected_version} ({', '.join(fields)})"
        ),
        "rule_key": (
            f"operator_update_work:{work_id.strip()}:v{expected_version}:"
            f"{changes_digest[:16]}"
        ),
    }


def _delegate_task_policy(args: Mapping[str, Any]) -> PolicyDecision:
    top_level_error = _delegation_entry_error(args)
    if top_level_error:
        return PolicyDecision(True, "generic_mutation", top_level_error)

    tasks = args.get("tasks")
    if tasks is None:
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return PolicyDecision(True, "generic_mutation", "delegate_task requires one goal")
        return ALLOW
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 3:
        return PolicyDecision(
            True, "generic_mutation", "parallel delegation requires a batch of one to three tasks"
        )
    for task in tasks:
        if not isinstance(task, Mapping):
            return PolicyDecision(
                True, "generic_mutation", "every delegated task must be an object"
            )
        entry_error = _delegation_entry_error(task)
        if entry_error:
            return PolicyDecision(True, "generic_mutation", entry_error)
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return PolicyDecision(
                True, "generic_mutation", "every delegated task requires a goal"
            )
    return ALLOW


def _delegated_child_count(args: Mapping[str, Any]) -> int:
    tasks = args.get("tasks")
    return len(tasks) if isinstance(tasks, list) else 1


def _delegation_entry_error(entry: Mapping[str, Any]) -> str:
    if "background" in entry and entry.get("background") is not False:
        return "all delegated tasks must run in the foreground"
    if "role" in entry and not isinstance(entry.get("role"), str):
        return "delegated roles must be strings"
    if _normalize_name(entry.get("role")) == "orchestrator":
        return "delegated orchestrator roles are not permitted"
    return ""


def _terminal_policy(
    args: Mapping[str, Any],
    *,
    current_task_id: str = "",
    execution_contract: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    command = _first_string(args, ("command", "cmd", "script"))
    if not command:
        return PolicyDecision(True, "generic_mutation", "empty or unknown terminal command")
    for category, pattern in _SHELL_RISK_PATTERNS:
        if pattern.search(command):
            return PolicyDecision(True, category, "terminal command can cause a prohibited side effect")
    if _is_safe_terminal_command(command):
        if current_task_id and not _contract_has_capability(
            execution_contract,
            current_task_id,
            "local_read",
        ):
            return PolicyDecision(
                True,
                "authorization",
                "local inspection requires the live local_read task capability",
            )
        if current_task_id:
            workspace_error = _terminal_workspace_error(command)
            if workspace_error:
                return PolicyDecision(True, "authorization", workspace_error)
        return ALLOW
    capability = _internal_command_capability(command)
    if capability and _contract_has_capability(
        execution_contract,
        current_task_id,
        capability,
    ):
        workspace_error = _terminal_workspace_error(command)
        if workspace_error:
            return PolicyDecision(True, "authorization", workspace_error)
        return ALLOW
    if capability:
        return PolicyDecision(
            True,
            "authorization",
            f"local command requires the live {capability} task capability",
        )
    return PolicyDecision(
        True,
        "generic_mutation",
        "terminal command is outside the local read, test, and build allowlists",
    )


def _is_safe_terminal_command(command: str) -> bool:
    segments = _command_segments(command)
    return bool(segments) and all(_safe_command_segment(segment) for segment in segments)


def _command_segments(command: str, *, allow_pipes: bool = True) -> list[list[str]]:
    if any(fragment in command for fragment in ("$(", "`", "\n", "\r", "<(", ">(")):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    disallowed = {">", ">>", "<", "<<", "&", "||", ";"}
    if not allow_pipes:
        disallowed.add("|")
    if not tokens or any(token in disallowed for token in tokens):
        return []

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"&&", "|"}:
            if not segments[-1]:
                return []
            segments.append([])
        else:
            segments[-1].append(token)
    return segments if segments[-1] else []


def _safe_command_segment(tokens: Sequence[str]) -> bool:
    values = list(tokens)
    if not values:
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", values[0]):
        return False
    if "/" in values[0] or "\\" in values[0]:
        return False
    program = values[0].rsplit("/", 1)[-1].lower()
    arguments = [value.lower() for value in values[1:]]
    if program == "cd":
        return len(arguments) == 1
    if program not in _SAFE_TERMINAL_PROGRAMS:
        return False
    if any("http://" in value or "https://" in value for value in arguments):
        return False
    if program == "rg" and any(value.startswith("--pre") for value in arguments):
        return False
    if program == "git":
        return _safe_git(arguments)
    if program == "tree" and any(
        value == "-o"
        or value.startswith("-o")
        or value.startswith("--output")
        for value in arguments
    ):
        return False
    if program == "diff" and any(value.startswith("--output") for value in arguments):
        return False
    return True


def _workspace_root() -> tuple[Path | None, str]:
    """Return the dispatcher-owned workspace as one canonical directory."""

    raw = os.getenv(_WORKSPACE_ENV, "").strip()
    if not raw:
        return None, f"{_WORKSPACE_ENV} is required for managed local work"
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        return None, f"{_WORKSPACE_ENV} contains invalid characters"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None, f"{_WORKSPACE_ENV} must be an absolute directory"
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"{_WORKSPACE_ENV} cannot be resolved"
    if not resolved.is_dir():
        return None, f"{_WORKSPACE_ENV} must identify an existing directory"
    return resolved, ""


def _contained_workspace_path(
    raw_path: Any,
    *,
    root: Path,
    field: str,
) -> tuple[Path | None, str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, f"{field} must be one nonempty workspace path"
    if any(character in raw_path for character in ("\x00", "\n", "\r")):
        return None, f"{field} contains invalid characters"
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, f"{field} escapes the authorized Hermes workspace"
    return resolved, ""


def _patch_paths(args: Mapping[str, Any]) -> tuple[list[str], str]:
    mode = _normalize_name(args.get("mode") or "replace")
    if mode == "replace":
        value = args.get("path")
        return ([value] if isinstance(value, str) and value.strip() else []), (
            "patch replace mode requires one explicit path"
            if not isinstance(value, str) or not value.strip()
            else ""
        )
    if mode != "patch":
        return [], "patch mode is not recognized"
    body = args.get("patch")
    if not isinstance(body, str) or not body.strip() or len(body) > 2_000_000:
        return [], "patch mode requires one bounded V4A patch"
    paths: list[str] = []
    for line in body.splitlines():
        match = _PATCH_FILE_LINE.fullmatch(line) or _PATCH_MOVE_LINE.fullmatch(line)
        if match:
            paths.append(match.group("path").strip())
    if not paths:
        return [], "patch mode must expose every target in V4A file headers"
    return paths, ""


def _file_tool_workspace_error(name: str, args: Mapping[str, Any]) -> str:
    root, root_error = _workspace_root()
    if root_error or root is None:
        return root_error
    if name == "search_files":
        paths = [
            value
            for key, value in args.items()
            if _normalize_name(key) in {"path", "directory", "root", "cwd"}
        ] or ["."]
    elif name in {"read_file", "write_file"}:
        paths = [
            value
            for key, value in args.items()
            if _normalize_name(key) in {"path", "file_path", "filename"}
        ]
        if len(paths) != 1:
            return f"{name} requires exactly one explicit workspace path"
    elif name == "patch":
        paths, patch_error = _patch_paths(args)
        if patch_error:
            return patch_error
    else:
        return "local file tool is not covered by workspace policy"
    for index, value in enumerate(paths):
        _, path_error = _contained_workspace_path(
            value,
            root=root,
            field=f"{name} path {index + 1}",
        )
        if path_error:
            return path_error
    return ""


def _terminal_workspace_error(command: str) -> str:
    """Conservatively bind managed terminal paths and cwd changes to the workspace."""

    root, root_error = _workspace_root()
    if root_error or root is None:
        return root_error
    segments = _command_segments(command)
    if not segments:
        return "terminal command cannot be safely parsed for workspace containment"
    cwd = root
    for segment in segments:
        if not segment:
            return "terminal command contains an empty segment"
        program = segment[0].lower()
        arguments = list(segment[1:])
        if program == "cd":
            if len(arguments) != 1:
                return "managed cd requires one workspace-relative directory"
            target, target_error = _contained_workspace_path(
                arguments[0], root=cwd, field="terminal cwd"
            )
            if target_error or target is None:
                return target_error
            try:
                target.relative_to(root)
            except ValueError:
                return "terminal cwd escapes the authorized Hermes workspace"
            cwd = target
            continue

        lowered = [value.lower() for value in arguments]
        if program == "tree" and any(
            value == "-o"
            or value.startswith("-o")
            or value.startswith("--output")
            for value in lowered
        ):
            return "tree output files are not allowed in managed inspection commands"
        if program == "diff" and any(value.startswith("--output") for value in lowered):
            return "diff output files are not allowed in managed inspection commands"
        if program == "git" and any(
            value == "-c"
            or value.startswith("--config-env")
            or value.startswith("--exec-path")
            or value.startswith("--git-dir")
            or value.startswith("--work-tree")
            or value.startswith("--output")
            or value in {"--ext-diff", "--textconv"}
            for value in lowered
        ):
            return "git configuration, output, or external helpers are not allowed"
        if program == "rg" and any(
            value.startswith(("--pre", "--ignore-file", "--file"))
            for value in lowered
        ):
            return "ripgrep helper and external pattern files are not allowed"
        external_file_options = {
            "date": ("--file", "-f"),
            "diff": ("--from-file", "--to-file"),
            "du": ("--files0-from",),
            "grep": ("--exclude-from", "--include-from"),
            "jq": ("--argfile", "--rawfile", "--slurpfile"),
            "tree": ("--fromfile",),
            "wc": ("--files0-from",),
        }.get(program, ())
        if any(
            value == option or value.startswith(f"{option}=")
            for value in lowered
            for option in external_file_options
        ):
            return f"{program} external file options are not allowed"

        for value in arguments:
            if value.startswith("-"):
                continue
            path_candidate = Path(value).expanduser()
            if path_candidate.is_absolute() or ".." in path_candidate.parts:
                _, path_error = _contained_workspace_path(
                    value, root=cwd, field="terminal argument"
                )
                if path_error:
                    return path_error

        path_values: list[str] = []
        if program in {
            "basename",
            "cat",
            "comm",
            "cut",
            "df",
            "diff",
            "dirname",
            "du",
            "file",
            "head",
            "ls",
            "stat",
            "tail",
            "tree",
            "wc",
        }:
            path_values = [value for value in arguments if not value.startswith("-")]
        elif program == "jq":
            values = [value for value in arguments if not value.startswith("-")]
            path_values = values[1:]
        elif program in {"grep", "rg"}:
            values = [value for value in arguments if not value.startswith("-")]
            path_values = values[1:]
        elif program == "git" and "--" in arguments:
            path_values = arguments[arguments.index("--") + 1 :]

        for value in path_values:
            _, path_error = _contained_workspace_path(
                value, root=cwd, field=f"{program} path"
            )
            if path_error:
                return path_error
    return ""


def _internal_command_capability(command: str) -> str:
    segments = _command_segments(command, allow_pipes=False)
    if not segments:
        return ""
    protected: set[str] = set()
    for segment in segments:
        if _safe_command_segment(segment):
            continue
        capability = _internal_command_segment_capability(segment)
        if not capability:
            return ""
        protected.add(capability)
    if len(protected) != 1:
        return ""
    return next(iter(protected))


def _internal_command_segment_capability(tokens: Sequence[str]) -> str:
    values = list(tokens)
    if not values or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", values[0]):
        return ""
    if "/" in values[0] or "\\" in values[0]:
        return ""
    if any("http://" in value.lower() or "https://" in value.lower() for value in values):
        return ""
    program = values[0].lower()
    arguments = [value.lower() for value in values[1:]]

    if program in {"pytest", "py.test", "ctest", "mypy", "ruff"}:
        return "local_test"
    if program in {"python", "python3"}:
        if len(arguments) >= 2 and arguments[0] == "-m":
            if arguments[1] in {"pytest", "unittest"}:
                return "local_test"
            if arguments[1] == "build" and "--no-isolation" in arguments[2:]:
                return "local_build"
        return ""
    if program in {"npm", "pnpm", "yarn"}:
        action = _package_script_action(arguments)
        if action.startswith(("test", "check", "lint")):
            return "local_test"
        if action.startswith("build"):
            return "local_build"
        return ""
    if program == "cargo":
        if "--offline" not in arguments:
            return ""
        actions = [value for value in arguments if not value.startswith("-")]
        if not actions:
            return ""
        if actions[0] == "test":
            return "local_test"
        if actions[0] in {"build", "check"}:
            return "local_build"
        return ""
    if program == "dotnet":
        if "--no-restore" not in arguments or not arguments:
            return ""
        if arguments[0] == "test":
            return "local_test"
        if arguments[0] == "build":
            return "local_build"
        return ""
    if program in {"make", "ninja"}:
        targets = [value for value in arguments if not value.startswith("-")]
        if not targets:
            return ""
        if all(value in {"test", "tests", "check", "lint"} for value in targets):
            return "local_test"
        if all(value in {"build", "all"} for value in targets):
            return "local_build"
        return ""
    if program == "tsc":
        return "local_test" if "--noemit" in arguments else "local_build"
    return ""


def _package_script_action(arguments: Sequence[str]) -> str:
    if not arguments:
        return ""
    if arguments[0] in {"test", "build"}:
        return arguments[0]
    if len(arguments) >= 2 and arguments[0] == "run":
        return arguments[1]
    return ""


def _safe_git(arguments: Sequence[str]) -> bool:
    if not arguments:
        return False
    if any(value in {"--ext-diff", "--textconv"} for value in arguments):
        return False
    subcommand = arguments[0]
    return subcommand in {
        "blame",
        "describe",
        "diff",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "status",
    }


def _execute_code_policy(args: Mapping[str, Any]) -> PolicyDecision:
    del args
    return PolicyDecision(
        True,
        "generic_mutation",
        "arbitrary code execution is not available to autonomous workers",
    )


def _normalize_name(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(part for part in _normalize_name(value).split("_") if part)


def _first_string(args: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _action(args: Mapping[str, Any]) -> str:
    return _normalize_name(
        _first_string(args, ("action", "operation", "command_name", "mode"))
    )


def _argument_operation_tokens(args: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("action", "operation", "method", "tool", "tool_name", "command_name"):
        value = args.get(key)
        if isinstance(value, str):
            values.extend(_tokens(value))
    return tuple(values)


__all__ = [
    "POLICY_DIGEST",
    "POLICY_MODE",
    "POLICY_VERSION",
    "PolicyDecision",
    "TaskScopedPolicyGuard",
    "evaluate_tool_call",
    "guard_external_side_effects",
]
