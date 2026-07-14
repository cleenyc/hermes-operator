# Operating Model

## Purpose

Hermes Operator turns inbound evidence and operator intent into a durable work graph, live plans, ranked next work, controlled Hermes execution, questions, review, and optional long-term memory. This document describes the behavior implemented in the current release.

## Core principles

1. SQLite holds one coherent operational record.
2. The model proposes; deterministic code authorizes and applies.
3. External content is evidence, never operator authority.
4. Existing work is mutated only against an expected version.
5. Internal work runs only through an exact, durable one-shot dispatch contract with a bounded attempt budget.
6. Hermes completion enters review and must satisfy every acceptance criterion.
7. The Operator daemon never executes external actions. Optional exact-action delivery uses the separately invoked broker; unmanaged interactive and Cron sessions use Hermes-native confirmation.
8. SQLite remains transactional memory; Obsidian is a projection with one bounded untrusted Inbox.

## Autonomy modes

The implemented modes are `shadow`, `internal`, and `active`.

### Shadow

- Ingests and processes events.
- Calls the live planner.
- Applies authorized work, hierarchy, questions, memory candidates, verification, and staged proposals.
- Records eligible Hermes dispatch requests but does not create Hermes cards.

### Internal

- Performs all shadow behavior.
- Dispatches eligible, explicitly authorized internal work through Hermes when every policy gate passes.

### Active

- Has the same implemented dispatch behavior as `internal`.
- Serves as a deployment rollout marker.
- Does not permit outbound execution.

There is no `off` or `supervised` mode in configuration. Stop the service to turn processing off. Use `shadow` to keep reasoning and state management active without new Hermes dispatch.

## Intake

### Operator intake

The CLI `ingest` command and `POST /v1/ingest` create `operator` trust events. Event type matters:

- `operator.request` may authorize work creation.
- It authorizes execution of work created from that request only when its payload has `allow_internal_execution: true`.
- Without execution authorization, the planner may still organize new operator-owned work into planning or ready states, ask a blocking question, and recommend it. Execution mode and assignee remain disabled.
- `operator.work_authorized` and `operator.work_updated` may update or dispatch only the exact `work_id` and listed capabilities.
- Answering a question produces `question.answered`, which may update only the question's recorded blocking work IDs.

An arbitrary operator-trusted event does not receive blanket mutation or dispatch authority.

### External intake

Generic webhooks accept source-specific HMAC signatures. Valid signatures yield `authenticated_untrusted`; unsigned development intake yields `untrusted`. Email, calendar, meeting, repository, and document contents cannot:

- Change policy or credentials.
- Approve an external action.
- Authorize Hermes execution.
- Promote themselves into long-term memory.
- Borrow authority from an operator event in the same model pass.

Privileged events are processed one at a time. Free-form untrusted state is redacted from their model context.

The ready-to-use Google path is a Hermes-native Cron contract using the bundled `google-workspace` skill and Hermes-managed OAuth. It reads Gmail, Calendar, and meeting evidence, then records normalized revisions through the scoped bridge. The same untrusted-content rules apply. Generic webhooks and fixed command readers remain optional paths for other providers.

### Hermes observations

Hermes lifecycle events and Kanban reconciliation remain `authenticated_untrusted`. They can supply evidence and trigger review, but they do not grant operator authority.

## Event lifecycle

```text
pending -> processing -> processed
                    \-> pending retry
                    \-> dead_letter
```

Processing uses a unique claim token and expiry. The supervisor renews its claim after the model returns and before applying the plan. If the claim has been reclaimed, the pass fails without mutation.

Every claimed event needs exactly one durable disposition: work recorded, question requested, execution reconciled, memory recorded, external action proposed, duplicate, non-actionable, or quarantined. Effect-bearing dispositions are checked against the effects that actually survived policy. Non-actionable and quarantine outcomes require a specific reason. Conservatively detected task signals, including request/task event types, structured action or deadline fields, and common imperative subjects, cannot be dismissed as non-actionable. A response that omits an event, duplicates a disposition, returns empty action arrays without a disposition, or tries to dismiss task-like intake fails and leaves the event retryable.

