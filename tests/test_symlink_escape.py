#!/usr/bin/env python3

import importlib.util
import os
import shutil
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))


def load_app_module():
    flask_stub = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}
            self.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

        def context_processor(self, func):
            return func

        get = route
        post = route

    flask_stub.Flask = FakeFlask
    flask_stub.Response = object
    flask_stub.redirect = lambda *args, **kwargs: ("redirected", 302)
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(
        headers={},
        args={},
        form={},
        files={},
        host="localhost",
        path="/",
    )
    flask_stub.send_file = lambda *args, **kwargs: None
    flask_stub.url_for = lambda endpoint, **kwargs: endpoint

    werkzeug_stub = types.ModuleType("werkzeug")
    werkzeug_utils_stub = types.ModuleType("werkzeug.utils")
    werkzeug_utils_stub.secure_filename = lambda value: value

    sys.modules.setdefault("flask", flask_stub)
    sys.modules.setdefault("werkzeug", werkzeug_stub)
    sys.modules.setdefault("werkzeug.utils", werkzeug_utils_stub)

    spec = importlib.util.spec_from_file_location("manager_web_app", MANAGER_WEB_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def can_create_symlink():
    """检测当前环境是否支持创建符号链接（Windows 非开发者模式会失败）。"""
    with TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.txt"
        src.write_text("x")
        dst = Path(tmp) / "dst.txt"
        try:
            os.symlink(src, dst)
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINK_AVAILABLE = can_create_symlink()

ATOMIC_OPEN_AVAILABLE = (
    SYMLINK_AVAILABLE
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
)


class FakeUploadedFile:
    def __init__(self, filename, content=b"uploaded content"):
        self.filename = filename
        self._content = content
        self.saved_to = None

    def save(self, destination):
        if hasattr(destination, "write"):
            destination.write(self._content)
            destination.flush()
            self.saved_to = getattr(destination, "name", None)
        else:
            self.saved_to = Path(destination)
            self.saved_to.write_bytes(self._content)


class _SymlinkEscapeTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.public_dir = Path(self._tmp.name)
        self.app_module.PUBLIC_DIR = self.public_dir

    def tearDown(self):
        self._tmp.cleanup()

    def _make_user_dirs(self, user_id):
        user_dir = self.public_dir / "users" / user_id
        roots = {}
        for _, relative_dir, _ in self.app_module.WORKSPACE_FILE_ROOTS.values():
            root = user_dir / relative_dir
            root.mkdir(parents=True, exist_ok=True)
            roots[relative_dir] = root
        return roots


def _patch_os_open_with_toctou_swap(swap_callback):
    real_os_open = os.open
    real_supports_dir_fd = os.supports_dir_fd
    parent_fd_holder = []

    def patched_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None and flags & os.O_DIRECTORY:
            fd = real_os_open(path, flags, mode)
            parent_fd_holder.append(fd)
            return fd
        if dir_fd is not None and parent_fd_holder and dir_fd == parent_fd_holder[0]:
            swap_callback()
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    patched_supports = set(real_supports_dir_fd) | {patched_os_open}
    return patched_os_open, patched_supports


class ResolveDirectDownloadFileTests(_SymlinkEscapeTestBase):

    def test_normal_file_is_downloadable(self):
        roots = self._make_user_dirs("alice")
        expected = roots["workspaces"] / "notes.md"
        expected.write_text("my notes")

        result = self.app_module.resolve_direct_download_file("alice", "notes.md")

        self.assertIsNotNone(result)
        self.assertEqual(result, expected.resolve())

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_to_parent_users_csv_blocked(self):
        roots = self._make_user_dirs("alice")
        users_csv = self.public_dir / "users.csv"
        users_csv.write_text("user_id,port,created_at,status\nalice,30016,2026-01-01,active\nbob,30017,2026-01-02,active\n")
        link_path = roots["workspaces"] / "users.csv"
        link_path.symlink_to("../../../users.csv")

        result = self.app_module.resolve_direct_download_file("alice", "users.csv")

        self.assertIsNone(result, "指向 users.csv 的符号链接逃逸必须被拦截")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_to_other_user_workspace_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        (roots_b["workspace"] / "IDENTITY.md").write_text("bob's secret identity")
        link_path = roots_a["workspaces"] / "bob_id.md"
        link_path.symlink_to("../../bob/workspace/IDENTITY.md")

        result = self.app_module.resolve_direct_download_file("alice", "bob_id.md")

        self.assertIsNone(result, "指向其他用户文件的符号链接逃逸必须被拦截")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_to_other_user_uploads_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        (roots_b["uploads"] / "report.pdf").write_bytes(b"%PDF-1.4 fake pdf")
        link_path = roots_a["workspaces"] / "bob_report.pdf"
        link_path.symlink_to("../../bob/uploads/report.pdf")

        result = self.app_module.resolve_direct_download_file("alice", "bob_report.pdf")

        self.assertIsNone(result, "指向其他用户 uploads 的符号链接逃逸必须被拦截")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_within_root_allowed(self):
        roots = self._make_user_dirs("alice")
        (roots["workspaces"] / "target.md").write_text("real content")
        link_path = roots["workspaces"] / "link.md"
        link_path.symlink_to("target.md")

        result = self.app_module.resolve_direct_download_file("alice", "link.md")

        self.assertIsNotNone(result, "root 内的符号链接应被允许")
        self.assertEqual(result.name, "target.md")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_chain_escaping_root_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        (roots_b["workspace"] / "secret.md").write_text("bob's secret")
        (roots_a["workspaces"] / "link2.md").symlink_to("../../bob/workspace/secret.md")
        (roots_a["workspaces"] / "link1.md").symlink_to("link2.md")

        result = self.app_module.resolve_direct_download_file("alice", "link1.md")

        self.assertIsNone(result, "多级符号链接链逃逸必须被拦截")

    def test_ambiguous_filename_across_roots_returns_none(self):
        roots = self._make_user_dirs("alice")
        (roots["workspaces"] / "dup.md").write_text("in workspaces")
        (roots["workspace"] / "dup.md").write_text("in workspace")

        result = self.app_module.resolve_direct_download_file("alice", "dup.md")

        self.assertIsNone(result, "歧义文件名应返回 None")

    def test_nonexistent_file_returns_none(self):
        self._make_user_dirs("alice")

        result = self.app_module.resolve_direct_download_file("alice", "missing.md")

        self.assertIsNone(result)


class ResolveWorkspaceFileTests(_SymlinkEscapeTestBase):
    def test_normal_flat_file_resolvable(self):
        """无符号链接的扁平文件应能解析成功。"""
        roots = self._make_user_dirs("alice")
        expected = roots["workspaces"] / "notes.md"
        expected.write_text("my notes")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "notes.md")

        self.assertIsNotNone(result)
        self.assertEqual(result, expected.resolve())

    def test_normal_subdirectory_file_resolvable(self):
        roots = self._make_user_dirs("alice")
        sub = roots["workspaces"] / "sub"
        sub.mkdir()
        expected = sub / "deep.md"
        expected.write_text("deep content")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "sub/deep.md")

        self.assertIsNotNone(result)
        self.assertEqual(result, expected.resolve())

    def test_invalid_root_key_returns_none(self):
        self._make_user_dirs("alice")

        result = self.app_module.resolve_workspace_file("alice", "nonexistent_root", "any.md")

        self.assertIsNone(result)

    def test_path_traversal_blocked(self):
        roots = self._make_user_dirs("alice")
        (roots["workspaces"] / "notes.md").write_text("my notes")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "../workspace/notes.md")

        self.assertIsNone(result, "../ 路径遍历必须被拦截")

    def test_unsupported_extension_blocked(self):
        roots = self._make_user_dirs("alice")
        (roots["workspaces"] / "evil.exe").write_text("executable")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "evil.exe")

        self.assertIsNone(result)

    def test_nonexistent_file_returns_none(self):
        self._make_user_dirs("alice")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "missing.md")

        self.assertIsNone(result)

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_to_other_user_workspace_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        (roots_b["workspace"] / "secret.md").write_text("bob's secret")
        link_path = roots_a["workspaces"] / "link.md"
        link_path.symlink_to("../../bob/workspace/secret.md")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "link.md")

        self.assertIsNone(result, "指向其他用户文件的符号链接逃逸必须被拦截")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_within_root_allowed(self):
        roots = self._make_user_dirs("alice")
        (roots["workspaces"] / "target.md").write_text("real content")
        link_path = roots["workspaces"] / "link.md"
        link_path.symlink_to("target.md")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "link.md")

        self.assertIsNotNone(result, "root 内的符号链接应被允许")
        self.assertEqual(result.name, "target.md")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_symlink_chain_escaping_root_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        (roots_b["workspace"] / "secret.md").write_text("bob's secret")
        (roots_a["workspaces"] / "link2.md").symlink_to("../../bob/workspace/secret.md")
        (roots_a["workspaces"] / "link1.md").symlink_to("link2.md")

        result = self.app_module.resolve_workspace_file("alice", "workspaces", "link1.md")

        self.assertIsNone(result, "多级符号链接链逃逸必须被拦截")


