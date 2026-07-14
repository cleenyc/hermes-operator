from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol

from .db import NotFound, SQLiteStore
from .models import utc_now
from .security import (
    ActionIntent,
    ApprovalDecisionReason,
    ApprovalGrant,
    ApprovalGrantStore,
    ExternalActionCategory,
    ExternalActionType,
    GrantConsumption,
    action_category,
)


class ApprovalError(RuntimeError):
    pass


class ApprovalStateError(ApprovalError):
    pass


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Stored timestamp is missing a timezone")
    return parsed.astimezone(UTC)


def _time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _deterministic_risk(action_type: str) -> str:
    category = action_category(action_type)
    if category in {
        ExternalActionCategory.CODE_CHANGE,
        ExternalActionCategory.DESTRUCTIVE,
        ExternalActionCategory.FINANCIAL,
        ExternalActionCategory.PUBLICATION,
        ExternalActionCategory.SECURITY,
    }:
        return "high"
    if category is None:
        return "high"
    return "medium"


def _execution_payload(intent: ActionIntent) -> dict[str, Any]:
    if isinstance(intent.content, bytes):
        content: Any = {
            "encoding": "base64",
            "value": base64.b64encode(intent.content).decode("ascii"),
        }
    else:
        content = {"encoding": "utf-8", "value": intent.content}
    return {
        "action_type": intent.action_type_value,
        "actor_id": intent.actor_id,
        "integration": intent.integration,
        "recipients": list(intent.recipients),
        "content": content,
        "target": intent.target,
        "content_media_type": intent.content_media_type,
        "attributes": dict(intent.attributes),
        "schema_version": intent.schema_version,
    }


def _intent_from_execution(payload: Mapping[str, Any]) -> ActionIntent:
    content_payload = payload.get("content", {})
    if not isinstance(content_payload, Mapping):
        raise ValueError("Stored action content is malformed")
    encoding = str(content_payload.get("encoding", "utf-8"))
    raw_content = content_payload.get("value", "")
    if encoding == "base64":
        content: str | bytes = base64.b64decode(str(raw_content), validate=True)
    elif encoding == "utf-8":
        content = str(raw_content)
    else:
        raise ValueError("Stored action content encoding is unsupported")
    attributes = payload.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ValueError("Stored action attributes are malformed")
    recipients = payload.get("recipients", [])
    if not isinstance(recipients, list):
        raise ValueError("Stored action recipients are malformed")
    return ActionIntent(
        action_type=str(payload["action_type"]),
        actor_id=str(payload["actor_id"]),
        integration=str(payload["integration"]),
        recipients=tuple(str(value) for value in recipients),
        content=content,
        target=str(payload.get("target", "")),
        content_media_type=str(payload.get("content_media_type", "text/plain")),
        attributes=tuple((str(key), str(value)) for key, value in attributes.items()),
        schema_version=int(payload.get("schema_version", 1)),
    )


