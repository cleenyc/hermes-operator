from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_operator.adapters import (
    AdapterResponseError,
    AdapterUnavailableError,
    HermesCLIAdapter,
    InMemoryHermesAdapter,
    ObsidianAdapter,
    UnsafePathError,
    discover_vault,
)


class FakeHermesRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.task = {
            "id": "t_123",
            "title": "Fallback",
            "status": "blocked",
            "body": "body",
            "assignee": "researcher",
        }

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[-1] == "--version" and "kanban" not in argv:
            return subprocess.CompletedProcess(argv, 0, "Hermes Agent 0.18.2\n", "")
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(
                argv,
                0,
                "commands: create show list comment block unblock runs diagnostics\n",
                "",
            )

        action_index = argv.index("kanban") + 1
        if argv[action_index] == "--board":
            action_index += 2
        action = argv[action_index]
        if action == "create":
            title = argv[action_index + 1]
            self.task = {
                "task_id": "t_123",
                "title": title,
                "status": "ready",
                "body": argv[argv.index("--body") + 1] if "--body" in argv else "",
                "assignee": (
                    argv[argv.index("--assignee") + 1] if "--assignee" in argv else None
                ),
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.task), "")
        if action == "show":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"task": self.task}), ""
            )
        if action == "list":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"tasks": [self.task]}), ""
            )
        if action == "comment":
            self.task = {
                **self.task,
                "comments": [{"body": argv[action_index + 2]}],
            }
            return subprocess.CompletedProcess(argv, 0, "Comment added\n", "")
        if action == "unblock":
            self.task = {**self.task, "status": "ready"}
            return subprocess.CompletedProcess(argv, 0, "Unblocked\n", "")
        if action == "block":
            self.task = {**self.task, "status": "blocked"}
            return subprocess.CompletedProcess(argv, 0, "Blocked\n", "")
        if action == "runs":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"runs": []}), "")
        raise AssertionError(f"Unexpected fake command: {argv}")


