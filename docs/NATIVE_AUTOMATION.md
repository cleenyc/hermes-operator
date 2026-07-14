# Hermes-native automation

Hermes Operator keeps portfolio state and live reasoning in its own event-driven
service, while deliberately delegating provider access, scheduled activation,
private delivery, and vault search to Hermes. This avoids a second provider SDK,
a second vault index, and an application-owned scheduler.

## Ownership split

| Concern | Owner |
| --- | --- |
| Event triage, durable dispositions, work graph, priorities, questions, execution authority, run reconciliation | Hermes Operator |
| Gmail, Calendar, Drive meeting evidence, and Google OAuth | Hermes bundled `google-workspace` skill |
| Scheduled polling, reminder delivery, and daily briefing delivery | Hermes Cron and Gateway |
| Vault-wide search and note access | Hermes bundled `obsidian` skill |
| Parallel durable execution cards and worker profiles | Hermes Kanban, dispatched by Hermes Operator |
| Final approval for interactive external writes | Hermes native approval UI and the bundled skill's confirmation rules |
| Optional exact-action staged delivery | Separate Hermes Operator outbound broker |

The plugin applies a strict policy only inside Operator-managed Kanban cards.
Normal interactive and Cron sessions retain Hermes-native behavior. Managed
workers cannot use the conversational authority, Google intake, or work-capture
tools to grant themselves more scope.

## Portable desired state

The example configuration includes three desired jobs:

- Read-only Google Workspace intake.
- Due reminders and blocking questions.
- A private daily briefing with ranked work and optional Obsidian context.

The source archive contains only the desired job contracts. It does not assume a
Hermes home, Google account, delivery target, or vault path.

The reminder job calls `operator_claim_attention` once per native Cron run.
That tool uses the atomic `POST /v1/hermes/attention/claim` contract, so a short
Cron cadence does not redeliver the same reminder or question inside the
configured suppression window. The `GET /v1/hermes/attention` and
`GET /v1/hermes/reminders` routes are read-only previews for inspection and do
not record a delivery. Hermes Cron remains the scheduler; Operator stores only
attention state and does not create another timer loop.

Preview the exact jobs and commands:

```bash
hermes-operator --config operator.toml native-jobs plan
hermes-operator --config operator.toml native-jobs install --dry-run
```

After the deployment bindings below are complete, install idempotently by stable
job name:

```bash
hermes-operator --config operator.toml native-jobs install
```

Existing jobs are not silently edited. Use `hermes cron edit` for a deliberate
change, or remove the old managed job and reinstall it.

## Deployment bindings

### 1. Google OAuth

Use Hermes' bundled Google Workspace skill setup. Authorize the services needed
by the operator, normally email, calendar, and Drive. The intake Cron prompt only
reads data and records normalized `google.gmail`, `google.calendar`, and
`google.meeting` evidence through `operator_ingest_inbound`. The core deduplicates
stable provider IDs and revisions and treats all provider content as
authenticated but untrusted evidence.

The same Google account may have write scopes. Interactive writes remain governed
by Hermes' native approval UI and the Google skill's confirmation rules. The
intake job is explicitly forbidden from sending mail, changing labels, RSVPing,
editing events, sharing files, or modifying Google data.

### 2. Gateway and private delivery

Hermes Gateway runs the Cron scheduler and delivers the agent's final response.
Set `native_automation.delivery` to a private target owned by the operator, then
run the Gateway continuously. Do not use `all` or a shared channel for personal
briefings unless that disclosure is intentional.

Hermes makes Cron output continuable through its `cron.mirror_delivery` setting
or a per-job native `attach_to_session` setting. The portable installer does not
edit Hermes configuration. Its output reports when continuable delivery was
requested so the deployment can enable the native setting.

### 3. Obsidian vault

Set `OBSIDIAN_VAULT_PATH` in the Hermes profile environment when the vault is
known. The daily briefing uses the bundled Obsidian skill to resolve that path
and search the vault only when notes materially improve the briefing. Hermes
Operator does not build a second vault-wide index.

The core's optional `[obsidian]` adapter is separate. It can project a rebuildable
Operator dashboard and managed work notes, but SQLite remains canonical. It can
stay disabled if only Hermes-native vault search is wanted.

### 4. Plugin and worker profiles

Install the native plugin in every Hermes profile that can receive Operator work.
Set a distinct `HERMES_OPERATOR_PROFILE` in each profile. Add those profile names
to `hermes.allowed_profiles`; dispatch requires a fresh policy attestation for the
exact selected profile.

Current Hermes top-level `delegate_task` runs in the background. Operator-managed
cards therefore do not treat it as durable child execution. The live planner
decomposes independent work into canonical WorkItems, and the dispatcher starts
those cards in parallel up to `operator.max_parallel_work`. Normal interactive
Hermes sessions may continue to use native delegation.

## Why this is still a live autonomous loop

Cron activates provider reads and private delivery. It does not contain the task
plan. Every new provider revision enters the durable event inbox and wakes the
live supervisor, which receives current work, links, rollups, completed history,
questions, and reviewed memory in a fresh model pass. That pass must give every
event a durable disposition before it can leave the queue.

Between events, the service continuously performs lightweight recovery and
execution reconciliation. A full eventless portfolio reconsideration runs on the
separate `operator.reasoning_refresh_seconds` cadence, so the system remains
proactive without paying for an LLM pass on every recovery tick.

## Native references

- <https://hermes-agent.nousresearch.com/docs/user-guide/features/cron>
- <https://hermes-agent.nousresearch.com/docs/reference/cli-commands>
- <https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/productivity/productivity-google-workspace>
- <https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/note-taking/note-taking-obsidian>
