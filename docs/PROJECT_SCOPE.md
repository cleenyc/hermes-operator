# Hermes Operator Project Scope

## Purpose

Hermes Operator is a portable control plane built on top of Hermes Agent. Its purpose is to
provide a continuously operating personal assistant that can understand incoming work,
maintain a durable model of that work, decide what should happen next, and autonomously
complete well-scoped internal tasks through Hermes-native capabilities.

The system should behave as a live, context-aware operating loop. Scheduled jobs may wake
the system or collect signals, but they are not the primary reasoning architecture.

## Core objectives

1. Maintain a canonical inventory of tasks, todos, reminders, projects, goals, statuses,
   dependencies, and related evidence.
2. Triage new work automatically and organize it into useful hierarchy, priority, and next
   actions.
3. Continuously recommend the highest-value work for the operator or the system to complete.
4. Complete internal work autonomously when the scope, authority, and acceptance criteria
   are clear.
5. Coordinate multiple Hermes workers or subagents in parallel while preserving durable
   ownership, status, and result tracking.
6. Monitor operator-approved inbound surfaces such as email, calendar events, meetings, and
   transcripts for actionable signals.
7. Ask the operator for context whenever uncertainty, missing authority, or scope ambiguity
   could cause drift.
8. Use an Obsidian vault as the operator's long-term second brain while keeping operational
   state deterministic and recoverable.

## Required capabilities

### Work intake and triage

- Normalize signals from conversations, provider readers, reminders, meetings, and manual
  capture into durable events.
- Distinguish actionable work, reference material, questions, reminders, and duplicates.
- Preserve source provenance and prevent untrusted provider content from silently granting
  authority.

### Work graph and prioritization

- Represent goals, projects, tasks, reminders, dependencies, parent-child relationships,
  blockers, due dates, effort, risk, and operator priority.
- Recalculate next-best work as state and evidence change.
- Surface blocked, stale, overdue, and clarification-dependent work proactively.

### Autonomous execution

- Dispatch only finalized, eligible, authorized work.
- Define explicit acceptance criteria and verification requirements before execution.
- Track attempts, leases, completion evidence, failures, retries, and recovery durably.
- Separate planning authority from execution authority.

### Parallel orchestration

- Run independent work concurrently up to a configurable capacity limit.
- Assign work to allowlisted Hermes profiles and skills.
- Preserve one canonical state transition path even when several workers execute in parallel.
- Prevent recursion, duplicate dispatch, and unbounded worker spawning.

### Proactive operation

- Use an event-driven supervisor with quiet-time reconsideration and bounded scheduled wakeups.
- Deliver private reminders, open questions, and concise briefings through Hermes-native
  delivery surfaces.
- Revisit priorities when new evidence, deadlines, dependencies, or operator answers arrive.

### Context and memory

- Project reviewed operational knowledge into an Obsidian vault without making the vault the
  sole source of transactional truth.
- Use Hermes-native Obsidian retrieval for broader context.
- Promote long-term memory deliberately, with provenance and review where appropriate.

### Human input

- Ask exact, answerable questions tied to the affected work item and current scope.
- Preserve answers without allowing stale answers to mutate changed work.
- Make unresolved questions visible until answered, superseded, or explicitly dismissed.

## Authority and external-action boundary

Hermes Operator may autonomously perform reversible internal work within an authorized task
scope. It must not independently send external communications, publish content, invite
participants, create public posts, execute financial actions, or perform equivalent
external-facing mutations without explicit final approval or a separately defined exact
authorization.

Approval should be bound to the exact action, recipients, target, content, relevant work
scope, and expiration. Preparing a draft is not authority to send it.

## Hermes-native integration requirements

- Hermes Kanban remains the durable execution surface for dispatched work.
- Hermes skills provide provider access and reusable operating procedures.
- Hermes profiles and worker mechanisms provide parallel compute.
- Hermes Cron may collect signals and deliver private reminders or briefings.
- The Google Workspace skill and account OAuth may provide Gmail, Calendar, meeting, and
  transcript access.
- The Obsidian skill provides vault-wide retrieval.
- A narrow native plugin connects Hermes lifecycle and policy hooks to the Operator control
  plane.

The Operator layer should integrate with these mechanisms without replacing or reordering
Hermes internals unnecessarily.

## Portability requirements

- No hard-coded Hermes installation path, Obsidian vault path, account identity, or deployment
  environment.
- Configuration is supplied at deployment time.
- Core operational state is durable and restart-safe.
- Host integrations are narrow, versioned, diagnosable, and fail closed when a required
  contract is known to be incompatible.
- Local, container, and service deployment should be possible from the same source release.

## Operating model

The intended loop is:

1. Observe new evidence or a change in canonical state.
2. Build a bounded snapshot of relevant work, authority, history, and memory.
3. Run a live planning pass.
4. Validate every proposed effect against deterministic policy and version fences.
5. Commit accepted state changes atomically.
6. Dispatch eligible work in parallel through Hermes.
7. Reconcile lifecycle events and verify completion evidence.
8. Ask for operator input or recommend the next best action when autonomy should stop.

## Initial success criteria

- No actionable inbound event disappears without a durable disposition or follow-up.
- A task can progress from intake through planning, execution, verification, and completion.
- Independent tasks can run in parallel without exceeding configured capacity.
- Scope changes invalidate stale execution authority.
- Reminder delivery and recurrence survive restarts and snoozing.
- The operator receives durable questions and next-work recommendations.
- Provider content cannot authorize execution merely by containing instructions.
- External-facing actions remain approval-gated.
- Operational state can be projected into and contextualized by an Obsidian vault.
- The release can be configured without assuming a specific host filesystem layout.

## Authorship

Maintained by [@cleenyc](https://github.com/cleenyc).
