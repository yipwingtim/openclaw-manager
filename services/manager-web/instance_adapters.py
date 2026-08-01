import hashlib
import base64
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from product_capabilities import product_capabilities


class OpenClawDockerAdapter:
    CAPABILITIES = product_capabilities("openclaw")

    def supports(self, action):
        return action in self.CAPABILITIES

    def get_runtime_target(self, instance):
        if not isinstance(instance, dict):
            raise TypeError("adapter runtime operations require an instance record")
        target = instance.get("runtime_identifier")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("instance runtime_identifier is required")
        return target.strip()

    def get_legacy_user_id(self, instance, required=True):
        if not isinstance(instance, dict):
            raise TypeError("adapter filesystem operations require an instance record")
        user_id = instance.get("legacy_user_id") or instance.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            if not required:
                return None
            raise ValueError("instance legacy_user_id is required")
        return user_id.strip()

    def __init__(self, manager_dir, public_dir, nginx_users_conf_dir, nginx_compose_dir, nginx_container_name):
        self.manager_dir = Path(manager_dir)
        self.public_dir = Path(public_dir)
        self.nginx_users_conf_dir = Path(nginx_users_conf_dir)
        self.nginx_compose_dir = Path(nginx_compose_dir)
        self.nginx_container_name = nginx_container_name

    def user_dir(self, user_id):
        return self.public_dir / "users" / user_id

    def run_command(self, command, timeout=30, cwd=None, env=None):
        result = subprocess.run(
            command,
            cwd=str(cwd or self.manager_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode, output

    def reload_nginx(self):
        test_code, test_output = self.run_command(["docker", "exec", self.nginx_container_name, "nginx", "-t"], timeout=30)
        if test_code != 0:
            return test_code, f"Nginx test failed:\n{test_output}"

        reload_code, reload_output = self.run_command(
            ["docker", "exec", self.nginx_container_name, "nginx", "-s", "reload"],
            timeout=30,
        )
        if reload_code != 0:
            return reload_code, f"Nginx reload failed:\n{reload_output}"

        return 0, "\n".join(part for part in [test_output, reload_output] if part)

    def apply_nginx_compose(self, compose_file, timeout=120):
        code, output = self.run_command(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            timeout=timeout,
        )
        if code != 0:
            return code, output
        reconnect_code, reconnect_output = self.run_command(
            [
                "bash", "-lc",
                'source "$1"; connect_shared_services_to_tenant_networks "$2" "$3"',
                "bash",
                str(self.manager_dir / "scripts" / "lib_tenant_network.sh"),
                self.nginx_container_name,
                os.environ.get("MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy"),
            ],
            timeout=90,
        )
        return reconnect_code, "\n".join(
            part for part in (output, reconnect_output) if part
        )

    def nginx_disabled_conf_dir(self):
        return self.nginx_users_conf_dir / "_disabled"

    def nginx_legacy_disabled_conf_dir(self):
        return Path(str(self.nginx_users_conf_dir) + ".disabled")

    def nginx_active_user_conf(self, user_id):
        return self.nginx_users_conf_dir / f"{user_id}.conf"

    def nginx_disabled_user_conf(self, user_id):
        return self.nginx_disabled_conf_dir() / f"{user_id}.conf"

    def nginx_legacy_disabled_user_conf(self, user_id):
        return self.nginx_legacy_disabled_conf_dir() / f"{user_id}.conf"

    def nginx_user_conf_candidates(self, user_id):
        return [
            self.nginx_active_user_conf(user_id),
            self.nginx_disabled_user_conf(user_id),
            self.nginx_legacy_disabled_user_conf(user_id),
        ]

    def disable_nginx_user_conf(self, user_id):
        active_conf = self.nginx_active_user_conf(user_id)
        disabled_conf = self.nginx_disabled_user_conf(user_id)
        if not active_conf.is_file():
            if disabled_conf.is_file():
                return 0, f"Nginx config already disabled: {disabled_conf}"
            return 0, f"Nginx config not found, skip disabling: {active_conf}"

        disabled_conf.parent.mkdir(parents=True, exist_ok=True)
        if disabled_conf.exists():
            return 1, f"Disabled nginx config already exists: {disabled_conf}"

        shutil.move(str(active_conf), str(disabled_conf))
        reload_code, reload_output = self.reload_nginx()
        if reload_code == 0:
            return 0, f"Disabled nginx config: {disabled_conf}\n{reload_output}".strip()

        shutil.move(str(disabled_conf), str(active_conf))
        rollback_code, rollback_output = self.reload_nginx()
        rollback_note = "\nRolled back nginx config disable."
        if rollback_code != 0:
            rollback_note += f"\nRollback reload failed:\n{rollback_output}"
        return reload_code, f"{reload_output}{rollback_note}"

    def enable_nginx_user_conf(self, user_id):
        active_conf = self.nginx_active_user_conf(user_id)
        disabled_conf = self.nginx_disabled_user_conf(user_id)
        legacy_disabled_conf = self.nginx_legacy_disabled_user_conf(user_id)
        if active_conf.is_file():
            return 0, f"Nginx config already enabled: {active_conf}"
        if not disabled_conf.is_file():
            if legacy_disabled_conf.is_file():
                disabled_conf = legacy_disabled_conf
            else:
                return 1, f"Disabled nginx config not found: {disabled_conf}"

        shutil.move(str(disabled_conf), str(active_conf))
        reload_code, reload_output = self.reload_nginx()
        if reload_code == 0:
            return 0, f"Enabled nginx config: {active_conf}\n{reload_output}".strip()

        shutil.move(str(active_conf), str(disabled_conf))
        rollback_code, rollback_output = self.reload_nginx()
        rollback_note = "\nRolled back nginx config enable."
        if rollback_code != 0:
            rollback_note += f"\nRollback reload failed:\n{rollback_output}"
        return reload_code, f"{reload_output}{rollback_note}"

    def status(self, instance):
        runtime_target = self.get_runtime_target(instance)
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", runtime_target],
            cwd=str(self.manager_dir),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return "Up" if result.returncode == 0 and result.stdout.strip() == "running" else "STOPPED"

    def logs(self, instance, tail=120):
        runtime_target = self.get_runtime_target(instance)
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), runtime_target],
            cwd=str(self.manager_dir),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode != 0:
            return output or "Could not read container logs."
        return output or "No recent logs."

    def start(self, instance):
        runtime_target = self.get_runtime_target(instance)
        legacy_user_id = self.get_legacy_user_id(instance, required=False)
        start_code, start_output = self.run_command(["docker", "start", runtime_target], timeout=90)
        if start_code != 0 or not legacy_user_id:
            return start_code, start_output

        nginx_code, nginx_output = self.enable_nginx_user_conf(legacy_user_id)
        combined_output = "\n".join(part for part in [start_output, nginx_output] if part)
        if nginx_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = self.run_command(["docker", "stop", runtime_target], timeout=60)
        rollback_note = "\nRolled back container start."
        if rollback_code != 0:
            rollback_note += f"\nRollback stop failed:\n{rollback_output}"
        return nginx_code, f"{combined_output}{rollback_note}"

    def stop(self, instance):
        runtime_target = self.get_runtime_target(instance)
        legacy_user_id = self.get_legacy_user_id(instance, required=False)
        nginx_code, nginx_output = (0, "")
        if legacy_user_id:
            nginx_code, nginx_output = self.disable_nginx_user_conf(legacy_user_id)
            if nginx_code != 0:
                return nginx_code, nginx_output

        stop_code, stop_output = self.run_command(["docker", "stop", runtime_target], timeout=60)
        combined_output = "\n".join(part for part in [nginx_output, stop_output] if part)
        if stop_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = (0, "")
        if legacy_user_id:
            rollback_code, rollback_output = self.enable_nginx_user_conf(legacy_user_id)
        rollback_note = "\nRolled back nginx config disable."
        if rollback_code != 0:
            rollback_note += f"\nRollback enable failed:\n{rollback_output}"
        return stop_code, f"{combined_output}{rollback_note}"

    def restart(self, instance):
        return self.run_command(["docker", "restart", self.get_runtime_target(instance)], timeout=90)

    def set_basic_auth(self, instance, enabled):
        user_id = self.get_legacy_user_id(instance)
        nginx_conf = self.nginx_active_user_conf(user_id)
        if not nginx_conf.is_file():
            return 1, f"Nginx config not found: {nginx_conf}"
        backup = nginx_conf.read_bytes()
        try:
            code, output = self.run_command(
                [
                    str(self.manager_dir / "scripts" / "set_basic_auth.sh"),
                    "true" if enabled else "false",
                    user_id,
                ],
                timeout=30,
                env={
                    **os.environ,
                    "OPENCLAW_SKIP_METADATA_WRITE": "1",
                    "OPENCLAW_SKIP_HTPASSWD_PERMISSIONS": "1",
                },
            )
        except Exception:
            nginx_conf.write_bytes(backup)
            raise
        if code != 0:
            nginx_conf.write_bytes(backup)
            return code, output
        try:
            reload_code, reload_output = self.reload_nginx()
        except Exception:
            nginx_conf.write_bytes(backup)
            try:
                self.reload_nginx()
            except Exception:
                pass
            raise
        combined = "\n".join(part for part in (output, reload_output) if part)
        if reload_code == 0:
            return 0, combined
        nginx_conf.write_bytes(backup)
        rollback_code, rollback_output = self.reload_nginx()
        rollback_note = "\nRestored Nginx config."
        if rollback_code != 0:
            rollback_note += f"\nRollback reload failed:\n{rollback_output}"
        return reload_code, f"{combined}{rollback_note}"

    def create(
        self, instance, basic_auth_enabled, basic_auth_password="",
        skip_nginx_reload=True, skip_metadata_write=False, timeout=420,
    ):
        user_id = self.get_legacy_user_id(instance)
        command = [
            str(self.manager_dir / "scripts" / "create_user.sh"),
            user_id,
            "--basic-auth-enabled",
            basic_auth_enabled,
        ]
        if skip_nginx_reload:
            command.append("--skip-nginx-reload")
        env = {**os.environ, "OPENCLAW_BASIC_AUTH_PASSWORD": basic_auth_password}
        if skip_metadata_write:
            env["OPENCLAW_SKIP_METADATA_WRITE"] = "1"
        return self.run_command(
            command,
            timeout=timeout,
            env=env,
        )

    def batch_create(self, input_csv, output_csv, timeout, skip_nginx_refresh=False):
        command = [str(self.manager_dir / "scripts" / "batch_create_users.sh"), str(input_csv), str(output_csv)]
        if skip_nginx_refresh:
            command.append("--skip-nginx-refresh")
        return self.run_command(
            command,
            timeout=timeout,
        )

    def delete(self, instance):
        return self._run_interruptible_command(
            [str(self.manager_dir / "scripts" / "delete_user.sh"), self.get_legacy_user_id(instance)],
            timeout=180,
        )

    def restore(self, instance):
        return self._run_interruptible_command(
            [str(self.manager_dir / "scripts" / "restore_user.sh"), self.get_legacy_user_id(instance)],
            timeout=240,
        )

    def update_version(self, instance, version, restore_model_provider=False, timeout=600):
        user_id = self.get_legacy_user_id(instance)
        user_dir = self.user_dir(user_id)
        compose_file = user_dir / "docker-compose.yml"
        if not compose_file.is_file():
            return 1, f"Compose file not found: {compose_file}"
        command = [
            str(self.manager_dir / "scripts" / "update_instance_version.sh"),
            user_id,
            version,
        ]
        if restore_model_provider:
            command.append("--restore-model-provider")
        process = None
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            def interrupt_update(signum, frame):
                raise SystemExit(128 + signum)

            previous_sigterm = signal.signal(
                signal.SIGTERM,
                interrupt_update,
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.manager_dir),
                env={**os.environ, "OPENCLAW_SKIP_METADATA_WRITE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output, _ = process.communicate(timeout=timeout)
            return process.returncode, output.strip()
        except BaseException:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    def set_model_provider(
        self, instance, provider_id, model_id, base_url="", alias="", timeout=180,
    ):
        return self.run_command(
            [
                str(self.manager_dir / "scripts" / "set_model_provider.sh"),
                self.get_legacy_user_id(instance), provider_id, model_id,
                base_url, alias or model_id,
            ],
            timeout=timeout,
        )

    def install_skill(self, instance, skill_id, request_id, timeout=180):
        runtime_target = self.get_runtime_target(instance)
        validation_code, validation_output = self._validate_skill_install(
            runtime_target, skill_id
        )
        if validation_code != 0:
            return validation_code, validation_output
        user_dir = self.user_dir(self.get_legacy_user_id(instance))
        skills_dir = user_dir / "skills"
        backup_parent = user_dir / "backups" / "skill-installs"
        backup_parent.mkdir(parents=True, exist_ok=True)
        backup_dir = backup_parent / hashlib.sha256(request_id.encode()).hexdigest()
        if backup_dir.exists():
            self._restore_skills(skills_dir, backup_dir)
        else:
            staging = Path(tempfile.mkdtemp(prefix="snapshot-", dir=backup_parent))
            if skills_dir.exists():
                shutil.copytree(skills_dir, staging / "skills")
            else:
                (staging / "skills-missing").touch()
            staging.rename(backup_dir)
        process = None
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            def interrupt_install(signum, frame):
                raise SystemExit(128 + signum)

            previous_sigterm = signal.signal(signal.SIGTERM, interrupt_install)
        try:
            process = subprocess.Popen(
                [
                    "docker", "exec", runtime_target, "timeout", f"{timeout}s",
                    "openclaw", "skills", "install", skill_id,
                ],
                cwd=str(self.manager_dir),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output, _ = process.communicate(timeout=timeout + 10)
            code = process.returncode
        except BaseException:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            kill_code, kill_output = self.run_command(
                ["docker", "exec", runtime_target, "pkill", "-9", "-f", "openclaw skills install"],
                timeout=10,
            )
            if kill_code not in {0, 1}:
                raise RuntimeError(
                    f"Could not stop container skill installer; snapshot retained: {backup_dir}\n{kill_output}"
                )
            self._restore_skills(skills_dir, backup_dir)
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
        if code == 0:
            return code, output.strip()
        rollback_output = self._restore_skills(skills_dir, backup_dir)
        return code, f"{output.strip()}\n{rollback_output}".strip()

    def refresh_devices(self, instance, timeout=60):
        return self._run_device_command(
            [
                str(self.manager_dir / "scripts" / "approve_device.sh"),
                self.get_legacy_user_id(instance),
                "--list-only",
            ],
            instance,
            timeout,
        )

    def approve_latest_device(self, instance, request_id, timeout=120):
        return self._run_device_command(
            [
                str(self.manager_dir / "scripts" / "approve_device.sh"),
                self.get_legacy_user_id(instance),
                "--latest",
            ],
            instance,
            timeout,
            env={
                "OPENCLAW_EXECUTION_REQUEST_ID": request_id,
            },
        )

    def _run_device_command(self, command, instance, timeout, env=None):
        return self._run_interruptible_command(
            command,
            timeout,
            env={
                **(env or {}),
                "OPENCLAW_RUNTIME_TARGET": self.get_runtime_target(instance),
            },
        )

    def _run_interruptible_command(self, command, timeout, env=None):
        process = None
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            def interrupt_device_action(signum, frame):
                raise SystemExit(128 + signum)

            previous_sigterm = signal.signal(signal.SIGTERM, interrupt_device_action)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.manager_dir),
                env={
                    **os.environ,
                    **(env or {}),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output, _ = process.communicate(timeout=timeout)
            return process.returncode, output.strip()
        except BaseException:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    def _validate_skill_install(self, runtime_target, skill_id):
        info_code, info_output = self.run_command(
            [
                "docker", "exec", runtime_target, "openclaw", "skills",
                "info", skill_id, "--json",
            ],
            timeout=30,
        )
        if info_code == 0:
            try:
                info = json.loads(info_output)
            except json.JSONDecodeError:
                return 1, "Skill info returned invalid JSON; installation refused."
            if info.get("source") == "openclaw-bundled" or info.get("bundled") is True:
                missing = info.get("missing") or {}
                requirements = ", ".join(
                    str(value)
                    for values in missing.values()
                    if isinstance(values, list)
                    for value in values
                )
                suffix = f" Missing requirements: {requirements}." if requirements else ""
                return 1, f"Skill is already bundled; installation refused.{suffix}"

        search_code, search_output = self.run_command(
            [
                "docker", "exec", runtime_target, "openclaw", "skills",
                "search", skill_id, "--json", "--limit", "100",
            ],
            timeout=30,
        )
        if search_code != 0:
            return 1, f"Could not verify Skill uniqueness:\n{search_output}"
        try:
            results = json.loads(search_output).get("results", [])
        except (AttributeError, json.JSONDecodeError):
            return 1, "Skill search returned invalid JSON; installation refused."
        candidates = [
            result
            for result in results
            if isinstance(result, dict) and result.get("slug") == skill_id
        ]
        references = [
            result.get("install", {}).get("reference")
            for result in candidates
            if isinstance(result.get("install"), dict)
            and result["install"].get("reference")
        ]
        if len(candidates) != 1 or len(references) != 1:
            detail = ", ".join(sorted(references)) or "no exact candidate"
            return 1, f"Skill slug is ambiguous or unavailable ({detail}); installation refused."
        return 0, f"Verified unique Skill source: {references[0]}"

    def _restore_skills(self, skills_dir, backup_dir):
        staging = Path(tempfile.mkdtemp(prefix="restore-", dir=skills_dir.parent))
        backup = backup_dir / "skills"
        if backup.exists():
            shutil.copytree(backup, staging / "skills")
        restored = staging / "skills"
        failed_dir = backup_dir / "failed-install-data"
        if failed_dir.exists():
            shutil.rmtree(failed_dir)
        if skills_dir.exists():
            skills_dir.rename(failed_dir)
        try:
            if restored.exists():
                restored.rename(skills_dir)
        except BaseException:
            if failed_dir.exists() and not skills_dir.exists():
                failed_dir.rename(skills_dir)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return f"Skill data restored; failed install data: {failed_dir}"

    def batch_set_model_provider(self, input_csv, output_csv, timeout):
        return self.run_command(
            [str(self.manager_dir / "scripts" / "batch_set_model_provider.sh"), str(input_csv), str(output_csv)],

            timeout=timeout,
        )

class EvoScientistDockerAdapter(OpenClawDockerAdapter):
    CAPABILITIES = product_capabilities("evoscientist")
    IMAGE_REPOSITORY = "ghcr.io/evoscientist/evoscientist"
    DEFAULT_DIGEST = "sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47"
    _SAFE_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    _SAFE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
    INGRESS_IMAGE = "nginx:alpine"
    _PROXY_SCRIPT = """import socket
import threading

LISTEN = (\"0.0.0.0\", 6175)
TARGET = (\"127.0.0.1\", 6174)

def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    finally:
        src.close()
        dst.close()

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(LISTEN)
server.listen(64)
while True:
    client, _ = server.accept()
    upstream = socket.create_connection(TARGET)
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
"""

    def supports(self, action):
        return action in self.CAPABILITIES

    def container_names(self, instance):
        runtime_target = self.get_runtime_target(instance)
        return [runtime_target, f"{runtime_target}-proxy"]

    def ingress_container_name(self, instance):
        public_id = instance.get("public_id")
        if not isinstance(public_id, str) or not self._SAFE_DOCKER_NAME.fullmatch(public_id):
            raise ValueError("EvoScientist instance public_id is invalid")
        return f"{self.get_runtime_target(instance)}-ingress"

    def tenant_network(self, instance):
        return "openclaw-user-" + hashlib.sha256(
            self.get_legacy_user_id(instance).encode()
        ).hexdigest()

    def _data_paths(self, instance):
        user_dir = Path(instance.get("data_path") or "")
        expected = self.user_dir(self.get_legacy_user_id(instance))
        if user_dir != expected:
            raise ValueError("EvoScientist data path is invalid")
        return user_dir, user_dir / "workspace", user_dir / "evoscientist-data", user_dir / "tcp_proxy.py"

    def recycle_dir(self, instance):
        public_id = instance.get("public_id")
        if not isinstance(public_id, str) or not self._SAFE_DOCKER_NAME.fullmatch(public_id):
            raise ValueError("EvoScientist instance public_id is invalid")
        return self.public_dir / "deleted" / "evoscientist" / public_id

    def ingress_conf(self, instance, disabled=False):
        user_id = self.get_legacy_user_id(instance)
        return self.nginx_disabled_user_conf(user_id) if disabled else self.nginx_active_user_conf(user_id)

    def _existing_ingress_conf(self, instance, disabled=False):
        return self.ingress_conf(instance, disabled=disabled)

    def _allocate_port(self):
        port_file = os.environ.get("PORT_FILE", str(self.public_dir / "ports.txt"))
        code, output = self.run_command(
            ["bash", "-lc", 'source "$1"; allocate_port "$2" "$3" "$4"', "bash",
             str(self.manager_dir / "scripts" / "lib_port_allocator.sh"), port_file,
             os.environ.get("PORT_START", "40001"), os.environ.get("PORT_END", "49999")],
            timeout=30,
        )
        ports = [line for line in output.splitlines() if line.isdigit()]
        if code != 0 or len(ports) != 1:
            raise RuntimeError(output or "EvoScientist ingress port allocation failed")
        return int(ports[0])

    def _image_ref(self, digest):
        if not isinstance(digest, str) or not self._SAFE_DIGEST.fullmatch(digest.lower()):
            raise ValueError("EvoScientist version must be a sha256 image digest")
        return f"{self.IMAGE_REPOSITORY}@{digest.lower()}"

    def _configured_image(self):
        image = os.environ.get(
            "EVOSCIENTIST_IMAGE", f"{self.IMAGE_REPOSITORY}@{self.DEFAULT_DIGEST}"
        ).strip()
        prefix = self.IMAGE_REPOSITORY + "@"
        if not image.startswith(prefix):
            raise ValueError("EVOSCIENTIST_IMAGE must use the official repository and a sha256 digest")
        self._image_ref(image.removeprefix(prefix))
        return image

    def _write_htpasswd(self, user_id, password):
        base = Path(os.environ.get("NGINX_HTPASSWD_FILE", "/data/docker/nginx/auth/.htpasswd"))
        target = base.parent / "users" / user_id / ".htpasswd"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["htpasswd", "-ci", str(target), user_id], input=password + "\n",
            text=True, capture_output=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stdout + "\n" + result.stderr).strip())
        target.chmod(0o644)

    def _run_containers(self, instance, image, network, timeout=420):
        runtime_target = self.get_runtime_target(instance)
        _, workspace, data_dir, proxy_script = self._data_paths(instance)
        code, output = self.run_command(
            [
                "docker", "run", "-d", "--name", runtime_target,
                "--restart", "unless-stopped", "--network", network,
                "-v", f"{workspace}:/workspace",
                "-v", f"{data_dir}:/home/evosci/.evoscientist",
                "-e", "EVOSCIENTIST_WORKSPACE_DIR=/workspace",
                "-e", "EVOSCIENTIST_DATA_DIR=/home/evosci/.evoscientist",
                image, "--ui", "webui",
            ],
            timeout=timeout,
        )
        if code != 0:
            return code, output
        code, proxy_output = self.run_command(
            [
                "docker", "run", "-d", "--name", f"{runtime_target}-proxy",
                "--restart", "unless-stopped", "--network", f"container:{runtime_target}",
                "--entrypoint", "python", "-v", f"{proxy_script}:/tmp/tcp_proxy.py:ro",
                image, "/tmp/tcp_proxy.py",
            ],
            timeout=timeout,
        )
        if code != 0:
            self.run_command(["docker", "rm", "-f", runtime_target], timeout=60)
        return code, "\n".join(part for part in (output, proxy_output) if part)

    def _wait_for_services(self, instance):
        for name in self.container_names(instance):
            code, output = self.run_command(
                ["docker", "inspect", "--format", "{{.State.Running}}", name], timeout=10
            )
            if code != 0 or output.strip() != "true":
                return 1, output or f"{name} is not running"
        return 0, "EvoScientist services are running"

    def configure_ingress(self, instance):
        port = instance.get("_created_port", instance.get("port"))
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return 1, "EvoScientist ingress port is required"
        runtime_target = self.get_runtime_target(instance)
        conf = self.ingress_conf(instance)
        ingress_container = self.ingress_container_name(instance)
        cert = os.environ.get("NGINX_SSL_CERT", "/etc/nginx/certs/fullchain.pem")
        key = os.environ.get("NGINX_SSL_KEY", "/etc/nginx/certs/privkey.pem")
        try:
            conf.parent.mkdir(parents=True, exist_ok=True)
            config_text = (
                f"upstream evosci_ui_{port} {{\n    resolver 127.0.0.11 valid=10s ipv6=off;\n"
                f"    server {runtime_target}:4716 resolve;\n}}\n\n"
                f"upstream evosci_api_{port} {{\n    resolver 127.0.0.11 valid=10s ipv6=off;\n"
                f"    server {runtime_target}:6175 resolve;\n}}\n\n"
                "server {\n"
                f"    listen {port} ssl;\n    server_name _;\n"
                f"    ssl_certificate {cert};\n    ssl_certificate_key {key};\n"
                f'    auth_basic "OpenClaw Login";\n    auth_basic_user_file /etc/nginx/auth/users/{self.get_legacy_user_id(instance)}/.htpasswd;\n'
                "    location = /api/workspace/upload { proxy_pass http://evosci_ui_"
                f"{port}; proxy_set_header Host $host:$server_port; proxy_set_header Origin \"\"; }}\n"
                f"    location /api/ {{ proxy_pass http://evosci_api_{port}/; proxy_http_version 1.1; proxy_buffering off; }}\n"
                f"    location / {{ proxy_pass http://evosci_ui_{port}; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection \"upgrade\"; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; }}\n"
                "}\n"
            )
            config_file = self.public_dir / "deleted" / "evoscientist" / f"{instance['public_id']}.nginx.conf"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(config_text, encoding="utf-8")
            network = self.tenant_network(instance)
            auth_dir = Path(os.environ.get("NGINX_AUTH_DIR", "/data/docker/nginx/auth"))
            cert_dir = Path(os.environ.get("NGINX_CERTS_DIR", "/data/docker/nginx/certs"))
            code, output = self.run_command(
                ["docker", "rm", "-f", ingress_container], timeout=60
            )
            code, output = self.run_command(
                [
                    "docker", "run", "-d", "--name", ingress_container,
                    "--restart", "unless-stopped", "--network", network,
                    "-p", f"{port}:443",
                    "-v", f"{config_file}:/etc/nginx/conf.d/default.conf:ro",
                    "-v", f"{cert_dir}:/etc/nginx/certs:ro",
                    "-v", f"{auth_dir}:/etc/nginx/auth:ro",
                    self.INGRESS_IMAGE,
                ], timeout=120,
            )
            if code != 0:
                config_file.unlink(missing_ok=True)
            return code, output
        except Exception as exc:
            return 1, f"EvoScientist ingress configuration failed: {exc}"

    def _inspect_deployment(self, instance):
        code, output = self.run_command(
            [
                "docker", "inspect", "--format",
                '{{.Image}}|{{.State.Running}}|{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s " $name}}{{end}}',
                self.get_runtime_target(instance),
            ],
            timeout=10,
        )
        if code != 0:
            raise RuntimeError(output or "EvoScientist container not found")
        parts = output.strip().split("|", 2)
        networks = parts[2].split() if len(parts) == 3 else []
        if len(parts) != 3 or parts[1] not in {"true", "false"} or len(networks) != 1:
            raise RuntimeError("Could not inspect EvoScientist deployment")
        return parts[0], parts[1] == "true", networks[0]

    def _remove_containers(self, instance):
        outputs = []
        for name in reversed(self.container_names(instance)):
            code, output = self.run_command(["docker", "rm", "-f", name], timeout=60)
            outputs.append(output)
            if code != 0 and "no such container" not in output.lower():
                return code, "\n".join(part for part in outputs if part)
        return 0, "\n".join(part for part in outputs if part)

    def _container_status(self, container_name):
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
            cwd=str(self.manager_dir),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return "MISSING"
        status = result.stdout.strip()
        return "Up" if status == "running" else "STOPPED"

    def status(self, instance):
        statuses = {
            name: self._container_status(name)
            for name in self.container_names(instance)
        }
        if all(value.startswith("Up") for value in statuses.values()):
            return "Up (" + "; ".join(f"{name}={value}" for name, value in statuses.items()) + ")"
        if all(value == "STOPPED" for value in statuses.values()):
            return "STOPPED"
        return "DEGRADED (" + "; ".join(f"{name}={value}" for name, value in statuses.items()) + ")"

    def logs(self, instance, tail=120):
        outputs = []
        failed = False
        for container_name in self.container_names(instance):
            code, output = self.run_command(
                ["docker", "logs", "--tail", str(tail), container_name],
                timeout=10,
            )
            failed = failed or code != 0
            outputs.append(f"===== {container_name} =====\n{output or 'No recent logs.'}")
        combined = "\n".join(outputs)
        return combined if not failed else f"{combined}\n[WARN] One or more container logs could not be read."

    def start(self, instance):
        started = []
        names = self.container_names(instance)
        if instance.get("public_id") and self._container_status(self.ingress_container_name(instance)) != "MISSING":
            names.append(self.ingress_container_name(instance))
        for container_name in names:
            code, output = self.run_command(["docker", "start", container_name], timeout=90)
            if code != 0:
                for started_name in reversed(started):
                    self.run_command(["docker", "stop", started_name], timeout=60)
                return code, output
            started.append(container_name)

        return 0, ""

    def stop(self, instance):
        outputs = []
        stopped = []
        names = list(self.container_names(instance))
        if instance.get("public_id") and self._container_status(self.ingress_container_name(instance)) != "MISSING":
            names.append(self.ingress_container_name(instance))
        for container_name in reversed(names):
            code, output = self.run_command(["docker", "stop", container_name], timeout=60)
            outputs.append(output)
            if code != 0:
                for stopped_name in reversed(stopped):
                    self.run_command(["docker", "start", stopped_name], timeout=90)
                return code, "\n".join(part for part in outputs if part)
            stopped.append(container_name)
        return 0, "\n".join(part for part in outputs if part)

    def restart(self, instance):
        outputs = []
        names = list(self.container_names(instance))
        if instance.get("public_id") and self._container_status(self.ingress_container_name(instance)) != "MISSING":
            names.append(self.ingress_container_name(instance))
        for container_name in names:
            code, output = self.run_command(["docker", "restart", container_name], timeout=90)
            outputs.append(output)
            if code != 0:
                return code, "\n".join(part for part in outputs if part)
        return 0, "\n".join(part for part in outputs if part)

    def create(self, instance, basic_auth_enabled, basic_auth_password="", **kwargs):
        del kwargs
        if basic_auth_enabled != "true" or not basic_auth_password:
            return 1, "Invalid EvoScientist creation parameters."
        try:
            image = self._configured_image()
            user_id = self.get_legacy_user_id(instance)
            if not self._SAFE_DOCKER_NAME.fullmatch(self.get_runtime_target(instance)):
                raise ValueError("Invalid EvoScientist runtime identifier")
            user_dir, workspace, data_dir, proxy_script = self._data_paths(instance)
            if user_dir.exists() and any(path.exists() for path in (workspace, data_dir, proxy_script)):
                raise ValueError(f"EvoScientist data path already exists: {user_dir}")
            network = self.tenant_network(instance)
            network_exists = self.run_command(["docker", "network", "inspect", network], timeout=10)[0] == 0
            code, output = self.run_command(
                ["bash", "-lc", 'source "$1"; prepare_tenant_networks "$2"', "bash",
                 str(self.manager_dir / "scripts" / "lib_tenant_network.sh"), network], timeout=60)
            if code != 0:
                return code, output
            user_dir.mkdir(parents=True, exist_ok=True)
            workspace.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            proxy_script.write_text(self._PROXY_SCRIPT, encoding="utf-8")
            proxy_script.chmod(0o644)
            if basic_auth_enabled == "true":
                self._write_htpasswd(user_id, basic_auth_password)
            code, output = self._run_containers(instance, image, network)
            if code != 0:
                raise RuntimeError(output)
            code, output = self._wait_for_services(instance)
            if code != 0:
                raise RuntimeError(output)
            instance["_created_port"] = int(instance.get("port") or 0) or self._allocate_port()
            return 0, output
        except Exception as exc:
            self._remove_containers(instance)
            user_dir = locals().get("user_dir")
            if user_dir is not None and user_dir.is_dir():
                for path in (locals().get("workspace"), locals().get("data_dir"), locals().get("proxy_script")):
                    if path is not None:
                        Path(path).unlink(missing_ok=True) if Path(path).is_file() else shutil.rmtree(path, ignore_errors=True)
                try:
                    user_dir.rmdir()
                except OSError:
                    pass
            if not locals().get("network_exists", True):
                self.run_command(["docker", "network", "rm", self.tenant_network(instance)], timeout=30)
            return 1, f"EvoScientist creation failed and was rolled back: {exc}"

    def delete(self, instance):
        try:
            _, workspace, data_dir, proxy_script = self._data_paths(instance)
            recycle = self.recycle_dir(instance)
            if recycle.exists():
                return 1, f"Recycle path already exists: {recycle}"
            image, was_running, network = self._inspect_deployment(instance)
            conf = self._existing_ingress_conf(instance)
            old_conf = conf.read_text(encoding="utf-8") if conf.is_file() else ""
            code, output = self._remove_containers(instance)
            if code != 0:
                return code, output
            recycle.mkdir(parents=True)
            for path in (workspace, data_dir, proxy_script):
                if path.exists():
                    shutil.move(str(path), str(recycle / path.name))
            self.run_command(["docker", "rm", "-f", self.ingress_container_name(instance)], timeout=60)
            self._existing_ingress_conf(instance).unlink(missing_ok=True)
            config_file = self.public_dir / "deleted" / "evoscientist" / f"{instance['public_id']}.nginx.conf"
            config_file.unlink(missing_ok=True)
            (recycle / "manifest.json").write_text(json.dumps({
                "image": image, "network": network, "was_running": was_running,
                "port": instance.get("port"),
                "ingress": old_conf,
            }), encoding="utf-8")
            return 0, "EvoScientist resources moved to recycle bin."
        except Exception as exc:
            rollback_errors = []
            try:
                if 'old_conf' in locals() and old_conf:
                    conf.write_text(old_conf, encoding="utf-8")
                if 'recycle' in locals() and recycle.is_dir():
                    for name, target in (("workspace", workspace), ("evoscientist-data", data_dir), ("tcp_proxy.py", proxy_script)):
                        source = recycle / name
                        if source.exists() and not target.exists():
                            shutil.move(str(source), str(target))
                    shutil.rmtree(recycle, ignore_errors=True)
                if 'image' in locals():
                    code, output = self._run_containers(instance, image, network)
                    if code != 0:
                        rollback_errors.append(output)
                    elif not was_running:
                        self.stop(instance)
                code, output = self.configure_ingress(instance)
                if code != 0:
                    rollback_errors.append(output)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                return 1, f"EvoScientist deletion failed; manual recovery is required: {exc}; {'; '.join(rollback_errors)}"
            return 1, f"EvoScientist deletion failed and was rolled back: {exc}"

    def restore(self, instance):
        try:
            user_dir, workspace, data_dir, proxy_script = self._data_paths(instance)
            recycle = self.recycle_dir(instance)
            if not recycle.is_dir():
                return 1, f"Recycle path not found: {recycle}"
            manifest = recycle / "manifest.json"
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            image = saved["image"]
            network = saved["network"]
            was_running = bool(saved["was_running"])
            if not isinstance(image, str) or not (
                image.startswith(self.IMAGE_REPOSITORY + "@") or self._SAFE_DIGEST.fullmatch(image)
            ):
                raise ValueError("invalid EvoScientist recycle image")
            code, output = self.run_command(
                ["bash", "-lc", 'source "$1"; prepare_tenant_networks "$2"', "bash",
                 str(self.manager_dir / "scripts" / "lib_tenant_network.sh"), network], timeout=60)
            if code != 0:
                return code, output
            for name, target in (("workspace", workspace), ("evoscientist-data", data_dir), ("tcp_proxy.py", proxy_script)):
                source = recycle / name
                if source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
            code, output = self._run_containers(instance, image, network)
            if code != 0:
                raise RuntimeError(output)
            instance["_created_port"] = saved.get("port", instance.get("port"))
            code, output = self.configure_ingress(instance)
            if code != 0:
                raise RuntimeError(output)
            shutil.rmtree(recycle)
            if not was_running:
                self.stop(instance)
            return 0, "EvoScientist instance restored."
        except Exception as exc:
            self._remove_containers(instance)
            for name, target in (("workspace", workspace), ("evoscientist-data", data_dir), ("tcp_proxy.py", proxy_script)):
                source = recycle / name
                if source.exists() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
            return 1, f"EvoScientist restore failed: {exc}"

    def update_version(self, instance, version, **kwargs):
        del kwargs
        try:
            target_image = self._image_ref(version)
            current_image, was_running, network = self._inspect_deployment(instance)
            code, output = self.run_command(["docker", "image", "inspect", target_image], timeout=30)
            if code != 0:
                return 1, f"Target image is not available locally: {target_image}. Run: docker pull '{target_image}'"
            code, output = self._remove_containers(instance)
            if code != 0:
                return code, output
            code, output = self._run_containers(instance, target_image, network)
            if code != 0:
                raise RuntimeError(output)
            code, output = self._wait_for_services(instance)
            if code != 0:
                raise RuntimeError(output)
            if not was_running:
                self.stop(instance)
            return 0, f"EvoScientist image updated to {target_image}."
        except Exception as exc:
            self._remove_containers(instance)
            try:
                code, output = self._run_containers(instance, current_image, network)
                if code != 0:
                    raise RuntimeError(output)
                code, output = self._wait_for_services(instance)
                if code != 0:
                    raise RuntimeError(output)
                if not was_running:
                    self.stop(instance)
            except Exception as rollback:
                return 1, f"EvoScientist update failed; rollback failed: {exc}; {rollback}"
            return 1, f"EvoScientist update failed and was rolled back: {exc}"


