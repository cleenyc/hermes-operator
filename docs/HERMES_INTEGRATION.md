# Hermes Native Integration

## Purpose

`integrations/hermes_operator_plugin` is an optional native Hermes plugin for the
operator control plane. It makes operator priorities and pending questions available in
Hermes, injects a small read-only planning context before each turn, and records selected
Hermes lifecycle events in the control plane.

The bridge depends on an HTTP contract, not a Hermes installation path, database path,
Obsidian vault path, or shared Python environment. Hermes and the control plane may run
on the same host, in different containers, or on separate private-network hosts.

The integration also supplies installable Hermes Cron contracts for read-only Google
Workspace intake, private reminder and pending-question delivery, and a daily briefing.
They use Hermes' bundled Google Workspace and Obsidian skills, account OAuth, Gateway,
and native delivery rather than duplicating provider SDKs, vault indexing, or scheduling
inside the Operator daemon.

```bash
hermes-operator --config operator.toml native-jobs plan
hermes-operator --config operator.toml native-jobs install --dry-run
hermes-operator --config operator.toml native-jobs install

# For reviewed upgrades of managed v0.3 or v0.4 jobs:
hermes-operator --config operator.toml native-jobs install --dry-run --reconcile
hermes-operator --config operator.toml native-jobs install --reconcile
```

## Scope and policy boundary

The plugin exposes thirteen scoped tools:

- Reads: `operator_status`, `operator_next_work`, `operator_open_questions`,
  `operator_due_reminders`, `operator_authorization_scope`, and
  `operator_diagnostics`.
- Private delivery: `operator_claim_attention`.
- Conversational management: `operator_create_work`, `operator_answer_question`,
  `operator_authorize_work`, `operator_update_work`, and
  `operator_resolve_reminder`.
- Provider evidence: `operator_ingest_inbound`.

These tools do not send messages, publish, deploy, purchase, issue approval grants, or
consume approval grants. Work creation is reversible triage. Exact question answers,
execution authorization, terminal or hierarchy changes, Cron mutations, and identifiable
Google writes require Hermes-native human confirmation outside managed worker cards.
Lifecycle POST requests record internal observations and do not constitute user approval.

The plugin also installs a required `pre_tool_call` policy hook. Hermes documents this
hook as firing before every built-in and plugin tool and accepting
`{"action": "block", "message": "..."}` as a veto. The strict task-scoped guard applies
when the hook identifies an Operator-managed Kanban card. It blocks direct managed-worker
attempts to cause external or destructive side effects. It has no configuration flag,
approval argument, model-visible token, or prompt phrase that bypasses the block.

Outside an Operator-managed card, the plugin defers normal interactive and Cron behavior
to Hermes. It requests the harness' native approval UI for identifiable scheduler and
external Google mutations. This preserves native Hermes behavior instead of attempting
host-wide plugin ordering or end-to-end mediation.

The guard covers these classes:

| Class | Examples blocked on an Operator-managed card |
| --- | --- |
| Communication | Email, chat, DM, comment, reply, and notification sends |
| Publication | Web and social publishing, releases, package publishing, deployments |
| Scheduling | Calendar create, update, cancel, invite, RSVP, and meeting join |
| Sharing and submission | File upload, document sharing, browser form interaction |
| Financial | Payment, purchase, refund, transfer, trade, and checkout mutation |
| Destructive | Delete, remove, destroy, purge, process kill, destructive shell commands |
| Permission | Role, member, ACL, IAM, moderation, and access changes |
| Code change | Push, merge, remote pull-request creation, review, and repository mutation |
| Generic mutation | Raw HTTP, every unreviewed MCP or plugin tool, device control |

On managed cards, Hermes multiplexed tools receive action-level checks. Read-only Discord fetches, cron
listing, process polling, Spotify inspection, and browser snapshots remain available.
Mutating actions through the same tools are blocked. Browser navigation, back, scroll,
click, typing, key presses, dialogs, raw CDP, raw HTTP including GET, and interactive
computer-control actions are blocked because URLs and interaction arguments can exfiltrate
data or commit external state.

Every MCP tool and every unknown plugin tool is fail-closed on managed cards, including names that appear
read-only. New tools require a code-reviewed addition to the explicit allowlist. The
terminal tool always permits a small set of local read-only inspection commands and local
Git inspection. Additional local writes, tests, and builds are allowed only when the
current Kanban task has a live execution contract containing the matching capability.
Shell escapes, network clients, interpreters, unknown commands, and commands with
external or destructive effects remain blocked. `awk` is not allowed because it can
invoke `system()`.

