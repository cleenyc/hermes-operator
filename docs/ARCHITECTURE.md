# Implemented Architecture

## Scope

Hermes Operator is a portable control plane placed beside Hermes Agent. It owns planning state and governance, calls a live model to reconcile that state, and uses Hermes Kanban as the execution boundary for authorized internal work.

The implementation does not import Hermes internals and does not depend on a Hermes Python environment. Integration occurs through public `hermes kanban` and `hermes cron` CLI commands, the authenticated Kanban run-control endpoint used for termination, and a native HTTP bridge plugin. The Google account, private delivery target, and Obsidian vault path are deployment bindings and can be supplied later.

## Authority model

| Component | Canonical authority |
| --- | --- |
| SQLite | Work intent, hierarchy, status, dependencies, questions, approvals, memory review, dispatch governance, run records, audit, and service leases |
| Hermes Kanban | Execution progress and worker-produced evidence after an authorized card is created |
| Obsidian | Rebuildable projection plus one bounded untrusted Inbox; Hermes' native skill owns optional vault-wide retrieval |
| Inbound connector | Delivery provenance only, never operator authority |
| Model | Proposed plan only, never direct capability |

SQLite is the only transactional source of truth. Hermes completion is evidence, not final approval. Obsidian Inbox content is untrusted evidence and is never read into execution authority.

## Runtime flow

```text
Google Cron, HTTP webhook, operator CLI/API, answer, or Hermes observation
                    |
                    v
             SQLite event inbox
                    |
                    v
        one active service leader lease
                    |
                    v
    live model pass over bounded current state
                    |
                    v
 schema + authority + version + policy validation
                    |
                    v
 atomic SQLite plan transaction and finalization
                    |
                    v
 deterministic scoring and atomic run reservation
                    |
                    v
      Hermes Kanban CLI and run control
                    |
                    v
      reconciliation, evidence, and review
                    |
                    v
       optional Obsidian state projection
```

The loop is event-driven. A recovery tick finds missed wakeups and expired leases. Periodic reconciliation reads linked Hermes cards and performs an eventless supervisor pass. Eventless passes may rescore and observe state, but they cannot authorize new execution, broad work mutation, or external-action staging.

## Single active leader

`OperatorService` obtains the `operator-control-plane` lease in SQLite at startup. The lease owner includes the configured instance ID and a random process identity. Its lifetime exceeds the model timeout and event lease. A process-local asynchronous heartbeat renews it during long Hermes or projection operations, and every runtime component also checks it before work.

A second service using the same database fails closed while the lease is live. This is process leadership, not distributed database clustering. The supported deployment runs one active service against one local SQLite database.

## Event processing and authority isolation

Events carry a source, type, payload, trust level, provenance, deduplication identity, state, attempt count, lease owner, lease token, and expiry.

Important rules:

1. External HMAC proves source possession but produces `authenticated_untrusted`, not operator trust.
2. `operator`, `system`, and `hermes` are reserved external webhook source names. The Hermes source accepts only the scoped bridge token.
3. A privileged event is processed in an isolated one-event supervisor pass.
4. In an authority-bearing pass, free-form text from untrusted work, questions, and memory is redacted from the model snapshot.
5. Authority is capability-specific, not inferred from a trusted-looking string.

The implemented authority mappings are:

| Event type | Authority |
| --- | --- |
| `operator.request` | Create work; may authorize execution of newly created work only when `allow_internal_execution` is exactly `true` |
| `operator.work_authorized` | Update or dispatch only the exact work scope digest and approved executor shape for listed capabilities |
| `operator.work_updated` | Update only the exact work scope digest for listed capabilities |
| `question.answered` | Update only current per-work scope bindings recorded by that question; resume only authority that already existed |
| `system.*` | Update or dispatch only exact entries in `authorized_work_bindings` |
| External and Hermes observations | Evidence, triage, quarantine, and verification input; no operator capability |

An event lease token is renewed after the model returns. If another worker reclaimed the event, the stale pass cannot apply.

## Model transport boundary

The OpenAI-compatible transport accepts only an HTTP or HTTPS base path without embedded credentials, query, or fragment, and requires HTTPS except for exact loopback hosts. It refuses redirects so the provider credential stays on the configured origin. Provider responses are capped at 4 MiB; duplicate keys and non-finite JSON constants are rejected in both the response envelope and the extracted plan before schema and authority validation.

