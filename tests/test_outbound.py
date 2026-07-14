from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.approvals import ExternalActionStager, OutboundBroker
from hermes_operator.db import SQLiteStore
from hermes_operator.outbound import (
    CommandOutboundConnector,
    CommandOutboundConnectorConfig,
    OutboundConfigError,
    load_outbound_config,
    main,
)


def proposal() -> dict[str, object]:
    return {
        "action_type": "email.send",
        "integration": "mail",
        "target": {
            "recipients": ["person@example.com"],
            "mailbox": "primary",
        },
        "content": "Exact approved body",
        "attributes": {"subject": "Exact approved subject"},
        "reason": "Operator reviewed this exact message",
    }


class OutboundExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "operator.db")
        self.store.initialize()
        self.stager = ExternalActionStager(self.store, ttl_seconds=300)
        self.connector_script = self.root / "connector.py"
        self.connector_script.write_text(
            textwrap.dedent(
                """
                import json
                import os
                import sys

                request = json.load(sys.stdin)
                action = request["action"]
                print(json.dumps({
                    "ok": True,
                    "action_id": request["action_id"],
                    "intent_digest": request["intent_digest"],
                    "recipient": action["recipients"][0],
                    "content": action["content"]["value"],
                    "subject": action["attributes"]["subject"],
                    "has_connector_secret": "MAIL_SEND_TOKEN" in os.environ,
                    "has_admin_secret": "HERMES_OPERATOR_API_TOKEN" in os.environ,
                    "has_unlisted_secret": "UNLISTED_SECRET" in os.environ,
                }))
                """
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def config_text(self, *, enabled: bool = True, extra: str = "") -> str:
        return textwrap.dedent(
            f"""
            [broker]
            enabled = {str(enabled).lower()}
            database_path = "{self.store.path}"
            actor = "broker:test"
            max_grant_lifetime_seconds = 300

            [[connectors]]
            integration = "mail"
            command = [{json.dumps(sys.executable)}, {json.dumps(str(self.connector_script))}]
            pass_env = ["MAIL_SEND_TOKEN"]
            timeout_seconds = 10
            max_input_bytes = 65536
            max_output_bytes = 65536
            {extra}
            """
        )

    def write_config(self, *, enabled: bool = True, extra: str = "") -> Path:
        path = self.root / "broker.toml"
        path.write_text(
            self.config_text(enabled=enabled, extra=extra), encoding="utf-8"
        )
        return path

    def stage_and_approve(self):
        action_id = self.stager.stage(proposal(), created_by="supervisor")
        grant = self.stager.approve(
            action_id, approved_by="operator:reviewer"
        )
        return action_id, grant

    def test_command_connector_receives_exact_payload_and_minimal_environment(
        self,
    ) -> None:
        action_id, grant = self.stage_and_approve()
        config = load_outbound_config(self.write_config())
        connector = CommandOutboundConnector(config.connectors[0])
        broker = OutboundBroker(
            self.stager,
            connectors={"mail": connector},
            enabled=True,
            max_grant_lifetime_seconds=300,
        )
        with patch.dict(
            os.environ,
            {
                "MAIL_SEND_TOKEN": "connector-secret",
                "HERMES_OPERATOR_API_TOKEN": "admin-secret",
                "UNLISTED_SECRET": "ambient-secret",
            },
        ):
            result = broker.execute(
                action_id, grant_id=grant.grant_id, actor="broker:test"
            )

        self.assertEqual(result["action_id"], action_id)
        self.assertEqual(result["intent_digest"], self.stager.get(action_id).intent.digest)
        self.assertEqual(result["recipient"], "person@example.com")
        self.assertEqual(result["content"], "Exact approved body")
        self.assertEqual(result["subject"], "Exact approved subject")
        self.assertTrue(result["has_connector_secret"])
        self.assertFalse(result["has_admin_secret"])
        self.assertFalse(result["has_unlisted_secret"])
        self.assertEqual(self.stager.get(action_id).status, "executed")

    def test_cli_is_disabled_by_default_and_does_not_consume_grant(self) -> None:
        action_id, grant = self.stage_and_approve()
        config = self.write_config(enabled=False)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--config",
                    str(config),
                    "execute",
                    action_id,
                    "--grant-id",
                    grant.grant_id,
                ]
            )

        self.assertEqual(exit_code, 3)
        self.assertIn("disabled", stderr.getvalue())
        self.assertEqual(self.stager.get(action_id).status, "approved")
        with self.store.connection() as connection:
            consumed_at = connection.execute(
                "SELECT consumed_at FROM approval_grants WHERE id = ?",
                (grant.grant_id,),
            ).fetchone()["consumed_at"]
        self.assertIsNone(consumed_at)

    def test_cli_executes_approved_action_and_records_binding_audit(self) -> None:
        action_id, grant = self.stage_and_approve()
        config = self.write_config()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--config",
                    str(config),
                    "execute",
                    action_id,
                    "--grant-id",
                    grant.grant_id,
                ]
            )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        with self.store.connection() as connection:
            audit = connection.execute(
                "SELECT event, data_json FROM audit_log "
                "WHERE entity_id = ? AND event LIKE 'external_action.%' "
                "ORDER BY sequence",
                (action_id,),
            ).fetchall()
        events = [row["event"] for row in audit]
        self.assertIn("external_action.executing", events)
        self.assertIn("external_action.executed", events)
        claim = next(
            json.loads(row["data_json"])
            for row in audit
            if row["event"] == "external_action.executing"
        )
        self.assertEqual(claim["digest"], self.stager.get(action_id).intent.digest)
        self.assertEqual(claim["recipients_digest"], grant.recipients_digest)
        self.assertEqual(claim["content_digest"], grant.content_digest)

    def test_oversize_connector_output_fails_after_one_shot_claim(self) -> None:
        script = self.root / "large.py"
        script.write_text(
            "import sys\nsys.stdin.read()\nsys.stdout.write('x' * 10000)\n",
            encoding="utf-8",
        )
        action_id, grant = self.stage_and_approve()
        connector = CommandOutboundConnector(
            CommandOutboundConnectorConfig(
                integration="mail",
                command=(sys.executable, str(script)),
                timeout_seconds=10,
                max_input_bytes=65536,
                max_output_bytes=100,
            )
        )
        broker = OutboundBroker(
            self.stager,
            connectors={"mail": connector},
            enabled=True,
            max_grant_lifetime_seconds=300,
        )

        with self.assertRaisesRegex(RuntimeError, "output exceeds limit"):
            broker.execute(
                action_id, grant_id=grant.grant_id, actor="broker:test"
            )

        self.assertEqual(self.stager.get(action_id).status, "execution_failed")
        with self.assertRaisesRegex(Exception, "not approved"):
            broker.execute(
                action_id, grant_id=grant.grant_id, actor="broker:test"
            )

    def test_connector_rejects_mismatched_integration_even_if_misregistered(
        self,
    ) -> None:
        action_id, grant = self.stage_and_approve()
        config = load_outbound_config(self.write_config())
        connector_config = config.connectors[0]
        connector = CommandOutboundConnector(
            CommandOutboundConnectorConfig(
                integration="chat",
                command=connector_config.command,
                pass_env=connector_config.pass_env,
                timeout_seconds=connector_config.timeout_seconds,
                max_input_bytes=connector_config.max_input_bytes,
                max_output_bytes=connector_config.max_output_bytes,
            )
        )
        broker = OutboundBroker(
            self.stager,
            connectors={"mail": connector},
            enabled=True,
            max_grant_lifetime_seconds=300,
        )

        with self.assertRaisesRegex(RuntimeError, "does not match connector"):
            broker.execute(
                action_id, grant_id=grant.grant_id, actor="broker:test"
            )

        self.assertEqual(self.stager.get(action_id).status, "execution_failed")

    def test_atomic_claim_rolls_back_grant_if_action_claim_fails(self) -> None:
        action_id, grant = self.stage_and_approve()
        with self.store.connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_execution_claim "
                "BEFORE UPDATE OF status ON action_intents "
                "WHEN NEW.status = 'executing' "
                "BEGIN SELECT RAISE(ABORT, 'claim rejected'); END"
            )
        config = load_outbound_config(self.write_config())
        broker = OutboundBroker(
            self.stager,
            connectors={
                "mail": CommandOutboundConnector(config.connectors[0])
            },
            enabled=True,
            max_grant_lifetime_seconds=300,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            broker.execute(
                action_id, grant_id=grant.grant_id, actor="broker:test"
            )

        self.assertEqual(self.stager.get(action_id).status, "approved")
        with self.store.connection() as connection:
            consumed_at = connection.execute(
                "SELECT consumed_at FROM approval_grants WHERE id = ?",
                (grant.grant_id,),
            ).fetchone()["consumed_at"]
        self.assertIsNone(consumed_at)

    def test_config_rejects_control_plane_secret_and_unknown_fields(self) -> None:
        path = self.root / "protected.toml"
        path.write_text(
            self.config_text().replace(
                'pass_env = ["MAIL_SEND_TOKEN"]',
                'pass_env = ["HERMES_OPERATOR_API_TOKEN"]',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OutboundConfigError, "control-plane secrets"):
            load_outbound_config(path)

        for protected_name in (
            "HERMES_KANBAN_CONTROL_TOKEN",
            "HERMES_OPERATOR_CUSTOM_CONTROL_SECRET",
        ):
            path.write_text(
                self.config_text().replace(
                    'pass_env = ["MAIL_SEND_TOKEN"]',
                    f'pass_env = ["{protected_name}"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                OutboundConfigError,
                "control-plane secrets",
            ):
                load_outbound_config(path)

        path.write_text(
            self.config_text(extra='shell = true'), encoding="utf-8"
        )
        with self.assertRaisesRegex(OutboundConfigError, "Unknown"):
            load_outbound_config(path)


if __name__ == "__main__":
    unittest.main()
