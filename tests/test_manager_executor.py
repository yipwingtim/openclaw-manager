#!/usr/bin/env python3

import importlib.util
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_FILE = ROOT_DIR / "services" / "manager-executor" / "executor.py"
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"


def load_executor():
    sys.path.insert(0, str(MANAGER_WEB_DIR))
    spec = importlib.util.spec_from_file_location("manager_executor", EXECUTOR_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManagerExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executor = load_executor()

    def test_run_once_retries_serial_job_and_records_success(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {
                "public_id": "instance-1",
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        adapter.start.side_effect = [(1, "temporary failure"), (0, "started")]

        worked = self.executor.run_once(
            control,
            lambda product: adapter,
            max_attempts=2,
        )

        self.assertTrue(worked)
        adapter.start.assert_called_with(control.claim.return_value["instance"])
        self.assertEqual(adapter.start.call_count, 2)
        self.assertEqual(
            control.update.call_args_list[-1].args,
            ("request-1", "succeeded"),
        )
        self.assertEqual(control.update.call_args_list[-1].kwargs["output"], "started")

    def test_run_once_skips_start_when_instance_is_already_up(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {"product": "evoscientist", "runtime_identifier": "evosci"},
        }
        adapter = Mock()
        adapter.status.return_value = "Up (evosci=Up)"

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.start.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-1", "succeeded"))
        self.assertIn("already running", control.update.call_args.kwargs["output"])

    def test_run_once_records_failure_after_limited_attempts(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.stop"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.stop.return_value = (1, "docker failed")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        self.assertEqual(adapter.stop.call_count, 2)
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))
        self.assertEqual(control.update.call_args.kwargs["output"], "docker failed")

    def test_run_once_retries_adapter_exceptions(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        adapter.start.side_effect = [TimeoutError("timed out"), (0, "started")]

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        self.assertEqual(adapter.start.call_count, 2)
        self.assertEqual(control.update.call_args.args, ("request-1", "succeeded"))

    def test_run_once_does_not_blindly_retry_restart(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.restart"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.restart.return_value = (1, "ambiguous failure")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.restart.assert_called_once()
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))

    def test_run_once_rejects_action_not_supported_by_adapter(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.restart"},
            "instance": {
                "product": "evoscientist",
                "runtime_identifier": "evosci",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = False

        self.executor.run_once(control, lambda product: adapter)

        adapter.status.assert_not_called()
        adapter.restart.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))
        self.assertEqual(
            control.update.call_args.kwargs["error_summary"],
            "instance product does not support restart",
        )

    def test_run_once_executes_wechat_bind_with_resolved_runtime_target(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw", "runtime_identifier": "openclaw_alice", "access_role": "owner"},
        }
        process = Mock()
        process.stdout.readline.return_value = "https://liteapp.weixin.qq.com/q/example\n"
        process.stdout.read.return_value = ""
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        control.get_job.return_value = {"status": "running"}
        control.get_runtime_instance.return_value = control.claim.return_value["instance"]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ) as popen, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "exec", "openclaw_alice"])
        self.assertIn("@tencent-weixin/openclaw-weixin-cli", command)
        self.assertEqual(control.update.call_args_list[-1].args, ("request-wechat", "succeeded"))

    def test_run_once_stops_wechat_bind_after_cancellation(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw", "runtime_identifier": "openclaw_alice", "access_role": "owner"},
        }
        control.get_job.return_value = {"status": "cancelled"}
        control.get_runtime_instance.return_value = control.claim.return_value["instance"]
        process = Mock()
        process.poll.return_value = None
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=Mock()
        ):
            self.executor.run_once(control, lambda product: adapter)

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_run_once_does_not_start_wechat_bind_for_stopped_instance(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        with patch.object(self.executor.subprocess, "Popen") as popen:
            self.executor.run_once(control, lambda product: adapter)

        popen.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-wechat", "failed"))
        self.assertEqual(control.update.call_args.kwargs["error_summary"], "instance is not running")

    def test_run_once_cleans_up_when_cancel_races_with_output_update(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        control.get_job.side_effect = [
            {"status": "running"},
            {"status": "cancelled"},
            {"status": "cancelled"},
        ]
        control.update.side_effect = [None, RuntimeError("invalid transition")]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        process = Mock()
        process.poll.return_value = None
        process.stdout.readline.return_value = "working\n"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        selector.close.assert_called_once()
        process.terminate.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_run_once_cleans_up_container_command_after_update_error(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        control.get_job.return_value = {"status": "running"}
        control.update.side_effect = [None, RuntimeError("control unavailable")]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        process = Mock()
        process.poll.return_value = None
        process.stdout.readline.return_value = "working\n"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        process.terminate.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_runtime_file_path_cannot_escape_instance_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            instance = {"data_path": temp_dir}
            inside = self.executor.resolve_instance_file(
                instance, "workspace", "notes.txt"
            )
            escaped = self.executor.resolve_instance_file(
                instance, "workspace", "../../etc/passwd"
            )

        self.assertEqual(inside, Path(temp_dir) / "workspace" / "notes.txt")
        self.assertIsNone(escaped)

    def test_runtime_file_root_symlink_cannot_escape_instance_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "instance"
            data_path.mkdir()
            (data_path / "workspace").symlink_to(Path(temp_dir))

            target = self.executor.resolve_instance_file(
                {"data_path": str(data_path)}, "workspace", "secret.txt"
            )

        self.assertIsNone(target)


if __name__ == "__main__":
    unittest.main()
