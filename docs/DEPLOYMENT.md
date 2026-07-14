# Deployment

## Supported shape

Hermes Operator is a Python 3.11 service with a local SQLite database. It can be installed before Hermes or an Obsidian vault is available.

The supported active topology is:

```text
one Hermes Operator service
  -> one local SQLite database
  -> optional local or wrapped Hermes CLI
  -> required authenticated Hermes run control for internal or active mode
  -> optional HTTP native plugin bridge
  -> Hermes-native Google, reminder, briefing, and Obsidian skill loops
  -> optional fixed-argv readers for other providers
  -> optional local Obsidian vault
  -> configured model endpoint or local command

separate optional outbound broker identity
  -> deployment-fixed connector and mutation credential
```

One SQLite leader lease permits only one active service instance for a database. Multi-node active-active operation is not implemented.

The native plugin bridge is not a remote Kanban transport. The dispatcher still invokes the configured `hermes` executable. Put the executable on the same host, include it in a deployment-specific image, or use a carefully scoped argv wrapper such as a container exec command.

## Deployment-time bindings

The application does not require fixed installation paths. Configure these at deployment:

- SQLite database and data directory.
- Model provider, model ID, endpoint, and credential environment name.
- Hermes executable, one or more explicitly allowed and independently attested profiles, board, skills, allowlists, authenticated run-control base URL and token, and optional environment passthrough.
- Admin and bridge token environment names.
- HMAC secrets for generic inbound sources.
- Hermes Google account OAuth, Gateway, private Cron delivery target, and optional native Obsidian vault path.
- Optional read-only command readers for non-Google providers and their narrowly scoped credential names.
- Optional core Obsidian projection path, managed directory, and bounded Inbox.

Relative paths are resolved from the TOML file location.

## Native Python installation

```bash
python -m venv /opt/hermes-operator/.venv
/opt/hermes-operator/.venv/bin/python -m pip install /path/to/hermes-operator
/opt/hermes-operator/.venv/bin/hermes-operator \
  --config /etc/hermes-operator/operator.toml init
```

Recommended layout:

```text
/opt/hermes-operator/.venv/                 Python environment
/etc/hermes-operator/operator.toml          non-secret configuration
/etc/hermes-operator/environment            secret environment values
/var/lib/hermes-operator/operator.db        canonical database
```

Use absolute paths in service configuration:

```toml
[operator]
database_path = "/var/lib/hermes-operator/operator.db"
data_dir = "/var/lib/hermes-operator"
```

The supplied [systemd unit](../deploy/hermes-operator.service) is a template for this layout. Review user, group, filesystem, network, executable, and model endpoint settings before installation.

## Docker and Compose

The supplied image contains Hermes Operator, not the Hermes executable. Its embedded configuration is shadow mode with Hermes and Obsidian disabled.

To start the safe base image:

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

Compose bind-mounts `./operator.toml` at `/app/operator.toml`, sets `HERMES_OPERATOR_BIND_HOST=0.0.0.0` inside the container, and publishes only to host loopback. It passes an optional `HERMES_KANBAN_CONTROL_TOKEN` into the daemon container for internal or active deployments; adapter environment filtering prevents forwarding it to the Hermes child. It runs as a non-root user, drops Linux capabilities, uses a read-only root filesystem, and stores `/app/data/operator.db` in a named volume.

To enable Hermes dispatch in a container, build a deployment-specific image or wrapper with the compatible Hermes CLI and required profile state. Validate that its Kanban help exposes `create`, `show`, `list`, `comment`, `block`, `unblock`, and `runs`. Internal and active mode also require network access from the daemon to the authenticated Hermes run-control base URL.

Install the native bridge from its independent wheel or source plugin directory as documented in [Hermes Native Integration](HERMES_INTEGRATION.md). A release deployment does not require a retained source checkout or a Python environment shared with this service.

Do not claim the provided image enforces a complete worker egress policy. Apply deployment-specific network controls as described below.

## Required environment

For the operator service:

```text
OPENAI_API_KEY=replace-with-provider-key
HERMES_OPERATOR_API_TOKEN=replace-with-random-admin-token
HERMES_OPERATOR_BRIDGE_TOKEN=replace-with-different-random-bridge-token
HERMES_KANBAN_CONTROL_TOKEN=replace-with-run-control-token
HERMES_OPERATOR_BIND_HOST=127.0.0.1
```

The bridge token is needed when the native plugin is used. The admin and bridge values must be different. The configuration loader rejects equal nonempty values.

For the Hermes native plugin:

```text
HERMES_OPERATOR_URL=http://127.0.0.1:8787
HERMES_OPERATOR_BRIDGE_TOKEN=replace-with-different-random-bridge-token
HERMES_OPERATOR_PROFILE=operator
HERMES_OPERATOR_ATTEST_INTERVAL_SECONDS=120
```

Do not inject `HERMES_OPERATOR_API_TOKEN` into the Hermes worker.

## Token and network separation

The two API credentials have different authority:

