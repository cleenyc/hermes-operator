from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import LLMConfig


_SAFE_ENV_NAMES = {
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERNAME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}

_MAX_PROVIDER_RESPONSE_BYTES = 4_194_304


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class LLMResult:
    data: dict[str, Any]
    raw_text: str
    usage: dict[str, Any]
    model: str


class PlannerLLM(Protocol):
    async def generate_json(self, *, system: str, user: str) -> LLMResult: ...


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the provider credential on the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Model response did not contain a JSON object")
        try:
            parsed = json.loads(
                value[start : end + 1],
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise LLMError(f"Model returned invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise LLMError("Model response must be a JSON object")
    return parsed


class OpenAICompatibleLLM:
    def __init__(self, config: LLMConfig):
        self.config = config

    async def generate_json(self, *, system: str, user: str) -> LLMResult:
        return await asyncio.to_thread(self._request, system, user)

    def _request(self, system: str, user: str) -> LLMResult:
        api_key = self.config.resolved_api_key()
        if not api_key:
            raise LLMNotConfigured(f"Missing API key in {self.config.api_key_env}")
        if not self.config.model:
            raise LLMNotConfigured("llm.model is not configured")
        url = self.config.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_PROVIDER_RESPONSE_BYTES:
                    raise LLMError("LLM response exceeded the provider response size limit")
                payload = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
        except urllib.error.HTTPError as error:
            detail = error.read(2001).decode(errors="replace")[:2000]
            raise LLMError(f"LLM HTTP {error.code}: {detail}") from error
        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise LLMError(f"LLM request failed: {error}") from error
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError("LLM response did not contain choices[0].message.content") from error
        return LLMResult(
            data=extract_json(content),
            raw_text=content,
            usage=dict(payload.get("usage", {})),
            model=str(payload.get("model", self.config.model)),
        )


class CommandLLM:
    """Runs a fixed, deployment-configured command that emits JSON to stdout."""

    def __init__(self, config: LLMConfig):
        self.config = config

    async def generate_json(self, *, system: str, user: str) -> LLMResult:
        return await asyncio.to_thread(self._run, system, user)

    def _run(self, system: str, user: str) -> LLMResult:
        if not self.config.command:
            raise LLMNotConfigured("llm.command is empty")
        command = [part.format(model=self.config.model) for part in self.config.command]
        input_payload = json.dumps({"system": system, "user": user}, ensure_ascii=False)
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_ENV_NAMES or key.startswith("LC_")
        }
        child_env.update(
            {
                key: os.environ[key]
                for key in self.config.pass_env
                if key in os.environ
            }
        )
        try:
            completed = subprocess.run(
                command,
                input=input_payload,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
                shell=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LLMError(f"LLM command failed: {error}") from error
        if completed.returncode != 0:
            raise LLMError(f"LLM command exited {completed.returncode}: {completed.stderr[:2000]}")
        return LLMResult(
            data=extract_json(completed.stdout),
            raw_text=completed.stdout,
            usage={},
            model=self.config.model or shlex.join(command),
        )


class ScriptedLLM:
    """Deterministic planner used by contract tests and simulations."""

    def __init__(self, responses: list[dict[str, Any]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, str]] = []

    async def generate_json(self, *, system: str, user: str) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        if not self.responses:
            raise LLMError("ScriptedLLM has no response")
        data = self.responses.pop(0)
        raw = json.dumps(data)
        return LLMResult(data=data, raw_text=raw, usage={}, model="scripted")


def build_llm(config: LLMConfig) -> PlannerLLM:
    if config.provider == "openai_compatible":
        return OpenAICompatibleLLM(config)
    if config.provider == "command":
        return CommandLLM(config)
    raise ValueError(f"Unsupported llm.provider: {config.provider}")
