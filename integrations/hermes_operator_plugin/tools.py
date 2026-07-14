"""Read-only Hermes tool handlers for the operator service."""

from __future__ import annotations

import json
from typing import Any, Callable

from .client import OperatorClient, OperatorUnavailable


def _result(call: Callable[[], Any]) -> str:
    try:
        return json.dumps({"success": True, "data": call()}, ensure_ascii=False)
    except (OperatorUnavailable, ValueError, TypeError) as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    except Exception:
        return json.dumps(
            {"success": False, "error": "unexpected operator bridge error"}
        )


def status(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del args, kwargs
    return _result(client.health)


def next_work(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: client.next_work(args.get("limit", 5)))


def open_questions(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: client.open_questions(args.get("limit", 10)))


def due_reminders(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: client.due_reminders(args.get("limit", 20)))


def claim_attention(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: client.claim_attention(args.get("limit", 20)))


def create_work(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(
        lambda: client.create_work(
            title=args.get("title", ""),
            description=args.get("description", ""),
            kind=args.get("kind", "task"),
            due_at=args.get("due_at"),
            parent_id=args.get("parent_id"),
            recurrence_rule=args.get("recurrence_rule"),
        )
    )


def answer_question(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(
        lambda: client.answer_question(
            args.get("question_id", ""), args.get("answer", "")
        )
    )


def authorize_work(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(
        lambda: client.authorize_work(
            args.get("work_id", ""), args.get("reason", "")
        )
    )


def update_work(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(
        lambda: client.update_work(
            args.get("work_id", ""),
            args.get("expected_version", 0),
            args.get("changes", {}),
        )
    )


def ingest_inbound(client: OperatorClient, args: dict, **kwargs: Any) -> str:
    del kwargs
    return _result(
        lambda: client.ingest_inbound(
            args.get("source", ""), args.get("events", [])
        )
    )


def diagnostics(report: dict[str, Any], args: dict, **kwargs: Any) -> str:
    del args, kwargs
    return json.dumps({"success": True, "data": report}, ensure_ascii=False)
