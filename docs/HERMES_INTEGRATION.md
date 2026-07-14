# Hermes Native Integration

## Purpose

`integrations/hermes_operator_plugin` is an optional native Hermes plugin for the
operator control plane. It makes operator priorities and pending questions available in
Hermes, injects a small read-only planning context before each turn, and records selected
Hermes lifecycle events in the control plane.

The bridge depends on an HTTP contract, not a Hermes installation path, database path,
Obsidian vault path, or shared Python environment. Hermes and the control plane may run
on the same host, in different containers, or on separate private-network hosts.

## Security boundary

The plugin intentionally exposes only three read-only tools:

- `operator_status`
- `operator_next_work`
- `operator_open_questions`

It has no tool or command for sending messages, publishing, deploying, purchasing,
answering a user question, approving an action, issuing an approval grant, or consuming
an approval grant. Lifecycle POST requests only record internal observations. They do
not execute work and do not constitute user approval.

The plugin also installs a required `pre_tool_call` policy hook. Hermes documents this
hook as firing before every built-in and plugin tool and accepting
`{"action": "block", "message": "..."}` as a veto. The guard blocks direct worker
attempts to cause external or destructive side effects. It has no configuration flag,
approval argument, model-visible token, or prompt phrase that bypasses the block.

The guard covers these classes:

| Class | Examples blocked in a worker |
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

Hermes multiplexed tools receive action-level checks. Read-only Discord fetches, cron
listing, process polling, Spotify inspection, and browser snapshots remain available.
Mutating actions through the same tools are blocked. Browser navigation, back, scroll,
click, typing, key presses, dialogs, raw CDP, raw HTTP including GET, and interactive
computer-control actions are blocked because URLs and interaction arguments can exfiltrate
data or commit external state.

Every MCP tool and every unknown plugin tool is fail-closed, including names that appear
read-only. New tools require a code-reviewed addition to the explicit allowlist. The
terminal tool always permits a small set of local read-only inspection commands and local
Git inspection. Additional local writes, tests, and builds are allowed only when the
current Kanban task has a live execution contract containing the matching capability.
Shell escapes, network clients, interpreters, unknown commands, and commands with
external or destructive effects remain blocked. `awk` is not allowed because it can
invoke `system()`.

Explicitly reviewed local file reads and edits, operator tools, bounded delegation,
clarification, session and skill reads, and selected dedicated read-only retrieval tools
remain usable. Kanban reads are allowed; completion, blocking, heartbeat, and comments
may target only the current live task. Native Kanban creation, linking, unblocking, and
foreign mutation are control-plane owned and blocked in the worker. An action that has
received user approval still does not run through Hermes. The daemon does not instantiate
an outbound broker. The separately installed `hermes-outbound-broker` may consume the
exact one-shot grant only under its own isolated credentials and network policy.

Project-defined test and build scripts can execute arbitrary repository code even when
their command shape was reviewed. Therefore `local_test` and `local_build` are not a hard
no-egress boundary. Run Hermes workers in an operating-system or container sandbox with
network egress disabled and filesystem access limited to the authorized workspace. The
guard still blocks direct network, publication, destructive, and shell-escape forms.

The injected context contains an explicit statement that priorities do not authorize
external side effects. If the control plane is unavailable, context injection returns
nothing and Hermes continues normally. Only context availability is fail-open. The
local `pre_tool_call` guard does not call the service and remains active during a service
outage.

### Enforcement limits and required defense in depth

The current official hook reliably vetoes a direct tool dispatch before its registered
handler runs. It cannot prove the implementation of an explicitly allowed tool, stop a
compromised plugin from acting during import or inside another hook, or prevent a local
file edit from triggering a privileged filesystem watcher outside the worker sandbox.
Name and argument policy is therefore defense in depth, not the hard security boundary.

Malformed arguments and internal policy-evaluation errors return a block response. They
are not allowed to escape as hook exceptions because Hermes treats plugin hook failures
as non-fatal and would otherwise continue processing.

For the production `operator` worker profile:

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

Credential isolation and outbound network policy are the hard boundary. The hook is a
second enforcement layer and a useful audit message, but it must not be the only control
preventing an autonomous worker from reaching external systems.

Hermes documents plugin failures as non-fatal to the host process. This plugin therefore
treats `pre_tool_call` registration as required and stops its own registration if Hermes
rejects the hook, but an operator must still prevent worker startup when plugin health
verification fails.

## HTTP contract

The plugin reads these endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service reachability |
| `GET` | `/v1/next?limit=N` | Prioritized work suggestions |
| `GET` | `/v1/questions?status=pending&limit=N` | Questions needing operator input |
| `GET` | `/v1/hermes/execution-contract?task_id=ID` | Exact live task capabilities for the guard |
| `POST` | `/v1/hermes/delegation-claim` | Atomic one-shot delegation batch claim for the canonical run |
| `POST` | `/v1/events/hermes` | Required startup attestation and internal lifecycle observation |

### Required startup policy attestation

The plugin registers the local `pre_tool_call` guard first. When both
`HERMES_OPERATOR_BRIDGE_TOKEN` and `HERMES_OPERATOR_PROFILE` are configured, it then
sends one synchronous policy attestation before registering any HTTP-backed tool,
context injection, command, skill, or lifecycle observer. Registration of those bridge
surfaces continues only when the endpoint returns an authenticated-ingress
acknowledgement containing a nonempty `event_id`, a Boolean `created` value, and
`"trust_level": "authenticated_untrusted"`.

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
    "plugin_version": "1.2.0",
    "policy_version": "3.0.0",
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