## Atomic supervisor transaction

The model returns one structured plan containing an explicit disposition for every claimed event, work operations, questions, dispatch proposals, memory candidates, verifications, and external-action proposals. The supervisor normalizes and validates shape, limits, references, timestamps, status values, allowlisted worker profiles, skills, and expected versions before the transaction can commit. A disposition must be backed by the effect it claims, such as applied work, a created question, or a verification. `duplicate` must identify existing work. `non_actionable` and `quarantined` require an auditable reason. A quarantined task-like signal deterministically creates a non-executable decision item and a bound pending question, so the source event may be finalized without disappearing from the active attention workflow. An empty plan cannot silently consume an event. `operator.max_authorizations_per_pass` caps exact dispatch authorizations in one plan independently of event and operation limits. Idempotent create reuse must match the original pass, source event, plan reference, and full normalized creation-identity digest, so a model-supplied key collision cannot borrow an existing task's authority.

The normalized plan is canonicalized and hashed with SHA-256. Plan application then occurs inside one `BEGIN IMMEDIATE` SQLite transaction. Nested store calls share that connection. The same transaction:

- Applies authorized work, link, question, memory, verification, and staged-action changes.
- Writes exact dispatch authorization metadata that carries the pass ID and plan digest.
- Writes one durable `event_dispositions` record for every claimed event.
- Marks every still-owned event processed.
- Writes `supervisor.pass:{pass_id}` with `finalized = true` and the same digest.
- Writes the last-pass state and completion audit.

Any exception rolls back the plan and finalization together. The event failure path runs after rollback and either retries the event or moves it to dead letter.

The dispatcher treats supervisor-issued authorization as inert unless the matching finalized pass record exists with the same pass ID and digest.

## Version-fenced mutations

Every model-requested update of existing work carries `expected_version`. Existing endpoints of a dependency link carry their expected versions. Blocking questions, verification, and dispatch also carry version fences. Interactive authorization first reads a bounded scope preview, then must echo its exact work version, scope revision, SHA-256 digest, and executor shape. The digest binds title, description, criteria, parent hierarchy, schedule, recurrence, verifier contract, profile, effective skills, and goal mode.

SQLite increments `work_items.version` on material mutation. A mismatch raises a state conflict and rolls back the entire supervisor plan. Model output therefore cannot silently overwrite operator or dispatcher changes made after the snapshot.

Priority rescoring and nonterminal runtime lifecycle changes do not invalidate an otherwise identical approval. Scope-bearing field changes, executor reassignment, explicit Hermes execution disable, authority quarantine, and run resolution increment `authorization_scope_revision`, revoke execution governance, and remove pending dispatch authorization. New `depends_on` and `blocks` edges increment both ordinary work versions and both scope revisions. Entering or leaving terminal work also starts a new scope generation and revokes authority; terminal scope cannot be edited without reopening in the same mutation. Reopening detaches the old Hermes card and clears runtime completion evidence before the item can be freshly authorized. Before every authorized update and dispatch, the supervisor rechecks the persisted scope revision and digest. It also rechecks the exact profile, effective skill set, and goal mode before dispatch. A stale authorization, duplicate authorization presented during a live canonical run, or stale question binding cannot mutate work; it creates a durable operator follow-up or an audited rejection instead.

Queued dispatch reservations add a stronger contract-field fence. Dependency semantics are stricter: SQLite rejects new `depends_on` or `blocks` edges while either endpoint has a compute-active or uncertain canonical run in `queued`, `running`, `cancel_requested`, `lost`, or `legacy_conflict`. It also prevents a completed dependency from reopening while dependent work has one of those runs.

## Work graph and status

Kinds are `area`, `goal`, `project`, `milestone`, `task`, `todo`, `reminder`, and `decision`. Any work item may have a parent, so the hierarchy is flexible rather than enforced as one fixed tree.

Statuses are:

```text
inbox, triage, planned, ready, running, waiting_input,
blocked, review, done, cancelled, archived
```

Links support `depends_on`, `blocks`, `related_to`, `duplicates`, and `derived_from`. SQLite prevents self-links and mixed dependency cycles. `affected --depends_on--> blocker` and `blocker --blocks--> affected` are execution-equivalent: affected work is eligible only when the blocker is `done`. Eligibility, run reservation and commit, and dependency reopen protection enforce both directions.

