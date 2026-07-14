# Operations Runbook

## Native installation

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install .
hermes-operator --config operator.toml init
```

Edit `operator.toml`, set a real model ID, and inject secrets through the process environment. Run `doctor` before starting the service.

Recommended system layout:

```text
/opt/hermes-operator/.venv/                 installed Python environment
/etc/hermes-operator/operator.toml          non-secret configuration
/etc/hermes-operator/environment            protected environment values
/var/lib/hermes-operator/operator.db        canonical database
```

For a fresh systemd host, create the service identity and paths, then install the
application into the exact virtual environment named by the unit:

```bash
sudo useradd --system --home-dir /var/lib/hermes-operator \
  --shell /usr/sbin/nologin hermes-operator
sudo install -d -m 0755 /opt/hermes-operator
sudo install -d -m 0750 -o root -g hermes-operator /etc/hermes-operator
sudo python3 -m venv /opt/hermes-operator/.venv
sudo /opt/hermes-operator/.venv/bin/python -m pip install /path/to/hermes-operator
sudo install -m 0640 -o root -g hermes-operator \
  config/operator.example.toml /etc/hermes-operator/operator.toml
sudo install -m 0644 deploy/hermes-operator.service \
  /etc/systemd/system/hermes-operator.service
```

If the account already exists, omit `useradd`. Create
`/etc/hermes-operator/environment` with mode `0640`, owner `root`, and group
`hermes-operator`; do not put secrets on a command line. `StateDirectory` creates and
owns `/var/lib/hermes-operator` when the unit starts.

Use absolute service paths:

```toml
[operator]
database_path = "/var/lib/hermes-operator/operator.db"
data_dir = "/var/lib/hermes-operator"
```

Service environment:

```text
OPENAI_API_KEY=replace-with-provider-key
HERMES_OPERATOR_API_TOKEN=replace-with-random-admin-token
HERMES_OPERATOR_BRIDGE_TOKEN=replace-with-different-random-bridge-token
HERMES_KANBAN_CONTROL_TOKEN=replace-with-run-control-token
```

The bridge value is needed only when the native plugin is used. It must differ from the admin value. The run-control token is required only for enabled Hermes execution in `internal` or `active` mode and must never be forwarded to the worker.

Install and review [the systemd template](../deploy/hermes-operator.service), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-operator
sudo systemctl status hermes-operator
```

## Docker and Compose

The base image runs as an unprivileged user and contains neither Hermes nor an outbound connector. The Python package includes the disabled `hermes-outbound-broker` executable, but Compose does not start it and supplies no mutation credential. The embedded daemon configuration is shadow mode with Hermes and Obsidian disabled.

```bash
cp config/operator.example.toml operator.toml
# Edit operator.toml and set llm.model before starting.
export OPENAI_API_KEY="replace-with-provider-key"
export HERMES_OPERATOR_API_TOKEN="replace-with-random-admin-token"
docker compose build
docker compose up -d
docker compose ps
curl -sS http://127.0.0.1:8787/health
```

Compose bind-mounts this file at `/app/operator.toml`. Keep its data paths relative to that location:

```toml
[operator]
database_path = "data/operator.db"
data_dir = "data"

[server]
enabled = true
host = "${HERMES_OPERATOR_BIND_HOST:-127.0.0.1}"
port = 8787
api_token_env = "HERMES_OPERATOR_API_TOKEN"
bridge_token_env = "HERMES_OPERATOR_BRIDGE_TOKEN"
```

Compose sets `HERMES_OPERATOR_BIND_HOST=0.0.0.0` inside the container and publishes to host loopback. It accepts optional `HERMES_KANBAN_CONTROL_TOKEN` for the daemon but does not forward it through `hermes.pass_env`. To enable Hermes dispatch, build a deployment-specific image with the compatible CLI and `operator` profile state, or run the operator beside Hermes on the host. The native plugin HTTP bridge does not replace the Kanban CLI transport. Internal and active mode also require an authenticated `hermes.control_base_url` reachable by the daemon and the daemon-only control token.

## Optional independent-boundary hardening

The base system enforces a task-scoped guard for autonomous managed cards and uses
Hermes-native confirmation for authority-bearing interactive and scheduled actions.
If a deployment also requires a hard no-outbound guarantee that is independent of
the Hermes harness, apply the following infrastructure controls before changing from
`shadow` to `internal`:

