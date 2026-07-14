"""Tool schemas shown to Hermes models."""

OPERATOR_STATUS = {
    "name": "operator_status",
    "description": (
        "Read control-plane liveness and content-free queue, question, work, and "
        "run counters. This cannot approve, send, or publish anything."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

OPERATOR_NEXT_WORK = {
    "name": "operator_next_work",
    "description": (
        "Read the operator's current next-best work suggestions and priority reasons. "
        "This does not start work or change task state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum suggestions to return.",
            }
        },
        "additionalProperties": False,
    },
}

OPERATOR_OPEN_QUESTIONS = {
    "name": "operator_open_questions",
    "description": (
        "Read unresolved questions that require the operator's context or decision. "
        "This does not answer questions on the user's behalf."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum questions to return.",
            }
        },
        "additionalProperties": False,
    },
}

OPERATOR_DUE_REMINDERS = {
    "name": "operator_due_reminders",
    "description": (
        "Read reminders that are due now. This is designed for a Hermes native cron "
        "turn to deliver a briefing; it does not create or mutate cron jobs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum due reminders to return.",
            }
        },
        "additionalProperties": False,
    },
}

OPERATOR_CLAIM_ATTENTION = {
    "name": "operator_claim_attention",
    "description": (
        "Atomically claim due reminders and pending Operator questions for one "
        "Hermes-native private delivery. A redelivery window prevents duplicate "
        "briefings. Use this only from the managed attention Cron job."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum reminders and questions to claim per type.",
            }
        },
        "additionalProperties": False,
    },
}

OPERATOR_CREATE_WORK = {
    "name": "operator_create_work",
    "description": (
        "Record one task, todo, reminder, project, goal, or other work item in "
        "Operator triage. Creation is reversible and never authorizes execution."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "description": {"type": "string", "maxLength": 20000},
            "kind": {
                "type": "string",
                "enum": [
                    "area",
                    "goal",
                    "project",
                    "milestone",
                    "task",
                    "todo",
                    "reminder",
                    "decision",
                ],
                "default": "task",
            },
            "due_at": {
                "type": ["string", "null"],
                "description": "Optional timezone-aware ISO 8601 due time.",
            },
            "parent_id": {
                "type": ["string", "null"],
                "description": "Optional exact existing parent work ID.",
            },
            "recurrence_rule": {
                "type": ["string", "null"],
                "pattern": "^P(?:[1-9][0-9]*[WD]|T[1-9][0-9]*[HM])$",
                "description": (
                    "Fixed recurrence for reminders only: PTnM, PTnH, PnD, or PnW. "
                    "A recurring reminder also requires due_at."
                ),
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    },
}

OPERATOR_ANSWER_QUESTION = {
    "name": "operator_answer_question",
    "description": (
        "Record the user's answer to one exact pending Operator question. Hermes "
        "must obtain native human confirmation before this tool executes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "answer": {"type": "string", "minLength": 1, "maxLength": 20000},
        },
        "required": ["question_id", "answer"],
        "additionalProperties": False,
    },
}

OPERATOR_AUTHORIZE_WORK = {
    "name": "operator_authorize_work",
    "description": (
        "Authorize execution for one exact Operator work ID after Hermes native human "
        "confirmation. This does not authorize external communication or publication."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "reason": {"type": "string", "maxLength": 2000},
        },
        "required": ["work_id"],
        "additionalProperties": False,
    },
}

OPERATOR_UPDATE_WORK = {
    "name": "operator_update_work",
    "description": (
        "Update reversible metadata or status on one exact Operator work item using "
        "optimistic concurrency. Completing, cancelling, or reparenting work requires "
        "Hermes native human confirmation. This never grants execution authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "expected_version": {"type": "integer", "minimum": 1},
            "changes": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "description": {"type": "string", "maxLength": 20000},
                    "status": {
                        "type": "string",
                        "enum": [
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
                        ],
                    },
                    "parent_id": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"]},
                    "scheduled_at": {"type": ["string", "null"]},
                    "recurrence_rule": {
                        "type": ["string", "null"],
                        "pattern": "^P(?:[1-9][0-9]*[WD]|T[1-9][0-9]*[HM])$",
                    },
                    "priority": {"type": "integer", "minimum": -1000, "maximum": 1000},
                },
                "minProperties": 1,
                "additionalProperties": False,
            },
        },
        "required": ["work_id", "expected_version", "changes"],
        "additionalProperties": False,
    },
}

OPERATOR_INGEST_INBOUND = {
    "name": "operator_ingest_inbound",
    "description": (
        "Record bounded email, calendar, or meeting items that were read using a "
        "Hermes native provider skill. Content remains untrusted triage input and this "
        "tool cannot send, reply, schedule, or authorize execution."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["google.gmail", "google.calendar", "google.meeting"],
            },
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string", "minLength": 1, "maxLength": 128},
                        "external_id": {"type": "string", "minLength": 1, "maxLength": 500},
                        "revision": {"type": "string", "minLength": 1, "maxLength": 128},
                        "payload": {"type": "object"},
                    },
                    "required": ["event_type", "external_id", "payload"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["source", "events"],
        "additionalProperties": False,
    },
}

OPERATOR_DIAGNOSTICS = {
    "name": "operator_diagnostics",
    "description": (
        "Read the local Hermes compatibility observation, including delegation mode, "
        "completion artifact support, profile match, and policy-hook position."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
