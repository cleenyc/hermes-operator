from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .approvals import ApprovalStateError, ExternalActionStager
from .authority import execution_scope_binding
from .db import NotFound, SQLiteStore, StateConflict
from .inbound import (
    InboundValidationError,
    normalize_external_event,
    normalize_operator_event,
    verify_webhook_signature,
)
from .models import QuestionStatus, WorkItem, WorkRelation, WorkStatus, utc_now
from .models import Event, ExecutionMode, TrustLevel, WorkKind


WakeCallback = Callable[[str], None]
HealthProvider = Callable[[], Mapping[str, Any]]
NextProvider = Callable[[int], Sequence[WorkItem | Mapping[str, Any]]]
ExecutionContractProvider = Callable[[str], Mapping[str, Any]]
DelegationClaimProvider = Callable[[str, int], Mapping[str, Any]]
RESERVED_WEBHOOK_SOURCES = frozenset({"operator", "system", "hermes"})


@dataclass(slots=True)
class APIContext:
    store: SQLiteStore
    api_token: str = ""
    bridge_token: str = ""
    webhook_secrets: Mapping[str, str] = field(default_factory=dict)
    max_body_bytes: int = 1_048_576
    wake: WakeCallback | None = None
    health_provider: HealthProvider | None = None
    next_provider: NextProvider | None = None
    execution_contract_provider: ExecutionContractProvider | None = None
    delegation_claim_provider: DelegationClaimProvider | None = None
    action_stager: ExternalActionStager | None = None
    allow_unsigned_webhooks: bool = False
    attention_redelivery_seconds: int = 3600
    authorization_profile: str = "operator"
    authorization_default_skills: Sequence[str] = field(default_factory=tuple)
    authorization_goal_mode: bool = False

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if (
            isinstance(self.attention_redelivery_seconds, bool)
            or not isinstance(self.attention_redelivery_seconds, int)
            or self.attention_redelivery_seconds < 1
        ):
            raise ValueError("attention_redelivery_seconds must be a positive integer")
        if (
            self.api_token
            and self.bridge_token
            and hmac.compare_digest(self.api_token, self.bridge_token)
        ):
            raise ValueError("Operator and Hermes bridge tokens must be distinct")
        if self.authorization_profile and not self.authorization_profile.strip():
            raise ValueError("authorization_profile must be empty or nonempty text")
        if not all(
            isinstance(value, str) and value.strip()
            for value in self.authorization_default_skills
        ):
            raise ValueError(
                "authorization_default_skills must contain nonempty strings"
            )
        reserved = RESERVED_WEBHOOK_SOURCES.intersection(self.webhook_secrets)
        if reserved:
            raise ValueError(
                "Reserved webhook sources cannot have HMAC secrets: "
                + ", ".join(sorted(reserved))
            )


class APIError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class OperatorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class OperatorHTTPServerV6(OperatorHTTPServer):
    address_family = socket.AF_INET6


def create_api_server(host: str, port: int, context: APIContext) -> OperatorHTTPServer:
    """Create, but do not start, a portable operator HTTP server."""

    handler = _handler_for(context)
    server_type = OperatorHTTPServerV6 if ":" in host else OperatorHTTPServer
    return server_type((host, port), handler)


class APIService:
    """Small lifecycle wrapper suitable for the main service or tests."""

    def __init__(self, host: str, port: int, context: APIContext):
        self.host = host
        self.port = port
        self.context = context
        self.server: OperatorHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        if self.server is None:
            return self.host, self.port
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return self.address
            if self.server is None:
                self.server = create_api_server(self.host, self.port, self.context)
            server = self.server
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="hermes-operator-api",
                daemon=True,
            )
            self._thread.start()
            return self.address

    def stop(self) -> None:
        with self._lifecycle_lock:
            server = self.server
            thread = self._thread
            if server is None:
                return
            if thread and thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()
            self.server = None
            self._thread = None


