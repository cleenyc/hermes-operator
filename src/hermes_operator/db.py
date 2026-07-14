from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import (
    ALLOWED_WORK_TRANSITIONS,
    TERMINAL_WORK_STATUSES,
    Event,
    EventState,
    ExecutionMode,
    QuestionStatus,
    RunRecord,
    TrustLevel,
    UserQuestion,
    WorkItem,
    WorkKind,
    WorkRelation,
    WorkStatus,
    new_id,
    next_recurrence_due,
    normalize_recurrence_rule,
    utc_now,
)


SCHEMA_VERSION = 9


class StateConflict(RuntimeError):
    pass


class LeaseFenceLost(StateConflict):
    pass


class NotFound(KeyError):
    pass


class SQLiteStore:
    """Transactional operational store.

    Connections are deliberately short-lived so the store can safely be used
    by the daemon, HTTP server, and CLI in separate threads or processes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._local = threading.local()

    def initialize(self) -> None:
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            self._chmod(self.path.parent, 0o700)
        with self.connection() as connection:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            if meta_exists is not None:
                stored_version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if stored_version is not None:
                    try:
                        version = int(stored_version["value"])
                    except (TypeError, ValueError) as error:
                        raise RuntimeError(
                            "Database schema version is invalid"
                        ) from error
                    if version > SCHEMA_VERSION:
                        raise RuntimeError(
                            "Database schema is newer than this Hermes Operator binary"
                        )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    dedupe_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    claimed_by TEXT,
                    claim_token TEXT,
                    claim_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    processed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_queue
                    ON events(state, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_external
                    ON events(source, external_id);

                CREATE TABLE IF NOT EXISTS event_dispositions (
                    event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                    supervisor_pass_id TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    related_work_ids_json TEXT NOT NULL DEFAULT '[]',
                    related_question_ids_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_dispositions_pass
                    ON event_dispositions(supervisor_pass_id, created_at);

                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    parent_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
                    source_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    priority INTEGER NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    priority_rationale TEXT NOT NULL DEFAULT '',
                    impact REAL NOT NULL DEFAULT 0.5,
                    urgency REAL NOT NULL DEFAULT 0.5,
                    strategic_alignment REAL NOT NULL DEFAULT 0.5,
                    unlock_value REAL NOT NULL DEFAULT 0,
                    risk REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    effort_minutes INTEGER NOT NULL DEFAULT 30,
                    due_at TEXT,
                    scheduled_at TEXT,
                    recurrence_rule TEXT,
                    reminder_last_delivered_at TEXT,
                    reminder_last_acknowledged_at TEXT,
                    reminder_delivery_count INTEGER NOT NULL DEFAULT 0,
                    assignee TEXT,
                    execution_mode TEXT NOT NULL DEFAULT 'none',
                    hermes_task_id TEXT UNIQUE,
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_work_status_priority
                    ON work_items(status, priority_score DESC, priority DESC);
                CREATE INDEX IF NOT EXISTS idx_work_parent ON work_items(parent_id);
                CREATE INDEX IF NOT EXISTS idx_work_due ON work_items(due_at);

                CREATE TABLE IF NOT EXISTS work_links (
                    id TEXT PRIMARY KEY,
                    from_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    to_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(from_id, to_id, relation),
                    CHECK(from_id <> to_id)
                );
                CREATE INDEX IF NOT EXISTS idx_links_to ON work_links(to_id, relation);

                CREATE TABLE IF NOT EXISTS work_rollups (
                    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
                    direct_child_count INTEGER NOT NULL DEFAULT 0,
                    descendant_count INTEGER NOT NULL DEFAULT 0,
                    terminal_count INTEGER NOT NULL DEFAULT 0,
                    done_count INTEGER NOT NULL DEFAULT 0,
                    running_count INTEGER NOT NULL DEFAULT 0,
                    blocked_count INTEGER NOT NULL DEFAULT 0,
                    waiting_input_count INTEGER NOT NULL DEFAULT 0,
                    overdue_count INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    health TEXT NOT NULL DEFAULT 'empty',
                    status_counts_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_questions (
                    id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '',
                    urgency REAL NOT NULL DEFAULT 0.5,
                    blocking_work_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    answer TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    last_delivered_at TEXT,
                    delivery_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_questions_status
                    ON user_questions(status, urgency DESC, created_at);

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    runner TEXT NOT NULL,
                    external_run_id TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_work ON runs(work_item_id, attempt DESC);

                CREATE TABLE IF NOT EXISTS action_intents (
                    id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    source_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT,
                    executed_at TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_intents_status
                    ON action_intents(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_intents_digest ON action_intents(digest);

                CREATE TABLE IF NOT EXISTS approval_grants (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES action_intents(id) ON DELETE CASCADE,
                    intent_digest TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    token_hash TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_grants_intent ON approval_grants(intent_id);

                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    supersedes_id TEXT REFERENCES memory_candidates(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    promoted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_status
                    ON memory_candidates(status, created_at);

                CREATE TABLE IF NOT EXISTS sync_cursors (
                    source TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_leases (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_type, entity_id, sequence DESC);
                """
            )
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "claim_token" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN claim_token TEXT")
            work_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(work_items)"
                ).fetchall()
            }
            for name, declaration in (
                ("recurrence_rule", "TEXT"),
                ("reminder_last_delivered_at", "TEXT"),
                ("reminder_last_acknowledged_at", "TEXT"),
                ("reminder_delivery_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in work_columns:
                    connection.execute(
                        f"ALTER TABLE work_items ADD COLUMN {name} {declaration}"
                    )
            question_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(user_questions)"
                ).fetchall()
            }
            for name, declaration in (
                ("last_delivered_at", "TEXT"),
                ("delivery_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in question_columns:
                    connection.execute(
                        f"ALTER TABLE user_questions ADD COLUMN {name} {declaration}"
                    )
            lease_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(service_leases)"
                ).fetchall()
            }
            if "epoch" not in lease_columns:
                connection.execute(
                    "ALTER TABLE service_leases "
                    "ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0"
                )
            legacy_internal_work = connection.execute(
                "SELECT id, status, metadata_json FROM work_items "
                "WHERE execution_mode = 'internal'"
            ).fetchall()
            for row in legacy_internal_work:
                metadata = json.loads(row["metadata_json"] or "{}")
                metadata["execution_mode_migration"] = {
                    "from": "internal",
                    "to": "none",
                    "requires_operator_review": True,
                    "migrated_at": utc_now(),
                }
                status = str(row["status"])
                if status not in {
                    WorkStatus.DONE.value,
                    WorkStatus.CANCELLED.value,
                    WorkStatus.ARCHIVED.value,
                }:
                    status = WorkStatus.BLOCKED.value
                connection.execute(
                    "UPDATE work_items SET execution_mode = 'none', status = ?, "
                    "metadata_json = ?, updated_at = ?, version = version + 1 "
                    "WHERE id = ?",
                    (status, self._json(metadata), utc_now(), row["id"]),
                )
                self.audit(
                    "schema-migration",
                    "work.internal_execution_disabled",
                    entity_type="work",
                    entity_id=str(row["id"]),
                    connection=connection,
                )
            legacy_dedupe_rows = connection.execute(
                "SELECT id, source, dedupe_key FROM events "
                "WHERE dedupe_key NOT LIKE 'v1:%'"
            ).fetchall()
            for row in legacy_dedupe_rows:
                scoped = f"v1:{row['source']}:" + hashlib.sha256(
                    str(row["dedupe_key"]).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    "UPDATE events SET dedupe_key = ? WHERE id = ?",
                    (scoped, row["id"]),
                )
            duplicate_attempt_work = connection.execute(
                "SELECT work_item_id FROM runs GROUP BY work_item_id, attempt "
                "HAVING COUNT(*) > 1"
            ).fetchall()
            for duplicate in duplicate_attempt_work:
                rows = connection.execute(
                    "SELECT id FROM runs WHERE work_item_id = ? "
                    "ORDER BY COALESCE(started_at, finished_at, ''), id",
                    (duplicate["work_item_id"],),
                ).fetchall()
                for attempt, run_row in enumerate(rows, start=1):
                    connection.execute(
                        "UPDATE runs SET attempt = ? WHERE id = ?",
                        (attempt, run_row["id"]),
                    )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_work_attempt "
                "ON runs(work_item_id, attempt)"
            )
            # SQLite cannot alter a partial-index predicate. Recreate it so
            # nonterminal remote states remain one active execution slot on
            # databases created by earlier schema versions.
            connection.execute("DROP INDEX IF EXISTS idx_runs_one_active_work")
            # A Hermes blocked task has no live worker. Preserve the attempt as
            # terminal history while allowing a governed resume to reserve a
            # fresh compute slot later.
            connection.execute(
                "UPDATE runs SET finished_at = COALESCE(finished_at, heartbeat_at, "
                "started_at, ?) WHERE status = 'blocked' AND finished_at IS NULL",
                (utc_now(),),
            )
            legacy_active_statuses = (
                "'queued', 'running', 'cancel_requested', 'lost'"
            )
            duplicate_active_work = connection.execute(
                "SELECT work_item_id FROM runs WHERE status IN ("
                + legacy_active_statuses
                + ") GROUP BY work_item_id HAVING COUNT(*) > 1"
            ).fetchall()
            for duplicate in duplicate_active_work:
                rows = connection.execute(
                    "SELECT id FROM runs WHERE work_item_id = ? AND status IN ("
                    + legacy_active_statuses
                    + ") ORDER BY attempt DESC, id DESC",
                    (duplicate["work_item_id"],),
                ).fetchall()
                for run_row in rows[1:]:
                    connection.execute(
                        "UPDATE runs SET status = 'legacy_conflict', finished_at = NULL, "
                        "error = CASE WHEN error IS NULL OR error = '' "
                        "THEN 'legacy active-run conflict requires operator review' "
                        "ELSE error || '; legacy active-run conflict requires operator review' "
                        "END WHERE id = ?",
                        (run_row["id"],),
                    )
                    self.audit(
                        "schema-migration",
                        "run.legacy_conflict_quarantined",
                        entity_type="run",
                        entity_id=str(run_row["id"]),
                        data={"work_item_id": duplicate["work_item_id"]},
                        connection=connection,
                    )
            connection.execute(
                "CREATE UNIQUE INDEX idx_runs_one_active_work "
                "ON runs(work_item_id) "
                "WHERE status IN "
                "('queued', 'running', 'cancel_requested', 'lost')"
            )
            hierarchy_parents = [
                str(row["parent_id"])
                for row in connection.execute(
                    "SELECT DISTINCT parent_id FROM work_items "
                    "WHERE parent_id IS NOT NULL"
                ).fetchall()
            ]
            self._refresh_rollup_chain(connection, hierarchy_parents)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        self._secure_database_files()

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except (OSError, NotImplementedError):
            pass

    def _secure_database_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if path.exists():
                self._chmod(path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_database_files()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Share one immediate SQLite transaction across nested store calls."""

        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        with self.connection() as connection:
            self._begin_immediate(connection)
            self._local.connection = connection
            try:
                yield connection
            finally:
                del self._local.connection

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def audit(
        self,
        actor: str,
        event: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        values = (utc_now(), actor, event, entity_type, entity_id, self._json(data or {}))
        if connection is not None:
            connection.execute(
                "INSERT INTO audit_log(timestamp, actor, event, entity_type, entity_id, data_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                values,
            )
            return
        with self.connection() as local:
            local.execute(
                "INSERT INTO audit_log(timestamp, actor, event, entity_type, entity_id, data_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                values,
            )

    def enqueue_event(self, event: Event, *, actor: str = "ingress") -> tuple[str, bool]:
        scoped_prefix = f"v1:{event.source}:"
        if event.dedupe_key and event.dedupe_key.startswith(scoped_prefix):
            scoped_dedupe = event.dedupe_key
        else:
            raw_dedupe = event.dedupe_key
        if not event.dedupe_key:
            material = self._json(
                {
                    "source": event.source,
                    "external_id": event.external_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                }
            )
            raw_dedupe = hashlib.sha256(material.encode()).hexdigest()
        if not (event.dedupe_key and event.dedupe_key.startswith(scoped_prefix)):
            scoped_dedupe = scoped_prefix + hashlib.sha256(
                str(raw_dedupe).encode("utf-8")
            ).hexdigest()
            event.dedupe_key = scoped_dedupe
        with self.connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO events(
                        id, source, external_id, event_type, payload_json, trust_level,
                        provenance_json, dedupe_key, state, created_at, available_at,
                        attempt_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.source,
                        event.external_id,
                        event.event_type,
                        self._json(event.payload),
                        event.trust_level.value,
                        self._json(event.provenance),
                        event.dedupe_key,
                        event.state.value,
                        event.created_at,
                        event.available_at,
                        event.attempt_count,
                    ),
                )
                self.audit(
                    actor,
                    "event.enqueued",
                    entity_type="event",
                    entity_id=event.id,
                    data={"source": event.source, "type": event.event_type},
                    connection=connection,
                )
                return event.id, True
            except sqlite3.IntegrityError as error:
                if "dedupe_key" not in str(error):
                    raise
                row = connection.execute(
                    "SELECT id FROM events WHERE dedupe_key = ?", (event.dedupe_key,)
                ).fetchone()
                assert row is not None
                return str(row["id"]), False

    def list_events(
        self,
        *,
        states: Sequence[EventState | str] | None = None,
        sources: Sequence[str] | None = None,
        event_types: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List event administration records with exact, bounded filters."""

        bounded = max(1, min(limit, 5000))
        clauses: list[str] = []
        parameters: list[Any] = []
        if states:
            normalized_states = [EventState(value).value for value in states]
            placeholders = ",".join("?" for _ in normalized_states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(normalized_states)
        if sources:
            normalized_sources = [str(value).strip() for value in sources]
            if any(not value for value in normalized_sources):
                raise ValueError("Event source filters cannot be empty")
            placeholders = ",".join("?" for _ in normalized_sources)
            clauses.append(f"source IN ({placeholders})")
            parameters.extend(normalized_sources)
        if event_types:
            normalized_types = [str(value).strip() for value in event_types]
            if any(not value for value in normalized_types):
                raise ValueError("Event type filters cannot be empty")
            placeholders = ",".join("?" for _ in normalized_types)
            clauses.append(f"event_type IN ({placeholders})")
            parameters.extend(normalized_types)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events"
                + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*parameters, bounded),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def replay_dead_letter_event(
        self,
        event_id: str,
        *,
        reason: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Requeue one reviewed dead letter with a fresh retry budget.

        The state predicate is an optimistic fence. Lease fields are cleared
        even though a correctly dead-lettered event should not retain them,
        and the prior failure details remain in the append-only audit record.
        """

        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 2000:
            raise ValueError(
                "Dead-letter replay needs a reason of at most 2000 characters"
            )
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise NotFound(event_id)
            if str(row["state"]) != EventState.DEAD_LETTER.value:
                raise StateConflict(
                    f"Event is {row['state']}, not expected dead_letter"
                )
            cursor = connection.execute(
                """
                UPDATE events
                SET state = ?, available_at = ?, claimed_by = NULL,
                    claim_token = NULL, claim_expires_at = NULL,
                    attempt_count = 0, processed_at = NULL, error = NULL
                WHERE id = ? AND state = ?
                """,
                (
                    EventState.PENDING.value,
                    now,
                    event_id,
                    EventState.DEAD_LETTER.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Event state changed before dead-letter replay")
            self.audit(
                actor,
                "event.dead_letter_replayed",
                entity_type="event",
                entity_id=event_id,
                data={
                    "reason": clean_reason,
                    "prior_state": EventState.DEAD_LETTER.value,
                    "prior_attempt_count": int(row["attempt_count"]),
                    "prior_error": row["error"],
                    "available_at": now,
                },
                connection=connection,
            )
            replayed = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            assert replayed is not None
            return self._event_row(replayed)

    def claim_events(self, worker_id: str, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        claim_token = new_id("claim")
        with self.connection() as connection:
            self._begin_immediate(connection)
            connection.execute(
                """
                UPDATE events
                SET state = ?, claimed_by = NULL, claim_token = NULL, claim_expires_at = NULL
                WHERE state = ? AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                """,
                (EventState.PENDING.value, EventState.PROCESSING.value, now_text),
            )
            first = connection.execute(
                """
                SELECT id, trust_level, source, event_type, attempt_count FROM events
                WHERE state = ? AND available_at <= ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (EventState.PENDING.value, now_text),
            ).fetchone()
            if first is None:
                return []

            # Privileged events and Hermes completion evidence are deliberately
            # leased one at a time. This prevents an LLM plan from attaching an
            # authority-bearing event to work derived from unrelated untrusted
            # content in the same context window. Ordinary inbound events can
            # still be batched for throughput because they grant no transition.
            if first["trust_level"] in {
                TrustLevel.OPERATOR.value,
                TrustLevel.SYSTEM.value,
            } or (
                first["source"] == "hermes"
                and first["event_type"] == "execution.completed"
            ) or int(first["attempt_count"]) > 0:
                rows = [first]
            else:
                rows = connection.execute(
                    """
                    SELECT id FROM events
                    WHERE state = ? AND available_at <= ?
                      AND trust_level NOT IN (?, ?)
                      AND NOT (source = ? AND event_type = ?)
                      AND attempt_count = 0
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        EventState.PENDING.value,
                        now_text,
                        TrustLevel.OPERATOR.value,
                        TrustLevel.SYSTEM.value,
                        "hermes",
                        "execution.completed",
                        limit,
                    ),
                ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE events
                SET state = ?, claimed_by = ?, claim_token = ?, claim_expires_at = ?,
                    attempt_count = attempt_count + 1
                WHERE id IN ({placeholders}) AND state = ?
                """,
                (
                    EventState.PROCESSING.value,
                    worker_id,
                    claim_token,
                    expires,
                    *ids,
                    EventState.PENDING.value,
                ),
            )
            claimed = connection.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders}) "
                "AND claimed_by = ? AND claim_token = ? ORDER BY created_at",
                (*ids, worker_id, claim_token),
            ).fetchall()
            return [self._event_row(row) for row in claimed]

    def has_pending_events(self) -> bool:
        now_text = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM events WHERE state = ? AND available_at <= ? LIMIT 1",
                (EventState.PENDING.value, now_text),
            ).fetchone()
        return row is not None

    def renew_event_claim(
        self,
        event_ids: Sequence[str],
        *,
        claim_token: str,
        lease_seconds: int,
    ) -> None:
        """Renew an owned event lease or fail before applying model output."""

        if not event_ids:
            return
        if not claim_token or lease_seconds < 1:
            raise ValueError("A claim token and positive lease are required")
        expires = (
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat().replace("+00:00", "Z")
        placeholders = ",".join("?" for _ in event_ids)
        with self.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE events SET claim_expires_at = ?
                WHERE id IN ({placeholders}) AND state = ? AND claim_token = ?
                """,
                (
                    expires,
                    *event_ids,
                    EventState.PROCESSING.value,
                    claim_token,
                ),
            )
            if cursor.rowcount != len(event_ids):
                raise StateConflict("Event claim was lost before plan application")

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "trust_level": row["trust_level"],
            "provenance": json.loads(row["provenance_json"] or "{}"),
            "dedupe_key": row["dedupe_key"],
            "state": row["state"],
            "created_at": row["created_at"],
            "available_at": row["available_at"],
            "claimed_by": row["claimed_by"],
            "claim_token": row["claim_token"],
            "claim_expires_at": row["claim_expires_at"],
            "attempt_count": row["attempt_count"],
            "processed_at": row["processed_at"],
            "error": row["error"],
        }

    def complete_events(
        self,
        event_ids: Sequence[str],
        *,
        claim_token: str,
        actor: str = "supervisor",
    ) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE events SET state = ?, processed_at = ?, claimed_by = NULL,
                    claim_token = NULL, claim_expires_at = NULL, error = NULL
                WHERE id IN ({placeholders}) AND state = ? AND claim_token = ?
                """,
                (
                    EventState.PROCESSED.value,
                    utc_now(),
                    *event_ids,
                    EventState.PROCESSING.value,
                    claim_token,
                ),
            )
            if cursor.rowcount != len(event_ids):
                raise StateConflict("Event claim was lost before completion")
            for event_id in event_ids:
                self.audit(
                    actor,
                    "event.processed",
                    entity_type="event",
                    entity_id=event_id,
                    connection=connection,
                )

    def finalize_supervisor_pass(
        self,
        pass_id: str,
        plan_digest: str,
        event_ids: Sequence[str],
        event_dispositions: Sequence[dict[str, Any]],
        *,
        claim_token: str,
        actor: str = "supervisor",
    ) -> None:
        """Atomically finalize a plan and consume its still-owned events.

        Dispatch authorization created during plan application is inert until
        this record exists. A partial or stale plan can therefore leave
        idempotent work records behind, but it cannot release execution.
        """

        if not pass_id or len(plan_digest) != 64:
            raise ValueError("A pass ID and SHA-256 plan digest are required")
        if not event_ids or not claim_token:
            raise ValueError("Finalization requires claimed events")
        disposition_ids = [
            str(disposition.get("event_id", ""))
            for disposition in event_dispositions
        ]
        if (
            len(disposition_ids) != len(event_ids)
            or len(set(disposition_ids)) != len(disposition_ids)
            or set(disposition_ids) != set(map(str, event_ids))
        ):
            raise ValueError(
                "Finalization requires exactly one disposition for every event"
            )
        placeholders = ",".join("?" for _ in event_ids)
        finalized_at = utc_now()
        value = {
            "pass_id": pass_id,
            "plan_digest": plan_digest,
            "event_ids": sorted(map(str, event_ids)),
            "finalized": True,
            "finalized_at": finalized_at,
        }
        with self.connection() as connection:
            self._begin_immediate(connection)
            for disposition in event_dispositions:
                event_id = str(disposition["event_id"])
                disposition_value = str(disposition.get("disposition", "")).strip()
                reason = str(disposition.get("reason", "")).strip()
                if not disposition_value or not reason:
                    raise ValueError("Event dispositions need a type and reason")
                try:
                    connection.execute(
                        """
                        INSERT INTO event_dispositions(
                            event_id, supervisor_pass_id, plan_digest, disposition,
                            reason, related_work_ids_json,
                            related_question_ids_json, metadata_json, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            pass_id,
                            plan_digest,
                            disposition_value,
                            reason[:4000],
                            self._json(disposition.get("related_work_ids", [])),
                            self._json(
                                disposition.get("related_question_ids", [])
                            ),
                            self._json(disposition.get("metadata", {})),
                            finalized_at,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    existing = connection.execute(
                        "SELECT supervisor_pass_id, plan_digest, disposition, reason "
                        "FROM event_dispositions WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if not (
                        existing
                        and existing["supervisor_pass_id"] == pass_id
                        and existing["plan_digest"] == plan_digest
                        and existing["disposition"] == disposition_value
                        and existing["reason"] == reason[:4000]
                    ):
                        raise StateConflict(
                            f"Event already has a different disposition: {event_id}"
                        ) from error
            cursor = connection.execute(
                f"""
                UPDATE events SET state = ?, processed_at = ?, claimed_by = NULL,
                    claim_token = NULL, claim_expires_at = NULL, error = NULL
                WHERE id IN ({placeholders}) AND state = ? AND claim_token = ?
                """,
                (
                    EventState.PROCESSED.value,
                    finalized_at,
                    *event_ids,
                    EventState.PROCESSING.value,
                    claim_token,
                ),
            )
            if cursor.rowcount != len(event_ids):
                raise StateConflict("Event claim was lost before pass finalization")
            connection.execute(
                """
                INSERT INTO system_state(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (
                    f"supervisor.pass:{pass_id}",
                    self._json(value),
                    finalized_at,
                ),
            )
            for event_id in event_ids:
                disposition = next(
                    item
                    for item in event_dispositions
                    if str(item["event_id"]) == str(event_id)
                )
                self.audit(
                    actor,
                    "event.processed",
                    entity_type="event",
                    entity_id=str(event_id),
                    data={
                        "disposition": disposition["disposition"],
                        "reason": disposition["reason"],
                        "related_work_ids": disposition.get(
                            "related_work_ids", []
                        ),
                        "related_question_ids": disposition.get(
                            "related_question_ids", []
                        ),
                    },
                    connection=connection,
                )
            self.audit(
                actor,
                "supervisor.pass_finalized",
                entity_type="supervisor_pass",
                entity_id=pass_id,
                data=value,
                connection=connection,
            )

    def fail_events(
        self,
        event_ids: Sequence[str],
        error: str,
        *,
        max_attempts: int,
        retry_delay_seconds: int = 30,
        actor: str = "supervisor",
        claim_token: str,
    ) -> None:
        if not event_ids:
            return
        with self.transaction() as connection:
            for event_id in event_ids:
                row = connection.execute(
                    "SELECT attempt_count FROM events "
                    "WHERE id = ? AND state = ? AND claim_token = ?",
                    (event_id, EventState.PROCESSING.value, claim_token),
                ).fetchone()
                if row is None:
                    raise StateConflict("Event claim was lost before failure handling")
                state = (
                    EventState.DEAD_LETTER.value
                    if int(row["attempt_count"]) >= max_attempts
                    else EventState.PENDING.value
                )
                # Each failed claim increases the recovery delay while keeping
                # the bound short enough for an operator to intervene quickly.
                delay = min(
                    retry_delay_seconds * (2 ** max(int(row["attempt_count"]) - 1, 0)),
                    3600,
                )
                available = (
                    datetime.now(UTC) + timedelta(seconds=delay)
                ).isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    UPDATE events SET state = ?, error = ?, available_at = ?,
                        claimed_by = NULL, claim_token = NULL, claim_expires_at = NULL
                    WHERE id = ? AND state = ? AND claim_token = ?
                    """,
                    (
                        state,
                        error[:4000],
                        available,
                        event_id,
                        EventState.PROCESSING.value,
                        claim_token,
                    ),
                )
                self.audit(
                    actor,
                    "event.failed",
                    entity_type="event",
                    entity_id=event_id,
                    data={"state": state, "error": error[:500]},
                    connection=connection,
                )

    def get_event_disposition(self, event_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_dispositions WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "supervisor_pass_id": row["supervisor_pass_id"],
            "plan_digest": row["plan_digest"],
            "disposition": row["disposition"],
            "reason": row["reason"],
            "related_work_ids": json.loads(row["related_work_ids_json"] or "[]"),
            "related_question_ids": json.loads(
                row["related_question_ids_json"] or "[]"
            ),
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }

    def _refresh_rollup_chain(
        self,
        connection: sqlite3.Connection,
        parent_ids: Sequence[str | None],
    ) -> None:
        """Refresh durable descendant rollups without changing work versions.

        Rollups are derived state. Keeping them outside ``work_items`` avoids
        invalidating optimistic versions while a supervisor pass is applying
        several related hierarchy operations.
        """

        roots = sorted({str(value) for value in parent_ids if value})
        if not roots:
            return
        placeholders = ",".join("?" for _ in roots)
        rows = connection.execute(
            f"""
            WITH RECURSIVE ancestors(id, parent_id) AS (
                SELECT id, parent_id FROM work_items
                WHERE id IN ({placeholders})
                UNION
                SELECT parent.id, parent.parent_id
                FROM work_items parent
                JOIN ancestors child ON parent.id = child.parent_id
            )
            SELECT DISTINCT id FROM ancestors
            """,
            roots,
        ).fetchall()
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        terminal = {
            WorkStatus.DONE.value,
            WorkStatus.CANCELLED.value,
            WorkStatus.ARCHIVED.value,
        }
        for row in rows:
            work_id = str(row["id"])
            descendants = connection.execute(
                """
                WITH RECURSIVE descendants(
                    id, status, due_at, parent_id, depth
                ) AS (
                    SELECT id, status, due_at, parent_id, 1
                    FROM work_items WHERE parent_id = ?
                    UNION ALL
                    SELECT child.id, child.status, child.due_at,
                           child.parent_id, descendants.depth + 1
                    FROM work_items child
                    JOIN descendants ON child.parent_id = descendants.id
                )
                SELECT id, status, due_at, depth FROM descendants
                """,
                (work_id,),
            ).fetchall()
            status_counts: dict[str, int] = {}
            direct_child_count = 0
            overdue_count = 0
            for descendant in descendants:
                status = str(descendant["status"])
                status_counts[status] = status_counts.get(status, 0) + 1
                if int(descendant["depth"]) == 1:
                    direct_child_count += 1
                due_at = descendant["due_at"]
                if due_at and status not in terminal:
                    try:
                        due = datetime.fromisoformat(
                            str(due_at).replace("Z", "+00:00")
                        )
                    except ValueError:
                        due = None
                    if due is not None and due.astimezone(UTC) < now:
                        overdue_count += 1
            descendant_count = len(descendants)
            terminal_count = sum(
                status_counts.get(status, 0) for status in terminal
            )
            done_count = status_counts.get(WorkStatus.DONE.value, 0)
            running_count = (
                status_counts.get(WorkStatus.RUNNING.value, 0)
                + status_counts.get(WorkStatus.REVIEW.value, 0)
            )
            blocked_count = status_counts.get(WorkStatus.BLOCKED.value, 0)
            waiting_count = status_counts.get(
                WorkStatus.WAITING_INPUT.value, 0
            )
            progress = (
                terminal_count / descendant_count if descendant_count else 0.0
            )
            if descendant_count == 0:
                health = "empty"
            elif terminal_count == descendant_count:
                health = "complete"
            elif blocked_count:
                health = "blocked"
            elif waiting_count:
                health = "waiting_input"
            elif overdue_count:
                health = "at_risk"
            elif running_count:
                health = "active"
            else:
                health = "on_track"
            connection.execute(
                """
                INSERT INTO work_rollups(
                    work_item_id, direct_child_count, descendant_count,
                    terminal_count, done_count, running_count, blocked_count,
                    waiting_input_count, overdue_count, progress, health,
                    status_counts_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    direct_child_count=excluded.direct_child_count,
                    descendant_count=excluded.descendant_count,
                    terminal_count=excluded.terminal_count,
                    done_count=excluded.done_count,
                    running_count=excluded.running_count,
                    blocked_count=excluded.blocked_count,
                    waiting_input_count=excluded.waiting_input_count,
                    overdue_count=excluded.overdue_count,
                    progress=excluded.progress,
                    health=excluded.health,
                    status_counts_json=excluded.status_counts_json,
                    updated_at=excluded.updated_at
                """,
                (
                    work_id,
                    direct_child_count,
                    descendant_count,
                    terminal_count,
                    done_count,
                    running_count,
                    blocked_count,
                    waiting_count,
                    overdue_count,
                    progress,
                    health,
                    self._json(status_counts),
                    now_text,
                ),
            )

    def get_work_rollups(
        self, work_ids: Sequence[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        clauses = ""
        params: list[Any] = []
        if work_ids is not None:
            ids = list(dict.fromkeys(map(str, work_ids)))
            if not ids:
                return {}
            clauses = f"WHERE work_item_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM work_rollups {clauses}", params
            ).fetchall()
        return {
            str(row["work_item_id"]): {
                "direct_child_count": int(row["direct_child_count"]),
                "descendant_count": int(row["descendant_count"]),
                "terminal_count": int(row["terminal_count"]),
                "done_count": int(row["done_count"]),
                "running_count": int(row["running_count"]),
                "blocked_count": int(row["blocked_count"]),
                "waiting_input_count": int(row["waiting_input_count"]),
                "overdue_count": int(row["overdue_count"]),
                "progress": float(row["progress"]),
                "health": row["health"],
                "status_counts": json.loads(row["status_counts_json"] or "{}"),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def create_work(self, item: WorkItem, *, actor: str = "supervisor") -> WorkItem:
        if not item.title.strip():
            raise ValueError("Work title cannot be empty")
        if len(item.title) > 500 or len(item.description) > 100_000:
            raise ValueError("Work title or description is too long")
        if item.parent_id == item.id:
            raise ValueError("Work item cannot be its own parent")
        self._validate_factors(item)
        item.due_at = self._validated_timestamp(item.due_at, "due_at")
        item.scheduled_at = self._validated_timestamp(item.scheduled_at, "scheduled_at")
        item.recurrence_rule = normalize_recurrence_rule(item.recurrence_rule)
        self._validate_reminder_lifecycle(item)
        self._validated_criteria(item.acceptance_criteria)
        self._validated_metadata(item.metadata)
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO work_items(
                    id, kind, title, description, status, parent_id, source_event_id,
                    provenance_json, priority, priority_score, priority_rationale,
                    impact, urgency, strategic_alignment, unlock_value, risk, confidence,
                    effort_minutes, due_at, scheduled_at, recurrence_rule,
                    reminder_last_delivered_at, reminder_last_acknowledged_at,
                    reminder_delivery_count, assignee, execution_mode,
                    hermes_task_id, acceptance_criteria_json, metadata_json, version,
                    created_at, updated_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.kind.value,
                    item.title.strip(),
                    item.description,
                    item.status.value,
                    item.parent_id,
                    item.source_event_id,
                    self._json(item.provenance),
                    item.priority,
                    item.priority_score,
                    item.priority_rationale,
                    item.impact,
                    item.urgency,
                    item.strategic_alignment,
                    item.unlock_value,
                    item.risk,
                    item.confidence,
                    item.effort_minutes,
                    item.due_at,
                    item.scheduled_at,
                    item.recurrence_rule,
                    item.reminder_last_delivered_at,
                    item.reminder_last_acknowledged_at,
                    item.reminder_delivery_count,
                    item.assignee,
                    item.execution_mode.value,
                    item.hermes_task_id,
                    self._json(item.acceptance_criteria),
                    self._json(item.metadata),
                    item.version,
                    item.created_at,
                    item.updated_at,
                    item.completed_at,
                ),
            )
            self.audit(
                actor,
                "work.created",
                entity_type="work",
                entity_id=item.id,
                data={"kind": item.kind.value, "status": item.status.value, "title": item.title},
                connection=connection,
            )
            self._refresh_rollup_chain(connection, [item.parent_id])
        return item

    @staticmethod
    def _validate_reminder_lifecycle(item: WorkItem) -> None:
        if item.recurrence_rule is not None:
            if item.kind != WorkKind.REMINDER:
                raise ValueError("recurrence_rule is supported only for reminder work")
            if item.due_at is None:
                raise ValueError("A recurring reminder requires due_at")
        if item.reminder_delivery_count < 0:
            raise ValueError("reminder_delivery_count cannot be negative")
        for field_name in (
            "reminder_last_delivered_at",
            "reminder_last_acknowledged_at",
        ):
            value = getattr(item, field_name)
            if value is not None:
                setattr(item, field_name, SQLiteStore._validated_timestamp(value, field_name))

    @staticmethod
    def _validate_factors(item: WorkItem) -> None:
        for name in ("impact", "urgency", "strategic_alignment", "unlock_value", "risk", "confidence"):
            value = float(getattr(item, name))
            if not math.isfinite(value) or value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if item.effort_minutes < 0:
            raise ValueError("effort_minutes cannot be negative")

    @staticmethod
    def _validated_timestamp(value: str | None, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be an ISO-8601 timestamp or null")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return value

    @staticmethod
    def _scheduled_time_is_due(value: str | None, now: datetime) -> bool:
        if value is None:
            return True
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return False
        return parsed.astimezone(UTC) <= now.astimezone(UTC)

    @classmethod
    def _validated_criteria(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("acceptance_criteria must be a list")
        if len(value) > 100:
            raise ValueError("acceptance_criteria contains too many entries")
        if not all(isinstance(item, str) and item.strip() and len(item) <= 2000 for item in value):
            raise ValueError("acceptance_criteria entries must be nonempty strings")
        return value

    @classmethod
    def _validated_metadata(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        encoded = cls._json(value)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("metadata is too large")
        return value

    def get_work(self, work_id: str) -> WorkItem:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM work_items WHERE id = ?", (work_id,)).fetchone()
        if row is None:
            raise NotFound(work_id)
        return WorkItem.from_row(row)

    def find_work_by_hermes_id(self, hermes_task_id: str) -> WorkItem | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE hermes_task_id = ?", (hermes_task_id,)
            ).fetchone()
        return WorkItem.from_row(row) if row else None

    def list_work(
        self,
        *,
        statuses: Sequence[WorkStatus | str] | None = None,
        kinds: Sequence[WorkKind | str] | None = None,
        parent_id: str | None = None,
        dependencies_satisfied_only: bool = False,
        running_bypasses_dependencies: bool = False,
        limit: int = 200,
        order_by: str = "priority",
    ) -> list[WorkItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            values = [status.value if isinstance(status, WorkStatus) else status for status in statuses]
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if kinds:
            values = [kind.value if isinstance(kind, WorkKind) else kind for kind in kinds]
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            params.append(parent_id)
        if dependencies_satisfied_only:
            eligibility = """
                NOT EXISTS (
                    SELECT 1
                    FROM (
                        SELECT to_id AS dependency_id
                        FROM work_links
                        WHERE from_id = work_items.id AND relation = ?
                        UNION ALL
                        SELECT from_id AS dependency_id
                        FROM work_links
                        WHERE to_id = work_items.id AND relation = ?
                    ) dependency_link
                    JOIN work_items dependency
                      ON dependency.id = dependency_link.dependency_id
                    WHERE dependency.status <> ?
                )
            """
            if running_bypasses_dependencies:
                eligibility = f"(status = ? OR ({eligibility}))"
                params.append(WorkStatus.RUNNING.value)
            else:
                eligibility = f"({eligibility})"
            params.extend(
                [
                    WorkRelation.DEPENDS_ON.value,
                    WorkRelation.BLOCKS.value,
                    WorkStatus.DONE.value,
                ]
            )
            clauses.append(eligibility)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        orders = {
            "priority": "priority_score DESC, priority DESC, created_at ASC",
            "created": "created_at DESC",
            "updated": "updated_at DESC",
            "due": "due_at IS NULL, due_at ASC, priority_score DESC",
        }
        order = orders.get(order_by, orders["priority"])
        params.append(max(1, min(limit, 5000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM work_items {where} ORDER BY {order} LIMIT ?", params
            ).fetchall()
        return [WorkItem.from_row(row) for row in rows]

    def update_work(
        self,
        work_id: str,
        changes: dict[str, Any],
        *,
        actor: str = "supervisor",
        expected_version: int | None = None,
        allow_transition_override: bool = False,
    ) -> WorkItem:
        allowed = {
            "title",
            "description",
            "status",
            "parent_id",
            "priority",
            "priority_score",
            "priority_rationale",
            "impact",
            "urgency",
            "strategic_alignment",
            "unlock_value",
            "risk",
            "confidence",
            "effort_minutes",
            "due_at",
            "scheduled_at",
            "recurrence_rule",
            "reminder_last_delivered_at",
            "reminder_last_acknowledged_at",
            "reminder_delivery_count",
            "assignee",
            "execution_mode",
            "hermes_task_id",
            "acceptance_criteria",
            "metadata",
            "completed_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported work fields: {sorted(unknown)}")
        current = self.get_work(work_id)
        normalized = dict(changes)
        acknowledged_recurring = False
        if "status" in normalized:
            target = WorkStatus(normalized["status"])
            if not allow_transition_override and target != current.status:
                if target not in ALLOWED_WORK_TRANSITIONS[current.status]:
                    raise StateConflict(f"Cannot transition {current.status.value} to {target.value}")
            normalized["status"] = target.value
            if target == WorkStatus.DONE and "completed_at" not in normalized:
                normalized["completed_at"] = utc_now()
            elif target != WorkStatus.DONE and current.status == WorkStatus.DONE:
                normalized.setdefault("completed_at", None)
        if "execution_mode" in normalized:
            normalized["execution_mode"] = ExecutionMode(normalized["execution_mode"]).value
        if "acceptance_criteria" in normalized:
            criteria = self._validated_criteria(normalized.pop("acceptance_criteria"))
            normalized["acceptance_criteria_json"] = self._json(criteria)
        if "metadata" in normalized:
            metadata = self._validated_metadata(normalized.pop("metadata"))
            normalized["metadata_json"] = self._json(metadata)
        if "recurrence_rule" in normalized:
            normalized["recurrence_rule"] = normalize_recurrence_rule(
                normalized["recurrence_rule"]
            )
        for timestamp in ("due_at", "scheduled_at"):
            if timestamp in normalized:
                normalized[timestamp] = self._validated_timestamp(
                    normalized[timestamp], timestamp
                )
        for timestamp in (
            "reminder_last_delivered_at",
            "reminder_last_acknowledged_at",
        ):
            if timestamp in normalized:
                normalized[timestamp] = self._validated_timestamp(
                    normalized[timestamp], timestamp
                )
        recurrence = normalized.get("recurrence_rule", current.recurrence_rule)
        effective_due = normalized.get("due_at", current.due_at)
        if recurrence is not None:
            if current.kind != WorkKind.REMINDER:
                raise ValueError("recurrence_rule is supported only for reminder work")
            if effective_due is None:
                raise ValueError("A recurring reminder requires due_at")
        if "reminder_delivery_count" in normalized and (
            isinstance(normalized["reminder_delivery_count"], bool)
            or not isinstance(normalized["reminder_delivery_count"], int)
            or normalized["reminder_delivery_count"] < 0
        ):
            raise ValueError("reminder_delivery_count must be a nonnegative integer")
        if (
            current.kind == WorkKind.REMINDER
            and recurrence is not None
            and normalized.get("status") == WorkStatus.DONE.value
        ):
            acknowledged_at = utc_now()
            normalized["status"] = WorkStatus.READY.value
            normalized["due_at"] = next_recurrence_due(
                str(effective_due),
                recurrence,
                after=datetime.fromisoformat(acknowledged_at.replace("Z", "+00:00")),
            )
            normalized["completed_at"] = None
            normalized["reminder_last_acknowledged_at"] = acknowledged_at
            normalized["reminder_last_delivered_at"] = None
            acknowledged_recurring = True
        elif current.kind == WorkKind.REMINDER and (
            "due_at" in normalized or "recurrence_rule" in normalized
        ):
            # A changed occurrence is immediately eligible at its new due time.
            normalized["reminder_last_delivered_at"] = None
        if "title" in normalized:
            if not isinstance(normalized["title"], str) or not normalized["title"].strip():
                raise ValueError("Work title cannot be empty")
            if len(normalized["title"]) > 500:
                raise ValueError("Work title is too long")
        if "description" in normalized and (
            not isinstance(normalized["description"], str)
            or len(normalized["description"]) > 100_000
        ):
            raise ValueError("Work description is invalid or too long")
        if "effort_minutes" in normalized and int(normalized["effort_minutes"]) < 0:
            raise ValueError("effort_minutes cannot be negative")
        for factor in ("impact", "urgency", "strategic_alignment", "unlock_value", "risk", "confidence"):
            if factor in normalized:
                value = float(normalized[factor])
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"{factor} must be between 0 and 1")
        normalized["updated_at"] = utc_now()
        assignments = ", ".join(f"{field} = ?" for field in normalized)
        params = list(normalized.values())
        if expected_version is not None and expected_version != current.version:
            raise StateConflict(f"Work item changed concurrently: {work_id}")
        where = "id = ? AND version = ?"
        params.extend((work_id, current.version))
        with self.connection() as connection:
            self._begin_immediate(connection)
            if "parent_id" in changes:
                self._validate_parent_change(
                    connection, work_id, changes["parent_id"]
                )
            live = connection.execute(
                "SELECT version FROM work_items WHERE id = ?", (work_id,)
            ).fetchone()
            if live is None:
                raise NotFound(work_id)
            if int(live["version"]) != current.version:
                raise StateConflict(f"Work item changed concurrently: {work_id}")
            queued = connection.execute(
                "SELECT id FROM runs WHERE work_item_id = ? AND status = 'queued' LIMIT 1",
                (work_id,),
            ).fetchone()
            if queued is not None and changes:
                raise StateConflict(
                    f"Work item has an active dispatch reservation: {work_id}"
                )
            if (
                current.status == WorkStatus.DONE
                and normalized.get("status") not in (None, WorkStatus.DONE.value)
            ):
                active_dependent = connection.execute(
                    """
                    SELECT 1
                    FROM (
                        SELECT from_id AS dependent_id
                        FROM work_links
                        WHERE to_id = ? AND relation = ?
                        UNION ALL
                        SELECT to_id AS dependent_id
                        FROM work_links
                        WHERE from_id = ? AND relation = ?
                    ) dependent_link
                    JOIN runs run ON run.work_item_id = dependent_link.dependent_id
                    WHERE run.status IN (
                          'queued', 'running', 'cancel_requested', 'lost',
                          'legacy_conflict'
                      )
                    LIMIT 1
                    """,
                    (
                        work_id,
                        WorkRelation.DEPENDS_ON.value,
                        work_id,
                        WorkRelation.BLOCKS.value,
                    ),
                ).fetchone()
                if active_dependent is not None:
                    raise StateConflict(
                        "A completed dependency cannot reopen while dependent work has an active run"
                    )
            cursor = connection.execute(
                f"UPDATE work_items SET {assignments}, version = version + 1 WHERE {where}", params
            )
            if cursor.rowcount != 1:
                raise StateConflict(f"Work item changed concurrently: {work_id}")
            self.audit(
                actor,
                "work.updated",
                entity_type="work",
                entity_id=work_id,
                data={"changes": changes, "from_version": current.version},
                connection=connection,
            )
            if acknowledged_recurring:
                self.audit(
                    actor,
                    "reminder.recurrence_advanced",
                    entity_type="work",
                    entity_id=work_id,
                    data={
                        "previous_due_at": current.due_at,
                        "next_due_at": normalized["due_at"],
                        "recurrence_rule": recurrence,
                    },
                    connection=connection,
                )
            if set(changes) & {"status", "parent_id", "due_at"}:
                self._refresh_rollup_chain(
                    connection,
                    [current.parent_id, normalized.get("parent_id", current.parent_id)],
                )
        return self.get_work(work_id)

    def _validate_parent_change(
        self,
        connection: sqlite3.Connection,
        work_id: str,
        parent_id: str | None,
    ) -> None:
        if parent_id is None:
            return
        if parent_id == work_id:
            raise StateConflict("Work item cannot be its own parent")
        parent = connection.execute(
            "SELECT id FROM work_items WHERE id = ?", (parent_id,)
        ).fetchone()
        if parent is None:
            raise NotFound(parent_id)
        cycle = connection.execute(
            """
            WITH RECURSIVE ancestors(id, parent_id) AS (
                SELECT id, parent_id FROM work_items WHERE id = ?
                UNION ALL
                SELECT item.id, item.parent_id
                FROM work_items item
                JOIN ancestors ON item.id = ancestors.parent_id
            )
            SELECT 1 FROM ancestors WHERE id = ? LIMIT 1
            """,
            (parent_id, work_id),
        ).fetchone()
        if cycle is not None:
            raise StateConflict("Work hierarchy cannot contain a cycle")

    @staticmethod
    def _attention_delivery_eligible(
        last_delivered_at: str | None,
        *,
        now: datetime,
        redelivery_seconds: int,
    ) -> bool:
        if last_delivered_at is None:
            return True
        try:
            delivered = datetime.fromisoformat(
                str(last_delivered_at).replace("Z", "+00:00")
            )
        except ValueError:
            return True
        if delivered.tzinfo is None or delivered.utcoffset() is None:
            return True
        return delivered.astimezone(UTC) + timedelta(seconds=redelivery_seconds) <= now

    def claim_attention(
        self,
        *,
        reminder_limit: int = 20,
        question_limit: int = 20,
        redelivery_seconds: int = 3600,
        now: datetime | None = None,
        actor: str = "hermes-cron",
    ) -> dict[str, Any]:
        """Atomically claim reminders and questions for a native Cron delivery.

        Claiming records a durable delivery attempt. A later poll cannot return
        the same attention item until the configured redelivery window elapses.
        This is a delivery lease, not a scheduler; Hermes Cron remains the sole
        mechanism that decides when to poll.
        """

        if (
            isinstance(redelivery_seconds, bool)
            or not isinstance(redelivery_seconds, int)
            or redelivery_seconds < 1
        ):
            raise ValueError("redelivery_seconds must be a positive integer")
        reminder_limit = max(0, min(int(reminder_limit), 100))
        question_limit = max(0, min(int(question_limit), 100))
        claimed_at = now or datetime.now(UTC)
        if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
            raise ValueError("Attention claim time must include a timezone")
        claimed_at = claimed_at.astimezone(UTC)
        claimed_text = claimed_at.isoformat().replace("+00:00", "Z")
        reminders: list[WorkItem] = []
        questions: list[dict[str, Any]] = []
        with self.transaction() as connection:
            if reminder_limit:
                candidates = connection.execute(
                    "SELECT * FROM work_items WHERE kind = ? "
                    "AND status NOT IN (?, ?, ?) "
                    "AND COALESCE(due_at, scheduled_at) IS NOT NULL "
                    "ORDER BY priority_score DESC, priority DESC, created_at ASC "
                    "LIMIT 5000",
                    (
                        WorkKind.REMINDER.value,
                        WorkStatus.DONE.value,
                        WorkStatus.CANCELLED.value,
                        WorkStatus.ARCHIVED.value,
                    ),
                ).fetchall()
                due_candidates: list[tuple[datetime, sqlite3.Row]] = []
                for row in candidates:
                    raw_due = row["due_at"] or row["scheduled_at"]
                    try:
                        due = datetime.fromisoformat(
                            str(raw_due).replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                    if due.tzinfo is None or due.utcoffset() is None:
                        continue
                    due = due.astimezone(UTC)
                    if due > claimed_at or not self._attention_delivery_eligible(
                        row["reminder_last_delivered_at"],
                        now=claimed_at,
                        redelivery_seconds=redelivery_seconds,
                    ):
                        continue
                    due_candidates.append((due, row))
                due_candidates.sort(
                    key=lambda pair: (
                        pair[0],
                        -float(pair[1]["priority_score"]),
                        str(pair[1]["id"]),
                    )
                )
                for _, row in due_candidates[:reminder_limit]:
                    cursor = connection.execute(
                        "UPDATE work_items SET reminder_last_delivered_at = ?, "
                        "reminder_delivery_count = reminder_delivery_count + 1, "
                        "updated_at = ?, version = version + 1 "
                        "WHERE id = ? AND version = ?",
                        (claimed_text, claimed_text, row["id"], row["version"]),
                    )
                    if cursor.rowcount != 1:
                        raise StateConflict(
                            f"Reminder changed during attention claim: {row['id']}"
                        )
                    updated = connection.execute(
                        "SELECT * FROM work_items WHERE id = ?", (row["id"],)
                    ).fetchone()
                    assert updated is not None
                    reminders.append(WorkItem.from_row(updated))
            if question_limit:
                question_rows = connection.execute(
                    "SELECT * FROM user_questions WHERE status = ? "
                    "ORDER BY urgency DESC, created_at ASC LIMIT 5000",
                    (QuestionStatus.PENDING.value,),
                ).fetchall()
                for row in question_rows:
                    if len(questions) >= question_limit:
                        break
                    if not self._attention_delivery_eligible(
                        row["last_delivered_at"],
                        now=claimed_at,
                        redelivery_seconds=redelivery_seconds,
                    ):
                        continue
                    cursor = connection.execute(
                        "UPDATE user_questions SET last_delivered_at = ?, "
                        "delivery_count = delivery_count + 1 "
                        "WHERE id = ? AND status = ?",
                        (
                            claimed_text,
                            row["id"],
                            QuestionStatus.PENDING.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise StateConflict(
                            f"Question changed during attention claim: {row['id']}"
                        )
                    updated = connection.execute(
                        "SELECT * FROM user_questions WHERE id = ?", (row["id"],)
                    ).fetchone()
                    assert updated is not None
                    questions.append(self._question_row(updated))
            if reminders or questions:
                self.audit(
                    actor,
                    "attention.claimed",
                    entity_type="attention",
                    data={
                        "reminder_ids": [item.id for item in reminders],
                        "question_ids": [item["id"] for item in questions],
                        "redelivery_seconds": redelivery_seconds,
                    },
                    connection=connection,
                )
        return {
            "reminders": reminders,
            "questions": questions,
            "claimed_at": claimed_text,
            "redelivery_seconds": redelivery_seconds,
        }

    def claim_due_reminders(
        self,
        *,
        limit: int = 20,
        redelivery_seconds: int = 3600,
        now: datetime | None = None,
        actor: str = "hermes-cron",
    ) -> list[WorkItem]:
        return self.claim_attention(
            reminder_limit=limit,
            question_limit=0,
            redelivery_seconds=redelivery_seconds,
            now=now,
            actor=actor,
        )["reminders"]

    def resolve_reminder(
        self,
        work_id: str,
        *,
        action: str,
        expected_version: int,
        until: str | None = None,
        actor: str = "operator",
    ) -> WorkItem:
        item = self.get_work(work_id)
        if item.kind != WorkKind.REMINDER:
            raise StateConflict(f"Work item is not a reminder: {work_id}")
        if item.status in TERMINAL_WORK_STATUSES:
            raise StateConflict(f"Reminder is already {item.status.value}")
        if action == "snooze":
            if until is None:
                raise ValueError("Snooze action requires until")
            snooze_until = self._validated_timestamp(until, "until")
            assert snooze_until is not None
            parsed = datetime.fromisoformat(snooze_until.replace("Z", "+00:00"))
            if parsed.astimezone(UTC) <= datetime.now(UTC):
                raise ValueError("Snooze time must be in the future")
            return self.update_work(
                work_id,
                {"due_at": snooze_until},
                actor=actor,
                expected_version=expected_version,
                allow_transition_override=True,
            )
        if action not in {"acknowledge", "complete"}:
            raise ValueError("Reminder action must be snooze, acknowledge, or complete")
        return self.update_work(
            work_id,
            {
                "status": WorkStatus.DONE.value,
                "reminder_last_acknowledged_at": utc_now(),
            },
            actor=actor,
            expected_version=expected_version,
            allow_transition_override=True,
        )

    def add_work_link(
        self,
        from_id: str,
        to_id: str,
        relation: WorkRelation | str,
        *,
        actor: str = "supervisor",
        expected_from_version: int | None = None,
        expected_to_version: int | None = None,
    ) -> str:
        relation_value = WorkRelation(relation).value
        if from_id == to_id:
            raise StateConflict("Work item cannot link to itself")
        link_id = new_id("lnk")
        with self.connection() as connection:
            self._begin_immediate(connection)
            versions = {
                str(row["id"]): int(row["version"])
                for row in connection.execute(
                    "SELECT id, version FROM work_items WHERE id IN (?, ?)",
                    (from_id, to_id),
                ).fetchall()
            }
            if from_id not in versions:
                raise NotFound(from_id)
            if to_id not in versions:
                raise NotFound(to_id)
            if (
                expected_from_version is not None
                and versions[from_id] != expected_from_version
            ):
                raise StateConflict(f"Work item changed concurrently: {from_id}")
            if (
                expected_to_version is not None
                and versions[to_id] != expected_to_version
            ):
                raise StateConflict(f"Work item changed concurrently: {to_id}")
            if relation_value in {
                WorkRelation.DEPENDS_ON.value,
                WorkRelation.BLOCKS.value,
            }:
                active_run = connection.execute(
                    """
                    SELECT 1 FROM runs
                    WHERE status IN (
                        'queued', 'running', 'cancel_requested', 'lost',
                        'legacy_conflict'
                    ) AND work_item_id IN (?, ?)
                    LIMIT 1
                    """,
                    (from_id, to_id),
                ).fetchone()
                if active_run is not None:
                    raise StateConflict(
                        "Dependency graph cannot change while either work item has an active run"
                    )
            if relation_value in {
                WorkRelation.DEPENDS_ON.value,
                WorkRelation.BLOCKS.value,
            }:
                dependent_id, dependency_id = (
                    (from_id, to_id)
                    if relation_value == WorkRelation.DEPENDS_ON.value
                    else (to_id, from_id)
                )
                cycle = connection.execute(
                    """
                    WITH RECURSIVE dependency_edges(dependent_id, dependency_id) AS (
                        SELECT from_id, to_id FROM work_links WHERE relation = ?
                        UNION
                        SELECT to_id, from_id FROM work_links WHERE relation = ?
                    ), dependencies(id) AS (
                        SELECT dependency_id FROM dependency_edges
                        WHERE dependent_id = ?
                        UNION
                        SELECT edge.dependency_id
                        FROM dependency_edges edge
                        JOIN dependencies dependency
                          ON edge.dependent_id = dependency.id
                    )
                    SELECT 1 FROM dependencies WHERE id = ? LIMIT 1
                    """,
                    (
                        WorkRelation.DEPENDS_ON.value,
                        WorkRelation.BLOCKS.value,
                        dependency_id,
                        dependent_id,
                    ),
                ).fetchone()
                if cycle is not None:
                    raise StateConflict("Dependency graph cannot contain a cycle")
            connection.execute(
                "INSERT OR IGNORE INTO work_links(id, from_id, to_id, relation, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (link_id, from_id, to_id, relation_value, utc_now()),
            )
            row = connection.execute(
                "SELECT id FROM work_links WHERE from_id = ? AND to_id = ? AND relation = ?",
                (from_id, to_id, relation_value),
            ).fetchone()
            assert row is not None
            link_id = str(row["id"])
            self.audit(
                actor,
                "work.linked",
                entity_type="work",
                entity_id=from_id,
                data={"to_id": to_id, "relation": relation_value},
                connection=connection,
            )
        return link_id

    def dependencies_satisfied(self, work_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS outstanding
                FROM (
                    SELECT to_id AS dependency_id
                    FROM work_links
                    WHERE from_id = ? AND relation = ?
                    UNION ALL
                    SELECT from_id AS dependency_id
                    FROM work_links
                    WHERE to_id = ? AND relation = ?
                ) dependency_link
                JOIN work_items dependency
                  ON dependency.id = dependency_link.dependency_id
                WHERE dependency.status <> ?
                """,
                (
                    work_id,
                    WorkRelation.DEPENDS_ON.value,
                    work_id,
                    WorkRelation.BLOCKS.value,
                    WorkStatus.DONE.value,
                ),
            ).fetchone()
        return bool(row and int(row["outstanding"]) == 0)

    def update_priority(self, work_id: str, score: float, rationale: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE work_items SET priority_score = ?, priority_rationale = ?, "
                "updated_at = ?, version = version + 1 "
                "WHERE id = ? AND (priority_score <> ? OR priority_rationale <> ?) "
                "AND NOT EXISTS (SELECT 1 FROM runs WHERE work_item_id = ? "
                "AND status = 'queued')",
                (score, rationale, utc_now(), work_id, score, rationale, work_id),
            )

    def create_question(self, question: UserQuestion, *, actor: str = "supervisor") -> UserQuestion:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO user_questions(
                    id, question, context, urgency, blocking_work_ids_json,
                    status, answer, created_at, answered_at,
                    last_delivered_at, delivery_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question.id,
                    question.question,
                    question.context,
                    question.urgency,
                    self._json(question.blocking_work_ids),
                    question.status.value,
                    question.answer,
                    question.created_at,
                    question.answered_at,
                    question.last_delivered_at,
                    question.delivery_count,
                ),
            )
            self.audit(
                actor,
                "question.created",
                entity_type="question",
                entity_id=question.id,
                data={"blocking_work_ids": question.blocking_work_ids},
                connection=connection,
            )
        return question

    @staticmethod
    def _question_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "question": row["question"],
            "context": row["context"],
            "urgency": row["urgency"],
            "blocking_work_ids": json.loads(row["blocking_work_ids_json"]),
            "status": row["status"],
            "answer": row["answer"],
            "created_at": row["created_at"],
            "answered_at": row["answered_at"],
            "last_delivered_at": row["last_delivered_at"],
            "delivery_count": int(row["delivery_count"]),
        }

    def list_questions(
        self,
        status: QuestionStatus | str | None = QuestionStatus.PENDING,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = None if limit is None else max(1, min(int(limit), 5000))
        with self.connection() as connection:
            if status is None:
                sql = "SELECT * FROM user_questions ORDER BY urgency DESC, created_at ASC"
                params: tuple[Any, ...] = ()
            else:
                status_value = QuestionStatus(status).value
                sql = (
                    "SELECT * FROM user_questions WHERE status = ? "
                    "ORDER BY urgency DESC, created_at ASC"
                )
                params = (status_value,)
            if bounded_limit is not None:
                sql += " LIMIT ?"
                params += (bounded_limit,)
            rows = connection.execute(sql, params).fetchall()
        return [self._question_row(row) for row in rows]

    def get_question(self, question_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_questions WHERE id = ?", (question_id,)
            ).fetchone()
        if row is None:
            raise NotFound(question_id)
        return self._question_row(row)

    def answer_question(self, question_id: str, answer: str, *, actor: str = "operator") -> dict[str, Any]:
        if not answer.strip():
            raise ValueError("Answer cannot be empty")
        clean_answer = answer.strip()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM user_questions WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                raise NotFound(question_id)
            if row["status"] != QuestionStatus.PENDING.value:
                raise StateConflict(f"Question is already {row['status']}")
            answered_at = utc_now()
            cursor = connection.execute(
                "UPDATE user_questions SET status = ?, answer = ?, answered_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    QuestionStatus.ANSWERED.value,
                    clean_answer,
                    answered_at,
                    question_id,
                    QuestionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Question was answered concurrently")
            self.audit(
                actor,
                "question.answered",
                entity_type="question",
                entity_id=question_id,
                data={"answer": clean_answer},
                connection=connection,
            )
            blocking = json.loads(row["blocking_work_ids_json"] or "[]")
            event = Event(
                source="operator",
                event_type="question.answered",
                external_id=question_id,
                trust_level=TrustLevel.OPERATOR,
                payload={
                    "question_id": question_id,
                    "question": row["question"],
                    "answer": clean_answer,
                    "blocking_work_ids": blocking,
                },
                provenance={"actor": actor},
            )
            self.enqueue_event(event, actor=actor)
        return {"id": question_id, "answer": clean_answer, "blocking_work_ids": blocking}

    def create_run(self, run: RunRecord, *, actor: str = "dispatcher") -> RunRecord:
        with self.connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, work_item_id, runner, external_run_id, status, attempt,
                        result_json, error, started_at, heartbeat_at, finished_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.work_item_id,
                        run.runner,
                        run.external_run_id,
                        run.status,
                        run.attempt,
                        self._json(run.result),
                        run.error,
                        run.started_at,
                        run.heartbeat_at,
                        run.finished_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT status, external_run_id FROM runs "
                    "WHERE work_item_id = ? AND attempt = ?",
                    (run.work_item_id, run.attempt),
                ).fetchone()
                if not (
                    existing is not None
                    and existing["status"] == run.status
                    and existing["external_run_id"] == run.external_run_id
                ):
                    raise StateConflict(
                        "Run attempt changed concurrently"
                    ) from error
                return run
            self.audit(
                actor,
                "run.created",
                entity_type="run",
                entity_id=run.id,
                data={"work_item_id": run.work_item_id, "runner": run.runner},
                connection=connection,
            )
        return run

    def reserve_run_slot(
        self,
        work_item_id: str,
        *,
        runner: str,
        max_active: int,
        stale_queue_seconds: int,
        expected_work_version: int,
        contract_digest: str,
        required_state_key: str | None = None,
        required_state_digest: str | None = None,
        actor: str = "dispatcher",
    ) -> dict[str, Any] | None:
        """Atomically reserve one global execution slot for a work item.

        Queued reservations are never aged out automatically. A process may
        have created remote work immediately before crashing without recording
        its external ID, so only idempotent recovery or explicit operator
        resolution may release that fail-closed capacity slot.
        """

        if max_active < 1 or stale_queue_seconds < 1:
            raise ValueError("Run capacity and queue timeout must be positive")
        if expected_work_version < 1 or len(contract_digest) != 64:
            raise ValueError("A valid work version and contract digest are required")
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        with self.connection() as connection:
            self._begin_immediate(connection)
            if required_state_key is not None:
                if not required_state_digest or len(required_state_digest) != 64:
                    raise ValueError("A required state digest must be SHA-256 hex")
                state = connection.execute(
                    "SELECT value_json FROM system_state WHERE key = ?",
                    (required_state_key,),
                ).fetchone()
                if state is None or hashlib.sha256(
                    str(state["value_json"]).encode("utf-8")
                ).hexdigest() != required_state_digest:
                    return None
            work = connection.execute(
                "SELECT version, status, execution_mode, scheduled_at, hermes_task_id "
                "FROM work_items WHERE id = ?",
                (work_item_id,),
            ).fetchone()
            if work is None:
                raise NotFound(work_item_id)
            if (
                int(work["version"]) != expected_work_version
                or work["status"] != WorkStatus.READY.value
                or work["execution_mode"] != ExecutionMode.HERMES.value
                or not self._scheduled_time_is_due(work["scheduled_at"], now)
            ):
                return None
            outstanding_dependency = connection.execute(
                """
                SELECT 1
                FROM (
                    SELECT to_id AS dependency_id
                    FROM work_links
                    WHERE from_id = ? AND relation = ?
                    UNION ALL
                    SELECT from_id AS dependency_id
                    FROM work_links
                    WHERE to_id = ? AND relation = ?
                ) dependency_link
                JOIN work_items dependency
                  ON dependency.id = dependency_link.dependency_id
                WHERE dependency.status <> ?
                LIMIT 1
                """,
                (
                    work_item_id,
                    WorkRelation.DEPENDS_ON.value,
                    work_item_id,
                    WorkRelation.BLOCKS.value,
                    WorkStatus.DONE.value,
                ),
            ).fetchone()
            if outstanding_dependency is not None:
                return None
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE work_item_id = ? "
                "AND status IN "
                "('queued', 'running', 'cancel_requested', 'lost', "
                "'legacy_conflict') LIMIT 1",
                (work_item_id,),
            ).fetchone()
            if existing is not None:
                return None
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM runs "
                "WHERE status IN "
                "('queued', 'running', 'cancel_requested', 'lost', "
                "'legacy_conflict')"
            ).fetchone()
            if int(active["count"]) >= max_active:
                return None
            latest = connection.execute(
                "SELECT MAX(attempt) AS attempt FROM runs WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            attempt = int(latest["attempt"] or 0) + 1
            run_id = new_id("run")
            reservation = {
                "reservation": {
                    "work_version": expected_work_version,
                    "contract_digest": contract_digest,
                    "required_state_key": required_state_key,
                    "required_state_digest": required_state_digest,
                    "previous_external_run_id": work["hermes_task_id"],
                    "reserved_at": now_text,
                }
            }
            connection.execute(
                """
                INSERT INTO runs(
                    id, work_item_id, runner, status, attempt, result_json,
                    started_at, heartbeat_at
                ) VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    work_item_id,
                    runner,
                    attempt,
                    self._json(reservation),
                    now_text,
                    now_text,
                ),
            )
            self.audit(
                actor,
                "run.slot_reserved",
                entity_type="run",
                entity_id=run_id,
                data={
                    "work_item_id": work_item_id,
                    "work_version": expected_work_version,
                    "contract_digest": contract_digest,
                    "max_active": max_active,
                },
                connection=connection,
            )
            return {
                "id": run_id,
                "work_item_id": work_item_id,
                "runner": runner,
                "status": "queued",
                "attempt": attempt,
                "external_run_id": None,
                "result": reservation,
                "started_at": now_text,
                "heartbeat_at": now_text,
            }

    def commit_dispatch_reservation(
        self,
        run_id: str,
        work_item_id: str,
        *,
        expected_work_version: int,
        contract_digest: str,
        external_run_id: str,
        metadata: dict[str, Any],
        result: dict[str, Any],
        actor: str = "dispatcher",
    ) -> WorkItem:
        """Atomically link a created Hermes card and activate its reserved run."""

        if not external_run_id.strip():
            raise ValueError("external_run_id cannot be empty")
        validated_metadata = self._validated_metadata(metadata)
        now = utc_now()
        with self.connection() as connection:
            self._begin_immediate(connection)
            run = connection.execute(
                "SELECT * FROM runs WHERE id = ? AND work_item_id = ?",
                (run_id, work_item_id),
            ).fetchone()
            if run is None or run["status"] != "queued":
                raise StateConflict("Dispatch reservation is not active")
            stored_result = json.loads(run["result_json"] or "{}")
            reservation = stored_result.get("reservation", {})
            if (
                not isinstance(reservation, dict)
                or reservation.get("work_version") != expected_work_version
                or reservation.get("contract_digest") != contract_digest
            ):
                raise StateConflict("Dispatch reservation contract does not match")
            work = connection.execute(
                "SELECT version, status, execution_mode, hermes_task_id, "
                "scheduled_at, parent_id "
                "FROM work_items WHERE id = ?",
                (work_item_id,),
            ).fetchone()
            if work is None:
                raise NotFound(work_item_id)
            if (
                int(work["version"]) != expected_work_version
                or work["status"] != WorkStatus.READY.value
                or work["execution_mode"] != ExecutionMode.HERMES.value
                or work["hermes_task_id"]
                not in (
                    reservation.get("previous_external_run_id"),
                    external_run_id,
                )
                or not self._scheduled_time_is_due(
                    work["scheduled_at"], datetime.now(UTC)
                )
            ):
                raise StateConflict("Work changed after dispatch reservation")
            outstanding_dependency = connection.execute(
                """
                SELECT 1
                FROM (
                    SELECT to_id AS dependency_id
                    FROM work_links
                    WHERE from_id = ? AND relation = ?
                    UNION ALL
                    SELECT from_id AS dependency_id
                    FROM work_links
                    WHERE to_id = ? AND relation = ?
                ) dependency_link
                JOIN work_items dependency
                  ON dependency.id = dependency_link.dependency_id
                WHERE dependency.status <> ?
                LIMIT 1
                """,
                (
                    work_item_id,
                    WorkRelation.DEPENDS_ON.value,
                    work_item_id,
                    WorkRelation.BLOCKS.value,
                    WorkStatus.DONE.value,
                ),
            ).fetchone()
            if outstanding_dependency is not None:
                raise StateConflict("A dependency became outstanding during dispatch")

            authorization_value = validated_metadata.get(
                "dispatch_authorization", {}
            )
            if not isinstance(authorization_value, dict):
                raise StateConflict("Dispatch authorization metadata is missing")
            authorization = dict(authorization_value)
            if authorization.get("contract_digest") != contract_digest:
                raise StateConflict("Dispatch authorization contract changed")
            if authorization.get("consumed_at") is not None:
                raise StateConflict("Dispatch authorization was already consumed")
            authorization["consumed_at"] = now
            authorization["consumed_run_id"] = run_id
            authorization["consumed_external_run_id"] = external_run_id
            validated_metadata["dispatch_authorization"] = authorization

            work_cursor = connection.execute(
                """
                UPDATE work_items
                SET status = ?, hermes_task_id = ?, metadata_json = ?,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    WorkStatus.RUNNING.value,
                    external_run_id,
                    self._json(validated_metadata),
                    now,
                    work_item_id,
                    expected_work_version,
                ),
            )
            if work_cursor.rowcount != 1:
                raise StateConflict("Work changed while committing dispatch")
            merged_result = dict(stored_result)
            merged_result.update(result)
            run_cursor = connection.execute(
                """
                UPDATE runs
                SET external_run_id = ?, status = 'running', result_json = ?,
                    error = NULL, heartbeat_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (
                    external_run_id,
                    self._json(merged_result),
                    now,
                    run_id,
                ),
            )
            if run_cursor.rowcount != 1:
                raise StateConflict("Dispatch reservation was lost during commit")
            self._refresh_rollup_chain(connection, [work["parent_id"]])
            self.audit(
                actor,
                "dispatcher.work_dispatched",
                entity_type="work",
                entity_id=work_item_id,
                data={"hermes_task_id": external_run_id, "run_id": run_id},
                connection=connection,
            )
        return self.get_work(work_item_id)

    def update_run(self, run_id: str, *, actor: str = "dispatcher", **changes: Any) -> None:
        allowed = {"external_run_id", "status", "result", "error", "started_at", "heartbeat_at", "finished_at"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {sorted(unknown)}")
        normalized = dict(changes)
        if "result" in normalized:
            normalized["result_json"] = self._json(normalized.pop("result"))
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*normalized.values(), run_id),
            )
            if cursor.rowcount != 1:
                raise NotFound(run_id)
            self.audit(
                actor,
                "run.updated",
                entity_type="run",
                entity_id=run_id,
                data={"changes": changes},
                connection=connection,
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(run_id)
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json") or "{}")
        return value

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 5000))
        with self.connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM runs ORDER BY "
                    "COALESCE(heartbeat_at, started_at, finished_at, '') DESC, id DESC "
                    "LIMIT ?",
                    (bounded,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM runs WHERE status = ? ORDER BY "
                    "COALESCE(heartbeat_at, started_at, finished_at, '') DESC, id DESC "
                    "LIMIT ?",
                    (status, bounded),
                ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["result"] = json.loads(value.pop("result_json") or "{}")
            values.append(value)
        return values

    def resolve_run(
        self,
        run_id: str,
        *,
        expected_status: str,
        reason: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Explicitly release a fail-closed execution slot after operator review."""

        allowed = {
            "queued",
            "lost",
            "legacy_conflict",
            "blocked",
            "cancel_requested",
        }
        if expected_status not in allowed:
            raise ValueError(
                "Only queued, lost, legacy_conflict, blocked, or cancel_requested runs can be resolved"
            )
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 2000:
            raise ValueError("Run resolution needs a reason of at most 2000 characters")
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFound(run_id)
            if str(row["status"]) != expected_status:
                raise StateConflict(
                    f"Run is {row['status']}, not expected {expected_status}"
                )
            connection.execute(
                "UPDATE runs SET status = 'abandoned', finished_at = ?, "
                "error = ? WHERE id = ? AND status = ?",
                (now, f"Operator resolved execution tracking: {clean_reason}", run_id, expected_status),
            )
            work_id = str(row["work_item_id"])
            remaining = connection.execute(
                "SELECT 1 FROM runs WHERE work_item_id = ? AND id <> ? "
                "AND status IN ('queued', 'running', 'cancel_requested', "
                "'lost', 'legacy_conflict') LIMIT 1",
                (work_id, run_id),
            ).fetchone()
            work_reset = False
            if remaining is None:
                work = connection.execute(
                    "SELECT status, completed_at, metadata_json, parent_id "
                    "FROM work_items WHERE id = ?",
                    (work_id,),
                ).fetchone()
                if work is not None:
                    metadata = json.loads(work["metadata_json"] or "{}")
                    metadata.pop("dispatch_authorization", None)
                    metadata.pop("dispatch_request", None)
                    governance_value = metadata.get("governance", {})
                    governance = (
                        dict(governance_value)
                        if isinstance(governance_value, dict)
                        else {}
                    )
                    governance["execution_authorized"] = False
                    governance["execution_revoked_at"] = now
                    governance["execution_revoked_by"] = actor
                    governance["execution_revocation_reason"] = clean_reason
                    metadata["governance"] = governance
                    metadata["run_resolution"] = {
                        "run_id": run_id,
                        "prior_status": expected_status,
                        "reason": clean_reason,
                        "resolved_at": now,
                        "resolved_by": actor,
                    }
                    current_status = WorkStatus(str(work["status"]))
                    reset_status = (
                        current_status
                        if current_status in TERMINAL_WORK_STATUSES
                        else WorkStatus.BLOCKED
                    )
                    completed_at = (
                        work["completed_at"]
                        if current_status in TERMINAL_WORK_STATUSES
                        else None
                    )
                    connection.execute(
                        "UPDATE work_items SET status = ?, execution_mode = ?, "
                        "hermes_task_id = NULL, metadata_json = ?, completed_at = ?, updated_at = ?, "
                        "version = version + 1 WHERE id = ?",
                        (
                            reset_status.value,
                            ExecutionMode.NONE.value,
                            self._json(metadata),
                            completed_at,
                            now,
                            work_id,
                        ),
                    )
                    work_reset = True
                    self._refresh_rollup_chain(
                        connection, [work["parent_id"]]
                    )
            self.audit(
                actor,
                "run.resolved_by_operator",
                entity_type="run",
                entity_id=run_id,
                data={
                    "work_item_id": work_id,
                    "prior_status": expected_status,
                    "reason": clean_reason,
                    "work_reset": work_reset,
                },
                connection=connection,
            )
        return self.get_run(run_id) | {"work_reset": work_reset}

    def list_active_runs(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status IN "
                "('queued', 'running', 'cancel_requested', 'lost', "
                "'legacy_conflict') "
                "ORDER BY attempt, id"
            ).fetchall()
        return [dict(row) | {"result": json.loads(row["result_json"] or "{}")} for row in rows]

    def save_memory_candidate(
        self,
        *,
        category: str,
        content: str,
        trust_level: TrustLevel | str,
        provenance: dict[str, Any],
        confidence: float = 0.5,
        status: str = "quarantined",
        actor: str = "supervisor",
        candidate_id: str | None = None,
    ) -> str:
        content = content.strip()
        if not content:
            raise ValueError("Memory candidate content cannot be empty")
        if len(content) > 16_000:
            raise ValueError("Memory candidate content is too long")
        candidate_id = candidate_id or new_id("mem")
        trust = TrustLevel(trust_level).value
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO memory_candidates(
                    id, category, content, trust_level, provenance_json,
                    status, confidence, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (candidate_id, category, content, trust, self._json(provenance), status, confidence, utc_now()),
            )
            if cursor.rowcount == 1:
                self.audit(
                    actor,
                    "memory.candidate_created",
                    entity_type="memory",
                    entity_id=candidate_id,
                    data={"category": category, "trust_level": trust, "status": status},
                    connection=connection,
                )
        return candidate_id

    def list_memory(
        self, *, status: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM memory_candidates ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 5000)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memory_candidates WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(limit, 5000))),
                ).fetchall()
        return [self._memory_row(row) for row in rows]

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:
            raise NotFound(memory_id)
        return self._memory_row(row)

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "category": row["category"],
            "content": row["content"],
            "trust_level": row["trust_level"],
            "provenance": json.loads(row["provenance_json"] or "{}"),
            "status": row["status"],
            "confidence": row["confidence"],
            "supersedes_id": row["supersedes_id"],
            "created_at": row["created_at"],
            "reviewed_at": row["reviewed_at"],
            "promoted_at": row["promoted_at"],
        }

    def review_memory(
        self,
        memory_id: str,
        *,
        decision: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        if decision not in {"promoted", "rejected"}:
            raise ValueError("Memory decision must be promoted or rejected")
        now = utc_now()
        with self.connection() as connection:
            self._begin_immediate(connection)
            row = connection.execute(
                "SELECT status FROM memory_candidates WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFound(memory_id)
            if row["status"] not in {"pending", "quarantined"}:
                raise StateConflict(f"Memory is already {row['status']}")
            cursor = connection.execute(
                "UPDATE memory_candidates SET status = ?, reviewed_at = ?, promoted_at = ? "
                "WHERE id = ? AND status IN ('pending', 'quarantined')",
                (decision, now, now if decision == "promoted" else None, memory_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Memory state changed concurrently")
            self.audit(
                actor,
                f"memory.{decision}",
                entity_type="memory",
                entity_id=memory_id,
                connection=connection,
            )
        return self.get_memory(memory_id)

    def set_cursor(self, source: str, cursor: str, metadata: dict[str, Any] | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sync_cursors(source, cursor, metadata_json, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    cursor=excluded.cursor,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (source, cursor, self._json(metadata or {}), utc_now()),
            )

    def get_cursor(self, source: str) -> tuple[str, dict[str, Any]] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT cursor, metadata_json FROM sync_cursors WHERE source = ?", (source,)
            ).fetchone()
        return (str(row["cursor"]), json.loads(row["metadata_json"])) if row else None

    def set_state(self, key: str, value: Any) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO system_state(key, value_json, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, self._json(value), utc_now()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connection() as connection:
            row = connection.execute("SELECT value_json FROM system_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def record_policy_attestation(
        self,
        profile: str,
        attestation_id: str,
        value: dict[str, Any],
        *,
        actor: str = "hermes-bridge",
    ) -> bool:
        """Atomically record monotonic worker evidence without queuing planner work."""

        if not profile or not attestation_id:
            raise ValueError("Policy attestation profile and identity are required")
        incoming_at = datetime.fromisoformat(
            str(value["attested_at"]).replace("Z", "+00:00")
        )
        if incoming_at.tzinfo is None:
            raise ValueError("Policy attestation timestamp must include a timezone")
        key = f"hermes.policy_attestation:{profile}"
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT value_json FROM system_state WHERE key = ?",
                (key,),
            ).fetchone()
            existing = json.loads(row["value_json"]) if row else None
            if isinstance(existing, dict):
                existing_id = str(existing.get("event_id", ""))
                try:
                    existing_at = datetime.fromisoformat(
                        str(existing.get("attested_at", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    existing_at = None
                if existing_id == attestation_id or (
                    existing_at is not None
                    and existing_at.tzinfo is not None
                    and incoming_at <= existing_at
                ):
                    self.audit(
                        actor,
                        "policy_attestation.ignored_replay",
                        entity_type="hermes_profile",
                        entity_id=profile,
                        data={"attestation_id": attestation_id},
                        connection=connection,
                    )
                    return False
            stored = {
                **value,
                "event_id": attestation_id,
                "received_at": utc_now(),
                "authenticated_ingress": True,
            }
            connection.execute(
                """
                INSERT INTO system_state(key, value_json, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, self._json(stored), utc_now()),
            )
            self.audit(
                actor,
                "policy_attestation.recorded",
                entity_type="hermes_profile",
                entity_id=profile,
                data={
                    "attestation_id": attestation_id,
                    "plugin_version": value.get("plugin_version"),
                    "policy_version": value.get("policy_version"),
                    "policy_digest": value.get("policy_digest"),
                },
                connection=connection,
            )
            return True

    def acquire_service_lease(
        self,
        name: str,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> int | None:
        if not name or not owner or ttl_seconds < 1:
            raise ValueError("Lease name, owner, and positive TTL are required")
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.connection() as connection:
            self._begin_immediate(connection)
            current = connection.execute(
                "SELECT owner, epoch, expires_at FROM service_leases WHERE name = ?",
                (name,),
            ).fetchone()
            if (
                current is not None
                and current["owner"] != owner
                and current["expires_at"] > now_text
            ):
                return None
            epoch = int(current["epoch"] if current is not None else 0) + 1
            connection.execute(
                """
                INSERT INTO service_leases(name, owner, epoch, expires_at, heartbeat_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner=excluded.owner,
                    epoch=excluded.epoch,
                    expires_at=excluded.expires_at,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (name, owner, epoch, expires, now_text),
            )
            return epoch

    def renew_service_lease(
        self,
        name: str,
        owner: str,
        *,
        ttl_seconds: int,
        epoch: int | None = None,
    ) -> bool:
        if ttl_seconds < 1:
            raise ValueError("Lease TTL must be positive")
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.connection() as connection:
            epoch_clause = " AND epoch = ?" if epoch is not None else ""
            params: list[Any] = [expires, now_text, name, owner, now_text]
            if epoch is not None:
                params.append(epoch)
            cursor = connection.execute(
                """
                UPDATE service_leases
                SET expires_at = ?, heartbeat_at = ?
                WHERE name = ? AND owner = ? AND expires_at > ?
                """ + epoch_clause,
                params,
            )
            return cursor.rowcount == 1

    def assert_service_lease(
        self,
        name: str,
        owner: str,
        epoch: int,
    ) -> None:
        now_text = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM service_leases "
                "WHERE name = ? AND owner = ? AND epoch = ? AND expires_at > ?",
                (name, owner, epoch, now_text),
            ).fetchone()
        if row is None:
            raise LeaseFenceLost("Control-plane lease fence is no longer valid")

    def release_service_lease(
        self,
        name: str,
        owner: str,
        *,
        epoch: int | None = None,
    ) -> bool:
        with self.connection() as connection:
            epoch_clause = " AND epoch = ?" if epoch is not None else ""
            params: list[Any] = [name, owner]
            if epoch is not None:
                params.append(epoch)
            cursor = connection.execute(
                "DELETE FROM service_leases WHERE name = ? AND owner = ?" + epoch_clause,
                params,
            )
            return cursor.rowcount == 1

    def operational_counters(self) -> dict[str, Any]:
        """Return stable, content-free queue and workload counters."""

        active_work_statuses = tuple(
            status.value
            for status in WorkStatus
            if status not in TERMINAL_WORK_STATUSES
        )
        active_run_statuses = (
            "queued",
            "running",
            "cancel_requested",
            "lost",
            "legacy_conflict",
        )
        with self.connection() as connection:
            event_counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM events "
                    "WHERE state IN (?, ?, ?, ?) GROUP BY state",
                    (
                        EventState.PENDING.value,
                        EventState.PROCESSING.value,
                        EventState.FAILED.value,
                        EventState.DEAD_LETTER.value,
                    ),
                ).fetchall()
            }
            work_placeholders = ",".join("?" for _ in active_work_statuses)
            active_work = connection.execute(
                f"SELECT COUNT(*) AS count FROM work_items "
                f"WHERE status IN ({work_placeholders})",
                active_work_statuses,
            ).fetchone()
            run_placeholders = ",".join("?" for _ in active_run_statuses)
            active_runs = connection.execute(
                f"SELECT COUNT(*) AS count FROM runs "
                f"WHERE status IN ({run_placeholders})",
                active_run_statuses,
            ).fetchone()
            pending_questions = connection.execute(
                "SELECT COUNT(*) AS count FROM user_questions WHERE status = ?",
                (QuestionStatus.PENDING.value,),
            ).fetchone()
        return {
            "as_of": utc_now(),
            "events": {
                state.value: event_counts.get(state.value, 0)
                for state in (
                    EventState.PENDING,
                    EventState.PROCESSING,
                    EventState.FAILED,
                    EventState.DEAD_LETTER,
                )
            },
            "pending_questions": int(pending_questions["count"]),
            "active_work": int(active_work["count"]),
            "active_runs": int(active_runs["count"]),
        }

    def snapshot(
        self,
        *,
        work_limit: int = 100,
        completed_limit: int = 50,
    ) -> dict[str, Any]:
        # Time can make a due descendant overdue without any row mutation.
        # Refresh hierarchy health at snapshot time so the planner never sees
        # a stale on-track rollup after a deadline passes.
        with self.transaction() as connection:
            hierarchy_parents = [
                str(row["parent_id"])
                for row in connection.execute(
                    "SELECT DISTINCT parent_id FROM work_items "
                    "WHERE parent_id IS NOT NULL"
                ).fetchall()
            ]
            self._refresh_rollup_chain(connection, hierarchy_parents)
        active_statuses = [
            WorkStatus.INBOX,
            WorkStatus.TRIAGE,
            WorkStatus.PLANNED,
            WorkStatus.READY,
            WorkStatus.RUNNING,
            WorkStatus.WAITING_INPUT,
            WorkStatus.BLOCKED,
            WorkStatus.REVIEW,
        ]
        work = self.list_work(statuses=active_statuses, limit=work_limit)
        completed = self.list_work(
            statuses=[WorkStatus.DONE],
            limit=max(1, min(completed_limit, 500)),
            order_by="updated",
        )
        visible_ids = list(dict.fromkeys(
            [item.id for item in work] + [item.id for item in completed]
        ))
        with self.connection() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM work_items GROUP BY status"
                ).fetchall()
            }
            event_counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM events GROUP BY state"
                ).fetchall()
            }
            links: list[dict[str, Any]] = []
            if visible_ids:
                placeholders = ",".join("?" for _ in visible_ids)
                link_rows = connection.execute(
                    f"""
                    SELECT link.id, link.from_id, link.to_id, link.relation,
                           link.created_at,
                           source.kind AS from_kind,
                           source.status AS from_status,
                           target.kind AS to_kind,
                           target.status AS to_status
                    FROM work_links link
                    JOIN work_items source ON source.id = link.from_id
                    JOIN work_items target ON target.id = link.to_id
                    WHERE link.from_id IN ({placeholders})
                       OR link.to_id IN ({placeholders})
                    ORDER BY link.created_at DESC, link.id
                    LIMIT ?
                    """,
                    (
                        *visible_ids,
                        *visible_ids,
                        max(500, min(work_limit * 10, 5000)),
                    ),
                ).fetchall()
                links = [dict(row) for row in link_rows]
        rollups = self.get_work_rollups(visible_ids)

        def with_rollup(item: WorkItem) -> dict[str, Any]:
            rendered = item.to_dict()
            if item.id in rollups:
                rendered["rollup"] = rollups[item.id]
            return rendered

        return {
            "generated_at": utc_now(),
            "operational_counters": self.operational_counters(),
            "work_counts": counts,
            "event_counts": event_counts,
            "work": [with_rollup(item) for item in work],
            "completed_work": [with_rollup(item) for item in completed],
            "work_links": links,
            "questions": self.list_questions(limit=100),
            "active_runs": self.list_active_runs(),
            "promoted_memory": [
                {
                    **memory,
                    "content": str(memory["content"])[:4000],
                }
                for memory in self.list_memory(status="promoted", limit=50)
            ],
            "memory_review_counts": {
                "pending": len(self.list_memory(status="pending", limit=1000)),
                "quarantined": len(
                    self.list_memory(status="quarantined", limit=1000)
                ),
            },
        }
