# Reminder and Attention Lifecycle

## Ownership

Hermes Cron owns scheduling and private delivery. Hermes Operator does not run
a second reminder scheduler. Each delivery invocation calls
`operator_claim_attention` once, renders the claimed attention through Hermes,
and then exits or waits for the next native Cron invocation.

The durable claim covers both due `reminder` work and pending Operator
questions. This lets the same native job deliver reminders and proactive
clarification requests without independent polling loops.

## Delivery claims

`GET /v1/hermes/attention` and `GET /v1/hermes/reminders` are read-only
previews. The first shows due reminders and pending questions; the second shows
due reminders only. Previewing never records delivery state and never consumes
the current delivery opportunity.

`POST /v1/hermes/attention/claim` is the atomic delivery operation. It accepts
an optional body such as `{"limit": 20}` and returns due reminders plus pending
questions. A successful claim records `last_delivered_at` and increments
`delivery_count` before returning content. The response also states the fixed
`redelivery_seconds` window and, for reminders, `next_eligible_at`.

An item is not returned by another claim inside that window, although it may
remain visible in a read-only preview. If Hermes fails after the claim but
before private delivery, the item becomes claimable again after the window.
This intentionally provides bounded at-least-once delivery without creating a
nested scheduler.

Configure the window independently from the Hermes Cron cadence:

```toml
[native_automation]
reminder_schedule = "every 15m"
attention_redelivery_seconds = 3600
```

## Recurrence

A reminder may have a `recurrence_rule`. The first version supports a fixed,
portable subset of ISO-8601 durations:

- `PT15M` for every 15 minutes
- `PT2H` for every 2 hours
- `P1D` for every day
- `P2W` for every 2 weeks

Months and years are excluded because their length depends on calendar and
timezone policy. A recurrence is valid only on `kind = "reminder"` and requires
an absolute `due_at` timestamp with a timezone.

Completing or acknowledging a recurring reminder advances `due_at` to the
first recurrence strictly after the current time, while preserving the
original due-time anchor. Missed occurrences do not create a backlog and the
next occurrence does not drift to the acknowledgement time. The reminder
returns to `ready`. A one-shot reminder retains the prior behavior and becomes
`done`.

## User actions

The bridge lifecycle route is:

```text
POST /v1/hermes/work/{id}/reminder
```

It requires the current `expected_version` and one action:

```json
{"expected_version": 3, "action": "acknowledge"}
```

```json
{"expected_version": 3, "action": "complete"}
```

```json
{
  "expected_version": 3,
  "action": "snooze",
  "until": "2026-07-15T15:00:00-04:00"
}
```

Snooze requires a future absolute time. It resets delivery eligibility for the
current occurrence by storing `reminder_snoozed_until`; it does not overwrite
`due_at`. Completing or acknowledging the reminder therefore advances from the
original recurring schedule rather than from the snooze time. Explicit schedule
edits clear the temporary override. Version fencing prevents an action based on
stale Cron output from overwriting a newer snooze, completion, or supervisor
update.

Equivalent local commands are available:

```bash
hermes-operator work add "Weekly review" \
  --kind reminder --status ready \
  --due 2026-07-20T09:00:00-04:00 --recurrence P1W

hermes-operator work reminder WORK_ID snooze \
  --until 2026-07-20T14:00:00-04:00 --expected-version 2

hermes-operator work reminder WORK_ID complete --expected-version 3
```

The lifecycle changes only canonical local state. External messages, calendar
writes, and publication remain subject to Hermes-native approval and are not
part of reminder delivery.
