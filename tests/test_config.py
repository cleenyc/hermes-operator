from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.config import load_config  # noqa: E402


CONFIG_TEMPLATE = """
[operator]
database_path = "data/operator.db"
data_dir = "data"
tick_seconds = {operator_tick_seconds}
reconciliation_seconds = {operator_reconciliation_seconds}
reasoning_refresh_seconds = {operator_reasoning_refresh_seconds}
max_events_per_pass = {operator_max_events_per_pass}
max_parallel_work = {operator_max_parallel_work}
max_authorizations_per_pass = {operator_max_authorizations_per_pass}
event_lease_seconds = {operator_event_lease_seconds}
event_max_attempts = {operator_event_max_attempts}

[llm]
provider = "command"
command = ["planner"]
pass_env = {llm_pass_env}
timeout_seconds = {llm_timeout_seconds}
temperature = {llm_temperature}
max_output_tokens = {llm_max_output_tokens}

[hermes]
enabled = false
command_timeout_seconds = {hermes_command_timeout_seconds}
dispatch_authorization_ttl_seconds = {hermes_dispatch_ttl}
max_execution_attempts = {hermes_max_execution_attempts}
policy_attestation_ttl_seconds = {hermes_attestation_ttl}

[obsidian]
enabled = false

[server]
enabled = false
port = {server_port}
max_body_bytes = {server_max_body_bytes}

[policy]
approval_ttl_seconds = {policy_approval_ttl}
max_llm_priority_adjustment = {policy_max_priority_adjustment}

[[inbound_connectors]]
name = "mail-reader"
source = "mail"
command = ["reader"]
interval_seconds = {connector_interval_seconds}
timeout_seconds = {connector_timeout_seconds}
max_output_bytes = {connector_max_output_bytes}
"""


class ConfigurationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "operator.toml"
        self.values = {
            "operator_tick_seconds": "30.0",
            "operator_reconciliation_seconds": "300.0",
            "operator_reasoning_refresh_seconds": "3600.0",
            "operator_max_events_per_pass": "25",
            "operator_max_parallel_work": "4",
            "operator_max_authorizations_per_pass": "40",
            "operator_event_lease_seconds": "500",
            "operator_event_max_attempts": "5",
            "llm_pass_env": "[]",
            "llm_timeout_seconds": "180",
            "llm_temperature": "0.1",
            "llm_max_output_tokens": "8000",
            "hermes_command_timeout_seconds": "120",
            "hermes_dispatch_ttl": "86400",
            "hermes_max_execution_attempts": "3",
            "hermes_attestation_ttl": "300",
            "server_port": "8787",
            "server_max_body_bytes": "1048576",
            "policy_approval_ttl": "3600",
            "policy_max_priority_adjustment": "10.0",
            "connector_interval_seconds": "60.0",
            "connector_timeout_seconds": "60",
            "connector_max_output_bytes": "4194304",
        }

    def load_with(self, **overrides: str):
        values = self.values | overrides
        self.path.write_text(CONFIG_TEMPLATE.format(**values), encoding="utf-8")
        return load_config(self.path)

    def test_valid_numeric_configuration_loads(self) -> None:
        config = self.load_with()
        self.assertEqual(config.inbound_connectors[0].name, "mail-reader")

    def test_toml_nan_and_infinity_are_rejected_for_every_numeric_field(self) -> None:
        fields = [
            key
            for key in self.values
            if key != "llm_pass_env"
        ]
        for field in fields:
            for literal in ("nan", "inf", "-inf"):
                with self.subTest(field=field, literal=literal):
                    with self.assertRaises(ValueError):
                        self.load_with(**{field: literal})

    def test_llm_command_cannot_receive_control_plane_secrets(self) -> None:
        with self.assertRaises(ValueError):
            self.load_with(llm_pass_env='["HERMES_OPERATOR_API_TOKEN"]')

    def test_openai_compatible_transport_requires_safe_base_url(self) -> None:
        template = CONFIG_TEMPLATE.replace(
            'provider = "command"\ncommand = ["planner"]',
            'provider = "openai_compatible"\n'
            'model = "test-model"\n'
            'base_url = "{llm_base_url}"\n'
            'api_key_env = "OPENAI_API_KEY"',
        )
        values = self.values | {"llm_base_url": "https://models.example/v1"}
        self.path.write_text(template.format(**values), encoding="utf-8")
        self.assertEqual(load_config(self.path).llm.base_url, values["llm_base_url"])

        for unsafe in (
            "http://models.example/v1",
            "https://user:secret@models.example/v1",
            "https://models.example/v1?redirect=other",
        ):
            with self.subTest(base_url=unsafe):
                values["llm_base_url"] = unsafe
                self.path.write_text(template.format(**values), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(self.path)

        values["llm_base_url"] = "http://127.0.0.1:11434/v1"
        self.path.write_text(template.format(**values), encoding="utf-8")
        self.assertEqual(load_config(self.path).llm.base_url, values["llm_base_url"])

    def test_execution_budgets_have_hard_upper_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_parallel_work"):
            self.load_with(operator_max_parallel_work="65")
        with self.assertRaisesRegex(ValueError, "max_authorizations_per_pass"):
            self.load_with(operator_max_authorizations_per_pass="81")
        with self.assertRaisesRegex(ValueError, "max_execution_attempts"):
            self.load_with(hermes_max_execution_attempts="11")

    def test_active_hermes_requires_control_transport_and_supports_attested_profiles(self) -> None:
        active_template = """
[operator]
database_path = "data/operator.db"
data_dir = "data"
autonomy_mode = "internal"
event_lease_seconds = 500

[llm]
provider = "command"
command = ["planner"]
timeout_seconds = 180

[hermes]
enabled = true
profile = "operator"
default_assignee = "{assignee}"
orchestrator_profile = "operator"
allowed_profiles = ["operator"]
control_base_url = "{control_url}"
control_token = "{control_token}"
require_policy_attestation = true
allowed_plugin_versions = ["1.1.0"]
allowed_policy_versions = ["2.0.0"]
allowed_policy_digests = ["{digest}"]

[obsidian]
enabled = false

[server]
enabled = true
bridge_token = "bridge-secret"
bridge_proof_secret = "proof-secret-that-is-at-least-32-bytes-long"

[policy]
external_actions_require_approval = true
"""

        def load_active(*, assignee: str, control_url: str, control_token: str):
            self.path.write_text(
                active_template.format(
                    assignee=assignee,
                    control_url=control_url,
                    control_token=control_token,
                    digest="a" * 64,
                ),
                encoding="utf-8",
            )
            return load_config(self.path)

        valid = load_active(
            assignee="operator",
            control_url="http://127.0.0.1:8000",
            control_token="run-control-secret",
        )
        self.assertEqual(valid.hermes.default_assignee, "operator")

        active_without_ack = active_template.replace(
            'autonomy_mode = "internal"',
            'autonomy_mode = "active"',
        )
        self.path.write_text(
            active_without_ack.format(
                assignee="operator",
                control_url="http://127.0.0.1:8000",
                control_token="run-control-secret",
                digest="a" * 64,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "isolation were reviewed"):
            load_config(self.path)
        self.path.write_text(
            self.path.read_text(encoding="utf-8").replace(
                "enabled = true\nprofile = \"operator\"",
                "enabled = true\nactive_isolation_acknowledged = true\nprofile = \"operator\"",
            ),
            encoding="utf-8",
        )
        self.assertTrue(load_config(self.path).hermes.active_isolation_acknowledged)
        self.path.write_text(
            self.path.read_text(encoding="utf-8").replace(
                "require_policy_attestation = true",
                "require_policy_attestation = false",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "policy attestation"):
            load_config(self.path)
        with self.assertRaisesRegex(ValueError, "run-control"):
            load_active(
                assignee="operator",
                control_url="",
                control_token="",
            )
        multi_profile = load_active(
            assignee="executor",
            control_url="http://127.0.0.1:8000",
            control_token="run-control-secret",
        )
        self.assertEqual(multi_profile.hermes.default_assignee, "executor")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_active(
                assignee="operator",
                control_url="http://hermes.example:8000",
                control_token="run-control-secret",
            )


if __name__ == "__main__":
    unittest.main()