def _validated_policy_attestation(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the fixed Hermes plugin attestation envelope and payload."""

    envelope_keys = {
        "source",
        "event_type",
        "external_id",
        "dedupe_key",
        "occurred_at",
        "payload",
        "provenance",
    }
    if set(document) != envelope_keys:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation envelope does not match the fixed contract",
        )
    if document.get("source") != "hermes_plugin":
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation source is invalid",
        )
    if document.get("provenance") != {
        "origin": "hermes_plugin",
        "trust": "authenticated_untrusted",
    }:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation provenance is invalid",
        )
    payload = document.get("payload")
    required = {
        "profile",
        "plugin_version",
        "policy_version",
        "policy_digest",
        "guard_active",
        "policy_mode",
        "attested_at",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation payload does not match the fixed contract",
        )
    for key in required - {"guard_active"}:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_policy_attestation",
                f"Policy attestation field {key} is invalid",
            )
    if payload.get("guard_active") is not True or payload.get("policy_mode") != "default_deny":
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation must report an active default-deny guard",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("policy_digest", ""))):
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation digest must be lowercase SHA-256 hex",
        )
    try:
        attested_at = datetime.fromisoformat(str(payload["attested_at"]))
    except ValueError as error:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation timestamp is invalid",
        ) from error
    if (
        attested_at.tzinfo is None
        or attested_at.utcoffset() != timedelta(0)
        or attested_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=60)
        or document.get("occurred_at") != payload["attested_at"]
    ):
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation timestamp must be current UTC evidence",
        )
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
    expected_id = f"hermes-policy:{hashlib.sha256(identity).hexdigest()}"
    if (
        document.get("external_id") != expected_id
        or document.get("dedupe_key") != expected_id
    ):
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "invalid_policy_attestation",
            "Policy attestation identity digest is invalid",
        )
    return dict(payload)


def _handler_for(context: APIContext) -> type[BaseHTTPRequestHandler]:
    class OperatorAPIHandler(BaseHTTPRequestHandler):
        server_version = "HermesOperator/0.1"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _dispatch(self, method: str) -> None:
            self._request_id = self.headers.get("X-Request-ID", "")[:128] or uuid.uuid4().hex
            try:
                split = urlsplit(self.path)
                path = split.path.rstrip("/") or "/"
                query = parse_qs(split.query, keep_blank_values=False)
                if method == "GET":
                    self._get(path, query)
                else:
                    self._post(path)
            except APIError as error:
                self._send_json(
                    error.status,
                    {"error": {"code": error.code, "message": error.message}},
                )
            except NotFound as error:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": {"code": "not_found", "message": str(error.args[0])}},
                )
            except StateConflict as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": {"code": "state_conflict", "message": str(error)}},
                )
            except ApprovalStateError as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": {"code": "approval_state_conflict", "message": str(error)}},
                )
            except (InboundValidationError, ValueError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "invalid_request", "message": str(error)}},
                )
            except Exception:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "The request could not be completed",
                        }
                    },
                )

        def _get(self, path: str, query: Mapping[str, list[str]]) -> None:
            if path == "/health":
                details: dict[str, Any] = {
                    "status": "ok",
                    "time": utc_now(),
                }
                if context.health_provider is not None:
                    # This endpoint is deliberately unauthenticated so load
                    # balancers can use it. Keep it to non-sensitive liveness
                    # fields; the authenticated status endpoint exposes the
                    # full operator snapshot.
                    supplied = dict(context.health_provider())
                    details["runtime"] = {
                        key: supplied[key]
                        for key in ("status", "running", "cycle_count")
                        if key in supplied
                    }
                self._send_json(HTTPStatus.OK, details)
                return

            if path == "/v1/status":
                self._require_operator(mutation=True)
                self._send_json(HTTPStatus.OK, context.store.snapshot())
                return
            if path == "/v1/hermes/status":
                self._require_bridge()
                supplied = (
                    dict(context.health_provider())
                    if context.health_provider is not None
                    else {}
                )
                raw_cycle_count = supplied.get("cycle_count", 0)
                cycle_count = (
                    raw_cycle_count
                    if isinstance(raw_cycle_count, int)
                    and not isinstance(raw_cycle_count, bool)
                    and raw_cycle_count >= 0
                    else 0
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": str(supplied.get("status", "unknown"))[:64],
                        "running": bool(supplied.get("running", False)),
                        "cycle_count": cycle_count,
                        "operational_counters": context.store.operational_counters(),
                        "as_of": utc_now(),
                    },
                )
                return
            if path == "/v1/work":
                self._require_operator(mutation=True)
                limit = self._query_limit(query, default=200, maximum=1000)
                statuses = _split_query_values(query.get("status", [])) or None
                kinds = _split_query_values(query.get("kind", [])) or None
                parent_values = query.get("parent_id", [])
                parent_id = parent_values[0] if parent_values else None
                items = context.store.list_work(
                    statuses=statuses,
                    kinds=kinds,
                    parent_id=parent_id,
                    limit=limit,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"items": [item.to_dict() for item in items], "count": len(items)},
                )
                return
            if path == "/v1/next":
                self._require_reader()
                limit = self._query_limit(query, default=5, maximum=100)
                items = self._next_items(limit)
                self._send_json(HTTPStatus.OK, {"items": items, "count": len(items)})
                return
            if path == "/v1/questions":
                reader_role = self._require_reader()
                limit = self._query_limit(query, default=100, maximum=1000)
                raw_status = query.get("status", [QuestionStatus.PENDING.value])[0]
                if (
                    reader_role == "bridge"
                    and raw_status != QuestionStatus.PENDING.value
                ):
                    raise APIError(
                        HTTPStatus.FORBIDDEN,
                        "bridge_question_scope",
                        "The Hermes bridge can read pending questions only",
                    )
                questions = context.store.list_questions(
                    status=raw_status,
                    limit=limit,
                )
                if reader_role == "bridge":
                    questions = [
                        {
                            key: value
                            for key, value in question.items()
                            if key not in {"answer", "answered_at"}
                        }
                        for question in questions
                    ]
                self._send_json(
                    HTTPStatus.OK,
                    {"items": questions, "count": len(questions)},
                )
                return
            if path == "/v1/hermes/reminders":
                self._require_bridge()
                limit = self._query_limit(query, default=20, maximum=100)
                items = self._read_due_reminders(limit)
                reminder_views = [
                    self._reminder_delivery_view(
                        item, context.attention_redelivery_seconds
                    )
                    for item in items
                ]
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "items": reminder_views,
                        "count": len(reminder_views),
                        "as_of": utc_now(),
                        "redelivery_seconds": context.attention_redelivery_seconds,
                    },
                )
                return
            if path == "/v1/hermes/attention":
                self._require_bridge()
                limit = self._query_limit(query, default=20, maximum=100)
                items = self._read_due_reminders(limit)
                reminders = [
                    self._reminder_delivery_view(
                        item, context.attention_redelivery_seconds
                    )
                    for item in items
                ]
                questions = context.store.list_questions(
                    status=QuestionStatus.PENDING.value,
                    limit=limit,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "reminders": reminders,
                        "questions": questions,
                        "count": len(reminders) + len(questions),
                        "as_of": utc_now(),
                        "redelivery_seconds": context.attention_redelivery_seconds,
                    },
                )
                return
            if path == "/v1/hermes/execution-contract":
                self._require_bridge()
                values = query.get("task_id", [])
                if len(values) != 1 or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", values[0]
                ):
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_task_id",
                        "One valid Hermes task_id is required",
                    )
                if context.execution_contract_provider is None:
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "execution_contract_unavailable",
                        "Task-scoped execution authorization is unavailable",
                    )
                contract = dict(context.execution_contract_provider(values[0]))
                if (
                    not isinstance(contract.get("authorized"), bool)
                    or contract.get("task_id") != values[0]
                ):
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "execution_contract_invalid",
                        "Task-scoped execution authorization is invalid",
                    )
                self._send_json(HTTPStatus.OK, contract)
                return
            if path == "/v1/approvals":
                self._require_operator(mutation=True)
                if context.action_stager is None:
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "approval_service_unavailable",
                        "Approval staging is not configured",
                    )
                limit = self._query_limit(query, default=100, maximum=1000)
                raw_status = query.get("status", ["pending_approval"])[0]
                actions = context.action_stager.list(status=raw_status, limit=limit)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "items": [action.to_dict(include_content=True) for action in actions],
                        "count": len(actions),
                    },
                )
                return
            if path == "/v1/memory":
                self._require_operator(mutation=True)
                limit = self._query_limit(query, default=200, maximum=1000)
                status_values = query.get("status", [])
                status = status_values[0] if status_values else None
                memories = context.store.list_memory(status=status, limit=limit)
                self._send_json(
                    HTTPStatus.OK,
                    {"items": memories, "count": len(memories)},
                )
                return
            memory_prefix = "/v1/memory/"
            if path.startswith(memory_prefix):
                self._require_operator(mutation=True)
                memory_id = unquote(path.removeprefix(memory_prefix)).strip("/")
                if not memory_id or "/" in memory_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                self._send_json(HTTPStatus.OK, context.store.get_memory(memory_id))
                return
            approval_prefix = "/v1/approvals/"
            if path.startswith(approval_prefix):
                self._require_operator(mutation=True)
                if context.action_stager is None:
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "approval_service_unavailable",
                        "Approval staging is not configured",
                    )
                action_id = unquote(path.removeprefix(approval_prefix)).strip("/")
                if not action_id or "/" in action_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                action = context.action_stager.get(action_id)
                self._send_json(HTTPStatus.OK, action.to_dict(include_content=True))
                return
            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")

        def _post(self, path: str) -> None:
            if path == "/v1/hermes/attention/claim":
                self._require_bridge()
                _, document = self._read_json(allow_empty=True)
                if set(document) - {"limit"}:
                    raise ValueError("attention claim accepts only an optional limit")
                limit = document.get("limit", 20)
                if (
                    not isinstance(limit, int)
                    or isinstance(limit, bool)
                    or not 1 <= limit <= 100
                ):
                    raise ValueError("attention claim limit must be from 1 through 100")
                claimed = context.store.claim_attention(
                    reminder_limit=limit,
                    question_limit=limit,
                    redelivery_seconds=context.attention_redelivery_seconds,
                    actor="hermes-bridge",
                )
                reminders = [
                    self._reminder_delivery_view(
                        item, context.attention_redelivery_seconds
                    )
                    for item in claimed["reminders"]
                ]
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "reminders": reminders,
                        "questions": claimed["questions"],
                        "count": len(reminders) + len(claimed["questions"]),
                        "as_of": claimed["claimed_at"],
                        "redelivery_seconds": claimed["redelivery_seconds"],
                    },
                )
                return

            if path == "/v1/hermes/work":
                self._require_bridge()
                _, document = self._read_json()
                allowed = {
                    "title",
                    "description",
                    "kind",
                    "due_at",
                    "parent_id",
                    "recurrence_rule",
                }
                if set(document) - allowed or "title" not in document:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_hermes_work",
                        "Hermes work creation accepts title, description, kind, due_at, parent_id, and recurrence_rule",
                    )
                title = document.get("title")
                description = document.get("description", "")
                due_at = document.get("due_at")
                parent_id = document.get("parent_id")
                recurrence_rule = document.get("recurrence_rule")
                if not isinstance(title, str) or not title.strip() or len(title) > 500:
                    raise ValueError("title must be a nonempty string of at most 500 characters")
                if not isinstance(description, str) or len(description) > 100_000:
                    raise ValueError("description must be a string of at most 100000 characters")
                if due_at is not None and not isinstance(due_at, str):
                    raise ValueError("due_at must be an ISO-8601 string or null")
                if recurrence_rule is not None and not isinstance(recurrence_rule, str):
                    raise ValueError("recurrence_rule must be a string or null")
                if parent_id is not None:
                    if not isinstance(parent_id, str) or not parent_id.strip() or "/" in parent_id:
                        raise ValueError("parent_id must be a valid work identity or null")
                    context.store.get_work(parent_id.strip())
                    parent_id = parent_id.strip()
                item = WorkItem(
                    title=title.strip(),
                    description=description,
                    kind=WorkKind(str(document.get("kind", WorkKind.TASK.value))),
                    status=WorkStatus.TRIAGE,
                    due_at=due_at,
                    recurrence_rule=recurrence_rule,
                    parent_id=parent_id,
                    execution_mode=ExecutionMode.NONE,
                    provenance={
                        "source": "hermes-conversation",
                        "trust_level": TrustLevel.OPERATOR.value,
                        "actor": "hermes-user",
                    },
                    metadata={
                        "governance": {
                            "source_trust": TrustLevel.OPERATOR.value,
                            "creation_authorized": True,
                            "execution_authorized": False,
                        }
                    },
                )
                context.store.create_work(item, actor="hermes-bridge")
                authority_binding = execution_scope_binding(
                    item,
                    profile=item.assignee or context.authorization_profile,
                    skills=(),
                    default_skills=context.authorization_default_skills,
                    goal_mode=context.authorization_goal_mode,
                    execution_authorized=False,
                )
                event = Event(
                    source="operator",
                    event_type="operator.work_updated",
                    external_id=f"hermes-work:{item.id}:{item.version}",
                    dedupe_key=f"hermes-work:{item.id}:{item.version}",
                    trust_level=TrustLevel.OPERATOR,
                    payload={
                        "work_id": item.id,
                        "work_version": item.version,
                        "scope_revision": item.authorization_scope_revision,
                        "scope_digest": authority_binding["scope_digest"],
                        "authorization_binding": authority_binding,
                        "capabilities": ["update"],
                        "reason": "User captured work in a Hermes conversation",
                    },
                    provenance={
                        "ingress": "hermes-bridge",
                        "authenticated": True,
                        "actor": "hermes-user",
                    },
                )
                event_id, _ = context.store.enqueue_event(event, actor="hermes-bridge")
                self._wake("hermes-work-created")
                self._send_json(
                    HTTPStatus.CREATED,
                    {"work": item.to_dict(), "event_id": event_id},
                )
                return

            if path == "/v1/hermes/inbound":
                self._require_bridge()
                _, document = self._read_json()
                if set(document) != {"source", "events"}:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_hermes_inbound",
                        "Hermes inbound payload requires exactly source and events",
                    )
                source = document.get("source")
                allowed_sources = {"google.gmail", "google.calendar", "google.meeting"}
                if source not in allowed_sources:
                    raise ValueError("source must be google.gmail, google.calendar, or google.meeting")
                events = document.get("events")
                if not isinstance(events, list) or not 1 <= len(events) <= 100:
                    raise ValueError("events must contain between 1 and 100 items")
                results: list[dict[str, Any]] = []
                created_count = 0
                for value in events:
                    if not isinstance(value, dict):
                        raise ValueError("every inbound event must be an object")
                    allowed_event = {"event_type", "external_id", "revision", "payload"}
                    if set(value) - allowed_event or not {
                        "event_type",
                        "external_id",
                        "payload",
                    }.issubset(value):
                        raise ValueError(
                            "each inbound event requires event_type, external_id, and payload"
                        )
                    external_id = value.get("external_id")
                    revision = value.get("revision")
                    if not isinstance(external_id, str) or not external_id.strip():
                        raise ValueError("external_id must be a nonempty string")
                    if revision is not None and (
                        not isinstance(revision, str) or not revision.strip()
                    ):
                        raise ValueError("revision must be a nonempty string when supplied")
                    if revision is None:
                        revision = hashlib.sha256(
                            json.dumps(
                                value.get("payload"),
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ).encode("utf-8")
                        ).hexdigest()
                    normalized = normalize_external_event(
                        str(source),
                        {
                            "event_type": value.get("event_type"),
                            "external_id": external_id.strip(),
                            "dedupe_key": f"{external_id.strip()}:{revision}",
                            "payload": value.get("payload"),
                        },
                        authenticated=True,
                        request_id=self._request_id,
                        remote_address=(
                            self.client_address[0] if self.client_address else None
                        ),
                    )
                    normalized.event.provenance.update(
                        {
                            "ingress": "hermes-google-skill",
                            "actor": "hermes-cron",
                            "revision": revision,
                        }
                    )
                    event_id, created = context.store.enqueue_event(
                        normalized.event,
                        actor="hermes-bridge",
                    )
                    created_count += int(created)
                    results.append(
                        {"external_id": external_id.strip(), "event_id": event_id, "created": created}
                    )
                if created_count:
                    self._wake(f"event:{source}")
                self._send_json(
                    HTTPStatus.ACCEPTED if created_count else HTTPStatus.OK,
                    {"source": source, "created": created_count, "items": results},
                )
                return

            hermes_question_prefix = "/v1/hermes/questions/"
            hermes_question_suffix = "/answer"
            if path.startswith(hermes_question_prefix) and path.endswith(
                hermes_question_suffix
            ):
                self._require_bridge()
                question_id = unquote(
                    path[len(hermes_question_prefix) : -len(hermes_question_suffix)]
                ).strip("/")
                if not question_id or "/" in question_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                _, document = self._read_json()
                if set(document) != {"answer"} or not isinstance(
                    document.get("answer"), str
                ):
                    raise ValueError("answer payload must contain exactly one string answer")
                result = context.store.answer_question(
                    question_id,
                    str(document["answer"]),
                    actor="hermes-user-approved",
                )
                self._wake("question-answered")
                self._send_json(HTTPStatus.OK, result)
                return

            hermes_work_prefix = "/v1/hermes/work/"
            hermes_update_suffix = "/update"
            if path.startswith(hermes_work_prefix) and path.endswith(
                hermes_update_suffix
            ):
                self._require_bridge()
                work_id = unquote(
                    path[len(hermes_work_prefix) : -len(hermes_update_suffix)]
                ).strip("/")
                if not work_id or "/" in work_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                _, document = self._read_json()
                if set(document) != {"expected_version", "changes"}:
                    raise ValueError("update requires expected_version and changes")
                expected_version = document.get("expected_version")
                changes = document.get("changes")
                if (
                    not isinstance(expected_version, int)
                    or isinstance(expected_version, bool)
                    or expected_version < 1
                ):
                    raise ValueError("expected_version must be a positive integer")
                if not isinstance(changes, dict) or not changes:
                    raise ValueError("changes must be a nonempty object")
                allowed_changes = {
                    "title",
                    "description",
                    "status",
                    "parent_id",
                    "due_at",
                    "scheduled_at",
                    "recurrence_rule",
                    "priority",
                }
                if set(changes) - allowed_changes:
                    raise ValueError(
                        "Hermes work updates may change title, description, status, parent_id, due_at, scheduled_at, recurrence_rule, or priority"
                    )
                updated = context.store.update_work(
                    work_id,
                    dict(changes),
                    actor="hermes-user",
                    expected_version=expected_version,
                )
                self._wake("hermes-work-updated")
                self._send_json(HTTPStatus.OK, {"work": updated.to_dict()})
                return

            hermes_reminder_suffix = "/reminder"
            if path.startswith(hermes_work_prefix) and path.endswith(
                hermes_reminder_suffix
            ):
                self._require_bridge()
                work_id = unquote(
                    path[len(hermes_work_prefix) : -len(hermes_reminder_suffix)]
                ).strip("/")
                if not work_id or "/" in work_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                _, document = self._read_json()
                allowed = {"expected_version", "action", "until"}
                if set(document) - allowed or not {
                    "expected_version",
                    "action",
                }.issubset(document):
                    raise ValueError(
                        "reminder lifecycle requires expected_version and action, with until only for snooze"
                    )
                expected_version = document.get("expected_version")
                action = document.get("action")
                until = document.get("until")
                if (
                    not isinstance(expected_version, int)
                    or isinstance(expected_version, bool)
                    or expected_version < 1
                ):
                    raise ValueError("expected_version must be a positive integer")
                if action not in {"snooze", "acknowledge", "complete"}:
                    raise ValueError(
                        "action must be snooze, acknowledge, or complete"
                    )
                if action == "snooze":
                    if not isinstance(until, str):
                        raise ValueError("snooze requires an ISO-8601 until timestamp")
                elif "until" in document:
                    raise ValueError("until is accepted only for snooze")
                updated = context.store.resolve_reminder(
                    work_id,
                    action=str(action),
                    expected_version=expected_version,
                    until=until,
                    actor="hermes-user",
                )
                self._wake(f"reminder-{action}")
                self._send_json(HTTPStatus.OK, {"work": updated.to_dict()})
                return

            hermes_authorize_suffix = "/authorize"
            if path.startswith(hermes_work_prefix) and path.endswith(
                hermes_authorize_suffix
            ):
                self._require_bridge()
                work_id = unquote(
                    path[len(hermes_work_prefix) : -len(hermes_authorize_suffix)]
                ).strip("/")
                if not work_id or "/" in work_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                _, document = self._read_json(allow_empty=True)
                allowed = {
                    "expected_version",
                    "reason",
                    "profile",
                    "skills",
                    "goal_mode",
                }
                if set(document) - allowed or "expected_version" not in document:
                    raise ValueError(
                        "authorize requires expected_version and accepts optional reason, profile, skills, and goal_mode"
                    )
                expected_version = document.get("expected_version")
                if (
                    not isinstance(expected_version, int)
                    or isinstance(expected_version, bool)
                    or expected_version < 1
                ):
                    raise ValueError("expected_version must be a positive integer")
                reason = document.get("reason", "Explicit approval in Hermes")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                    raise ValueError("reason must be a nonempty string of at most 2000 characters")
                item = context.store.get_work(work_id)
                profile = document.get(
                    "profile", item.assignee or context.authorization_profile
                )
                skills = document.get("skills", [])
                goal_mode = document.get(
                    "goal_mode", context.authorization_goal_mode
                )
                if not isinstance(profile, str) or not profile.strip():
                    raise ValueError("profile must be a nonempty string")
                if not isinstance(skills, list) or not all(
                    isinstance(value, str) and value.strip() for value in skills
                ):
                    raise ValueError("skills must be a list of nonempty strings")
                if not isinstance(goal_mode, bool):
                    raise ValueError("goal_mode must be a boolean")
                result = context.store.enqueue_work_authorization(
                    work_id,
                    expected_version=expected_version,
                    profile=profile,
                    skills=skills,
                    default_skills=context.authorization_default_skills,
                    goal_mode=goal_mode,
                    reason=reason.strip(),
                    actor="hermes-user-approved",
                )
                if result["created"]:
                    self._wake("hermes-work-authorized")
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    result,
                )
                return

            if path == "/v1/hermes/delegation-claim":
                self._require_bridge()
                if context.delegation_claim_provider is None:
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "delegation_claim_unavailable",
                        "Task-scoped delegation authorization is unavailable",
                    )
                _, document = self._read_json()
                if set(document) != {"task_id", "requested_children"}:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_delegation_claim",
                        "Delegation claim must contain the exact task and child count",
                    )
                task_id = document.get("task_id")
                requested_children = document.get("requested_children")
                if not isinstance(task_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id
                ):
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_task_id",
                        "A valid Hermes task_id is required",
                    )
                if (
                    not isinstance(requested_children, int)
                    or isinstance(requested_children, bool)
                    or not 1 <= requested_children <= 3
                ):
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_delegation_claim",
                        "requested_children must be an integer from 1 through 3",
                    )
                claim = dict(
                    context.delegation_claim_provider(
                        task_id,
                        requested_children,
                    )
                )
                if (
                    not isinstance(claim.get("claimed"), bool)
                    or claim.get("task_id") != task_id
                ):
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "delegation_claim_invalid",
                        "Task-scoped delegation response is invalid",
                    )
                self._send_json(HTTPStatus.OK, claim)
                return
            if path.startswith("/v1/events/"):
                source = unquote(path.removeprefix("/v1/events/"))
                raw, document = self._read_json()
                configured = source in context.webhook_secrets
                authenticated = False
                bridge_authenticated = False
                if source == "hermes":
                    if not context.bridge_token:
                        raise APIError(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "hermes_bridge_auth_unconfigured",
                            "Hermes bridge authentication is not configured",
                        )
                    if not hmac.compare_digest(
                        self.headers.get("Authorization", ""),
                        f"Bearer {context.bridge_token}",
                    ):
                        code = (
                            "policy_attestation_auth_required"
                            if document.get("event_type") == "policy.attested"
                            else "hermes_bridge_auth_required"
                        )
                        raise APIError(
                            HTTPStatus.UNAUTHORIZED,
                            code,
                            "The scoped Hermes bridge token is required",
                        )
                    authenticated = True
                    bridge_authenticated = True
                elif configured:
                    secret = context.webhook_secrets[source]
                    if not secret:
                        raise APIError(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "webhook_secret_unavailable",
                            "The webhook source has no usable secret",
                        )
                    authenticated = verify_webhook_signature(
                        raw,
                        self.headers.get("X-Hermes-Signature"),
                        secret,
                    )
                    if not authenticated:
                        raise APIError(
                            HTTPStatus.UNAUTHORIZED,
                            "invalid_signature",
                            "Webhook signature is missing or invalid",
                        )
                elif context.api_token and hmac.compare_digest(
                    self.headers.get("Authorization", ""),
                    f"Bearer {context.api_token}",
                ):
                    authenticated = True
                elif not context.allow_unsigned_webhooks:
                    raise APIError(
                        HTTPStatus.UNAUTHORIZED,
                        "webhook_auth_required",
                        "A configured HMAC secret or operator bearer token is required",
                    )
                attestation = None
                if document.get("event_type") == "policy.attested":
                    if source != "hermes" or not bridge_authenticated:
                        raise APIError(
                            HTTPStatus.UNAUTHORIZED,
                            "policy_attestation_auth_required",
                            "Policy attestation requires the scoped Hermes bridge token",
                        )
                    attestation = _validated_policy_attestation(document)
                normalized = normalize_external_event(
                    source,
                    document,
                    authenticated=authenticated,
                    request_id=self._request_id,
                    remote_address=self.client_address[0] if self.client_address else None,
                )
                if attestation is not None:
                    attestation_id = str(document["external_id"])
                    created = context.store.record_policy_attestation(
                        str(attestation["profile"]),
                        attestation_id,
                        attestation,
                        actor="hermes-bridge",
                    )
                    self._send_json(
                        HTTPStatus.ACCEPTED if created else HTTPStatus.OK,
                        {
                            "event_id": attestation_id,
                            "created": created,
                            "trust_level": normalized.event.trust_level.value,
                        },
                    )
                    return
                event_id, created = context.store.enqueue_event(
                    normalized.event, actor="webhook"
                )
                event_type = str(document.get("event_type", ""))
                immediate_reconcile_types = {
                    "card.state_changed",
                    "execution.state_changed",
                    "task.state_changed",
                }
                wake_reason = (
                    "hermes-state"
                    if source == "hermes" and event_type in immediate_reconcile_types
                    else f"event:{source}"
                )
                if created:
                    self._wake(wake_reason)
                self._send_json(
                    HTTPStatus.ACCEPTED if created else HTTPStatus.OK,
                    {"event_id": event_id, "created": created, "trust_level": normalized.event.trust_level.value},
                )
                return

            if path == "/v1/ingest":
                self._require_operator(mutation=True)
                _, document = self._read_json()
                normalized = normalize_operator_event(
                    document,
                    request_id=self._request_id,
                )
                event_id, created = context.store.enqueue_event(normalized.event, actor="operator-api")
                if created:
                    self._wake("operator-ingest")
                self._send_json(
                    HTTPStatus.ACCEPTED if created else HTTPStatus.OK,
                    {"event_id": event_id, "created": created, "trust_level": normalized.event.trust_level.value},
                )
                return

            if path == "/v1/wake":
                self._require_operator(mutation=True)
                _, document = self._read_json(allow_empty=True)
                reason = document.get("reason", "operator")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 128:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_reason",
                        "reason must be a nonempty string of at most 128 characters",
                    )
                self._wake(reason.strip())
                self._send_json(HTTPStatus.ACCEPTED, {"woken": True, "reason": reason.strip()})
                return

            if path == "/v1/work/links":
                self._require_operator(mutation=True)
                _, document = self._read_json()
                required = {
                    "from_id",
                    "to_id",
                    "relation",
                    "expected_from_version",
                    "expected_to_version",
                }
                if set(document) != required:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_work_link",
                        "Work link payload must contain the exact link contract",
                    )
                from_id = str(document["from_id"]).strip()
                to_id = str(document["to_id"]).strip()
                if not from_id or not to_id:
                    raise ValueError("Work link endpoint IDs cannot be empty")
                from_version = int(document["expected_from_version"])
                to_version = int(document["expected_to_version"])
                if from_version < 1 or to_version < 1:
                    raise ValueError("Work link versions must be positive integers")
                relation = WorkRelation(str(document["relation"]))
                link_id = context.store.add_work_link(
                    from_id,
                    to_id,
                    relation,
                    actor="operator-api",
                    expected_from_version=from_version,
                    expected_to_version=to_version,
                )
                self._wake("work-linked")
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "id": link_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "relation": relation.value,
                    },
                )
                return

            run_prefix = "/v1/runs/"
            run_suffix = "/resolve"
            if path.startswith(run_prefix) and path.endswith(run_suffix):
                self._require_operator(mutation=True)
                run_id = unquote(
                    path[len(run_prefix) : -len(run_suffix)]
                ).strip("/")
                if not run_id or "/" in run_id:
                    raise APIError(
                        HTTPStatus.NOT_FOUND,
                        "not_found",
                        "Endpoint not found",
                    )
                _, document = self._read_json()
                if set(document) != {"expected_status", "reason"}:
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_run_resolution",
                        "Run resolution requires expected_status and reason",
                    )
                result = context.store.resolve_run(
                    run_id,
                    expected_status=str(document["expected_status"]),
                    reason=str(document["reason"]),
                    actor="operator-api",
                )
                self._wake("run-resolved")
                self._send_json(HTTPStatus.OK, result)
                return

            prefix = "/v1/questions/"
            suffix = "/answer"
            if path.startswith(prefix) and path.endswith(suffix):
                self._require_operator(mutation=True)
                question_id = unquote(path[len(prefix) : -len(suffix)]).strip("/")
                if not question_id or "/" in question_id:
                    raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
                _, document = self._read_json()
                answer = document.get("answer")
                if not isinstance(answer, str):
                    raise APIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_answer",
                        "answer must be a string",
                    )
                result = context.store.answer_question(question_id, answer, actor="operator-api")
                self._wake("question-answered")
                self._send_json(HTTPStatus.OK, result)
                return

            approval_prefix = "/v1/approvals/"
            for decision in ("approve", "deny"):
                decision_suffix = f"/{decision}"
                if path.startswith(approval_prefix) and path.endswith(decision_suffix):
                    self._require_operator(mutation=True)
                    if context.action_stager is None:
                        raise APIError(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "approval_service_unavailable",
                            "Approval staging is not configured",
                        )
                    action_id = unquote(
                        path[len(approval_prefix) : -len(decision_suffix)]
                    ).strip("/")
                    if not action_id or "/" in action_id:
                        raise APIError(
                            HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found"
                        )
                    _, document = self._read_json(allow_empty=True)
                    if decision == "approve":
                        grant = context.action_stager.approve(
                            action_id, approved_by="operator-api"
                        )
                        self._wake("external-action-approved")
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "action_id": action_id,
                                "status": "approved",
                                "grant_id": grant.grant_id,
                                "expires_at": grant.expires_at.isoformat(),
                            },
                        )
                    else:
                        reason = document.get("reason", "")
                        if not isinstance(reason, str) or len(reason) > 2000:
                            raise APIError(
                                HTTPStatus.BAD_REQUEST,
                                "invalid_reason",
                                "reason must be a string of at most 2000 characters",
                            )
                        context.action_stager.deny(
                            action_id, denied_by="operator-api", reason=reason
                        )
                        self._wake("external-action-denied")
                        self._send_json(
                            HTTPStatus.OK,
                            {"action_id": action_id, "status": "denied"},
                        )
                    return

            memory_prefix = "/v1/memory/"
            for decision, stored_decision in (
                ("promote", "promoted"),
                ("reject", "rejected"),
            ):
                decision_suffix = f"/{decision}"
                if path.startswith(memory_prefix) and path.endswith(decision_suffix):
                    self._require_operator(mutation=True)
                    memory_id = unquote(
                        path[len(memory_prefix) : -len(decision_suffix)]
                    ).strip("/")
                    if not memory_id or "/" in memory_id:
                        raise APIError(
                            HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found"
                        )
                    self._read_json(allow_empty=True)
                    result = context.store.review_memory(
                        memory_id,
                        decision=stored_decision,
                        actor="operator-api",
                    )
                    self._wake(f"memory-{decision}")
                    self._send_json(HTTPStatus.OK, result)
                    return

            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")

        def _read_json(self, *, allow_empty: bool = False) -> tuple[bytes, dict[str, Any]]:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("application/json"):
                raise APIError(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "unsupported_media_type",
                    "Content-Type must be application/json",
                )
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise APIError(
                    HTTPStatus.LENGTH_REQUIRED,
                    "length_required",
                    "Content-Length is required",
                )
            try:
                length = int(raw_length)
            except ValueError as error:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_content_length",
                    "Content-Length is invalid",
                ) from error
            if length < 0:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_content_length",
                    "Content-Length is invalid",
                )
            if length > context.max_body_bytes:
                self.close_connection = True
                raise APIError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "body_too_large",
                    f"Request body exceeds {context.max_body_bytes} bytes",
                )
            raw = self.rfile.read(length)
            if not raw and allow_empty:
                return raw, {}
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_json",
                    "Request body must contain valid UTF-8 JSON",
                ) from error
            if not isinstance(document, dict):
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_json_type",
                    "JSON body must be an object",
                )
            return raw, document

        def _require_operator(self, *, mutation: bool) -> None:
            token = context.api_token
            if not token:
                if mutation:
                    raise APIError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "operator_auth_unconfigured",
                        "Operator API authentication is not configured",
                    )
                return
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(authorization, expected):
                raise APIError(
                    HTTPStatus.UNAUTHORIZED,
                    "unauthorized",
                    "A valid operator bearer token is required",
                )

        def _require_reader(self) -> str:
            if not context.api_token and not context.bridge_token:
                return "operator"
            authorization = self.headers.get("Authorization", "")
            if context.api_token and hmac.compare_digest(
                authorization,
                f"Bearer {context.api_token}",
            ):
                return "operator"
            if context.bridge_token and hmac.compare_digest(
                authorization,
                f"Bearer {context.bridge_token}",
            ):
                return "bridge"
            raise APIError(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "A valid reader bearer token is required",
            )

        def _require_bridge(self) -> None:
            if not context.bridge_token:
                raise APIError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "hermes_bridge_auth_unconfigured",
                    "Hermes bridge authentication is not configured",
                )
            authorization = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                authorization,
                f"Bearer {context.bridge_token}",
            ):
                raise APIError(
                    HTTPStatus.UNAUTHORIZED,
                    "unauthorized",
                    "The scoped Hermes bridge token is required",
                )

        def _wake(self, reason: str) -> None:
            if context.wake is not None:
                context.wake(reason)

        def _next_items(self, limit: int) -> list[dict[str, Any]]:
            if context.next_provider is not None:
                supplied = context.next_provider(limit)
                return [_work_to_dict(item) for item in supplied][:limit]
            candidates = context.store.list_work(
                statuses=[WorkStatus.TRIAGE, WorkStatus.READY, WorkStatus.REVIEW],
                dependencies_satisfied_only=True,
                limit=max(limit * 4, limit),
            )
            return [item.to_dict() for item in candidates[:limit]]

        def _read_due_reminders(self, limit: int) -> list[WorkItem]:
            now = datetime.now(UTC)
            candidates = context.store.list_work(
                kinds=[WorkKind.REMINDER],
                order_by="due",
                limit=5000,
            )
            due: list[tuple[datetime, WorkItem]] = []
            for item in candidates:
                if item.status in {
                    WorkStatus.DONE,
                    WorkStatus.CANCELLED,
                    WorkStatus.ARCHIVED,
                }:
                    continue
                raw_due = (
                    item.reminder_snoozed_until
                    or item.due_at
                    or item.scheduled_at
                )
                if not raw_due:
                    continue
                try:
                    parsed = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    continue
                parsed = parsed.astimezone(UTC)
                if parsed <= now:
                    due.append((parsed, item))
            due.sort(
                key=lambda pair: (
                    pair[0],
                    -float(pair[1].priority_score),
                    pair[1].id,
                )
            )
            return [item for _, item in due[:limit]]

        @staticmethod
        def _reminder_delivery_view(
            item: WorkItem, redelivery_seconds: int
        ) -> dict[str, Any]:
            document = item.to_dict()
            next_eligible_at = None
            if item.reminder_last_delivered_at:
                delivered = datetime.fromisoformat(
                    item.reminder_last_delivered_at.replace("Z", "+00:00")
                )
                next_eligible_at = (
                    delivered.astimezone(UTC)
                    + timedelta(seconds=redelivery_seconds)
                ).isoformat().replace("+00:00", "Z")
            document["delivery_state"] = {
                "last_delivered_at": item.reminder_last_delivered_at,
                "last_acknowledged_at": item.reminder_last_acknowledged_at,
                "delivery_count": item.reminder_delivery_count,
                "next_eligible_at": next_eligible_at,
            }
            return document

        @staticmethod
        def _query_limit(
            query: Mapping[str, list[str]], *, default: int, maximum: int
        ) -> int:
            raw = query.get("limit", [str(default)])[0]
            try:
                value = int(raw)
            except ValueError as error:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_limit",
                    "limit must be an integer",
                ) from error
            if not 1 <= value <= maximum:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_limit",
                    f"limit must be between 1 and {maximum}",
                )
            return value

        def _method_not_allowed(self) -> None:
            self._request_id = self.headers.get("X-Request-ID", "")[:128] or uuid.uuid4().hex
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": {"code": "method_not_allowed", "message": "Method not allowed"}},
            )

        def _send_json(self, status: HTTPStatus, document: Mapping[str, Any]) -> None:
            payload = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", self._request_id)
            self.end_headers()
            self.wfile.write(payload)

    return OperatorAPIHandler


def _split_query_values(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


def _work_to_dict(item: WorkItem | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, WorkItem):
        return item.to_dict()
    return dict(item)
