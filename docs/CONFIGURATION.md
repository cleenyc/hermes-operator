# Configuration Reference

## Loading and paths

The CLI reads `operator.toml` by default. Override it with `--config` or `HERMES_OPERATOR_CONFIG`:

```bash
hermes-operator --config /etc/hermes-operator/operator.toml doctor
```

Relative filesystem paths resolve from the TOML file directory. String values support ordinary environment expansion and full-value defaults such as `${NAME:-fallback}`. Prefer named environment fields for secrets.

Generate a safe template with:

```bash
hermes-operator --config operator.toml init
```

## Operator

```toml
[operator]
instance_id = "personal-operator"
database_path = "data/operator.db"
data_dir = "data"
timezone = "America/New_York"
autonomy_mode = "shadow"
tick_seconds = 30
reconciliation_seconds = 300
reasoning_refresh_seconds = 3600
max_events_per_pass = 25
max_parallel_work = 4
max_authorizations_per_pass = 40
event_lease_seconds = 300
event_max_attempts = 5
```

| Field | Implemented meaning |
| --- | --- |
| `instance_id` | Stable actor label; service leader ownership also adds a random process identity |
| `database_path` | Canonical local SQLite path |
| `data_dir` | Deployment data-directory value; database location is still controlled by `database_path` |
| `timezone` | Timezone supplied to planner context |
| `autonomy_mode` | `shadow`, `internal`, or `active` |
| `tick_seconds` | Recovery wake interval |
| `reconciliation_seconds` | Full Hermes and eventless supervisor reconciliation interval |
| `reasoning_refresh_seconds` | Minimum interval between eventless live-model portfolio reconsiderations; events still wake reasoning immediately |
| `max_events_per_pass` | Maximum events leased into one unprivileged pass; a privileged event is always isolated |
| `max_parallel_work` | Database-wide maximum count of compute-active or uncertain Hermes runs: `queued`, `running`, `cancel_requested`, `lost`, and quarantined `legacy_conflict` rows. Closed `blocked` attempts do not consume capacity |
| `max_authorizations_per_pass` | Maximum exact dispatch authorizations a single validated model pass may issue |
| `event_lease_seconds` | Event processing lease; must exceed model timeout by more than 30 seconds |
| `event_max_attempts` | Failed attempts before dead letter |

Only one service can hold the active control-plane lease for a database. Its TTL is derived from event lease, model timeout, and tick values rather than exposed as a separate setting. A process-local asynchronous heartbeat maintains the lease during long adapter and projection operations.

`internal` and `active` have identical current dispatch behavior. Neither permits external execution.

## LLM planner

### OpenAI-compatible endpoint

```toml
[llm]
provider = "openai_compatible"
model = "provider-model-id"
base_url = "https://provider.example/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 180
temperature = 0.1
max_output_tokens = 8000
command = []
```

The client calls `POST {base_url}/chat/completions`, unless the URL already ends in `/chat/completions`. It requests a JSON object and expects `choices[0].message.content` to contain that object. `base_url` must use HTTPS unless its host is exactly `127.0.0.1`, `::1`, or `localhost`; embedded credentials, query strings, and fragments are rejected. The client refuses redirects, caps the provider response at 4 MiB, and rejects duplicate JSON keys and non-finite constants in both the provider envelope and extracted plan.

The model ID is deployment-supplied. The core does not validate a vendor model catalog.

An inline `api_key` field is accepted, but `api_key_env` is safer.

### Local command provider

```toml
[llm]
provider = "command"
model = "local-planner"
command = ["/opt/planner/bin/plan", "--model", "{model}"]
timeout_seconds = 180
pass_env = ["LOCAL_MODEL_TOKEN"]
```

The command runs with an argv list and no shell. It receives a JSON object with `system` and `user` prompt strings on stdin and must emit one JSON object to stdout. Its environment contains only a small operating-system base, locale variables, and names explicitly listed in `llm.pass_env`. The configured operator admin token, bridge token, and approval secret names are forbidden. Keep unrelated outbound credentials out of this allowlist.

