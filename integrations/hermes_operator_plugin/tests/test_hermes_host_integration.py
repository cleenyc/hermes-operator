"""Optional integration contract against the pinned real Hermes installation.

Normal source-only test runs skip this module when Hermes is not installed.  Release
CI installs the pinned host and sets HERMES_OPERATOR_REQUIRE_HOST_INTEGRATION=1, which
turns a missing or mismatched host into a failure instead of a skip.
"""

from __future__ import annotations

import asyncio
from importlib import import_module, metadata
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch


PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

compatibility = import_module("hermes_operator_plugin.compatibility")
policy = import_module("hermes_operator_plugin.policy")


def _installed_hermes_version() -> str | None:
    for distribution in ("hermes-agent", "hermes_agent", "hermes-cli"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return None


class PinnedHermesHostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        installed = _installed_hermes_version()
        required = os.getenv("HERMES_OPERATOR_REQUIRE_HOST_INTEGRATION") == "1"
        if installed != compatibility.SUPPORTED_HERMES_VERSION:
            message = (
                "real-host tests require hermes-agent=="
                f"{compatibility.SUPPORTED_HERMES_VERSION}; found {installed or 'none'}"
            )
            if required:
                raise AssertionError(message)
            raise unittest.SkipTest(message)

    def test_real_dispatcher_marker_is_canonical_across_quiet_turn_uuid(self):
        turn_context = import_module("agent.turn_context")
        host_plugins = import_module("hermes_cli.plugins")
        kanban_db = import_module("hermes_cli.kanban_db")
        host_profiles = import_module("hermes_cli.profiles")
        source = inspect.getsource(turn_context.build_turn_context)
        self.assertIn("effective_task_id = task_id or str(uuid.uuid4())", source)
        self.assertEqual(
            compatibility._managed_worker_identity_semantics(),
            "dispatcher_environment",
        )

        # Run the pinned host's real launcher up to its process boundary and
        # capture the exact argv/environment it would give the quiet worker.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            task = kanban_db.Task(
                id="task-1",
                title="Managed launch",
                body=None,
                assignee="operator",
                status="doing",
                priority=0,
                created_by="test",
                created_at=0,
                started_at=0,
                completed_at=None,
                workspace_kind="dir",
                workspace_path=str(workspace),
                claim_lock="claim-1",
                claim_expires=None,
                tenant=None,
            )
            with (
                patch.dict(
                    os.environ,
                    {"HOME": str(root), "HERMES_HOME": str(root / "home")},
                    clear=True,
                ),
                patch.object(
                    host_profiles,
                    "resolve_profile_env",
                    return_value=str(root / "profile-home"),
                ),
                patch.object(
                    kanban_db, "_resolve_hermes_argv", return_value=["hermes"]
                ),
                patch.object(
                    kanban_db, "_resolve_worker_cli_toolsets", return_value=[]
                ),
                patch.object(
                    kanban_db, "kanban_db_path", return_value=root / "kanban.db"
                ),
                patch.object(
                    kanban_db, "workspaces_root", return_value=root / "workspaces"
                ),
                patch.object(
                    kanban_db, "worker_logs_dir", return_value=root / "logs"
                ),
                patch.object(kanban_db, "get_current_board", return_value="default"),
                patch("subprocess.Popen") as popen,
            ):
                popen.return_value.pid = 4242
                self.assertEqual(
                    kanban_db._default_spawn(task, str(workspace)),
                    4242,
                )
            launch_argv = popen.call_args.args[0]
            launch_environment = dict(popen.call_args.kwargs["env"])
            popen.call_args.kwargs["stdout"].close()
            self.assertEqual(
                launch_argv[-3:],
                ["chat", "-q", "work kanban task task-1"],
            )
            self.assertEqual(launch_environment["HERMES_KANBAN_TASK"], "task-1")
            self.assertEqual(launch_environment["HERMES_PROFILE"], "operator")

        # Exercise the plugin through the real host's directive resolver with the same
        # UUID-shaped effective id generated by its turn builder. No dispatcher marker
        # means the invocation must stay native.
        ordinary_id = str(uuid.uuid4())
        contract = {
            "authorized": True,
            "task_id": "task-1",
            "work_id": "wrk_1",
            "profile": "operator",
            "contract_digest": "a" * 64,
            "run_id": "run_1",
            "internal_capabilities": ["local_test"],
        }
        contract_lookups = []
        guard = policy.TaskScopedPolicyGuard(
            lambda task_id: contract_lookups.append(task_id) or contract,
            expected_profile="operator",
            delegation_mode="background",
        )
        manager = host_plugins.get_plugin_manager()
        previous = list(manager._hooks.get("pre_tool_call", []))
        manager._hooks["pre_tool_call"] = [guard]
        try:
            with patch.dict(os.environ, {}, clear=True):
                native = host_plugins._get_pre_tool_call_directive_details(
                    "mcp_google_gmail_search",
                    {"query": "is:unread"},
                    task_id=ordinary_id,
                )
                approval = host_plugins._get_pre_tool_call_directive_details(
                    "mcp_google_gmail_send_email",
                    {"to": "operator@example.com"},
                    task_id=ordinary_id,
                )
            self.assertIsNone(native.action)
            self.assertEqual(approval.action, "approve")

            with patch.dict(os.environ, launch_environment, clear=True):
                quiet_turn_id = str(uuid.uuid4())
                self.assertNotEqual(quiet_turn_id, task.id)
                managed = host_plugins._get_pre_tool_call_directive_details(
                    "terminal",
                    {"command": "pytest -q"},
                    task_id=quiet_turn_id,
                )
            self.assertIsNone(managed.action)
            self.assertEqual(contract_lookups, ["task-1"])
        finally:
            manager._hooks["pre_tool_call"] = previous

    def test_real_host_pre_tool_resolution_contract_is_first_valid(self):
        self.assertEqual(compatibility._pre_tool_semantics(), "first_valid")
        self.assertEqual(
            compatibility._managed_worker_identity_semantics(),
            "dispatcher_environment",
        )

    def test_real_host_blocks_completion_prose_before_artifact_promotion(self):
        host_plugins = import_module("hermes_cli.plugins")
        kanban_db = import_module("hermes_cli.kanban_db")
        model_tools = import_module("model_tools")
        watchers = import_module("gateway.kanban_watchers")
        platform_base = import_module("gateway.platforms.base")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspaces = root / "workspaces"
            workspace = workspaces / "worker"
            workspace.mkdir(parents=True)
            artifact = workspace / "report.pdf"
            artifact.write_bytes(b"report")
            outside = root / "outside" / "report.pdf"
            outside.parent.mkdir()
            outside.write_bytes(b"outside")
            workspace_link = root / "worker-link"
            workspace_link.symlink_to(workspace, target_is_directory=True)
            connection = kanban_db.connect(root / "kanban.db")
            manager = host_plugins.get_plugin_manager()
            previous = list(manager._hooks.get("pre_tool_call", []))
            try:
                unguarded_task_id = kanban_db.create_task(
                    connection,
                    title="Unguarded report control",
                    workspace_kind="dir",
                    workspace_path=str(workspace),
                )
                kanban_db.add_notify_sub(
                    connection,
                    task_id=unguarded_task_id,
                    platform="test",
                    chat_id="private-chat",
                )
                base_environment = {
                    "HOME": str(root),
                    "HERMES_HOME": str(root / "hermes-home"),
                    "HERMES_KANBAN_DB": str(root / "kanban.db"),
                    "HERMES_KANBAN_WORKSPACE": str(workspace),
                    "HERMES_KANBAN_WORKSPACES_ROOT": str(workspaces),
                    "HERMES_MEDIA_DELIVERY_STRICT": "0",
                }

                # First prove the pinned host's complete-task and Gateway path
                # turns this prose into a native document delivery when no guard
                # vetoes the tool call.
                manager._hooks["pre_tool_call"] = []
                with patch.dict(
                    os.environ,
                    {**base_environment, "HERMES_KANBAN_TASK": unguarded_task_id},
                    clear=True,
                ):
                    model_tools.handle_function_call(
                        "kanban_complete",
                        {
                            "task_id": unguarded_task_id,
                            "summary": f"Created {artifact}.",
                        },
                        task_id=unguarded_task_id,
                    )
                    _, events = kanban_db.unseen_events_for_sub(
                        connection,
                        task_id=unguarded_task_id,
                        platform="test",
                        chat_id="private-chat",
                        kinds=["completed"],
                    )
                    self.assertEqual(len(events), 1)
                    self.assertIn(str(artifact), events[0].payload["summary"])

                    class RecordingAdapter:
                        extract_local_files = staticmethod(
                            platform_base.BasePlatformAdapter.extract_local_files
                        )

                        def __init__(self):
                            self.documents = []

                        async def send_document(
                            self, *, chat_id, file_path, metadata
                        ):
                            self.documents.append((chat_id, file_path, metadata))

                        async def send_multiple_images(self, **_kwargs):
                            raise AssertionError("PDF must not use image delivery")

                        async def send_video(self, **_kwargs):
                            raise AssertionError("PDF must not use video delivery")

                    adapter = RecordingAdapter()
                    asyncio.run(
                        watchers.GatewayKanbanWatchersMixin()._deliver_kanban_artifacts(
                            adapter=adapter,
                            chat_id="private-chat",
                            metadata={},
                            event_payload=events[0].payload,
                            task=kanban_db.get_task(connection, unguarded_task_id),
                        )
                    )
                    self.assertEqual(
                        adapter.documents,
                        [("private-chat", str(artifact.resolve()), {})],
                    )

                managed_task_id = kanban_db.create_task(
                    connection,
                    title="Managed report",
                    workspace_kind="dir",
                    workspace_path=str(workspace),
                )
                contract = {
                    "authorized": True,
                    "task_id": managed_task_id,
                    "work_id": "wrk_1",
                    "profile": "operator",
                    "contract_digest": "a" * 64,
                    "run_id": "run_1",
                    "internal_capabilities": ["local_read"],
                }
                guard = policy.TaskScopedPolicyGuard(
                    lambda requested: contract if requested == managed_task_id else {},
                    lambda _task, _count: {},
                    expected_profile="operator",
                    delegation_mode="background",
                )
                manager._hooks["pre_tool_call"] = [guard]
                with patch.dict(
                    os.environ,
                    {**base_environment, "HERMES_KANBAN_TASK": managed_task_id},
                    clear=True,
                ):
                    blocked = json.loads(
                        model_tools.handle_function_call(
                            "kanban_complete",
                            {
                                "task_id": managed_task_id,
                                "summary": f"Created {artifact}.",
                            },
                            task_id=str(uuid.uuid4()),
                        )
                    )
                self.assertIn("error", blocked)
                self.assertIn("Operator policy", blocked["error"])
                self.assertEqual(
                    kanban_db.get_task(connection, managed_task_id).status,
                    "ready",
                )
                self.assertFalse(
                    any(
                        event.kind == "completed"
                        for event in kanban_db.list_events(
                            connection, managed_task_id
                        )
                    )
                )
                aliases = (
                    (f"{workspaces}/../workspaces/worker/report.pdf", artifact),
                    ("~/workspaces/worker/report.pdf", artifact),
                    (f"{workspace_link}/report.pdf", artifact),
                    (str(outside), outside),
                    ("~/outside/report.pdf", outside),
                )
                for alias, expected_artifact in aliases:
                    with self.subTest(alias=alias), patch.dict(
                        os.environ,
                        {**base_environment, "HERMES_KANBAN_TASK": managed_task_id},
                        clear=True,
                    ):
                        extracted, _ = platform_base.BasePlatformAdapter.extract_local_files(
                            f"Created {alias}."
                        )
                        self.assertEqual(len(extracted), 1)
                        self.assertEqual(
                            Path(extracted[0]).resolve(), expected_artifact.resolve()
                        )
                        blocked_alias = json.loads(
                            model_tools.handle_function_call(
                                "kanban_complete",
                                {
                                    "task_id": managed_task_id,
                                    "summary": f"Created {alias}.",
                                },
                                task_id=str(uuid.uuid4()),
                            )
                        )
                    self.assertIn("error", blocked_alias)
                    self.assertEqual(
                        kanban_db.get_task(connection, managed_task_id).status,
                        "ready",
                    )
                media_summary = f"MEDIA:{outside}"
                media, _ = platform_base.BasePlatformAdapter.extract_media(
                    media_summary
                )
                self.assertEqual(
                    [Path(path).resolve() for path, _is_voice in media],
                    [outside.resolve()],
                )
                with patch.dict(
                    os.environ,
                    {**base_environment, "HERMES_KANBAN_TASK": managed_task_id},
                    clear=True,
                ):
                    blocked_media = json.loads(
                        model_tools.handle_function_call(
                            "kanban_complete",
                            {
                                "task_id": managed_task_id,
                                "summary": media_summary,
                            },
                            task_id=str(uuid.uuid4()),
                        )
                    )
                self.assertIn("error", blocked_media)
                self.assertEqual(
                    kanban_db.get_task(connection, managed_task_id).status,
                    "ready",
                )
                with patch.dict(
                    os.environ,
                    {**base_environment, "HERMES_KANBAN_TASK": managed_task_id},
                    clear=True,
                ):
                    malformed = json.loads(
                        model_tools.handle_function_call(
                            "kanban_complete",
                            {
                                "task_id": managed_task_id,
                                "summary": [str(artifact)],
                            },
                            task_id=str(uuid.uuid4()),
                        )
                    )
                self.assertIn("error", malformed)
                self.assertEqual(
                    kanban_db.get_task(connection, managed_task_id).status,
                    "ready",
                )
            finally:
                manager._hooks["pre_tool_call"] = previous
                connection.close()


if __name__ == "__main__":
    unittest.main()