1. Confirm the Hermes worker has only `HERMES_OPERATOR_BRIDGE_TOKEN`, never the admin token.
2. Remove outbound connector, mail-send, messaging, calendar-write, publishing, payment, repository-write, and cloud-write credentials.
3. Remove authenticated browser sessions that can mutate external state.
4. Run the worker in an operating-system or container sandbox with network egress disabled by default and filesystem access limited to its authorized workspace.
5. Apply the same egress restriction to project-defined test and build scripts. Reviewed `local_test` and `local_build` command shapes can still execute arbitrary repository code.
6. Allow only required model, operator API, artifact, read-only data, and internal execution endpoints through controlled policy.

These controls are optional for the core integration, but credential and egress
isolation are required if the deployment claims a hard no-outbound boundary that
remains enforceable even when the Hermes process or plugin is compromised.

## Native plugin environment

```text
HERMES_OPERATOR_URL=http://127.0.0.1:8787
HERMES_OPERATOR_BRIDGE_TOKEN=replace-with-different-random-bridge-token
HERMES_OPERATOR_PROFILE=operator
HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS=120
```

Plugin registration sends a synchronous policy attestation and starts one daemon heartbeat. The default heartbeat is 120 seconds and the example core TTL is 300 seconds. Pre-LLM and lifecycle hooks share the same refresh limiter.

## Normal checks

```bash
hermes-operator --config operator.toml doctor
hermes-operator --config operator.toml status
hermes-operator --config operator.toml event list --state dead_letter
hermes-operator --config operator.toml next --limit 10
hermes-operator --config operator.toml question list
hermes-operator --config operator.toml approval list
hermes-operator --config operator.toml memory list
```

Review:

- Runtime is running and the last cycle has no component errors.
- Pending events are moving to processed and dead-letter count is stable.
- Capacity-active `queued`, `running`, `cancel_requested`, `lost`, and `legacy_conflict` runs do not exceed `max_parallel_work`. Closed `blocked` attempts are history, not active compute.
- Enabled Hermes and Obsidian adapters are available.
- Pending questions, approvals, and memory candidates are reviewed.
- Exactly one service owns the database leader lease.
- Fresh `operator` profile policy attestation exists before dispatch.

The public `/health` route reports only minimal liveness. Use authenticated state, CLI status, and logs for details.

## Start, stop, and one cycle

```bash
hermes-operator --config operator.toml run
hermes-operator --config operator.toml run-once
hermes-operator --config operator.toml run-once --no-reconcile
```

`run-once` acquires and releases the same active leader lease as the long-running service. It fails while another live instance owns that database.

With systemd:

```bash
sudo systemctl restart hermes-operator
sudo systemctl status hermes-operator
sudo journalctl -u hermes-operator -n 200 --no-pager
```

The service handles `SIGINT` and `SIGTERM`. It finishes at most the current bounded
model or Hermes command, skips remaining cycle components, stops its HTTP server,
records stopped state, and releases its leader lease. The supplied systemd and Compose
artifacts allow 360 seconds so the default 180-second model timeout and possible
two-command Hermes create resolution can finish without forced termination.

## Immediate safety pause

If behavior leaves scope:

1. Stop `hermes-outbound-broker` or its invocation path if it is deployed.
2. Stop Hermes Operator.
3. Change `operator.autonomy_mode` to `shadow`.
4. Set `policy.external_action_mode` to `disabled` if proposal staging should stop.
5. Pause suspect inbound readers.
6. Inspect work, questions, approvals, memory, audit state, runs, and logs.
7. Inspect already-created Hermes cards and native runs. Reconciliation normally terminates active compute and blocks the card when canonical work is terminal or authorization is invalid; verify the stop acknowledgement rather than assuming it succeeded.
8. Run `doctor`, then one reviewed `run-once` before continuous restart.

The daemon has no outbound connector, so an approval cannot be sent by this process. If `hermes-outbound-broker` is deployed, stop its separate service or invocation path and revoke connector credentials independently.

## Backup

SQLite uses WAL. Use the SQLite backup API rather than copying only the main file while the service is active:

```bash
mkdir -p backups
SOURCE_DB="$PWD/data/operator.db" BACKUP_DB="$PWD/backups/operator-$(date -u +%Y%m%dT%H%M%SZ).db" \
python -c 'import os,sqlite3; source=sqlite3.connect(os.environ["SOURCE_DB"]); target=sqlite3.connect(os.environ["BACKUP_DB"]); source.backup(target); target.close(); source.close()'
```