## Hermes

```toml
[hermes]
enabled = false
binary = "hermes"
profile = "operator"
board = "default"
default_assignee = "operator"
orchestrator_profile = "operator"
command_timeout_seconds = 120
goal_mode = false
default_skills = []
allowed_profiles = ["operator"]
allowed_skills = []
pass_env = []
dispatch_authorization_ttl_seconds = 86400
max_execution_attempts = 3
control_base_url = ""
control_token_env = "HERMES_KANBAN_CONTROL_TOKEN"
control_timeout_seconds = 10
require_policy_attestation = true
policy_attestation_ttl_seconds = 300
allowed_plugin_versions = ["1.5.0"]
allowed_policy_versions = ["6.0.0"]
allowed_policy_digests = ["e1f6f56429df64374f9c8b32682a773706b2e35cf5711753904149e503fc31a0"]
```

| Field | Implemented meaning |
| --- | --- |
| `enabled` | Construct the CLI adapter and permit reconciliation and eligible dispatch |
| `binary` | Executable name or path, or a TOML argv array such as `["docker", "exec", "hermes", "hermes"]`; no shell is used |
| `profile` | Passed to every CLI command as `-p PROFILE` |
| `board` | Passed as `--board BOARD` |
| `default_assignee` | Default execution profile; it is part of the effective profile allowlist |
| `orchestrator_profile` | Compatibility/default routing profile; it is part of the effective profile allowlist |
| `command_timeout_seconds` | Per-command timeout |
| `goal_mode` | Default exact dispatch contract flag |
| `default_skills` | Skills recorded in every task contract and included in the skill allowlist |
| `allowed_profiles` | Exact profile allowlist. Every profile that receives Operator work needs its own fresh accepted plugin attestation |
| `allowed_skills` | Additional skills the planner or operator may request |
| `pass_env` | Exact extra environment variable names passed to the Hermes child process |
| `dispatch_authorization_ttl_seconds` | Interval used to calculate the authorization `review_after` marker, minimum 60 seconds. Durable authorization does not expire automatically |
| `max_execution_attempts` | Maximum new-card execution attempts permitted under one durable authorization root, including bounded verification retries |
| `control_base_url` | Hermes Kanban HTTP control base URL used only for native run termination; required with enabled Hermes in `internal` or `active` mode |
| `control_token` | Optional inline control API Bearer token; environment injection is safer |
| `control_token_env` | Environment name containing the control API Bearer token; default `HERMES_KANBAN_CONTROL_TOKEN` |
| `control_timeout_seconds` | Timeout for authenticated run-control requests |
| `require_policy_attestation` | Require fresh native-plugin policy evidence before each new reservation |
| `policy_attestation_ttl_seconds` | Core freshness window, minimum 60 seconds; example and recommended value 300 |
| `allowed_plugin_versions` | Exact accepted native plugin versions |
| `allowed_policy_versions` | Exact accepted worker policy versions |
| `allowed_policy_digests` | Exact lowercase SHA-256 digests of accepted `policy.py` source |

When attestation is required and Hermes is enabled, all three attestation allowlists must be nonempty and every digest must be 64 lowercase hex characters. `profile`, `default_assignee`, `orchestrator_profile`, and the entries in `allowed_profiles` form the effective allowlist. Multiple allowed profiles are supported. Install and configure the plugin in each one with a distinct `HERMES_OPERATOR_PROFILE`; dispatch checks fresh attestation state for the exact selected profile.

The CLI adapter checks for `create`, `show`, `list`, `comment`, `block`, `unblock`, and `runs`. It does not provide generic update operations. The separate authenticated control transport terminates the current native run, after which the CLI adapter blocks the card to prevent redispatch.

The Hermes child process receives only a safe base environment, locale variables, and values explicitly named in `pass_env`. Do not add the admin token, bridge token, approval secret, Kanban control token, or outbound connector credentials. The daemon itself resolves the control token and does not forward it to the worker.