A validated plan, event dispositions, event consumption, finalized plan record, last-pass state, and completion audit commit in one immediate SQLite transaction. A late validation or version failure rolls back all plan effects.

## Work organization

Supported kinds:

```text
area, goal, project, milestone, task, todo, reminder, decision
```

Any item can have a parent. This supports conventional area to goal to project hierarchies without requiring a rigid type sequence.

Dependency and relationship links are:

```text
depends_on, blocks, related_to, duplicates, derived_from
```

Dependency cycles and self-links are rejected across both execution-effective edge forms. `affected --depends_on--> blocker` and `blocker --blocks--> affected` are equivalent: affected work is eligible only when the blocker is `done`. Reservation, dispatch commit, and dependency reopen protection enforce the same semantics.

The live snapshot carries active work, recent terminal history, graph links, and derived hierarchy rollups. Rollups summarize descendant status, progress, overdue count, and project health and refresh whenever an affected child changes. They are advisory derived state: they do not automatically mark a parent `done`, because the parent's own definition of done may require review beyond child completion. Parent status remains explicit while derived progress and health update automatically.

## Work lifecycle

Statuses are:

```text
inbox -> triage -> planned -> ready -> running -> review -> done
                    |          |          |
                    v          v          v
               waiting_input  blocked   waiting_input
```

`cancelled` and `archived` are terminal operating states, although authenticated direct operator CLI updates can reopen them. The model cannot create terminal work. Model-requested terminal transitions need exact operator authority or verified Hermes evidence.

Hermes reports map as follows:

- Active state moves eligible local work to `running`.
- Blocked state moves it to `blocked` and records evidence.
- Completed state moves it to `review` and emits `execution.completed`.
- Cancelled state blocks local work for operator or supervisor decision; it is not treated as trusted cancellation authority.
- Repeated missing or failed reconciliation marks a run `lost` and blocks the work.

When Hermes blocks, the active run attempt is closed and releases compute capacity. The card stays blocked. Answered context plus fresh authorization can reserve a new attempt, add the bounded answer to the same card, and unblock it. The adapter also has an authenticated run-control call for terminating native compute when canonical work becomes terminal or authorization is invalid.

## Planning

Each live pass receives:

- Current time, timezone, autonomy mode, and action mode.
- Leased triggering events as explicitly labeled untrusted evidence.
- A bounded active-work, recent-completion, relationship, hierarchy-rollup, question, run, memory, and approval snapshot.
- Limits on work operations, questions, dispatches, verification, memory, and actions.

The model returns one JSON plan. The supervisor rejects malformed operations, unknown references, invalid timestamps, terminal creates, unauthorized capabilities, out-of-date versions, unlisted profiles or skills, and unsupported action types. `operator.max_authorizations_per_pass` independently caps how many exact dispatch authorizations that pass may write.

A stable semantic idempotency key prevents the same pass from recreating the same work. Reuse is accepted only when the stored pass, source event, plan reference, and full normalized creation-identity digest match. A collision cannot inherit existing update or execution authority.

Eventless reconciliation is deliberately non-authorizing. It may adjust numeric priority factors, create triage observations, or ask nonblocking questions. It cannot issue dispatch, add work links, apply broad updates, or stage external actions.

## Prioritization

The score is deterministic and explainable. It combines:

- Impact
- Urgency
- Strategic alignment
- Dependency unlock value
- Due-date proximity or overdue state
- Age
- Estimated effort
- Confidence
- Risk
- Work status
- Dependency readiness
- Explicit operator priority
- A bounded contextual adjustment field

The ordinary store rescore uses zero contextual adjustment. `max_llm_priority_adjustment` defines the available bound for integrations, but the current supervisor does not directly set a separate adjustment.

`next` recommends dependency-eligible `triage`, `ready`, `review`, and `running` items. Triage entries surface intake for operator review without granting execution authority. The query does not rescore, mutate, or dispatch work.

## Questions and drift prevention