The planner snapshot includes bounded relationship edges and recent completed work, rather than only active nodes and aggregate counts. A durable derived rollup for each parent summarizes direct children, all descendants, progress, status counts, overdue work, and health (`empty`, `on_track`, `active`, `at_risk`, `waiting_input`, `blocked`, or `complete`). Child creation, reparenting, due-date changes, and status changes refresh every affected ancestor. Rollups live outside `work_items`, so recalculation does not invalidate optimistic work versions or automatically declare a project complete. Parent status remains an explicit decision even while derived progress and health update automatically.

## Priority and next work

Priority is deterministic. The engine scores impact, urgency, strategic alignment, unlock value, due date, age, effort, confidence, risk, status, dependency readiness, and explicit operator priority. Contextual adjustment is bounded by `policy.max_llm_priority_adjustment`; ordinary rescoring currently supplies no model adjustment.

The next-work query returns ranked `triage`, `ready`, `review`, and optionally `running` items whose dependencies are satisfied. Triage entries surface untrusted intake for review without making it executable. The query is read-only and does not rescore, mutate, or dispatch work.

## Hermes dispatch

The supported transport is a local or wrapped public CLI plus an authenticated HTTP call for native run termination. Required CLI capability discovery checks for:

```text
create, show, list, comment, block, unblock, runs
```

The adapter does not implement generic Hermes card update, profile discovery, or arbitrary remote API transport. It dispatches each bounded canonical WorkItem through one authorized Kanban card assigned to an effective allowlisted profile. Every execution-contract lookup rechecks dependency satisfaction and the service leader fence before and after resolving canonical state, so a late dependency or lost lease fails closed.

Parallel execution comes from several independent canonical cards reserved up to `operator.max_parallel_work`. Current Hermes top-level `delegate_task` is background and non-durable, so its foreground completion cannot be bound to a canonical run. The plugin therefore blocks it and native Kanban fanout only when the hook identifies an Operator-managed card. Unmanaged interactive Hermes sessions retain native delegation and harness behavior. The plugin still records available lifecycle observations as evidence.

Internal and active mode require `hermes.control_base_url` and the token resolved from `hermes.control_token_env`. When canonical work becomes terminal or authorization is invalid while a native run remains live, the adapter calls `/api/plugins/kanban/runs/{run_id}/terminate`, then blocks the card through the CLI. Failure leaves the local run fail closed rather than pretending compute stopped.

Before creating a card, the dispatcher verifies:

- Autonomy mode is `internal` or `active`.
- Work is `ready`, uses `execution_mode = "hermes"`, and has no active run.
- Dependencies are done and acceptance criteria are present.
- Governance explicitly authorizes execution.
- Dispatch profile is in the effective configured allowlist, has a fresh exact-profile attestation, and skill values match the configured allowlist.
- Durable authorization is not yet consumed for another run, `not_before` matches the work schedule, and its attempt budget is valid.
- Exact work ID, profile, skills, goal mode, and contract digest match current work.
- A supervisor authorization points to a finalized plan with the same plan digest, or a direct operator CLI authorization is present.
- Fresh policy attestation exists for the exact worker profile when required.

The dispatch contract digest binds the work ID, kind, title, description, parent, acceptance criteria, due and scheduled times, operator priority, profile, effective skills after configured defaults are added, and goal mode. Authorization uses `lifetime = "until_consumed_or_contract_change"`, `expires_at = null`, and a stable `authorization_root`. `review_after` is an operational review marker rather than an automatic expiry. `max_attempts` cannot exceed `hermes.max_execution_attempts`.

## Atomic global concurrency

`reserve_run_slot` uses `BEGIN IMMEDIATE` to count every compute-active or uncertain run and insert one queued reservation atomically. Counted states are `queued`, `running`, `cancel_requested`, `lost`, and `legacy_conflict`. This fail-closed accounting prevents an uncertain remote execution from releasing capacity prematurely. A Hermes-blocked run is closed with `finished_at`, so it preserves attempt history without occupying compute capacity.