Explicitly reviewed local file reads and edits, operator reads,
clarification, session and skill reads, and selected dedicated read-only retrieval tools
remain usable. Kanban reads are allowed; completion, blocking, heartbeat, and comments
may target only the current live task. Native Kanban creation, linking, unblocking, and
foreign mutation are control-plane owned and blocked in the managed worker. Current
top-level `delegate_task` is also blocked there because Hermes runs it in the background
without durable foreground completion semantics. The daemon does not instantiate an
outbound broker. The separately installed `hermes-outbound-broker` is an optional path
for exact grants, not a requirement for the control plane.

Project-defined test and build scripts can execute arbitrary repository code even when
their command shape was reviewed. Therefore `local_test` and `local_build` are not a hard
no-egress boundary. Deployments that need a stronger boundary can add operating-system or
container filesystem scoping, credential isolation, and network egress controls. The
managed-card guard still blocks direct network, publication, destructive, and shell-escape forms.

Managed `kanban_complete` calls also reject explicit artifact fields and every absolute,
home-relative, Windows drive-letter, or `MEDIA:` local path in completion prose. The
check does not trust workspace containment or file existence because Hermes Gateway can
promote any readable matching path, including an outside-workspace file, after the hook
returns. Relative paths and URLs remain ordinary text. Use an unmanaged interactive turn
and Hermes-native approval when a file must be delivered.

The injected context contains an explicit statement that priorities do not authorize
external side effects. If the control plane is unavailable, context injection returns
nothing and Hermes continues normally. Only context availability is fail-open. The
local `pre_tool_call` guard does not call the service and remains active during a service
outage.

### Enforcement limits and optional defense in depth

The current official hook reliably vetoes a direct tool dispatch before its registered
handler runs. It cannot prove the implementation of an explicitly allowed tool, stop a
compromised plugin from acting during import or inside another hook, or prevent a local
file edit from triggering a privileged filesystem watcher outside the worker sandbox.
Name and argument policy is therefore defense in depth, not the hard security boundary.

Malformed arguments and internal policy-evaluation errors return a block response. They
are not allowed to escape as hook exceptions because Hermes treats plugin hook failures
as non-fatal and would otherwise continue processing.

For a deployment that requires a harder managed-worker boundary:

1. Keep messaging, Discord administration, browsers, computer control, raw MCP, and other
   mutation-capable toolsets out of the `operator` worker profile.
2. Run workers in an isolated backend with outbound network denied by default. If online
   research is required, allow only reviewed read providers through a controlled proxy.
3. Give workers no email, calendar, social, cloud administration, payment, publication,
   deployment, or outbound-broker credentials.
4. Give this plugin only `HERMES_OPERATOR_BRIDGE_TOKEN`. Never provide the admin API
   token, approval secret, or Hermes run-control token.
5. Isolate the worker filesystem from deployment watchers, host credentials, and the
   outbound broker.
6. Verify the plugin and `pre_tool_call` hook are loaded before starting worker dispatch.

Credential isolation and outbound network policy can provide a harder boundary. The hook
is a task-scoped enforcement layer and useful audit message, but this project does not
claim that it completely mediates the Hermes host or unmanaged sessions.

Hermes documents plugin failures as non-fatal to the host process. This plugin therefore
treats `pre_tool_call` registration as required and stops its own registration if Hermes
rejects the hook. Operator-managed dispatch should remain disabled until plugin health
verification succeeds; the plugin does not attempt to stop or reorder the Hermes host.

The tested host target is Hermes Agent `0.18.2`, tag `v2026.7.7.2`, commit
`9de9c25f620ff7f1ce0fd5457d596052d5159596`. The release CI installs that exact commit
and exercises the real turn-ID, hook-resolution, and completion-delivery contracts as a
required lane. A separate advisory lane runs the same contracts against current Hermes
`main`; failures there do not weaken the pinned release gate. Other versions require a
deployment-owned reviewed-host override after the real-host contracts are exercised.