Parallel compute is represented as multiple independent canonical WorkItems. The dispatcher reserves and starts their Hermes cards in parallel up to the database-wide `operator.max_parallel_work` limit. Current top-level Hermes `delegate_task` uses background execution and does not provide foreground completion semantics the Operator can bind to a canonical run, so the plugin blocks it on Operator-managed cards. Native Kanban fanout is also blocked on those cards. Unmanaged interactive Hermes sessions retain native delegation and harness behavior.

The task-scoped guard applies only when the hook identifies an Operator-managed Kanban card. Outside that scope, interactive and Cron sessions use Hermes-native policy. Conversational question answers, exact work authorization, terminal or hierarchy changes, Cron mutations, and identifiable Google writes use Hermes' native human-confirmation gate.

Each execution-contract lookup rechecks dependency satisfaction and the active service leader fence before returning capabilities. `blocker --blocks--> affected` is execution-equivalent to `affected --depends_on--> blocker`. Adding either edge form is rejected while an endpoint has a `queued`, `running`, `cancel_requested`, `lost`, or `legacy_conflict` canonical run. Mixed cycles are rejected, and a completed blocker cannot reopen while affected work has one of those runs.

Dispatch authorization is durable until consumed or the exact contract changes. It binds `not_before` to `scheduled_at`, records a non-expiring `expires_at = null`, sets an operational `review_after`, and caps attempts with `max_execution_attempts`. The first dispatch consumes the authorization for that exact card. A failed verification may reuse only the authorization root and remaining attempt budget, not the consumed card grant.

## Hermes-native automation

```toml
[native_automation]
enabled = false
delivery = "local"
google_intake_enabled = true
google_intake_schedule = "every 10m"
reminder_delivery_enabled = true
reminder_schedule = "every 15m"
attention_redelivery_seconds = 3600
briefing_enabled = true
briefing_schedule = "0 8 * * *"
attach_to_session = true
google_skill = "google-workspace"
obsidian_skill = "obsidian"
```

`attention_redelivery_seconds` is the durable suppression window shared by
reminder and pending-question delivery claims. It must be a positive integer.
Hermes Cron still owns the polling cadence. A shorter Cron schedule can safely
poll during the window without returning the same attention items each time.

This is an implemented desired-state installer, not a provider placeholder. `native-jobs plan` shows the three fixed contracts; `native-jobs install --dry-run` shows the Hermes CLI calls; and `native-jobs install` creates missing jobs by stable name. The Google job uses Hermes' bundled `google-workspace` skill and account OAuth to read Gmail, Calendar, and meeting evidence into the scoped bridge. The reminder job atomically claims due reminders and pending questions for private delivery. The daily briefing combines ranked work with the bundled Obsidian skill when vault context is useful. OAuth, private delivery target, Gateway operation, and `OBSIDIAN_VAULT_PATH` remain deployment bindings.

See [Reminder and Attention Lifecycle](REMINDERS.md).

## Deterministic verification

```toml
[verification]
enabled = true
max_artifacts = 64
max_files_per_directory = 2000
max_artifact_bytes = 268435456
max_total_artifact_bytes = 536870912

[verification.artifact_roots]
workspace = "${HERMES_OPERATOR_WORKSPACE:-/srv/hermes/workspace}"

[[verification.checks]]
name = "unit-tests"
command = ["python", "-m", "pytest", "-q"]
cwd = "${HERMES_OPERATOR_WORKSPACE:-/srv/hermes/workspace}"
timeout_seconds = 300
max_output_bytes = 1048576
pass_env = []
```