- One ordinary active run per work item through a partial unique index covering `queued`, `running`, `cancel_requested`, and `lost`. Migrated duplicate rows are quarantined as `legacy_conflict`, remain capacity-active, and require explicit operator resolution.
- One database-wide `operator.max_parallel_work` cap across concurrent dispatcher callers.
- Current work version, ready state, Hermes execution mode, and dependency readiness.
- Exact contract digest and, when enabled, the digest of the policy-attestation state used at reservation time.

After Hermes creates or resolves the card, `commit_dispatch_reservation` atomically links the card, rechecks the work version, dependencies, and exact policy-state digest captured by the reservation, changes the run to `running`, consumes the exact authorization for that run and card, and changes work to `running`. A policy revocation committed during remote creation therefore prevents the local running transition. Queued reservations are not aged out automatically because a lost create response can leave real remote compute. Recovery either rediscovers the idempotently created card with the same immutable execution-contract snapshot or requires explicit operator resolution.

No profile-specific, project-specific, or model-provider-specific concurrency pools are implemented.

## Per-profile worker policy attestation

The native plugin registers a local `pre_tool_call` guard before it initializes the HTTP bridge. It then sends a synchronous `policy.attested` envelope through the scoped bridge token. The API validates it and writes monotonic profile evidence directly to authenticated system state and audit. It does not place heartbeat attestations in the planner event queue or wake the autonomy loop. Each Hermes profile that can receive Operator work installs the plugin with its own `HERMES_OPERATOR_PROFILE` value. Dispatch requires current accepted evidence for the exact selected profile. The fixed payload binds:

- Hermes profile
- Plugin version
- Policy version
- SHA-256 digest of the loaded policy source
- Active guard state
- `default_deny` mode
- UTC attestation time

The example core configuration accepts plugin `1.5.0`, policy `6.0.0`, and the included policy digest for 300 seconds. After synchronous startup attestation, the plugin starts one daemon heartbeat that attempts a fresh attestation every 120 seconds by default. Pre-LLM and lifecycle hooks opportunistically use the same monotonic lock and rate limiter, so competing refresh paths do not duplicate a call. A stale or invalid attestation prevents new run reservation for that profile.

The heartbeat is process-scoped because current Hermes exposes no plugin-unload hook. It uses a process-exit wake and bounded join. A refresh failure leaves the local guard and bridge installed, but core freshness expires and blocks subsequent reservations.

If a compatibility check proves the required guard semantics unavailable, the
plugin sends an authenticated `policy.revoked` observation. The core replaces
that profile's cached attestation with `guard_active: false` immediately, so
dispatch fails closed without waiting for the attestation TTL. Only a strictly
newer valid attestation can restore the profile.

Attestation proves possession of the bridge credential and reports plugin state. It is not hardware-backed evidence and is not a substitute for process isolation.

## Reconciliation and verification

The dispatcher maps observed Hermes status into running, blocked, review, or lost local state. It does not mark work `done` merely because Hermes reports completion.

When Hermes reports blocked, the current run attempt becomes terminal `blocked` and releases global compute capacity. The lifecycle edge is accepted only when its card, run, attempt, and status match the latest canonical run. Once a linked operator question is answered and the exact work is freshly authorized, the dispatcher reserves a new attempt and capacity slot. It reuses and unblocks the card only when the prior immutable execution contract exactly matches the current scope; only answers with current bindings enter the resume comment. A scope change starts a new card instead. If the unblock response is lost, the queued reservation and same card identity support idempotent recovery.

Completion closes the run, moves canonical work to `review`, and queues a distinct evidence event. A failed independent verification can authorize a correction only while the durable authorization root has remaining attempts. That correction receives a new run ID, new attempt number, new idempotency key, and new Hermes card. This prevents failed evidence from being overwritten on the completed card and makes each verification retry independently auditable.

A completion event is bound to:

- Source `hermes`
- Adapter provenance `hermes-kanban`
- Exact work ID and card ID
- A completion evidence fingerprint stored in the work metadata
- A completed local run for the same card