The model may create focused questions when context affects scope, ownership, priority, deadline, recipient, risk, or definition of done. A question can move authorized blocking work to `waiting_input`.

Answering a question:

1. Atomically marks the question answered.
2. Records the answer in audit state.
3. Creates an operator-trusted `question.answered` event.
4. Wakes the live loop.

That event can update only the work IDs already linked to the question. It cannot authorize unrelated work.

## Hermes dispatch policy

Work is dispatchable only when:

- Mode is `internal` or `active`.
- Status is `ready`.
- Execution mode is `hermes`.
- Governance records explicit execution authorization.
- At least one acceptance criterion exists.
- Dependencies are done.
- Profile is in the effective configured allowlist, has a fresh exact-profile attestation, and skills are in the configured allowlist.
- Dispatch authorization is exact, unconsumed for another card, not early, and within its attempt budget.
- Supervisor-issued authorization names a finalized plan digest, or a direct operator CLI dispatch authorization is present.
- A fresh policy attestation for the exact profile passes configured plugin, policy, and digest allowlists.
- Global run capacity is available.

The authorization digest covers the current execution contract. Changes to title, description, hierarchy, criteria, timing, profile, skills, or goal mode invalidate it. Durable authorization has `expires_at = null`, binds `not_before` to `scheduled_at`, and records `review_after` as an operational review marker. `max_attempts` is capped by `hermes.max_execution_attempts`, and the first committed card consumes the authorization for that exact run.

The database reserves one slot atomically before any Hermes call. This is a global limit across concurrent dispatcher callers. `queued`, `running`, `cancel_requested`, `lost`, and `legacy_conflict` count against it. A terminal `blocked` attempt does not. There is no implemented profile, project, or provider concurrency policy.

Enabled Hermes execution in `internal` or `active` mode requires an authenticated Kanban control base URL and token. The daemon uses them only to terminate a native run, then blocks the card through the CLI. The token is forbidden from Hermes worker and connector environment allowlists.

## Native plugin policy heartbeat

The plugin registers a local pre-tool guard before bridge startup. It then sends a synchronous attestation for its configured `HERMES_OPERATOR_PROFILE` and starts one daemon heartbeat. Install it in each explicitly allowed execution profile. The heartbeat and normal pre-LLM and lifecycle hooks share one monotonic lock and rate limiter. They refresh every 120 seconds by default, configurable from 120 to 240 seconds.

The example core TTL is 300 seconds. A missing, stale, wrong-profile, wrong-version, or wrong-digest attestation prevents new dispatch reservations. The daemon heartbeat keeps an otherwise idle healthy process fresh. Each accepted refresh updates authenticated profile state and audit directly; it does not queue planner work or wake an LLM pass. Refresh failure leaves installed bridge and guard behavior in place, while the core TTL eventually expires and blocks new reservations.

The bridge uses `HERMES_OPERATOR_BRIDGE_TOKEN`, not the admin token. It can read next work, questions, attention, and the exact live task contract; capture reversible work; submit version-fenced updates; record exact answers and authorization supplied through Hermes; ingest normalized Google evidence; claim private attention delivery; and post observations and attestations. Authority-bearing conversational operations use Hermes-native confirmation. It cannot inspect or approve external-action grants, fetch the admin status graph, or execute outbound delivery.

## Parallel execution

The planner may propose independent dispatches in one plan, bounded by `max_authorizations_per_pass`. Actual concurrent cards are bounded by `max_parallel_work`, which the dispatcher enforces against the database-wide compute-active count atomically.

Each parallel unit is an independent canonical WorkItem and Hermes card, so it retains its own status, authorization, attempt budget, verification, and audit history. Current Hermes top-level `delegate_task` is background and non-durable. Because the Operator cannot prove foreground completion semantics, the managed-card guard blocks that tool and native Kanban fanout on Operator-managed cards. Unmanaged interactive sessions retain Hermes-native delegation behavior.

The managed-card guard is task-scoped defense in depth. It does not replace Hermes host policy or claim control over interactive and Cron sessions. Outside managed cards, identifiable Cron mutations and Google writes use Hermes' native human-confirmation gate.