Before reserving work for the single Hermes `operator` profile, the control plane
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
attestation. Credential isolation, worker network denial, and restricted toolsets remain
the hard boundary.

### Task-scoped execution contract and lifecycle

Before allowing delegation, local writes, tests, builds, or a current-task Kanban
mutation, the guard resolves the exact task ID from the hook and `HERMES_KANBAN_TASK`.
Conflicting or malformed identities fail closed. It then fetches the live execution
contract and requires the same task ID, `operator` profile, contract digest, canonical
running state, and named capability. A cached prompt, worker claim, card description, or
tool argument cannot create this authority.

Before Hermes invokes `delegate_task`, the guard posts exactly `task_id` and
`requested_children` to `/v1/hermes/delegation-claim`. The child count must be from one
through three. The core revalidates the live task contract and atomically writes a
one-shot SQLite claim keyed by canonical run ID. The response is bound to the task, run,
contract digest, and requested child count. The guard fails closed on a stale contract,
malformed response, or duplicate claim. Because the core consumes the claim before tool
execution, plugin or worker restart cannot recover another batch, and a failed tool
invocation does not restore it.

When a card reports blocked, the control plane closes that run attempt and releases its
global compute slot. After a linked operator answer and fresh authorization, it reserves
a new attempt, comments bounded answer context onto the same card, and calls `unblock`.
The worker itself cannot unblock the card. If independent verification fails after a
completion, a bounded correction uses a new run and a new Hermes card under the remaining
`max_execution_attempts` budget. These two paths deliberately differ: missing context
resumes the same card, while failed evidence starts a separately auditable card.

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

Use only the scoped bridge token. Never place the operator API token, approval secret,
Hermes run-control token, or any outbound connector credential in the Hermes plugin
environment. The bridge token is authorized only for `/health`, prioritized work and
question reads, exact task-contract reads, one atomic task-scoped delegation claim, and
Hermes lifecycle event ingestion. It cannot answer questions, mutate operator work,
inspect approvals, approve an action, or invoke the outbound broker.

## Installation

The plugin is packaged independently from the control plane. A release wheel can be expanded into the active Hermes plugin directory without retaining a project checkout or sharing the control-plane Python environment:

```bash
export HERMES_PLUGIN_ROOT="${HERMES_HOME:-$HOME/.hermes}/plugins"
export PLUGIN_WHEEL="/path/to/hermes_operator_plugin-1.2.0-py3-none-any.whl"
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
export HERMES_OPERATOR_PROFILE="operator"
export HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS="120"
```

Enable the opt-in plugin and restart Hermes:

```bash
hermes plugins enable hermes-operator
hermes plugins list
```

In a Hermes conversation, use `/operator status`, `/operator next`, or
`/operator questions`. The model can also call the three registered tools. The bundled
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
| `HERMES_OPERATOR_PROFILE` | required for HTTP bridge | Stable worker profile name used for policy attestation |
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
- `ctx.register_skill(...)` exposes a read-only namespaced skill.

The plugin currently observes `post_llm_call`, `on_session_start`, `on_session_end`,
`subagent_start`, and `subagent_stop`. Turn completion records contain message lengths,
not raw user or assistant message bodies. Subagent records contain bounded goals and
summaries so the control plane can reconcile delegated execution. Stop events use the
native Hermes `parent_turn_id` and `child_session_id` identities and retain a stable
content fallback only for hosts that omit a child session ID.

The control plane passes every validated effective skill as a repeated native
`hermes kanban create --skill NAME` flag and passes `--goal` when the exact dispatch
contract enables goal mode. Parallel compute uses the native `delegate_task` tool only
inside the current live task contract. The guard permits one delegation batch per
canonical run, containing one foreground goal or a flat batch of one to three foreground
goals. Its core-backed atomic claim is consumed before the tool handler runs and survives
plugin and worker restarts. It blocks a second batch in that run, background mode, and an
orchestrator role. Durable fan-out through `kanban_create` or `kanban_link` is not
worker-authorized.
Operator metadata remains in SQLite for reconciliation and audit.

The core configuration must use the same `operator` value for `profile`,
`default_assignee`, `orchestrator_profile`, and `allowed_profiles`. Internal and active
mode also require `hermes.control_base_url` and the daemon-only token named by
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
8. Start and finish a small delegated Hermes task. Confirm the core records one
   `execution.delegation_batch_claimed` audit before child execution. Restart the plugin
   and confirm a second batch for that canonical run remains blocked.
9. Confirm authenticated `hermes.subagent_started` and `hermes.subagent_stopped` events
   appear in the control-plane event log.
10. Confirm there is no plugin tool or slash command that approves or performs an
   external-facing action.
11. In a non-production sandbox, ask a worker to call `browser_navigate`, `browser_click`,
   send a Discord message, run `git push`, run an HTTP GET and POST, call an opaque tool,
   and use `execute_code`. Confirm each call returns the policy block message before any
   handler executes.
12. Confirm `read_file`, task-scoped `patch`, `git diff`, bounded foreground delegation,
   operator reads, current-task Kanban lifecycle operations, and contract-authorized local
   tests and builds remain available. Confirm native Kanban create, link, and unblock,
   background or orchestrator delegation, network commands, and interpreters remain
   unavailable inside the autonomous worker profile.
13. Start Hermes without `pre_tool_call` support or simulate a rejected hook. Confirm the
   plugin stops registration and the deployment health check prevents worker dispatch.

## Official references

- [Hermes plugins overview](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
- [Hermes plugin authoring guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Hermes plugin hook reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [Hermes built-in tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference)
- [Hermes skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes Agent source repository](https://github.com/NousResearch/hermes-agent)
