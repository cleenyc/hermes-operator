# Hermes Operator native plugin

This package is the narrow native bridge between Hermes and the portable
Hermes Operator control plane. It contributes work, question, reminder, inbound
capture, diagnostics, lifecycle observations, policy attestation, and a task-scoped
default-deny pre-tool guard.
The guard permits internal work only when the control plane returns an exact
live execution contract for the current Hermes task.

The pinned real-host compatibility target for this release is Hermes Agent
`0.18.2`, tag `v2026.7.7.2`, commit
`9de9c25f620ff7f1ce0fd5457d596052d5159596`. The version is diagnostic rather than
an exact activation lock: another build may activate only when the required profile,
dispatcher identity, and pre-tool directive semantics are positively verified. The
optional real-host test becomes mandatory when
`HERMES_OPERATOR_REQUIRE_HOST_INTEGRATION=1` is set.
Release CI runs that test suite against the pinned commit as a required check and against
current Hermes `main` as an advisory compatibility signal.

Run the pinned host lane in an isolated environment with:

```text
python -m pip install -r tests/hermes-host-requirements.txt
HERMES_OPERATOR_REQUIRE_HOST_INTEGRATION=1 python -m unittest discover -s tests -v
```

Install the wheel in the same Python environment as Hermes, register the
plugin using the normal Hermes plugin mechanism, and configure these required
environment variables:

```text
HERMES_OPERATOR_URL=https://operator.internal
HERMES_OPERATOR_BRIDGE_TOKEN=<dedicated bridge token>
HERMES_OPERATOR_PROFILE=operator
```

Loopback HTTP is supported for same-host development. Remote bridge URLs must
use HTTPS. The bridge token must be distinct from the operator admin token.

For Operator-managed Kanban cards, the plugin blocks external communication and
publication tools, completion artifact delivery, native Kanban fanout, non-durable
delegation, and uncontracted local mutation. Current Hermes top-level
`delegate_task` is background and non-durable, so managed cards fail closed and the
Operator uses canonical parallel cards instead. Interactive and cron sessions keep
Hermes-native behavior; identifiable scheduler and external Google writes use the
native human-approval gate. Project-defined test and build scripts can still execute
repository code. Deployments that require a hard boundary independent of Hermes can
add operating-system or container filesystem scoping, credential isolation, and
network egress controls without changing this plugin.

`HERMES_KANBAN_TASK`, which Hermes sets only for dispatcher-spawned workers, is
the authoritative managed-card marker. Ordinary interactive and Cron turns remain
native even though Hermes gives each one an ephemeral UUID and forwards that UUID
to hooks. A quiet Kanban worker also receives a new turn UUID, so the dispatcher-owned
environment marker remains canonical while the UUID is used only as turn correlation.

Bridge attestation and all Operator tools remain disabled when the active Hermes
profile, dispatcher ownership of `HERMES_KANBAN_TASK`, first-valid pre-tool directive
semantics, or first-position Operator guard cannot be positively verified. The guard
itself stays installed in fail-closed policy-only mode, and the plugin publishes a
best-effort negative policy event so the control plane can revoke stale positive state.

Managed completions reject explicit artifacts, artifact-like metadata, and every
absolute, drive-letter, or `~`-relative local path embedded in `summary` or `result`,
regardless of whether the path resolves inside the worker workspace. This includes
`..` and symlink aliases. There is intentionally no
environment flag that declares a private artifact sink: the current pre-tool hook does
not receive an authenticated notification-recipient identity, so it cannot prove the
configured label is the actual destination. Deliver files from an interactive Hermes
turn using native approval.

`/operator` supports direct task capture, reminder capture, question answers, exact
work authorization, completion, snoozing, priorities, due reminders, and compatibility
diagnostics. Model-facing answers, authorization, reminder resolution, terminal-status,
hierarchy, cron, and external Google mutations require Hermes native approval.
Authorization is version-, graph-, and digest-fenced. First read the current shape with
`operator_authorization_scope` or `/operator scope <work-id>`, then use
`/operator authorize <work-id> <version> <scope-revision> <scope-digest> [reason]`.
Model-facing authorization may also bind an exact profile, skills list, and goal mode.
Hermes approval cache keys include the work version, scope revision, scope digest, and
execution shape.

Hermes' managed attention cron calls `operator_claim_attention` once per delivery turn.
That atomic claim returns due reminders and pending questions together and applies the
core redelivery window. `operator_due_reminders` is preview-only. Reminder creation and
updates accept fixed ISO-8601 recurrence rules (`PTnM`, `PTnH`, `PnD`, or `PnW`), and
`/operator snooze` and `operator_resolve_reminder` use the dedicated reminder lifecycle
route. They record a temporary `reminder_snoozed_until` delivery override without
moving the recurring reminder's `due_at` schedule anchor.

See the main project documentation for installation, deployment, policy pins,
and the full threat model.