The plugin records a separate `compatibility_observed` diagnostic and exposes
`operator_diagnostics`. It reports host surfaces, active profile, hook position, managed
worker task/workspace identity semantics, credential scrubbing, completion artifact
transport, and detected delegation mode without attempting to reorder Hermes plugins
and hooks. Bridge activation requires the pinned host or reviewed override plus positive
evidence for the active profile, first-valid directive behavior, first-position Operator
guard, and dispatcher ownership of `HERMES_KANBAN_TASK` and
`HERMES_KANBAN_WORKSPACE`. Unknown or incompatible required semantics leave the
local guard installed in fail-closed policy-only mode and publish a best-effort negative
policy observation. Unsupported optional hooks reduce observation only; rejection of the
required pre-tool hook disables the bridge for managed execution.

## HTTP contract

The plugin reads these endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service reachability |
| `GET` | `/v1/hermes/status` | Content-free runtime and operational counters |
| `GET` | `/v1/next?limit=N` | Prioritized work suggestions |
| `GET` | `/v1/questions?status=pending&limit=N` | Questions needing operator input |
| `GET` | `/v1/hermes/attention?limit=N` | Read-only reminder and question preview |
| `GET` | `/v1/hermes/reminders?limit=N` | Read-only reminder preview |
| `GET` | `/v1/hermes/execution-contract?task_id=ID` | Exact live task capabilities for the guard |
| `GET` | `/v1/hermes/work/{id}/authorization-scope` | Exact work and executor preview for authorization |
| `POST` | `/v1/hermes/attention/claim` | Atomic private Cron delivery claim |
| `POST` | `/v1/hermes/work` | Capture reversible work or a reminder |
| `POST` | `/v1/hermes/work/{id}/update` | Apply a version-fenced work update |
| `POST` | `/v1/hermes/work/{id}/reminder` | Snooze, acknowledge, or complete a reminder |
| `POST` | `/v1/hermes/work/{id}/authorize` | Record exact operator authorization |
| `POST` | `/v1/hermes/questions/{id}/answer` | Record an exact operator answer |
| `POST` | `/v1/hermes/inbound` | Record normalized Google evidence |
| `POST` | `/v1/hermes/delegation-claim` | Compatibility claim if a future host proves foreground delegation |
| `POST` | `/v1/events/hermes` | Required startup attestation and internal lifecycle observation |

### Required startup policy attestation

The plugin registers the local `pre_tool_call` guard first. When both
`HERMES_OPERATOR_BRIDGE_TOKEN` and `HERMES_OPERATOR_PROFILE` are configured, it then
sends one synchronous policy attestation before registering any HTTP-backed tool,
context injection, command, skill, or lifecycle observer. Registration of those bridge
surfaces continues only when the endpoint returns an authenticated-ingress
acknowledgement containing a nonempty `event_id`, a Boolean `created` value, and
`"trust_level": "authenticated_untrusted"`.

If startup or a later compatibility check proves that required guard semantics
are unavailable, the plugin sends an authenticated `policy.revoked` envelope
with `guard_active: false` and a bounded reason. The core immediately replaces
that profile's cached attestation, so managed dispatch stops without waiting
for attestation expiry.

The request envelope is:

```json
{
  "source": "hermes_plugin",
  "event_type": "policy.attested",
  "external_id": "hermes-policy:<sha256>",
  "dedupe_key": "hermes-policy:<sha256>",
  "occurred_at": "2026-07-13T22:30:00+00:00",
  "payload": {
    "profile": "operator",
    "plugin_version": "1.6.0",
    "policy_version": "7.0.0",
    "policy_digest": "<64 lowercase SHA-256 hex characters>",
    "guard_active": true,
    "policy_mode": "default_deny",
    "attested_at": "2026-07-13T22:30:00+00:00"
  },
  "provenance": {
    "origin": "hermes_plugin",
    "trust": "authenticated_untrusted"
  }
}
```

The payload contract is closed: it contains exactly the seven fields shown above and
no others. `profile` must exactly match `HERMES_OPERATOR_PROFILE`. `policy_digest` is
the lowercase SHA-256 digest of the exact loaded `policy.py` source bytes.
`policy_mode = "default_deny"` describes the Operator-managed-card branch. The same
reviewed policy deliberately defers unmanaged interactive and Cron sessions to native
Hermes behavior and confirmation.
`attested_at` must be an offset-aware ISO 8601 timestamp in UTC, and `occurred_at`
equals it. The external ID and dedupe key are identical and hash the profile, plugin
version, policy version, policy digest, and timestamp. A new plugin registration thus
produces a fresh identity, while a retry of the exact request remains idempotent.