| Credential | Intended holder | Capability |
| --- | --- | --- |
| Admin token | Operator CLI/UI or trusted administration client | Read canonical work and approvals, ingest operator events, answer questions, approve or deny actions, and review memory |
| Bridge token | Native Hermes plugin only | Use explicit context, attention, conversational-management, Google-ingress, exact task-contract, lifecycle, compatibility, and attestation routes; no approval inspection or external execution |
| Run-control token | Hermes Operator daemon only | Authenticate native Hermes run termination; never passed to the worker |
| Webhook HMAC secret | One inbound reader | Deliver authenticated but untrusted events for one source |

Use TLS termination or a private authenticated network when traffic leaves loopback. The built-in server speaks plain HTTP.

## Managed-card scope and optional worker hardening

The daemon contains no outbound connector and no execute route. The plugin adds strict task-scoped policy on Operator-managed cards. Interactive and Cron sessions outside those cards retain Hermes-native behavior, and identifiable mutations use the native confirmation gate. A Hermes worker may still have terminals, interpreters, browsers, custom plugins, MCP tools, or ambient cloud credentials, so this project does not claim host-wide or end-to-end mediation. In particular, contract-authorized `local_test` and `local_build` commands can invoke project-defined scripts that execute arbitrary repository code.

When a deployment needs a harder worker boundary, optional controls include:

1. Give the Hermes worker only the scoped bridge token and minimum read or internal-work credentials.
2. Do not give it the admin token, approval secret, email-send credential, calendar-write credential, messaging credential, publishing credential, repository-write credential, payment credential, or a browser session that can mutate external state.
3. Run the worker in an operating-system or container sandbox with network egress disabled by default and filesystem access scoped to the authorized workspace. Apply the same restriction to project test and build scripts.
4. Allow only the specific model, operator API, read-only data, artifact, and internal execution endpoints required for the `operator` profile through a controlled proxy or network policy.
5. Run `hermes-outbound-broker` under a separate identity and network policy. Do not colocate its connector credentials with the worker or control-plane daemon.

Without these controls, managed-card policy is defense in depth rather than a hard security guarantee. The core autonomy path can still operate under Hermes-native confirmation and the deployment's existing harness policy.

## Hermes policy attestation

When `hermes.require_policy_attestation = true`, each selected worker profile must have a fresh attestation for that exact profile before it can receive a new run.

The example allows:

```toml
require_policy_attestation = true
policy_attestation_ttl_seconds = 300
allowed_plugin_versions = ["1.3.0"]
allowed_policy_versions = ["4.0.0"]
allowed_policy_digests = ["dde4664b6db0ac57fb5ef9b773e2f707c63831cc81ad0086a139f76dbfd17685"]
```

The plugin performs a synchronous startup attestation and starts one process-scoped daemon heartbeat that attempts refresh every 120 seconds by default. Normal hooks share the same lock and rate limiter. Core dispatch validates freshness, profile, active default-deny guard, plugin version, policy version, and source digest. The dispatch reservation also binds the exact attestation state digest used for its decision. Enabling Hermes with required attestation also requires the built-in HTTP server and a nonempty bridge token; configuration fails closed otherwise.

Internal and active execution additionally require `hermes.control_base_url` and the token named by `hermes.control_token_env`. The value must be an HTTP or HTTPS base URL without embedded credentials, query, or fragment. The daemon uses this authenticated path to terminate the current native run, then blocks the card with the CLI. Never put this token in `hermes.pass_env`, the plugin environment, an inbound reader, or the outbound broker.

## Optional separate outbound broker deployment

The package includes `hermes-outbound-broker`, but neither the supplied Compose service nor the systemd unit starts it. It is not required for autonomous intake, planning, Hermes work, reminders, briefings, or Obsidian use. If exact-action delivery is wanted outside Hermes-native confirmation, copy [the disabled example](../config/outbound.example.toml) to a protected configuration, set a fixed connector argv, and allowlist only that connector's mutation credential. A distinct service account and network policy are optional hardening.

The broker must be invoked with an exact approved action ID and grant ID. Its database path points to the canonical SQLite store, so filesystem permissions must allow the broker to atomically claim that action without giving the Hermes worker database access. The default systemd `StateDirectoryMode=0700` and `UMask=0077` intentionally block a second account. If the broker uses a distinct operating-system identity, grant a narrowly scoped group or ACL access to the exact SQLite directory, database, WAL, and SHM files, then audit that access. The broker can read canonical state and is therefore a privileged process.

The connector receives bounded strict JSON on stdin and must return bounded strict JSON with `"ok": true`. The broker never polls arbitrary model output for commands and cannot change recipients, content, target, attributes, integration, or action type after approval.

For a long-running integration, wrap broker invocation in a separately reviewed deployment service. Do not add broker credentials to `compose.yaml`, `deploy/hermes-operator.service`, the daemon environment file, or the native plugin. Approval does not automatically invoke it.

After modifying the plugin policy source, compute its SHA-256 digest, update the reviewed core allowlist, deploy both sides, and verify startup attestation before enabling `internal` mode.

## One leader and local SQLite

At startup, the service obtains the `operator-control-plane` lease. Startup fails if another live owner holds it. `run-once` also acquires and releases this lease.

