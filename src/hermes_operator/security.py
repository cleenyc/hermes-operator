"""Portable approval, provenance, and inbound-content security primitives.

This module deliberately has no Hermes, database, or web-framework dependency.  It
is intended to sit at the boundary between an autonomous reasoning process and
adapters that can cause side effects.  The central rules are:

* every known external action requires an exact, unexpired, one-shot grant;
* unknown actions and store failures are denied;
* approval is bound to the complete canonical action, its recipients, and its
  content;
* content received from an external source remains untrusted even when the
  transport authenticated the sender; and
* untrusted content may be indexed or used to propose work, but may not redefine
  authority or become trusted memory.

The in-memory grant store is useful for tests and single-process deployments.
Production stores should implement :class:`ApprovalGrantStore` with an atomic
``consume`` operation backed by a transaction or compare-and-swap primitive.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


UTC = timezone.utc
_HEX_DIGEST_LENGTH = 64


class ExternalActionCategory(str, Enum):
    """High-level classes of externally observable side effects."""

    COMMUNICATION = "communication"
    PUBLICATION = "publication"
    SCHEDULING = "scheduling"
    SHARING = "sharing"
    SUBMISSION = "submission"
    CODE_CHANGE = "code_change"
    FINANCIAL = "financial"
    DESTRUCTIVE = "destructive"
    SECURITY = "security"
    GENERIC_MUTATION = "generic_mutation"


class ExternalActionType(str, Enum):
    """Closed taxonomy of actions that always require an approval grant.

    A generic member exists for adapters whose mutation cannot yet be modeled
    more narrowly.  Adapters must not invent strings at runtime.  An unknown
    string is denied by :class:`ApprovalAuthorizer`.
    """

    EMAIL_SEND = "email.send"
    EMAIL_REPLY = "email.reply"
    MESSAGE_SEND = "message.send"
    CALENDAR_CREATE = "calendar.create"
    CALENDAR_UPDATE = "calendar.update"
    CALENDAR_CANCEL = "calendar.cancel"
    MEETING_JOIN = "meeting.join"
    DOCUMENT_SHARE = "document.share"
    FILE_UPLOAD = "file.upload"
    FORM_SUBMIT = "form.submit"
    WEB_PUBLISH = "web.publish"
    SOCIAL_PUBLISH = "social.publish"
    CODE_PUSH = "code.push"
    CODE_MERGE = "code.merge"
    FINANCIAL_TRANSACTION = "financial.transaction"
    DATA_DELETE = "data.delete"
    ACCOUNT_PERMISSION_CHANGE = "account.permission_change"
    EXTERNAL_API_MUTATION = "external_api.mutate"


_ACTION_CATEGORIES: dict[ExternalActionType, ExternalActionCategory] = {
    ExternalActionType.EMAIL_SEND: ExternalActionCategory.COMMUNICATION,
    ExternalActionType.EMAIL_REPLY: ExternalActionCategory.COMMUNICATION,
    ExternalActionType.MESSAGE_SEND: ExternalActionCategory.COMMUNICATION,
    ExternalActionType.CALENDAR_CREATE: ExternalActionCategory.SCHEDULING,
    ExternalActionType.CALENDAR_UPDATE: ExternalActionCategory.SCHEDULING,
    ExternalActionType.CALENDAR_CANCEL: ExternalActionCategory.SCHEDULING,
    ExternalActionType.MEETING_JOIN: ExternalActionCategory.SCHEDULING,
    ExternalActionType.DOCUMENT_SHARE: ExternalActionCategory.SHARING,
    ExternalActionType.FILE_UPLOAD: ExternalActionCategory.SHARING,
    ExternalActionType.FORM_SUBMIT: ExternalActionCategory.SUBMISSION,
    ExternalActionType.WEB_PUBLISH: ExternalActionCategory.PUBLICATION,
    ExternalActionType.SOCIAL_PUBLISH: ExternalActionCategory.PUBLICATION,
    ExternalActionType.CODE_PUSH: ExternalActionCategory.CODE_CHANGE,
    ExternalActionType.CODE_MERGE: ExternalActionCategory.CODE_CHANGE,
    ExternalActionType.FINANCIAL_TRANSACTION: ExternalActionCategory.FINANCIAL,
    ExternalActionType.DATA_DELETE: ExternalActionCategory.DESTRUCTIVE,
    ExternalActionType.ACCOUNT_PERMISSION_CHANGE: ExternalActionCategory.SECURITY,
    ExternalActionType.EXTERNAL_API_MUTATION: ExternalActionCategory.GENERIC_MUTATION,
}


def action_category(
    action_type: ExternalActionType | str,
) -> ExternalActionCategory | None:
    """Return a known action's category, or ``None`` for an unknown action."""

    try:
        known = (
            action_type
            if isinstance(action_type, ExternalActionType)
            else ExternalActionType(action_type)
        )
    except (TypeError, ValueError):
        return None
    return _ACTION_CATEGORIES[known]


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_text(value: str, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in normalized:
        raise ValueError(f"{name} must not contain NUL characters")
    return normalized


def _content_bytes(content: str | bytes) -> tuple[bytes, str]:
    if isinstance(content, str):
        normalized = unicodedata.normalize("NFC", content)
        return normalized.encode("utf-8"), "utf-8"
    if isinstance(content, bytes):
        return content, "binary"
    raise TypeError("content must be str or bytes")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_digest(value: str) -> bool:
    if not isinstance(value, str) or len(value) != _HEX_DIGEST_LENGTH:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _canonical_recipients(recipients: Iterable[str]) -> tuple[str, ...]:
    if isinstance(recipients, (str, bytes)):
        raise TypeError("recipients must be an iterable of strings")
    normalized = tuple(
        _normalize_text(recipient, "recipient") for recipient in recipients
    )
    # Recipient order has no semantic meaning for an action.  Duplicates are
    # retained because sending twice is not equivalent to sending once.
    return tuple(sorted(normalized))


def _canonical_attributes(
    attributes: Mapping[str, str] | Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    items = attributes.items() if isinstance(attributes, Mapping) else attributes
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_key, raw_value in items:
        key = _normalize_text(raw_key, "attribute key")
        value = _normalize_text(raw_value, f"attribute {key}", allow_empty=True)
        if key in seen:
            raise ValueError(f"duplicate attribute key: {key}")
        seen.add(key)
        normalized.append((key, value))
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """An immutable description of exactly one proposed external side effect.

    ``attributes`` holds adapter-specific values that affect execution, such as a
    subject line, attachment digest, HTTP method, or calendar timezone.  Secrets
    should not be put in an intent.  Recipient and attribute order are
    canonicalized so semantically identical intents have the same digest.
    """

    action_type: ExternalActionType | str
    actor_id: str
    integration: str
    recipients: tuple[str, ...] = ()
    content: str | bytes = ""
    target: str = ""
    content_media_type: str = "text/plain"
    attributes: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.action_type, ExternalActionType):
            action_type: ExternalActionType | str = self.action_type
        else:
            action_type = _normalize_text(self.action_type, "action_type")
        actor_id = _normalize_text(self.actor_id, "actor_id")
        integration = _normalize_text(self.integration, "integration")
        recipients = _canonical_recipients(self.recipients)
        target = _normalize_text(self.target, "target", allow_empty=True)
        media_type = _normalize_text(self.content_media_type, "content_media_type")
        attributes = _canonical_attributes(self.attributes)
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise TypeError("schema_version must be an integer")
        if self.schema_version != 1:
            raise ValueError("unsupported action intent schema_version")

        content_bytes, encoding = _content_bytes(self.content)
        content: str | bytes
        if encoding == "utf-8":
            content = content_bytes.decode("utf-8")
        else:
            content = bytes(content_bytes)

        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "integration", integration)
        object.__setattr__(self, "recipients", recipients)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "content_media_type", media_type)
        object.__setattr__(self, "attributes", attributes)

    @property
    def action_type_value(self) -> str:
        if isinstance(self.action_type, ExternalActionType):
            return self.action_type.value
        return self.action_type

    @property
    def known_action_type(self) -> ExternalActionType | None:
        try:
            return ExternalActionType(self.action_type_value)
        except ValueError:
            return None

    @property
    def content_digest(self) -> str:
        content, _ = _content_bytes(self.content)
        return _sha256(content)

    @property
    def recipients_digest(self) -> str:
        encoded = json.dumps(
            list(self.recipients),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256(encoded)

    def canonical_payload(self) -> dict[str, object]:
        content, encoding = _content_bytes(self.content)
        return {
            "action_type": self.action_type_value,
            "actor_id": self.actor_id,
            "attributes": dict(self.attributes),
            "content": {
                # The digest binds exact content without placing potentially
                # sensitive message text in approval records or audit logs.
                "encoding": encoding,
                "length": len(content),
                "sha256": _sha256(content),
            },
            "content_media_type": self.content_media_type,
            "integration": self.integration,
            "recipients": list(self.recipients),
            "schema_version": self.schema_version,
            "target": self.target,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return _sha256(self.canonical_bytes())


def canonicalize_action_intent(intent: ActionIntent) -> bytes:
    """Return deterministic bytes suitable for signing or hashing."""

    if not isinstance(intent, ActionIntent):
        raise TypeError("intent must be an ActionIntent")
    return intent.canonical_bytes()


def digest_action_intent(intent: ActionIntent) -> str:
    """Return the SHA-256 digest of a canonical action intent."""

    return _sha256(canonicalize_action_intent(intent))


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """A time-bounded approval tied to one exact action intent."""

    grant_id: str
    action_type: str
    action_digest: str
    recipients_digest: str
    content_digest: str
    approved_by: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        grant_id = _normalize_text(self.grant_id, "grant_id")
        action_type = _normalize_text(self.action_type, "action_type")
        approved_by = _normalize_text(self.approved_by, "approved_by")
        for name, digest in (
            ("action_digest", self.action_digest),
            ("recipients_digest", self.recipients_digest),
            ("content_digest", self.content_digest),
        ):
            if not _valid_digest(digest):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        issued_at = _require_aware(self.issued_at, "issued_at")
        expires_at = _require_aware(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        object.__setattr__(self, "grant_id", grant_id)
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "approved_by", approved_by)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def issue(
        cls,
        intent: ActionIntent,
        *,
        approved_by: str,
        ttl: timedelta,
        now: datetime | None = None,
        grant_id: str | None = None,
    ) -> "ApprovalGrant":
        """Create a grant after an authenticated approval signal.

        Calling this method is not itself proof of human approval.  The adapter
        that calls it must authenticate the operator and should persist the
        original approval event.
        """

        if intent.known_action_type is None:
            raise ValueError("cannot issue a grant for an unknown action type")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        issued_at = _require_aware(now or datetime.now(UTC), "now")
        return cls(
            grant_id=grant_id or f"gr_{secrets.token_urlsafe(24)}",
            action_type=intent.action_type_value,
            action_digest=intent.digest,
            recipients_digest=intent.recipients_digest,
            content_digest=intent.content_digest,
            approved_by=approved_by,
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )

    def matches(self, intent: ActionIntent) -> bool:
        """Compare every explicit and aggregate action binding."""

        return (
            self.action_type == intent.action_type_value
            and hmac.compare_digest(self.action_digest, intent.digest)
            and hmac.compare_digest(
                self.recipients_digest, intent.recipients_digest
            )
            and hmac.compare_digest(self.content_digest, intent.content_digest)
        )


class ApprovalDecisionReason(str, Enum):
    APPROVED = "approved"
    APPROVAL_REQUIRED = "approval_required"
    INVALID_INTENT = "invalid_intent"
    INVALID_DECISION_TIME = "invalid_decision_time"
    INVALID_GRANT_ID = "invalid_grant_id"
    UNKNOWN_ACTION_TYPE = "unknown_action_type"
    GRANT_NOT_FOUND = "grant_not_found"
    GRANT_ALREADY_CONSUMED = "grant_already_consumed"
    GRANT_REVOKED = "grant_revoked"
    GRANT_NOT_YET_VALID = "grant_not_yet_valid"
    GRANT_EXPIRED = "grant_expired"
    GRANT_LIFETIME_EXCEEDED = "grant_lifetime_exceeded"
    GRANT_BINDING_MISMATCH = "grant_binding_mismatch"
    SECURITY_STORE_FAILURE = "security_store_failure"


@dataclass(frozen=True, slots=True)
class GrantConsumption:
    allowed: bool
    reason: ApprovalDecisionReason
    grant: ApprovalGrant | None = None


@runtime_checkable
class ApprovalGrantStore(Protocol):
    """Storage contract whose consume operation must be atomic."""

    def add(self, grant: ApprovalGrant) -> None:
        """Persist a new grant, rejecting an existing grant ID."""

    def consume(
        self,
        grant_id: str,
        *,
        intent: ActionIntent,
        now: datetime,
        max_lifetime: timedelta,
    ) -> GrantConsumption:
        """Atomically validate and consume a matching grant."""

    def revoke(self, grant_id: str) -> bool:
        """Revoke a grant, returning whether it existed."""


class InMemoryApprovalGrantStore:
    """Thread-safe reference implementation of one-shot grant storage."""

    def __init__(self) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._consumed: dict[str, datetime] = {}
        self._revoked: set[str] = set()
        self._lock = threading.RLock()

    def add(self, grant: ApprovalGrant) -> None:
        if not isinstance(grant, ApprovalGrant):
            raise TypeError("grant must be an ApprovalGrant")
        with self._lock:
            if grant.grant_id in self._grants:
                raise ValueError("grant_id already exists")
            self._grants[grant.grant_id] = grant

    def get(self, grant_id: str) -> ApprovalGrant | None:
        with self._lock:
            return self._grants.get(grant_id)

    def is_consumed(self, grant_id: str) -> bool:
        with self._lock:
            return grant_id in self._consumed

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            if grant_id not in self._grants:
                return False
            self._revoked.add(grant_id)
            return True

    def consume(
        self,
        grant_id: str,
        *,
        intent: ActionIntent,
        now: datetime,
        max_lifetime: timedelta,
    ) -> GrantConsumption:
        now = _require_aware(now, "now")
        if not isinstance(max_lifetime, timedelta) or max_lifetime <= timedelta(0):
            raise ValueError("max_lifetime must be positive")
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_NOT_FOUND
                )
            if grant_id in self._consumed:
                return GrantConsumption(
                    False,
                    ApprovalDecisionReason.GRANT_ALREADY_CONSUMED,
                    grant,
                )
            if grant_id in self._revoked:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_REVOKED, grant
                )
            if grant.expires_at - grant.issued_at > max_lifetime:
                return GrantConsumption(
                    False,
                    ApprovalDecisionReason.GRANT_LIFETIME_EXCEEDED,
                    grant,
                )
            if now < grant.issued_at:
                return GrantConsumption(
                    False,
                    ApprovalDecisionReason.GRANT_NOT_YET_VALID,
                    grant,
                )
            if now >= grant.expires_at:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_EXPIRED, grant
                )
            if not grant.matches(intent):
                return GrantConsumption(
                    False,
                    ApprovalDecisionReason.GRANT_BINDING_MISMATCH,
                    grant,
                )

            # The transition happens under the same lock as all validation.
            # Persistent implementations must provide the same atomicity.
            self._consumed[grant_id] = now
            return GrantConsumption(True, ApprovalDecisionReason.APPROVED, grant)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: ApprovalDecisionReason
    intent_digest: str
    grant_id: str | None
    decided_at: datetime


