# HTTP API

## Scope

The standard-library HTTP API supports health, inspection, trusted operator intake, scoped Hermes observations, signed external intake, questions, exact-action approval, memory review, and runtime wakeups.

It has no external-action execute, send, publish, deploy, purchase, delete, or connector endpoint.

The default address is `http://127.0.0.1:8787`. Responses are JSON with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and `X-Request-ID`.

## Credentials

Configure distinct values:

```text
HERMES_OPERATOR_API_TOKEN=<admin-token>
HERMES_OPERATOR_BRIDGE_TOKEN=<different-bridge-token>
```

Send either as:

```http
Authorization: Bearer TOKEN
```

Authority is intentionally different:

| Credential | Access |
| --- | --- |
| Admin token | Operator reads and mutations, approval and memory review, operator ingest, wake, and fallback delivery to unconfigured event sources |
| Bridge token | The scoped read and mutation contracts under `/v1/hermes/*`, plus `GET /v1/next`, `GET /v1/questions`, and `POST /v1/events/hermes`; see the endpoint matrix |
| Source HMAC | One configured generic webhook source only |

The server rejects equal nonempty admin and bridge tokens. Never put the admin token in the Hermes worker or an inbound reader.

`GET /health` is public. If neither reader token is configured, `/v1/next` and `/v1/questions` are locally readable. Admin endpoints still fail with `503 operator_auth_unconfigured`. A non-loopback bind requires an admin token.

Use TLS termination or a private authenticated network beyond loopback. The built-in server speaks plain HTTP.

## Endpoint matrix

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | None | Minimal process liveness |
| `GET` | `/v1/status` | Admin | Canonical state snapshot |
| `GET` | `/v1/work` | Admin | Work query |
| `GET` | `/v1/next` | Admin or bridge | Ranked next work |
| `GET` | `/v1/questions` | Admin or bridge | Questions by status |
| `GET` | `/v1/hermes/status` | Bridge | Content-free runtime and operational counters |
| `GET` | `/v1/hermes/attention` | Bridge | Read-only preview of due reminders and pending questions |
| `GET` | `/v1/hermes/reminders` | Bridge | Read-only preview of due reminders |
| `GET` | `/v1/hermes/execution-contract?task_id=ID` | Bridge | Exact live capabilities for the current Hermes task |
| `GET` | `/v1/hermes/work/{id}/authorization-scope` | Bridge | Exact work and executor preview for authorization |
| `POST` | `/v1/hermes/delegation-claim` | Bridge | Fail-closed compatibility claim for a host with proven foreground delegation |
| `POST` | `/v1/hermes/attention/claim` | Bridge | Atomically claim attention for one private native-Cron delivery |
| `POST` | `/v1/hermes/work` | Bridge | Capture operator-authorized local work, including reminders |
| `POST` | `/v1/hermes/work/{id}/update` | Bridge | Apply a version-fenced local work update |
| `POST` | `/v1/hermes/work/{id}/reminder` | Bridge | Snooze, acknowledge, or complete a reminder |
| `POST` | `/v1/hermes/work/{id}/authorize` | Bridge | Record exact work authorization supplied through Hermes |
| `POST` | `/v1/hermes/questions/{id}/answer` | Bridge | Record an answer supplied by the operator in Hermes |
| `POST` | `/v1/hermes/inbound` | Bridge | Record normalized Google Workspace evidence |
| `POST` | `/v1/work/links` | Admin | Create a version-fenced relationship edge |
| `POST` | `/v1/runs/{id}/resolve` | Admin | Resolve an uncertain run with an expected-state fence and reason |
| `GET` | `/v1/approvals` | Admin | Exact action list, including content |
| `GET` | `/v1/approvals/{id}` | Admin | Exact action detail |
| `GET` | `/v1/memory` | Admin | Memory candidates |
| `GET` | `/v1/memory/{id}` | Admin | Memory detail |
| `POST` | `/v1/events/{source}` | Source HMAC, constrained bridge, admin fallback, or explicit unsigned development mode | Record an external observation |
| `POST` | `/v1/ingest` | Admin | Record an operator-trusted event |
| `POST` | `/v1/wake` | Admin | Wake the live loop |
| `POST` | `/v1/questions/{id}/answer` | Admin | Answer a question |
| `POST` | `/v1/approvals/{id}/approve` | Admin | Issue an exact, expiring grant |
| `POST` | `/v1/approvals/{id}/deny` | Admin | Deny and revoke an unconsumed grant |
| `POST` | `/v1/memory/{id}/promote` | Admin | Promote reviewed memory |
| `POST` | `/v1/memory/{id}/reject` | Admin | Reject a memory candidate |