class HermesCLIAdapterTests(unittest.TestCase):
    def test_cli_uses_safe_argv_and_configured_profile_and_board(self) -> None:
        runner = FakeHermesRunner()
        adapter = HermesCLIAdapter(
            binary="/opt/hermes bin/hermes",
            profile="operator",
            board="personal",
            runner=runner,
        )
        malicious_title = 'Review $(touch /tmp/nope); echo "unsafe"'

        task = adapter.create_task(
            title=malicious_title,
            description="Acceptance criteria",
            assignee="researcher",
            priority=4,
            idempotency_key="source:123",
            scheduled_at="2026-07-14T09:00:00Z",
            metadata={
                "skills": ["kanban-orchestrator", "research"],
                "goal_mode": True,
            },
        )

        self.assertEqual(task.title, malicious_title)
        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv[:7],
            [
                "/opt/hermes bin/hermes",
                "-p",
                "operator",
                "kanban",
                "--board",
                "personal",
                "create",
            ],
        )
        self.assertIn(malicious_title, argv)
        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--skill"],
            ["kanban-orchestrator", "research"],
        )
        self.assertIn("--goal", argv)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["text"])

    def test_health_and_capability_detection(self) -> None:
        runner = FakeHermesRunner()
        adapter = HermesCLIAdapter(runner=runner)

        health = adapter.health()

        self.assertTrue(health.available)
        self.assertEqual(health.version, "0.18.2")
        self.assertEqual(
            set(health.capabilities),
            {"create", "show", "list", "comment", "block", "unblock", "runs"},
        )

    def test_show_list_comment_and_unblock(self) -> None:
        runner = FakeHermesRunner()
        adapter = HermesCLIAdapter(board="work", runner=runner)
        adapter.create_task(title="Research", assignee="researcher")

        self.assertEqual(adapter.show_task("t_123").id, "t_123")
        self.assertEqual(len(adapter.list_tasks(status="ready", limit=1)), 1)
        commented = adapter.comment_task("t_123", "Use primary sources")
        self.assertEqual(commented.comments[0]["body"], "Use primary sources")
        self.assertEqual(adapter.unblock_task("t_123").status, "ready")

        comment_call = next(argv for argv, _ in runner.calls if "comment" in argv)
        self.assertEqual(comment_call[-3:], ["comment", "t_123", "Use primary sources"])
        self.assertNotIn("--json", comment_call)

    def test_invalid_json_is_reported(self) -> None:
        def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "not json", "")

        with self.assertRaises(AdapterResponseError):
            HermesCLIAdapter(runner=runner).show_task("t_bad")

    def test_child_environment_excludes_control_plane_secrets_by_default(self) -> None:
        runner = FakeHermesRunner()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "llm-secret",
                "HERMES_OPERATOR_API_TOKEN": "admin-secret",
                "HERMES_OPERATOR_BRIDGE_TOKEN": "bridge-secret",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            HermesCLIAdapter(
                runner=runner,
                env={"EXPLICIT_WORKER_VALUE": "allowed"},
            ).health()

        child_env = runner.calls[0][1]["env"]
        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertEqual(child_env["EXPLICIT_WORKER_VALUE"], "allowed")
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("HERMES_OPERATOR_API_TOKEN", child_env)
        self.assertNotIn("HERMES_OPERATOR_BRIDGE_TOKEN", child_env)

    def test_terminate_uses_authenticated_native_run_control(self) -> None:
        runner = FakeHermesRunner()
        runner.task = {
            **runner.task,
            "status": "running",
            "current_run_id": "run_7",
        }
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                self.limit = limit
                return b"{}"

        class Opener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return Response()

        adapter = HermesCLIAdapter(
            runner=runner,
            control_base_url="http://127.0.0.1:8000",
            control_token="control-secret",
            control_timeout_seconds=7,
        )
        with patch(
            "hermes_operator.adapters.hermes.urlrequest.build_opener",
            return_value=Opener(),
        ):
            task = adapter.terminate_task("t_123")

        self.assertEqual(task.id, "t_123")
        request, timeout = requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8000/api/plugins/kanban/runs/run_7/terminate",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer control-secret")
        self.assertEqual(timeout, 7)

    def test_terminate_fails_closed_without_control_transport(self) -> None:
        runner = FakeHermesRunner()
        runner.task = {
            **runner.task,
            "status": "running",
            "current_run_id": "run_7",
        }
        with self.assertRaises(AdapterUnavailableError):
            HermesCLIAdapter(runner=runner).terminate_task("t_123")

    def test_terminate_fails_closed_when_running_task_has_no_run_identity(self) -> None:
        runner = FakeHermesRunner()
        runner.task = {
            **runner.task,
            "status": "running",
        }
        with self.assertRaisesRegex(AdapterUnavailableError, "no native run id"):
            HermesCLIAdapter(
                runner=runner,
                control_base_url="http://127.0.0.1:8000",
                control_token="control-secret",
            ).terminate_task("t_123")


class InMemoryHermesAdapterTests(unittest.TestCase):
    def test_idempotency_filters_comments_and_defensive_copies(self) -> None:
        adapter = InMemoryHermesAdapter()
        first = adapter.create_task(
            title="Draft brief",
            assignee="writer",
            idempotency_key="mail:42",
            metadata={"source": "email"},
        )
        repeated = adapter.create_task(
            title="Different title",
            assignee="writer",
            idempotency_key="mail:42",
        )

        self.assertEqual(first.id, repeated.id)
        first.title = "mutated by caller"
        self.assertEqual(adapter.show_task(repeated.id).title, "Draft brief")
        self.assertEqual(adapter.list_tasks(status="ready", assignee="writer")[0].id, first.id)
        commented = adapter.comment_task(first.id, "Approved context")
        self.assertEqual(commented.comments[0]["body"], "Approved context")
        self.assertEqual(adapter.unblock_task(first.id).status, "ready")


