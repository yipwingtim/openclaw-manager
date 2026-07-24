import shutil
import subprocess
from pathlib import Path


class OpenClawDockerAdapter:
    CAPABILITIES = frozenset(
        {
            "status", "logs", "start", "stop", "restart", "create",
            "batch_create", "delete", "restore", "update_version",
            "batch_set_model_provider", "basic_auth", "dashboard", "access",
            "device_pairing", "file_upload", "file_download", "file_delete",
        }
    )

    def supports(self, action):
        return action in self.CAPABILITIES

    def __init__(self, manager_dir, public_dir, nginx_users_conf_dir, nginx_compose_dir, nginx_container_name):
        self.manager_dir = Path(manager_dir)
        self.public_dir = Path(public_dir)
        self.nginx_users_conf_dir = Path(nginx_users_conf_dir)
        self.nginx_compose_dir = Path(nginx_compose_dir)
        self.nginx_container_name = nginx_container_name

    def user_dir(self, user_id):
        return self.public_dir / "users" / user_id

    def container_name(self, user_id):
        return f"openclaw_{user_id}"

    def run_command(self, command, timeout=30, cwd=None):
        result = subprocess.run(
            command,
            cwd=str(cwd or self.manager_dir),
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

    def status(self, user_id):
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{self.container_name(user_id)}$", "--format", "{{.Status}}"],
            cwd=str(self.manager_dir),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() or "STOPPED"

    def logs(self, user_id, tail=120):
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), self.container_name(user_id)],
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

    def start(self, user_id):
        start_code, start_output = self.run_command(["docker", "start", self.container_name(user_id)], timeout=90)
        if start_code != 0:
            return start_code, start_output

        nginx_code, nginx_output = self.enable_nginx_user_conf(user_id)
        combined_output = "\n".join(part for part in [start_output, nginx_output] if part)
        if nginx_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = self.run_command(["docker", "stop", self.container_name(user_id)], timeout=60)
        rollback_note = "\nRolled back container start."
        if rollback_code != 0:
            rollback_note += f"\nRollback stop failed:\n{rollback_output}"
        return nginx_code, f"{combined_output}{rollback_note}"

    def stop(self, user_id):
        nginx_code, nginx_output = self.disable_nginx_user_conf(user_id)
        if nginx_code != 0:
            return nginx_code, nginx_output

        stop_code, stop_output = self.run_command(["docker", "stop", self.container_name(user_id)], timeout=60)
        combined_output = "\n".join(part for part in [nginx_output, stop_output] if part)
        if stop_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = self.enable_nginx_user_conf(user_id)
        rollback_note = "\nRolled back nginx config disable."
        if rollback_code != 0:
            rollback_note += f"\nRollback enable failed:\n{rollback_output}"
        return stop_code, f"{combined_output}{rollback_note}"

    def restart(self, user_id):
        return self.run_command(["docker", "restart", self.container_name(user_id)], timeout=90)

    def create(self, user_id, basic_auth_enabled, basic_auth_password="", skip_nginx_reload=True, timeout=420):
        command = [
            str(self.manager_dir / "scripts" / "create_user.sh"),
            user_id,
            "--basic-auth-enabled",
            basic_auth_enabled,
        ]
        if skip_nginx_reload:
            command.append("--skip-nginx-reload")
        if basic_auth_password:
            command.extend(["--password", basic_auth_password])
        return self.run_command(command, timeout=timeout)

    def batch_create(self, input_csv, output_csv, timeout, skip_nginx_refresh=False):
        command = [str(self.manager_dir / "scripts" / "batch_create_users.sh"), str(input_csv), str(output_csv)]
        if skip_nginx_refresh:
            command.append("--skip-nginx-refresh")
        return self.run_command(
            command,
            timeout=timeout,
        )

    def delete(self, user_id):
        return self.run_command([str(self.manager_dir / "scripts" / "delete_user.sh"), user_id], timeout=180)

    def restore(self, user_id):
        return self.run_command([str(self.manager_dir / "scripts" / "restore_user.sh"), user_id], timeout=240)

    def update_version(self, user_id, version, restore_model_provider=False, timeout=600):
        command = [str(self.manager_dir / "scripts" / "update_instance_version.sh"), user_id, version]
        if restore_model_provider:
            command.append("--restore-model-provider")
        return self.run_command(command, timeout=timeout)

    def batch_set_model_provider(self, input_csv, output_csv, timeout):
        return self.run_command(
            [str(self.manager_dir / "scripts" / "batch_set_model_provider.sh"), str(input_csv), str(output_csv)],

            timeout=timeout,
        )

