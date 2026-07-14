# Inbound Connectors

## Connector boundary

Hermes Operator deliberately keeps provider SDKs out of the core package. A connector is a separate read-only process that watches one inbound surface and normalizes provider objects. The live service supports two provider-neutral intake paths:

- A configured command connector that the autonomy loop polls with a durable cursor
- A push or independently scheduled reader that sends signed JSON to the webhook API

Command connectors are polled in parallel before each supervisor pass. A failure in one source is recorded without preventing other sources from being observed. Events created by either path are available to the LLM supervisor in that same control-plane cycle.

Suitable sources include:

- Email messages and threads
- Calendar events and invitations
- Meeting transcripts, notes, and action-item extractors
- Issue trackers, forms, document inboxes, and repository events
- Hermes lifecycle observations through the bundled native plugin

The connector must not mutate canonical work directly. It also must not reuse the operator admin token, Hermes bridge token, or outbound credentials. For email, calendar, and meeting sources, request the narrowest read-only scopes the provider supports.

## Built-in command polling contract

Register any executable that implements the provider-specific read side:

```toml
[[inbound_connectors]]
name = "work-mail"
source = "gmail"
command = ["/opt/hermes-readers/bin/gmail-reader", "--account", "work"]
enabled = true
interval_seconds = 60
timeout_seconds = 45
pass_env = ["GMAIL_READ_TOKEN"]
max_output_bytes = 4194304
```

The command is invoked as fixed argv with no shell. It receives a small base environment, only the credential variables named by `pass_env`, and these control values:

```text
HERMES_OPERATOR_CONNECTOR_CURSOR
HERMES_OPERATOR_CONNECTOR_NAME
HERMES_OPERATOR_CONNECTOR_SOURCE
```

It must print one strict JSON object to stdout:

```json
{
  "cursor": "provider-cursor-after-this-page",
  "events": [
    {
      "event_type": "email.received",
      "external_id": "provider-message-123",
      "dedupe_key": "gmail:work:provider-message-123:v1",
      "payload": {}
    }
  ],
  "metadata": {"account": "work"}
}
```

`cursor` and `events` are required. `metadata` is optional. Unknown or duplicate keys, nonstandard JSON numbers, more than 500 events, cursors longer than 16,000 characters, and output beyond `max_output_bytes` are rejected. The core validates every event before changing state.

After a successful command, all normalized events and the new cursor commit in one fenced SQLite transaction. A crash, invalid event, or lost leader lease advances neither. Provider retries remain safe when the reader supplies a stable dedupe key. All command output enters as `authenticated_untrusted` evidence.

The process boundary does not prove that a provider token is read-only. Enforce that property at the provider, service account, container, and network layers. Configuration rejects attempts to pass the operator admin token, bridge token, approval secret, or Hermes run-control token by their configured environment names.

## Webhook delivery contract

Send one event to:

```text
POST /v1/events/{source}
Content-Type: application/json
X-Hermes-Signature: sha256=<lowercase hex HMAC>
```

The JSON envelope is:

```json
{
  "event_type": "provider.object_event",
  "external_id": "stable-provider-object-id",
  "dedupe_key": "stable-delivery-or-object-version-key",
  "payload": {}
}
```

`event_type` and `payload` are required. Put provider occurrence timestamps, account aliases, thread identifiers, and source-specific facts inside `payload`. The core adds receipt time, request ID, remote address, authentication result, and trust level.

Use a deterministic `dedupe_key`. A retry with the same source and dedupe key returns the prior event ID instead of creating another event. When provider objects can change, include the version, update timestamp, or change sequence in the key.

Examples:

```text
gmail:work:message:18f8d7
calendar:primary:event:abc123:updated:20260713T150000Z
meeting:zoom:987654:transcript:v2
```

## Signing requests

Configure one secret per source:

```toml
[server.webhook_secrets]
gmail = "${GMAIL_WEBHOOK_SECRET}"
calendar = "${CALENDAR_WEBHOOK_SECRET}"
meetings = "${MEETING_WEBHOOK_SECRET}"
```

The signature is lowercase HMAC-SHA256 over the exact UTF-8 request body. This standard-library sender shows the complete operation:

```python
import hashlib
import hmac
import json
import os
import urllib.request

source = "gmail"
secret = os.environ["GMAIL_WEBHOOK_SECRET"].encode("utf-8")
document = {
    "event_type": "email.received",
    "external_id": "provider-message-123",
    "dedupe_key": "gmail:work:provider-message-123",
    "payload": {
        "account": "work",
        "thread_id": "provider-thread-44",
        "from": "sender@example.test",
        "to": ["operator@example.test"],
        "subject": "Forecast follow-up",
        "received_at": "2026-07-13T15:00:00Z",
        "body_text": "Please include the updated forecast.",
    },
}
body = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
request = urllib.request.Request(
    f"http://127.0.0.1:8787/v1/events/{source}",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Hermes-Signature": f"sha256={signature}",
    },
)
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.read().decode("utf-8"))
```

Never log source secrets, Bearer tokens, or the signature input when it contains sensitive message content.

## Email mapping

A practical email payload can include:

```json
{
  "event_type": "email.received",
  "external_id": "message-id",
  "dedupe_key": "mail:account-alias:message-id",
  "payload": {
    "account": "account-alias",
    "thread_id": "thread-id",
    "message_id": "message-id",
    "from": "sender@example.test",
    "to": ["recipient@example.test"],
    "cc": [],
    "subject": "Subject",
    "received_at": "2026-07-13T15:00:00Z",
    "body_text": "Plain-text body",
    "attachment_metadata": [
      {"name": "brief.pdf", "media_type": "application/pdf", "size": 12345}
    ]
  }
}
```

