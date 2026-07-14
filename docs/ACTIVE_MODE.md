# Active Mode Acceptance

Version 0.5 can run the control plane unattended in `active` mode once the
deployment-specific bindings below are complete. Active mode does not grant external
send, publish, deploy, purchase, delete, or other outbound execution to the daemon.
Interactive and Cron sessions outside managed cards remain governed by Hermes-native
policy and confirmation.

## Required configuration

1. Configure a real model and verify its fixed command or API credential.
2. Enable Hermes, set the pinned `0.18.2` host or a reviewed-host override, and install
   plugin `1.6.0` with policy `7.0.0` in every effective execution profile.
3. Configure the admin token, distinct bridge token, independent proof secret, and
   daemon-only Kanban control token.
4. Configure the authenticated Kanban control base URL used for read-only health and
   native run termination.
5. Review worker filesystem, credential, and network egress controls, then set
   `hermes.active_isolation_acknowledged = true`.
6. Set `operator.autonomy_mode = "active"` only after these checks pass.

The acknowledgement is a fail-closed rollout gate. It does not make the plugin an
operating-system sandbox and it does not override any host compatibility blocker.

## Native loops

1. Complete Google account OAuth through Hermes' bundled Google Workspace skill.
2. Run Hermes Gateway continuously and select a private, non-local delivery target.
3. Bind `OBSIDIAN_VAULT_PATH` in the Hermes profile when the vault is available.
4. Preview and install the native jobs. Upgrade older jobs with explicit reconciliation:

```bash
hermes-operator --config operator.toml native-jobs plan
hermes-operator --config operator.toml native-jobs install --dry-run --reconcile
hermes-operator --config operator.toml native-jobs install --reconcile
```

The installer uses `hermes cron list --all`, so paused and disabled managed jobs are
found and updated instead of duplicated. Reconciliation preserves native paused state;
review a paused job and use `hermes cron resume JOB_ID` before rerunning live doctor.
Duplicate exact managed names must be removed before reconciliation or live readiness
can pass.

## Acceptance sequence

Run these commands as the same service identity and with the same environment used by
the unattended service:

```bash
hermes-operator --config operator.toml doctor
hermes-operator --config operator.toml doctor --live
hermes-operator --config operator.toml run-once
hermes-operator --config operator.toml status
hermes-operator --config operator.toml audit --limit 100
```

`doctor --live` must pass the real model request, every effective profile attestation,
the authenticated Kanban active-worker probe, and installed native-job/private-delivery
and Cron scheduler or Gateway ticker checks. It deliberately does not claim to test Google OAuth, native Obsidian vault
access, Gateway delivery, or operating-system egress policy. Exercise those paths in
Hermes with a private test message and read-only Google and vault queries.

Before leaving the daemon unattended, verify one bounded managed work item through
dispatch, completion evidence, deterministic verification when required, and terminal
state. Also verify one blocked item through question, answer, fresh authorization, and
resume. The service must report no cycle errors, no stale leader lease, no unexplained
active run, and no growing dead-letter queue.

## Stop condition

If scope drifts, stop the service, change back to `shadow`, pause native jobs, inspect
the audit log and live runs, and revoke worker credentials as needed. Do not rely on a
card status change alone to prove remote compute stopped; confirm the authenticated
run-control acknowledgement.
