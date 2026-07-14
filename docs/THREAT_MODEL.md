# Threat Model

## Security objective

The primary invariant is:

> This daemon does not execute external-facing communications, publication, financial actions, destructive actions, permission changes, repository writes, or generic external mutations.

It may stage and approve an exact action, but it contains no outbound connector instance and exposes no daemon API or `hermes-operator` execution route. The separately installed `hermes-outbound-broker` is an optional exact-action delivery path when a separately authorized operator invokes it with a one-shot grant. Unmanaged interactive and Cron sessions remain governed by Hermes-native confirmation and harness policy.

The broader objective is to prevent untrusted evidence, stale model output, concurrent processes, or a compromised worker from silently acquiring execution authority.

## In scope

- SQLite canonical state and service lease.
- Live model prompts and plans.
- Operator CLI and HTTP API.
- Generic signed inbound webhooks.
- Hermes CLI adapter, dispatcher, runs, and reconciliation.
- Native Hermes plugin, bridge credential, pre-tool guard, and policy heartbeat.
- Exact-action staging and grants.
- Separately deployed outbound broker and fixed-argv connector boundary.
- Obsidian projection.
- Optional deployment credential, process, and network hardening.

## Trust boundaries

| Boundary | Trust treatment |
| --- | --- |
| Authenticated operator CLI/API | Operator authority, still limited by event type and exact payload |
| Scoped native plugin bridge | Authenticated Hermes observation, not operator authority |
| HMAC connector | Authenticated delivery of untrusted external content |
| Model output | Untrusted plan proposal validated by code |
| Hermes worker result | Execution evidence requiring reconciliation and verification |
| Obsidian vault | Projection destination plus bounded untrusted Inbox, never execution authority |
| Separate outbound broker | Optional, installed but disabled by default; separate configuration and explicit invocation |

## Protected assets

- Operator intent, work status, priorities, deadlines, and hierarchy.
- Execution authorization and dispatch contracts.
- Admin, bridge, and Hermes run-control credentials.
- Inbound source HMAC secrets.
- Model and data-provider credentials.
- Approval action content and one-use grants.
- Promoted long-term memory.
- Hermes workspaces, tools, profiles, and artifacts.

## Threats and implemented controls

### Prompt injection through email, meetings, or documents

Threat: external text instructs the model to change policy, approve an action, dispatch a worker, or alter unrelated work.

Controls:

- Signed external events remain `authenticated_untrusted`.
- Privileged events are processed in isolated one-event passes.
- Free-form untrusted work, question, and memory text is redacted from authority-bearing snapshots.
- Specific event types grant only specific capabilities and work IDs.
- Protected metadata keys such as governance and dispatch authorization cannot be supplied by the model.
- Eventless reconciliation cannot authorize execution or broad mutation.
- External memory is quarantined for operator review.
- Native Google intake is a read-only Hermes Cron contract and the bridge accepts only
  normalized `google.gmail`, `google.calendar`, and `google.meeting` evidence. Google
  writes outside managed cards use Hermes-native confirmation.

Residual risk: the model may still misclassify untrusted evidence into low-impact triage state or create excessive questions. Review provenance, use body limits, and keep event-attempt limits conservative.

### Model endpoint credential leakage or hostile response

Threat: a configured model endpoint redirects the provider credential, returns an oversized body, or uses ambiguous JSON to influence validation.

Controls:

- OpenAI-compatible base URLs reject embedded credentials, queries, and fragments, and require HTTPS except for exact loopback hosts.
- The client refuses redirects, keeping the bearer credential on the configured origin.
- Provider responses are capped at 4 MiB.
- Duplicate keys and `NaN` or infinity constants are rejected in both the provider envelope and extracted model plan.

### Stale or racing model plans

Threat: operator or dispatcher state changes after the model snapshot, and stale output overwrites it.

Controls:

