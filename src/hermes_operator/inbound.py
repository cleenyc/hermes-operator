from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .models import Event, TrustLevel, utc_now


_SOURCE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_EVENT_TYPE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_RESERVED_EXTERNAL_SOURCES = frozenset({"operator", "system"})


class InboundValidationError(ValueError):
    """An inbound event envelope is malformed or violates an ingress boundary."""


@dataclass(frozen=True, slots=True)
class NormalizedInbound:
    event: Event
    authenticated: bool


def verify_webhook_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify an ``X-Hermes-Signature`` value of the form ``sha256=<hex>``.

    A configured but empty secret is treated as unusable and never verifies.
    """

    if not secret or not signature:
        return False
    prefix = "sha256="
    if not signature.startswith(prefix):
        return False
    supplied = signature[len(prefix) :]
    if len(supplied) != 64:
        return False
    try:
        bytes.fromhex(supplied)
    except ValueError:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def normalize_external_event(
    source: str,
    document: Mapping[str, Any],
    *,
    authenticated: bool,
    request_id: str | None = None,
    remote_address: str | None = None,
) -> NormalizedInbound:
    """Create an event from an external webhook envelope.

    The envelope has ``event_type``, an object-valued ``payload``, and optional
    ``external_id`` and ``dedupe_key`` fields. HMAC authentication proves source
    possession of a shared secret, but never turns external content into trusted
    operator instructions.
    """

    source = _validate_source(source)
    if source.lower() in _RESERVED_EXTERNAL_SOURCES:
        raise InboundValidationError(f"External source is reserved: {source}")
    event_type, payload, external_id, dedupe_key = _validate_envelope(document)
    trust = (
        TrustLevel.AUTHENTICATED_UNTRUSTED if authenticated else TrustLevel.UNTRUSTED
    )
    received_at = utc_now()
    event = Event(
        source=source,
        event_type=event_type,
        payload=payload,
        external_id=external_id,
        dedupe_key=dedupe_key,
        trust_level=trust,
        provenance={
            "ingress": "webhook",
            "authenticated": authenticated,
            "request_id": request_id,
            "remote_address": remote_address,
            "received_at": received_at,
        },
    )
    return NormalizedInbound(event=event, authenticated=authenticated)


def normalize_operator_event(
    document: Mapping[str, Any],
    *,
    request_id: str | None = None,
    actor: str = "operator-api",
) -> NormalizedInbound:
    """Create an operator-trusted event from an authenticated API request."""

    raw_source = document.get("source", "operator")
    if not isinstance(raw_source, str):
        raise InboundValidationError("source must be a string")
    source = _validate_source(raw_source)
    if source == "system":
        raise InboundValidationError("The operator API cannot create system events")
    event_type, payload, external_id, dedupe_key = _validate_envelope(document)
    event = Event(
        source=source,
        event_type=event_type,
        payload=payload,
        external_id=external_id,
        dedupe_key=dedupe_key,
        trust_level=TrustLevel.OPERATOR,
        provenance={
            "ingress": "operator-api",
            "authenticated": True,
            "actor": actor,
            "request_id": request_id,
            "received_at": utc_now(),
        },
    )
    return NormalizedInbound(event=event, authenticated=True)


def _validate_source(source: str) -> str:
    source = source.strip()
    if not _SOURCE_PATTERN.fullmatch(source):
        raise InboundValidationError(
            "source must be 1 to 64 characters using letters, numbers, dot, underscore, or hyphen"
        )
    return source


def _validate_envelope(
    document: Mapping[str, Any],
) -> tuple[str, dict[str, Any], str | None, str | None]:
    if not isinstance(document, Mapping):
        raise InboundValidationError("JSON body must be an object")
    event_type = document.get("event_type")
    if not isinstance(event_type, str) or not _EVENT_TYPE_PATTERN.fullmatch(event_type.strip()):
        raise InboundValidationError(
            "event_type must be 1 to 128 characters using letters, numbers, dot, colon, underscore, or hyphen"
        )
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise InboundValidationError("payload must be an object")
    external_id = document.get("external_id")
    if external_id is not None:
        if not isinstance(external_id, str) or not external_id.strip():
            raise InboundValidationError("external_id must be a nonempty string")
        if len(external_id) > 512:
            raise InboundValidationError("external_id cannot exceed 512 characters")
        external_id = external_id.strip()
    dedupe_key = document.get("dedupe_key")
    if dedupe_key is not None:
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise InboundValidationError("dedupe_key must be a nonempty string")
        if len(dedupe_key) > 256:
            raise InboundValidationError("dedupe_key cannot exceed 256 characters")
        dedupe_key = dedupe_key.strip()
    return event_type.strip(), payload, external_id, dedupe_key