| Field | Implemented meaning |
| --- | --- |
| `enabled` | Enable the deterministic gate. A declared artifact or verification contract fails closed when this is false |
| `artifact_roots` | Stable root names mapped to paths visible to the core. Relative artifact declarations require a named root when more than one root exists |
| `max_artifacts` | Maximum declarations inspected for one completion, hard-capped at 1,000 |
| `max_files_per_directory` | Maximum regular files traversed in one declared directory artifact |
| `max_artifact_bytes` | Maximum bytes hashed for one file or directory |
| `max_total_artifact_bytes` | Maximum bytes hashed across the completion |
| `checks[].name` | Stable name referenced by a canonical work item's protected `verification_contract` |
| `checks[].command` | Deployment-owned fixed argv executed with `shell=false` |
| `checks[].cwd` | Explicit working directory. A check without one fails closed |
| `checks[].timeout_seconds` | Per-check wall-clock limit, at most 3,600 seconds |
| `checks[].max_output_bytes` | Combined stdout/stderr cap, at most 16 MiB |
| `checks[].pass_env` | Minimal extra environment allowlist. Control-plane and approval secret names are forbidden |

Native Hermes artifact declarations are recognized from the task's `artifacts`, `metadata.artifacts`, `result.artifacts`, and `result.metadata.artifacts` fields. A declaration may be a path string or an object containing `path`, optional `root`, optional `type` (`file` or `directory`), and optional lowercase `sha256`. Paths outside configured roots, parent traversal, symlinks, special files, missing files, type mismatches, digest mismatches, count limits, and byte limits all fail closed. Directories receive a stable tree digest over relative names, file sizes, and file SHA-256 values.

A canonical work item may carry protected metadata like this:

```json
{
  "verification_contract": {
    "artifacts": [
      {"root": "workspace", "path": "dist/report.pdf", "type": "file"}
    ],
    "checks": ["unit-tests"]
  }
}
```

The contract is included in the exact dispatch digest and rendered into the Hermes task. Model-authored work updates cannot replace it. Checks are selected only by this canonical contract, never by worker completion output. After the dispatcher records immutable completion evidence, the supervisor validates the exact event, card, run, attempt, and evidence fingerprint, then runs the verifier once outside every SQLite write transaction. The report is bound to the work version, canonical execution scope, run result, verification inputs, and observed artifact digests, and is cached by that completion binding. Only fast binding and report-digest validation occurs in the final state transaction. An applicable failure overrides a model's `passed` verdict and leaves the work blocked for correction. Text-only work with no contract and no native artifact declarations remains unaffected.

Set a verifier contract as a separate scope-bearing mutation with
`hermes-operator work verification-contract WORK_ID --set contract.json --expected-version VERSION`.
Use `--clear` to deliberately remove it. The CLI validates the bounded schema,
artifact-root names, and configured check names. Then run `work
authorization-scope`, review its result, and pass all three returned fences to
`work dispatch`. Dispatch cannot change the verifier contract after preview.

The verifier establishes existence, path scope, type, content identity, and the exit status of deployment-owned checks. It does not prove that an arbitrary artifact is semantically correct. Run configured checks under a restricted identity because test and build scripts are executable project code.

## Obsidian

```toml
[obsidian]
enabled = false
vault_path = ""
discover = true
operator_root = "Hermes Operator"
write_mode = "projection"
```

| Field | Implemented meaning |
| --- | --- |
| `enabled` | Enable configured path or bounded vault discovery |
| `vault_path` | Explicit vault directory |
| `discover` | Check bounded common candidates when enabled and no explicit path exists |
| `operator_root` | Managed directory inside the vault |
| `write_mode` | Must be `projection` |

`HERMES_OPERATOR_VAULT` overrides `vault_path` and enables projection. This is the preferred late-binding mechanism.

The projector writes dashboard, active and terminal work notes, and promoted memory. Pending and quarantined memory stays in SQLite.

The observation phase also reads direct Markdown children of `<operator_root>/Inbox`. This input is non-recursive, byte and count bounded, skips symlinks, and always enters as `authenticated_untrusted`. Notes outside that one Inbox directory are not read. `HERMES_OPERATOR_VAULT` late binding enables both projection and Inbox observation.

