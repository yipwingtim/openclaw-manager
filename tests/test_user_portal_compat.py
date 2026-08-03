#!/usr/bin/env python3

import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "services" / "manager-web"


class UserPortalCompatibilityTests(unittest.TestCase):
    def test_split_user_app_reuses_full_legacy_portal(self):
        source = (WEB_DIR / "user_app.py").read_text(encoding="utf-8")
        template = (WEB_DIR / "templates" / "user.html").read_text(encoding="utf-8")

        self.assertIn('render_template(\n        "user.html",', source)
        self.assertIn('elif status == "succeeded":\n            status = "success"', source)
        for text in ("微信绑定", "文件下载", "最近日志", "确认删除文件"):
            self.assertIn(text, template)
        members = (WEB_DIR / "templates" / "_instance_members.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("实例成员", members)

    def test_split_user_pages_do_not_override_authenticated_display_name(self):
        source = (WEB_DIR / "user_app.py").read_text(encoding="utf-8")

        self.assertNotIn('current_user=current["username"]', source)
        self.assertIn('render_template("my_instances.html", instances=instances)', source)


if __name__ == "__main__":
    unittest.main()
