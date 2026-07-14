# Changelog

All notable project changes are recorded in this file.

## [0.4.0] - 2026-07-14

### Added

- Added an exact authorization-scope preview and three-part version, scope-revision,
  and scope-digest fence across the bridge API, native plugin, slash command, and CLI.
- Added immutable per-run execution contracts that survive completion and bind evidence
  to the dispatched work scope, profile, run, card, and attempt.
- Added authenticated `policy.revoked` evidence so a failed host compatibility check
  invalidates cached managed-execution attestation immediately.
- Added deterministic blocked-card questions and durable completion-evidence review paths.

### Fixed

- Bound managed policy and lifecycle correlation to the dispatcher-owned
  `HERMES_KANBAN_TASK` identity while allowing Hermes quiet turns to keep their native UUIDs.
- Revoked execution authority on terminal transitions and reopen, rejected authorization of
  terminal work, detached prior cards on reopen, and prevented pre-terminal events from
  reviving cancelled or archived work.
- Advanced both work versions and authorization scope revisions when dependency edges change,
  preventing stale graph confirmation from authorizing a newer dependency view.
- Preserved the immutable dispatch contract through run completion and rejected old, malformed,
  recovered-without-contract, or differently scoped completion evidence with durable follow-up.
- Rejected absolute, home-relative, Windows, alias, symlink, outside-workspace, and `MEDIA:`
  completion paths before Hermes can promote local files into Gateway attachments.
- Repaired the blocked worker question, answer, fresh authorization, and same-card resume loop.
- Bound blocked and completion lifecycle evidence to the latest canonical card, run, attempt, and
  scope generation; malformed evidence creates a separate review without mutating claimed work.
- Linearized live execution-contract decisions, revalidated policy state when committing a remote
  card, and preserved immutable execution contracts through queued-run recovery.
- Prevented stale or duplicate authorization from replacing live-run authority, advanced scope
  generations on executor changes, explicit execution disable, quarantine, and run resolution,
  and required terminal scope edits to reopen work first.
- Limited same-card unblock to an unchanged immutable execution contract; changed scope starts a
  new card, and only answers whose stored binding matches the current scope enter resume context.
- Required a successfully staged durable intent before recording an external-action proposal
  disposition, and expanded actionable intake detection to documented body and action-item shapes.
- Registered the schedule-anchor-safe reminder lifecycle tool and corrected Cron and skill guidance
  so snoozing never changes `due_at`.

### Changed

- Managed activation now requires positive evidence for active-profile, hook-order, directive,
  and dispatcher identity semantics; Hermes version differences remain diagnostic rather than a
  brittle exact-version lock.
- Removed background `delegate_task` from managed execution capabilities and worker guidance;
  durable parallel canonical cards remain the supported orchestration mechanism.
- Moved verifier-contract mutation to its own CLI command so dispatch cannot change scope after
  the operator reviews its authorization digest.
- Existing in-flight runs without an immutable execution contract now fail closed into durable
  operator review instead of being migrated as trusted completion evidence.
- CI now runs the complete core and source-only plugin checks in addition to required pinned-host
  and advisory current-host compatibility lanes.
- Updated the core to `0.4.0`, the native plugin to `1.5.0`, and the policy contract to `6.0.0`.

## [0.3.0] - 2026-07-14

### Added

- Added canonical execution-scope digests and an independent authorization scope revision
  covering work semantics, hierarchy, schedule, verification, execution profile, effective
  skills, and goal mode.
- Added durable work bindings for operator questions and explicit reauthorization follow-up
  when an answer or approval becomes stale.
- Added durable non-executable review items and operator questions for quarantined actionable
  events.
- Added pinned Hermes Agent `0.18.2` host-contract tests and CI against commit
  `9de9c25f620ff7f1ce0fd5457d596052d5159596`, with an advisory current-`main` lane.
- Added full-host coverage for ordinary turn IDs, first-valid hook resolution, and Gateway
  completion artifact delivery.

### Fixed

- Treated `HERMES_KANBAN_TASK` as the authoritative managed-worker marker so ordinary Hermes
  UUID turns retain native interactive and Cron behavior.
- Invalidated authority when scope-bearing work or dependency state changes while preserving
  valid approval across priority-only and runtime-only changes.
- Moved deterministic filesystem and subprocess verification outside SQLite write
  transactions, bound the report to immutable completion evidence, and eliminated duplicate
  fixed-check execution.
- Prevented actionable quarantine from reaching processed state without a durable review and
  attention path.
- Rejected explicit artifacts, workspace paths in completion prose, and non-string completion
  payloads before Hermes can promote them into Gateway file delivery.
- Disabled bridge attestation and managed execution when a known active-profile mismatch or
  incompatible first-valid hook order is observed.
- Preserved the recurrence schedule anchor when a reminder is snoozed.
- Corrected read-only due-reminder previews to honor the current snooze window.

### Changed

- Updated the core to `0.3.0`, the native plugin to `1.4.0`, the policy contract to `5.0.0`,
  and the database schema to version 12.
- Expanded the release suite to 249 core tests and 88 plugin tests.

## [0.2.0] - 2026-07-14

### Added

- Delivered the first complete reproducible source release, including build metadata,
  release tooling, 315 tests, Docker, Compose, systemd, examples, licensing, and expanded
  API, configuration, connector, operating-model, reminder, automation, and threat-model
  documentation.
- Added durable dispositions for claimed events, effect validation, event inspection, and
  audited dead-letter replay.
- Added operational dependency blocking, mixed-cycle protection, hierarchy rollups, progress,
  health, and richer portfolio snapshots.
- Added recurring reminder lifecycle, proactive question and reminder delivery, daily
  briefings, and Hermes-native Google Workspace intake.
- Added conversational work management, exact authorization, provider intake, lifecycle
  observation, and compatibility diagnostics through the Hermes plugin.
- Added artifact-aware deterministic verification with rooted containment, traversal and
  symlink checks, bounded artifacts, file and tree digests, fixed checks, and completion
  binding.

### Changed

- Reworked parallel execution around multiple durable canonical Hermes cards across
  allowlisted and independently attested profiles.
- Scoped strict worker policy to Operator-managed cards so ordinary interactive and Cron
  sessions retain native Hermes behavior.
- Added a quieter live reasoning cadence while preserving event-driven wakeups.
- Strengthened attempt fencing, recovery, and policy attestation.

### Known limitations

- Managed-worker identity, exact authorization scope, verifier transaction isolation,
  quarantine follow-up, completion-prose artifact delivery, compatibility activation, and
  reminder recurrence anchoring required further hardening.

## [0.1.0] - 2026-07-14

### Added

- Defined the objectives, capabilities, authority boundary, Hermes-native integration model,
  portability requirements, operating loop, and initial success criteria.
- Added the first portable event-driven Operator control-plane bundle.
- Added durable SQLite work state, live planning passes, deterministic prioritization,
  bounded Hermes dispatch, exact-action approvals, and optional Obsidian projection.
- Added the Hermes native plugin with lifecycle observation, policy attestation, and a
  task-scoped execution guard.
- Added core and plugin wheels, deployment configuration, and initial architecture,
  integration, deployment, and runbook documentation.
