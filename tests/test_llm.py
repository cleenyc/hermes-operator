from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.config import LLMConfig  # noqa: E402
from hermes_operator.llm import (  # noqa: E402
    CommandLLM,
    LLMError,
    OpenAICompatibleLLM,
    _MAX_PROVIDER_RESPONSE_BYTES,
    _NoRedirect,
    extract_json,
)


class LLMJSONTests(unittest.TestCase):
    def test_extract_json_rejects_non_finite_constants(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(LLMError):
                    extract_json('{"confidence": ' + constant + "}")

    def test_extract_json_accepts_standard_json_object(self) -> None:
        self.assertEqual(extract_json('{"confidence": 0.9}'), {"confidence": 0.9})

    def test_extract_json_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(LLMError):
            extract_json('{"confidence": 0.1, "confidence": 0.9}')


class OpenAICompatibleTransportTests(unittest.TestCase):
    def test_redirect_handler_refuses_redirects(self) -> None:
        self.assertIsNone(
            _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.test")
        )

    def test_provider_response_has_a_hard_size_limit(self) -> None:
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit: int) -> bytes:
                self.requested_limit = limit
                return b"x" * limit

        response = OversizedResponse()
        opener = mock.Mock()
        opener.open.return_value = response
        planner = OpenAICompatibleLLM(
            LLMConfig(model="test-model", api_key="test-key")
        )
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(LLMError, "size limit"):
                planner._request("policy", "input")
        self.assertEqual(
            response.requested_limit,
            _MAX_PROVIDER_RESPONSE_BYTES + 1,
        )


class CommandLLMIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_substitutes_only_documented_model_token(self) -> None:
        script = (
            "import json; print(json.dumps("
            '{"model":"{model}","literal":{"nested":True}}))'
        )
        planner = CommandLLM(
            LLMConfig(
                provider="command",
                model="test-model",
                command=[sys.executable, "-c", script],
            )
        )

        result = await planner.generate_json(system="policy", user="input")

        self.assertEqual(result.data["model"], "test-model")
        self.assertEqual(result.data["literal"], {"nested": True})

    async def test_command_receives_only_safe_and_explicit_environment(self) -> None:
        script = (
            "import json,os,sys;json.load(sys.stdin);"
            "print(json.dumps(dict("
            "allowed=os.environ.get('LLM_WRAPPER_SETTING'),"
            "admin=os.environ.get('HERMES_OPERATOR_API_TOKEN'),"
            "custom=os.environ.get('CUSTOM_OUTBOUND_SECRET'))))"
        )
        planner = CommandLLM(
            LLMConfig(
                provider="command",
                model="test-model",
                command=[sys.executable, "-c", script],
                pass_env=["LLM_WRAPPER_SETTING"],
            )
        )
        environment = {
            "LLM_WRAPPER_SETTING": "allowed-value",
            "HERMES_OPERATOR_API_TOKEN": "admin-secret",
            "CUSTOM_OUTBOUND_SECRET": "outbound-secret",
        }

        with mock.patch.dict(os.environ, environment, clear=False):
            result = await planner.generate_json(system="policy", user="untrusted input")

        self.assertEqual(result.data["allowed"], "allowed-value")
        self.assertIsNone(result.data["admin"])
        self.assertIsNone(result.data["custom"])


if __name__ == "__main__":
    unittest.main()