## Optional inbound command connectors

The native Google Workspace job above is the ready-to-use Gmail, Calendar, and meeting path. Use this generic interface for another provider or for a deployment that deliberately prefers an external read-only executable:

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

| Field | Implemented meaning |
| --- | --- |
| `name` | Unique stable cursor and health identifier, up to 128 safe identifier characters |
| `source` | Lowercase event source; `operator`, `system`, and `hermes` are reserved |
| `command` | Fixed nonempty argv list invoked without a shell |
| `enabled` | Poll this reader during the observation phase |
| `interval_seconds` | Minimum monotonic interval between polls |
| `timeout_seconds` | Hard child-process timeout |
| `pass_env` | Exact provider credential environment names added to the minimal child environment |
| `max_output_bytes` | Maximum stdout JSON size |

The configured admin token, bridge token, and approval secret environment names are forbidden in `pass_env`. Connector commands receive the prior cursor and must return the strict JSON envelope documented in [Inbound Connectors](CONNECTORS.md). The next cursor and all accepted events commit atomically under the active service leader fence.

## HTTP server

```toml
[server]
enabled = true
host = "${HERMES_OPERATOR_BIND_HOST:-127.0.0.1}"
port = 8787
api_token_env = "HERMES_OPERATOR_API_TOKEN"
bridge_token_env = "HERMES_OPERATOR_BRIDGE_TOKEN"
max_body_bytes = 1048576
allow_unsigned_webhooks = false

[server.webhook_secrets]
gmail = "${GMAIL_WEBHOOK_SECRET}"
calendar = "${CALENDAR_WEBHOOK_SECRET}"
```

| Field | Implemented meaning |
| --- | --- |
| `enabled` | Start the standard-library HTTP server |
| `host` | Bind address |
| `port` | TCP port from 1 to 65535 |
| `api_token_env` | Environment variable containing the admin Bearer token |
| `bridge_token_env` | Environment variable containing the scoped native-plugin Bearer token |
| `max_body_bytes` | Maximum JSON POST body |
| `allow_unsigned_webhooks` | Accept unconfigured unsigned sources as `untrusted`; development only |
| `webhook_secrets` | Per-source HMAC secrets for generic intake; `operator`, `system`, and `hermes` are forbidden keys |

Inline `api_token` and `bridge_token` values are accepted, but environment injection is safer. Nonempty admin and bridge tokens must be distinct. A non-loopback bind requires an admin token.

The admin token can mutate operator state and review approvals. The bridge token is restricted to the explicit Hermes routes. It can read next work, questions, attention, and exact live task contracts; capture reversible work; submit version-fenced work updates; record exact operator answers and work authorization; ingest normalized Google evidence; claim private attention delivery; and post Hermes observations and policy attestations. Hermes-native confirmation gates authority-bearing conversational mutations before the plugin calls these routes. Accepted attestations update per-profile state and audit without entering the planner queue. The bridge cannot inspect approval content, approve an external action, fetch the admin status graph, or execute outbound delivery.

The built-in server is plain HTTP. Use loopback, a private network, or TLS termination.

## Policy

```toml
[policy]
external_actions_require_approval = true
external_action_mode = "stage_only"
approval_ttl_seconds = 3600
approval_secret_env = "HERMES_OPERATOR_APPROVAL_SECRET"
trusted_event_sources = ["operator", "system"]
allow_memory_auto_promotion = false
max_llm_priority_adjustment = 10.0
```

| Field | Implemented meaning |
| --- | --- |
| `external_actions_require_approval` | Required safety invariant; configuration rejects `false` |
| `external_action_mode` | `disabled`, `stage_only`, or `approved` |
| `approval_ttl_seconds` | Staged action and grant lifetime |
| `approval_secret_env` | Reserved name; the core daemon does not read or use this secret |
| `trusted_event_sources` | Declarative setting retained for policy configuration; current trust assignment comes from authenticated ingress and reserved event paths |
| `allow_memory_auto_promotion` | Reserved behavior flag; current supervisor still requires operator review |
| `max_llm_priority_adjustment` | Bound supported by the priority engine; ordinary rescoring currently supplies zero adjustment |

