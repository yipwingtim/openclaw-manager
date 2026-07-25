#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


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


if __name__ == "__main__":
    unittest.main()