class ApprovalAuthorizer:
    """Fail-closed decision point for every external-action adapter."""

    def __init__(
        self,
        store: ApprovalGrantStore,
        *,
        max_grant_lifetime: timedelta = timedelta(minutes=15),
    ) -> None:
        if not isinstance(max_grant_lifetime, timedelta) or (
            max_grant_lifetime <= timedelta(0)
        ):
            raise ValueError("max_grant_lifetime must be positive")
        self._store = store
        self._max_grant_lifetime = max_grant_lifetime

    def authorize(
        self,
        intent: ActionIntent,
        *,
        grant_id: str | None,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        fallback_time = datetime.now(UTC)
        if not isinstance(intent, ActionIntent):
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.INVALID_INTENT,
                "",
                grant_id if isinstance(grant_id, str) else None,
                fallback_time,
            )
        try:
            decided_at = _require_aware(now or fallback_time, "now")
        except (TypeError, ValueError):
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.INVALID_DECISION_TIME,
                intent.digest,
                grant_id if isinstance(grant_id, str) else None,
                fallback_time,
            )
        intent_digest = intent.digest
        if intent.known_action_type is None:
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.UNKNOWN_ACTION_TYPE,
                intent_digest,
                grant_id,
                decided_at,
            )
        if grant_id is None or grant_id == "":
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.APPROVAL_REQUIRED,
                intent_digest,
                None,
                decided_at,
            )
        if not isinstance(grant_id, str) or not grant_id.strip():
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.INVALID_GRANT_ID,
                intent_digest,
                grant_id if isinstance(grant_id, str) else None,
                decided_at,
            )

        try:
            result = self._store.consume(
                grant_id,
                intent=intent,
                now=decided_at,
                max_lifetime=self._max_grant_lifetime,
            )
        except Exception:
            # A side effect is never permitted merely because approval storage
            # is unavailable, corrupt, or implemented incorrectly.
            return AuthorizationDecision(
                False,
                ApprovalDecisionReason.SECURITY_STORE_FAILURE,
                intent_digest,
                grant_id,
                decided_at,
            )
        return AuthorizationDecision(
            result.allowed,
            result.reason,
            intent_digest,
            grant_id,
            decided_at,
        )


