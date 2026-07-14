"""Separate, fail-closed executable for approved external side effects.

This module is intentionally not imported by the daemon composition root. The
broker must be deployed under a separate identity with its own connector
credentials. Connector commands are fixed in a deployment-owned TOML file and
receive only the exact action that was bound to a consumed approval grant.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from .approvals import (
    ApprovalError,
    ExternalActionStager,
    OutboundBroker,
    StagedAction,
    _execution_payload,
)
from .db import NotFound, SQLiteStore


class OutboundConfigError(ValueError):
    pass


class OutboundConnectorError(RuntimeError):
    pass


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_INTEGRATION_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PROTECTED_ENVIRONMENT_NAMES = {
    "HERMES_OPERATOR_API_TOKEN",
    "HERMES_OPERATOR_APPROVAL_SECRET",
    "HERMES_OPERATOR_BRIDGE_TOKEN",
    "HERMES_OPERATOR_BRIDGE_PROOF_SECRET",
    "HERMES_KANBAN_CONTROL_TOKEN",
}


def _protected_environment_name(value: str) -> bool:
    return value in _PROTECTED_ENVIRONMENT_NAMES or value.startswith(
        ("HERMES_OPERATOR_", "HERMES_KANBAN_")
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is not allowed: {value}")


def _bounded_integer(value: Any, name: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise OutboundConfigError(
            f"{name} must be an integer from 1 through {maximum}"
        )
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OutboundConfigError(
            f"Unknown {name} configuration keys: {', '.join(unknown)}"
        )


@dataclass(frozen=True, slots=True)
class CommandOutboundConnectorConfig:
    integration: str
    command: tuple[str, ...]
    pass_env: tuple[str, ...] = ()
    timeout_seconds: int = 60
    max_input_bytes: int = 1_048_576
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not isinstance(self.integration, str) or not _INTEGRATION_NAME.fullmatch(
            self.integration
        ):
            raise OutboundConfigError("Outbound connector integration is invalid")
        if (
            not isinstance(self.command, tuple)
            or not 1 <= len(self.command) <= 64
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 16_384
                or "\x00" in item
                for item in self.command
            )
        ):
            raise OutboundConfigError(
                "Outbound connector command must be fixed argv"
            )
        if (
            not isinstance(self.pass_env, tuple)
            or len(self.pass_env) > 64
            or len(self.pass_env) != len(set(self.pass_env))
            or any(
                not isinstance(item, str) or not _ENVIRONMENT_NAME.fullmatch(item)
                for item in self.pass_env
            )
            or any(_protected_environment_name(item) for item in self.pass_env)
        ):
            raise OutboundConfigError(
                "Outbound connector environment allowlist is invalid"
            )
        _bounded_integer(
            self.timeout_seconds,
            "Outbound connector timeout_seconds",
            maximum=3_600,
        )
        _bounded_integer(
            self.max_input_bytes,
            "Outbound connector max_input_bytes",
            maximum=16_777_216,
        )
        _bounded_integer(
            self.max_output_bytes,
            "Outbound connector max_output_bytes",
            maximum=16_777_216,
        )


@dataclass(frozen=True, slots=True)
class OutboundBrokerConfig:
    enabled: bool
    database_path: Path
    actor: str
    max_grant_lifetime_seconds: int
    connectors: tuple[CommandOutboundConnectorConfig, ...]


def load_outbound_config(path: str | Path) -> OutboundBrokerConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        if config_path.stat().st_size > 1_048_576:
            raise OutboundConfigError("Broker configuration exceeds 1 MiB")
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OutboundConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise OutboundConfigError(f"Cannot read broker configuration: {error}") from error
    if not isinstance(raw, dict):
        raise OutboundConfigError("Broker configuration must be a TOML table")
    _only_keys(raw, {"broker", "connectors"}, "top-level")
    broker_raw = raw.get("broker", {})
    if not isinstance(broker_raw, dict):
        raise OutboundConfigError("broker must be a table")
    _only_keys(
        broker_raw,
        {
            "enabled",
            "database_path",
            "actor",
            "max_grant_lifetime_seconds",
        },
        "broker",
    )
    enabled = broker_raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise OutboundConfigError("broker.enabled must be true or false")
    database_raw = broker_raw.get("database_path")
    if not isinstance(database_raw, str) or not database_raw.strip():
        raise OutboundConfigError("broker.database_path must be a non-empty path")
    database_path = Path(database_raw).expanduser()
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    database_path = database_path.resolve()
    actor = broker_raw.get("actor", "outbound-broker")
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or len(actor) > 128
        or "\x00" in actor
    ):
        raise OutboundConfigError("broker.actor must be a bounded non-empty string")
    lifetime = _bounded_integer(
        broker_raw.get("max_grant_lifetime_seconds", 3600),
        "broker.max_grant_lifetime_seconds",
        maximum=86_400,
    )

    connectors_raw = raw.get("connectors", [])
    if not isinstance(connectors_raw, list) or len(connectors_raw) > 64:
        raise OutboundConfigError(
            "connectors must be an array of at most 64 tables"
        )
    connectors: list[CommandOutboundConnectorConfig] = []
    integrations: set[str] = set()
    for index, connector_raw in enumerate(connectors_raw):
        name = f"connectors[{index}]"
        if not isinstance(connector_raw, dict):
            raise OutboundConfigError(f"{name} must be a table")
        _only_keys(
            connector_raw,
            {
                "integration",
                "command",
                "pass_env",
                "timeout_seconds",
                "max_input_bytes",
                "max_output_bytes",
            },
            name,
        )
        integration = connector_raw.get("integration")
        if not isinstance(integration, str) or not _INTEGRATION_NAME.fullmatch(
            integration
        ):
            raise OutboundConfigError(f"{name}.integration is invalid")
        if integration in integrations:
            raise OutboundConfigError(
                f"Duplicate outbound integration: {integration}"
            )
        integrations.add(integration)
        command_raw = connector_raw.get("command")
        if (
            not isinstance(command_raw, list)
            or not 1 <= len(command_raw) <= 64
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 16_384
                or "\x00" in item
                for item in command_raw
            )
        ):
            raise OutboundConfigError(
                f"{name}.command must be a fixed argv list of 1 to 64 strings"
            )
        pass_env_raw = connector_raw.get("pass_env", [])
        if not isinstance(pass_env_raw, list) or len(pass_env_raw) > 64 or any(
            not isinstance(item, str) or not _ENVIRONMENT_NAME.fullmatch(item)
            for item in pass_env_raw
        ):
            raise OutboundConfigError(
                f"{name}.pass_env must contain valid environment names"
            )
        if len(pass_env_raw) != len(set(pass_env_raw)):
            raise OutboundConfigError(f"{name}.pass_env contains duplicates")
        protected = sorted(
            item for item in pass_env_raw if _protected_environment_name(item)
        )
        if protected:
            raise OutboundConfigError(
                f"{name}.pass_env cannot include operator control-plane secrets"
            )
        connectors.append(
            CommandOutboundConnectorConfig(
                integration=integration,
                command=tuple(command_raw),
                pass_env=tuple(pass_env_raw),
                timeout_seconds=_bounded_integer(
                    connector_raw.get("timeout_seconds", 60),
                    f"{name}.timeout_seconds",
                    maximum=3_600,
                ),
                max_input_bytes=_bounded_integer(
                    connector_raw.get("max_input_bytes", 1_048_576),
                    f"{name}.max_input_bytes",
                    maximum=16_777_216,
                ),
                max_output_bytes=_bounded_integer(
                    connector_raw.get("max_output_bytes", 1_048_576),
                    f"{name}.max_output_bytes",
                    maximum=16_777_216,
                ),
            )
        )
    if enabled and not connectors:
        raise OutboundConfigError(
            "An enabled broker must configure at least one connector"
        )
    return OutboundBrokerConfig(
        enabled=enabled,
        database_path=database_path,
        actor=actor.strip(),
        max_grant_lifetime_seconds=lifetime,
        connectors=tuple(connectors),
    )


def _minimal_environment(pass_env: Sequence[str]) -> dict[str, str]:
    environment = {"PATH": os.defpath}
    safe_names = {
        "LANG",
        "LC_ALL",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    }
    environment.update(
        {
            key: value
            for key, value in os.environ.items()
            if key in safe_names or key.startswith("LC_")
        }
    )
    environment.update(
        {key: os.environ[key] for key in pass_env if key in os.environ}
    )
    return environment


def _run_fixed_command(
    command: Sequence[str],
    *,
    payload: bytes,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[int, bytes, str, bool]:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    output_exceeded = threading.Event()

    def write_input() -> None:
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def read_pipe(
        stream: BinaryIO,
        chunks: list[bytes],
        *,
        store_limit: int,
        hard_limit: int | None = None,
    ) -> None:
        observed = 0
        stored = 0
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

    writer = threading.Thread(target=write_input, daemon=True)
    stdout_reader = threading.Thread(
        target=read_pipe,
        args=(process.stdout, stdout_chunks),
        kwargs={
            "store_limit": max_output_bytes + 1,
            "hard_limit": max_output_bytes,
        },
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=read_pipe,
        args=(process.stderr, stderr_chunks),
        kwargs={"store_limit": 2_000},
        daemon=True,
    )
    writer.start()
    stdout_reader.start()
    stderr_reader.start()
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=5)
        raise OutboundConnectorError("Outbound connector timed out") from error
    finally:
        writer.join(timeout=5)
        stdout_reader.join(timeout=5)
        stderr_reader.join(timeout=5)
    if writer.is_alive() or stdout_reader.is_alive() or stderr_reader.is_alive():
        process.kill()
        raise OutboundConnectorError("Outbound connector streams did not close")
    process.stdout.close()
    process.stderr.close()
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return return_code, b"".join(stdout_chunks), stderr, output_exceeded.is_set()


class CommandOutboundConnector:
    """Execute one exact approved action through a deployment-fixed command."""

    def __init__(self, config: CommandOutboundConnectorConfig) -> None:
        self.config = config

    def execute(self, action: StagedAction) -> dict[str, Any]:
        if action.intent.integration != self.config.integration:
            raise OutboundConnectorError(
                "Approved action integration does not match connector"
            )
        envelope = {
            "schema_version": 1,
            "action_id": action.id,
            "intent_digest": action.intent.digest,
            "recipients_digest": action.intent.recipients_digest,
            "content_digest": action.intent.content_digest,
            "action": _execution_payload(action.intent),
        }
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > self.config.max_input_bytes:
            raise OutboundConnectorError("Approved action exceeds connector input limit")
        try:
            return_code, stdout, stderr, exceeded = _run_fixed_command(
                self.config.command,
                payload=payload,
                environment=_minimal_environment(self.config.pass_env),
                timeout_seconds=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )
        except OSError as error:
            raise OutboundConnectorError(
                f"Cannot start outbound connector: {error}"
            ) from error
        if exceeded:
            raise OutboundConnectorError("Outbound connector output exceeds limit")
        if return_code != 0:
            raise OutboundConnectorError(
                f"Outbound connector exited {return_code}: {stderr.strip()[:2000]}"
            )
        try:
            output_text = stdout.decode("utf-8")
            result = json.loads(
                output_text,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            raise OutboundConnectorError(
                "Outbound connector returned invalid JSON"
            ) from error
        if not isinstance(result, dict):
            raise OutboundConnectorError(
                "Outbound connector result must be a JSON object"
            )
        if result.get("ok") is not True:
            detail = str(result.get("error", "connector reported failure"))[:1000]
            raise OutboundConnectorError(
                f"Outbound connector did not confirm success: {detail}"
            )
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-outbound-broker",
        description="Execute one exact, approved external action",
    )
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("action_id")
    execute.add_argument("--grant-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_outbound_config(arguments.config)
    except OutboundConfigError as error:
        print(f"broker configuration error: {error}", file=sys.stderr)
        return 2
    if not config.enabled:
        print("outbound execution is disabled by broker configuration", file=sys.stderr)
        return 3
    if not config.database_path.is_file():
        print("operator database does not exist", file=sys.stderr)
        return 2
    store = SQLiteStore(config.database_path)
    stager = ExternalActionStager(
        store,
        ttl_seconds=config.max_grant_lifetime_seconds,
    )
    connectors = {
        item.integration: CommandOutboundConnector(item)
        for item in config.connectors
    }
    broker = OutboundBroker(
        stager,
        connectors=connectors,
        enabled=True,
        max_grant_lifetime_seconds=config.max_grant_lifetime_seconds,
    )
    try:
        result = broker.execute(
            arguments.action_id,
            grant_id=arguments.grant_id,
            actor=config.actor,
        )
    except (
        ApprovalError,
        NotFound,
        OutboundConnectorError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"outbound execution failed: {error}", file=sys.stderr)
        return 4
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