Verify:

```bash
BACKUP_DB="$(ls -1t backups/operator-*.db | head -n 1)" \
python -c 'import os,sqlite3; db=sqlite3.connect(os.environ["BACKUP_DB"]); result=db.execute("PRAGMA integrity_check").fetchone()[0]; db.close(); print(result); raise SystemExit(0 if result == "ok" else 1)'
```

Back up configuration, connector cursor state, reviewed custom plugin source, and secret-manager configuration through their native mechanisms. The Obsidian projection is not a database backup.

For the supplied Compose service, create the online backup inside the writable data volume, copy it to the host, then remove the temporary volume copy:

```bash
mkdir -p backups
export BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T operator python -c 'import sqlite3; source=sqlite3.connect("/app/data/operator.db"); target=sqlite3.connect("/app/data/operator-backup.db"); source.backup(target); target.close(); source.close()'
docker compose cp operator:/app/data/operator-backup.db "backups/operator-${BACKUP_STAMP}.db"
docker compose exec -T operator python -c 'from pathlib import Path; Path("/app/data/operator-backup.db").unlink(missing_ok=True)'
```

## Restore

1. Stop the service and any process using the database.
2. Verify the backup with `PRAGMA integrity_check`.
3. Preserve the current database, WAL, and SHM files as an incident snapshot.
4. Restore the verified database to `database_path`.
5. Restore ownership and restrictive permissions.
6. Start in `shadow`.
7. Run `doctor`, `status`, and one `run-once`.
8. Reconcile linked Hermes cards and inspect review events.
9. Reproject Obsidian.
10. Restore `internal` only after policy attestation and egress checks pass.

Do not manually merge SQLite files. Re-ingest later source events with stable source-scoped dedupe keys.

To restore the named Compose volume, first preserve the current database as an incident backup, stop the service, and provide one already-verified host backup through a read-only mount:

```bash
docker compose stop operator
export BACKUP_FILE="/restore/operator-YYYYMMDDTHHMMSSZ.db"
docker compose run --rm --no-deps --entrypoint python -v "$PWD/backups:/restore:ro" -e BACKUP_FILE operator -c 'import os,sqlite3; source=sqlite3.connect("file:"+os.environ["BACKUP_FILE"]+"?mode=ro", uri=True); target=sqlite3.connect("/app/data/operator.db"); source.backup(target); result=target.execute("PRAGMA integrity_check").fetchone()[0]; target.close(); source.close(); print(result); raise SystemExit(0 if result == "ok" else 1)'
docker compose up -d operator
```

## LLM failures

Symptoms include timeout, invalid JSON, HTTP failure, missing model configuration, or supervisor component errors.

Checks:

1. Run `doctor`.
2. Verify model ID, endpoint, credential variable, quotas, and service environment.
3. Confirm an OpenAI-compatible endpoint supports the expected chat-completions shape and JSON-object response. Use HTTPS unless the host is exact loopback, and remove embedded credentials, query strings, or fragments from `base_url`.
4. Configure the final provider URL directly. Redirects are intentionally refused so the bearer credential cannot move to another origin.
5. Check for a response over 4 MiB, duplicate JSON keys, or `NaN` or infinity constants. All fail closed and are not configurable relaxations.
6. For `command`, run the exact argv as the service user and verify one JSON object on stdout.
7. Keep sensitive prompt content out of logs.

The supervisor transaction rolls back on any failure. A claimed event returns to pending until `event_max_attempts`, then moves to dead letter. Dispatch is skipped in a cycle whose reconciliation or event processing failed.

After correcting the cause, inspect and replay one dead letter through the audited administration path:

```bash
hermes-operator --config operator.toml event list \
  --state dead_letter --source gmail --limit 50
hermes-operator --config operator.toml event replay EVENT_ID \
  --reason "Connector parser corrected and payload reviewed"
```

Replay succeeds only while the exact event is still in `dead_letter`. It clears stale lease and error fields, resets that event's retry budget, preserves its identity and dedupe key, and records the operator, reason, prior error, and prior attempt count in the audit log. A concurrent state change fails closed. Do not edit queue state directly in SQLite.

## Leader lease conflict

Symptom:

```text
Another Hermes Operator instance owns the control-plane lease
```

Checks:

