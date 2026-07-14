# Hermes Operator

Hermes Operator is a portable, event-driven autonomy control plane for Hermes Agent. It maintains durable goals, projects, tasks, reminders, dependencies, questions, runs, memory candidates, and exact-action approvals in SQLite. A live model pass reconciles new evidence with that state, while deterministic policy decides what may mutate and what may be dispatched to Hermes.

It is installed independently from Hermes. The Hermes executable, model endpoint, API credentials, database, and optional Obsidian vault are deployment-time settings. The vault can be bound after the service is already operating.

The daemon has no outbound connector and no outbound execution route. It may stage and approve an exact external action, but it cannot send, publish, deploy, purchase, delete, or otherwise execute that action. The package also installs `hermes-outbound-broker`, a disabled-by-default executable that must be deployed and invoked separately with connector credentials the daemon and Hermes worker never receive.

Project scope: [docs/PROJECT_SCOPE.md](docs/PROJECT_SCOPE.md)  
Release history: [CHANGELOG.md](CHANGELOG.md)  
Maintainer: [@cleenyc](https://github.com/cleenyc)

## Implemented control plane

- Live `asyncio` loop woken by durable events, operator answers, Hermes observations, and recovery ticks.
- SQLite canonical state with WAL, foreign keys, event leases, retries, dead-letter state, audit records, work hierarchy, dependency links, runs, questions, reviewed memory, and exact-action approvals.
- One active control-plane leader lease per database. A second service instance fails closed until the lease expires or is released.
- A structured OpenAI-compatible or local-command planner contract.
- One atomic SQLite transaction for each validated supervisor plan, event consumption, finalized plan digest, and audit result.
- Event authority isolation. A privileged event is processed alone, untrusted text is redacted from authority-bearing context, and specific event types grant only specific capabilities.
- Optimistic version fences on model-requested work updates, links, questions, verification, and dispatch decisions.
- Deterministic priority scoring across impact, urgency, alignment, dependency value, due date, age, effort, confidence, risk, state, and operator priority.
- Hermes Kanban dispatch through public CLI commands plus an authenticated run-control endpoint for terminating native compute. The adapter uses `create`, `show`, `list`, `comment`, `block`, `unblock`, and `runs`.
- Atomic run-slot reservations with one compute-active run per work item and a database-wide `max_parallel_work` cap. A blocked Hermes attempt is closed and releases compute capacity.
- Finalized-plan, exact-contract, single-profile, skill, timing, attempt-budget, work-version, and fresh policy-attestation checks before dispatch.
- Native Hermes plugin with read-only operator tools, bounded context injection, lifecycle observations, a default-deny pre-tool guard, and policy-attestation refresh.
- One flat foreground `delegate_task` batch per canonical run, with one to three task-scoped children for bounded parallel compute. Native Kanban child creation, background delegation, and nested orchestrator roles are blocked.
- Parallel command polling and signed generic webhooks for email, calendar, meeting, repository, and other inbound readers. Provider SDKs stay outside the core.
- Exact-action, expiring, one-use approval grants bound to action type, integration, recipients, target, attributes, and content digest.
- Separate fixed-argv outbound broker with atomic grant consumption, minimal connector environments, strict bounded JSON, and replay-safe audit state.
- Reviewed long-term memory and an optional, rebuildable Obsidian projection.
- CLI, local HTTP API, Docker, Compose, and systemd artifacts.

## How a live pass works

```text
signed event or operator request
  -> durable SQLite event inbox
  -> live model plan over current canonical state
  -> schema, authority, version, and policy validation
  -> one atomic plan transaction and finalized plan digest
  -> deterministic reprioritization
  -> atomic Hermes run reservations within global capacity
  -> Hermes reconciliation and evidence-bound review
  -> optional Obsidian projection
```

Timers wake and reconcile the loop. They do not contain the plan. Eventless reconciliation cannot authorize new execution or broad work mutation.

## Quick start

Requirements:

- Python 3.11 or newer
- A model endpoint compatible with `POST /chat/completions`, or a fixed command that reads a JSON prompt on stdin and emits a JSON object
- Hermes only when native execution is enabled

Install and create a configuration:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
hermes-operator --config operator.toml init
```

Set a real model in `operator.toml`, then inject secrets:

```bash
export OPENAI_API_KEY="replace-with-provider-key"
export HERMES_OPERATOR_API_TOKEN="replace-with-a-random-admin-token"
hermes-operator --config operator.toml doctor
hermes-operator --config operator.toml run-once
hermes-operator --config operator.toml run
```

The generated configuration starts in `shadow` mode with Hermes and Obsidian disabled.

For the native Hermes bridge, use a different scoped credential and identify the worker profile:

```bash
export HERMES_OPERATOR_URL="http://127.0.0.1:8787"
export HERMES_OPERATOR_BRIDGE_TOKEN="replace-with-a-different-random-bridge-token"
export HERMES_OPERATOR_PROFILE="operator"
```

The admin and bridge tokens must be distinct. The bridge token can read next work, questions, and the exact live task contract; post Hermes observations and policy attestations; and atomically claim the one bounded delegation batch for that canonical run. It cannot inspect approvals, mutate operator work, answer questions, approve actions, or invoke the outbound broker.

## First operations

Record an operator request. `allow_internal_execution` is an explicit capability for work created from this request:

```bash
hermes-operator --config operator.toml ingest \
  --type operator.request \
  --payload '{"summary":"Prepare the quarterly planning packet by Friday","allow_internal_execution":false}'
```

Inspect state:

```bash
hermes-operator --config operator.toml status
hermes-operator --config operator.toml next --limit 10
hermes-operator --config operator.toml work list
hermes-operator --config operator.toml question list
hermes-operator --config operator.toml approval list
hermes-operator --config operator.toml memory list
```

Answer a clarification:

```bash
hermes-operator --config operator.toml question answer QUESTION_ID \
  "Use the board packet as the source of truth"
```

Create and explicitly dispatch directly managed work:

```bash
hermes-operator --config operator.toml work add "Draft planning summary" \
  --kind task \
  --status ready \
  --execution none \
  --criterion "A Markdown draft exists" \
  --criterion "Every metric cites its source"

hermes-operator --config operator.toml work dispatch WORK_ID \
  --profile operator
```

`work dispatch` writes a durable one-shot authorization for the exact current contract. `not_before` is bound to the work schedule, `review_after` marks when the authorization should be reviewed operationally, and `max_attempts` is capped by `hermes.max_execution_attempts`. This authorization has no automatic wall-clock expiry, but contract changes invalidate it and the first dispatch consumes it. The dispatcher still checks work version, contract digest, allowlists, dependencies, acceptance criteria, capacity, attempt budget, and policy attestation. Live model plans can issue at most `operator.max_authorizations_per_pass` exact authorizations in one pass.

## Autonomy modes

| Mode | Planner may update canonical work | Hermes dispatch | External execution |
| --- | --- | --- | --- |
| `shadow` | Yes, within event authority | No | No |
| `internal` | Yes, within event authority | Yes, when every gate passes | No |
| `active` | Yes, within event authority | Same implemented behavior as `internal` | No |

`active` is an operational rollout marker in this release. It does not add capabilities beyond `internal` and does not relax the outbound boundary.

This table describes the daemon. The separate outbound broker remains disabled until it is configured and deliberately invoked after an exact approval.

## Hermes integration and attestation

The CLI adapter uses safe argv execution with `shell=False` and passes only a small operating-system environment plus explicitly named `hermes.pass_env` entries.

```toml
[hermes]
enabled = true
binary = "/absolute/path/to/hermes"
profile = "operator"
board = "default"
default_assignee = "operator"
orchestrator_profile = "operator"
default_skills = []
allowed_profiles = ["operator"]
allowed_skills = []
pass_env = []
dispatch_authorization_ttl_seconds = 86400
max_execution_attempts = 3
control_base_url = "https://hermes-control.internal"
control_token_env = "HERMES_KANBAN_CONTROL_TOKEN"
control_timeout_seconds = 10
require_policy_attestation = true
policy_attestation_ttl_seconds = 300
allowed_plugin_versions = ["1.2.0"]
allowed_policy_versions = ["3.0.0"]
allowed_policy_digests = ["6b8b21ef6d4a7f7ee5d04c9cf8b4a2fe15e9ed434d42980c879fda150df21d2f"]
```

This release requires `profile`, `default_assignee`, `orchestrator_profile`, and every `allowed_profiles` entry to resolve to the single attested `operator` profile in `internal` and `active` modes. Parallel work happens inside an authorized card through one foreground, flat `delegate_task` batch of at most three children per canonical run. Before Hermes invokes that tool, the plugin atomically consumes a core-backed claim keyed to the canonical run ID. The durable claim survives plugin and worker restarts, so a second batch remains blocked even if the first tool invocation fails. Unmanaged Kanban child creation and nested orchestrator delegation are also blocked.

The plugin sends one synchronous attestation at registration, then starts one daemon heartbeat and shares the same refresh limiter with normal Hermes lifecycle hooks. The default refresh interval is 120 seconds; the core example accepts an attestation for 300 seconds. Dispatch fails closed when evidence is absent, stale, for another profile, or outside the configured plugin version, policy version, or policy digest allowlists. Internal and active execution also require `control_base_url` and the token named by `control_token_env`; the daemon uses that authenticated control endpoint to terminate native compute and then blocks the card. Never add the control token to `hermes.pass_env`.

When a Hermes card blocks for missing context, reconciliation closes that run attempt and releases its compute slot. After the operator answers a linked question and the work is freshly authorized, the dispatcher reserves a new slot, comments the bounded answer context onto the same card, and unblocks it. A failed independent verification uses a new card and a new run attempt instead. Retries retain the original authorization root and cannot exceed `max_execution_attempts`.

The plugin guard is defense in depth, not a complete security boundary. Contract-authorized local tests and builds can invoke arbitrary project-defined code. For a hard no-outbound guarantee, the autonomous Hermes worker must receive no admin token or outbound service credential and must run in an operating-system or container sandbox with scoped filesystem permissions and network egress denied to mail, messaging, calendar, publishing, financial, repository-write, and generic mutation endpoints. See [Hermes Integration](docs/HERMES_INTEGRATION.md) and [Threat Model](docs/THREAT_MODEL.md).

## Inbound surfaces

Provider-specific readers remain outside the core. The autonomy loop can poll them as fixed command argv with an atomic durable cursor, or each reader can send a normalized event to:

```text
POST /v1/events/{source}
```

Use a separate HMAC secret per webhook source. Command readers receive only explicitly named provider environment variables and return a bounded JSON event page. Both paths produce `authenticated_untrusted` content. Email, meetings, webpages, and provider summaries cannot change policy, approve an action, or authorize execution.

See [Connectors](docs/CONNECTORS.md).

## Obsidian late binding

SQLite is the source of truth. Obsidian is a rebuildable human-readable projection. Leave it disabled until the vault is known:

```toml
[obsidian]
enabled = false
vault_path = ""
discover = true
operator_root = "Hermes Operator"
write_mode = "projection"
```

Later, set `vault_path`, set `HERMES_OPERATOR_VAULT`, or project explicitly:

```bash
hermes-operator --config operator.toml project --vault /absolute/path/to/vault
```

Add `--create` only when intentionally creating a new vault. The projector writes a dashboard, work notes, and promoted memory below `operator_root`. The live observation phase reads only direct Markdown children of `<operator_root>/Inbox`, with strict count and byte bounds. Inbox notes are untrusted evidence and never execution authority.

## Exact-action approval

Review an action before approving:

```bash
hermes-operator --config operator.toml approval show ACTION_ID
hermes-operator --config operator.toml approval approve ACTION_ID
```

Approval creates an expiring one-use grant. It does not execute the action. Any side-effect-relevant change produces a different digest and requires a new approval. There is no outbound execution route in the daemon.

To execute an approved action, deploy the broker separately, copy and review its disabled example, configure one deployment-owned fixed-argv connector, then deliberately enable and invoke it:

```bash
cp config/outbound.example.toml config/outbound.toml
# Edit connector argv, pass_env, database_path, and set enabled = true.
hermes-outbound-broker --config config/outbound.toml execute ACTION_ID \
  --grant-id GRANT_ID
```

The broker atomically verifies and consumes the exact grant while claiming the action. Its connector receives bounded JSON containing the approved action and binding digests, and must return a JSON object with `"ok": true`. A replay, expired or revoked grant, changed recipient, content, target, attributes, integration, or action type fails closed. Broker invocation is separate from approval and is the only installed path that can cross the external side-effect boundary.

## Deployment and verification

Use a local filesystem with reliable SQLite locking. Do not put the live database in an Obsidian vault, network share, or cloud-sync directory.

Compose bind-mounts a root-level `operator.toml`. Create and edit it before starting:

```bash
cp config/operator.example.toml operator.toml
# Edit operator.toml and set llm.model.
export OPENAI_API_KEY="replace-with-provider-key"
export HERMES_OPERATOR_API_TOKEN="replace-with-random-admin-token"
docker compose up -d --build
```

Compose sets `HERMES_OPERATOR_BIND_HOST=0.0.0.0` inside the container while publishing only to host loopback.

```bash
make test
make check
make dist
```

Tests use temporary or in-memory integrations and require no live Hermes, vault, model endpoint, or outbound network.
`make dist` cleans generated build state first, then creates independent core and
native-plugin wheels in `dist/` without network or dependency resolution.

Documentation:

- [Architecture](docs/ARCHITECTURE.md)
- [Operating Model](docs/OPERATING_MODEL.md)
- [Configuration](docs/CONFIGURATION.md)
- [API](docs/API.md)
- [Connectors](docs/CONNECTORS.md)
- [Hermes Integration](docs/HERMES_INTEGRATION.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Threat Model](docs/THREAT_MODEL.md)
- [Runbook](docs/RUNBOOK.md)

## License

MIT. See [LICENSE](LICENSE).
