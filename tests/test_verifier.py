from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.config import (  # noqa: E402
    AppConfig,
    HermesConfig,
    LLMConfig,
    ObsidianConfig,
    OperatorConfig,
    PolicyConfig,
    ServerConfig,
    VerificationCheckConfig,
    VerificationConfig,
)
from hermes_operator.authority import execution_scope_digest  # noqa: E402
from hermes_operator.db import SQLiteStore  # noqa: E402
from hermes_operator.dispatcher import (  # noqa: E402
    MANAGED_INTERNAL_CAPABILITIES,
    dispatch_contract_digest,
)
from hermes_operator.llm import ScriptedLLM  # noqa: E402
from hermes_operator.models import (  # noqa: E402
    Event,
    RunRecord,
    TrustLevel,
    WorkItem,
    WorkStatus,
)
from hermes_operator.prioritization import PriorityEngine  # noqa: E402
from hermes_operator.supervisor import Supervisor  # noqa: E402
from hermes_operator.verifier import (  # noqa: E402
    ArtifactVerifier,
    bind_verification_report,
    validate_verification_contract,
    validate_bound_verification_report,
)


class ArtifactVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _verifier(self, **changes: object) -> ArtifactVerifier:
        values: dict[str, object] = {"artifact_roots": {"workspace": self.root}}
        values.update(changes)
        return ArtifactVerifier(VerificationConfig(**values))

    def test_no_contract_or_native_artifact_is_not_applicable(self) -> None:
        outcome = self._verifier().verify(
            work=WorkItem(title="Text-only result"),
            completion={"raw": {}},
        )

        self.assertFalse(outcome.applicable)
        self.assertTrue(outcome.passed)

    def test_native_artifact_is_scoped_hashed_and_type_checked(self) -> None:
        artifact = self.root / "result.txt"
        artifact.write_text("verified output\n", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        outcome = self._verifier().verify(
            work=WorkItem(title="Produce result"),
            completion={
                "raw": {
                    "metadata": {
                        "artifacts": [
                            {
                                "root": "workspace",
                                "path": "result.txt",
                                "type": "file",
                                "sha256": digest,
                            }
                        ]
                    }
                }
            },
        )

        self.assertTrue(outcome.applicable)
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.artifacts[0]["sha256"], digest)
        self.assertEqual(outcome.artifacts[0]["path"], "result.txt")

    def test_digest_mismatch_and_path_escape_fail_closed(self) -> None:
        artifact = self.root / "result.txt"
        artifact.write_text("changed", encoding="utf-8")
        mismatch = self._verifier().verify(
            work=WorkItem(title="Produce result"),
            completion={
                "artifacts": [
                    {
                        "root": "workspace",
                        "path": "result.txt",
                        "sha256": "0" * 64,
                    }
                ]
            },
        )
        escape = self._verifier().verify(
            work=WorkItem(title="Escape"),
            completion={"artifacts": [{"root": "workspace", "path": "../x"}]},
        )

        self.assertFalse(mismatch.passed)
        self.assertIn("digest mismatch", mismatch.errors[0])
        self.assertFalse(escape.passed)
        self.assertIn("parent traversal", escape.errors[0])

    def test_symlink_and_oversize_artifacts_are_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "link.txt"
        link.symlink_to(outside)
        linked = self._verifier().verify(
            work=WorkItem(title="Link"),
            completion={"artifacts": [{"root": "workspace", "path": "link.txt"}]},
        )
        large = self.root / "large.bin"
        large.write_bytes(b"x" * 5)
        oversized = self._verifier(max_artifact_bytes=4).verify(
            work=WorkItem(title="Large"),
            completion={"artifacts": [{"root": "workspace", "path": "large.bin"}]},
        )

        self.assertFalse(linked.passed)
        self.assertTrue(any("escapes" in value or "symlink" in value for value in linked.errors))
        self.assertFalse(oversized.passed)
        self.assertIn("byte limit", oversized.errors[0])

    def test_canonical_contract_runs_only_named_fixed_check(self) -> None:
        check = VerificationCheckConfig(
            name="unit",
            command=[sys.executable, "-c", "print('ok')"],
            cwd=self.root,
            timeout_seconds=5,
            max_output_bytes=1_000,
        )
        work = WorkItem(
            title="Checked work",
            metadata={"verification_contract": {"artifacts": [], "checks": ["unit"]}},
        )

        outcome = self._verifier(checks=[check]).verify(work=work, completion={})

        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.checks[0]["name"], "unit")
        self.assertEqual(outcome.checks[0]["return_code"], 0)

    def test_failed_or_unknown_contract_check_fails_closed(self) -> None:
        check = VerificationCheckConfig(
            name="unit",
            command=[sys.executable, "-c", "raise SystemExit(3)"],
            cwd=self.root,
            timeout_seconds=5,
            max_output_bytes=1_000,
        )
        failed = self._verifier(checks=[check]).verify(
            work=WorkItem(
                title="Checked work",
                metadata={
                    "verification_contract": {"artifacts": [], "checks": ["unit"]}
                },
            ),
            completion={},
        )
        unknown = self._verifier(checks=[check]).verify(
            work=WorkItem(
                title="Unknown check",
                metadata={
                    "verification_contract": {
                        "artifacts": [],
                        "checks": ["not-configured"],
                    }
                },
            ),
            completion={},
        )

        self.assertFalse(failed.passed)
        self.assertEqual(failed.checks[0]["return_code"], 3)
        self.assertFalse(unknown.passed)
        self.assertIn("not configured", unknown.errors[0])

    def test_pre_dispatch_contract_validation_rejects_worker_selected_checks(self) -> None:
        config = VerificationConfig(artifact_roots={"workspace": self.root})
        with self.assertRaisesRegex(ValueError, "unconfigured checks"):
            validate_verification_contract(
                {"artifacts": [], "checks": ["worker-choice"]},
                config,
            )
        with self.assertRaisesRegex(ValueError, "root is unknown"):
            validate_verification_contract(
                {
                    "artifacts": [
                        {"root": "unknown", "path": "result.txt", "type": "file"}
                    ],
                    "checks": [],
                },
                config,
            )

    def test_directory_digest_is_stable_and_rejects_special_entries(self) -> None:
        directory = self.root / "bundle"
        directory.mkdir()
        (directory / "a.txt").write_text("a", encoding="utf-8")
        nested = directory / "nested"
        nested.mkdir()
        (nested / "b.txt").write_text("b", encoding="utf-8")
        verifier = self._verifier()

        first = verifier.verify(
            work=WorkItem(title="Bundle"),
            completion={
                "artifacts": [
                    {"root": "workspace", "path": "bundle", "type": "directory"}
                ]
            },
        )
        second = verifier.verify(
            work=WorkItem(title="Bundle"),
            completion={"artifacts": [{"root": "workspace", "path": "bundle"}]},
        )

        self.assertTrue(first.passed)
        self.assertEqual(first.artifacts[0]["files"], 2)
        self.assertEqual(
            first.artifacts[0]["sha256"],
            second.artifacts[0]["sha256"],
        )

    def test_bound_report_detects_artifact_and_binding_tampering(self) -> None:
        artifact = self.root / "bound.txt"
        artifact.write_text("bound output\n", encoding="utf-8")
        outcome = self._verifier().verify(
            work=WorkItem(title="Bound report"),
            completion={
                "artifacts": [
                    {"root": "workspace", "path": "bound.txt", "type": "file"}
                ]
            },
        )
        binding = {
            "schema": "hermes-operator.completion-verification.v1",
            "work_id": "wrk_bound",
            "work_version": 3,
            "run_id": "run_bound",
        }
        report = bind_verification_report(outcome.to_dict(), binding)

        validated = validate_bound_verification_report(report, binding)

        self.assertEqual(validated["report_digest"], report["report_digest"])
        tampered = dict(report)
        tampered["artifacts"] = [dict(report["artifacts"][0])]
        tampered["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "artifact binding mismatch"):
            validate_bound_verification_report(tampered, binding)
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            validate_bound_verification_report(
                report,
                {**binding, "work_version": 4},
            )


class DeterministicCompletionGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = SQLiteStore(self.root / "operator.db")
        self.store.initialize()
        self.config = AppConfig(
            config_path=self.root / "operator.toml",
            operator=OperatorConfig(
                instance_id="test-verifier",
                database_path=self.root / "operator.db",
                data_dir=self.root,
                autonomy_mode="shadow",
            ),
            llm=LLMConfig(provider="command"),
            hermes=HermesConfig(enabled=True),
            obsidian=ObsidianConfig(),
            server=ServerConfig(enabled=False),
            policy=PolicyConfig(),
            verification=VerificationConfig(
                artifact_roots={"workspace": self.root}
            ),
        )

    def _immutable_run_result(
        self,
        item: WorkItem,
        completion: dict,
    ) -> dict:
        profile = str(item.assignee or self.config.hermes.default_assignee)
        skills: list[str] = []
        goal_mode = self.config.hermes.goal_mode
        return {
            "execution_contract": {
                "schema": "hermes-operator.run-execution-contract.v1",
                "dispatch_contract_digest": dispatch_contract_digest(
                    item,
                    profile=profile,
                    skills=skills,
                    default_skills=self.config.hermes.default_skills,
                    goal_mode=goal_mode,
                ),
                "execution_scope_digest": execution_scope_digest(
                    item,
                    profile=profile,
                    skills=skills,
                    default_skills=self.config.hermes.default_skills,
                    goal_mode=goal_mode,
                ),
                "scope_revision": item.authorization_scope_revision,
                "work_version": item.version,
                "profile": profile,
                "skills": skills,
                "default_skills": list(self.config.hermes.default_skills),
                "goal_mode": goal_mode,
                "internal_capabilities": list(MANAGED_INTERNAL_CAPABILITIES),
                "verification_requirement": item.metadata.get(
                    "verification_requirement",
                    "model_evidence",
                ),
                "captured_at": "2026-07-15T00:00:00+00:00",
            },
            "completion": completion,
        }

    @staticmethod
    def _completion_contract_payload(run: RunRecord) -> dict:
        contract = run.result["execution_contract"]
        return {
            "dispatch_contract_digest": contract["dispatch_contract_digest"],
            "execution_scope_digest": contract["execution_scope_digest"],
            "scope_revision": contract["scope_revision"],
            "work_version": contract["work_version"],
            "profile": contract["profile"],
            "internal_capabilities": contract["internal_capabilities"],
            "verification_requirement": contract[
                "verification_requirement"
            ],
        }

    async def test_failed_artifact_gate_overrides_model_passed_verdict(self) -> None:
        item = WorkItem(
            title="Produce required artifact",
            status=WorkStatus.REVIEW,
            assignee="operator",
            hermes_task_id="task-artifact",
            acceptance_criteria=["The artifact exists"],
            metadata={
                "verification_contract": {
                    "artifacts": [
                        {
                            "root": "workspace",
                            "path": "missing.txt",
                            "type": "file",
                        }
                    ],
                    "checks": [],
                },
                "hermes": {
                    "completion_fingerprint": "artifact-evidence",
                    "completion_run_id": "",
                    "completion_attempt": 1,
                },
                "governance": {
                    "source_trust": "operator",
                    "creation_authorized": True,
                    "execution_authorized": True,
                },
            },
        )
        self.store.create_work(item)
        run = RunRecord(
            work_item_id=item.id,
            runner="hermes-kanban",
            external_run_id="task-artifact",
            status="completed",
            result=self._immutable_run_result(
                item,
                {"updated_at": "artifact-evidence", "raw": {}},
            ),
        )
        item.metadata["hermes"]["completion_run_id"] = run.id
        self.store.update_work(
            item.id,
            {"metadata": item.metadata},
            expected_version=item.version,
        )
        item = self.store.get_work(item.id)
        self.store.create_run(run)
        event = Event(
            source="hermes",
            external_id="task-artifact",
            event_type="execution.completed",
            payload={
                "work_id": item.id,
                "hermes_task_id": "task-artifact",
                "run_id": run.id,
                "attempt": 1,
                "evidence_fingerprint": "artifact-evidence",
                **self._completion_contract_payload(run),
                "execution_evidence": run.result["completion"],
            },
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            provenance={"adapter": "hermes-kanban"},
        )
        self.store.enqueue_event(event)
        events = self.store.claim_events("test-verifier", 1, 60)
        PriorityEngine().rescore_store(self.store)
        current = self.store.get_work(item.id)
        plan = {
            "summary": "Assessed completion",
            "observations": [],
            "event_dispositions": [
                {
                    "event_id": event.id,
                    "disposition": "execution_reconciled",
                    "reason": "Completion evidence was assessed",
                    "related_work_ids": [item.id],
                    "related_work_refs": [],
                }
            ],
            "work_operations": [],
            "questions": [],
            "dispatch": [],
            "memory_candidates": [],
            "verifications": [
                {
                    "work_id": item.id,
                    "expected_version": current.version,
                    "verdict": "passed",
                    "confidence": 0.99,
                    "summary": "Model considered the prose sufficient",
                    "criteria_results": [
                        {
                            "criterion": "The artifact exists",
                            "passed": True,
                            "evidence": "Worker claimed an artifact",
                        }
                    ],
                }
            ],
            "external_action_proposals": [],
        }
        supervisor = Supervisor(
            config=self.config,
            store=self.store,
            llm=ScriptedLLM([plan]),
            priority_engine=PriorityEngine(),
        )

        result = await supervisor.run_pass(trigger="event", events=events)

        completed = self.store.get_work(item.id)
        self.assertEqual(completed.status, WorkStatus.BLOCKED)
        self.assertEqual(result.verified_work_ids, [])
        verification = completed.metadata["last_verification"]
        self.assertEqual(verification["requested_verdict"], "passed")
        self.assertEqual(verification["verdict"], "failed")
        self.assertFalse(verification["deterministic"]["passed"])

    async def test_required_deterministic_report_cannot_be_non_applicable(self) -> None:
        item = WorkItem(
            title="Never accept an inapplicable required check",
            status=WorkStatus.REVIEW,
            assignee="operator",
            hermes_task_id="task-required-check",
            acceptance_criteria=["The deployment check passes"],
            metadata={
                "verification_requirement": "deterministic_required",
                "hermes": {
                    "completion_fingerprint": "required-check-evidence",
                    "completion_run_id": "",
                    "completion_attempt": 1,
                },
                "governance": {
                    "source_trust": "operator",
                    "creation_authorized": True,
                    "execution_authorized": True,
                },
            },
        )
        self.store.create_work(item)
        run = RunRecord(
            work_item_id=item.id,
            runner="hermes-kanban",
            external_run_id="task-required-check",
            status="completed",
            result=self._immutable_run_result(
                item,
                {"updated_at": "required-check-evidence"},
            ),
        )
        item.metadata["hermes"]["completion_run_id"] = run.id
        self.store.update_work(
            item.id,
            {"metadata": item.metadata},
            expected_version=item.version,
        )
        self.store.create_run(run)
        event = Event(
            source="hermes",
            external_id="task-required-check",
            event_type="execution.completed",
            payload={
                "work_id": item.id,
                "hermes_task_id": "task-required-check",
                "run_id": run.id,
                "attempt": 1,
                "evidence_fingerprint": "required-check-evidence",
                **self._completion_contract_payload(run),
            },
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            provenance={"adapter": "hermes-kanban"},
        )
        self.store.enqueue_event(event)
        events = self.store.claim_events("required-verifier", 1, 60)
        PriorityEngine().rescore_store(self.store)
        current = self.store.get_work(item.id)
        plan = {
            "summary": "Assessed the required deterministic completion",
            "observations": [],
            "event_dispositions": [
                {
                    "event_id": event.id,
                    "disposition": "execution_reconciled",
                    "reason": "The required report was assessed",
                    "related_work_ids": [item.id],
                    "related_work_refs": [],
                }
            ],
            "work_operations": [],
            "questions": [],
            "dispatch": [],
            "memory_candidates": [],
            "verifications": [
                {
                    "work_id": item.id,
                    "expected_version": current.version,
                    "verdict": "passed",
                    "confidence": 0.99,
                    "summary": "The model requested completion",
                    "criteria_results": [
                        {
                            "criterion": "The deployment check passes",
                            "passed": True,
                            "evidence": "The worker claimed success",
                        }
                    ],
                }
            ],
            "external_action_proposals": [],
        }

        result = await Supervisor(
            config=self.config,
            store=self.store,
            llm=ScriptedLLM([plan]),
            priority_engine=PriorityEngine(),
        ).run_pass(trigger="event", events=events)

        completed = self.store.get_work(item.id)
        self.assertEqual(completed.status, WorkStatus.BLOCKED)
        self.assertEqual(result.verified_work_ids, [])
        verification = completed.metadata["last_verification"]
        self.assertEqual(
            verification["verification_requirement"],
            "deterministic_required",
        )
        self.assertFalse(verification["deterministic"]["applicable"])
        self.assertTrue(verification["deterministic"]["passed"])
        self.assertEqual(verification["verdict"], "failed")

    async def test_check_runs_once_without_blocking_lease_or_api_writer(self) -> None:
        check = VerificationCheckConfig(
            name="unit",
            command=[sys.executable, "-c", "print('ok')"],
            cwd=self.root,
            timeout_seconds=5,
            max_output_bytes=1_000,
        )
        item = WorkItem(
            title="Run one nonblocking check",
            status=WorkStatus.REVIEW,
            assignee="operator",
            hermes_task_id="task-nonblocking",
            acceptance_criteria=["The fixed check passes"],
            metadata={
                "verification_requirement": "deterministic_required",
                "verification_contract": {
                    "artifacts": [],
                    "checks": ["unit"],
                },
                "hermes": {
                    "completion_fingerprint": "nonblocking-evidence",
                    "completion_run_id": "",
                    "completion_attempt": 1,
                },
                "governance": {
                    "source_trust": "operator",
                    "creation_authorized": True,
                    "execution_authorized": True,
                },
            },
        )
        self.store.create_work(item)
        run = RunRecord(
            work_item_id=item.id,
            runner="hermes-kanban",
            external_run_id="task-nonblocking",
            status="completed",
            result=self._immutable_run_result(
                item,
                {"updated_at": "nonblocking-evidence"},
            ),
        )
        item.metadata["hermes"]["completion_run_id"] = run.id
        self.store.update_work(
            item.id,
            {"metadata": item.metadata},
            expected_version=item.version,
        )
        item = self.store.get_work(item.id)
        self.store.create_run(run)
        event = Event(
            source="hermes",
            external_id="task-nonblocking",
            event_type="execution.completed",
            payload={
                "work_id": item.id,
                "hermes_task_id": "task-nonblocking",
                "run_id": run.id,
                "attempt": 1,
                "evidence_fingerprint": "nonblocking-evidence",
                **self._completion_contract_payload(run),
            },
            trust_level=TrustLevel.AUTHENTICATED_UNTRUSTED,
            provenance={"adapter": "hermes-kanban"},
        )
        self.store.enqueue_event(event)
        events = self.store.claim_events("test-verifier", 1, 60)
        PriorityEngine().rescore_store(self.store)
        current = self.store.get_work(item.id)
        plan = {
            "summary": "Assessed one fixed check",
            "observations": [],
            "event_dispositions": [
                {
                    "event_id": event.id,
                    "disposition": "execution_reconciled",
                    "reason": "The completion and fixed check were assessed",
                    "related_work_ids": [item.id],
                    "related_work_refs": [],
                }
            ],
            "work_operations": [],
            "questions": [],
            "dispatch": [],
            "memory_candidates": [],
            "verifications": [
                {
                    "work_id": item.id,
                    "expected_version": current.version,
                    "verdict": "passed",
                    "confidence": 0.99,
                    "summary": "The deployment-owned check passed",
                    "criteria_results": [
                        {
                            "criterion": "The fixed check passes",
                            "passed": True,
                            "evidence": "The named fixed check returned zero",
                        }
                    ],
                }
            ],
            "external_action_proposals": [],
        }
        entered = threading.Event()
        release = threading.Event()

        class BlockingVerifier:
            def __init__(self) -> None:
                self.delegate = ArtifactVerifier(
                    VerificationConfig(
                        artifact_roots={"workspace": self_root},
                        checks=[check],
                    )
                )
                self.calls = 0

            def verify(self, *, work: WorkItem, completion: dict):
                self.calls += 1
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release deterministic verifier")
                return self.delegate.verify(work=work, completion=completion)

        self_root = self.root
        blocking = BlockingVerifier()
        supervisor = Supervisor(
            config=self.config,
            store=self.store,
            llm=ScriptedLLM([plan]),
            priority_engine=PriorityEngine(),
        )
        supervisor.verifier = blocking  # type: ignore[assignment]
        epoch = self.store.acquire_service_lease(
            "verification-test",
            "owner",
            ttl_seconds=60,
        )
        assert epoch is not None
        supervisor_task = asyncio.create_task(
            supervisor.run_pass(trigger="event", events=events)
        )
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            renewed = await asyncio.wait_for(
                asyncio.to_thread(
                    self.store.renew_service_lease,
                    "verification-test",
                    "owner",
                    ttl_seconds=60,
                    epoch=epoch,
                ),
                timeout=1,
            )
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.store.set_state,
                    "api.concurrent-write",
                    {"accepted": True},
                ),
                timeout=1,
            )
            self.assertTrue(renewed)
            self.assertEqual(
                self.store.get_state("api.concurrent-write"),
                {"accepted": True},
            )
        finally:
            release.set()
        result = await asyncio.wait_for(supervisor_task, timeout=5)

        assert result is not None
        self.assertEqual(blocking.calls, 1)
        self.assertEqual(result.verified_work_ids, [item.id])
        deterministic = self.store.get_work(item.id).metadata["last_verification"][
            "deterministic"
        ]
        self.assertEqual(len(deterministic["checks"]), 1)
        self.assertIn("completion_binding_digest", deterministic["binding"])
        self.assertEqual(
            self.store.get_work(item.id).metadata["last_verification"][
                "verification_requirement"
            ],
            "deterministic_required",
        )


if __name__ == "__main__":
    unittest.main()