Do not include an attachment body unless the deployment has explicitly accepted the privacy and database-growth impact. A preferred connector stores the attachment in an approved artifact store and supplies an immutable reference and hash.

Useful email event types include `email.received`, `email.thread.updated`, `email.label.changed`, and `email.message.deleted`. These are conventions for the planner, not separate API routes.

## Calendar mapping

```json
{
  "event_type": "calendar.event.updated",
  "external_id": "calendar-event-id",
  "dedupe_key": "calendar:work:event-id:sequence-7",
  "payload": {
    "account": "work",
    "calendar_id": "primary",
    "event_id": "event-id",
    "sequence": 7,
    "title": "Quarterly review",
    "start": "2026-07-15T14:00:00-04:00",
    "end": "2026-07-15T15:00:00-04:00",
    "timezone": "America/New_York",
    "organizer": "organizer@example.test",
    "attendees": ["operator@example.test"],
    "response_status": "accepted",
    "location": "Video call"
  }
}
```

Recommended event types include `calendar.event.created`, `calendar.event.updated`, `calendar.event.cancelled`, and `calendar.reminder.due`. A calendar reader may report invitations and schedule changes, but it must never accept, decline, create, update, or cancel an event through the inbound credential.

## Meeting mapping

```json
{
  "event_type": "meeting.transcript.ready",
  "external_id": "meeting-987654",
  "dedupe_key": "meetings:meeting-987654:transcript:v2",
  "payload": {
    "provider": "meeting-provider",
    "meeting_id": "meeting-987654",
    "title": "Project review",
    "started_at": "2026-07-13T18:00:00Z",
    "ended_at": "2026-07-13T18:45:00Z",
    "participants": ["Chris", "Teammate"],
    "transcript_version": 2,
    "transcript_text": "Bounded transcript text or an approved reference",
    "provider_action_items": [
      {"text": "Prepare the revised estimate", "owner": "Chris"}
    ]
  }
}
```

Provider-extracted action items remain claims from an external system. The supervisor may reconcile them into candidate work, ask who owns them, or mark them uncertain.

## Trust and prompt-injection handling

A valid HMAC proves that a configured connector delivered the bytes. The event is still `authenticated_untrusted`. External events are never combined with a privileged event in the same supervisor pass. Therefore:

- Text inside a message, meeting, calendar description, attachment, or linked page cannot modify system policy.
- An inbound statement such as "approve this" cannot approve an action.
- External content cannot promote itself into trusted long-term memory.
- Claimed owners, deadlines, and commitments should retain provenance and may trigger a user question.
- Provider summaries and action items are evidence, not verified facts.

Keep the rawest useful facts in the payload. Do not preface external content with trusted instructions or merge it into a system prompt inside the connector.

## Polling, push, and replay

The built-in command boundary owns its durable provider cursor in SQLite and commits it atomically with normalized events. An external polling process that uses the webhook path should maintain its own durable cursor and rely on operator event deduplication as a second line of defense.

Recommended behavior:

1. Fetch a bounded page with a read-only credential.
2. Normalize and sign each object independently.
3. Retry network errors with bounded backoff.
4. Treat `200` and `202` as accepted.
5. Do not advance the provider cursor past an event that was not accepted.
6. Re-send safely after ambiguous connection failures using the same dedupe key.
7. Alert on repeated `400`, `401`, `413`, or `500` responses.

Hermes Operator does not include provider SDKs or provider-specific logic. Deploy each reader as a small isolated executable or provider automation. Use command polling when the operator should own scheduling and cursor durability. Use webhooks when the provider already offers push or the reader must run in another security zone.

## Obsidian Inbox

When Obsidian is configured, the same observation phase reads direct Markdown children of:

```text
<operator_root>/Inbox
```

The scan is non-recursive and bounded to 100 documents and 128 KiB of total note data per cycle. Symlinked files and directories are skipped. Notes elsewhere in the vault, including projected dashboards, work notes, and promoted memory, are never ingested.

Each new or changed note becomes a `vault.note.changed` event with its relative path, parsed frontmatter, and body. A content digest deduplicates unchanged scans. Vault content is `authenticated_untrusted` because Obsidian sync tools and plugins can modify it. It may inform triage or prompt a user question, but it cannot authorize dispatch, approve an external action, or promote itself into trusted memory.

## Onboarding checklist

- Create a unique source name and HMAC secret.
- Request read-only provider scopes.
- Decide which content fields may enter the operator database and model context.
- Define stable external IDs and dedupe keys.
- Bound body and attachment sizes below `server.max_body_bytes`.
- Send fixtures in `shadow` mode.
- Confirm duplicate delivery creates one event.
- Confirm malicious instruction text stays untrusted.
- Confirm source failure cannot block other sources.
- Confirm the connector has no outbound mutation capability.
- For command polling, confirm cursor and event writes roll back together on failure.
- For Obsidian, confirm only direct notes under `<operator_root>/Inbox` are observed.
- Document retention and secret rotation.

## Rotation and disablement

To rotate a source secret, coordinate a short connector pause, update the injected environment value, restart or reload the operator, update the connector, and send a signed test event. The current service reads configuration at process start.

To disable a command source, set its `enabled` field to `false` and restart the service. To disable a webhook source, remove its configured secret and keep `allow_unsigned_webhooks = false`. An admin client can still use the webhook fallback for an unconfigured source, so inbound readers must never possess the admin token. The Hermes bridge token is accepted only for source `hermes` and is not a general connector credential.