1. Find the running service, container, or manual `run` process using that database.
2. Confirm two configurations do not point at one file unintentionally.
3. Stop the old owner cleanly and wait for release.
4. If it crashed, wait for lease expiry before starting the replacement.

Do not bypass the lease by editing the database while an owner may still be active. Live execution-contract reads check the leader fence before and after canonical authorization evaluation, so a stale service must be stopped rather than made authoritative through retries.

## Hermes unavailable or incompatible

```bash
hermes-operator --config operator.toml doctor
hermes --version
hermes -p operator kanban --board default --help
```

The adapter requires `create`, `show`, `list`, `comment`, `block`, `unblock`, and `runs`. It uses JSON output plus create options for body, assignee, priority, idempotency key, scheduled time, repeated skills, and optional goal mode. Canonical hierarchy is included as context, not translated into a Hermes dependency parent.

Check executable path, the single `operator` profile, board, command timeout, service-user access to Hermes state, and `pass_env`. For internal or active mode, also check `control_base_url`, the daemon-only `HERMES_KANBAN_CONTROL_TOKEN`, and `/api/plugins/kanban/runs/{run_id}/terminate`. The adapter does not inherit arbitrary service secrets.

When Hermes is down, canonical ready work remains in SQLite. Individual dispatch errors are isolated. Do not create a duplicate card manually. Each attempt uses `hermes-operator:{work_id}:attempt:{attempt}`, and a queued reservation may represent a card whose create response was lost.

## Dependency mutation rejected during execution

Adding a `depends_on` or `blocks` edge fails while either endpoint has a `queued`, `running`, `cancel_requested`, `lost`, or `legacy_conflict` canonical run. Reopening a completed dependency also fails while dependent work has one of those runs. This preserves the contract the worker is already executing.

Do not edit SQLite to bypass the conflict. Reconcile or explicitly resolve the uncertain run, wait for a terminal state, then apply the dependency change and issue fresh authorization if execution should continue. If a worker execution contract reports `dependencies_not_satisfied`, restore a valid dependency state or stop the run; contract lookup rechecks both dependencies and the active leader fence.

## Lost or uncertain Hermes run recovery

`queued`, `lost`, `legacy_conflict`, and `cancel_requested` run states remain capacity-active because remote execution may still exist. Never release one only to make room for new work. A reconciled `blocked` attempt has `finished_at` and does not consume a slot.

First inspect the recorded run and the remote card or worker logs:

```bash
hermes-operator --config operator.toml run-state list --status lost
hermes -p operator kanban --board default show HERMES_TASK_ID --json
```

After independently confirming that the execution has stopped, resolve the exact current run state with an operator reason:

```bash
hermes-operator --config operator.toml run-state resolve RUN_ID \
  --expected-status lost \
  --reason "Remote card is absent and the worker has no live process"
```

Resolution changes the run to `abandoned`. When no other capacity-active run exists, it clears the Hermes link and execution mode, revokes the prior dispatch request and authorization, and blocks nonterminal work. Already `done`, `cancelled`, or `archived` work remains terminal. A concurrent state change fails the command rather than releasing the wrong run.

Review and correct the work before retrying. A generic status update cannot reuse the old execution authority. A retry needs a fresh explicit dispatch:

```bash
hermes-operator --config operator.toml work show WORK_ID
hermes-operator --config operator.toml work update WORK_ID --description "Corrected bounded scope"
hermes-operator --config operator.toml work authorization-scope WORK_ID --profile operator
hermes-operator --config operator.toml work dispatch WORK_ID --profile operator \
  --expected-version VERSION \
  --expected-scope-revision SCOPE_REVISION \
  --expected-scope-digest SCOPE_DIGEST
```

## Blocked card resume

When Hermes reports a card blocked, reconciliation closes that canonical run attempt and releases its compute slot. Do not resolve or abandon that historical run merely because it is blocked.

For missing operator context:

1. Confirm the pending question lists the blocked work ID.
2. Answer through `question answer` or the authenticated API.
3. Let the live supervisor apply that answer only to the linked work.
4. Confirm the work receives a fresh exact authorization and becomes ready.
5. Run or wake reconciliation.

The dispatcher reserves a new slot and compares the prior immutable run contract with the newly authorized scope. When they match exactly, it posts only currently bound answered context and calls `unblock` on the same Hermes card. If scope, verifier, executor, skills, goal mode, or dispatch digest changed, it creates a new card instead of resuming stale instructions. The historical attempt remains `blocked`; the new attempt becomes `running`. If capacity is full, execution waits until a slot is available. A lost unblock response is recovered against the same queued attempt and card.