There are no `PUT`, `PATCH`, or `DELETE` routes.

## Health and status

```bash
curl -sS http://127.0.0.1:8787/health

curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  http://127.0.0.1:8787/v1/status
```

`/health` exposes only outer status and time plus runtime `status`, `running`, and `cycle_count`. It intentionally omits configuration, paths, approvals, work content, and integration details. HTTP `200` means the process answered.

Authenticated `/v1/status` returns the canonical SQLite snapshot, including bounded work and question state, recent runs, promoted memory, and memory review counts. Its `operational_counters` object always includes pending, processing, failed, and dead-letter event counts plus pending-question, active-work, and active-run counts. These counters contain no event, question, or work content. Use the local CLI `status` or service logs for composed Hermes and Obsidian health details.

## Work queries

`GET /v1/work` accepts:

- `status`, repeated or comma-separated
- `kind`, repeated or comma-separated
- `parent_id`
- `limit`, from 1 to 1000, default 200

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  "http://127.0.0.1:8787/v1/work?status=ready,running&kind=task&limit=50"
```

`GET /v1/next?limit=5` returns ranked dependency-eligible `triage`, `ready`, `review`, and `running` work. Triage entries are review candidates, not execution authorization. The read does not rescore or change work versions. Limit is 1 to 100.

`GET /v1/questions?status=pending` accepts `pending`, `answered`, or `dismissed`.

The bridge token is intentionally limited to the explicit Hermes routes in the matrix. It can read context and content-free status; capture reversible work; claim private attention; submit version-fenced work updates; record exact answers and authorization supplied through Hermes; ingest normalized Google revisions; and post lifecycle, compatibility, and attestation evidence. The plugin uses Hermes-native confirmation for authority-bearing conversational mutations. The bridge cannot fetch the admin work graph or status snapshot, inspect or approve external-action grants, review memory, or execute outbound delivery.

For `POST /v1/work/links`, `blocker --blocks--> affected` is execution-equivalent to `affected --depends_on--> blocker`. Eligibility, run reservation and commit, dependency reopen protection, and mixed-cycle detection enforce both forms. Parent status remains explicit; derived rollup progress and health update automatically.

## Generic external webhook

The source path segment must use 1 to 64 letters, numbers, dots, underscores, or hyphens. `operator`, `system`, and `hermes` are reserved. `hermes` cannot be configured as an HMAC webhook source and accepts only the scoped bridge token.

```json
{
  "event_type": "email.received",
  "external_id": "provider-message-123",
  "dedupe_key": "gmail:account-a:provider-message-123",
  "payload": {
    "subject": "Planning follow-up",
    "from": "sender@example.test",
    "received_at": "2026-07-13T15:00:00Z",
    "body_text": "Please include the updated forecast."
  }
}
```

`event_type` and object-valued `payload` are required. `external_id` and `dedupe_key` are optional. Deduplication is scoped by source.

For a configured source secret, sign the exact request bytes:

```text
X-Hermes-Signature: sha256=<lowercase HMAC-SHA256 hex>
```

A configured source always requires its HMAC. The admin token does not bypass a configured HMAC secret. For an unconfigured source, the admin token is accepted as a delivery fallback, but normalized content remains external evidence rather than operator authority.

Success is `202` for a new event or `200` for a duplicate:

```json
{
  "created": true,
  "event_id": "evt_...",
  "trust_level": "authenticated_untrusted"
}
```

With `allow_unsigned_webhooks = true`, an unconfigured unsigned source is accepted as `untrusted`. This is for isolated development only.

## Hermes bridge intake

The bridge credential is accepted only by the scoped routes shown in the
endpoint matrix. Core inspection and attention routes include:

```text
POST /v1/events/hermes
GET /v1/next
GET /v1/questions
GET /v1/hermes/status
GET /v1/hermes/attention
GET /v1/hermes/reminders
GET /v1/hermes/execution-contract?task_id=ID
GET /v1/hermes/work/{id}/authorization-scope
POST /v1/hermes/attention/claim
POST /v1/hermes/work
POST /v1/hermes/work/{id}/update
POST /v1/hermes/work/{id}/reminder
POST /v1/hermes/work/{id}/authorize
POST /v1/hermes/questions/{id}/answer
POST /v1/hermes/inbound
POST /v1/hermes/delegation-claim
```

`GET /v1/hermes/work/{id}/authorization-scope` is the required read step before
interactive authorization. Optional `profile`, repeated or comma-separated
`skill`, and Boolean `goal_mode` query parameters select the executor shape.
The response contains the bounded `scope` document that must be shown to the
operator, plus `work_version`, `authorization_scope_revision`, and
`authorization_scope_digest`. `authorizable` is false for terminal work.

`POST /v1/hermes/work/{id}/authorize` must echo all three displayed fences and
the same executor shape:

```json
{
  "expected_version": 7,
  "expected_scope_revision": 3,
  "expected_scope_digest": "64-lowercase-hex-characters",
  "reason": "Approved this bounded implementation",
  "profile": "operator",
  "skills": ["kanban-orchestrator"],
  "goal_mode": false
}
```

Omitted executor fields use the configured work and Hermes defaults in both
steps. In one immediate SQLite transaction, the API verifies the exact work
version, scope revision, and caller-supplied digest against the live work and
submitted executor shape before it enqueues the authorization event. A changed
dependency edge, work scope, lifecycle generation, or executor shape returns
`409 state_conflict`. Terminal work is never authorizable.

Priority rescoring and nonterminal runtime status changes do not alter the
authorization scope. Scope-bearing work changes and dependency graph changes
advance both the ordinary work version and dedicated scope revision, revoke
existing execution governance, and invalidate the prior digest. Entering a
terminal state does the same. Reopening terminal work starts a fresh execution
generation with no prior card, completion evidence, or execution authority.

Ordinary lifecycle observations use the generic envelope and remain `authenticated_untrusted`.

An event whose `event_type` is `policy.attested` has a stricter fixed contract. It must use the bridge token and contain exactly the expected source, provenance, occurrence time, external identity, dedupe identity, and seven payload fields:

```json
{
  "profile": "operator",
  "plugin_version": "1.5.0",
  "policy_version": "6.0.0",
  "policy_digest": "64-lowercase-hex-characters",
  "guard_active": true,
  "policy_mode": "default_deny",
  "attested_at": "2026-07-13T15:00:00+00:00"
}
```

The API validates the exact identity digest and UTC timestamp before atomically storing monotonic profile attestation state and an audit record. Each explicitly configured Hermes profile maintains independent evidence, so multiple attested profiles are supported. Attestations are not inserted into the planner event queue and do not wake or invoke the model. A replay or older timestamp cannot replace newer evidence. Core dispatch later validates freshness and allowlists for the exact selected profile.

`policy.revoked` uses the same fixed envelope and authenticated bridge route.
Its payload adds a nonempty `reason`, requires `guard_active: false`, and binds
the identity digest to `"revoked"`, profile and policy identity, timestamp, and
reason. A newer revocation immediately replaces the cached profile state and
blocks dispatch without waiting for its TTL. A replay or older attestation
cannot overwrite it; a strictly newer valid attestation can restore service.

`GET /v1/hermes/execution-contract` requires one bounded `task_id`. It returns `authorized: true` only when that exact task is linked to canonical running work, its consumed dispatch authorization names the same task and local run, and the current contract is still valid. The response binds the task, work, profile, contract digest, run, and explicit internal capabilities. Missing, stale, terminal, blocked, foreign, or mismatched tasks fail closed. This endpoint lets the native guard authorize task-scoped local writes, tests, builds, and any future compatible foreground delegation without exposing operator mutation authority. Current background top-level delegation remains blocked on managed cards.

`POST /v1/hermes/delegation-claim` accepts exactly:

```json
{
  "task_id": "task_123",
  "requested_children": 3
}
```

For a live contract that permits delegation, the first claim for its canonical run returns:

```json
{
  "claimed": true,
  "task_id": "task_123",
  "run_id": "run_123",
  "contract_digest": "64-lowercase-hex-characters",
  "requested_children": 3,
  "reason": "claimed"
}
```

This is a compatibility contract for a Hermes host that can prove foreground delegation semantics. The core records a successful claim atomically under the canonical run ID and audits it. Current top-level Hermes `delegate_task` is background and non-durable, so the plugin blocks it on Operator-managed cards before calling this endpoint. Parallel execution currently uses independent canonical cards up to `operator.max_parallel_work`. Unmanaged interactive sessions retain Hermes-native delegation.

The bridge token cannot use `/v1/ingest`, `/v1/wake`, admin question-answer
routes, approval routes, memory routes, `/v1/status`, or the admin `GET
/v1/work` graph. Its local mutations are limited to the explicit `/v1/hermes/*`
contracts.

## Native attention delivery

`GET /v1/hermes/attention?limit=20` is a read-only preview of currently due
reminders and pending questions. `GET /v1/hermes/reminders?limit=20` is the
reminder-only preview. Neither route records a delivery, increments a counter,
or suppresses a later result. Reminder objects include `delivery_state` showing
the most recent committed claim, acknowledgement, total claim count, and next
eligible delivery time.

The native Cron delivery job uses `POST /v1/hermes/attention/claim` exactly once
per run through `operator_claim_attention`. Its optional JSON body is
`{"limit": 20}`; an empty body uses 20. The operation atomically returns due
reminders and pending questions that are outside
`native_automation.attention_redelivery_seconds`, then persists their delivery
timestamps and counts. This avoids duplicate private delivery on short Cron
cadences while permitting bounded redelivery after an interrupted run.

See [Reminder and Attention Lifecycle](REMINDERS.md) for recurrence and
version-fenced snooze, acknowledge, and complete payloads.

## Native Google intake

The installed Google Cron job uses Hermes' bundled `google-workspace` skill and account
OAuth, then calls `POST /v1/hermes/inbound` through `operator_ingest_inbound`. A request
names exactly one of `google.gmail`, `google.calendar`, or `google.meeting` and carries a
bounded list of provider IDs, revisions, event types, and object payloads. Stable
provider revision identity deduplicates retries. Accepted content is
`authenticated_untrusted`, wakes the live loop when new, and cannot grant operator
authority. The intake contract is read-only; Google mutations outside managed cards use
Hermes-native confirmation.

## Operator-trusted intake

`POST /v1/ingest` defaults source to `operator` and rejects source `system`:

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source":"operator","event_type":"operator.request","dedupe_key":"quarterly-plan:v1","payload":{"summary":"Use the board packet","allow_internal_execution":false}}' \
  http://127.0.0.1:8787/v1/ingest
```

Possession of the admin token creates operator trust, but authority still depends on the event type and exact payload fields. See [Operating Model](OPERATING_MODEL.md).

## Wake

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"operator-review"}' \
  http://127.0.0.1:8787/v1/wake
```

A wake is advisory. Record durable work or an event before waking. An optional reason is limited to 128 characters.

## Questions

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"answer":"Use Friday at 5 PM America/New_York"}' \
  http://127.0.0.1:8787/v1/questions/QUESTION_ID/answer
```

Answering atomically updates the question, creates a trusted `question.answered` event scoped to the recorded per-work scope bindings, and wakes the loop. The answer is always retained, but it grants mutation or resume authority only while each stored scope revision and digest still matches. A stale binding creates a durable operator follow-up and applies no work mutation or dispatch. An answer cannot make work executable when it was not executable when the question was created.

## Exact-action approvals

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  "http://127.0.0.1:8787/v1/approvals?status=pending_approval&limit=100"

curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  http://127.0.0.1:8787/v1/approvals/ACTION_ID
```

Approve or deny:

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8787/v1/approvals/ACTION_ID/approve

curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Recipient list is incomplete"}' \
  http://127.0.0.1:8787/v1/approvals/ACTION_ID/deny
```

Approval returns a grant ID and expiry. It does not execute the action. This API has no grant-consumption or outbound execution route. If an installation wants exact-action delivery outside Hermes-native confirmation, it may invoke the optional `hermes-outbound-broker` with the exact action and grant IDs; the broker never receives authority through this API route.

## Memory review

Memory content requires admin authentication:

```bash
curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  "http://127.0.0.1:8787/v1/memory?status=quarantined&limit=100"

curl -sS \
  -H "Authorization: Bearer $HERMES_OPERATOR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8787/v1/memory/MEMORY_ID/promote
```

Only `pending` or `quarantined` records can be promoted or rejected. Promotion is internal memory review, not authorization for an external action.

## Request and error rules

Every POST requires:

- `Content-Type: application/json`
- A valid `Content-Length`
- A JSON object body, except routes that allow an empty object
- A body no larger than `server.max_body_bytes`

Errors use:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "payload must be an object"
  }
}
```

Common statuses are `400`, `401`, `404`, `409`, `411`, `413`, `415`, `500`, and `503`.
