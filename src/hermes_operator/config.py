from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_ENV_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.match(value)
    if match:
        name, default = match.groups()
        return os.environ.get(name, default or "")
    return os.path.expandvars(value)


def _resolve_path(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(slots=True)
class OperatorConfig:
    instance_id: str = "default"
    database_path: Path = Path("data/operator.db")
    data_dir: Path = Path("data")
    tick_seconds: float = 30.0
    reconciliation_seconds: float = 300.0
    reasoning_refresh_seconds: float = 3600.0
    max_events_per_pass: int = 25
    max_parallel_work: int = 4
    max_authorizations_per_pass: int = 40
    autonomy_mode: str = "shadow"
    timezone: str = "UTC"
    event_lease_seconds: int = 300
    event_max_attempts: int = 5


@dataclass(slots=True)
class LLMConfig:
    provider: str = "openai_compatible"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: int = 180
    temperature: float = 0.1
    max_output_tokens: int = 8000
    command: list[str] = field(default_factory=list)
    pass_env: list[str] = field(default_factory=list)

    def resolved_api_key(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")


@dataclass(slots=True)
class HermesConfig:
    enabled: bool = False
    binary: str | list[str] = "hermes"
    profile: str = "operator"
    board: str = "default"
    default_assignee: str = "operator"
    orchestrator_profile: str = "operator"
    command_timeout_seconds: int = 120
    goal_mode: bool = False
    default_skills: list[str] = field(default_factory=list)
    allowed_profiles: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    pass_env: list[str] = field(default_factory=list)
    dispatch_authorization_ttl_seconds: int = 86400
    max_execution_attempts: int = 3
    control_base_url: str = ""
    control_token: str = ""
    control_token_env: str = "HERMES_KANBAN_CONTROL_TOKEN"
    control_timeout_seconds: int = 10
    require_policy_attestation: bool = True
    policy_attestation_ttl_seconds: int = 300
    allowed_plugin_versions: list[str] = field(default_factory=lambda: ["1.4.0"])
    allowed_policy_versions: list[str] = field(default_factory=lambda: ["5.0.0"])
    allowed_policy_digests: list[str] = field(default_factory=list)

    def resolved_control_token(self) -> str:
        return self.control_token or os.environ.get(self.control_token_env, "")


@dataclass(slots=True)
class ObsidianConfig:
    enabled: bool = False
    vault_path: Path | None = None
    discover: bool = True
    operator_root: str = "Hermes Operator"
    write_mode: str = "projection"


@dataclass(slots=True)
class NativeAutomationConfig:
    """Desired Hermes-native skills and cron jobs.

    Installation is explicit because delivery targets and OAuth belong to the
    Hermes deployment. The generated plan remains portable until those values
    are supplied.
    """

    enabled: bool = False
    delivery: str = "local"
    google_intake_enabled: bool = True
    google_intake_schedule: str = "every 10m"
    reminder_delivery_enabled: bool = True
    reminder_schedule: str = "every 15m"
    attention_redelivery_seconds: int = 3600
    briefing_enabled: bool = True
    briefing_schedule: str = "0 8 * * *"
    attach_to_session: bool = True
    google_skill: str = "google-workspace"
    obsidian_skill: str = "obsidian"


@dataclass(slots=True)
class ServerConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    api_token: str = ""
    api_token_env: str = "HERMES_OPERATOR_API_TOKEN"
    bridge_token: str = ""
    bridge_token_env: str = "HERMES_OPERATOR_BRIDGE_TOKEN"
    max_body_bytes: int = 1_048_576
    webhook_secrets: dict[str, str] = field(default_factory=dict)
    allow_unsigned_webhooks: bool = False

    def resolved_api_token(self) -> str:
        return self.api_token or os.environ.get(self.api_token_env, "")

    def resolved_bridge_token(self) -> str:
        return self.bridge_token or os.environ.get(self.bridge_token_env, "")


@dataclass(slots=True)
class PolicyConfig:
    external_actions_require_approval: bool = True
    approval_ttl_seconds: int = 3600
    approval_secret_env: str = "HERMES_OPERATOR_APPROVAL_SECRET"
    trusted_event_sources: list[str] = field(default_factory=lambda: ["operator", "system"])
    allow_memory_auto_promotion: bool = False
    max_llm_priority_adjustment: float = 10.0
    external_action_mode: str = "stage_only"


@dataclass(slots=True)
class VerificationCheckConfig:
    """One deployment-approved deterministic completion check."""

    name: str
    command: list[str]
    cwd: Path | None = None
    timeout_seconds: int = 120
    max_output_bytes: int = 1_048_576
    pass_env: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerificationConfig:
    """Bounds for artifact inspection and deterministic completion checks."""

    enabled: bool = True
    artifact_roots: dict[str, Path] = field(default_factory=dict)
    max_artifacts: int = 64
    max_files_per_directory: int = 2_000
    max_artifact_bytes: int = 268_435_456
    max_total_artifact_bytes: int = 536_870_912
    checks: list[VerificationCheckConfig] = field(default_factory=list)


@dataclass(slots=True)
class InboundConnectorConfig:
    name: str
    source: str
    command: list[str]
    enabled: bool = True
    interval_seconds: float = 60.0
    timeout_seconds: int = 60
    pass_env: list[str] = field(default_factory=list)
    max_output_bytes: int = 4_194_304


@dataclass(slots=True)
class AppConfig:
    config_path: Path
    operator: OperatorConfig
    llm: LLMConfig
    hermes: HermesConfig
    obsidian: ObsidianConfig
    server: ServerConfig
    policy: PolicyConfig
    native_automation: NativeAutomationConfig = field(
        default_factory=NativeAutomationConfig
    )
    inbound_connectors: list[InboundConnectorConfig] = field(default_factory=list)
    verification: VerificationConfig = field(default_factory=VerificationConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    configured = path or os.environ.get("HERMES_OPERATOR_CONFIG", "operator.toml")
    config_path = Path(configured).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("rb") as handle:
        raw = _expand_env(tomllib.load(handle))

    base = config_path.parent
    operator_raw = raw.get("operator", {})
    llm_raw = raw.get("llm", {})
    hermes_raw = raw.get("hermes", {})
    obsidian_raw = raw.get("obsidian", {})
    server_raw = raw.get("server", {})
    policy_raw = raw.get("policy", {})
    native_automation_raw = raw.get("native_automation", {})
    verification_raw = raw.get("verification", {})
    if not isinstance(native_automation_raw, dict):
        raise ValueError("native_automation must be a table")
    connectors_raw = raw.get("inbound_connectors", [])
    if not isinstance(connectors_raw, list):
        raise ValueError("inbound_connectors must be an array of tables")

    operator = OperatorConfig(
        **{
            **operator_raw,
            "database_path": _resolve_path(operator_raw.get("database_path", "data/operator.db"), base),
            "data_dir": _resolve_path(operator_raw.get("data_dir", "data"), base),
        }
    )
    obsidian = ObsidianConfig(
        **{
            **obsidian_raw,
            "vault_path": _resolve_path(obsidian_raw.get("vault_path"), base),
        }
    )
    server = ServerConfig(
        **{
            **server_raw,
            "webhook_secrets": dict(server_raw.get("webhook_secrets", {})),
        }
    )
    if not isinstance(verification_raw, dict):
        raise ValueError("verification must be a table")
    verification_roots_raw = verification_raw.get("artifact_roots", {})
    if not isinstance(verification_roots_raw, dict):
        raise ValueError("verification.artifact_roots must be a table")
    if any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or not value.strip()
        for name, value in verification_roots_raw.items()
    ):
        raise ValueError("verification artifact roots must map names to paths")
    verification_checks_raw = verification_raw.get("checks", [])
    if not isinstance(verification_checks_raw, list):
        raise ValueError("verification.checks must be an array of tables")
    verification = VerificationConfig(
        **{
            **{
                key: value
                for key, value in verification_raw.items()
                if key not in {"artifact_roots", "checks"}
            },
            "artifact_roots": {
                str(name): _resolve_path(str(value), base)
                for name, value in verification_roots_raw.items()
            },
            "checks": [
                VerificationCheckConfig(
                    **{
                        **value,
                        "cwd": _resolve_path(value.get("cwd"), base),
                    }
                )
                for value in verification_checks_raw
                if isinstance(value, dict)
            ],
        }
    )
    if len(verification.checks) != len(verification_checks_raw):
        raise ValueError("Every verification check must be a table")
    config = AppConfig(
        config_path=config_path,
        operator=operator,
        llm=LLMConfig(**llm_raw),
        hermes=HermesConfig(**hermes_raw),
        obsidian=obsidian,
        server=server,
        policy=PolicyConfig(**policy_raw),
        native_automation=NativeAutomationConfig(**native_automation_raw),
        verification=verification,
        inbound_connectors=[
            InboundConnectorConfig(**value)
            for value in connectors_raw
        ],
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    def finite_number(value: Any, field_name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"{field_name} must be a finite number")
        return float(value)

    def positive_integer(value: Any, field_name: str, *, minimum: int = 1) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{field_name} must be an integer of at least {minimum}")
        return value

    def env_names(values: Any, field_name: str) -> set[str]:
        if not isinstance(values, list) or any(
            not isinstance(value, str)
            or _ENV_NAME_PATTERN.fullmatch(value) is None
            for value in values
        ):
            raise ValueError(f"{field_name} must contain valid environment variable names")
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name} cannot contain duplicates")
        return set(values)

    if config.operator.autonomy_mode not in {"shadow", "internal", "active"}:
        raise ValueError("operator.autonomy_mode must be shadow, internal, or active")
    if config.policy.external_action_mode not in {"disabled", "stage_only", "approved"}:
        raise ValueError("policy.external_action_mode must be disabled, stage_only, or approved")
    positive_integer(config.operator.max_events_per_pass, "operator.max_events_per_pass")
    positive_integer(config.operator.max_parallel_work, "operator.max_parallel_work")
    positive_integer(
        config.operator.max_authorizations_per_pass,
        "operator.max_authorizations_per_pass",
    )
    if config.operator.max_parallel_work > 64:
        raise ValueError("operator.max_parallel_work cannot exceed 64")
    if config.operator.max_authorizations_per_pass > 80:
        raise ValueError("operator.max_authorizations_per_pass cannot exceed 80")
    tick_seconds = finite_number(config.operator.tick_seconds, "operator.tick_seconds")
    reconciliation_seconds = finite_number(
        config.operator.reconciliation_seconds,
        "operator.reconciliation_seconds",
    )
    reasoning_refresh_seconds = finite_number(
        config.operator.reasoning_refresh_seconds,
        "operator.reasoning_refresh_seconds",
    )
    if tick_seconds <= 0 or reconciliation_seconds <= 0 or reasoning_refresh_seconds <= 0:
        raise ValueError("operator timing values must be positive")
    if reasoning_refresh_seconds < reconciliation_seconds:
        raise ValueError(
            "operator.reasoning_refresh_seconds cannot be shorter than reconciliation_seconds"
        )
    positive_integer(config.operator.event_lease_seconds, "operator.event_lease_seconds")
    positive_integer(config.operator.event_max_attempts, "operator.event_max_attempts")
    positive_integer(config.llm.timeout_seconds, "llm.timeout_seconds")
    if config.operator.event_lease_seconds <= config.llm.timeout_seconds + 30:
        raise ValueError(
            "operator.event_lease_seconds must exceed llm.timeout_seconds by more than 30 seconds"
        )
    if config.llm.provider not in {"openai_compatible", "command"}:
        raise ValueError("llm.provider must be openai_compatible or command")
    if config.llm.provider == "openai_compatible":
        if not isinstance(config.llm.base_url, str):
            raise ValueError("llm.base_url must be a string")
        parsed_llm_url = urlsplit(config.llm.base_url)
        if (
            parsed_llm_url.scheme not in {"http", "https"}
            or not parsed_llm_url.hostname
            or parsed_llm_url.username
            or parsed_llm_url.password
            or parsed_llm_url.query
            or parsed_llm_url.fragment
        ):
            raise ValueError(
                "llm.base_url must be an http(s) URL without credentials, query, or fragment"
            )
        if (
            parsed_llm_url.scheme == "http"
            and parsed_llm_url.hostname not in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError(
                "llm.base_url must use HTTPS unless it targets loopback"
            )
        if (
            not isinstance(config.llm.api_key_env, str)
            or _ENV_NAME_PATTERN.fullmatch(config.llm.api_key_env) is None
        ):
            raise ValueError("llm.api_key_env must be an environment variable name")
    positive_integer(config.llm.max_output_tokens, "llm.max_output_tokens")
    temperature = finite_number(config.llm.temperature, "llm.temperature")
    if not 0 <= temperature <= 2:
        raise ValueError("llm.temperature must be between 0 and 2")
    positive_integer(
        config.hermes.command_timeout_seconds,
        "hermes.command_timeout_seconds",
    )
    if isinstance(config.hermes.binary, list):
        if not config.hermes.binary or not all(
            isinstance(part, str) and part for part in config.hermes.binary
        ):
            raise ValueError("hermes.binary argv must contain nonempty strings")
    elif not isinstance(config.hermes.binary, str) or not config.hermes.binary:
        raise ValueError("hermes.binary must be an executable or argv list")
    positive_integer(
        config.hermes.dispatch_authorization_ttl_seconds,
        "hermes.dispatch_authorization_ttl_seconds",
        minimum=60,
    )
    positive_integer(
        config.hermes.max_execution_attempts,
        "hermes.max_execution_attempts",
    )
    if config.hermes.max_execution_attempts > 10:
        raise ValueError("hermes.max_execution_attempts cannot exceed 10")
    positive_integer(
        config.hermes.control_timeout_seconds,
        "hermes.control_timeout_seconds",
    )
    if not isinstance(config.hermes.control_base_url, str):
        raise ValueError("hermes.control_base_url must be a string")
    if not isinstance(config.hermes.control_token, str):
        raise ValueError("hermes.control_token must be a string")
    if (
        not isinstance(config.hermes.control_token_env, str)
        or _ENV_NAME_PATTERN.fullmatch(config.hermes.control_token_env) is None
    ):
        raise ValueError("hermes.control_token_env must be an environment variable name")
    if config.hermes.control_base_url:
        parsed_control_url = urlsplit(config.hermes.control_base_url)
        if (
            parsed_control_url.scheme not in {"http", "https"}
            or not parsed_control_url.hostname
            or parsed_control_url.username
            or parsed_control_url.password
            or parsed_control_url.query
            or parsed_control_url.fragment
        ):
            raise ValueError(
                "hermes.control_base_url must be an http(s) origin without credentials, query, or fragment"
            )
        if (
            parsed_control_url.scheme == "http"
            and parsed_control_url.hostname not in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError(
                "hermes.control_base_url must use HTTPS unless it targets loopback"
            )
    if (
        config.hermes.enabled
        and config.operator.autonomy_mode in {"internal", "active"}
        and (
            not config.hermes.control_base_url
            or not config.hermes.resolved_control_token()
        )
    ):
        raise ValueError(
            "Active Hermes execution requires the authenticated Kanban run-control API"
        )
    if (
        config.hermes.enabled
        and config.operator.autonomy_mode in {"internal", "active"}
        and not config.hermes.require_policy_attestation
    ):
        raise ValueError(
            "Internal or active Hermes execution requires native policy attestation"
        )
    positive_integer(
        config.hermes.policy_attestation_ttl_seconds,
        "hermes.policy_attestation_ttl_seconds",
        minimum=60,
    )
    for values, field_name in (
        (config.hermes.default_skills, "hermes.default_skills"),
        (config.hermes.allowed_profiles, "hermes.allowed_profiles"),
        (config.hermes.allowed_skills, "hermes.allowed_skills"),
        (config.hermes.allowed_plugin_versions, "hermes.allowed_plugin_versions"),
        (config.hermes.allowed_policy_versions, "hermes.allowed_policy_versions"),
        (config.hermes.allowed_policy_digests, "hermes.allowed_policy_digests"),
    ):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"{field_name} must be a list of nonempty strings")
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name} cannot contain duplicates")
    if config.hermes.enabled and config.hermes.require_policy_attestation:
        if not config.server.enabled:
            raise ValueError(
                "Hermes policy attestation requires the HTTP bridge server"
            )
        if not config.server.resolved_bridge_token():
            raise ValueError(
                "Hermes policy attestation requires a configured bridge token"
            )
        if not (
            config.hermes.allowed_plugin_versions
            and config.hermes.allowed_policy_versions
            and config.hermes.allowed_policy_digests
        ):
            raise ValueError(
                "Hermes policy attestation requires allowed versions and digests"
            )
        if any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in config.hermes.allowed_policy_digests
        ):
            raise ValueError("Hermes policy digests must be lowercase SHA-256 hex")
        configured_profiles = [
            value
            for value in (
                config.hermes.profile,
                config.hermes.default_assignee,
                config.hermes.orchestrator_profile,
                *config.hermes.allowed_profiles,
            )
            if value
        ]
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None
            for value in configured_profiles
        ):
            raise ValueError("Hermes profile names must be bounded safe identifiers")
    if config.obsidian.write_mode != "projection":
        raise ValueError("obsidian.write_mode must be projection")
    native = config.native_automation
    if not isinstance(native.enabled, bool):
        raise ValueError("native_automation.enabled must be a Boolean")
    if not isinstance(native.attach_to_session, bool):
        raise ValueError("native_automation.attach_to_session must be a Boolean")
    positive_integer(
        native.attention_redelivery_seconds,
        "native_automation.attention_redelivery_seconds",
    )
    for value, field_name in (
        (native.delivery, "native_automation.delivery"),
        (native.google_intake_schedule, "native_automation.google_intake_schedule"),
        (native.reminder_schedule, "native_automation.reminder_schedule"),
        (native.briefing_schedule, "native_automation.briefing_schedule"),
        (native.google_skill, "native_automation.google_skill"),
        (native.obsidian_skill, "native_automation.obsidian_skill"),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f"{field_name} must be a nonempty bounded string")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"{field_name} cannot contain control characters")
    if native.enabled and not config.hermes.enabled:
        raise ValueError("Hermes must be enabled for native automation")
    positive_integer(config.server.port, "server.port")
    if not 1 <= config.server.port <= 65535:
        raise ValueError("server.port is invalid")
    positive_integer(config.server.max_body_bytes, "server.max_body_bytes")
    reserved_webhook_sources = {"operator", "system", "hermes"}.intersection(
        config.server.webhook_secrets
    )
    if reserved_webhook_sources:
        raise ValueError(
            "Reserved webhook sources cannot have HMAC secrets: "
            + ", ".join(sorted(reserved_webhook_sources))
        )
    if (
        config.server.resolved_api_token()
        and config.server.resolved_bridge_token()
        and config.server.resolved_api_token()
        == config.server.resolved_bridge_token()
    ):
        raise ValueError("server API and Hermes bridge tokens must be distinct")
    if (
        config.server.enabled
        and config.server.host not in {"127.0.0.1", "::1", "localhost"}
        and not config.server.resolved_api_token()
    ):
        raise ValueError("A server API token is required when binding beyond loopback")
    if not config.policy.external_actions_require_approval:
        raise ValueError("External actions must require exact operator approval")
    positive_integer(config.policy.approval_ttl_seconds, "policy.approval_ttl_seconds")
    max_priority_adjustment = finite_number(
        config.policy.max_llm_priority_adjustment,
        "policy.max_llm_priority_adjustment",
    )
    if max_priority_adjustment < 0:
        raise ValueError("policy.max_llm_priority_adjustment cannot be negative")
    protected_env = {
        config.server.api_token_env,
        config.server.bridge_token_env,
        config.policy.approval_secret_env,
        config.hermes.control_token_env,
    } - {""}
    if not isinstance(config.verification.enabled, bool):
        raise ValueError("verification.enabled must be a Boolean")
    positive_integer(config.verification.max_artifacts, "verification.max_artifacts")
    positive_integer(
        config.verification.max_files_per_directory,
        "verification.max_files_per_directory",
    )
    positive_integer(
        config.verification.max_artifact_bytes,
        "verification.max_artifact_bytes",
    )
    positive_integer(
        config.verification.max_total_artifact_bytes,
        "verification.max_total_artifact_bytes",
    )
    if config.verification.max_artifacts > 1_000:
        raise ValueError("verification.max_artifacts cannot exceed 1000")
    if config.verification.max_files_per_directory > 100_000:
        raise ValueError("verification.max_files_per_directory cannot exceed 100000")
    if (
        config.verification.max_artifact_bytes
        > config.verification.max_total_artifact_bytes
    ):
        raise ValueError(
            "verification.max_artifact_bytes cannot exceed max_total_artifact_bytes"
        )
    if any(
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None
        or not isinstance(path, Path)
        for name, path in config.verification.artifact_roots.items()
    ):
        raise ValueError("verification artifact roots need stable names and paths")
    verification_check_names: set[str] = set()
    for check in config.verification.checks:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", check.name) is None:
            raise ValueError("Verification check names must be stable identifiers")
        if check.name in verification_check_names:
            raise ValueError(f"Duplicate verification check name: {check.name}")
        verification_check_names.add(check.name)
        if not check.command or not all(
            isinstance(value, str) and value and "\x00" not in value
            for value in check.command
        ):
            raise ValueError("Verification checks need fixed nonempty argv elements")
        positive_integer(
            check.timeout_seconds,
            f"verification.checks.{check.name}.timeout_seconds",
        )
        if check.timeout_seconds > 3_600:
            raise ValueError("Verification check timeout cannot exceed 3600 seconds")
        positive_integer(
            check.max_output_bytes,
            f"verification.checks.{check.name}.max_output_bytes",
        )
        if check.max_output_bytes > 16_777_216:
            raise ValueError("Verification check output cannot exceed 16777216 bytes")
        check_env = env_names(
            check.pass_env,
            f"verification.checks.{check.name}.pass_env",
        )
        if protected_env.intersection(check_env):
            raise ValueError(
                "Verification checks cannot receive operator, bridge, or approval secrets"
            )
    connector_names: set[str] = set()
    reserved_connector_sources = {"operator", "system", "hermes"}
    for value, field_name in (
        (config.server.api_token_env, "server.api_token_env"),
        (config.server.bridge_token_env, "server.bridge_token_env"),
        (config.policy.approval_secret_env, "policy.approval_secret_env"),
    ):
        if not isinstance(value, str) or _ENV_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be an environment variable name")
    llm_env = env_names(config.llm.pass_env, "llm.pass_env")
    hermes_env = env_names(config.hermes.pass_env, "hermes.pass_env")
    if protected_env.intersection(llm_env):
        raise ValueError(
            "The LLM command cannot receive operator, bridge, or approval secrets"
        )
    if protected_env.intersection(hermes_env):
        raise ValueError(
            "Hermes cannot receive operator, bridge, or approval secrets"
        )
    for connector in config.inbound_connectors:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", connector.name):
            raise ValueError("Inbound connector names must be stable identifiers")
        if connector.name in connector_names:
            raise ValueError(f"Duplicate inbound connector name: {connector.name}")
        connector_names.add(connector.name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", connector.source):
            raise ValueError("Inbound connector sources must be lowercase identifiers")
        if connector.source in reserved_connector_sources:
            raise ValueError("Inbound connectors cannot claim a reserved source")
        if not connector.command or not all(
            isinstance(value, str) and value for value in connector.command
        ):
            raise ValueError("Inbound connector commands need fixed argv elements")
        interval_seconds = finite_number(
            connector.interval_seconds,
            f"inbound_connectors.{connector.name}.interval_seconds",
        )
        if interval_seconds <= 0:
            raise ValueError("Inbound connector timing values must be positive")
        positive_integer(
            connector.timeout_seconds,
            f"inbound_connectors.{connector.name}.timeout_seconds",
        )
        positive_integer(
            connector.max_output_bytes,
            f"inbound_connectors.{connector.name}.max_output_bytes",
        )
        connector_env = env_names(
            connector.pass_env,
            f"inbound_connectors.{connector.name}.pass_env",
        )
        if protected_env.intersection(connector_env):
            raise ValueError(
                "Inbound connectors cannot receive operator, bridge, or approval secrets"
            )