class ObsidianAdapterTests(unittest.TestCase):
    def test_disabled_adapter_is_a_graceful_noop_and_can_be_configured_later(self) -> None:
        adapter = ObsidianAdapter(
            env={},
            include_default_candidates=False,
        )

        result = adapter.project_note({"id": "n1", "title": "A note"})
        self.assertFalse(adapter.enabled)
        self.assertFalse(result.enabled)
        self.assertIn("No Obsidian vault", result.reason or "")

        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(adapter.configure(directory))
            projected = adapter.project_note({"id": "n1", "title": "A note"})
            self.assertTrue(projected.created)

    def test_env_and_candidate_discovery_are_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configured"
            configured.mkdir()
            self.assertEqual(
                discover_vault(
                    env={"OBSIDIAN_VAULT_PATH": str(configured)},
                    include_defaults=False,
                ),
                configured.resolve(),
            )

            parent = root / "vaults"
            vault = parent / "only-vault"
            (vault / ".obsidian").mkdir(parents=True)
            self.assertEqual(
                discover_vault(
                    candidates=[parent], env={}, include_defaults=False
                ),
                vault.resolve(),
            )

            other = parent / "other-vault"
            (other / ".obsidian").mkdir(parents=True)
            self.assertIsNone(
                discover_vault(
                    candidates=[parent], env={}, include_defaults=False
                )
            )

    def test_projection_merges_frontmatter_and_preserves_human_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ObsidianAdapter(directory)
            result = adapter.project_project(
                {
                    "id": "project-17",
                    "title": "Operator Build",
                    "status": "active",
                    "summary": "Initial scope",
                }
            )
            self.assertTrue(result.created)
            assert result.path is not None

            initial = result.path.read_text(encoding="utf-8")
            result.path.write_text(
                initial.replace("---\n", "---\ncustom:\n  nested: true\n", 1)
                + "\nHuman-maintained observations.\n",
                encoding="utf-8",
            )
            updated = adapter.project_project(
                {
                    "id": "project-17",
                    "title": "Operator Build",
                    "status": "blocked",
                    "summary": "Revised scope",
                }
            )

            self.assertTrue(updated.updated)
            text = result.path.read_text(encoding="utf-8")
            self.assertIn("custom:\n  nested: true", text)
            self.assertIn("Human-maintained observations.", text)
            self.assertIn('status: "blocked"', text)
            self.assertIn("Revised scope", text)
            self.assertNotIn("Initial scope\n<!-- hermes-operator:project:end", text)

            no_change = adapter.project_project(
                {
                    "id": "project-17",
                    "title": "Operator Build",
                    "status": "blocked",
                    "summary": "Revised scope",
                }
            )
            self.assertFalse(no_change.changed)

    def test_project_event_has_deterministic_path_and_parseable_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ObsidianAdapter(directory)
            result = adapter.project_event(
                {
                    "event_id": "gmail:abc/123",
                    "title": "Follow up",
                    "occurred_at": "2026-07-13T18:30:00-04:00",
                    "content": "A proposed next action.",
                }
            )
            assert result.path is not None
            self.assertEqual(result.path.name, "2026-07-13-gmail-abc-123.md")
            document = adapter.read_document("Events/2026-07-13-gmail-abc-123.md")
            assert document is not None
            self.assertEqual(document.frontmatter["kind"], "event")
            self.assertEqual(document.frontmatter["event_id"], "gmail:abc/123")
            self.assertIn("A proposed next action.", document.body)

    def test_path_containment_blocks_traversal_absolute_paths_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            adapter = ObsidianAdapter(directory)
            with self.assertRaises(UnsafePathError):
                adapter.project_note(
                    {"title": "escape"}, relative_path="../escape.md"
                )
            with self.assertRaises(UnsafePathError):
                adapter.project_note(
                    {"title": "absolute"}, relative_path="/tmp/escape.md"
                )

            link = Path(directory) / "linked"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(UnsafePathError):
                adapter.project_note(
                    {"title": "symlink"}, relative_path="linked/escape.md"
                )
            self.assertFalse((Path(outside) / "escape.md").exists())


if __name__ == "__main__":
    unittest.main()