The supervisor can mark review work done only when its verification lists every acceptance criterion exactly, every criterion has specific passing evidence, and confidence is at least `0.75`. A separate deterministic gate inspects every native Hermes artifact declaration and every artifact or named check in the canonical protected `verification_contract`. It confines paths to configured roots, rejects traversal and symlinks, computes SHA-256 content identities under count and byte limits, and runs only deployment-approved fixed argv with bounded time, output, cwd, and environment. The supervisor runs that gate once, outside SQLite write transactions, after validating the canonical completion identity. It binds and caches the result using work version, execution-scope digest, card, run, attempt, evidence fingerprint, canonical result digest, verification-input digest, and observed artifact digest. The final transaction performs only fast identity and digest checks. An applicable deterministic failure overrides a model's passed verdict. Failed or inconclusive verification moves work to `blocked` or `waiting_input`.

## Approval and outbound integration

External action proposals use a closed taxonomy and canonical exact-action digest. Approval grants are expiring and one use. When deployed, the optional `hermes-outbound-broker` atomically validates and consumes the exact grant while claiming the action for execution.

The service composition root does not create an `OutboundBroker`, does not register an outbound action connector, and exposes no outbound API or daemon command. Read-only inbound paths can record evidence but cannot execute an approved action. The broker is an optional different executable with a different TOML file and a deliberate invocation containing the exact action and grant IDs. Deployments that need a harder boundary can also give it a separate process identity, environment allowlist, and network policy.

The broker selects only a deployment-owned fixed-argv connector. It sends bounded strict JSON containing the exact action and binding digests, requires bounded strict JSON success output, records claim and result audits, and rejects replay or any action, recipient, content, target, attribute, integration, or type mismatch. A crash after the one-shot claim is treated as an unknown outcome and cannot silently replay the external side effect.

The native Hermes plugin guard adds task-scoped defense for managed cards; it is not claimed as host-wide or end-to-end enforcement. Interactive and Cron mutations outside managed cards use Hermes-native confirmation. Optional hardening for a stricter deployment includes:

- No admin API token or outbound connector credential in the Hermes worker.
- Only the scoped bridge token in the plugin environment.
- Operating-system, container, or network policy that denies the worker outbound access to mutation endpoints.
- A separate broker identity and network zone for any enabled outbound connector.

## Native and generic inbound observation

`POST /v1/events/{source}` accepts a normalized JSON envelope. A configured source uses HMAC-SHA256 over the exact body. Source and dedupe key are scoped together in SQLite, so independent connectors cannot collide.

The installed Google intake contract runs under Hermes Cron, uses the bundled `google-workspace` skill and Hermes-managed account OAuth, and posts normalized Gmail, Calendar, and meeting revisions through the scoped bridge. Google content remains authenticated untrusted evidence. The job prompt is read-only and forbids sends, label changes, RSVPs, event edits, sharing, and other provider mutations.

Generic signed endpoints and fixed command readers remain available for other providers or deployment-specific alternatives. Command readers are polled in parallel before each supervisor pass, and their cursor advances atomically with accepted events.

## Obsidian projection

Vault binding is optional. `HERMES_OPERATOR_VAULT` overrides the configured path and enables projection. When no vault is configured, the rest of the system continues normally.

Separately, the installed native briefing contract can use Hermes' bundled Obsidian skill and `OBSIDIAN_VAULT_PATH` for vault-wide retrieval. The Operator core does not duplicate that index.

The projector writes:

- `Dashboard.md`
- One note per active or terminal work item
- One note per promoted memory record

Writes are restricted to the managed root and preserve content outside managed markers. The live SQLite database must not be stored in the vault.

The observation phase separately scans direct Markdown children of `<operator_root>/Inbox`. The scan is non-recursive, bounded by document count and total bytes, skips symlinks, and deduplicates by path plus content. Every note enters as `authenticated_untrusted`; projection directories are not read.

## Explicit non-features

The current release does not implement:

- A transactional outbox or downstream event bus.
- Provider-specific SDKs inside the Operator daemon. Google Workspace intake is implemented through Hermes-native Cron and skill integration.
- Direct internal tool execution outside Hermes Kanban.
- Generic Hermes card update calls.
- Remote Hermes Kanban HTTP transport beyond authenticated native-run termination.
- Automated Hermes profile or project discovery. Multiple explicitly configured and independently attested profiles are supported.
- Profile, project, or model-provider concurrency quotas.
- Cost or token budgets.
- A daemon-hosted outbound broker or outbound API route.
- Multi-node active-active service operation.

These are possible extension points, not current behavior.