Use a local filesystem with reliable POSIX or platform SQLite locking. Do not place the live database in:

- An Obsidian vault.
- A cloud-sync directory.
- A network filesystem with uncertain locking.
- A shared volume used by independent active service instances.

SQLite runs in WAL mode. Use the SQLite backup API for online backups.

## Native Google intake and optional generic readers

Install the desired native jobs after configuring Hermes' bundled Google Workspace skill and account OAuth:

```bash
hermes-operator --config operator.toml native-jobs plan
hermes-operator --config operator.toml native-jobs install --dry-run
hermes-operator --config operator.toml native-jobs install
```

The Google job reads Gmail, Calendar, and meeting evidence through Hermes and records normalized revisions with the plugin. The reminder job privately delivers one atomic claim of due reminders and pending questions. The daily briefing combines current priorities with optional Hermes-native Obsidian context. Run Hermes Gateway continuously and configure a private delivery target. These jobs are the implemented default path, not provider placeholders.

For non-Google providers or an intentionally external integration, the live observation phase can poll a fixed argv using `[[inbound_connectors]]`, or a reader can deliver signed webhook events.

Each reader should:

1. Use a read-only provider credential.
2. Return a bounded page with a next cursor for built-in command polling, or maintain its own cursor when using webhooks.
3. Normalize one object or version into the generic event envelope.
4. Use only explicitly allowlisted environment variables. For webhook delivery, sign the exact body with a dedicated HMAC secret.
5. Reuse a stable source-scoped dedupe key on retry.
6. Keep untrusted body size and attachments bounded.

See [Connectors](CONNECTORS.md).

## Obsidian late binding

Obsidian is optional. Install and run the control plane with:

```toml
[obsidian]
enabled = false
vault_path = ""
```

Later, bind it through configuration or environment:

```bash
export HERMES_OPERATOR_VAULT=/absolute/path/to/vault
hermes-operator --config operator.toml project
```

For containers or systemd, add the exact vault mount or writable path at the same time. Do not grant broad home-directory write access only to support discovery.

The service projects managed state and separately reads only direct Markdown children of `<operator_root>/Inbox`. Inbox reads are non-recursive, bounded, symlink-safe, deduplicated by path and content, and always treated as untrusted evidence.

For native vault-wide use, set `OBSIDIAN_VAULT_PATH` in the Hermes profile. The daily briefing uses Hermes' bundled Obsidian skill; the Operator core does not create a second vault index.

## Rollout

### Phase 1: shadow

- Configure model and admin authentication.
- Run migrations through normal initialization.
- Send operator and signed connector fixtures.
- Review work hierarchy, provenance, questions, priorities, approvals, and memory candidates.
- Confirm the second service instance cannot acquire the leader lease.

### Phase 2: Hermes integration in shadow

- Install or expose the compatible Hermes CLI.
- Install the native plugin.
- Configure the distinct bridge token and exact worker profile.
- Configure each selected profile explicitly and verify a fresh attestation for each identity.
- Verify the plugin guard and startup attestation.
- Check the allowed policy digest against the deployed source.
- Confirm shadow mode records but does not create cards.

### Phase 3: bounded internal autonomy

- Choose deployment-appropriate credential and egress hardening.
- Configure a narrow skill allowlist and only the explicitly needed attested profiles.
- Configure authenticated Hermes run control and verify terminate-then-block behavior.
- Set a conservative global `max_parallel_work`.
- Move to `internal`.
- Dispatch test work with exact acceptance criteria.
- Confirm atomic capacity, review state, and evidence-bound verification.

### Phase 4: optional memory projection

- Bind the vault explicitly.
- Promote reviewed memory.
- Confirm writes stay below `operator_root` and canonical state remains in SQLite.

### Phase 5: optional approved outbound execution

- Keep the broker disabled while configuring and reviewing it.
- Create a distinct broker identity, exact database ACL, network policy, and connector credential.
- Use one fixed-argv connector per integration and an explicit environment allowlist.
- Test with an isolated non-production provider target.
- Approve one exact action and invoke the broker with its returned grant ID.
- Confirm replay and every content, recipient, target, option, integration, and type change fail closed.

`active` may be used after operational review, but it has no extra code capability in this release.

## Unsupported deployment claims

The current implementation does not provide:

- PostgreSQL or another server database adapter.
- Active-active services.
- Transactional outbox delivery.
- Provider SDKs inside the Operator daemon. Google account OAuth is handled by Hermes' bundled Google Workspace skill and the installed native Cron contract.
- Generic remote Hermes Kanban HTTP transport beyond authenticated native-run termination.
- Automatic profile discovery or capability routing. Multiple explicitly configured attested profiles are supported.
- Profile, project, or model-provider concurrency limits.
- Automatic recovery of an outbound side effect after connector outcome becomes unknown.
- A broker service in the supplied Compose or systemd deployment.

Design deployment and monitoring around the implemented local SQLite, Hermes CLI and Cron, native plugin bridge, generic webhook extensions, and optional projection boundaries.