If configuration, transport, authentication, response validation, or attestation fails,
the plugin stops bridge registration. The already-installed local guard remains active
in policy-only mode.

After a successful synchronous startup attestation, the plugin starts one daemon policy
heartbeat before enabling its bridge surfaces. The heartbeat uses `Event.wait()` rather
than an LLM pass, scheduler, cron expression, or nested agent. It contains no reasoning,
task selection, or work execution. Its only operation is renewing the startup policy
evidence for an otherwise idle but healthy Hermes process.

Every supported pre-LLM and normal lifecycle callback also opportunistically asks the
same refresher to renew the evidence. A monotonic, thread-safe rate gate shared by the
heartbeat and hooks permits only one in-flight refresh and at most one attempt per
configured interval. The heartbeat computes the remaining time from the last attempt,
so a hook that wins the race cannot cause a duplicate call or defer the next heartbeat
by two full intervals. The default interval is 120 seconds. Each permitted attempt
creates a new payload and timestamp under the same exact seven-field contract.

Refresh failures are contained and logged. They do not unregister tools, hooks, or the
local guard, and failed attempts use the same rate limit to avoid a retry storm. If
failures continue, the control plane's last accepted attestation naturally exceeds its
freshness TTL and new work reservations stop. With the default 300-second control-plane
TTL, keep the refresh interval at its 120-second default unless deployment latency
requires a larger value. The supported range is 120 through 240 seconds, and the core
TTL must remain greater than the configured interval plus an outage margin.

The heartbeat starts only after the required initial attestation is acknowledged. If
initial attestation or heartbeat thread startup fails, bridge registration stops and the
already-installed local guard remains in policy-only mode. Refresh failure does not
unregister anything; if the failure persists, the 300-second core freshness check stops
new reservations as designed.

The current official Hermes plugin API has session finalization hooks but no process- or
plugin-unload hook. Stopping on `on_session_finalize` would be incorrect because the
gateway also emits it when an idle session is garbage-collected. The heartbeat is
therefore daemon-scoped to the loaded process. A process `atexit` handler wakes and joins
it when normal interpreter shutdown permits; as a daemon it can never hold process exit
open.

Accepted attestations update a profile-scoped system-state record and audit entry
directly. They are not queued as supervisor events and do not wake the autonomy loop.
The update is monotonic, so an exact replay or older timestamp cannot replace newer
evidence.

#### Dispatcher acceptance rule

Before reserving work for any effective allowlisted Hermes profile, the control plane
requires a fresh stored profile record satisfying every condition below:

1. The record was written only by the exact `policy.attested` contract on the reserved
   `hermes` bridge route and says `authenticated_ingress: true`.
2. The payload has exactly the seven attestation fields, its `profile` exactly matches
   the profile being reserved, `guard_active` is the Boolean `true`, and
   `policy_mode` is exactly `default_deny`.
3. `plugin_version` and `policy_version` are in deployment allowlists, and
   `policy_digest` exactly matches the digest approved for that policy version. Version
   matching without digest matching is insufficient.
4. `attested_at` is valid UTC, is not unreasonably in the future, and is within the
   deployment-configured freshness window.
5. No reservation occurs when the attestation is missing, stale, malformed, for a
   different profile, or for an unknown version or digest.

The dispatcher must derive this decision from the authenticated ingress record, never
from task metadata, model output, a worker claim, or a prompt. This record is startup
evidence from possession of the scoped bridge credential, not hardware-backed remote
attestation. Credential isolation, worker network denial, and restricted toolsets are
optional hardening when the deployment needs a stronger boundary than managed-card policy.

### Task-scoped execution contract and lifecycle

Before allowing delegation, local writes, tests, builds, or a current-task Kanban
mutation, the guard resolves the exact task ID from the hook and `HERMES_KANBAN_TASK`.
Conflicting or malformed identities fail closed. It then fetches the live execution
contract and requires the same task ID, exact configured profile, canonical running
state, contract digest, and named capability. A cached prompt, worker claim, card
description, or tool argument cannot create this authority.

Current Hermes reports top-level `delegate_task` as background and non-durable. The guard
therefore blocks it on Operator-managed cards before any child is launched. Parallel
execution is represented by independent canonical WorkItems and cards up to
`operator.max_parallel_work`.

The `/v1/hermes/delegation-claim` contract remains a fail-closed compatibility surface
for a host that can prove foreground delegation semantics. If such a host is explicitly
validated, the guard must post exactly `task_id` and `requested_children`, and the core
atomically binds one claim to the canonical run. The current supported mode does not use
that path.