- Existing work operations carry positive `expected_version` values.
- Links, blocking questions, verification, and dispatch use version fences.
- Work versions increment on material change.
- A mismatch rolls back the entire supervisor transaction.
- Event lease tokens are renewed after the model call; a reclaimed event cannot be applied by the stale owner.
- Queued dispatch reservations prevent mutation of contract-bound work fields.
- New `depends_on` and `blocks` edges are rejected while either endpoint has a compute-active or uncertain canonical run. `blocker --blocks--> affected` is execution-equivalent to `affected --depends_on--> blocker`; mixed cycles are rejected and a completed blocker cannot reopen while affected work has such a run.

### Partial plan application

Threat: early model operations commit before a later operation fails, leaving inconsistent state or active authorization.

Controls:

- Each normalized supervisor plan applies inside one `BEGIN IMMEDIATE` SQLite transaction.
- Nested store calls reuse the same transaction connection.
- Plan operations, event consumption, finalized plan state, and audit completion commit or roll back together.
- The canonical normalized plan has a SHA-256 digest.
- Supervisor-issued dispatch authorization is inert until the matching finalized pass record exists with the same digest.

### Duplicate or concurrent event processing

Threat: multiple workers process the same input or a late failure handler changes a newly claimed event.

Controls:

- Source-scoped dedupe identities.
- Event state, owner, unique claim token, and expiry.
- Completion and failure updates require the current claim token.
- Event pass IDs are deterministic for a claimed event set.
- Failed events retry up to `event_max_attempts`, then enter dead letter.

### Multiple service instances

Threat: two daemons perform eventless planning, reconciliation, or dispatch against one database.

Controls:

- One named SQLite service lease per database.
- Startup fails if another owner holds the lease.
- A process-local asynchronous heartbeat renews the lease during long Hermes and projection operations.
- Every runtime component renews leadership before operation.
- Execution-contract lookup checks the leader fence before and after canonical authorization evaluation and rechecks current dependency satisfaction.
- Loss of leadership raises a state conflict and stops that component from dispatching.

Residual risk: this is a local SQLite lease, not a distributed consensus system. Use one active service and a local filesystem.

### Dispatch contract substitution

Threat: authorization for one task, profile, or scope is reused after work content changes.

Controls:

- Governance must explicitly authorize execution.
- Authorization carries exact work ID, selected allowlisted profile, skills, goal mode, issuer, issue time, `not_before`, `review_after`, durable lifetime, authorization root, attempt budget, and contract digest.
- Contract digest binds kind, title, description, parent, criteria, timing, operator priority, profile, effective skills including defaults, and goal mode.
- Supervisor authorization includes pass ID and finalized plan digest.
- Dispatcher re-reads and revalidates work after atomic reservation.
- Reservation stores the work version and contract digest.
- Reservation commit repeats version, state, and dependency checks.
- A live worker contract also rechecks dependencies and the leader fence before returning internal capabilities.
- Commit consumes the authorization for the exact local run and Hermes card. A verification retry can retain only the authorization root and unused bounded attempts.

### Global capacity race

Threat: concurrent dispatcher callers each observe free capacity and exceed `max_parallel_work`.

Controls:

- Capacity count and queued-run insert occur in one immediate transaction.
- A partial unique index allows only one ordinary capacity-active row per work item across `queued`, `running`, `cancel_requested`, and `lost`.
- Capacity counts `queued`, `running`, `cancel_requested`, `lost`, and quarantined `legacy_conflict` runs across the database.
- A Hermes-blocked attempt is closed and releases compute capacity. A governed resume must reserve a fresh slot before unblocking the same card.
- Queued reservations are not aged out automatically because a lost create response may hide live remote compute. Idempotent reconciliation or explicit operator resolution is required.

There are no profile, project, or provider capacity pools. Deploy separate instances only with separate databases and authority domains.

### Native compute survives revocation or terminal work