class SQLiteApprovalGrantStore(ApprovalGrantStore):
    """Atomic one-shot approval storage backed by the operator database."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    def add(self, grant: ApprovalGrant) -> None:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM action_intents WHERE digest = ? ORDER BY created_at DESC LIMIT 2",
                (grant.action_digest,),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Approval intent digest must identify exactly one staged intent")
            self._insert(connection, str(rows[0]["id"]), grant)

    def add_for_intent(self, intent_id: str, grant: ApprovalGrant) -> None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT digest FROM action_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise NotFound(intent_id)
            if str(row["digest"]) != grant.action_digest:
                raise ValueError("Grant does not match the staged intent")
            self._insert(connection, intent_id, grant)

    def _insert(
        self, connection: sqlite3.Connection, intent_id: str, grant: ApprovalGrant
    ) -> None:
        metadata = {
            "action_type": grant.action_type,
            "recipients_digest": grant.recipients_digest,
            "content_digest": grant.content_digest,
        }
        try:
            connection.execute(
                """
                INSERT INTO approval_grants(
                    id, intent_id, intent_digest, decision, approver, created_at,
                    expires_at, metadata_json
                ) VALUES(?, ?, ?, 'approved', ?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    intent_id,
                    grant.action_digest,
                    grant.approved_by,
                    _time_text(grant.issued_at),
                    _time_text(grant.expires_at),
                    _json_value(metadata),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("grant_id already exists") from error

    def revoke(self, grant_id: str) -> bool:
        with self.store.connection() as connection:
            cursor = connection.execute(
                "UPDATE approval_grants SET decision = 'revoked' "
                "WHERE id = ? AND decision = 'approved' AND consumed_at IS NULL",
                (grant_id,),
            )
            return cursor.rowcount == 1

    def consume(
        self,
        grant_id: str,
        *,
        intent: ActionIntent,
        now: datetime,
        max_lifetime: timedelta,
    ) -> GrantConsumption:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_grants WHERE id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                return GrantConsumption(False, ApprovalDecisionReason.GRANT_NOT_FOUND)
            try:
                grant = self.grant_from_row(row)
            except (KeyError, TypeError, ValueError):
                return GrantConsumption(False, ApprovalDecisionReason.SECURITY_STORE_FAILURE)
            if row["consumed_at"] is not None:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_ALREADY_CONSUMED, grant
                )
            if row["decision"] == "revoked":
                return GrantConsumption(False, ApprovalDecisionReason.GRANT_REVOKED, grant)
            if row["decision"] != "approved":
                return GrantConsumption(False, ApprovalDecisionReason.APPROVAL_REQUIRED, grant)
            if grant.expires_at - grant.issued_at > max_lifetime:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_LIFETIME_EXCEEDED, grant
                )
            if now < grant.issued_at:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_NOT_YET_VALID, grant
                )
            if now >= grant.expires_at:
                return GrantConsumption(False, ApprovalDecisionReason.GRANT_EXPIRED, grant)
            if not grant.matches(intent):
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_BINDING_MISMATCH, grant
                )
            cursor = connection.execute(
                "UPDATE approval_grants SET consumed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL AND decision = 'approved'",
                (_time_text(now), grant_id),
            )
            if cursor.rowcount != 1:
                return GrantConsumption(
                    False, ApprovalDecisionReason.GRANT_ALREADY_CONSUMED, grant
                )
            return GrantConsumption(True, ApprovalDecisionReason.APPROVED, grant)

    @staticmethod
    def grant_from_row(row: sqlite3.Row) -> ApprovalGrant:
        metadata = json.loads(row["metadata_json"] or "{}")
        if not isinstance(metadata, dict):
            raise ValueError("Approval grant metadata must be an object")
        return ApprovalGrant(
            grant_id=str(row["id"]),
            action_type=str(metadata["action_type"]),
            action_digest=str(row["intent_digest"]),
            recipients_digest=str(metadata["recipients_digest"]),
            content_digest=str(metadata["content_digest"]),
            approved_by=str(row["approver"]),
            issued_at=_parse_time(str(row["created_at"])),
            expires_at=_parse_time(str(row["expires_at"])),
        )


@dataclass(frozen=True, slots=True)
class StagedAction:
    id: str
    status: str
    risk: str
    reason: str
    source_event_id: str | None
    created_at: str
    expires_at: str
    intent: ActionIntent
    result: dict[str, Any]

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        execution = _execution_payload(self.intent)
        if not include_content:
            execution["content"] = {
                "encoding": execution["content"]["encoding"],
                "sha256": self.intent.content_digest,
            }
        return {
            "id": self.id,
            "status": self.status,
            "risk": self.risk,
            "reason": self.reason,
            "source_event_id": self.source_event_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "digest": self.intent.digest,
            "intent": execution,
            "result": self.result,
        }