Mode behavior:

- `disabled`: proposals are audited and not staged.
- `stage_only`: exact proposals may enter the approval queue.
- `approved`: same daemon behavior as `stage_only` in this release.

No setting gives the daemon an outbound connector or execute command. `approval_secret_env` does not arm execution.

## Optional separate outbound broker

`hermes-outbound-broker` is installed with the package but is not started or imported by the daemon. Its example is [config/outbound.example.toml](../config/outbound.example.toml):

```toml
[broker]
enabled = false
database_path = "../data/operator.db"
actor = "outbound-broker"
max_grant_lifetime_seconds = 3600

[[connectors]]
integration = "mail"
command = ["/opt/hermes-outbound-connectors/mail-send"]
pass_env = ["MAIL_SEND_TOKEN"]
timeout_seconds = 60
max_input_bytes = 1048576
max_output_bytes = 1048576
```

The broker remains disabled until its separate deployment configuration sets `enabled = true`. A connector is fixed argv with `shell=False`; model output cannot choose or modify it. The connector receives a minimal operating-system environment plus explicitly named values. The broker reads the exact staged action from SQLite, verifies the action and grant binding, and atomically consumes the one-shot grant while changing the action to `executing` before invoking the connector.

Connector stdin is one strict JSON object containing `schema_version`, `action_id`, `intent_digest`, `recipients_digest`, `content_digest`, and the exact `action`. The action includes type, actor, integration, recipients, content, target, media type, attributes, and schema version. Connector stdout must stay within `max_output_bytes` and be a strict JSON object containing `"ok": true`. Nonzero exit, timeout, malformed JSON, duplicate keys, non-finite values, a false or missing `ok`, and output overflow fail closed and are audited.

The connector and its mutation credentials can belong to a separate process identity and network policy when that hardening is useful. Do not place its credential in the daemon, planner, managed Hermes worker, native plugin, bridge, or inbound readers. Approval alone does not invoke the broker; execution also requires a deliberate separate broker invocation with the exact action and grant IDs. The broker is not required for native intake, work execution, reminders, briefings, Obsidian access, or interactive Hermes actions governed by native confirmation.

## Environment summary

| Variable | Purpose |
| --- | --- |
| `HERMES_OPERATOR_CONFIG` | Default configuration path |
| `OPENAI_API_KEY` | Default model credential variable |
| `HERMES_OPERATOR_API_TOKEN` | Admin API credential |
| `HERMES_OPERATOR_BRIDGE_TOKEN` | Scoped Hermes native-plugin credential |
| `HERMES_OPERATOR_BIND_HOST` | Optional HTTP bind override used by the example and Compose |
| `HERMES_OPERATOR_VAULT` | Late-bound Obsidian vault |
| `HERMES_OPERATOR_APPROVAL_SECRET` | Reserved name, unused by core daemon |
| `HERMES_OPERATOR_URL` | Native plugin control-plane URL |
| `HERMES_OPERATOR_PROFILE` | Native plugin worker profile used in attestation |
| `HERMES_KANBAN_CONTROL_TOKEN` | Daemon-only credential for authenticated Hermes native run termination |
| `HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS` | Native plugin heartbeat and hook refresh interval, 120 to 240 seconds |

See [Hermes Integration](HERMES_INTEGRATION.md) for all plugin settings.

## Validation

```bash
hermes-operator --config operator.toml doctor
```

`doctor` loads and validates TOML, initializes SQLite, checks that a model and credential or command are configured, probes enabled Hermes and Obsidian adapters, and reports that the daemon has no outbound connectors. It does not inspect or invoke the separately configured broker, make a live model request, acquire the long-running leader lease, or prove network egress isolation.