## Verification

Hermes completion never directly becomes `done`. The supervisor verifies only work currently in `review`, and only from an `execution.completed` event with exact adapter provenance, card identity, evidence fingerprint, and a completed local run.

Every acceptance criterion must appear exactly once in the verification result. A passing verdict requires:

- `passed: true` for every criterion.
- Nonempty evidence for every criterion.
- Confidence of at least `0.75`.

Failure moves work to `blocked`. Missing input moves it to `waiting_input`. The evidence fingerprint prevents later reconciliation of the same card result from erasing a failed review outcome.

A failed verification may produce a bounded correction attempt only from the exact completion event and prior authorization root, while the attempt budget remains. The correction gets a new canonical run, new idempotency key, and new Hermes card. By contrast, a card blocked for missing operator context resumes on the same card after answer delivery and a fresh slot reservation.

## Memory and Obsidian

The supervisor stores trusted memory candidates as `pending` and external candidates as `quarantined`. Automatic promotion is not implemented, even if `allow_memory_auto_promotion` is configured. An authenticated operator must promote or reject a candidate.

Only promoted memory is projected to Obsidian. The projector also writes a dashboard and work notes under its managed directory. The observation phase reads only direct Markdown children of `<operator_root>/Inbox`, with fixed count and byte bounds and symlinks skipped. Each new or changed note is authenticated to the configured vault surface but remains untrusted content. SQLite remains canonical, and the vault can be added, moved, or disabled without changing execution state.

Vault-wide retrieval belongs to Hermes' bundled Obsidian skill. The installed daily-briefing Cron contract uses that skill only when vault context materially helps and resolves `OBSIDIAN_VAULT_PATH` at deployment. The Operator core does not maintain a second vault-wide index.

## Native scheduled loops

`native-jobs install` creates three stable Hermes Cron jobs: read-only Google intake, private reminder and pending-question delivery, and a daily briefing. Cron supplies activation and delivery, not the plan. New provider revisions enter the durable event queue and trigger a fresh live-model pass; attention delivery uses one atomic redelivery claim; and briefings combine ranked work with optional native Obsidian context. OAuth, Gateway operation, private delivery target, and vault path remain deployment bindings.

## External actions

The model may stage proposals in a closed taxonomy. Canonicalization binds recipients, integration, action type, target, content, and attributes into an exact digest. The staged action and approval grant also have an expiry window.

Approval creates an expiring one-use grant. It does not cause execution. This daemon:

- Has no outbound connector instance.
- Has no API execute route.
- Has no `hermes-operator` outbound execute command.
- Does not give Hermes an approval-consumption tool.

The package separately installs `hermes-outbound-broker` as an optional exact-action delivery path. It is disabled by default, is not instantiated by the daemon, and must be invoked with its own configuration, exact action ID, and exact grant ID. It atomically consumes the one-shot grant and claims the immutable action before executing a deployment-fixed connector. Connector stdin and stdout are bounded strict JSON, and the process receives only a minimal safe base environment plus explicitly allowlisted values.

## Managed-card scope and optional hardening

The native plugin guard blocks known side-effect tool categories and risky terminal commands on Operator-managed cards, but it cannot prove that every custom tool, plugin, MCP server, shell, interpreter, or network route is safe. It is not an end-to-end replacement for Hermes policy.

When a deployment requires a harder no-outbound worker boundary, optional hardening includes:

1. Give the Hermes worker only the scoped bridge token.
2. Do not give it the operator admin token, approval secret, connector credential, cloud write credential, repository write credential, or browser session with write access.
3. Deny external mutation egress with operating-system, container, proxy, or network policy.
4. Allow only required model, control-plane, package, artifact, and read-only data endpoints.
5. Run `hermes-outbound-broker` under a separate identity and network policy only when approved outbound execution is required.

Without credential and egress isolation, managed-card policy remains defense in depth. This does not prevent the core autonomous workflow from operating under Hermes-native confirmation and deployment policy.
