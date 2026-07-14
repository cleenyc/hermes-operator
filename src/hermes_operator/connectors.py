"""Portable read-only command connector boundary for inbound surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .adapters import ObsidianAdapter
from .config import InboundConnectorConfig
from .db import SQLiteStore
from .inbound import normalize_external_event


class ConnectorError(RuntimeError):
    pass


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is not allowed: {value}")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key}")
        value[key] = item
    return value


def _run_bounded_command(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[int, str, str, bool]:
    """Run fixed argv while bounding captured stdout before JSON parsing."""

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=environment,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    output_exceeded = threading.Event()

    def read_pipe(
        stream: BinaryIO,
        chunks: list[bytes],
        *,
        store_limit: int,
        hard_limit: int | None = None,
    ) -> None:
        stored = 0
        observed = 0
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                observed += len(chunk)
                if stored < store_limit:
                    retained = chunk[: store_limit - stored]
                    chunks.append(retained)
                    stored += len(retained)
                if hard_limit is not None and observed > hard_limit:
                    output_exceeded.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        except (OSError, ValueError):
            return

    stdout_thread = threading.Thread(
        target=read_pipe,
        args=(process.stdout, stdout_chunks),
        kwargs={
            "store_limit": max_output_bytes + 1,
            "hard_limit": max_output_bytes,
        },
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_pipe,
        args=(process.stderr, stderr_chunks),
        kwargs={"store_limit": 2_000},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        return_code = process.wait(timeout=5)
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        process.stdout.close()
        process.stderr.close()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise ConnectorError("Connector output streams did not close")
    process.stdout.close()
    process.stderr.close()
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    try:
        stdout = b"".join(stdout_chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConnectorError("Connector output must be UTF-8") from error
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return return_code, stdout, stderr, output_exceeded.is_set()


@dataclass(frozen=True, slots=True)
class ConnectorPollReport:
    name: str
    source: str
    polled: bool
    received: int = 0
    created: int = 0
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class VaultInboxPollReport:
    polled: bool
    received: int = 0
    created: int = 0


class CommandInboundConnector:
    """Poll a deployment-provided read-only connector using fixed argv.

    The command receives its durable cursor in an environment variable and
    returns ``{"cursor": "...", "events": [...]}``. Provider credentials are
    available only when explicitly named in ``pass_env``. Output always enters
    the planner as authenticated but untrusted evidence.
    """

    def __init__(
        self,
        config: InboundConnectorConfig,
        store: SQLiteStore,
        *,
        leadership_guard: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.leadership_guard = leadership_guard or (lambda: None)
        self._lock = threading.Lock()
        self._last_poll = 0.0

    def poll(self, *, force: bool = False) -> ConnectorPollReport:
        if not self.config.enabled:
            return ConnectorPollReport(
                self.config.name,
                self.config.source,
                False,
            )
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._last_poll
                and now - self._last_poll < self.config.interval_seconds
            ):
                return ConnectorPollReport(
                    self.config.name,
                    self.config.source,
                    False,
                )
            self._last_poll = now

        stored_cursor = self.store.get_cursor(self.config.name)
        cursor = stored_cursor[0] if stored_cursor else ""
        safe_environment_names = {
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in safe_environment_names or key.startswith("LC_")
        }
        environment.update(
            {
                key: os.environ[key]
                for key in self.config.pass_env
                if key in os.environ
            }
        )
        environment["HERMES_OPERATOR_CONNECTOR_CURSOR"] = cursor
        environment["HERMES_OPERATOR_CONNECTOR_NAME"] = self.config.name
        environment["HERMES_OPERATOR_CONNECTOR_SOURCE"] = self.config.source
        try:
            return_code, stdout, stderr, output_exceeded = _run_bounded_command(
                self.config.command,
                environment=environment,
                timeout_seconds=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConnectorError(
                f"Connector {self.config.name} failed: {error}"
            ) from error
        if output_exceeded:
            raise ConnectorError(f"Connector {self.config.name} output is too large")
        if return_code != 0:
            detail = stderr.strip()[:2000]
            raise ConnectorError(
                f"Connector {self.config.name} exited {return_code}: {detail}"
            )
        try:
            document = json.loads(
                stdout,
                parse_constant=_reject_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ConnectorError(
                f"Connector {self.config.name} returned invalid JSON"
            ) from error
        if (
            not isinstance(document, dict)
            or not {"cursor", "events"}.issubset(document)
            or set(document) - {
                "cursor",
                "events",
                "metadata",
            }
        ):
            raise ConnectorError(
                f"Connector {self.config.name} returned an invalid envelope"
            )
        events = document.get("events", [])
        if not isinstance(events, list) or len(events) > 500:
            raise ConnectorError("Connector events must be a list of at most 500")
        if not all(isinstance(event, dict) for event in events):
            raise ConnectorError("Every connector event must be an object")
        next_cursor_raw = document.get("cursor", cursor)
        if not isinstance(next_cursor_raw, str) or len(next_cursor_raw) > 16_000:
            raise ConnectorError("Connector cursor must be a bounded string")
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConnectorError("Connector metadata must be an object")

        normalized = []
        for event in events:
            value = normalize_external_event(
                self.config.source,
                event,
                authenticated=True,
                request_id=f"connector:{self.config.name}",
            )
            value.event.provenance.update(
                {
                    "ingress": "command-connector",
                    "connector": self.config.name,
                }
            )
            normalized.append(value)
        created = 0
        with self.store.transaction():
            self.leadership_guard()
            for value in normalized:
                _, was_created = self.store.enqueue_event(
                    value.event,
                    actor=f"connector:{self.config.name}",
                )
                created += int(was_created)
            self.leadership_guard()
            self.store.set_cursor(
                self.config.name,
                next_cursor_raw,
                metadata={
                    **metadata,
                    "source": self.config.source,
                    "received": len(normalized),
                },
            )
            self.store.audit(
                f"connector:{self.config.name}",
                "connector.poll_completed",
                entity_type="connector",
                entity_id=self.config.name,
                data={
                    "source": self.config.source,
                    "received": len(normalized),
                    "created": created,
                },
            )
        return ConnectorPollReport(
            self.config.name,
            self.config.source,
            True,
            received=len(normalized),
            created=created,
            cursor=next_cursor_raw,
        )


class ObsidianInboxReader:
    """Read a bounded, non-recursive vault Inbox into the event queue.

    A configured local path authenticates the surface, not its contents.
    Notes can be changed by sync software or plugins, so every note enters as
    authenticated but untrusted evidence and cannot grant execution authority.
    """

    def __init__(
        self,
        adapter: ObsidianAdapter,
        store: SQLiteStore,
        *,
        operator_root: str,
        leadership_guard: Callable[[], None] | None = None,
        max_documents: int = 100,
        max_bytes: int = 131_072,
    ) -> None:
        if max_documents < 1 or max_bytes < 1:
            raise ValueError("Vault Inbox limits must be positive")
        self.adapter = adapter
        self.store = store
        self.operator_root = operator_root
        self.leadership_guard = leadership_guard or (lambda: None)
        self.max_documents = max_documents
        self.max_bytes = max_bytes

    @property
    def name(self) -> str:
        return "obsidian-inbox"

    def poll(self) -> VaultInboxPollReport:
        if not self.adapter.enabled:
            return VaultInboxPollReport(polled=False)
        relative_directory = Path(self.operator_root) / "Inbox"
        documents = self.adapter.list_documents(
            relative_directory,
            limit=self.max_documents,
            max_bytes=self.max_bytes,
        )
        vault_root = self.adapter.vault_path
        assert vault_root is not None
        events = []
        for document in documents:
            relative_path = document.path.relative_to(vault_root).as_posix()
            content_digest = hashlib.sha256(
                (relative_path + "\0" + document.raw).encode("utf-8")
            ).hexdigest()
            normalized = normalize_external_event(
                "obsidian",
                {
                    "event_type": "vault.note.changed",
                    "external_id": relative_path,
                    "dedupe_key": f"vault-note:{content_digest}",
                    "payload": {
                        "path": relative_path,
                        "frontmatter": dict(document.frontmatter),
                        "body": document.body,
                    },
                },
                authenticated=True,
                request_id="connector:obsidian-inbox",
            )
            normalized.event.provenance.update(
                {
                    "ingress": "obsidian-inbox",
                    "adapter": "obsidian-inbox",
                    "content_digest": content_digest,
                }
            )
            events.append(normalized.event)

        created = 0
        with self.store.transaction():
            self.leadership_guard()
            for event in events:
                _, was_created = self.store.enqueue_event(
                    event,
                    actor="connector:obsidian-inbox",
                )
                created += int(was_created)
            self.leadership_guard()
            self.store.audit(
                "connector:obsidian-inbox",
                "connector.poll_completed",
                entity_type="connector",
                entity_id="obsidian-inbox",
                data={
                    "source": "obsidian",
                    "received": len(events),
                    "created": created,
                },
            )
        return VaultInboxPollReport(
            polled=True,
            received=len(events),
            created=created,
        )


__all__ = [
    "CommandInboundConnector",
    "ConnectorError",
    "ConnectorPollReport",
    "ObsidianInboxReader",
    "VaultInboxPollReport",
]