class ExternalActionStager:
    """Stages exact action proposals and records authenticated decisions."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        ttl_seconds: int = 3600,
        actor_id: str = "hermes-operator",
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.store = store
        self.ttl = timedelta(seconds=ttl_seconds)
        self.actor_id = actor_id
        self.grants = SQLiteApprovalGrantStore(store)

    def stage(self, proposal: dict[str, Any], *, created_by: str) -> str:
        intent = self._proposal_to_intent(proposal)
        if intent.known_action_type is None:
            raise ValueError(f"Unsupported external action type: {intent.action_type_value}")
        now = datetime.now(UTC)
        expires = now + self.ttl
        source_event_id = proposal.get("source_event_id") or None
        risk = _deterministic_risk(intent.action_type_value)
        stable = _json_value(
            {
                "intent": intent.digest,
                "source_event_id": source_event_id,
                "reason": str(proposal.get("reason", "")),
                "created_by": created_by,
            }
        )
        action_id = "act_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        record = {
            "canonical": intent.canonical_payload(),
            "execution": _execution_payload(intent),
            "reason": str(proposal.get("reason", "")),
        }
        with self.store.connection() as connection:
            existing = connection.execute(
                "SELECT digest FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != intent.digest:
                    raise ApprovalStateError("Action idempotency key collision")
                return action_id
            connection.execute(
                """
                INSERT INTO action_intents(
                    id, action_type, canonical_json, digest, status, risk,
                    created_by, source_event_id, created_at, expires_at
                ) VALUES(?, ?, ?, ?, 'pending_approval', ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    intent.action_type_value,
                    _json_value(record),
                    intent.digest,
                    risk,
                    created_by,
                    source_event_id,
                    _time_text(now),
                    _time_text(expires),
                ),
            )
            self.store.audit(
                created_by,
                "external_action.staged",
                entity_type="action_intent",
                entity_id=action_id,
                data={
                    "action_type": intent.action_type_value,
                    "digest": intent.digest,
                    "recipients_digest": intent.recipients_digest,
                    "content_digest": intent.content_digest,
                },
                connection=connection,
            )
        return action_id

    def _proposal_to_intent(self, proposal: Mapping[str, Any]) -> ActionIntent:
        action_type = str(proposal.get("action_type", "")).strip()
        target_data = proposal.get("target", {})
        if isinstance(target_data, Mapping):
            recipients_raw = target_data.get("recipients", proposal.get("recipients", []))
            target_copy = {
                str(key): value
                for key, value in target_data.items()
                if str(key) != "recipients"
            }
            target = _json_value(target_copy) if target_copy else ""
        else:
            recipients_raw = proposal.get("recipients", [])
            target = str(target_data)
        if isinstance(recipients_raw, str):
            recipients = (recipients_raw,)
        elif isinstance(recipients_raw, (list, tuple)):
            recipients = tuple(str(value) for value in recipients_raw)
        else:
            raise ValueError("External action recipients must be a list or string")
        content_raw = proposal.get("content", "")
        if isinstance(content_raw, (dict, list)):
            content = _json_value(content_raw)
            media_type = "application/json"
        elif isinstance(content_raw, (str, bytes)):
            content = content_raw
            media_type = str(proposal.get("content_media_type", "text/plain"))
        else:
            raise ValueError("External action content must be text, bytes, or JSON")
        attributes_raw = proposal.get("attributes", {})
        if not isinstance(attributes_raw, Mapping):
            raise ValueError("External action attributes must be an object")
        integration = str(proposal.get("integration", "")).strip()
        if not integration:
            integration = action_type.split(".", 1)[0] or "external"
        return ActionIntent(
            action_type=action_type,
            actor_id=self.actor_id,
            integration=integration,
            recipients=recipients,
            content=content,
            target=target,
            content_media_type=media_type,
            attributes=tuple((str(key), str(value)) for key, value in attributes_raw.items()),
        )

    def get(self, action_id: str) -> StagedAction:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise NotFound(action_id)
        return self._row_to_action(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[StagedAction]:
        with self.store.connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM action_intents WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(limit, 1000))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM action_intents ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
        return [self._row_to_action(row) for row in rows]

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> StagedAction:
        record = json.loads(row["canonical_json"])
        intent = _intent_from_execution(record["execution"])
        if intent.digest != str(row["digest"]):
            raise ApprovalStateError("Stored action intent digest does not match its payload")
        return StagedAction(
            id=str(row["id"]),
            status=str(row["status"]),
            risk=str(row["risk"]),
            reason=str(record.get("reason", "")),
            source_event_id=row["source_event_id"],
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            intent=intent,
            result=json.loads(row["result_json"] or "{}"),
        )

    def approve(self, action_id: str, *, approved_by: str) -> ApprovalGrant:
        now = datetime.now(UTC)
        expired = False
        grant: ApprovalGrant | None = None
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise NotFound(action_id)
            status = str(row["status"])
            if status != "pending_approval":
                raise ApprovalStateError(f"Action is {status}, not pending approval")
            action = self._row_to_action(row)
            action_expiry = _parse_time(action.expires_at)
            if now >= action_expiry:
                connection.execute(
                    "UPDATE action_intents SET status = 'expired' WHERE id = ?",
                    (action_id,),
                )
                self.store.audit(
                    approved_by,
                    "external_action.expired",
                    entity_type="action_intent",
                    entity_id=action_id,
                    connection=connection,
                )
                expired = True
            else:
                ttl = min(self.ttl, action_expiry - now)
                grant = ApprovalGrant.issue(
                    action.intent,
                    approved_by=approved_by,
                    ttl=ttl,
                    now=now,
                )
                self.grants._insert(connection, action_id, grant)
                connection.execute(
                    "UPDATE action_intents SET status = 'approved', approved_at = ? WHERE id = ?",
                    (_time_text(now), action_id),
                )
                self.store.audit(
                    approved_by,
                    "external_action.approved",
                    entity_type="action_intent",
                    entity_id=action_id,
                    data={"grant_id": grant.grant_id, "digest": grant.action_digest},
                    connection=connection,
                )
        if expired:
            raise ApprovalStateError("Action approval window has expired")
        assert grant is not None
        return grant

    def deny(self, action_id: str, *, denied_by: str, reason: str = "") -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise NotFound(action_id)
            status = str(row["status"])
            if status not in {"pending_approval", "approved"}:
                raise ApprovalStateError(f"Action is {status} and cannot be denied")
            consumed = connection.execute(
                "SELECT 1 FROM approval_grants "
                "WHERE intent_id = ? AND consumed_at IS NOT NULL LIMIT 1",
                (action_id,),
            ).fetchone()
            if consumed is not None:
                raise ApprovalStateError("Action authorization was already consumed")
            cursor = connection.execute(
                "UPDATE action_intents SET status = 'denied', result_json = ? "
                "WHERE id = ? AND status IN ('pending_approval', 'approved')",
                (_json_value({"denial_reason": reason}), action_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalStateError("Action state changed concurrently")
            connection.execute(
                "UPDATE approval_grants SET decision = 'revoked' "
                "WHERE intent_id = ? AND consumed_at IS NULL",
                (action_id,),
            )
            self.store.audit(
                denied_by,
                "external_action.denied",
                entity_type="action_intent",
                entity_id=action_id,
                data={"reason": reason},
                connection=connection,
            )

    def _set_status(self, action_id: str, status: str, *, actor: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE action_intents SET status = ? WHERE id = ?", (status, action_id)
            )
            self.store.audit(
                actor,
                f"external_action.{status}",
                entity_type="action_intent",
                entity_id=action_id,
                connection=connection,
            )


class ExternalActionConnector(Protocol):
    def execute(self, action: StagedAction) -> dict[str, Any]: ...


class OutboundBroker:
    """The only component allowed to cross the external side-effect boundary."""

    def __init__(
        self,
        stager: ExternalActionStager,
        *,
        connectors: Mapping[str, ExternalActionConnector] | None = None,
        enabled: bool = False,
        max_grant_lifetime_seconds: int = 3600,
    ) -> None:
        if (
            not isinstance(max_grant_lifetime_seconds, int)
            or isinstance(max_grant_lifetime_seconds, bool)
            or max_grant_lifetime_seconds < 1
        ):
            raise ValueError("max_grant_lifetime_seconds must be a positive integer")
        self.stager = stager
        self.connectors = dict(connectors or {})
        self.enabled = enabled
        self.max_grant_lifetime = timedelta(
            seconds=max_grant_lifetime_seconds
        )

    def execute(self, action_id: str, *, grant_id: str, actor: str) -> dict[str, Any]:
        for name, value, maximum in (
            ("action_id", action_id, 256),
            ("grant_id", grant_id, 512),
            ("actor", actor, 256),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or "\x00" in value
            ):
                raise ApprovalStateError(
                    f"Outbound {name} must be a bounded non-empty string"
                )
        if not self.enabled:
            self._audit_rejection(
                action_id,
                actor=actor,
                grant_id=grant_id,
                reason="Outbound execution is disabled",
            )
            raise ApprovalStateError("Outbound execution is disabled")
        try:
            action = self.stager.get(action_id)
        except (NotFound, ApprovalStateError) as error:
            self._audit_rejection(
                action_id,
                actor=actor,
                grant_id=grant_id,
                reason=str(error),
            )
            raise
        if action.status != "approved":
            self._audit_rejection(
                action.id,
                actor=actor,
                grant_id=grant_id,
                reason=f"Action is {action.status}, not approved",
            )
            raise ApprovalStateError(f"Action is {action.status}, not approved")
        connector = self.connectors.get(action.intent.integration)
        if connector is None:
            self._audit_rejection(
                action.id,
                actor=actor,
                grant_id=grant_id,
                reason=(
                    "No outbound connector is registered for "
                    f"{action.intent.integration}"
                ),
            )
            raise ApprovalStateError(
                f"No outbound connector is registered for {action.intent.integration}"
            )
        try:
            action = self._claim(action.id, grant_id=grant_id, actor=actor)
        except ApprovalStateError as error:
            self._audit_rejection(
                action.id,
                actor=actor,
                grant_id=grant_id,
                reason=str(error),
            )
            raise
        try:
            result = connector.execute(action)
            if not isinstance(result, dict):
                raise TypeError("Outbound connector result must be a JSON object")
            _json_value(result)
        except Exception as error:
            result = {"ok": False, "error": str(error)[:2000]}
            self._finish(action, "execution_failed", result, actor)
            raise
        self._finish(action, "executed", result, actor)
        return result

    def _claim(
        self,
        action_id: str,
        *,
        grant_id: str,
        actor: str,
    ) -> StagedAction:
        """Atomically validate, consume, and claim one exact approved action."""

        now = datetime.now(UTC)
        with self.stager.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            action_row = connection.execute(
                "SELECT * FROM action_intents WHERE id = ?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise NotFound(action_id)
            action = self.stager._row_to_action(action_row)
            if action.status != "approved":
                raise ApprovalStateError(
                    f"Action is {action.status}, not approved"
                )
            grant_row = connection.execute(
                "SELECT * FROM approval_grants WHERE id = ? AND intent_id = ?",
                (grant_id, action_id),
            ).fetchone()
            if grant_row is None:
                raise ApprovalStateError(
                    "Authorization denied: grant_not_found"
                )
            try:
                grant = SQLiteApprovalGrantStore.grant_from_row(grant_row)
            except (KeyError, TypeError, ValueError) as error:
                raise ApprovalStateError(
                    "Authorization denied: security_store_failure"
                ) from error
            reason: ApprovalDecisionReason | None = None
            if grant_row["consumed_at"] is not None:
                reason = ApprovalDecisionReason.GRANT_ALREADY_CONSUMED
            elif grant_row["decision"] == "revoked":
                reason = ApprovalDecisionReason.GRANT_REVOKED
            elif grant_row["decision"] != "approved":
                reason = ApprovalDecisionReason.APPROVAL_REQUIRED
            elif grant.expires_at - grant.issued_at > self.max_grant_lifetime:
                reason = ApprovalDecisionReason.GRANT_LIFETIME_EXCEEDED
            elif now < grant.issued_at:
                reason = ApprovalDecisionReason.GRANT_NOT_YET_VALID
            elif now >= grant.expires_at:
                reason = ApprovalDecisionReason.GRANT_EXPIRED
            elif not grant.matches(action.intent):
                reason = ApprovalDecisionReason.GRANT_BINDING_MISMATCH
            if reason is not None:
                raise ApprovalStateError(
                    f"Authorization denied: {reason.value}"
                )
            consumed = connection.execute(
                "UPDATE approval_grants SET consumed_at = ? "
                "WHERE id = ? AND intent_id = ? AND consumed_at IS NULL "
                "AND decision = 'approved'",
                (_time_text(now), grant_id, action_id),
            )
            claimed = connection.execute(
                "UPDATE action_intents SET status = 'executing' "
                "WHERE id = ? AND status = 'approved'",
                (action_id,),
            )
            if consumed.rowcount != 1 or claimed.rowcount != 1:
                raise ApprovalStateError(
                    "Action or approval state changed during execution claim"
                )
            self.stager.store.audit(
                actor,
                "external_action.executing",
                entity_type="action_intent",
                entity_id=action.id,
                data={
                    "grant_id": grant_id,
                    "digest": action.intent.digest,
                    "integration": action.intent.integration,
                    "recipients_digest": action.intent.recipients_digest,
                    "content_digest": action.intent.content_digest,
                },
                connection=connection,
            )
        return action

    def _audit_rejection(
        self,
        action_id: str,
        *,
        actor: str,
        grant_id: str,
        reason: str,
    ) -> None:
        with self.stager.store.connection() as connection:
            self.stager.store.audit(
                actor,
                "external_action.execution_rejected",
                entity_type="action_intent",
                entity_id=action_id,
                data={
                    "grant_id_sha256": hashlib.sha256(
                        grant_id.encode("utf-8")
                    ).hexdigest(),
                    "reason": reason[:500],
                },
                connection=connection,
            )

    def _finish(
        self, action: StagedAction, status: str, result: dict[str, Any], actor: str
    ) -> None:
        with self.stager.store.connection() as connection:
            cursor = connection.execute(
                "UPDATE action_intents SET status = ?, executed_at = ?, result_json = ? "
                "WHERE id = ? AND status = 'executing'",
                (status, utc_now(), _json_value(result), action.id),
            )
            if cursor.rowcount != 1:
                raise ApprovalStateError(
                    "Action execution result could not be committed"
                )
            self.stager.store.audit(
                actor,
                f"external_action.{status}",
                entity_type="action_intent",
                entity_id=action.id,
                data={
                    "digest": action.intent.digest,
                    "integration": action.intent.integration,
                    "result": result,
                },
                connection=connection,
            )


KNOWN_ACTION_TYPES = tuple(action.value for action in ExternalActionType)
