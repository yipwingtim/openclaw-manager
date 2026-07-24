#!/usr/bin/env python3

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_user_access import load_app_module


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
CLIENT_FILE = MANAGER_WEB_DIR / "control_client.py"


def load_control_client():
    spec = importlib.util.spec_from_file_location(
        "manager_control_client",
        CLIENT_FILE,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(
            {"instances": [{"public_id": "instance-1"}]}
        ).encode()


class ManagerControlClientTests(unittest.TestCase):
    def test_instance_list_sends_service_token_and_actor_identity(self):
        client = load_control_client()
        client.BASE_URL = "http://manager-control:8082"
        client.SERVICE_TOKEN = "user-service-token"

        with patch.object(
            client.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            instances = client.list_instances("user-1")

        self.assertEqual(instances, [{"public_id": "instance-1"}])
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://manager-control:8082/internal/v1/users/user-1/instances",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer user-service-token",
        )
        self.assertEqual(
            request.get_header("X-actor-user-public-id"),
            "user-1",
        )


class MultiInstancePortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_web = load_app_module()

    def test_me_renders_owned_and_shared_instances_from_control(self):
        actor = {
            "id": 10,
            "public_id": "user-1",
            "username": "alice",
            "status": "active",
            "role": "user",
        }
        instances = [
            {
                "public_id": "instance-owned",
                "product": "openclaw",
                "instance_name": "My OpenClaw",
                "status": "active",
                "access_role": "owner",
            },
            {
                "public_id": "instance-shared",
                "product": "evoscientist",
                "instance_name": "Shared Evo",
                "status": "stopped",
                "access_role": "operator",
            },
        ]
        render = Mock(return_value="rendered")

        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ):
            with patch.object(
                self.manager_web.control_client,
                "list_instances",
                return_value=instances,
            ):
                with patch.object(
                    self.manager_web,
                    "render_template",
                    render,
                ):
                    response = self.manager_web.my_instance()

        self.assertEqual(response, "rendered")
        expected_instances = [
            {
                **instances[0],
                "allowed_actions": [
                    "access",
                    "device_pairing",
                    "file_delete",
                    "file_download",
                    "file_upload",
                    "logs",
                    "member_manage",
                    "status",
                ],
            },
            {
                **instances[1],
                "allowed_actions": ["access", "status"],
            },
        ]
        render.assert_called_once_with(
            "my_instances.html",
            instances=expected_instances,
            current_user="alice",
            is_admin=False,
            show_global_admin_nav=False,
        )

    def test_admin_can_open_explicitly_authorized_instance_list(self):
        actor = {
            "public_id": "admin-1",
            "username": "admin",
            "status": "active",
            "role": "admin",
        }
        render = Mock(return_value="rendered")
        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ), patch.object(
            self.manager_web.control_client,
            "list_instances",
            return_value=[],
        ), patch.object(
            self.manager_web,
            "render_template",
            render,
        ):
            response = self.manager_web.my_instance()

        self.assertEqual(response, "rendered")
        self.assertTrue(render.call_args.kwargs["is_admin"])
        self.assertTrue(render.call_args.kwargs["show_global_admin_nav"])

    def test_manager_detail_uses_role_and_product_capabilities(self):
        actor = {
            "id": 10,
            "public_id": "user-1",
            "username": "alice",
            "status": "active",
            "role": "user",
        }
        instance = {
            "public_id": "instance-1",
            "legacy_user_id": "alice",
            "product": "openclaw",
            "instance_name": "Research assistant",
            "status": "active",
            "access_url": "https://example.test:30021",
            "access_role": "manager",
        }
        render = Mock(return_value="rendered")

        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ):
            with patch.object(
                self.manager_web.control_client,
                "get_instance",
                return_value=instance,
            ), patch.object(
                self.manager_web.control_client,
                "list_members",
                return_value=[],
            ):
                with patch.object(
                    self.manager_web,
                    "get_container_status",
                    return_value="Up",
                ):
                    with patch.object(
                        self.manager_web,
                        "get_container_logs",
                        return_value="recent logs",
                    ):
                        with patch.object(
                            self.manager_web,
                            "read_devices_cache",
                            return_value="devices",
                        ):
                            with patch.object(
                                self.manager_web,
                                "list_uploaded_files",
                                return_value=[],
                            ):
                                with patch.object(
                                    self.manager_web,
                                    "list_downloadable_files",
                                    return_value=[],
                                ):
                                    with patch.object(
                                        self.manager_web,
                                        "render_template",
                                        render,
                                    ):
                                        response = self.manager_web.instance_detail(
                                            "instance-1"
                                        )

        self.assertEqual(response, "rendered")
        template, context = render.call_args.args[0], render.call_args.kwargs
        self.assertEqual(template, "user.html")
        self.assertEqual(context["instance_public_id"], "instance-1")
        self.assertEqual(context["instance_name"], "Research assistant")
        self.assertEqual(context["access_role"], "manager")
        self.assertEqual(
            context["allowed_actions"],
            {
                "access",
                "status",
                "logs",
                "device_pairing",
                "file_upload",
                "file_download",
                "file_delete",
                "member_manage",
            },
        )
        self.assertNotIn("restart", context["allowed_actions"])

    def test_file_upload_route_rechecks_role_and_product_capability(self):
        actor = {
            "id": 10,
            "public_id": "user-1",
            "username": "alice",
            "status": "active",
            "role": "user",
        }
        instance = {
            "public_id": "instance-1",
            "legacy_user_id": "alice",
            "product": "openclaw",
            "instance_name": "Research assistant",
            "status": "active",
            "access_role": "operator",
        }

        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ):
            with patch.object(
                self.manager_web.control_client,
                "get_instance",
                return_value=instance,
            ):
                with patch.object(
                    self.manager_web,
                    "upload_file_for_user",
                    return_value="uploaded",
                ) as upload:
                    denied = self.manager_web.portal_upload_file("instance-1")
                    instance["access_role"] = "manager"
                    allowed = self.manager_web.portal_upload_file("instance-1")

        self.assertEqual(denied[1], 403)
        self.assertEqual(allowed, "uploaded")
        upload.assert_called_once_with(
            "alice",
            instance_public_id="instance-1",
        )

    def test_owner_can_add_existing_platform_user_as_member(self):
        actor = {
            "public_id": "owner-1",
            "username": "alice",
            "status": "active",
            "role": "user",
        }
        instance = {
            "public_id": "instance-1",
            "legacy_user_id": None,
            "product": "openclaw",
            "access_role": "owner",
        }
        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ), patch.object(
            self.manager_web.control_client,
            "get_instance",
            return_value=instance,
        ), patch.object(
            self.manager_web.control_client,
            "add_member",
        ) as add_member, patch.object(
            self.manager_web.request,
            "form",
            {"username": "bob", "role": "operator"},
        ):
            self.manager_web.portal_add_instance_member("instance-1")

        add_member.assert_called_once_with(
            "owner-1",
            "instance-1",
            "bob",
            "operator",
        )

    def test_non_legacy_instance_renders_metadata_and_members(self):
        actor = {
            "public_id": "owner-1",
            "username": "alice",
            "status": "active",
            "role": "user",
        }
        instance = {
            "public_id": "instance-1",
            "legacy_user_id": None,
            "product": "evoscientist",
            "instance_name": "Evo",
            "status": "active",
            "access_url": "https://example.test:30022",
            "access_role": "owner",
            "version": None,
        }
        render = Mock(return_value="rendered")
        with patch.object(
            self.manager_web,
            "get_actor_user_record",
            return_value=actor,
        ), patch.object(
            self.manager_web.control_client,
            "get_instance",
            return_value=instance,
        ), patch.object(
            self.manager_web.control_client,
            "list_members",
            return_value=[],
        ), patch.object(
            self.manager_web,
            "render_template",
            render,
        ):
            response = self.manager_web.instance_detail("instance-1")

        self.assertEqual(response, "rendered")
        self.assertEqual(render.call_args.args[0], "instance_detail.html")
        self.assertTrue(render.call_args.kwargs["can_manage_members"])

    def test_legacy_user_id_mutation_route_never_runs_action(self):
        with patch.object(
            self.manager_web,
            "approve_latest_for_user",
        ) as approve, patch.object(
            self.manager_web,
            "render_template",
            return_value="disabled",
        ):
            response = self.manager_web.approve_latest("alice")

        self.assertEqual(response, ("disabled", 410))
        approve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