class UploadFileSymlinkEscapeTests(_SymlinkEscapeTestBase):

    def _run_upload(self, user_id, fake_file):
        """执行 upload_file_for_user 并捕获 redirect_to_user_dashboard 的调用参数。"""
        calls = []

        def fake_redirect(uid, instance_mode=False, result="", error="", wechat_url=""):
            calls.append({"user_id": uid, "result": result, "error": error})
            return ("redirected", 302)

        with patch.object(self.app_module, "redirect_to_user_dashboard", fake_redirect):
            with patch.object(self.app_module, "persist_operation_metadata", return_value=""):
                with patch.object(self.app_module.request, "files", {"file": fake_file}):
                    self.app_module.upload_file_for_user(user_id, instance_mode=True)

        return calls

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "原子上传需要 O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_normal_upload_succeeds(self):
        self._make_user_dirs("alice")
        fake_file = FakeUploadedFile("notes.md", b"my notes")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["error"], "", "正常上传不应报错")
        self.assertIn("Uploaded notes.md", calls[0]["result"])
        uploaded_path = self.public_dir / "users" / "alice" / "uploads" / "notes.md"
        self.assertTrue(uploaded_path.exists())
        self.assertEqual(uploaded_path.read_bytes(), b"my notes")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_upload_to_symlink_escaping_to_other_user_blocked(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")
        link_path = roots_a["uploads"] / "evil.md"
        link_path.symlink_to("../../bob/workspace/evil.md")

        fake_file = FakeUploadedFile("evil.md", b"malicious content")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertIn("Invalid file path", calls[0]["error"], "符号链接逃逸应返回 Invalid file path 错误")
        bob_target = self.public_dir / "users" / "bob" / "workspace" / "evil.md"
        self.assertFalse(bob_target.exists(), "符号链接目标文件不应被创建")

    @unittest.skipUnless(SYMLINK_AVAILABLE, "当前环境不支持创建符号链接")
    def test_upload_to_symlink_escaping_to_parent_blocked(self):
        roots_a = self._make_user_dirs("alice")
        link_path = roots_a["uploads"] / "pwned.md"
        link_path.symlink_to("../../../pwned.md")

        fake_file = FakeUploadedFile("pwned.md", b"escape attempt")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertIn("Invalid file path", calls[0]["error"])
        escaped_target = self.public_dir / "pwned.md"
        self.assertFalse(escaped_target.exists(), "逃逸目标文件不应被创建")

    @unittest.skipUnless(
        SYMLINK_AVAILABLE and hasattr(os, "O_NOFOLLOW"),
        "当前环境不支持创建符号链接或缺少 O_NOFOLLOW（仅 POSIX 原子路径可拒绝符号链接）",
    )
    def test_upload_to_symlink_within_upload_dir_blocked(self):
        roots_a = self._make_user_dirs("alice")
        link_path = roots_a["uploads"] / "alias.md"
        link_path.symlink_to("within_root_target.md")

        fake_file = FakeUploadedFile("alias.md", b"via symlink within root")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertIn("Invalid file path", calls[0]["error"],
                      "O_NOFOLLOW 应拒绝符号链接目标，即使是 root 内的合法符号链接")

    def test_upload_unsupported_extension_blocked(self):
        self._make_user_dirs("alice")
        fake_file = FakeUploadedFile("evil.exe", b"executable")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertIn("Unsupported file type", calls[0]["error"])

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "原子上传需要 O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_upload_existing_file_blocked(self):
        roots_a = self._make_user_dirs("alice")
        existing = roots_a["uploads"] / "exists.md"
        existing.write_text("already here")

        fake_file = FakeUploadedFile("exists.md", b"overwrite attempt")

        calls = self._run_upload("alice", fake_file)

        self.assertEqual(len(calls), 1)
        self.assertIn("File already exists", calls[0]["error"])
        self.assertEqual(existing.read_text(), "already here")

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "需要符号链接、O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_upload_rejects_symlink_replacement_between_validation_and_open(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")

        race_path = roots_a["uploads"] / "race.md"
        race_path.write_text("placeholder")

        stolen_target = roots_b["workspace"] / "stolen.md"

        fake_file = FakeUploadedFile("race.md", b"malicious via TOCTOU")

        staging_link = roots_a["uploads"] / ".staging_race.md"
        staging_link.symlink_to("../../bob/workspace/stolen.md")

        def swap():
            os.rename(staging_link, race_path)

        patched_open, patched_supports = _patch_os_open_with_toctou_swap(swap)

        calls = []
        original_redirect = self.app_module.redirect_to_user_dashboard

        def capture_redirect(uid, instance_mode=False, result="", error="", wechat_url=""):
            calls.append({"user_id": uid, "result": result, "error": error})
            return original_redirect(uid, instance_mode=instance_mode, result=result, error=error, wechat_url=wechat_url)

        with patch.object(self.app_module, "redirect_to_user_dashboard", capture_redirect):
            with patch.object(self.app_module, "persist_operation_metadata", return_value=""):
                with patch.object(self.app_module.request, "files", {"file": fake_file}):
                    with patch("os.open", patched_open), patch("os.supports_dir_fd", patched_supports):
                        self.app_module.upload_file_for_user("alice", instance_mode=True)

        self.assertEqual(len(calls), 1, "应只产生一次 redirect 响应")
        self.assertIn("Invalid file path", calls[0]["error"],
                      "TOCTOU 替换为符号链接后必须以 Invalid file path 拒绝，而非写入成功")
        self.assertFalse(stolen_target.exists(),
                         "TOCTOU 攻击成功会导致 bob/workspace/stolen.md 被创建——必须被拦截")
        self.assertTrue(race_path.is_symlink(),
                        "测试注入后 race.md 应仍是符号链接，未被实际写入覆盖")