class HermesDockerAdapter(OpenClawDockerAdapter):
    CAPABILITIES = product_capabilities("hermes")
    IMAGE = "nousresearch/hermes-agent:v2026.7.20"

    _SAFE_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    _SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    @staticmethod
    def _password_hash(password):
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return (
            "scrypt$16384$8$1$"
            + base64.b64encode(salt).decode()
            + "$"
            + base64.b64encode(derived).decode()
        )

    def ingress_conf(self, instance, disabled=False):
        public_id = instance.get("public_id")
        if not isinstance(public_id, str) or not self._SAFE_DOCKER_NAME.fullmatch(public_id):
            raise ValueError("Hermes instance public_id is not safe for ingress")
        directory = self.nginx_disabled_conf_dir() if disabled else self.nginx_users_conf_dir
        return directory / f"hermes-{public_id}.conf"

    def tenant_network(self, instance):
        user_id = self.get_legacy_user_id(instance)
        return "openclaw-user-" + hashlib.sha256(user_id.encode()).hexdigest()

    def hermes_recycle_dir(self, instance):
        public_id = instance.get("public_id")
        if not isinstance(public_id, str) or not self._SAFE_DOCKER_NAME.fullmatch(public_id):
            raise ValueError("Hermes instance public_id is not safe for retention")
        return self.public_dir / "deleted" / "hermes" / public_id

    def _hermes_data_path(self, instance):
        data_path = Path(instance.get("data_path") or "")
        if data_path.parent != self.public_dir / "hermes":
            raise ValueError("Hermes data path is invalid")
        return data_path

    def _inspect_hermes_container(self, instance):
        code, output = self.run_command(
            [
                "docker", "inspect", "--format",
                '{{.Config.Image}}|{{.State.Running}}|{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s " $name}}{{end}}',
                self.get_runtime_target(instance),
            ],
            timeout=10,
        )
        if code != 0:
            raise RuntimeError(output or "Hermes container not found.")
        parts = output.strip().split("|", 2)
        if len(parts) != 3 or parts[1] not in {"true", "false"}:
            raise RuntimeError("Could not inspect Hermes container deployment.")
        networks = [value for value in parts[2].split() if value]
        if len(networks) != 1:
            raise RuntimeError("Hermes container must have exactly one tenant network.")
        tenant_network = networks[0]
        if not self._SAFE_DOCKER_NAME.fullmatch(tenant_network):
            raise RuntimeError("Hermes tenant network is invalid.")
        return parts[0], parts[1] == "true", tenant_network

    def _run_hermes_container(self, instance, image, network, timeout=420):
        return self.run_command(
            [
                "docker", "run", "-d", "--name", self.get_runtime_target(instance),
                "--restart", "unless-stopped", "--network", network,
                "--shm-size", "1g", "-v", f"{self._hermes_data_path(instance)}:/opt/data",
                "-e", "HERMES_DASHBOARD=1", image, "gateway", "run",
            ],
            timeout=timeout,
        )

    def _wait_for_dashboard(self, instance):
        return self.run_command(
            [
                "docker", "exec", self.get_runtime_target(instance), "sh", "-lc",
                "i=0; while [ $i -lt 30 ]; do "
                "curl -fsS --max-time 2 http://127.0.0.1:9119/ >/dev/null && exit 0; "
                "i=$((i+1)); sleep 2; done; exit 1",
            ],
            timeout=70,
        )

    def _restore_hermes_runtime(self, instance, image, was_running, network):
        code, output = self.run_command(
            [
                "bash", "-lc", 'source "$1"; prepare_tenant_networks "$2"', "bash",
                str(self.manager_dir / "scripts" / "lib_tenant_network.sh"), network,
            ],
            timeout=60,
        )
        if code != 0:
            return code, output
        code, output = self._run_hermes_container(instance, image, network)
        if code != 0:
            return code, output
        code, output = self._wait_for_dashboard(instance)
        if code != 0:
            return code, output or "Hermes Dashboard did not become ready"
        proxy = os.environ.get("MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy")
        code, output = self.run_command(
            ["docker", "network", "connect", network, proxy], timeout=30
        )
        if code != 0 and "already exists" not in output.lower():
            return code, output
        code, output = self.configure_ingress(instance)
        if code != 0:
            return code, output
        if not was_running:
            return self.stop(instance)
        return 0, output

    @staticmethod
    def _write_private_file(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def start(self, instance):
        code, output = self.run_command(
            ["docker", "start", self.get_runtime_target(instance)], timeout=90
        )
        if code != 0:
            return code, output
        disabled = self.ingress_conf(instance, disabled=True)
        active = self.ingress_conf(instance)
        if not disabled.is_file():
            return 0, output
        active.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(disabled, active)
        reload_code, reload_output = self.reload_nginx()
        if reload_code == 0:
            return 0, "\n".join(part for part in (output, reload_output) if part)
        shutil.move(active, disabled)
        self.run_command(["docker", "stop", self.get_runtime_target(instance)], timeout=60)
        return reload_code, reload_output

    def stop(self, instance):
        active = self.ingress_conf(instance)
        disabled = self.ingress_conf(instance, disabled=True)
        if active.is_file():
            disabled.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(active, disabled)
            reload_code, reload_output = self.reload_nginx()
            if reload_code != 0:
                shutil.move(disabled, active)
                self.reload_nginx()
                return reload_code, reload_output
        code, output = self.run_command(
            ["docker", "stop", self.get_runtime_target(instance)], timeout=60
        )
        if code == 0:
            return 0, output
        if disabled.is_file():
            shutil.move(disabled, active)
            self.reload_nginx()
        return code, output

    def configure_ingress(self, instance):
        """Publish the registered Hermes dashboard through the shared Nginx."""
        runtime_target = self.get_runtime_target(instance)
        port = instance.get("_created_port", instance.get("port"))
        public_id = instance.get("public_id")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Hermes ingress port is required")
        if not isinstance(public_id, str) or not self._SAFE_DOCKER_NAME.fullmatch(public_id):
            raise ValueError("Hermes instance public_id is not safe for ingress")
        if not self._SAFE_DOCKER_NAME.fullmatch(runtime_target):
            raise ValueError("Hermes runtime_identifier is not a valid container name")

        inspected = subprocess.run(
            [
                "docker", "inspect", "--format",
                '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\\n" $name}}{{end}}',
                runtime_target,
            ],
            cwd=str(self.manager_dir),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if inspected.returncode != 0:
            return inspected.returncode, inspected.stderr.strip() or "Hermes container not found."
        networks = [
            line.strip() for line in inspected.stdout.splitlines()
            if line.strip()
            and line.strip() not in {"manager-net", "bridge", "host", "none"}
        ]
        if len(networks) != 1 or not self._SAFE_DOCKER_NAME.fullmatch(networks[0]):
            return 1, "Hermes container must have exactly one safe tenant network."
        target_network = networks[0]

        self.nginx_users_conf_dir.mkdir(parents=True, exist_ok=True)
        conf = self.ingress_conf(instance)
        compose_file = self.nginx_compose_dir / "docker-compose.yml"
        if not compose_file.is_file():
            return 1, f"Nginx compose file not found: {compose_file}"
        old_conf = conf.read_bytes() if conf.is_file() else None
        old_compose = compose_file.read_bytes()
        cert = os.environ.get("NGINX_SSL_CERT", "/etc/nginx/ssl/fullchain.pem")
        key = os.environ.get("NGINX_SSL_KEY", "/etc/nginx/ssl/privkey.pem")
        try:
            conf.write_text(
                f"upstream hermes_backend_{port} {{\n"
                f"    zone hermes_backend_{port} 64k;\n"
                "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
                "    resolver_timeout 5s;\n"
                f"    server {runtime_target}:9119 resolve;\n"
                "}\n\n"
                "server {\n"
                f"    listen {port} ssl;\n"
                "    server_name _;\n"
                f"    ssl_certificate {cert};\n"
                f"    ssl_certificate_key {key};\n\n"
                "    location / {\n"
                f"        proxy_pass http://hermes_backend_{port};\n"
                "        proxy_http_version 1.1;\n"
                "        proxy_set_header Upgrade $http_upgrade;\n"
                '        proxy_set_header Connection "upgrade";\n'
                "        proxy_set_header Host $host;\n"
                "        proxy_set_header X-Real-IP $remote_addr;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_set_header X-Forwarded-Proto $scheme;\n"
                "        proxy_read_timeout 86400;\n"
                "        proxy_send_timeout 86400;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            compose_file.write_text(
                self._add_ingress_to_nginx_compose(
                    old_compose.decode("utf-8"), port, target_network
                ),
                encoding="utf-8",
            )
            up_code, up_output = self.apply_nginx_compose(compose_file)
            if up_code != 0:
                raise RuntimeError(up_output)
            reload_code, reload_output = self.reload_nginx()
            if reload_code != 0:
                raise RuntimeError(reload_output)
            return 0, "\n".join(part for part in (up_output, reload_output) if part)
        except Exception as exc:
            compose_file.write_bytes(old_compose)
            if old_conf is None:
                conf.unlink(missing_ok=True)
            else:
                conf.write_bytes(old_conf)
            self.apply_nginx_compose(compose_file)
            rollback_code, rollback_output = self.reload_nginx()
            if rollback_code != 0:
                return 1, (
                    "Hermes ingress configuration failed; file rollback completed "
                    f"but Nginx recovery failed: {rollback_output}"
                )
            return 1, f"Hermes ingress configuration failed and was rolled back: {exc}"

    @staticmethod
    def _add_ingress_to_nginx_compose(text, port, network):
        mapping = f'      - "{port}:{port}"\n'
        service_network = f"      - {network}\n"
        network_definition = f"  {network}:\n    external: true\n"
        lines = text.splitlines(keepends=True)
        service_start = next(
            (index for index, line in enumerate(lines) if line == "  nginx:\n"), None
        )
        if service_start is None:
            raise ValueError("Nginx compose service not found")
        service_end = next(
            (
                index for index in range(service_start + 1, len(lines))
                if re.match(r"^  [^ ]+:[ ]*\n$", lines[index])
            ),
            len(lines),
        )
        port_pattern = re.compile(
            rf"^\s*-\s*['\"]?{port}:{port}['\"]?\s*$"
        )
        if not any(port_pattern.match(line) for line in lines[service_start:service_end]):
            ports_index = next(
                (
                    index for index in range(service_start + 1, service_end)
                    if lines[index] == "    ports:\n"
                ),
                None,
            )
            if ports_index is None:
                raise ValueError("Nginx compose ports section not found")
            lines.insert(ports_index + 1, mapping)
            service_end += 1
        if not any(
            line.strip().lstrip("-").strip() == network
            for line in lines[service_start:service_end]
        ):
            networks_index = next(
                (
                    index for index in range(service_start + 1, service_end)
                    if lines[index] == "    networks:\n"
                ),
                None,
            )
            if networks_index is None:
                raise ValueError("Nginx compose service networks section not found")
            lines.insert(networks_index + 1, service_network)
        if network_definition not in "".join(lines):
            for index, line in enumerate(lines):
                if line == "networks:\n":
                    lines.insert(index + 1, network_definition)
                    break
            else:
                raise ValueError("Nginx compose top-level networks section not found")
        return "".join(lines)

    def create(
        self, instance, basic_auth_enabled, basic_auth_password="",
        skip_nginx_reload=True, skip_metadata_write=False, timeout=420,
    ):
        del skip_nginx_reload, skip_metadata_write
        runtime_target = self.get_runtime_target(instance)
        data_path = self._hermes_data_path(instance)
        user_id = self.get_legacy_user_id(instance)
        if (
            basic_auth_enabled != "true"
            or not basic_auth_password
            or not self._SAFE_DOCKER_NAME.fullmatch(runtime_target)
            or data_path.parent != self.public_dir / "hermes"
        ):
            return 1, "Invalid Hermes creation parameters."
        if data_path.exists():
            return 1, f"Hermes data path already exists: {data_path}"

        network = self.tenant_network(instance)
        port_file = os.environ.get("PORT_FILE", str(self.public_dir / "ports.txt"))
        port_start = os.environ.get("PORT_START", "30021")
        port_end = os.environ.get("PORT_END", "39999")
        created_network = False
        try:
            data_path.mkdir(parents=True)
            (data_path / ".env").write_text(
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=" + user_id + "\n"
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH="
                + self._password_hash(basic_auth_password) + "\n"
                "HERMES_DASHBOARD_BASIC_AUTH_SECRET=" + secrets.token_urlsafe(48) + "\n",
                encoding="utf-8",
            )
            (data_path / ".env").chmod(0o600)
            (data_path / "config.yaml").write_text(
                "security:\n  allow_lazy_installs: false\n",
                encoding="utf-8",
            )
            network_existed = self.run_command(
                ["docker", "network", "inspect", network], timeout=10
            )[0] == 0
            code, output = self.run_command(
                [
                    "bash", "-lc",
                    'source "$1"; prepare_tenant_networks "$2"', "bash",
                    str(self.manager_dir / "scripts" / "lib_tenant_network.sh"), network,
                ],
                timeout=60,
            )
            if code != 0:
                raise RuntimeError(output)
            created_network = not network_existed
            code, output = self.run_command(
                [
                    "bash", "-lc",
                    'source "$1"; allocate_port "$2" "$3" "$4"', "bash",
                    str(self.manager_dir / "scripts" / "lib_port_allocator.sh"),
                    port_file, port_start, port_end,
                ],
                timeout=30,
            )
            allocated_ports = [line for line in output.splitlines() if line.isdigit()]
            if code != 0 or len(allocated_ports) != 1:
                raise RuntimeError(output or "Hermes ingress port allocation failed")
            instance["_created_port"] = int(allocated_ports[0])
            code, output = self._run_hermes_container(instance, self.IMAGE, network, timeout)
            if code != 0:
                raise RuntimeError(output)
            code, readiness = self._wait_for_dashboard(instance)
            if code != 0:
                raise RuntimeError(readiness or "Hermes Dashboard did not become ready")
            return 0, output
        except Exception as exc:
            self.run_command(["docker", "rm", "-f", runtime_target], timeout=60)
            shutil.rmtree(data_path, ignore_errors=True)
            if created_network:
                self.run_command(["docker", "network", "rm", network], timeout=30)
            instance.pop("_created_port", None)
            return 1, f"Hermes creation failed and was rolled back: {exc}"

    def delete(self, instance):
        runtime_target = self.get_runtime_target(instance)
        try:
            data_path = self._hermes_data_path(instance)
        except ValueError as exc:
            return 1, str(exc)
        recycle_dir = self.hermes_recycle_dir(instance)
        if data_path.parent != self.public_dir / "hermes" or not data_path.is_dir():
            return 1, f"Hermes data path not found or invalid: {data_path}"
        if recycle_dir.exists():
            return 1, f"Hermes recycle path already exists: {recycle_dir}"
        try:
            image, was_running, network = self._inspect_hermes_container(instance)
        except RuntimeError as exc:
            return 1, str(exc)
        port = instance.get("_created_port", instance.get("port"))
        if not isinstance(port, int) or not 1 <= port <= 65535:
            return 1, "Hermes ingress port is required for deletion."
        conf = self.ingress_conf(instance)
        disabled_conf = self.ingress_conf(instance, disabled=True)
        compose_file = self.nginx_compose_dir / "docker-compose.yml"
        if not compose_file.is_file():
            return 1, f"Nginx compose file not found: {compose_file}"
        networks = [network]
        old_compose = compose_file.read_bytes()
        old_conf = conf.read_bytes() if conf.is_file() else None
        old_disabled_conf = disabled_conf.read_bytes() if disabled_conf.is_file() else None
        conf.unlink(missing_ok=True)
        disabled_conf.unlink(missing_ok=True)
        compose_file.write_text(
            self._remove_ingress_from_nginx_compose(
                compose_file.read_text(encoding="utf-8"), port, networks
            ),
            encoding="utf-8",
        )
        apply_code, apply_output = self.apply_nginx_compose(compose_file)
        reload_code, reload_output = self.reload_nginx() if apply_code == 0 else (apply_code, apply_output)
        if reload_code != 0:
            compose_file.write_bytes(old_compose)
            if old_conf is not None:
                conf.write_bytes(old_conf)
            if old_disabled_conf is not None:
                disabled_conf.parent.mkdir(parents=True, exist_ok=True)
                disabled_conf.write_bytes(old_disabled_conf)
            self.apply_nginx_compose(compose_file)
            self.reload_nginx()
            return reload_code, f"Hermes ingress cleanup failed: {reload_output}"
        code, output = self.run_command(["docker", "rm", "-f", runtime_target], timeout=60)
        if code != 0:
            compose_file.write_bytes(old_compose)
            if old_conf is not None:
                conf.write_bytes(old_conf)
            if old_disabled_conf is not None:
                disabled_conf.parent.mkdir(parents=True, exist_ok=True)
                disabled_conf.write_bytes(old_disabled_conf)
            self.apply_nginx_compose(compose_file)
            self.reload_nginx()
            return code, output
        try:
            recycle_dir.mkdir(parents=True)
            shutil.move(str(data_path), str(recycle_dir / "data"))
            self._write_private_file(
                recycle_dir / "manifest.json",
                json.dumps({"image": image, "was_running": was_running, "network": network}).encode(),
            )
        except Exception as exc:
            if (recycle_dir / "data").is_dir() and not data_path.exists():
                data_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(recycle_dir / "data"), str(data_path))
            shutil.rmtree(recycle_dir, ignore_errors=True)
            rollback_code, rollback_output = self._restore_hermes_runtime(
                instance, image, was_running, network
            )
            if rollback_code != 0:
                return 1, (
                    "Hermes data retention failed and automatic rollback failed; "
                    f"manual recovery is required: {exc}; {rollback_output}"
                )
            return 1, f"Hermes data retention failed and was rolled back: {exc}"
        cleanup_errors = []
        for network in networks:
            if network.startswith("openclaw-user-"):
                for shared in (
                    self.nginx_container_name,
                    os.environ.get("MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy"),
                ):
                    cleanup_code, cleanup_output = self.run_command(
                        ["docker", "network", "disconnect", network, shared], timeout=30
                    )
                    if cleanup_code != 0 and "not connected" not in cleanup_output.lower():
                        cleanup_errors.append(cleanup_output)
                cleanup_code, cleanup_output = self.run_command(
                    ["docker", "network", "rm", network], timeout=30
                )
                if cleanup_code != 0 and "not found" not in cleanup_output.lower():
                    cleanup_errors.append(cleanup_output)
        if cleanup_errors:
            shutil.move(str(recycle_dir / "data"), str(data_path))
            shutil.rmtree(recycle_dir, ignore_errors=True)
            rollback_code, rollback_output = self._restore_hermes_runtime(
                instance, image, was_running, network
            )
            detail = "; ".join(error for error in cleanup_errors if error)
            if rollback_code != 0:
                return 1, (
                    "Hermes network cleanup failed and automatic rollback failed; "
                    f"manual recovery is required: {detail}; {rollback_output}"
                )
            return 1, f"Hermes network cleanup failed and was rolled back: {detail}"
        return 0, "Hermes resources moved to recycle bin."

    @staticmethod
    def _remove_ingress_from_nginx_compose(text, port, networks):
        tenant_networks = {network for network in networks if network.startswith("openclaw-user-")}
        lines = text.splitlines(keepends=True)
        lines = [
            line for line in lines
            if not re.match(rf"^\s*-\s*['\"]?{port}:{port}['\"]?\s*$", line)
            and line.strip().lstrip("-").strip() not in tenant_networks
        ]
        for network in tenant_networks:
            definition = f"  {network}:\n    external: true\n"
            text = "".join(lines).replace(definition, "")
            lines = text.splitlines(keepends=True)
        return "".join(lines)

    def restore(self, instance):
        try:
            data_path = self._hermes_data_path(instance)
        except ValueError as exc:
            return 1, str(exc)
        recycle_dir = self.hermes_recycle_dir(instance)
        recycled_data = recycle_dir / "data"
        manifest_file = recycle_dir / "manifest.json"
        if data_path.exists():
            return 1, f"Hermes data path already exists: {data_path}"
        if not recycled_data.is_dir() or not manifest_file.is_file():
            return 1, f"Hermes recycle data is incomplete: {recycle_dir}"
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            image = manifest["image"]
            network = manifest["network"]
            was_running = manifest["was_running"]
            if (
                not isinstance(image, str) or not image.startswith("nousresearch/hermes-agent:")
                or not isinstance(network, str) or not self._SAFE_DOCKER_NAME.fullmatch(network)
                or not isinstance(was_running, bool)
            ):
                raise ValueError("invalid manifest")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return 1, f"Hermes recycle manifest is invalid: {exc}"
        code, output = self.run_command(["docker", "image", "inspect", image], timeout=30)
        if code != 0:
            return 1, f"Hermes restore image is not available locally: {image}"
        moved = False
        compose_file = self.nginx_compose_dir / "docker-compose.yml"
        if not compose_file.is_file():
            return 1, f"Nginx compose file not found: {compose_file}"
        old_compose = compose_file.read_bytes()
        active_conf = self.ingress_conf(instance)
        disabled_conf = self.ingress_conf(instance, disabled=True)
        old_active_conf = active_conf.read_bytes() if active_conf.is_file() else None
        old_disabled_conf = disabled_conf.read_bytes() if disabled_conf.is_file() else None
        network_existed = self.run_command(
            ["docker", "network", "inspect", network], timeout=10
        )[0] == 0
        proxy = os.environ.get("MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy")
        proxy_code, proxy_networks = self.run_command(
            [
                "docker", "inspect", "--format",
                '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s " $name}}{{end}}',
                proxy,
            ],
            timeout=10,
        )
        proxy_was_connected = proxy_code == 0 and network in proxy_networks.split()
        try:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(recycled_data), str(data_path))
            moved = True
            code, output = self._restore_hermes_runtime(
                instance, image, was_running, network
            )
            if code != 0:
                raise RuntimeError(output)
            shutil.rmtree(recycle_dir)
            return 0, "Hermes instance restored."
        except Exception as exc:
            rollback_errors = []
            cleanup_code, cleanup_output = self.run_command(
                ["docker", "rm", "-f", self.get_runtime_target(instance)], timeout=60
            )
            if cleanup_code != 0 and "no such container" not in cleanup_output.lower():
                rollback_errors.append(cleanup_output)
            compose_file.write_bytes(old_compose)
            active_conf.unlink(missing_ok=True)
            disabled_conf.unlink(missing_ok=True)
            if old_active_conf is not None:
                active_conf.write_bytes(old_active_conf)
            if old_disabled_conf is not None:
                disabled_conf.parent.mkdir(parents=True, exist_ok=True)
                disabled_conf.write_bytes(old_disabled_conf)
            ingress_code, ingress_output = self.apply_nginx_compose(compose_file)
            if ingress_code == 0:
                ingress_code, ingress_output = self.reload_nginx()
            if not proxy_was_connected:
                cleanup_code, cleanup_output = self.run_command(
                    ["docker", "network", "disconnect", network, proxy], timeout=30
                )
                if cleanup_code != 0 and "not connected" not in cleanup_output.lower():
                    rollback_errors.append(cleanup_output)
            if not network_existed:
                cleanup_code, cleanup_output = self.run_command(
                    ["docker", "network", "rm", network], timeout=30
                )
                if cleanup_code != 0 and "not found" not in cleanup_output.lower():
                    rollback_errors.append(cleanup_output)
            if moved and data_path.is_dir() and not recycled_data.exists():
                try:
                    recycle_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(data_path), str(recycled_data))
                except OSError as move_exc:
                    rollback_errors.append(str(move_exc))
            if ingress_code != 0:
                rollback_errors.append(ingress_output)
            if rollback_errors:
                return 1, (
                    "Hermes restore failed and automatic rollback failed; manual recovery "
                    f"is required: {exc}; {'; '.join(error for error in rollback_errors if error)}"
                )
            return 1, f"Hermes restore failed and was rolled back: {exc}"

    def update_version(self, instance, version, restore_model_provider=False, timeout=600):
        del restore_model_provider
        if not isinstance(version, str) or not self._SAFE_VERSION.fullmatch(version):
            return 1, "Invalid Hermes version."
        try:
            self._hermes_data_path(instance)
        except ValueError as exc:
            return 1, str(exc)
        target_image = f"nousresearch/hermes-agent:{version}"
        try:
            current_image, was_running, network = self._inspect_hermes_container(instance)
        except RuntimeError as exc:
            return 1, str(exc)
        if current_image == target_image:
            return 0, f"Hermes instance already uses target image: {target_image}"
        code, _ = self.run_command(["docker", "image", "inspect", target_image], timeout=30)
        if code != 0:
            return 1, (
                f"Target image is not available locally: {target_image}. "
                f"Run: docker pull '{target_image}'"
            )
        code, output = self.run_command(
            ["docker", "rm", "-f", self.get_runtime_target(instance)], timeout=60
        )
        if code != 0:
            return code, output
        try:
            code, output = self._run_hermes_container(instance, target_image, network, timeout)
            if code != 0:
                raise RuntimeError(output)
            code, output = self._wait_for_dashboard(instance)
            if code != 0:
                raise RuntimeError(output or "Hermes Dashboard did not become ready")
            if not was_running:
                code, output = self.stop(instance)
                if code != 0:
                    raise RuntimeError(output)
            return 0, f"Hermes version updated to {version}."
        except Exception as exc:
            self.run_command(["docker", "rm", "-f", self.get_runtime_target(instance)], timeout=60)
            rollback_code, rollback_output = self._run_hermes_container(
                instance, current_image, network, timeout
            )
            if rollback_code == 0:
                rollback_code, rollback_output = self._wait_for_dashboard(instance)
            if rollback_code == 0 and not was_running:
                rollback_code, rollback_output = self.stop(instance)
            if rollback_code != 0:
                return 1, (
                    f"Hermes version update failed and automatic rollback failed: "
                    f"{exc}; {rollback_output}"
                )
            return 1, f"Hermes version update failed and was rolled back: {exc}"

    def set_model_provider(
        self, instance, provider_id, model_id, base_url="", alias="", timeout=180,
    ):
        del base_url, alias
        runtime_target = self.get_runtime_target(instance)
        user_id = self.get_legacy_user_id(instance)
        data_path = Path(instance["data_path"])
        config_file = data_path / "config.yaml"
        if not config_file.is_file():
            return 1, f"Hermes config file not found: {config_file}"

        model_short_id = model_id.removeprefix(provider_id + "/")
        proxy_base_url = os.environ.get(
            "MODEL_PROXY_PUBLIC_BASE_URL",
            "http://openclaw-model-proxy:8081/v1",
        )
        proxy_container = os.environ.get(
            "MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy"
        )
        token_dir = Path(os.environ.get(
            "MODEL_PROXY_TOKEN_DIR",
            str(self.public_dir / "model-proxy-tokens"),
        ))
        token_file = token_dir / f"{user_id}.token"
        models_file = token_dir / f"{user_id}.models"
        old_config = config_file.read_bytes()
        old_token = token_file.read_bytes() if token_file.is_file() else None
        old_models = models_file.read_bytes() if models_file.is_file() else None
        network = self.tenant_network(instance)
        connected = False
        token = ""

        try:
            existing_token = old_token.decode("utf-8").strip() if old_token else ""
            token = existing_token or "ocm_" + secrets.token_urlsafe(32)
            old_allowed_models = {
                line.strip()
                for line in (old_models or b"").decode("utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
            staged_models = old_allowed_models | {model_short_id}
            self._write_private_file(
                models_file,
                ("\n".join(sorted(staged_models)) + "\n").encode(),
            )
            if not existing_token:
                self._write_private_file(token_file, (token + "\n").encode())

            inspect_code, inspect_output = self.run_command(
                [
                    "docker", "inspect", "--format",
                    '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\\n" $name}}{{end}}',
                    proxy_container,
                ],
                timeout=10,
            )
            if inspect_code != 0:
                raise RuntimeError(inspect_output or "Model Proxy container not found")
            if network not in inspect_output.splitlines():
                code, output = self.run_command(
                    ["docker", "network", "connect", network, proxy_container],
                    timeout=30,
                )
                if code != 0:
                    raise RuntimeError(output or "Could not connect Model Proxy network")
                connected = True

            for key, value in (
                ("model.default", model_short_id),
                ("model.provider", "custom"),
                ("model.base_url", proxy_base_url),
                ("model.api_key", token),
                ("model.api_mode", "chat_completions"),
            ):
                code, output = self.run_command(
                    [
                        "docker", "exec", runtime_target,
                        "hermes", "config", "set", key, value,
                    ],
                    timeout=timeout,
                )
                if code != 0:
                    raise RuntimeError(output or f"Could not set {key}")
            self._write_private_file(models_file, (model_short_id + "\n").encode())
            return 0, "Hermes model provider updated."
        except Exception as exc:
            rollback_errors = []
            try:
                config_file.write_bytes(old_config)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            for path, content in ((token_file, old_token), (models_file, old_models)):
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._write_private_file(path, content)
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if connected:
                try:
                    rollback_code, rollback_output = self.run_command(
                        ["docker", "network", "disconnect", network, proxy_container],
                        timeout=30,
                    )
                    if rollback_code != 0:
                        rollback_errors.append(
                            rollback_output or "Could not disconnect Model Proxy network"
                        )
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            detail = str(exc).replace(token, "[REDACTED]")
            if rollback_errors:
                recovery = "; ".join(rollback_errors).replace(token, "[REDACTED]")
                return 1, (
                    "Hermes model provider update failed; automatic rollback was "
                    f"incomplete and manual recovery is required: {detail}; {recovery}"
                )
            return 1, f"Hermes model provider update failed and was rolled back: {detail}"
