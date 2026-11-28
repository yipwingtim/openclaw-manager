import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/manager-web"))
from instance_adapters import DeepSeekHarnessDockerAdapter, OpenClawDockerAdapter


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.host = patch.dict("os.environ", {"PUBLIC_HOST": "manager.example.test"})
        self.host.start()
        self.addCleanup(self.host.stop)

    def test_private_runtime_preserves_workspace_on_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            root = Path(tmp) / "instance"
            instance = {"public_id": "11111111-1111-4111-8111-111111111111", "legacy_user_id": "alice", "runtime_identifier": "deepseek-harness_alice", "data_path": str(root)}
            with patch("instance_adapters.os.chown"), patch.object(
                adapter, "run_command", return_value=(0, "")
            ) as run, patch.object(adapter, "configure_ingress", return_value=(0, "")):
                self.assertEqual(adapter.create(instance, False)[0], 0)
                launch = run.call_args_list[2][0][0]
                self.assertTrue(launch[launch.index("--network") + 1].startswith("openclaw-user-"))
                self.assertEqual(launch[-1], "openclaw-manager/deepseek-harness:0.2.2-rc.5")
                marker = root / "workspace" / "keep"
                marker.write_text("mine")
                adapter.delete(instance)
                self.assertEqual(adapter.restore(instance)[0], 0)
                self.assertEqual(marker.read_text(), "mine")
                self.assertIn(launch, [call[0][0] for call in run.call_args_list])
                self.assertEqual(adapter.create(instance, False, version="latest")[0], 1)
            with patch.object(OpenClawDockerAdapter, "logs", return_value="/?token=secret"):
                self.assertNotIn("secret", adapter.logs(instance))

    def test_cleanup_preserves_data_and_tolerates_missing_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            marker = Path(tmp) / "retained"
            marker.write_text("user data")
            instance = dict(public_id="11111111-1111-4111-8111-111111111111", status="failed", runtime_identifier="deepseek-harness_alice", data_path=tmp)
            with patch.object(adapter, "run_command", return_value=(1, "Error: No such container: x")), patch.object(adapter, "reload_nginx", return_value=(0, "")):
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


    def test_ingress_rejects_unsafe_paths_and_preserves_previous_config_on_failure(self):
        from product_capabilities import harness_access_path
        for value in ("admin", "auth", "../other", "alice.conf", "_private"):
            with self.assertRaises(ValueError):
                harness_access_path(value)
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            instance = dict(public_id="11111111-1111-4111-8111-111111111111",
                            legacy_user_id="alice", runtime_identifier="deepseek-harness_alice")
            conf = adapter.ingress_conf(instance)
            conf.parent.mkdir()
            conf.write_text("previous")
            env = "/deepseek-harness_alice-ingress"
            with patch.object(adapter, "run_command", side_effect=[(0, env), (0, ""), (0, "")]), patch.object(
                adapter, "reload_nginx", return_value=(1, "bad config")
            ):
                self.assertEqual(adapter.configure_ingress(instance)[0], 1)
            self.assertEqual(conf.read_text(), "previous")

    def test_old_instance_upgrade_rolls_back_without_touching_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            instance = dict(runtime_identifier="old", public_id="11111111-1111-4111-8111-111111111111")
            with patch.object(OpenClawDockerAdapter, "status", return_value="Up"), patch.object(
                adapter, "run_command", return_value=(0, "")
            ) as run, patch.object(adapter, "_launch", return_value=(1, "failed")):
                self.assertEqual(adapter.upgrade_ingress(instance), (1, "failed"))
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["docker", "rename", "old-previous", "old"], commands)
            self.assertIn(["docker", "start", "old"], commands)

    def test_absent_runtime_can_be_deleted_and_data_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            instance = dict(runtime_identifier="missing", public_id="11111111-1111-4111-8111-111111111111")
            conf = adapter.ingress_conf(instance)
            conf.parent.mkdir()
            conf.write_text("old ingress")
            with patch.object(adapter, "run_command", return_value=(1, "No such container")), patch.object(
                adapter, "reload_nginx", return_value=(0, "")
            ):
                self.assertEqual(adapter.delete(instance)[0], 0)
            self.assertFalse(conf.exists())

    def test_ingress_writes_prefix_stripping_proxy_without_external_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = DeepSeekHarnessDockerAdapter(tmp, tmp, tmp, tmp, "nginx")
            instance = dict(public_id="11111111-1111-4111-8111-111111111111",
                            legacy_user_id="alice", runtime_identifier="deepseek-harness_alice")
            env = "/deepseek-harness_alice-ingress"
            with patch.object(adapter, "run_command", side_effect=[(0, env), (0, ""), (0, "")]), patch.object(
                adapter, "reload_nginx", return_value=(0, "")
            ):
                self.assertEqual(adapter.configure_ingress(instance)[0], 0)
            text = adapter.ingress_conf(instance).read_text()
            self.assertIn("location ^~ /alice/", text)
            self.assertIn("rewrite ^/alice/(.*)$ /$1 break", text)
            self.assertIn("http://deepseek-harness_alice-ingress:3081", text)
            self.assertIn("proxy_set_header Host $http_host", text)
            self.assertNotIn("listen", text)
            self.assertEqual(adapter.instance_auth_contract()["edge_authorization"], "uis")