When a card reports blocked, the control plane accepts the event only when the card, run,
attempt, and status match the latest canonical blocked run, then closes that attempt and
releases its global compute slot. After a linked operator answer and fresh authorization,
it reserves a new attempt. It comments currently bound answer context and calls `unblock`
only when the old card's immutable contract still matches the new authorization; changed
scope creates a new card. The worker itself cannot unblock the card. If independent
verification fails after a
completion, a bounded correction uses a new run and a new Hermes card under the remaining
`max_execution_attempts` budget. These two paths deliberately differ: missing context may
resume an unchanged card, while changed scope or failed evidence starts a separately
auditable card.

If canonical work becomes terminal or its authorization is invalid while Hermes compute
is still live, the core uses its separate authenticated Kanban control token to terminate
the native run and then blocks the card. This control credential belongs only to the core
daemon and is not part of the plugin bridge environment.

Lifecycle requests use the same normalized envelope. Subagent stop observations preserve Hermes' `parent_turn_id` and `child_session_id`, so parallel children with the same role and status remain distinct:

```json
{
  "source": "hermes_plugin",
  "event_type": "hermes.subagent_stopped",
  "external_id": "hermes:<sha256>",
  "dedupe_key": "hermes:<sha256>",
  "occurred_at": "2026-07-13T12:00:00+00:00",
  "payload": {
    "task_id": "kanban-task-123",
    "parent_turn_id": "turn-parent-123",
    "child_session_id": "session-child-456",
    "child_role": "researcher",
    "child_status": "completed",
    "child_summary": "Bounded result summary"
  },
  "provenance": {
    "origin": "hermes_plugin",
    "trust": "authenticated_untrusted"
  }
}
```

Every bridge HTTP request carries the required `HERMES_OPERATOR_BRIDGE_TOKEN` as
`Authorization: Bearer <token>`. Put the service behind a private network or TLS when
it is not bound only to localhost.

Answers, work authorization and updates, reminder resolution, policy attestation, and
policy revocation also carry an HMAC made with
`HERMES_OPERATOR_BRIDGE_PROOF_SECRET`. It binds the exact purpose, method, endpoint,
body digest, timestamp, and nonce. The server consumes the nonce atomically in SQLite,
so capture, body substitution, cross-endpoint use, process restart, and replay fail
closed. The plugin removes both bridge secrets from `os.environ` immediately after
loading its private client configuration.

Use only the scoped bridge token. Never place the operator API token, approval secret,
Hermes run-control token, or any outbound connector credential in the Hermes plugin
environment. The bridge token is authorized only for the endpoints in the table above.
Its mutations are closed contracts for conversational Operator management, atomic private
attention delivery, and normalized Google evidence. The plugin applies Hermes-native
confirmation before authority-bearing conversational calls. The token cannot inspect or
approve external-action grants, fetch the admin graph, or invoke the outbound broker.

## Installation

The plugin is packaged independently from the control plane. A release wheel can be expanded into the active Hermes plugin directory without retaining a project checkout or sharing the control-plane Python environment:

```bash
export HERMES_PLUGIN_ROOT="${HERMES_HOME:-$HOME/.hermes}/plugins"
export PLUGIN_WHEEL="/path/to/hermes_operator_plugin-1.6.0-py3-none-any.whl"
export PLUGIN_STAGE="$(mktemp -d)"
python -m pip install --no-deps --target "$PLUGIN_STAGE" "$PLUGIN_WHEEL"
install -d "$HERMES_PLUGIN_ROOT"
cp -R "$PLUGIN_STAGE/hermes_operator_plugin" "$HERMES_PLUGIN_ROOT/hermes-operator"
```

For development from this repository, copy or symlink the source plugin directory. In either case, the destination name should match the manifest name:

```bash
cp -R integrations/hermes_operator_plugin \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-operator"
```

Set runtime configuration in the environment used to start Hermes:

```bash
export HERMES_OPERATOR_URL="http://127.0.0.1:8787"
export HERMES_OPERATOR_BRIDGE_TOKEN="replace-with-a-random-scoped-bridge-token"
export HERMES_OPERATOR_BRIDGE_PROOF_SECRET="replace-with-an-independent-32-byte-secret"
export HERMES_OPERATOR_PROFILE="operator"
export HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS="120"
```

Enable the opt-in plugin and restart Hermes:

```bash
hermes plugins enable hermes-operator
hermes plugins list
```

In a Hermes conversation, use `/operator status`, `/operator next`, `/operator questions`,
`/operator reminders`, `/operator add`, `/operator remind`, `/operator answer`,
`/operator authorize`, `/operator done`, `/operator snooze`, or `/operator diagnostics`.
The model can also call the thirteen registered tools. Authority-bearing tool calls use
Hermes-native confirmation where described above. The bundled
skill is namespaced as `hermes-operator:operator-workflow` when the running Hermes build
supports `ctx.register_skill()`.

Project-local installation under `./.hermes/plugins/` is also supported by Hermes, but
Hermes disables project-local plugins unless `HERMES_ENABLE_PROJECT_PLUGINS=true` is set
for a repository the operator trusts.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_OPERATOR_URL` | `http://127.0.0.1:8787` | Control-plane base URL |
| `HERMES_OPERATOR_BRIDGE_TOKEN` | required for HTTP bridge | Scoped bridge API token |
| `HERMES_OPERATOR_BRIDGE_PROOF_SECRET` | required for authority-bearing bridge calls | Independent HMAC secret of at least 32 bytes; scrubbed before tools run |
| `HERMES_OPERATOR_PROFILE` | required for HTTP bridge | Stable worker profile name used for policy attestation |
| `HERMES_OPERATOR_REVIEWED_HOST_OVERRIDE` | `false` | Deployment-owned confirmation that a non-pinned Hermes build passed the real-host contract; never bypasses semantic gates |
| `HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS` | `120` | Daemon and hook refresh attempt interval, from 120 to 240 seconds |
| `HERMES_OPERATOR_TIMEOUT_SECONDS` | `1.5` | Per-request timeout, from 0.1 to 10 seconds |
| `HERMES_OPERATOR_INJECT_CONTEXT` | `true` | Read and inject next work and questions |
| `HERMES_OPERATOR_EMIT_LIFECYCLE` | `true` | Record supported lifecycle observations |

The URL accepts `http` or `https` and rejects embedded credentials, query strings, and
fragments. No environment variable names a Hermes home or vault directory.

If the bridge token or profile is absent or invalid, or if startup attestation is not
acknowledged, the plugin registers only the local `pre_tool_call` policy. It does not
register HTTP-backed tools or lifecycle observers. This policy-only fallback prevents a
missing service credential from silently removing the worker guard. A production
deployment should still treat missing bridge health and attestation as a startup failure
because priorities, questions, lifecycle reconciliation, and work reservation are
absent.

Managed bridge activation is pinned to Hermes `0.18.2` by default. A different host
requires `HERMES_OPERATOR_REVIEWED_HOST_OVERRIDE=true` after its real-host suite is
reviewed. Profile, first-hook, directive, task/workspace ownership, credential scrub,
and completion-artifact transport checks still fail closed under an override.

## Hermes capabilities used

The implementation follows the current official general-plugin conventions:

- A plugin directory contains `plugin.yaml` and `__init__.py` with `register(ctx)`.
- Tools use `ctx.register_tool(...)`, accept an argument dictionary plus `**kwargs`, and
  return JSON strings for both success and failure.
- Lifecycle callbacks use `ctx.register_hook(...)` and accept `**kwargs` for forward
  compatibility.
- `pre_tool_call` may veto a direct tool dispatch with
  `{"action": "block", "message": "..."}`.
- `pre_llm_call` may return `{"context": "..."}`.
- `ctx.register_command(...)` adds the `/operator` command.
- `ctx.register_skill(...)` exposes a namespaced workflow skill.

The plugin currently observes `post_llm_call`, `on_session_start`, `on_session_end`,
`subagent_start`, and `subagent_stop`. Turn completion records contain message lengths,
not raw user or assistant message bodies. Subagent records contain bounded goals and
summaries as observational evidence; they do not become canonical child execution by
themselves. Stop events use the native Hermes `parent_turn_id` and `child_session_id`
identities and retain a stable content fallback only for hosts that omit a child session ID.

The control plane passes every validated effective skill as a repeated native
`hermes kanban create --skill NAME` flag and passes `--goal` when the exact dispatch
contract enables goal mode. Parallel compute uses multiple canonical cards. Current
background `delegate_task` and durable fanout through `kanban_create` or `kanban_link`
are not authorized on managed cards. Operator metadata remains in SQLite for
reconciliation and audit.

