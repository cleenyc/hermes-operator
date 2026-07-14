"""Deterministic, artifact-aware completion verification.

This module deliberately does not call a model.  It treats Hermes completion
metadata as untrusted declarations, resolves artifacts only inside explicitly
configured roots, hashes their content, and runs only deployment-approved
fixed-argv checks named by the canonical work contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .config import VerificationCheckConfig, VerificationConfig
from .models import WorkItem, utc_now


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ENV_NAMES = {
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
}


def validate_verification_contract(
    value: Any,
    config: VerificationConfig,
) -> dict[str, Any]:
    """Validate a trusted, pre-dispatch contract without requiring outputs yet."""

    if not isinstance(value, Mapping):
        raise ValueError("verification contract must be an object")
    contract = dict(value)
    unknown = set(map(str, contract)) - {"artifacts", "checks"}
    if unknown:
        raise ValueError(
            "verification contract contains unsupported keys: "
            + ", ".join(sorted(unknown))
        )
    artifacts = contract.get("artifacts", [])
    checks = contract.get("checks", [])
    if not isinstance(artifacts, list) or len(artifacts) > config.max_artifacts:
        raise ValueError("verification contract artifacts exceed the configured limit")
    if (
        not isinstance(checks, list)
        or any(not isinstance(name, str) or not name for name in checks)
        or len(checks) != len(set(checks))
    ):
        raise ValueError("verification contract checks must contain unique names")
    configured_checks = {check.name for check in config.checks}
    unknown_checks = set(checks) - configured_checks
    if unknown_checks:
        raise ValueError(
            "verification contract names unconfigured checks: "
            + ", ".join(sorted(unknown_checks))
        )
    for declaration in artifacts:
        if isinstance(declaration, str):
            raw: dict[str, Any] = {"path": declaration}
        elif isinstance(declaration, Mapping):
            raw = dict(declaration)
        else:
            raise ValueError("verification contract artifact must be a path or object")
        extra = set(map(str, raw)) - {"path", "root", "type", "sha256"}
        if extra:
            raise ValueError(
                "verification contract artifact contains unsupported keys: "
                + ", ".join(sorted(extra))
            )
        path = raw.get("path")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("verification contract artifact path is invalid")
        if ".." in Path(path).parts:
            raise ValueError("verification contract artifact cannot use parent traversal")
        root = raw.get("root")
        if root is not None and (
            not isinstance(root, str) or root not in config.artifact_roots
        ):
            raise ValueError(f"verification contract artifact root is unknown: {root}")
        if raw.get("type") is not None and raw.get("type") not in {
            "file",
            "directory",
        }:
            raise ValueError("verification contract artifact type is invalid")
        digest = raw.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ValueError("verification contract artifact sha256 is invalid")
    if (artifacts or checks) and not config.enabled:
        raise ValueError("deterministic verification is disabled")
    # Round-trip produces a detached JSON-only value suitable for canonical metadata.
    return json.loads(
        json.dumps(
            {"artifacts": artifacts, "checks": checks},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


@dataclass(slots=True)
class VerificationOutcome:
    """Serializable result from the deterministic verification boundary."""

    applicable: bool
    passed: bool
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verified_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "applicable": self.applicable,
            "passed": self.passed,
            "artifacts": self.artifacts,
            "checks": self.checks,
            "errors": self.errors,
            "verified_at": self.verified_at,
        }


class ArtifactVerifier:
    """Validate local artifacts and execute named deterministic checks."""

    def __init__(self, config: VerificationConfig):
        self.config = config
        self._checks = {value.name: value for value in config.checks}

    def verify(
        self,
        *,
        work: WorkItem,
        completion: Mapping[str, Any],
    ) -> VerificationOutcome:
        contract_raw = work.metadata.get("verification_contract")
        evidence_artifacts, extraction_errors = self._extract_artifacts(completion)
        contract_present = contract_raw is not None
        errors = list(extraction_errors)
        contract_artifacts: list[Any] = []
        requested_checks: list[str] = []

        if contract_present:
            if not isinstance(contract_raw, Mapping):
                errors.append("verification_contract must be an object")
            else:
                unknown = set(map(str, contract_raw)) - {"artifacts", "checks"}
                if unknown:
                    errors.append(
                        "verification_contract contains unsupported keys: "
                        + ", ".join(sorted(unknown))
                    )
                raw_artifacts = contract_raw.get("artifacts", [])
                if not isinstance(raw_artifacts, list):
                    errors.append("verification_contract.artifacts must be a list")
                else:
                    contract_artifacts = raw_artifacts
                raw_checks = contract_raw.get("checks", [])
                if (
                    not isinstance(raw_checks, list)
                    or any(not isinstance(value, str) or not value for value in raw_checks)
                    or len(raw_checks) != len(set(raw_checks))
                ):
                    errors.append(
                        "verification_contract.checks must contain unique check names"
                    )
                else:
                    requested_checks = list(raw_checks)

        declarations = self._deduplicate_declarations(
            [*contract_artifacts, *evidence_artifacts]
        )
        applicable = bool(contract_present or declarations or requested_checks or errors)
        if not applicable:
            return VerificationOutcome(applicable=False, passed=True)
        if not self.config.enabled:
            errors.append("deterministic verification is disabled")
            return VerificationOutcome(applicable=True, passed=False, errors=errors)
        if len(declarations) > self.config.max_artifacts:
            errors.append(
                f"artifact count exceeds configured maximum {self.config.max_artifacts}"
            )
            return VerificationOutcome(applicable=True, passed=False, errors=errors)

        artifacts: list[dict[str, Any]] = []
        total_bytes = 0
        for declaration in declarations:
            try:
                report, consumed = self._verify_artifact(
                    declaration,
                    remaining_bytes=(
                        self.config.max_total_artifact_bytes - total_bytes
                    ),
                )
                artifacts.append(report)
                total_bytes += consumed
            except (OSError, ValueError) as error:
                errors.append(str(error))

        checks: list[dict[str, Any]] = []
        for name in requested_checks:
            configured = self._checks.get(name)
            if configured is None:
                errors.append(f"verification check is not configured: {name}")
                continue
            report = self._run_check(configured)
            checks.append(report)
            if not report["passed"]:
                errors.append(f"verification check failed: {name}")

        return VerificationOutcome(
            applicable=True,
            passed=not errors,
            artifacts=artifacts,
            checks=checks,
            errors=errors,
        )

    @staticmethod
    def _extract_artifacts(
        completion: Mapping[str, Any],
    ) -> tuple[list[Any], list[str]]:
        """Read only documented/native artifact locations, never arbitrary keys."""

        candidates: list[Any] = []
        invalid_locations: list[str] = []
        locations: list[tuple[str, Any]] = [("artifacts", completion.get("artifacts"))]
        raw = completion.get("raw")
        if isinstance(raw, Mapping):
            locations.append(("raw.artifacts", raw.get("artifacts")))
            metadata = raw.get("metadata")
            if isinstance(metadata, Mapping):
                locations.append(("raw.metadata.artifacts", metadata.get("artifacts")))
            result = raw.get("result")
            if isinstance(result, Mapping):
                locations.append(("raw.result.artifacts", result.get("artifacts")))
                result_metadata = result.get("metadata")
                if isinstance(result_metadata, Mapping):
                    locations.append(
                        (
                            "raw.result.metadata.artifacts",
                            result_metadata.get("artifacts"),
                        )
                    )
        for location, value in locations:
            if value is None:
                continue
            if not isinstance(value, list):
                invalid_locations.append(f"{location} must be a list")
                continue
            candidates.extend(value)
        return candidates, invalid_locations

    @staticmethod
    def _deduplicate_declarations(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        seen: set[str] = set()
        for value in values:
            try:
                identity = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                identity = repr(value)
            if identity not in seen:
                seen.add(identity)
                result.append(value)
        return result

    def _verify_artifact(
        self,
        declaration: Any,
        *,
        remaining_bytes: int,
    ) -> tuple[dict[str, Any], int]:
        if isinstance(declaration, str):
            value: dict[str, Any] = {"path": declaration}
        elif isinstance(declaration, Mapping):
            value = dict(declaration)
        else:
            raise ValueError("artifact declaration must be a path or object")
        unknown = set(map(str, value)) - {"path", "root", "type", "sha256"}
        if unknown:
            raise ValueError(
                "artifact declaration contains unsupported keys: "
                + ", ".join(sorted(unknown))
            )
        raw_path = value.get("path")
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("artifact path must be a nonempty string")
        expected_type = value.get("type")
        if expected_type is not None and expected_type not in {"file", "directory"}:
            raise ValueError("artifact type must be file or directory")
        expected_digest = value.get("sha256")
        if expected_digest is not None and (
            not isinstance(expected_digest, str)
            or _SHA256_RE.fullmatch(expected_digest) is None
        ):
            raise ValueError("artifact sha256 must be lowercase hexadecimal")

        root_name, root, path, relative = self._resolve_artifact(
            raw_path,
            value.get("root"),
        )
        if path.is_file():
            actual_type = "file"
            digest, byte_count = self._hash_file(
                path,
                limit=min(self.config.max_artifact_bytes, remaining_bytes),
            )
            file_count = 1
        elif path.is_dir():
            actual_type = "directory"
            digest, byte_count, file_count = self._hash_directory(
                path,
                limit=min(self.config.max_artifact_bytes, remaining_bytes),
            )
        else:
            raise ValueError(f"artifact is not a regular file or directory: {raw_path}")
        if expected_type is not None and expected_type != actual_type:
            raise ValueError(
                f"artifact type mismatch for {raw_path}: expected {expected_type}, got {actual_type}"
            )
        if expected_digest is not None and expected_digest != digest:
            raise ValueError(f"artifact digest mismatch for {raw_path}")
        return (
            {
                "root": root_name,
                "path": relative.as_posix(),
                "type": actual_type,
                "sha256": digest,
                "bytes": byte_count,
                "files": file_count,
                "expected_sha256_matched": expected_digest is not None,
            },
            byte_count,
        )

    def _resolve_artifact(
        self,
        raw_path: str,
        root_hint: Any,
    ) -> tuple[str, Path, Path, Path]:
        if root_hint is not None and (
            not isinstance(root_hint, str) or root_hint not in self.config.artifact_roots
        ):
            raise ValueError(f"unknown artifact root: {root_hint}")
        if not self.config.artifact_roots:
            raise ValueError("artifact roots are not configured")
        supplied = Path(raw_path)
        if ".." in supplied.parts:
            raise ValueError(f"artifact path cannot contain parent traversal: {raw_path}")
        roots: list[tuple[str, Path]] = []
        for name, configured in self.config.artifact_roots.items():
            try:
                resolved = configured.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"artifact root is unavailable: {name}") from error
            if not resolved.is_dir():
                raise ValueError(f"artifact root is not a directory: {name}")
            roots.append((name, resolved))

        if supplied.is_absolute():
            try:
                resolved_path = supplied.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"artifact does not exist: {raw_path}") from error
            eligible = [
                (name, root)
                for name, root in roots
                if (root_hint is None or name == root_hint)
                and resolved_path.is_relative_to(root)
            ]
            if not eligible:
                raise ValueError(f"artifact escapes configured roots: {raw_path}")
            name, root = max(eligible, key=lambda pair: len(pair[1].parts))
            if not supplied.is_relative_to(root):
                raise ValueError(
                    f"absolute artifact path must use its canonical root: {raw_path}"
                )
            self._reject_symlink_components(root, supplied)
        else:
            if root_hint is not None:
                eligible = [pair for pair in roots if pair[0] == root_hint]
            elif len(roots) == 1:
                eligible = roots
            else:
                raise ValueError(
                    f"relative artifact path needs a named root: {raw_path}"
                )
            name, root = eligible[0]
            original_path = root / supplied
            self._reject_symlink_components(root, original_path)
            try:
                resolved_path = original_path.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"artifact does not exist: {raw_path}") from error
            if not resolved_path.is_relative_to(root):
                raise ValueError(f"artifact escapes configured root: {raw_path}")
        return name, root, resolved_path, resolved_path.relative_to(root)

    @staticmethod
    def _reject_symlink_components(root: Path, path: Path) -> None:
        relative = path.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            try:
                if cursor.is_symlink():
                    raise ValueError(f"artifact path contains a symlink: {relative}")
            except OSError as error:
                raise ValueError(f"artifact path cannot be inspected: {relative}") from error

    @staticmethod
    def _hash_file(path: Path, *, limit: int) -> tuple[str, int]:
        if limit < 0:
            raise ValueError("artifact set exceeds the configured total byte limit")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        digest = hashlib.sha256()
        count = 0
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"artifact is not a regular file: {path.name}")
            if metadata.st_size > limit:
                raise ValueError(f"artifact exceeds the configured byte limit: {path.name}")
            while True:
                chunk = os.read(descriptor, 1_048_576)
                if not chunk:
                    break
                count += len(chunk)
                if count > limit:
                    raise ValueError(
                        f"artifact exceeds the configured byte limit: {path.name}"
                    )
                digest.update(chunk)
        finally:
            os.close(descriptor)
        return digest.hexdigest(), count

    def _hash_directory(
        self,
        path: Path,
        *,
        limit: int,
    ) -> tuple[str, int, int]:
        digest = hashlib.sha256()
        byte_count = 0
        file_count = 0

        def visit(directory: Path) -> None:
            nonlocal byte_count, file_count
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda value: value.name)
            for entry in ordered:
                child = Path(entry.path)
                relative = child.relative_to(path).as_posix()
                if entry.is_symlink():
                    raise ValueError(f"artifact directory contains a symlink: {relative}")
                if entry.is_dir(follow_symlinks=False):
                    digest.update(f"D\0{relative}\n".encode("utf-8"))
                    visit(child)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError(
                        f"artifact directory contains a special file: {relative}"
                    )
                file_count += 1
                if file_count > self.config.max_files_per_directory:
                    raise ValueError(
                        "artifact directory exceeds the configured file-count limit"
                    )
                file_digest, size = self._hash_file(
                    child,
                    limit=min(
                        self.config.max_artifact_bytes,
                        limit - byte_count,
                    ),
                )
                byte_count += size
                digest.update(
                    f"F\0{relative}\0{size}\0{file_digest}\n".encode("utf-8")
                )

        visit(path)
        return digest.hexdigest(), byte_count, file_count

    def _run_check(self, check: VerificationCheckConfig) -> dict[str, Any]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_ENV_NAMES or key.startswith("LC_")
        }
        environment.update(
            {key: os.environ[key] for key in check.pass_env if key in os.environ}
        )
        environment["HERMES_OPERATOR_VERIFICATION_CHECK"] = check.name
        if check.cwd is None:
            return {
                "name": check.name,
                "passed": False,
                "error": "verification check has no configured working directory",
            }
        try:
            return_code, stdout, stderr, exceeded = _run_bounded_command(
                check.command,
                cwd=check.cwd,
                environment=environment,
                timeout_seconds=check.timeout_seconds,
                max_output_bytes=check.max_output_bytes,
            )
        except subprocess.TimeoutExpired:
            return {
                "name": check.name,
                "passed": False,
                "timed_out": True,
                "timeout_seconds": check.timeout_seconds,
            }
        except OSError as error:
            return {
                "name": check.name,
                "passed": False,
                "error": str(error)[:1_000],
            }
        combined = (stdout + "\n" + stderr).encode("utf-8", errors="replace")
        return {
            "name": check.name,
            "passed": return_code == 0 and not exceeded,
            "return_code": return_code,
            "output_exceeded": exceeded,
            "output_sha256": hashlib.sha256(combined).hexdigest(),
            "stdout_preview": stdout[:2_000],
            "stderr_preview": stderr[:2_000],
        }


def _run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[int, str, str, bool]:
    """Run fixed argv without a shell and cap combined captured output."""

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=environment,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    exceeded = threading.Event()
    lock = threading.Lock()
    observed = 0

    def read_pipe(stream: BinaryIO, chunks: list[bytes]) -> None:
        nonlocal observed
        stored = 0
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                with lock:
                    observed += len(chunk)
                    too_large = observed > max_output_bytes
                if stored < max_output_bytes:
                    retained = chunk[: max_output_bytes - stored]
                    chunks.append(retained)
                    stored += len(retained)
                if too_large:
                    exceeded.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        except (OSError, ValueError):
            return

    threads = [
        threading.Thread(
            target=read_pipe,
            args=(process.stdout, stdout_chunks),
            daemon=True,
        ),
        threading.Thread(
            target=read_pipe,
            args=(process.stderr, stderr_chunks),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        return_code = process.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    if any(thread.is_alive() for thread in threads):
        raise OSError("verification check output streams did not close")
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    return (
        return_code,
        b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        exceeded.is_set(),
    )
