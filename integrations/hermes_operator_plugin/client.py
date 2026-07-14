"""Small standard-library client for the operator control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Mapping
from urllib import error, parse, request

from .config import PluginConfig


class OperatorUnavailable(RuntimeError):
    """The control plane could not answer a plugin request."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    data: Any


class _NoRedirect(request.HTTPRedirectHandler):
    """Keep Bearer credentials on the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


class OperatorClient:
    """Bounded HTTP client that exposes only the plugin's narrow contract."""

    def __init__(self, config: PluginConfig):
        self.config = config

    def health(self) -> Any:
        data = self._request("GET", "/v1/hermes/status").data
        counters = data.get("operational_counters") if isinstance(data, Mapping) else None
        event_counts = counters.get("events") if isinstance(counters, Mapping) else None
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("status"), str)
            or not isinstance(data.get("running"), bool)
            or not isinstance(data.get("cycle_count"), int)
            or isinstance(data.get("cycle_count"), bool)
            or not isinstance(data.get("as_of"), str)
            or not isinstance(counters, Mapping)
            or not isinstance(event_counts, Mapping)
            or any(
                not isinstance(event_counts.get(name), int)
                or isinstance(event_counts.get(name), bool)
                for name in ("pending", "processing", "failed", "dead_letter")
            )
            or any(
                not isinstance(counters.get(name), int)
                or isinstance(counters.get(name), bool)
                for name in ("pending_questions", "active_work", "active_runs")
            )
        ):
            raise OperatorUnavailable("operator returned an invalid status report")
        return data

    def next_work(self, limit: int = 5) -> Any:
        safe_limit = max(1, min(int(limit), 20))
        query = parse.urlencode({"limit": safe_limit})
        return self._request("GET", f"/v1/next?{query}").data

    def open_questions(self, limit: int = 10) -> Any:
        safe_limit = max(1, min(int(limit), 50))
        query = parse.urlencode({"status": "pending", "limit": safe_limit})
        return self._request("GET", f"/v1/questions?{query}").data

    def due_reminders(self, limit: int = 20) -> Any:
        """Preview due reminders without consuming their delivery claim."""

        safe_limit = max(1, min(int(limit), 100))
        query = parse.urlencode({"limit": safe_limit})
        data = self._request("GET", f"/v1/hermes/reminders?{query}").data
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("items"), list)
            or not isinstance(data.get("count"), int)
            or not isinstance(data.get("as_of"), str)
            or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                for item in data.get("items", [])
            )
        ):
            raise OperatorUnavailable("operator returned an invalid reminder list")
        return data

    def claim_attention(self, limit: int = 20) -> Any:
        """Atomically claim due reminders and pending questions for delivery."""

        safe_limit = max(1, min(int(limit), 100))
        data = self._request(
            "POST",
            "/v1/hermes/attention/claim",
            {"limit": safe_limit},
        ).data
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("reminders"), list)
            or not isinstance(data.get("questions"), list)
            or not isinstance(data.get("count"), int)
            or not isinstance(data.get("as_of"), str)
            or not isinstance(data.get("redelivery_seconds"), int)
            or data["count"] != len(data["reminders"]) + len(data["questions"])
            or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                for item in [*data["reminders"], *data["questions"]]
            )
        ):
            raise OperatorUnavailable("operator returned an invalid attention claim")
        return data

    def create_work(
        self,
        *,
        title: str,
        description: str = "",
        kind: str = "task",
        due_at: str | None = None,
        parent_id: str | None = None,
        recurrence_rule: str | None = None,
    ) -> Any:
        """Create one bridge-scoped triage item with no execution authority."""

        title = _bounded_text(title, "title", 500, required=True)
        description = _bounded_text(description, "description", 20_000)
        if kind not in {
            "area",
            "goal",
            "project",
            "milestone",
            "task",
            "todo",
            "reminder",
            "decision",
        }:
            raise ValueError("kind is not a supported work type")
        due_at = _optional_timestamp(due_at, "due_at")
        parent_id = _optional_identity(parent_id, "parent_id")
        recurrence_rule = _optional_recurrence_rule(recurrence_rule)
        if recurrence_rule is not None and kind != "reminder":
            raise ValueError("recurrence_rule is supported only for reminder work")
        if recurrence_rule is not None and due_at is None:
            raise ValueError("a recurring reminder requires due_at")
        data = self._request(
            "POST",
            "/v1/hermes/work",
            {
                "title": title,
                "description": description,
                "kind": kind,
                "due_at": due_at,
                "parent_id": parent_id,
                "recurrence_rule": recurrence_rule,
            },
        ).data
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("event_id"), str)
            or not isinstance(data.get("work"), Mapping)
            or not isinstance(data["work"].get("id"), str)
        ):
            raise OperatorUnavailable("operator returned an invalid work item")
        return data

    def answer_question(self, question_id: str, answer: str) -> Any:
        """Record a user-confirmed answer through the scoped Hermes bridge."""

        question_id = _required_identity(question_id, "question_id")
        answer = _bounded_text(answer, "answer", 20_000, required=True)
        path = f"/v1/hermes/questions/{parse.quote(question_id, safe='')}/answer"
        data = self._request(
            "POST",
            path,
            {"answer": answer},
            proof_purpose="human.answer_question",
        ).data
        if (
            not isinstance(data, Mapping)
            or data.get("id") != question_id
            or data.get("status") != "answered"
            or not isinstance(data.get("answer"), str)
        ):
            raise OperatorUnavailable("operator returned an invalid question result")
        return data

    def authorization_scope(
        self,
        work_id: str,
        *,
        profile: str | None = None,
        skills: list[str] | None = None,
        goal_mode: bool | None = None,
    ) -> Any:
        """Read the exact current scope fence for one proposed execution shape."""

        work_id = _required_identity(work_id, "work_id")
        query: list[tuple[str, str]] = []
        normalized_profile: str | None = None
        normalized_skills: list[str] | None = None
        if profile is not None:
            normalized_profile = _bounded_text(
                profile, "profile", 128, required=True
            ).strip()
            query.append(("profile", normalized_profile))
        if skills is not None:
            if (
                not isinstance(skills, list)
                or len(skills) > 64
                or len(set(skills)) != len(skills)
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 128
                    for value in skills
                )
            ):
                raise ValueError("skills must be unique nonempty bounded strings")
            normalized_skills = [value.strip() for value in skills]
            query.extend(("skill", value) for value in normalized_skills)
        if goal_mode is not None:
            if not isinstance(goal_mode, bool):
                raise ValueError("goal_mode must be a boolean")
            query.append(("goal_mode", "true" if goal_mode else "false"))
        path = (
            f"/v1/hermes/work/{parse.quote(work_id, safe='')}/authorization-scope"
        )
        if query:
            path = f"{path}?{parse.urlencode(query)}"
        data = self._request("GET", path).data
        expected_keys = {
            "authorization_scope_digest",
            "authorization_scope_revision",
            "authorizable",
            "default_skills",
            "goal_mode",
            "profile",
            "scope",
            "skills",
            "status",
            "work_id",
            "work_version",
        }
        scope_keys = {
            "acceptance_criteria",
            "description",
            "due_at",
            "effective_skills",
            "goal_mode",
            "kind",
            "parent_id",
            "profile",
            "recurrence_rule",
            "scheduled_at",
            "schema",
            "scope_revision",
            "title",
            "verification_contract",
            "work_id",
        }
        scope = data.get("scope") if isinstance(data, Mapping) else None
        if (
            not isinstance(data, Mapping)
            or set(data) != expected_keys
            or data.get("work_id") != work_id
            or not isinstance(data.get("work_version"), int)
            or isinstance(data.get("work_version"), bool)
            or data["work_version"] < 1
            or not isinstance(data.get("status"), str)
            or not data["status"]
            or not isinstance(data.get("authorizable"), bool)
            or not isinstance(data.get("authorization_scope_revision"), int)
            or isinstance(data.get("authorization_scope_revision"), bool)
            or data["authorization_scope_revision"] < 1
            or not isinstance(data.get("authorization_scope_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["authorization_scope_digest"])
            or not isinstance(data.get("profile"), str)
            or not data["profile"]
            or not _valid_skill_list(data.get("skills"))
            or not _valid_skill_list(data.get("default_skills"))
            or not isinstance(data.get("goal_mode"), bool)
            or not isinstance(scope, Mapping)
            or set(scope) != scope_keys
            or scope.get("work_id") != work_id
            or scope.get("scope_revision") != data["authorization_scope_revision"]
            or scope.get("profile") != data["profile"]
            or scope.get("effective_skills")
            != sorted(set([*data["default_skills"], *data["skills"]]))
            or scope.get("goal_mode") != data["goal_mode"]
            or (normalized_profile is not None and data["profile"] != normalized_profile)
            or (normalized_skills is not None and data["skills"] != normalized_skills)
            or (goal_mode is not None and data["goal_mode"] is not goal_mode)
        ):
            raise OperatorUnavailable("operator returned an invalid authorization scope")
        return data

    def authorize_work(
        self,
        work_id: str,
        expected_version: int,
        expected_scope_revision: int,
        expected_scope_digest: str,
        reason: str = "",
        *,
        profile: str | None = None,
        skills: list[str] | None = None,
        goal_mode: bool | None = None,
    ) -> Any:
        """Authorize one exact version, graph scope, and execution shape."""

        work_id = _required_identity(work_id, "work_id")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        if (
            not isinstance(expected_scope_revision, int)
            or isinstance(expected_scope_revision, bool)
            or expected_scope_revision < 1
        ):
            raise ValueError("expected_scope_revision must be a positive integer")
        if not isinstance(expected_scope_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_scope_digest
        ):
            raise ValueError("expected_scope_digest must be a lowercase SHA-256 hex value")
        reason = _bounded_text(reason, "reason", 2_000)
        body: dict[str, Any] = {
            "expected_version": expected_version,
            "expected_scope_revision": expected_scope_revision,
            "expected_scope_digest": expected_scope_digest,
        }
        if reason.strip():
            body["reason"] = reason
        if profile is not None:
            body["profile"] = _bounded_text(
                profile, "profile", 128, required=True
            ).strip()
        if skills is not None:
            if not isinstance(skills, list) or any(
                not isinstance(value, str) or not value.strip() or len(value) > 128
                for value in skills
            ):
                raise ValueError("skills must be a list of nonempty bounded strings")
            body["skills"] = [value.strip() for value in skills]
        if goal_mode is not None:
            if not isinstance(goal_mode, bool):
                raise ValueError("goal_mode must be a boolean")
            body["goal_mode"] = goal_mode
        path = f"/v1/hermes/work/{parse.quote(work_id, safe='')}/authorize"
        data = self._request(
            "POST",
            path,
            body,
            proof_purpose="human.authorize_work",
        ).data
        expected_keys = {
            "authorization_scope_digest",
            "authorization_scope_revision",
            "created",
            "event_id",
            "goal_mode",
            "profile",
            "skills",
            "work_id",
            "work_version",
        }
        if (
            not isinstance(data, Mapping)
            or set(data) != expected_keys
            or data.get("work_id") != work_id
            or data.get("work_version") != expected_version
            or data.get("authorization_scope_revision") != expected_scope_revision
            or data.get("authorization_scope_digest") != expected_scope_digest
            or not isinstance(data.get("authorization_scope_revision"), int)
            or isinstance(data.get("authorization_scope_revision"), bool)
            or data["authorization_scope_revision"] < 1
            or not isinstance(data.get("authorization_scope_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["authorization_scope_digest"])
            or not isinstance(data.get("profile"), str)
            or not data["profile"]
            or not isinstance(data.get("skills"), list)
            or any(
                not isinstance(value, str) or not value
                for value in data["skills"]
            )
            or not isinstance(data.get("goal_mode"), bool)
            or not isinstance(data.get("event_id"), str)
            or not isinstance(data.get("created"), bool)
        ):
            raise OperatorUnavailable("operator returned an invalid authorization result")
        return data

    def update_work(
        self,
        work_id: str,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Any:
        """Apply one optimistic, non-authorizing work update."""

        work_id = _required_identity(work_id, "work_id")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        normalized_changes = _validated_work_changes(changes)
        path = f"/v1/hermes/work/{parse.quote(work_id, safe='')}/update"
        data = self._request(
            "POST",
            path,
            {"expected_version": expected_version, "changes": normalized_changes},
            proof_purpose="human.update_work",
        ).data
        work = data.get("work") if isinstance(data, Mapping) else None
        if (
            not isinstance(work, Mapping)
            or work.get("id") != work_id
            or not isinstance(work.get("version"), int)
        ):
            raise OperatorUnavailable("operator returned an invalid work update")
        return data

    def resolve_reminder(
        self,
        work_id: str,
        expected_version: int,
        action: str,
        *,
        until: str | None = None,
    ) -> Any:
        """Apply a reminder lifecycle action without changing its schedule anchor."""

        work_id = _required_identity(work_id, "work_id")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        if action not in {"snooze", "acknowledge", "complete"}:
            raise ValueError("action must be snooze, acknowledge, or complete")
        body: dict[str, Any] = {
            "expected_version": expected_version,
            "action": action,
        }
        if action == "snooze":
            body["until"] = _optional_timestamp(until, "until")
            if body["until"] is None:
                raise ValueError("snooze requires an ISO 8601 until timestamp")
        elif until is not None:
            raise ValueError("until is accepted only for snooze")
        path = f"/v1/hermes/work/{parse.quote(work_id, safe='')}/reminder"
        data = self._request(
            "POST",
            path,
            body,
            proof_purpose="human.resolve_reminder",
        ).data
        work = data.get("work") if isinstance(data, Mapping) else None
        if (
            not isinstance(work, Mapping)
            or work.get("id") != work_id
            or not isinstance(work.get("version"), int)
        ):
            raise OperatorUnavailable("operator returned an invalid reminder result")
        return data

    def ingest_inbound(self, source: str, events: list[Mapping[str, Any]]) -> Any:
        """Record bounded provider reads obtained through Hermes native skills."""

        if source not in {"google.gmail", "google.calendar", "google.meeting"}:
            raise ValueError(
                "source must be google.gmail, google.calendar, or google.meeting"
            )
        if not isinstance(events, list) or not 1 <= len(events) <= 50:
            raise ValueError("events must contain between 1 and 50 items")
        normalized: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError("every inbound event must be an object")
            allowed = {"event_type", "external_id", "revision", "payload"}
            if not set(event) <= allowed or not {"event_type", "external_id", "payload"} <= set(event):
                raise ValueError("inbound event fields do not match the fixed contract")
            event_type = _bounded_text(
                event.get("event_type"), "event_type", 128, required=True
            )
            external_id = _bounded_text(
                event.get("external_id"), "external_id", 500, required=True
            )
            revision = event.get("revision")
            if revision is not None:
                revision = _bounded_text(revision, "revision", 128, required=True)
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError("inbound payload must be an object")
            item = {
                "event_type": event_type,
                "external_id": external_id,
                "payload": _json_safe(payload),
            }
            if revision is not None:
                item["revision"] = revision
            normalized.append(item)
        data = self._request(
            "POST",
            "/v1/hermes/inbound",
            {"source": source.strip().lower(), "events": normalized},
        ).data
        if (
            not isinstance(data, Mapping)
            or data.get("source") != source.strip().lower()
            or not isinstance(data.get("created"), int)
            or not isinstance(data.get("items"), list)
        ):
            raise OperatorUnavailable("operator returned an invalid inbound result")
        for item in data["items"]:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("external_id"), str)
                or not isinstance(item.get("event_id"), str)
                or not isinstance(item.get("created"), bool)
            ):
                raise OperatorUnavailable("operator returned an invalid inbound result")
        return data

    def execution_contract(self, task_id: str) -> Mapping[str, Any]:
        """Read and validate the exact live authorization for one Hermes task."""

        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id
        ):
            raise ValueError("task_id must be a valid Hermes task identity")
        query = parse.urlencode({"task_id": task_id})
        data = self._request(
            "GET", f"/v1/hermes/execution-contract?{query}"
        ).data
        if not isinstance(data, Mapping):
            raise OperatorUnavailable("operator returned an invalid execution contract")
        if data.get("authorized") is not True or data.get("task_id") != task_id:
            raise OperatorUnavailable("operator did not authorize the current Hermes task")
        expected = {
            "authorized",
            "contract_digest",
            "internal_capabilities",
            "profile",
            "run_id",
            "task_id",
            "work_id",
        }
        if set(data) != expected:
            raise OperatorUnavailable("operator execution contract has an invalid shape")
        for key in ("profile", "run_id", "work_id"):
            value = data.get(key)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise OperatorUnavailable(
                    f"operator execution contract has an invalid {key}"
                )
        digest = data.get("contract_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise OperatorUnavailable(
                "operator execution contract has an invalid contract_digest"
            )
        capabilities = data.get("internal_capabilities")
        known = {
            "delegate_task",
            "local_build",
            "local_read",
            "local_test",
            "local_write",
        }
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or len(capabilities) > len(known)
            or len(set(capabilities)) != len(capabilities)
            or any(not isinstance(value, str) or value not in known for value in capabilities)
        ):
            raise OperatorUnavailable(
                "operator execution contract has invalid internal_capabilities"
            )
        return dict(data)

    def claim_delegation(
        self,
        task_id: str,
        requested_children: int,
    ) -> Mapping[str, Any]:
        """Atomically consume one bounded subagent batch for the live run."""

        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id
        ):
            raise ValueError("task_id must be a valid Hermes task identity")
        if (
            not isinstance(requested_children, int)
            or isinstance(requested_children, bool)
            or not 1 <= requested_children <= 3
        ):
            raise ValueError("requested_children must be an integer from 1 through 3")
        data = self._request(
            "POST",
            "/v1/hermes/delegation-claim",
            {
                "task_id": task_id,
                "requested_children": requested_children,
            },
        ).data
        expected = {
            "claimed",
            "contract_digest",
            "reason",
            "requested_children",
            "run_id",
            "task_id",
        }
        if not isinstance(data, Mapping) or set(data) != expected:
            raise OperatorUnavailable("operator returned an invalid delegation claim")
        if (
            not isinstance(data.get("claimed"), bool)
            or data.get("task_id") != task_id
            or data.get("requested_children") != requested_children
            or not isinstance(data.get("run_id"), str)
            or not data["run_id"]
            or not isinstance(data.get("contract_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["contract_digest"])
            or not isinstance(data.get("reason"), str)
            or not data["reason"]
            or len(data["reason"]) > 128
        ):
            raise OperatorUnavailable("operator returned an invalid delegation claim")
        return dict(data)

    def emit_lifecycle(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        *,
        identity_parts: tuple[str, ...] = (),
    ) -> Any:
        """Record an internal lifecycle event. This cannot approve or send work."""

        safe_event = "".join(
            character
            for character in event_name.lower()
            if character.isalnum() or character in {"_", "."}
        )
        if not safe_event:
            raise ValueError("event_name must contain a safe character")
        occurred_at = datetime.now(timezone.utc).isoformat()
        canonical_identity = json.dumps(
            [safe_event, *identity_parts], separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_identity).hexdigest()
        body = {
            "source": "hermes_plugin",
            "event_type": f"hermes.{safe_event}",
            "external_id": f"hermes:{digest}",
            "dedupe_key": f"hermes:{digest}",
            "occurred_at": occurred_at,
            "payload": _json_safe(payload),
            "provenance": {
                "origin": "hermes_plugin",
                "trust": "authenticated_untrusted",
            },
        }
        return self._request("POST", "/v1/events/hermes", body).data

    def attest_policy(self, payload: Mapping[str, Any]) -> Any:
        """Synchronously record the required per-profile worker policy attestation."""

        required = {
            "profile",
            "plugin_version",
            "policy_version",
            "policy_digest",
            "guard_active",
            "policy_mode",
            "attested_at",
        }
        if set(payload) != required:
            raise ValueError("policy attestation payload does not match the fixed contract")
        if payload.get("guard_active") is not True:
            raise ValueError("policy attestation requires an active guard")
        if payload.get("policy_mode") != "default_deny":
            raise ValueError("policy attestation requires default-deny mode")
        for key in required - {"guard_active"}:
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"policy attestation field {key} must be a non-empty string")
        if payload["profile"] != self.config.profile:
            raise ValueError("policy attestation profile does not match bridge configuration")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["policy_digest"]):
            raise ValueError("policy attestation digest must be a lowercase SHA-256 hex value")
        try:
            attested_at = datetime.fromisoformat(payload["attested_at"])
        except ValueError as exc:
            raise ValueError("policy attestation timestamp must be ISO 8601") from exc
        if attested_at.tzinfo is None or attested_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("policy attestation timestamp must include a UTC offset")

        identity = json.dumps(
            [
                payload["profile"],
                payload["plugin_version"],
                payload["policy_version"],
                payload["policy_digest"],
                payload["attested_at"],
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        event_id = f"hermes-policy:{hashlib.sha256(identity).hexdigest()}"
        body = {
            "source": "hermes_plugin",
            "event_type": "policy.attested",
            "external_id": event_id,
            "dedupe_key": event_id,
            "occurred_at": payload["attested_at"],
            "payload": dict(payload),
            "provenance": {
                "origin": "hermes_plugin",
                "trust": "authenticated_untrusted",
            },
        }
        response = self._request(
            "POST",
            "/v1/events/hermes",
            body,
            proof_purpose="policy.attest",
        ).data
        if (
            not isinstance(response, Mapping)
            or not isinstance(response.get("event_id"), str)
            or not response["event_id"].strip()
            or not isinstance(response.get("created"), bool)
            or response.get("trust_level") != "authenticated_untrusted"
        ):
            raise OperatorUnavailable(
                "operator did not acknowledge the authenticated policy attestation"
            )
        return response

    def revoke_policy(self, payload: Mapping[str, Any]) -> Any:
        """Best-effort negative policy evidence for an incompatible local host.

        This uses a separate fixed event contract so a failed compatibility check can
        never be mistaken for a positive attestation. The control plane can invalidate
        an older fresh attestation immediately instead of waiting for its TTL.
        """

        required = {
            "profile",
            "plugin_version",
            "policy_version",
            "policy_digest",
            "guard_active",
            "policy_mode",
            "attested_at",
            "reason",
        }
        if set(payload) != required:
            raise ValueError("policy revocation payload does not match the fixed contract")
        if payload.get("guard_active") is not False:
            raise ValueError("policy revocation requires an inactive bridge")
        if payload.get("policy_mode") != "default_deny":
            raise ValueError("policy revocation requires default-deny mode")
        for key in required - {"guard_active"}:
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"policy revocation field {key} must be a non-empty string")
        if payload["profile"] != self.config.profile:
            raise ValueError("policy revocation profile does not match bridge configuration")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["policy_digest"]):
            raise ValueError("policy revocation digest must be a lowercase SHA-256 hex value")
        if len(payload["reason"]) > 512:
            raise ValueError("policy revocation reason exceeds 512 characters")
        try:
            attested_at = datetime.fromisoformat(payload["attested_at"])
        except ValueError as exc:
            raise ValueError("policy revocation timestamp must be ISO 8601") from exc
        if attested_at.tzinfo is None or attested_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("policy revocation timestamp must include a UTC offset")

        identity = json.dumps(
            [
                "revoked",
                payload["profile"],
                payload["plugin_version"],
                payload["policy_version"],
                payload["policy_digest"],
                payload["attested_at"],
                payload["reason"],
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        event_id = f"hermes-policy:{hashlib.sha256(identity).hexdigest()}"
        body = {
            "source": "hermes_plugin",
            "event_type": "policy.revoked",
            "external_id": event_id,
            "dedupe_key": event_id,
            "occurred_at": payload["attested_at"],
            "payload": dict(payload),
            "provenance": {
                "origin": "hermes_plugin",
                "trust": "authenticated_untrusted",
            },
        }
        response = self._request(
            "POST",
            "/v1/events/hermes",
            body,
            proof_purpose="policy.revoke",
        ).data
        if (
            not isinstance(response, Mapping)
            or not isinstance(response.get("event_id"), str)
            or not response["event_id"].strip()
            or not isinstance(response.get("created"), bool)
            or response.get("trust_level") != "authenticated_untrusted"
        ):
            raise OperatorUnavailable(
                "operator did not acknowledge the authenticated policy revocation"
            )
        return response

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        proof_purpose: str | None = None,
    ) -> Response:
        endpoint = path.split("?", 1)[0]
        allowed = {
            ("GET", "/v1/hermes/status"),
            ("GET", "/v1/hermes/execution-contract"),
            ("GET", "/v1/next"),
            ("GET", "/v1/questions"),
            ("GET", "/v1/hermes/reminders"),
            ("POST", "/v1/hermes/attention/claim"),
            ("POST", "/v1/hermes/delegation-claim"),
            ("POST", "/v1/hermes/inbound"),
            ("POST", "/v1/hermes/work"),
            ("POST", "/v1/events/hermes"),
        }
        normalized_method = method.upper()
        bridge_scope_read = bool(
            normalized_method == "GET"
            and re.fullmatch(
                r"/v1/hermes/work/[^/]+/authorization-scope", endpoint
            )
        )
        bridge_mutation = bool(
            normalized_method == "POST"
            and (
                re.fullmatch(r"/v1/hermes/questions/[^/]+/answer", endpoint)
                or re.fullmatch(r"/v1/hermes/work/[^/]+/authorize", endpoint)
                or re.fullmatch(r"/v1/hermes/work/[^/]+/update", endpoint)
                or re.fullmatch(r"/v1/hermes/work/[^/]+/reminder", endpoint)
            )
        )
        if (
            (normalized_method, endpoint) not in allowed
            and not bridge_scope_read
            and not bridge_mutation
        ):
            raise ValueError("plugin request is outside its read and observation contract")
        if "#" in path:
            raise ValueError("fragments are not permitted in API paths")

        encoded = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "hermes-operator-plugin/1.6.0",
        }
        if body is not None:
            encoded = json.dumps(
                body, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        if proof_purpose is not None:
            headers.update(
                self._proof_headers(
                    normalized_method,
                    endpoint,
                    encoded or b"",
                    proof_purpose,
                )
            )

        req = request.Request(
            f"{self.config.base_url}{path}",
            data=encoded,
            headers=headers,
            method=normalized_method,
        )
        try:
            opener = request.build_opener(_NoRedirect())
            with opener.open(req, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise OperatorUnavailable("operator response exceeded size limit")
                return Response(response.status, _decode_json(raw))
        except error.HTTPError as exc:
            exc.read(2_048)
            raise OperatorUnavailable(f"operator returned HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise OperatorUnavailable(f"operator is unavailable: {exc}") from exc

    def _proof_headers(
        self,
        method: str,
        endpoint: str,
        encoded_body: bytes,
        purpose: str,
    ) -> dict[str, str]:
        secret = self.config.proof_secret
        if not secret:
            raise OperatorUnavailable(
                "operator bridge proof secret is required for this authority-bearing request"
            )
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", purpose):
            raise ValueError("bridge proof purpose is invalid")
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        body_digest = hashlib.sha256(encoded_body).hexdigest()
        canonical = "\n".join(
            ("v1", timestamp, nonce, purpose, method, endpoint, body_digest)
        ).encode("utf-8")
        signature = hmac.new(
            secret.encode("utf-8"), canonical, hashlib.sha256
        ).hexdigest()
        return {
            "X-Hermes-Operator-Proof": signature,
            "X-Hermes-Operator-Proof-Nonce": nonce,
            "X-Hermes-Operator-Proof-Timestamp": timestamp,
        }


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OperatorUnavailable("operator returned invalid JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Bound hook payloads and remove objects that cannot cross the API safely."""

    if depth > 5:
        return "[depth limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:8_000]
    if isinstance(value, Mapping):
        return {
            str(key)[:100]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:100]]
    return str(value)[:1_000]


def _bounded_text(
    value: Any, name: str, maximum: int, *, required: bool = False
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip() if required else value
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return normalized


def _valid_skill_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and len(value) <= 64
        and all(
            isinstance(item, str) and item.strip() and len(item) <= 128
            for item in value
        )
    ) or value == []


def _required_identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value.strip()
    ):
        raise ValueError(f"{name} must be a valid identifier")
    return value.strip()


def _optional_identity(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    return _required_identity(value, name)


def _optional_timestamp(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError(f"{name} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value


def _validated_work_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("changes must be a nonempty object")
    allowed = {
        "title",
        "description",
        "status",
        "parent_id",
        "due_at",
        "scheduled_at",
        "recurrence_rule",
        "priority",
    }
    if not set(changes) <= allowed:
        raise ValueError("changes contains an unsupported field")
    normalized: dict[str, Any] = {}
    if "title" in changes:
        normalized["title"] = _bounded_text(
            changes["title"], "title", 500, required=True
        )
    if "description" in changes:
        normalized["description"] = _bounded_text(
            changes["description"], "description", 20_000
        )
    if "status" in changes:
        status = changes["status"]
        statuses = {
            "inbox",
            "triage",
            "planned",
            "ready",
            "running",
            "waiting_input",
            "blocked",
            "review",
            "done",
            "cancelled",
            "archived",
        }
        if status not in statuses:
            raise ValueError("status is not supported")
        normalized["status"] = status
    if "parent_id" in changes:
        normalized["parent_id"] = _optional_identity(changes["parent_id"], "parent_id")
    for key in ("due_at", "scheduled_at"):
        if key in changes:
            normalized[key] = _optional_timestamp(changes[key], key)
    if "recurrence_rule" in changes:
        normalized["recurrence_rule"] = _optional_recurrence_rule(
            changes["recurrence_rule"]
        )
    if "priority" in changes:
        priority = changes["priority"]
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not -1_000 <= priority <= 1_000
        ):
            raise ValueError("priority must be an integer from -1000 through 1000")
        normalized["priority"] = priority
    return normalized


_RECURRENCE_PATTERN = re.compile(
    r"^P(?:(?P<weeks>[1-9][0-9]*)W|(?P<days>[1-9][0-9]*)D|"
    r"T(?:(?P<hours>[1-9][0-9]*)H|(?P<minutes>[1-9][0-9]*)M))$"
)


def _optional_recurrence_rule(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("recurrence_rule must be a string or null")
    normalized = value.strip().upper()
    match = _RECURRENCE_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "recurrence_rule must be PTnM, PTnH, PnD, or PnW with a positive integer"
        )
    limits = {"minutes": 5_256_000, "hours": 87_600, "days": 3_650, "weeks": 521}
    for unit, raw in match.groupdict().items():
        if raw is not None and int(raw) > limits[unit]:
            raise ValueError("recurrence_rule cannot exceed 3650 days")
    return normalized