Threat: canonical work is cancelled, archived, done, or loses authorization while a Hermes worker continues running.

Controls:

- Enabled Hermes execution in `internal` or `active` mode is rejected at configuration time unless `hermes.control_base_url` and the token resolved from `hermes.control_token_env` are present.
- The control URL must be an HTTP or HTTPS base URL without embedded credentials, query, or fragment.
- Reconciliation discovers the current native run ID, sends an authenticated termination request, then blocks the Kanban card to prevent redispatch.
- The control token is daemon-only and is forbidden from Hermes, planner, inbound-reader, bridge, and outbound-connector environment allowlists.
- Failed termination does not release uncertain capacity or claim that execution stopped; it remains fail closed for operator recovery.

### Fabricated worker completion

Threat: a worker or forged observation claims work is complete.

Controls:

- Hermes completion moves work to `review`, not `done`.
- Verification requires source `hermes`, exact `hermes-kanban` provenance, work ID, card ID, evidence fingerprint, and completed local run.
- Every acceptance criterion must be assessed exactly.
- Passing requires specific evidence and confidence of at least `0.75`.
- Native artifact declarations and protected verification contracts are checked by a deterministic, model-free gate. Paths are confined to configured roots; traversal, symlinks, special files, missing content, type or digest mismatches, and byte/count overruns fail closed.
- Canonical contracts may name only deployment-approved fixed-argv checks with bounded cwd, environment, timeout, and output. Worker evidence cannot select a check.
- Filesystem hashing and fixed checks execute once outside SQLite write transactions. Their report is bound to work version, execution scope, card, run, attempt, fingerprint, canonical result, verification inputs, and artifact digests before fast transactional application.
- An applicable deterministic failure overrides a model's passed verdict after exact binding validation.
- Failed verification is persisted and the same evidence cannot automatically regress it to review.

Residual risk: existence, hashing, and a successful configured check do not prove arbitrary semantic correctness. A compromised worker may also influence project-defined test scripts. Keep verifier commands deployment-owned, run them under a restricted identity, and use human review for consequential or subjective output.

### Native plugin bypass

Threat: a custom tool, renamed tool, shell, interpreter, browser, plugin, or MCP server bypasses the pre-tool guard.

Controls:

- Plugin registers a `pre_tool_call` guard before bridge startup; its Operator-managed-card branch is default deny.
- Guard blocks known communication, calendar, financial, destructive, permissions, repository-write, browser-mutation, generic HTTP, and risky terminal patterns.
- Guard has no approval input, so an action cannot be unlocked by adding an `approved` argument.
- Dispatcher requires a fresh policy attestation for the exact selected allowlisted profile.
- Plugin and policy versions plus exact policy source digest are allowlisted.
- Plugin sends synchronous startup evidence and maintains a daemon heartbeat, with hooks sharing the same rate limiter.
- Task-scoped writes, tests, builds, delegation, and Kanban lifecycle mutation require a live execution-contract lookup for the exact current task.
- Current top-level `delegate_task` is background and non-durable, so it and native Kanban fanout are blocked on Operator-managed cards. Parallelism comes from independent canonical cards capped atomically by `max_parallel_work`.
- Outside managed cards, interactive and Cron sessions defer to Hermes-native policy; identifiable scheduler and external Google mutations use native human confirmation.

Residual risk: name and argument inspection is not complete mediation. The plugin attestation proves bridge credential possession and reports loaded source, but it is not hardware-backed attestation. Reviewed `local_test` and `local_build` command shapes can invoke project-defined scripts, which may execute arbitrary repository code.

Optional hardening for deployments that require a stronger worker boundary:

1. Hermes worker has only the scoped bridge token, never the admin token.
2. Worker has no outbound connector, cloud write, repository write, payment, mail, messaging, calendar-write, or approval credentials.
3. Operating-system or container sandboxing can limit the worker to the authorized workspace and deny network egress by default, including for project test and build scripts.
4. Custom tools, plugins, and project scripts run in the same constrained identity and network namespace.
5. `hermes-outbound-broker` uses a different process identity, credentials, and network policy.

