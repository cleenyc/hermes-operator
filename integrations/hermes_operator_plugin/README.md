# Hermes Operator native plugin

This package is the narrow native bridge between Hermes and the portable
Hermes Operator control plane. It contributes work, question, reminder, inbound
capture, diagnostics, lifecycle observations, policy attestation, and a task-scoped
default-deny pre-tool guard.
The guard permits internal work only when the control plane returns an exact
live execution contract for the current Hermes task.

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

`/operator` supports direct task capture, reminder capture, question answers, exact
work authorization, completion, snoozing, priorities, due reminders, and compatibility
diagnostics. Model-facing answer, authorization, terminal-status, hierarchy, cron, and
external Google mutations require Hermes native approval.

Hermes' managed attention cron calls `operator_claim_attention` once per delivery turn.
That atomic claim returns due reminders and pending questions together and applies the
core redelivery window. `operator_due_reminders` is preview-only. Reminder creation and
updates accept fixed ISO-8601 recurrence rules (`PTnM`, `PTnH`, `PnD`, or `PnW`), and
`/operator snooze` moves the reminder's `due_at` value.

See the main project documentation for installation, deployment, policy pins,
and the full threat model.
