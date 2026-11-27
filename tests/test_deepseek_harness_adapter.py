import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/manager-web"))
from instance_adapters import DeepSeekHarnessDockerAdapter, OpenClawDockerAdapter


class HarnessTests(unittest.TestCase):
    def test_private_runtime_preserves_workspace_on_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            root = Path(tmp) / "instance"
            instance = {"runtime_identifier": "deepseek-harness_alice", "data_path": str(root)}
            with patch("instance_adapters.os.chown"), patch.object(
                adapter, "run_command", return_value=(0, "")
            ) as run:
                self.assertEqual(adapter.create(instance, False)[0], 0)
                launch = run.call_args_list[1][0][0]
                self.assertEqual(launch[launch.index("--network") + 1], "none")
                self.assertEqual(launch[-1], "openclaw-manager/deepseek-harness:0.2.2-rc.5")
                marker = root / "workspace" / "keep"
                marker.write_text("mine")
                adapter.delete(instance)
                self.assertEqual(adapter.restore(instance)[0], 0)
                self.assertEqual(marker.read_text(), "mine")
                self.assertEqual(run.call_args_list[-2][0][0], launch)
                self.assertEqual(adapter.create(instance, False, version="latest")[0], 1)
            with patch.object(OpenClawDockerAdapter, "logs", return_value="/?token=secret"):
                self.assertNotIn("secret", adapter.logs(instance))

    def test_cleanup_preserves_data_and_tolerates_missing_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            marker = Path(tmp) / "retained"
            marker.write_text("user data")
            instance = dict(status="failed", runtime_identifier="deepseek-harness_alice", data_path=tmp)
            with patch.object(adapter, "run_command", return_value=(1, "Error: No such container: x")):
                self.assertEqual(adapter.cleanup_failed(instance)[0], 0)
            with patch.object(adapter, "run_command", return_value=(1, "permission denied")):
                self.assertNotEqual(adapter.cleanup_failed(instance)[0], 0)
            instance["status"] = "active"
            with patch.object(adapter, "run_command") as run:
                self.assertNotEqual(adapter.cleanup_failed(instance)[0], 0)
                run.assert_not_called()
            self.assertEqual(marker.read_text(), "user data")


    def test_missing_image_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            root = Path(tmp) / "new-instance"
            with patch.object(adapter, "run_command", return_value=(1, "missing")):
                code, message = adapter.create(dict(data_path=str(root)), False)
            self.assertEqual(code, 1)
            self.assertIn("image missing", message)
            self.assertFalse(root.exists())