Without these deployment controls, the managed-card guard is defense in depth, not a hard no-outbound guarantee. It is not claimed as complete mediation for the Hermes host.

### Policy attestation replay or staleness

Threat: an old or different-profile attestation is reused to authorize new work.

Controls:

- Policy event requires the bridge token and a fixed exact envelope.
- Profile, plugin version, policy version, digest, active guard, mode, and UTC time are validated.
- External and dedupe IDs are derived from attestation identity.
- Core freshness TTL defaults to 300 seconds in the example.
- Plugin refresh defaults to 120 seconds through one daemon heartbeat and opportunistic hooks.
- Reservation binds the digest of the exact stored attestation state checked.

Failure behavior: initial attestation or heartbeat startup failure leaves the plugin in guard-only mode. Later refresh failure leaves installed behavior intact, but the core TTL expires and blocks new reservations.

### API credential confusion

Threat: a worker or connector receives operator mutation authority.

Controls:

- Admin and bridge tokens are separate configuration fields and equal values are rejected.
- Bridge token is confined to explicit Hermes routes for context, attention, exact task contracts, reversible work capture, version-fenced work updates, exact operator answers and authorization, normalized Google intake, compatibility evidence, and lifecycle observations.
- Authority-bearing conversational bridge calls are gated by Hermes-native confirmation outside managed worker cards.
- Approval, memory, work, status, ingest, wake, and question-answer routes require admin authority.
- Per-source HMAC credentials are independent and retain external trust.
- A configured HMAC source cannot be bypassed with the admin token.

### External action execution without approval

Threat: model, daemon, or worker sends or publishes content without final approval.

Controls:

- Closed external action taxonomy.
- Exact canonical digest binds all side-effect-relevant fields.
- Grant is expiring and one use.
- SQLite validation, grant consumption, and transition of the immutable action to `executing` are atomic.
- Daemon composition root does not instantiate `OutboundBroker` or outbound action connectors. Configured command connectors are read-only inbound readers.
- No daemon HTTP or `hermes-operator` execute command exists.
- Native plugin exposes no approval or grant-consumption tool.
- Hermes work descriptions state the outbound boundary.
- `hermes-outbound-broker` is an optional disabled-by-default path requiring a separate TOML file, exact action ID, exact grant ID, and a deployment-fixed connector.
- Connector argv cannot be supplied by model output; input and output are strict and byte bounded, and its environment includes only a minimal safe base plus explicitly named values.

Residual risk: a broker crash after a connector side effect but before result recording produces an unknown outcome. The consumed grant prevents silent replay. An independently credentialed worker is outside the daemon boundary; deployments needing stronger isolation can add credential and egress controls.

### Approval content substitution

Threat: content, recipients, target, connector, or options change after approval.

Controls:

- Action intent is canonicalized before digesting.
- Grant binds the exact digest.
- Any changed side-effect field produces a different digest.
- Denial revokes unconsumed grant state.
- The separate broker revalidates the action, recipient, content, target, attributes, integration, and type bindings before its atomic claim.

### Webhook spoofing or replay

Threat: an attacker submits fabricated or repeated provider events.

Controls:

- HMAC-SHA256 over exact body for configured sources.
- Constant-time comparison.
- Source syntax and reserved-source validation.
- The reserved `hermes` route accepts only the scoped bridge token and cannot be replaced by an HMAC webhook configuration.
- Source-scoped deterministic deduplication.
- Body size and JSON schema bounds.
- Even authenticated webhook content remains untrusted.

Residual risk: the webhook format has no independent timestamp nonce. Rotate a compromised secret, pause its reader, and use provider object versions in dedupe keys.

### Obsidian path or content attack