The core configuration forms an effective profile allowlist from `profile`,
`default_assignee`, `orchestrator_profile`, and `allowed_profiles`. Install the plugin in
each selected profile and set that profile's exact `HERMES_OPERATOR_PROFILE`. Internal and
active mode also require `hermes.control_base_url` and the daemon-only token named by
`hermes.control_token_env`. That credential authenticates native run termination. It
must not be included in `hermes.pass_env` or the plugin environment.

The observational APIs are registered individually. If a Hermes release lacks a
lifecycle observer, command, or skill API, that capability is skipped. The
`pre_tool_call` policy hook and `ctx.register_tool` are required native contracts. The
plugin registers the policy hook before its tools and stops registration if the policy
hook is rejected.

## Version and compatibility notes

This integration was checked on 2026-07-13 against the official Hermes Agent plugin,
hook, and skills documentation on the repository's `main` line. The consulted pages do
not publish a minimum Hermes semantic version for every API. They do state that the
plugin allow-list migration belongs to configuration schema version 21 or later.

Before enabling this plugin on an older deployment, confirm these commands exist and
that the plugin hook reference includes blocking `pre_tool_call` behavior:

```bash
hermes plugins list
hermes plugins enable hermes-operator
```

Use `HERMES_PLUGINS_DEBUG=1 hermes plugins list` when discovery or registration fails.
Hermes also writes plugin failures to its normal agent log. Since Hermes evolves quickly,
the HTTP boundary is the stable integration contract and the small registration shim is
the only component expected to need release-specific adaptation.

## Verification

Run the bridge tests without installing Hermes:

```bash
python -m unittest discover -s integrations/hermes_operator_plugin/tests -v
```

Then test against a running control plane:

1. Confirm `/operator status` returns a successful health response.
2. Confirm plugin registration first records authenticated `policy.attested` state and
   audit for the exact configured profile, version, and expected policy digest, without
   adding a pending planner event.
3. Confirm a missing, stale, wrong-profile, or unknown-digest attestation prevents the
   dispatcher from reserving work for that profile.
4. Advance a test clock by 120 seconds with no Hermes turn. Confirm the daemon heartbeat
   sends one fresh attestation without invoking an LLM or task runner.
5. Race a pre-LLM callback with the heartbeat wake. Confirm the shared rate limiter sends
   exactly one attestation. Simulate a refresh failure and confirm the guard stays
   installed while reservation eligibility expires after the core TTL.
6. Confirm `/operator next` matches `GET /v1/next`.
7. Confirm `/operator questions` shows only pending questions.
8. Dispatch several independent canonical WorkItems. Confirm their cards run in parallel
   without exceeding `operator.max_parallel_work`.
9. On an Operator-managed card, confirm current top-level `delegate_task`, native Kanban
   fanout, explicit artifact fields, absolute/home/Windows paths, and `MEDIA:` completion
   delivery fail closed, including an outside-workspace file. In an unmanaged
   interactive session, confirm delegation retains Hermes-native behavior.
10. Confirm authenticated lifecycle events that the host supplies appear in the
   control-plane event log with stable task and child identities.
11. Confirm there is no plugin tool or slash command that approves or performs an
   external-facing action. Confirm identifiable interactive or Cron Google writes show
   the Hermes-native confirmation gate.
12. In a non-production sandbox, ask a managed worker to call `browser_navigate`, `browser_click`,
   send a Discord message, run `git push`, run an HTTP GET and POST, call an opaque tool,
   and use `execute_code`. Confirm each call returns the policy block message before any
   handler executes.
13. Confirm `read_file`, task-scoped `patch`, `git diff`, operator reads, current-task
   Kanban lifecycle operations, and contract-authorized local tests and builds remain
   available on managed cards. Confirm native Kanban create, link, unblock, and current
   background delegation remain unavailable there.
14. Install the three native jobs in a test profile. Confirm Google revisions enter as
   authenticated untrusted evidence, attention is atomically suppressed within its
   redelivery window, and the briefing can use the native Obsidian skill.
15. Start Hermes without `pre_tool_call` support or simulate a rejected hook. Confirm the
   plugin stops registration and the deployment health check prevents worker dispatch.

## Official references

- [Hermes plugins overview](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
- [Hermes plugin authoring guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Hermes plugin hook reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [Hermes built-in tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference)
- [Hermes skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes Agent source repository](https://github.com/NousResearch/hermes-agent)
