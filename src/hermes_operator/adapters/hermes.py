"""Portable Hermes Kanban adapters.

``HermesCLIAdapter`` talks to the public ``hermes kanban`` CLI rather than
importing Hermes internals.  This keeps the operator install independent from
the Python environment where Hermes happens to run.  ``InMemoryHermesAdapter``
implements the same protocol for local development and deterministic tests.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit, urlunsplit

from .base import (
    AdapterCommandError,
    AdapterHealth,
    AdapterResponseError,
    AdapterUnavailableError,
    HermesCapabilities,
    HermesTask,
)


KANBAN_CAPABILITIES: tuple[str, ...] = (
    "create",
    "show",
    "list",
    "comment",
    "block",
    "unblock",
    "runs",
)

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
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
}


def _clean_argv_part(value: object, *, field: str = "argument") -> str:
    """Convert a value to one safe argv element.

    Shell metacharacters are intentionally allowed because the process is
    always executed with ``shell=False``.  NUL is forbidden by all supported
    process APIs and an empty executable or option value is almost certainly a
    configuration error.
    """

    text = os.fspath(value) if isinstance(value, os.PathLike) else str(value)
    if not text:
        raise ValueError(f"{field} cannot be empty")
    if "\x00" in text:
        raise ValueError(f"{field} cannot contain NUL")
    return text


def _task_from_mapping(
    value: Mapping[str, Any],
    *,
    fallback_id: str | None = None,
    fallback_title: str = "",
) -> HermesTask:
    task_id = value.get("id") or value.get("task_id") or fallback_id
    if task_id is None:
        raise AdapterResponseError("Hermes task response did not contain an id")

    comments_value = value.get("comments", [])
    comments: list[Mapping[str, Any]] = []
    if isinstance(comments_value, list):
        comments = [item for item in comments_value if isinstance(item, Mapping)]

    return HermesTask(
        id=str(task_id),
        title=str(value.get("title") or value.get("name") or fallback_title),
        status=str(value.get("status") or value.get("state") or "triage"),
        description=str(value.get("body") or value.get("description") or ""),
        priority=value.get("priority"),
        assignee=_optional_text(value.get("assignee") or value.get("profile")),
        parent_id=_optional_text(value.get("parent_id") or value.get("parent")),
        scheduled_at=_optional_text(value.get("scheduled_at")),
        created_at=_optional_text(value.get("created_at") or value.get("created")),
        updated_at=_optional_text(value.get("updated_at") or value.get("updated")),
        current_run_id=_optional_text(
            value.get("current_run_id")
            or (
                value.get("current_run", {}).get("id")
                if isinstance(value.get("current_run"), Mapping)
                else None
            )
        ),
        comments=comments,
        raw=dict(value),
    )


def _optional_text(value: object | None) -> str | None:
    return None if value is None else str(value)


class HermesCLIAdapter:
    """Hermes Kanban adapter implemented through safe subprocess argv calls.

    Parameters are deliberately explicit and portable.  ``binary`` can be a
    path or an argv prefix, which supports wrappers such as
    ``("docker", "exec", "hermes", "hermes")`` without invoking a shell.
    The injectable runner is primarily intended for contract tests.
    """

    def __init__(
        self,
        *,
        binary: str | os.PathLike[str] | Sequence[str] = "hermes",
        profile: str | None = None,
        board: str | None = None,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
        control_base_url: str = "",
        control_token: str = "",
        control_timeout_seconds: float = 10.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if isinstance(binary, (str, os.PathLike)):
            binary_parts = (_clean_argv_part(binary, field="binary"),)
        else:
            binary_parts = tuple(
                _clean_argv_part(part, field="binary argv") for part in binary
            )
        if not binary_parts:
            raise ValueError("binary cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if control_timeout_seconds <= 0:
            raise ValueError("control_timeout_seconds must be positive")

        normalized_control_url = control_base_url.strip().rstrip("/")
        if normalized_control_url:
            parsed = urlsplit(normalized_control_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "control_base_url must be an http(s) URL without credentials, query, or fragment"
                )
            normalized_control_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
            )

        self.binary = binary_parts
        self.profile = _clean_argv_part(profile, field="profile") if profile else None
        self.board = _clean_argv_part(board, field="board") if board else None
        self.timeout_seconds = float(timeout_seconds)
        self.env = dict(env or {})
        self.control_base_url = normalized_control_url
        self.control_token = control_token.strip()
        self.control_timeout_seconds = float(control_timeout_seconds)
        self._runner = runner

    def _kanban_argv(self, action: str, *arguments: object) -> list[str]:
        argv = list(self.binary)
        if self.profile:
            argv.extend(("-p", self.profile))
        argv.append("kanban")
        if self.board:
            argv.extend(("--board", self.board))
        argv.append(_clean_argv_part(action, field="action"))
        argv.extend(_clean_argv_part(value) for value in arguments)
        return argv

    def _execute(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
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
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"Hermes executable was not found: {self.binary[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterUnavailableError(
                f"Hermes command timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise AdapterUnavailableError(f"Could not execute Hermes: {exc}") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            detail = stderr or (result.stdout or "").strip() or "no error output"
            raise AdapterCommandError(
                f"Hermes command failed with exit code {result.returncode}: {detail}",
                argv=argv,
                returncode=result.returncode,
                stderr=stderr,
            )
        return result

    def _run_text(self, argv: Sequence[str]) -> str:
        return (self._execute(argv).stdout or "").strip()

    def _run_json(self, argv: Sequence[str]) -> Any:
        output = self._run_text(argv)
        if not output:
            raise AdapterResponseError("Hermes returned an empty JSON response")
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            # Some installations print a short informational line before JSON.
            # Parsing the final non-empty line preserves strictness while
            # remaining compatible with those versions.
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            if lines:
                try:
                    return json.loads(lines[-1])
                except json.JSONDecodeError:
                    pass
            raise AdapterResponseError("Hermes returned invalid JSON") from exc

    @staticmethod
    def _task_payload(payload: Any) -> Mapping[str, Any]:
        if isinstance(payload, Mapping):
            task = payload.get("task")
            if isinstance(task, Mapping):
                return task
            data = payload.get("data")
            if isinstance(data, Mapping):
                nested_task = data.get("task")
                if isinstance(nested_task, Mapping):
                    return nested_task
                return data
            return payload
        raise AdapterResponseError("Hermes returned an unexpected task response")

    def health(self) -> AdapterHealth:
        try:
            version_output = self._run_text([*self.binary, "--version"])
            capabilities = self.check_capabilities()
        except (AdapterUnavailableError, AdapterCommandError, AdapterResponseError) as exc:
            return AdapterHealth(
                enabled=True,
                available=False,
                detail=str(exc),
            )

        version_match = re.search(r"\d+(?:\.\d+)+(?:[-+._a-zA-Z0-9]*)?", version_output)
        version = version_match.group(0) if version_match else version_output or None
        available = capabilities.complete
        detail = (
            "Hermes CLI and required Kanban commands are available"
            if available
            else f"Missing Kanban commands: {', '.join(capabilities.missing)}"
        )
        return AdapterHealth(
            enabled=True,
            available=available,
            detail=detail,
            version=version,
            capabilities=capabilities.available,
        )

    def check_capabilities(
        self, required: Sequence[str] | None = None
    ) -> HermesCapabilities:
        required_names = tuple(required or KANBAN_CAPABILITIES)
        try:
            help_text = self._run_text(self._kanban_argv("--help"))
        except (AdapterUnavailableError, AdapterCommandError) as exc:
            return HermesCapabilities(
                available=(),
                missing=required_names,
                detail=str(exc),
            )

        available = tuple(
            name
            for name in required_names
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", help_text)
        )
        missing = tuple(name for name in required_names if name not in available)
        detail = "All required commands found" if not missing else "Some commands are missing"
        return HermesCapabilities(available=available, missing=missing, detail=detail)

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        priority: int | float | str | None = None,
        assignee: str | None = None,
        parent_id: str | None = None,
        idempotency_key: str | None = None,
        scheduled_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> HermesTask:
        argv = self._kanban_argv("create", title)
        if description:
            argv.extend(("--body", _clean_argv_part(description, field="description")))
        if assignee:
            argv.extend(("--assignee", _clean_argv_part(assignee, field="assignee")))
        if parent_id:
            argv.extend(("--parent", _clean_argv_part(parent_id, field="parent_id")))
        if priority is not None:
            argv.extend(("--priority", _clean_argv_part(priority, field="priority")))
        if idempotency_key:
            argv.extend(
                (
                    "--idempotency-key",
                    _clean_argv_part(idempotency_key, field="idempotency_key"),
                )
            )
        if scheduled_at:
            argv.extend(
                ("--scheduled-at", _clean_argv_part(scheduled_at, field="scheduled_at"))
            )
        if metadata:
            skills = metadata.get("skills", [])
            if not isinstance(skills, (list, tuple)) or any(
                not isinstance(skill, str) or not skill.strip()
                for skill in skills
            ):
                raise ValueError("metadata.skills must be a list of nonempty strings")
            for skill in skills:
                argv.extend(("--skill", _clean_argv_part(skill, field="skill")))
            goal_mode = metadata.get("goal_mode", False)
            if not isinstance(goal_mode, bool):
                raise ValueError("metadata.goal_mode must be a Boolean")
            if goal_mode:
                argv.append("--goal")
        argv.append("--json")
        payload = self._task_payload(self._run_json(argv))
        task = _task_from_mapping(payload, fallback_title=title)
        # Older CLI versions return only ``task_id`` from create.  Resolve the
        # canonical row when the create response does not describe task state.
        if not any(key in payload for key in ("title", "status", "state", "body")):
            task = self.show_task(task.id)
        if metadata:
            # Skills and goal mode are translated to native create flags above.
            # Keep the rest request-local while the operator database remains
            # the canonical durable metadata store.
            task.raw = {**dict(task.raw), "operator_metadata": dict(metadata)}
        return task

    def show_task(self, task_id: str) -> HermesTask:
        argv = self._kanban_argv("show", task_id)
        argv.append("--json")
        payload = self._task_payload(self._run_json(argv))
        return _task_from_mapping(payload, fallback_id=task_id)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assignee: str | None = None,
        limit: int | None = None,
    ) -> list[HermesTask]:
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        argv = self._kanban_argv("list")
        if status:
            argv.extend(("--status", _clean_argv_part(status, field="status")))
        if assignee:
            argv.extend(("--assignee", _clean_argv_part(assignee, field="assignee")))
        argv.append("--json")
        payload = self._run_json(argv)

        if isinstance(payload, Mapping):
            values = payload.get("tasks", payload.get("items", payload.get("data", [])))
        else:
            values = payload
        if not isinstance(values, list):
            raise AdapterResponseError("Hermes returned an unexpected task list response")
        tasks = [_task_from_mapping(item) for item in values if isinstance(item, Mapping)]
        return tasks if limit is None else tasks[:limit]

    def comment_task(self, task_id: str, comment: str) -> HermesTask:
        self._run_text(self._kanban_argv("comment", task_id, comment))
        return self.show_task(task_id)

    def unblock_task(self, task_id: str) -> HermesTask:
        self._run_text(self._kanban_argv("unblock", task_id))
        return self.show_task(task_id)

    def block_task(self, task_id: str, reason: str) -> HermesTask:
        if not reason.strip():
            raise ValueError("block reason cannot be empty")
        self._run_text(self._kanban_argv("block", task_id, reason))
        return self.show_task(task_id)

    def terminate_task(self, task_id: str) -> HermesTask:
        task = self.show_task(task_id)
        run_id = task.current_run_id or self._active_run_id(task_id)
        if run_id is None:
            state = "_".join(
                str(task.status).strip().lower().replace("-", " ").split()
            )
            safely_inactive = {
                "archived",
                "backlog",
                "blocked",
                "canceled",
                "cancelled",
                "closed",
                "complete",
                "completed",
                "discarded",
                "done",
                "needs_input",
                "open",
                "planned",
                "queued",
                "ready",
                "resolved",
                "stalled",
                "success",
                "todo",
                "triage",
                "waiting",
                "waiting_input",
            }
            if state in safely_inactive:
                return task
            raise AdapterUnavailableError(
                "Hermes reports possible active execution but exposes no native run id"
            )
        if not self.control_base_url or not self.control_token:
            raise AdapterUnavailableError(
                "Hermes run termination needs the authenticated Kanban control API"
            )
        endpoint = (
            f"{self.control_base_url}/api/plugins/kanban/runs/"
            f"{quote(run_id, safe='')}/terminate"
        )
        request = urlrequest.Request(
            endpoint,
            data=b"{}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.control_token}",
                "Content-Type": "application/json",
                "User-Agent": "hermes-operator/0.1",
            },
            method="POST",
        )
        try:
            opener = urlrequest.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.control_timeout_seconds) as response:
                raw = response.read(262_145)
                if len(raw) > 262_144:
                    raise AdapterResponseError(
                        "Hermes terminate response exceeded size limit"
                    )
        except urlerror.HTTPError as exc:
            exc.read(2_048)
            raise AdapterCommandError(
                f"Hermes run termination returned HTTP {exc.code}"
            ) from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise AdapterUnavailableError(
                f"Hermes run termination is unavailable: {exc}"
            ) from exc
        return self.show_task(task_id)

    def _active_run_id(self, task_id: str) -> str | None:
        payload = self._run_json([*self._kanban_argv("runs", task_id), "--json"])
        if isinstance(payload, Mapping):
            values = payload.get("runs", payload.get("data", []))
        else:
            values = payload
        if not isinstance(values, list):
            raise AdapterResponseError("Hermes returned an unexpected run list response")
        terminal = {
            "blocked",
            "cancelled",
            "canceled",
            "completed",
            "crashed",
            "done",
            "failed",
            "reclaimed",
            "spawn_failed",
            "timed_out",
        }
        for value in reversed(values):
            if not isinstance(value, Mapping):
                continue
            run_id = value.get("id") or value.get("run_id")
            state = str(value.get("status") or value.get("outcome") or "").lower()
            ended = value.get("ended_at") or value.get("finished_at")
            if run_id and not ended and state not in terminal:
                return str(run_id)
        return None


class InMemoryHermesAdapter:
    """Thread-safe Hermes adapter for tests and Hermes-free deployments."""

    def __init__(self) -> None:
        self._tasks: dict[str, HermesTask] = {}
        self._idempotency_keys: dict[str, str] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            enabled=True,
            available=True,
            detail="In-memory Hermes adapter is available",
            version="in-memory",
            capabilities=KANBAN_CAPABILITIES,
        )

    def check_capabilities(
        self, required: Sequence[str] | None = None
    ) -> HermesCapabilities:
        required_names = tuple(required or KANBAN_CAPABILITIES)
        available = tuple(name for name in required_names if name in KANBAN_CAPABILITIES)
        missing = tuple(name for name in required_names if name not in KANBAN_CAPABILITIES)
        return HermesCapabilities(
            available=available,
            missing=missing,
            detail="In-memory capability set",
        )

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        priority: int | float | str | None = None,
        assignee: str | None = None,
        parent_id: str | None = None,
        idempotency_key: str | None = None,
        scheduled_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> HermesTask:
        if not title:
            raise ValueError("title cannot be empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotency_keys:
                return self.show_task(self._idempotency_keys[idempotency_key])
            self._counter += 1
            task_id = f"t_{self._counter:06d}"
            task = HermesTask(
                id=task_id,
                title=title,
                status="ready" if assignee and not parent_id else "todo",
                description=description,
                priority=priority,
                assignee=assignee,
                parent_id=parent_id,
                scheduled_at=scheduled_at,
                created_at=now,
                updated_at=now,
                raw={"operator_metadata": dict(metadata or {})},
            )
            self._tasks[task_id] = task
            if idempotency_key:
                self._idempotency_keys[idempotency_key] = task_id
            return copy.deepcopy(task)

    def show_task(self, task_id: str) -> HermesTask:
        with self._lock:
            try:
                return copy.deepcopy(self._tasks[task_id])
            except KeyError as exc:
                raise AdapterResponseError(f"Unknown Hermes task: {task_id}") from exc

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assignee: str | None = None,
        limit: int | None = None,
    ) -> list[HermesTask]:
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        with self._lock:
            tasks = [
                task
                for task in self._tasks.values()
                if (status is None or task.status == status)
                and (assignee is None or task.assignee == assignee)
            ]
            if limit is not None:
                tasks = tasks[:limit]
            return copy.deepcopy(tasks)

    def comment_task(self, task_id: str, comment: str) -> HermesTask:
        if not comment:
            raise ValueError("comment cannot be empty")
        with self._lock:
            task = self._get_mutable(task_id)
            now = datetime.now(timezone.utc).isoformat()
            task.comments.append(
                {"body": comment, "author": "hermes-operator", "created_at": now}
            )
            task.updated_at = now
            return copy.deepcopy(task)

    def unblock_task(self, task_id: str) -> HermesTask:
        with self._lock:
            task = self._get_mutable(task_id)
            task.status = "ready"
            task.updated_at = datetime.now(timezone.utc).isoformat()
            return copy.deepcopy(task)

    def block_task(self, task_id: str, reason: str) -> HermesTask:
        if not reason.strip():
            raise ValueError("block reason cannot be empty")
        with self._lock:
            task = self._get_mutable(task_id)
            task.status = "blocked"
            task.current_run_id = None
            task.updated_at = datetime.now(timezone.utc).isoformat()
            task.raw = {**dict(task.raw), "blocked_reason": reason}
            return copy.deepcopy(task)

    def terminate_task(self, task_id: str) -> HermesTask:
        with self._lock:
            task = self._get_mutable(task_id)
            task.current_run_id = None
            if task.status == "running":
                task.status = "ready"
            task.updated_at = datetime.now(timezone.utc).isoformat()
            return copy.deepcopy(task)

    def _get_mutable(self, task_id: str) -> HermesTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise AdapterResponseError(f"Unknown Hermes task: {task_id}") from exc


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None