Threat: projection escapes the vault, overwrites unrelated notes, or vault content gains authority.

Controls:

- Conservative vault discovery and explicit-path preference.
- Managed root and path-safety validation.
- Atomic filesystem replacement.
- Managed body markers preserve operator content outside generated regions.
- Source content cannot terminate a managed marker early.
- Only direct Markdown children of `<operator_root>/Inbox` are read.
- Inbox scans are non-recursive, count and byte bounded, and skip symlinks.
- Inbox notes always enter as `authenticated_untrusted` events and cannot grant authority.
- Projected dashboard, work, and memory directories are not read as input.
- Only promoted memory is projected.

Residual risk: Obsidian plugins or sync systems may execute code or publish note content. Review those separately and do not put secrets or the SQLite database in the vault.

Vault-wide retrieval in the native briefing uses Hermes' bundled Obsidian skill and `OBSIDIAN_VAULT_PATH`. The core does not maintain a second vault-wide index, so Hermes skill and vault-plugin security remain deployment concerns.

### Secret leakage to Hermes child process

Threat: ambient service credentials are inherited by Hermes or a subagent.

Controls:

- CLI adapter constructs a small safe environment.
- Only names in `hermes.pass_env` are added.
- Admin, bridge, approval, run-control, and outbound secrets are not passed automatically. Config validation forbids configured control-plane secret names.

Operator requirement: audit `pass_env`, service environment, wrapper scripts, profile files, browser sessions, and mounted credential stores.

### SQLite corruption or unsafe storage

Threat: power loss, cloud synchronization, network locking, or manual file copy corrupts canonical state.

Controls:

- WAL, foreign keys, immediate transactions, uniqueness constraints, and schema versioning.
- Database and sidecar files are restricted to owner permissions where supported.
- Idempotent initialization and migration.

Operator requirement: keep the database on a reliable local filesystem and use the SQLite backup API.

## Availability and recovery

- A model failure rolls back the pass, retries the event, and eventually dead-letters it.
- A reconciliation or event-processing failure prevents dispatch in that runtime cycle.
- A Hermes command failure is isolated per work item and audited.
- A blocked Hermes attempt closes and releases compute capacity; same-card resume requires answered context, fresh authorization, and a new slot.
- Failed verification retry uses a distinct card and cannot exceed the durable authorization attempt budget.
- Repeated card lookup failure marks a run lost and blocks work for review.
- An unavailable Obsidian vault does not block canonical operation.
- An unavailable approval service fails closed for external proposals.
- Loss of leader lease prevents further component work.

## Security verification checklist

- Admin and bridge tokens differ.
- Managed Hermes worker has no admin token.
- If a hard worker boundary is required, outbound credentials and protected mutation egress are isolated, including from project-defined test and build scripts.
- Native plugin policy digest matches the reviewed allowlist.
- Startup attestation succeeds for the exact dispatched profile.
- Heartbeat interval is below core attestation TTL.
- A stale or wrong-profile attestation blocks reservation.
- Concurrent dispatchers do not exceed global capacity.
- Work changes invalidate a queued dispatch contract.
- Privileged and external events are never processed in one pass.
- A late invalid plan operation rolls back earlier operations.
- Hermes completion enters review and cannot bypass exact criterion verification.
- Approval alone does not execute in the daemon; optional broker delivery requires a separate invocation.
- Signed webhook replay returns the original source-scoped event.
- Vault projection never becomes execution input.

## Explicit non-controls

Do not assume this release provides:

- Hardware-backed worker attestation.
- A complete sandbox for arbitrary Hermes tools.
- Provider-specific connector security.
- Automated token or cost budgets.
- Profile, project, or provider concurrency quotas.
- Transactional outbox delivery.
- Active-active service failover.
- A hard sandbox for project-defined test or build scripts.
- Automatic recovery or replay after an outbound connector's outcome becomes unknown.

These require deployment controls or additional components outside the current implementation.