Do not ask the worker to call `kanban_unblock`. The native guard reserves unblock ownership for the control plane.

## Native stop and block failures

If terminal or unauthorized canonical work still has a live Hermes run, reconciliation calls the authenticated native terminate endpoint and then blocks the card. Check:

1. `hermes.control_base_url` is the correct HTTP or HTTPS base URL with no embedded credentials, query, or fragment.
2. `HERMES_KANBAN_CONTROL_TOKEN` is present only in the daemon environment and is accepted by Hermes.
3. The card exposes a current run ID or a discoverable active run.
4. The service identity can reach `/api/plugins/kanban/runs/{run_id}/terminate`.
5. `hermes kanban block` succeeds for the same card.

If either operation fails, treat compute as potentially live. Keep the fail-closed run state and capacity accounting, isolate the worker manually, then use `run-state resolve` only after independently proving execution stopped.

## Work is not dispatching

```bash
hermes-operator --config operator.toml work show WORK_ID
hermes-operator --config operator.toml status
hermes-operator --config operator.toml question list
```

Check:

- Mode is `internal` or `active`.
- Work is `ready` and `execution_mode` is `hermes`.
- Governance records execution authorization.
- Durable dispatch authorization matches current contract fields, is not consumed for another card, has reached `not_before`, and has remaining attempts.
- Supervisor authorization names a finalized plan digest, or direct CLI dispatch issued it.
- Profile is `operator` and skills are allowed.
- At least one acceptance criterion exists.
- Dependencies are `done`.
- Global active run count is below `max_parallel_work`.
- Work is not waiting for input.
- Fresh policy attestation exists for the `operator` profile.

After material work changes, issue a fresh explicit `work dispatch` or a new scoped operator authorization. Do not copy authorization metadata between work items.

## Policy attestation failure

Common dispatcher audit reasons include missing, unauthenticated, profile mismatch, plugin version, policy version, digest, invalid timestamp, or stale attestation.

Checks:

1. Confirm the worker has the bridge token and exact `HERMES_OPERATOR_PROFILE` used as the assignee.
2. Confirm the admin and bridge tokens differ.
3. Confirm plugin startup received an acknowledged `policy.attested` response.
4. Confirm the daemon heartbeat thread started.
5. Compare plugin version, policy version, and `sha256sum policy.py` with core allowlists.
6. Keep heartbeat interval below the 300-second example core TTL.
7. Check host clock and UTC timestamp handling.
8. Check bridge network reachability without adding admin credentials to the worker.

If initial attestation or heartbeat startup fails, the plugin retains its local guard but does not register HTTP bridge surfaces. If later refresh fails, bridge surfaces remain, but core freshness expires and new reservations stop.

## Delegation claim rejection

The first bounded `delegate_task` call for a live canonical run atomically consumes that run's delegation claim before the tool handler starts. If the guard reports that the run cannot claim another batch:

1. Confirm the task ID, run ID, contract digest, and requested child count match the live execution contract.
2. Check audit for `execution.delegation_batch_claimed` under the canonical run ID.
3. Treat an existing claim as consumed even if Hermes restarted or the earlier tool call failed. Do not delete the SQLite state or retry through another plugin process.
4. Continue the parent task without more delegation, or move genuinely new work through a separately authorized canonical run.

A stale, terminal, blocked, foreign, malformed, or already-claimed run fails closed. The bridge credential cannot override this result.

## Reconciliation and verification

Hermes completion moves local work to `review`. To reach `done`, the next supervisor pass must see:

- Exact Hermes card and work IDs.
- `hermes-kanban` provenance.
- Matching completion fingerprint in event and work metadata.
- Completed local run.
- Evidence for every exact acceptance criterion.
- Confidence of at least `0.75`.
- A passing deterministic report whenever Hermes declares artifacts or the canonical work carries a `verification_contract`.

Failed verification moves work to `blocked`; incomplete evidence moves it to `waiting_input`. A deterministic failure records exact artifact or named-check errors under `metadata.last_verification.deterministic`; it cannot be overridden by a model's passed verdict. Check configured artifact-root visibility first, then missing files, symlinks, type or digest mismatches, byte/count limits, check cwd, timeout, output cap, and exit code. Correct evidence or scope rather than manually forcing done.

