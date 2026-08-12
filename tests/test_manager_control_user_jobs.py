#!/usr/bin/env python3

import os
import unittest
from contextlib import nullcontext
from unittest.mock import Mock
from unittest.mock import patch

from tests.test_manager_control_api import load_control_app, response_parts


class ManagerControlUserJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control_app()

    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "MANAGER_CONTROL_USER_WEB_TOKEN": "user-token",
                "MANAGER_CONTROL_ADMIN_WEB_TOKEN": "admin-token",
                "MANAGER_CONTROL_EXECUTOR_TOKEN": "executor-token",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_user_web_cannot_create_lifecycle_job(self):
        payload = {
            "request_id": "request-1",
            "actor_user_public_id": "user-1",
            "instance_public_id": "instance-1",
            "action": "instance.restart",
            "params": {},
        }
        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": "user-1",
            },
        ), patch.object(
            self.control.request, "get_json", return_value=payload
        ), patch.object(
            self.control.metadata_store,
            "get_user_by_public_id",
            return_value={"status": "active", "role": "user"},
        ):
            response, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 403)
        self.assertEqual(response.get_json(), {"error": "user web action is not allowed"})

    def test_user_web_cannot_create_job_for_another_actor(self):
        payload = {
            "request_id": "request-1",
            "actor_user_public_id": "user-2",
            "instance_public_id": "instance-1",
            "action": "instance.wechat_bind",
            "params": {},
        }
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": "user-1",
        }
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ), patch.object(
            self.control.metadata_store,
            "get_user_by_public_id",
            return_value={"status": "active", "role": "user"},
        ):
            response, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 403)
        self.assertEqual(
            response.get_json(),
            {"error": "user service cannot impersonate another user"},
        )

    def test_user_web_cannot_read_or_cancel_another_users_job(self):
        job = {
            "actor_user_public_id": "user-2",
            "action": "instance.wechat_bind",
        }
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": "user-1",
        }
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.metadata_store, "get_execution_job", return_value=job
        ):
            read, read_status = response_parts(self.control.get_execution_job("request-1"))
            cancel, cancel_status = response_parts(
                self.control.cancel_execution_job("request-1")
            )

        self.assertEqual(read_status, 403)
        self.assertEqual(cancel_status, 403)
        self.assertEqual(read.get_json(), {"error": "execution job is not available"})
        self.assertEqual(cancel.get_json(), {"error": "execution job is not available"})

    def test_current_wechat_job_is_filtered_by_actor_and_instance(self):
        job = {
            "request_id": "request-1",
            "parent_request_id": None,
            "actor_user_public_id": "user-1",
            "instance_public_id": "instance-1",
            "action": "instance.wechat_bind",
            "params_json": "{}",
            "status": "running",
            "current_step": None,
            "error_summary": None,
            "output": None,
        }
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": "user-1",
        }
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.metadata_store, "list_execution_jobs", return_value=[job]
        ) as list_jobs:
            response, status = response_parts(
                self.control.current_wechat_bind_job("instance-1")
            )

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["job"]["request_id"], "request-1")
        list_jobs.assert_called_once_with(
            limit=1,
            actor_user_public_id="user-1",
            instance_public_id="instance-1",
            action="instance.wechat_bind",
            newest_first=True,
            db_file=self.control.DB_FILE,
        )

    def test_wechat_job_creation_locks_before_checking_instance_jobs(self):
        payload = {
            "request_id": "request-1",
            "actor_user_public_id": "user-1",
            "instance_public_id": "instance-1",
            "action": "instance.wechat_bind",
            "params": {},
        }
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": "user-1",
        }
        conn = Mock()
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ), patch.object(
            self.control.metadata_store,
            "get_user_by_public_id",
            return_value={"id": 1, "status": "active", "role": "user"},
        ), patch.object(
            self.control.metadata_store,
            "get_instance_for_user",
            return_value={"access_role": "owner", "product": "openclaw", "status": "active"},
        ), patch.object(
            self.control.metadata_store, "get_execution_job", return_value=None
        ), patch.object(
            self.control.metadata_store, "connect", return_value=nullcontext(conn)
        ), patch.object(
            self.control.metadata_store, "list_execution_jobs", return_value=[]
        ) as list_jobs, patch.object(
            self.control.metadata_store,
            "create_execution_job",
            return_value={
                "request_id": "request-1",
                "parent_request_id": None,
                "action": "instance.wechat_bind",
                "params_json": "{}",
                "status": "queued",
                "current_step": None,
                "error_summary": None,
                "output": None,
            },
        ) as create_job:
            _, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 200)
        conn.execute.assert_called_once_with("BEGIN IMMEDIATE")
        self.assertIs(list_jobs.call_args.kwargs["conn"], conn)
        self.assertNotIn("actor_user_public_id", list_jobs.call_args.kwargs)
        self.assertIs(create_job.call_args.kwargs["conn"], conn)


if __name__ == "__main__":
    unittest.main()
