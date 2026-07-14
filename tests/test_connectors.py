from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.adapters import ObsidianAdapter  # noqa: E402
from hermes_operator.config import (  # noqa: E402
    AppConfig,
    HermesConfig,
    InboundConnectorConfig,
    LLMConfig,
    ObsidianConfig,
    OperatorConfig,
    PolicyConfig,
    ServerConfig,
)
from hermes_operator.connectors import (  # noqa: E402
    CommandInboundConnector,
    ConnectorError,
    ConnectorPollReport,
    ObsidianInboxReader,
)
from hermes_operator.db import LeaseFenceLost, SQLiteStore  # noqa: E402
from hermes_operator.service import OperatorService  # noqa: E402


class CommandInboundConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteStore(Path(self.temporary.name) / "operator.db")
        self.store.initialize()

    def _config(self, script: str, **changes: object) -> InboundConnectorConfig:
        values: dict[str, object] = {
            "name": "mail-reader",
            "source": "gmail",
            "command": [sys.executable, "-c", script],
            "interval_seconds": 60.0,
        }
        values.update(changes)
        return InboundConnectorConfig(**values)

    def _event_rows(self) -> list[dict[str, object]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT source, event_type, payload_json, trust_level, "
                "provenance_json FROM events ORDER BY created_at"
            ).fetchall()
        return [
            {
                "source": row["source"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "trust_level": row["trust_level"],
                "provenance": json.loads(row["provenance_json"]),
            }
            for row in rows
        ]

    def test_fixed_command_uses_cursor_and_minimal_explicit_environment(self) -> None:
        script = """
import json
import os

cursor = os.environ.get("HERMES_OPERATOR_CONNECTOR_CURSOR", "")
print(json.dumps({
    "cursor": "cursor-2" if cursor == "cursor-1" else "cursor-1",
    "events": [{
        "event_type": "email.received",
        "external_id": "message-1",
        "dedupe_key": "message-1:v1",
        "payload": {
            "cursor_seen": cursor,
            "provider_token_seen": bool(os.environ.get("PROVIDER_READ_TOKEN")),
            "admin_token_seen": bool(os.environ.get("HERMES_OPERATOR_API_TOKEN")),
            "bridge_token_seen": bool(os.environ.get("HERMES_OPERATOR_BRIDGE_TOKEN")),
            "approval_secret_seen": bool(os.environ.get("HERMES_OPERATOR_APPROVAL_SECRET")),
        },
    }],
    "metadata": {"account": "work"},
}))
"""
        connector = CommandInboundConnector(
            self._config(script, pass_env=["PROVIDER_READ_TOKEN"]),
            self.store,
        )
        environment = {
            "PROVIDER_READ_TOKEN": "read-only-token",
            "HERMES_OPERATOR_API_TOKEN": "admin-secret",
            "HERMES_OPERATOR_BRIDGE_TOKEN": "bridge-secret",
            "HERMES_OPERATOR_APPROVAL_SECRET": "approval-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            first = connector.poll(force=True)
            second = connector.poll(force=True)

        self.assertEqual(asdict(first)["created"], 1)
        self.assertEqual(first.cursor, "cursor-1")
        self.assertEqual(second.created, 0)
        self.assertEqual(second.cursor, "cursor-2")
        self.assertEqual(self.store.get_cursor("mail-reader")[0], "cursor-2")
        rows = self._event_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "gmail")
        self.assertEqual(rows[0]["trust_level"], "authenticated_untrusted")
        self.assertEqual(rows[0]["provenance"]["ingress"], "command-connector")
        payload = rows[0]["payload"]
        self.assertTrue(payload["provider_token_seen"])
        self.assertFalse(payload["admin_token_seen"])
        self.assertFalse(payload["bridge_token_seen"])
        self.assertFalse(payload["approval_secret_seen"])

    def test_invalid_or_duplicate_json_keys_are_rejected(self) -> None:
        script = 'print(\'{"cursor":"a","cursor":"b","events":[]}\')'
        connector = CommandInboundConnector(self._config(script), self.store)

        with self.assertRaises(ConnectorError):
            connector.poll(force=True)

        self.assertIsNone(self.store.get_cursor("mail-reader"))

    def test_stdout_is_stopped_at_the_configured_byte_limit(self) -> None:
        connector = CommandInboundConnector(
            self._config('print("x" * 10000)', max_output_bytes=128),
            self.store,
        )

        with self.assertRaisesRegex(ConnectorError, "output is too large"):
            connector.poll(force=True)

        self.assertIsNone(self.store.get_cursor("mail-reader"))

    def test_leader_fence_rolls_back_events_and_cursor_together(self) -> None:
        script = """
import json
print(json.dumps({
    "cursor": "do-not-commit",
    "events": [{"event_type": "email.received", "payload": {"id": 1}}],
}))
"""

        guard_calls = 0

        def lose_fence() -> None:
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 2:
                raise LeaseFenceLost("test fence was lost")

        connector = CommandInboundConnector(
            self._config(script),
            self.store,
            leadership_guard=lose_fence,
        )
        with self.assertRaises(LeaseFenceLost):
            connector.poll(force=True)

        self.assertEqual(self._event_rows(), [])
        self.assertIsNone(self.store.get_cursor("mail-reader"))


class ObsidianInboxReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SQLiteStore(self.root / "operator.db")
        self.store.initialize()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.adapter = ObsidianAdapter(self.vault)

    def _rows(self) -> list[dict[str, object]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT external_id, payload_json, trust_level, provenance_json "
                "FROM events ORDER BY created_at"
            ).fetchall()
        return [
            {
                "external_id": row["external_id"],
                "payload": json.loads(row["payload_json"]),
                "trust_level": row["trust_level"],
                "provenance": json.loads(row["provenance_json"]),
            }
            for row in rows
        ]

    def test_only_bounded_direct_inbox_notes_enter_as_untrusted_evidence(self) -> None:
        inbox = self.vault / "Hermes Operator" / "Inbox"
        inbox.mkdir(parents=True)
        note = inbox / "capture.md"
        note.write_text(
            "---\nkind: \"capture\"\n---\nPrepare the review.\n",
            encoding="utf-8",
        )
        nested = inbox / "Nested"
        nested.mkdir()
        (nested / "ignored.md").write_text("Nested", encoding="utf-8")
        outside = self.vault / "Hermes Operator" / "Projects"
        outside.mkdir()
        (outside / "ignored.md").write_text("Projected", encoding="utf-8")

        external = self.root / "external.md"
        external.write_text("External", encoding="utf-8")
        link = inbox / "linked.md"
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError):
            link = None

        reader = ObsidianInboxReader(
            self.adapter,
            self.store,
            operator_root="Hermes Operator",
            max_documents=10,
            max_bytes=1024,
        )
        first = reader.poll()
        repeated = reader.poll()

        self.assertTrue(first.polled)
        self.assertEqual(first.received, 1)
        self.assertEqual(first.created, 1)
        self.assertEqual(repeated.received, 1)
        self.assertEqual(repeated.created, 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "Hermes Operator/Inbox/capture.md")
        self.assertEqual(rows[0]["trust_level"], "authenticated_untrusted")
        self.assertEqual(rows[0]["provenance"]["ingress"], "obsidian-inbox")
        self.assertEqual(rows[0]["payload"]["frontmatter"]["kind"], "capture")
        self.assertIn("Prepare the review", rows[0]["payload"]["body"])

        note.write_text("Changed capture", encoding="utf-8")
        changed = reader.poll()
        self.assertEqual(changed.created, 1)
        self.assertEqual(len(self._rows()), 2)

    def test_total_byte_limit_skips_oversized_notes(self) -> None:
        inbox = self.vault / "Hermes Operator" / "Inbox"
        inbox.mkdir(parents=True)
        (inbox / "large.md").write_text("x" * 200, encoding="utf-8")
        (inbox / "small.md").write_text("small", encoding="utf-8")

        documents = self.adapter.list_documents(
            "Hermes Operator/Inbox",
            limit=10,
            max_bytes=32,
        )

        self.assertEqual([document.path.name for document in documents], ["small.md"])

    def test_symlinked_inbox_directory_is_not_scanned(self) -> None:
        actual = self.vault / "actual-inbox"
        actual.mkdir()
        (actual / "capture.md").write_text("Do not ingest", encoding="utf-8")
        managed_root = self.vault / "Hermes Operator"
        managed_root.mkdir()
        try:
            (managed_root / "Inbox").symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")

        documents = self.adapter.list_documents("Hermes Operator/Inbox")

        self.assertEqual(documents, [])


class InboundServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_failures_are_isolated_and_polls_run_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = OperatorService(
                AppConfig(
                    config_path=root / "operator.toml",
                    operator=OperatorConfig(
                        database_path=root / "operator.db",
                        data_dir=root,
                    ),
                    llm=LLMConfig(provider="command"),
                    hermes=HermesConfig(enabled=False),
                    obsidian=ObsidianConfig(enabled=False, discover=False),
                    server=ServerConfig(enabled=False),
                    policy=PolicyConfig(),
                )
            )
            barrier = threading.Barrier(2)

            class Reader:
                def __init__(self, name: str, *, fail: bool) -> None:
                    self.config = SimpleNamespace(name=name)
                    self.fail = fail

                def poll(self) -> ConnectorPollReport:
                    barrier.wait(timeout=1)
                    if self.fail:
                        raise RuntimeError("source unavailable")
                    return ConnectorPollReport(
                        name=self.config.name,
                        source="test",
                        polled=True,
                    )

            service.inbound_connectors = [
                Reader("working", fail=False),
                Reader("broken", fail=True),
            ]
            service._acquire_leader()
            try:
                result = await asyncio.wait_for(
                    service._observe_inbound(),
                    timeout=2,
                )
            finally:
                service.store.release_service_lease(
                    service._leader_name,
                    service._leader_owner,
                    epoch=service._leader_epoch,
                )
                service._leader_held = False
                service._leader_epoch = None

            self.assertIn("working", result["readers"])
            self.assertIn("broken", result["errors"])
            self.assertIn("obsidian-inbox", result["readers"])
            self.assertEqual(
                service.store.get_state("inbound.health")["errors"],
                result["errors"],
            )


if __name__ == "__main__":
    unittest.main()