A failed independent verification may authorize a correction only from the exact completion event and while the durable authorization root has remaining attempts. The correction uses a new local run, a new `attempt` idempotency key, and a new Hermes card. It does not unblock or overwrite the completed card. `hermes.max_execution_attempts` is the hard upper bound; once exhausted, operator review and a newly scoped authorization are required.

## Webhook authentication

For `401 invalid_signature`:

1. Match the source name to `[server.webhook_secrets]` exactly.
2. Confirm both processes received the same nonempty secret.
3. Compute HMAC over exact final JSON bytes.
4. Use lowercase hex with `sha256=` prefix.
5. Confirm a proxy did not rewrite the body.
6. Restart the operator after configuration changes.

A configured source always requires HMAC. The admin token does not bypass it. An unconfigured source may use the admin token as a delivery fallback, but an inbound reader should never receive that token.

The bridge token is accepted only for source `hermes`. It is not a generic inbound credential.

Repeated delivery with the same source and dedupe key returns `created: false` and the original event ID.

## Obsidian late binding and failures

Bind a vault later:

```bash
HERMES_OPERATOR_VAULT=/absolute/path/to/vault \
hermes-operator --config operator.toml project
```

For systemd, add only the exact writable path:

```ini
[Service]
Environment=HERMES_OPERATOR_VAULT=/absolute/path/to/vault
ReadWritePaths=/absolute/path/to/vault
```

For a container, add one explicit read-write mount. Do not mount the vault over the database volume.

Check path existence, `.obsidian` marker or intentional `--create`, service permissions, sandbox mounts, symlink safety, and ambiguous discovery candidates.

Do not repair canonical work by editing generated Markdown. Make the change through CLI or API and reproject.

## Approval incidents

```bash
hermes-operator --config operator.toml approval show ACTION_ID
hermes-operator --config operator.toml approval deny ACTION_ID \
  --reason "Content or recipient is incorrect"
```

Any changed recipient, target, content, attributes, integration, or action type needs a new proposal and digest. Approval never sends the action. The core API and `hermes-operator` CLI cannot consume a grant for execution.

The installed broker is separate and disabled by default:

```bash
cp config/outbound.example.toml config/outbound.toml
# Review fixed connector argv, pass_env, database path, and set enabled = true.
hermes-outbound-broker --config config/outbound.toml execute ACTION_ID \
  --grant-id GRANT_ID
```

Invoke it only after final approval and under its separate service identity. If the broker consumed a grant but provider state is uncertain, inspect the action's `executing`, `executed`, or `execution_failed` status plus broker and provider idempotency records before approving anything again. The one-shot grant prevents retrying an unknown outcome silently.

## Memory review

```bash
hermes-operator --config operator.toml memory list --status quarantined
hermes-operator --config operator.toml memory list --status pending
hermes-operator --config operator.toml memory show MEMORY_ID
hermes-operator --config operator.toml memory promote MEMORY_ID
# or
hermes-operator --config operator.toml memory reject MEMORY_ID
```

Review is one way in the current CLI. Add a new trusted correction rather than editing SQLite or projected Markdown. Only promoted memory enters the Obsidian projection.

## Database integrity and disk pressure

```bash
DB_PATH="$PWD/data/operator.db" \
python -c 'import os,sqlite3; db=sqlite3.connect(os.environ["DB_PATH"]); print(db.execute("PRAGMA integrity_check").fetchone()[0]); db.close()'
```

Keep SQLite on a reliable local filesystem. WAL and SHM files are normal and must not be deleted during operation. Bound webhook bodies and store large attachments in an approved artifact store instead of the event database.

## Upgrade

1. Review code, schema, plugin, and policy changes.
2. Take and verify a database backup.
3. Stop the service.
4. Install into a fresh environment or image.
5. Run core and plugin tests.
6. If `policy.py` changed, review and update the exact digest allowlist.
7. Run `doctor` against a copy of production configuration.
8. Start in `shadow` and verify one cycle.
9. Verify token separation, signed webhooks, leader lease, Hermes health, policy heartbeat, and stale-attestation fail-closed behavior.
10. Restore `internal` only after worker credential and egress isolation are confirmed.

Initialization and schema migration are idempotent, but there is no downgrade command. Keep the pre-upgrade backup until a full reviewed cycle completes.