class EvoScientistDockerAdapter(OpenClawDockerAdapter):
    CAPABILITIES = frozenset(
        {"access", "status", "logs", "start", "stop", "restart"}
    )

    def supports(self, action):
        return action in self.CAPABILITIES

    def container_name(self, user_id):
        return f"evoscientist_{user_id}"

    def proxy_container_name(self, user_id):
        return f"{self.container_name(user_id)}-proxy"

    def container_names(self, user_id):
        return [self.container_name(user_id), self.proxy_container_name(user_id)]

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

    def status(self, user_id):
        statuses = {
            name: self._container_status(name)
            for name in self.container_names(user_id)
        }
        if all(value.startswith("Up") for value in statuses.values()):
            return "Up (" + "; ".join(f"{name}={value}" for name, value in statuses.items()) + ")"
        if all(value == "STOPPED" for value in statuses.values()):
            return "STOPPED"
        return "DEGRADED (" + "; ".join(f"{name}={value}" for name, value in statuses.items()) + ")"

    def logs(self, user_id, tail=120):
        outputs = []
        failed = False
        for container_name in self.container_names(user_id):
            code, output = self.run_command(
                ["docker", "logs", "--tail", str(tail), container_name],
                timeout=10,
            )
            failed = failed or code != 0
            outputs.append(f"===== {container_name} =====\n{output or 'No recent logs.'}")
        combined = "\n".join(outputs)
        return combined if not failed else f"{combined}\n[WARN] One or more container logs could not be read."

    def start(self, user_id):
        started = []
        for container_name in self.container_names(user_id):
            code, output = self.run_command(["docker", "start", container_name], timeout=90)
            if code != 0:
                for started_name in reversed(started):
                    self.run_command(["docker", "stop", started_name], timeout=60)
                return code, output
            started.append(container_name)

        nginx_code, nginx_output = self.enable_nginx_user_conf(user_id)
        if nginx_code == 0:
            return 0, nginx_output

        for container_name in reversed(started):
            self.run_command(["docker", "stop", container_name], timeout=60)
        return nginx_code, nginx_output

    def stop(self, user_id):
        nginx_code, nginx_output = self.disable_nginx_user_conf(user_id)
        if nginx_code != 0:
            return nginx_code, nginx_output

        outputs = [nginx_output]
        stopped = []
        for container_name in reversed(self.container_names(user_id)):
            code, output = self.run_command(["docker", "stop", container_name], timeout=60)
            outputs.append(output)
            if code != 0:
                for stopped_name in reversed(stopped):
                    self.run_command(["docker", "start", stopped_name], timeout=90)
                self.enable_nginx_user_conf(user_id)
                return code, "\n".join(part for part in outputs if part)
            stopped.append(container_name)
        return 0, "\n".join(part for part in outputs if part)

    def restart(self, user_id):
        outputs = []
        for container_name in self.container_names(user_id):
            code, output = self.run_command(["docker", "restart", container_name], timeout=90)
            outputs.append(output)
            if code != 0:
                return code, "\n".join(part for part in outputs if part)
        return 0, "\n".join(part for part in outputs if part)

    def create(self, *args, **kwargs):
        return 1, "EvoScientist create is not supported yet."

    def delete(self, user_id):
        return 1, "EvoScientist delete is not supported yet."

    def restore(self, user_id):
        return 1, "EvoScientist restore is not supported yet."

    def update_version(self, *args, **kwargs):
        return 1, "EvoScientist version update is not supported yet."