class OpenDownloadFileAtomicTests(_SymlinkEscapeTestBase):

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "需要符号链接、O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_normal_file_opens_successfully(self):
        """正常文件应能通过原子打开返回可读文件对象，内容与磁盘一致。"""
        roots = self._make_user_dirs("alice")
        target = roots["workspaces"] / "notes.md"
        target.write_text("my notes")
        resolved_target = target.resolve()

        file_obj = self.app_module._open_download_file_atomic(resolved_target)

        self.assertIsNotNone(file_obj, "正常文件应能成功打开")
        try:
            self.assertEqual(file_obj.read(), b"my notes")
        finally:
            file_obj.close()

    def test_returns_none_when_platform_lacks_dir_fd_support(self):
        with patch("os.supports_dir_fd", set()):
            file_obj = self.app_module._open_download_file_atomic(
                Path("/nonexistent/path/to/file.md")
            )

        self.assertIsNone(
            file_obj,
            "平台不支持 dir_fd 时必须短路返回 None，而非尝试打开或抛异常",
        )

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "需要符号链接、O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_rejects_symlink_replacement_between_validation_and_open(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")

        race_path = roots_a["workspaces"] / "race.md"
        race_path.write_text("placeholder")
        stolen_target = roots_b["workspace"] / "stolen.md"
        stolen_target.write_text("bob's secret")

        resolved_target = race_path.resolve()

        staging_link = roots_a["workspaces"] / ".staging_race.md"
        staging_link.symlink_to("../../bob/workspace/stolen.md")

        def swap():
            os.rename(staging_link, race_path)

        patched_open, patched_supports = _patch_os_open_with_toctou_swap(swap)

        with patch("os.open", patched_open), patch("os.supports_dir_fd", patched_supports):
            file_obj = self.app_module._open_download_file_atomic(resolved_target)

        self.assertIsNone(file_obj,
                          "TOCTOU 替换为符号链接后必须返回 None，而非打开逃逸目标")

        self.assertEqual(stolen_target.read_text(), "bob's secret",
                         "bob 侧文件应存在且内容为攻击者预期，证明攻击场景成立")

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "需要符号链接、O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_rejects_intermediate_ancestor_dir_swap_via_proc_fd_check(self):
        roots_a = self._make_user_dirs("alice")
        roots_b = self._make_user_dirs("bob")

        alice_sub = roots_a["workspaces"] / "sub"
        alice_sub_deeper = alice_sub / "deeper"
        alice_sub_deeper.mkdir(parents=True)
        alice_target_file = alice_sub_deeper / "foo.md"
        alice_target_file.write_text("alice's foo")

        bob_deeper = roots_b["workspace"] / "deeper"
        bob_deeper.mkdir(parents=True)
        bob_target_file = bob_deeper / "foo.md"
        bob_target_file.write_text("bob's foo")

        resolved_target = alice_target_file.resolve()

        real_os_open = os.open
        swap_done = [False]

        def patched_os_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None and flags & os.O_DIRECTORY and not swap_done[0]:
                swap_done[0] = True
                shutil.rmtree(alice_sub)
                alice_sub.symlink_to("../../bob/workspace")
                assert alice_sub.is_symlink(), "swap 必须成功把 alice_sub 替换为符号链接"
            return real_os_open(path, flags, mode, dir_fd=dir_fd)

        patched_supports = set(os.supports_dir_fd) | {patched_os_open}
        with patch("os.open", patched_os_open), patch("os.supports_dir_fd", patched_supports):
            file_obj = self.app_module._open_download_file_atomic(resolved_target)

        self.assertIsNone(file_obj,
                          "TOCTOU 替换中间祖先目录为符号链接后必须返回 None，"
                          "即使 O_NOFOLLOW 未能直接拦截，/proc/self/fd 比较也必须兜底拒绝")
        self.assertEqual(bob_target_file.read_text(), "bob's foo",
                         "bob 侧文件应存在且内容为攻击者预期，证明攻击场景成立")

    @unittest.skipUnless(
        ATOMIC_OPEN_AVAILABLE,
        "需要符号链接、O_NOFOLLOW、O_DIRECTORY 与 dir_fd 支持（仅 POSIX）",
    )
    def test_rejects_when_proc_fd_readlink_raises_oserror(self):
        roots = self._make_user_dirs("alice")
        target = roots["workspaces"] / "notes.md"
        target.write_text("my notes")
        resolved_target = target.resolve()

        real_readlink = os.readlink

        def failing_readlink(path):
            if str(path).startswith("/proc/self/fd/"):
                raise OSError("模拟 /proc/self/fd 读取失败（如非 Linux 内核或 procfs 不可用）")
            return real_readlink(path)

        with patch("os.readlink", failing_readlink):
            file_obj = self.app_module._open_download_file_atomic(resolved_target)

        self.assertIsNone(
            file_obj,
            "os.readlink 抛出 OSError 时必须 fail-closed 返回 None，"
            "否则 /proc/self/fd 兜底校验失效将重新引入 TOCTOU 风险",
        )


if __name__ == "__main__":
    unittest.main()
