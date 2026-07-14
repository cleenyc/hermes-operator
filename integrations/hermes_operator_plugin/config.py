"""Environment based configuration for the portable Hermes bridge."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """Raised when plugin configuration is unsafe or malformed."""


DEFAULT_ATTEST_INTERVAL_SECONDS = 120.0
MIN_ATTEST_INTERVAL_SECONDS = 120.0
MAX_ATTEST_INTERVAL_SECONDS = 240.0


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError("HERMES_OPERATOR_URL must use http or https")
    if not parsed.hostname:
        raise ConfigurationError("HERMES_OPERATOR_URL must include a host")
    if parsed.username or parsed.password:
        raise ConfigurationError("HERMES_OPERATOR_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("HERMES_OPERATOR_URL must not contain query or fragment data")
    if (
        parsed.scheme == "http"
        and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ConfigurationError(
            "HERMES_OPERATOR_URL must use HTTPS unless it targets loopback"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True, slots=True)
class PluginConfig:
    """Runtime settings with no dependency on a Hermes installation path."""

    base_url: str
    api_token: str | None
    profile: str
    timeout_seconds: float
    inject_context: bool
    emit_lifecycle: bool
    proof_secret: str | None = None
    credentials_scrubbed: bool = False
    reviewed_host_override: bool = False
    attestation_refresh_seconds: float = DEFAULT_ATTEST_INTERVAL_SECONDS
    max_response_bytes: int = 262_144

    @classmethod
    def from_env(cls) -> "PluginConfig":
        # Load once into this private client object, then remove both authority
        # credentials from the process environment before Hermes can spawn any
        # project-controlled test, build, terminal, or delegated subprocess.
        token = os.environ.pop("HERMES_OPERATOR_BRIDGE_TOKEN", "").strip() or None
        proof_secret = (
            os.environ.pop("HERMES_OPERATOR_BRIDGE_PROOF_SECRET", "").strip()
            or None
        )
        if token is None:
            raise ConfigurationError(
                "HERMES_OPERATOR_BRIDGE_TOKEN is required; do not substitute an admin token"
            )
        if proof_secret is not None and len(proof_secret.encode("utf-8")) < 32:
            raise ConfigurationError(
                "HERMES_OPERATOR_BRIDGE_PROOF_SECRET must contain at least 32 bytes"
            )
        profile = os.getenv("HERMES_OPERATOR_PROFILE", "").strip()
        if not profile:
            raise ConfigurationError("HERMES_OPERATOR_PROFILE is required for attestation")
        if len(profile) > 128 or not all(
            character.isalnum() or character in {"-", "_", "."}
            for character in profile
        ):
            raise ConfigurationError(
                "HERMES_OPERATOR_PROFILE must use 1 to 128 letters, digits, dots, underscores, or hyphens"
            )
        return cls(
            base_url=_base_url(
                os.getenv("HERMES_OPERATOR_URL", "http://127.0.0.1:8787")
            ),
            api_token=token,
            proof_secret=proof_secret,
            credentials_scrubbed=True,
            reviewed_host_override=_flag(
                "HERMES_OPERATOR_REVIEWED_HOST_OVERRIDE",
                False,
            ),
            profile=profile,
            timeout_seconds=_bounded_float(
                "HERMES_OPERATOR_TIMEOUT_SECONDS", 1.5, 0.1, 10.0
            ),
            inject_context=_flag("HERMES_OPERATOR_INJECT_CONTEXT", True),
            emit_lifecycle=_flag("HERMES_OPERATOR_EMIT_LIFECYCLE", True),
            attestation_refresh_seconds=_bounded_float(
                "HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS",
                DEFAULT_ATTEST_INTERVAL_SECONDS,
                MIN_ATTEST_INTERVAL_SECONDS,
                MAX_ATTEST_INTERVAL_SECONDS,
            ),
        )