class SourceKind(str, Enum):
    OPERATOR_INPUT = "operator_input"
    SYSTEM_STATE = "system_state"
    EMAIL = "email"
    CALENDAR_EVENT = "calendar_event"
    MEETING_TRANSCRIPT = "meeting_transcript"
    CHAT_MESSAGE = "chat_message"
    WEB_CONTENT = "web_content"
    FILE_ATTACHMENT = "file_attachment"
    WEBHOOK = "webhook"
    TOOL_OUTPUT = "tool_output"
    MODEL_DERIVED = "model_derived"


class TrustLabel(str, Enum):
    """Trust is about authority, not sender authentication or factual accuracy."""

    TRUSTED_OPERATOR = "trusted_operator"
    TRUSTED_SYSTEM = "trusted_system"
    UNTRUSTED_AUTHENTICATED = "untrusted_authenticated"
    UNTRUSTED_EXTERNAL = "untrusted_external"
    UNTRUSTED_DERIVED = "untrusted_derived"
    UNKNOWN = "unknown"

    @property
    def is_authoritative(self) -> bool:
        return self in {self.TRUSTED_OPERATOR, self.TRUSTED_SYSTEM}

    @property
    def is_operator(self) -> bool:
        return self is self.TRUSTED_OPERATOR


_EXTERNAL_SOURCE_KINDS = {
    SourceKind.EMAIL,
    SourceKind.CALENDAR_EVENT,
    SourceKind.MEETING_TRANSCRIPT,
    SourceKind.CHAT_MESSAGE,
    SourceKind.WEB_CONTENT,
    SourceKind.FILE_ATTACHMENT,
    SourceKind.WEBHOOK,
    SourceKind.TOOL_OUTPUT,
}


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """Tamper-evident metadata describing where a piece of content came from."""

    source_kind: SourceKind
    source_id: str
    trust: TrustLabel
    captured_at: datetime
    content_digest: str
    parent_digests: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("source_kind must be a SourceKind")
        if not isinstance(self.trust, TrustLabel):
            raise TypeError("trust must be a TrustLabel")
        source_id = _normalize_text(self.source_id, "source_id")
        captured_at = _require_aware(self.captured_at, "captured_at")
        if not _valid_digest(self.content_digest):
            raise ValueError("content_digest must be a SHA-256 hex digest")
        parents = tuple(sorted(self.parent_digests))
        if any(not _valid_digest(digest) for digest in parents):
            raise ValueError("every parent digest must be a SHA-256 hex digest")
        metadata = _canonical_attributes(self.metadata)

        if self.source_kind in _EXTERNAL_SOURCE_KINDS and self.trust.is_authoritative:
            raise ValueError("external source content cannot carry a trusted label")
        if (
            self.source_kind is SourceKind.MODEL_DERIVED
            and self.trust is not TrustLabel.UNTRUSTED_DERIVED
        ):
            raise ValueError("model-derived content must remain untrusted")
        if (
            self.source_kind is SourceKind.OPERATOR_INPUT
            and self.trust not in {TrustLabel.TRUSTED_OPERATOR, TrustLabel.UNKNOWN}
        ):
            raise ValueError("operator input has an incompatible trust label")
        if (
            self.source_kind is SourceKind.SYSTEM_STATE
            and self.trust not in {TrustLabel.TRUSTED_SYSTEM, TrustLabel.UNKNOWN}
        ):
            raise ValueError("system state has an incompatible trust label")

        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "parent_digests", parents)
        object.__setattr__(self, "metadata", metadata)

    def canonical_payload(self) -> dict[str, object]:
        captured_at = self.captured_at.isoformat().replace("+00:00", "Z")
        return {
            "captured_at": captured_at,
            "content_digest": self.content_digest,
            "metadata": dict(self.metadata),
            "parent_digests": list(self.parent_digests),
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "trust": self.trust.value,
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return _sha256(canonical)

    @classmethod
    def capture(
        cls,
        *,
        source_kind: SourceKind,
        source_id: str,
        trust: TrustLabel,
        content: str | bytes,
        captured_at: datetime | None = None,
        parent_digests: Sequence[str] = (),
        metadata: Mapping[str, str] | Iterable[tuple[str, str]] = (),
    ) -> "ProvenanceRecord":
        content_bytes, _ = _content_bytes(content)
        return cls(
            source_kind=source_kind,
            source_id=source_id,
            trust=trust,
            captured_at=captured_at or datetime.now(UTC),
            content_digest=_sha256(content_bytes),
            parent_digests=tuple(parent_digests),
            metadata=_canonical_attributes(metadata),
        )

    @classmethod
    def derive(
        cls,
        *,
        source_id: str,
        content: str | bytes,
        parents: Sequence["ProvenanceRecord"],
        captured_at: datetime | None = None,
        metadata: Mapping[str, str] | Iterable[tuple[str, str]] = (),
    ) -> "ProvenanceRecord":
        if not parents:
            raise ValueError("derived provenance requires at least one parent")
        return cls.capture(
            source_kind=SourceKind.MODEL_DERIVED,
            source_id=source_id,
            trust=TrustLabel.UNTRUSTED_DERIVED,
            content=content,
            captured_at=captured_at,
            parent_digests=tuple(parent.digest for parent in parents),
            metadata=metadata,
        )


class ContentUse(str, Enum):
    """Ways inbound content can influence the autonomous system."""

    STORE_QUARANTINED = "store_quarantined"
    INDEX_FOR_RETRIEVAL = "index_for_retrieval"
    EXTRACT_WORK_CANDIDATE = "extract_work_candidate"
    CITE_AS_UNTRUSTED_EVIDENCE = "cite_as_untrusted_evidence"
    EXECUTE_EMBEDDED_INSTRUCTION = "execute_embedded_instruction"
    CHANGE_POLICY = "change_policy"
    CHANGE_IDENTITY = "change_identity"
    CHANGE_PERMISSION = "change_permission"
    PROMOTE_TRUSTED_MEMORY = "promote_trusted_memory"
    AUTHORIZE_EXTERNAL_ACTION = "authorize_external_action"


_QUARANTINE_SAFE_USES = {
    ContentUse.STORE_QUARANTINED,
    ContentUse.INDEX_FOR_RETRIEVAL,
    ContentUse.EXTRACT_WORK_CANDIDATE,
    ContentUse.CITE_AS_UNTRUSTED_EVIDENCE,
}

_OPERATOR_ONLY_USES = {
    ContentUse.EXECUTE_EMBEDDED_INSTRUCTION,
    ContentUse.CHANGE_POLICY,
    ContentUse.CHANGE_IDENTITY,
    ContentUse.CHANGE_PERMISSION,
    ContentUse.PROMOTE_TRUSTED_MEMORY,
    ContentUse.AUTHORIZE_EXTERNAL_ACTION,
}


class QuarantineDecisionReason(str, Enum):
    ALLOWED_TRUSTED = "allowed_trusted"
    ALLOWED_IN_QUARANTINE = "allowed_in_quarantine"
    OPERATOR_AUTHORITY_REQUIRED = "operator_authority_required"
    UNKNOWN_CONTENT_USE = "unknown_content_use"


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    allowed: bool
    quarantined: bool
    operator_review_required: bool
    reason: QuarantineDecisionReason


class InboundContentPolicy:
    """Fail-closed policy for using content according to its provenance."""

    def decide(
        self,
        provenance: ProvenanceRecord,
        use: ContentUse | str,
    ) -> QuarantineDecision:
        try:
            known_use = use if isinstance(use, ContentUse) else ContentUse(use)
        except (TypeError, ValueError):
            return QuarantineDecision(
                False,
                True,
                True,
                QuarantineDecisionReason.UNKNOWN_CONTENT_USE,
            )

        if known_use in _QUARANTINE_SAFE_USES:
            if provenance.trust.is_authoritative:
                return QuarantineDecision(
                    True,
                    False,
                    False,
                    QuarantineDecisionReason.ALLOWED_TRUSTED,
                )
            return QuarantineDecision(
                True,
                True,
                known_use is ContentUse.EXTRACT_WORK_CANDIDATE,
                QuarantineDecisionReason.ALLOWED_IN_QUARANTINE,
            )

        if known_use in _OPERATOR_ONLY_USES:
            if provenance.trust.is_operator:
                return QuarantineDecision(
                    True,
                    False,
                    False,
                    QuarantineDecisionReason.ALLOWED_TRUSTED,
                )
            return QuarantineDecision(
                False,
                not provenance.trust.is_authoritative,
                True,
                QuarantineDecisionReason.OPERATOR_AUTHORITY_REQUIRED,
            )

        return QuarantineDecision(
            False,
            True,
            True,
            QuarantineDecisionReason.UNKNOWN_CONTENT_USE,
        )


@dataclass(frozen=True, slots=True)
class QuarantinedContent:
    """Content plus provenance, with substitution protection."""

    content: str | bytes
    provenance: ProvenanceRecord
    quarantined_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        content, encoding = _content_bytes(self.content)
        if not hmac.compare_digest(_sha256(content), self.provenance.content_digest):
            raise ValueError("content does not match its provenance digest")
        normalized_content: str | bytes = (
            content.decode("utf-8") if encoding == "utf-8" else bytes(content)
        )
        object.__setattr__(self, "content", normalized_content)
        object.__setattr__(
            self,
            "quarantined_at",
            _require_aware(self.quarantined_at, "quarantined_at"),
        )


__all__ = [
    "ActionIntent",
    "ApprovalAuthorizer",
    "ApprovalDecisionReason",
    "ApprovalGrant",
    "ApprovalGrantStore",
    "AuthorizationDecision",
    "ContentUse",
    "ExternalActionCategory",
    "ExternalActionType",
    "GrantConsumption",
    "InboundContentPolicy",
    "InMemoryApprovalGrantStore",
    "ProvenanceRecord",
    "QuarantineDecision",
    "QuarantineDecisionReason",
    "QuarantinedContent",
    "SourceKind",
    "TrustLabel",
    "action_category",
    "canonicalize_action_intent",
    "digest_action_intent",
]
