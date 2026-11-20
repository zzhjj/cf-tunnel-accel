"""Installer contract tests: isolated HOME/PATH, no real network/packages/systemd."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
UUID = "12345678-1234-4234-8234-123456789abc"


def snapshot(root):
    """Include bytes, modes, symlink targets and directories, not access times."""
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if path.is_symlink():
            value = ("link", os.readlink(path))
        elif path.is_dir():
            value = ("dir",)
        else:
            value = ("file", path.read_bytes())
        result[str(path.relative_to(root))] = (stat.S_IMODE(mode), value)
    return result


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installer-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.commands = self.base / "commands.log"
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.tools),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CF_TUNNEL_ACCEL_CLOUDFLARED": str(self.base / "missing-cloudflared"),
            "CFA_SYSTEMD_DIR": str(self.base / "systemd"),
            "CFA_SYSTEMD_RUNTIME_DIR": str(self.base / "systemd-not-running"),
        }
        for command in ("bash", "python3", "dirname", "date", "mktemp", "mkdir", "rmdir",
                        "rm", "cp", "mv", "install", "chmod", "ln", "find", "sed", "basename"):
            real = sys.executable if command == "python3" else shutil.which(command)
            if not real:
                self.skipTest("Missing test tool: " + command)
            (self.tools / command).symlink_to(real)
        self.tool("id", '#!/bin/sh\nprintf "0\\n"\n')
        self.tool("uname", '#!/bin/sh\ncase "$1" in -s) echo Linux;; *) echo x86_64;; esac\n')
        # Deliberately blocked fixtures, so test regressions cannot touch the host.
        for command in ("curl", "wget", "apt-get", "dnf", "yum", "apk", "pacman", "systemctl", "dig"):
            self.tool(command, '#!/bin/sh\nprintf "%s\\n" "' + command + ' $*" >> "' + str(self.commands) + '"\nexit 97\n')

    def tool(self, name, content):
        target = self.tools / name
        if target.is_symlink():
            target.unlink()
        target.write_text(content, encoding="utf-8")
        target.chmod(0o755)
        return target

    def run_install(self, *args, env=None):
        merged = self.env.copy()
        # A broken root-mode preflight must still never write host prefixes.
        if '--user' not in args and '--dry-run' not in args:
            merged.setdefault('CFA_BIN_DIR', str(self.base / 'system-package'))
            merged.setdefault('CFA_LINK_DIR', str(self.base / 'system-links'))
        merged.update(env or {})
        return subprocess.run([str(self.tools / "bash"), str(INSTALLER), *args],
                              cwd=self.base, env=merged, capture_output=True, text=True, timeout=25)

    def assert_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def assert_no_commands(self):
        self.assertFalse(self.commands.exists(), self.commands.read_text() if self.commands.exists() else "")

    @property
    def package(self):
        return self.home / ".local/share/cf-tunnel-accel"

    @property
    def config(self):
        return self.home / ".config/cf-tunnel-accel"

    @property
    def links(self):
        return self.home / ".local/bin"

    def fixture_package(self, fail_version=False):
        source = self.base / "source"
        source.mkdir()
        (source / "cf-tunnel-accel").write_text(
            "import sys\nSCRIPT_VERSION='fixture'\n"
            + ("raise SystemExit(23)\n" if fail_version else
               "if __name__ == '__main__': print('cf-tunnel-accel fixture')\n"))
        shutil.copytree(ROOT / "ui", source / "ui", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        return source

    def seed_old(self):
        self.package.mkdir(parents=True)
        (self.package / "cf-tunnel-accel").write_text("old-cli\n")
        (self.package / "ui").mkdir()
        (self.package / "ui/server.py").write_text("old-ui\n")
        self.config.mkdir(parents=True)
        (self.config / "env").write_text("# local comment\nCF_TUNNEL_ACCEL_AUX_ZONE=old.example\n")
        (self.config / "env").chmod(0o640)
        self.links.mkdir(parents=True)
        (self.links / "cf-tunnel-accel").symlink_to("old-cli-target")
        (self.links / "cf-tunnel-ui").write_text("old-ui-link\n")

    def select_service(self):
        self.env["CFA_SYSTEMD_RUNTIME_DIR"] = str(self.base / "runtime")
        (self.base / "runtime").mkdir()
        self.env["CF_TUNNEL_ACCEL_CLOUDFLARED"] = str(self.tools / "cloudflared")
        self.tool("cloudflared", '#!/bin/sh\nif [ "$1" = --version ]; then echo "cloudflared version 2026.1.0"; exit 0; fi\nprintf "cloudflared $*\\n" >> "' + str(self.commands) + '"\nexit "${VALIDATION_EXIT:-0}"\n')
        config = self.base / "selected.yml"
        config.write_text("tunnel: " + UUID + "\ningress:\n  - service: http_status:404\n")
        return config

    def test_dry_run_zero_mutations_even_with_deps_and_services(self):
        config = self.select_service()
        before = snapshot(self.base)
        proc = self.run_install("--dry-run", "--service", "--config", str(config),
                                "--service-name", "selected.service", "--ui-service")
        self.assert_ok(proc)
        self.assertIn("[dry-run]", proc.stdout)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_dry_run_env_flag_and_custom_paths_zero_mutations(self):
        before = snapshot(self.base)
        proc = self.run_install("--user", "--no-deps", env={"CFA_DRY_RUN": "1",
                                "CFA_BIN_DIR": str(self.base / "pkg"), "CFA_CONFIG_DIR": str(self.base / "conf")})
        self.assert_ok(proc)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_user_install_real_cli_ui_version_env_and_import(self):
        self.config.mkdir(parents=True)
        fixture_token = "installer-inline-fixture-token"
        (self.config / "env").write_text("CF_TUNNEL_ACCEL_API_TOKEN=" + fixture_token + "\n")
        proc = self.run_install("--user", "--no-deps")
        self.assert_ok(proc)
        self.assertIn("cloudflared is missing", proc.stderr)
        self.assertTrue((self.links / "cf-tunnel-accel").is_symlink())
        self.assertTrue((self.links / "cf-tunnel-ui").is_symlink())
        for name in ("server.py", "profiles.py", "static/index.html", "static/app.js", "static/style.css"):
            self.assertTrue((self.package / "ui" / name).is_file(), name)
        self.assertTrue((self.package / 'LICENSE').is_file())
        self.assertEqual(stat.S_IMODE((self.config / "env").stat().st_mode), 0o600)
        persisted_env = (self.config / "env").read_text()
        self.assertIn("CF_TUNNEL_ACCEL_TOKEN_FILE=" + str(self.config / "token"), persisted_env)
        for launcher in (self.package / "launchers/cf-tunnel-accel", self.package / "cf-tunnel-ui"):
            self.assertNotIn("CF_TUNNEL_ACCEL_TOKEN_FILE", launcher.read_text())
        for command in ("--version", "env"):
            result = subprocess.run([str(self.links / "cf-tunnel-accel"), command], cwd=self.base,
                                    env=self.env, capture_output=True, text=True, timeout=15)
            self.assert_ok(result)
            if command == "env":
                self.assertIn(str(self.config / "env"), result.stdout)
                self.assertIn("环境变量 ✓", result.stdout)
                self.assertNotIn(fixture_token, result.stdout + result.stderr)
        result = subprocess.run([str(self.links / "cf-tunnel-ui"), "--help"], cwd=self.base,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assert_ok(result)
        self.assertIn("--module", result.stdout)
        # Use the real installed UI loader, but do not start a server/create credentials.
        probe = r'''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('installed_ui', sys.argv[1])
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
accel, profiles = ui.load_console_profiles(sys.argv[2], None, None)
assert accel.SCRIPT_VERSION
assert Path(accel.__file__).resolve() == Path(sys.argv[2]).resolve()
assert Path(accel.ENV_FILE) == Path(sys.argv[3])
assert accel._SETTINGS['token_source'] == 'configuration'
assert accel._SETTINGS['token'] == os.environ['EXPECTED_FIXTURE_TOKEN']
'''
        env = self.env | {"CF_TUNNEL_ACCEL_ENV_FILE": str(self.config / "env"),
                          "CF_TUNNEL_ACCEL_UI_DIR": str(self.config),
                          "EXPECTED_FIXTURE_TOKEN": fixture_token}
        result = subprocess.run([sys.executable, "-B", "-c", probe, str(self.package / "ui/server.py"),
                                 str(self.package / "cf-tunnel-accel"), str(self.config / "env")],
                                cwd=self.base, env=env, capture_output=True, text=True, timeout=15)
        self.assert_ok(result)
        self.assertFalse((self.config / "ui-token").exists())
        self.assert_no_commands()


    def test_existing_cloudflared_symlink_is_accepted_without_replacement(self):
        target = self.base / 'cloudflared-real'
        target.write_text('#!/bin/sh\necho "cloudflared version 2026.1.0"\n')
        target.chmod(0o755)
        link = self.base / 'cloudflared'
        link.symlink_to(target)
        proc = self.run_install('--user', '--no-deps', env={'CF_TUNNEL_ACCEL_CLOUDFLARED': str(link)})
        self.assert_ok(proc)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(target))
        self.assertTrue((self.package / 'cf-tunnel-accel').is_file())
        self.assert_no_commands()
    def test_xdg_and_user_nonroot(self):
        self.tool("id", '#!/bin/sh\necho 1000\n')
        proc = self.run_install("--user", "--no-deps", env={"XDG_DATA_HOME": str(self.base / "xdg-data"),
                                "XDG_CONFIG_HOME": str(self.base / "xdg-config")})
        self.assert_ok(proc)
        self.assertTrue((self.base / "xdg-data/cf-tunnel-accel/ui/server.py").exists())
        self.assertTrue((self.base / "xdg-config/cf-tunnel-accel/env").exists())
        self.assert_no_commands()

    def test_default_root_destinations_plan(self):
        proc = self.run_install("--no-deps", "--dry-run")
        self.assert_ok(proc)
        self.assertIn("-> /usr/local/lib/cf-tunnel-accel", proc.stdout)
        self.assertIn("Launchers: /usr/local/bin", proc.stdout)
        self.assert_no_commands()

    def test_nonroot_system_install_fails_before_writes(self):
        self.tool("id", '#!/bin/sh\necho 1000\n')
        before = snapshot(self.base)
        proc = self.run_install("--no-deps")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("use --user", proc.stderr)
        self.assertEqual(snapshot(self.base), before)

    def test_malformed_arguments_fail_before_writes(self):
        cases = [("--source-dir",), ("--config", "--no-deps"), ("--token=",),
                 ("--tunnel-id", "bad"), ("--unknown",), ("one", "two"),
                 ("--service-name", "../bad.service"), ("--source-dir", "")]
        for args in cases:
            with self.subTest(args=args):
                before = snapshot(self.base)
                proc = self.run_install("--user", "--no-deps", *args)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_missing_or_incomplete_source_preserves_existing_install(self):
        self.seed_old()
        incomplete = self.base / "incomplete"
        incomplete.mkdir()
        (incomplete / "cf-tunnel-accel").write_text("import sys\n")
        for source in (self.base / "absent", incomplete):
            before = snapshot(self.base)
            proc = self.run_install("--user", "--no-deps", "--source-dir", str(source))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Incomplete source", proc.stderr)
            self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_source_dir_installs_and_validates_root_cfa_helpers(self):
        source = self.fixture_package()
        helper = source / "cfa_ingress.py"
        helper.write_text("VALUE = 7\n")
        cli = source / "cf-tunnel-accel"
        cli.write_text("import cfa_ingress\nimport sys\nSCRIPT_VERSION='fixture'\n"
                       "if __name__ == '__main__': print(cfa_ingress.VALUE)\n")
        proc = self.run_install("--user", "--no-deps", "--source-dir", str(source))
        self.assert_ok(proc)
        self.assertEqual((self.package / "cfa_ingress.py").read_text(), "VALUE = 7\n")
        result = subprocess.run([str(self.links / "cf-tunnel-accel")], env=self.env, cwd=self.base,
                                capture_output=True, text=True, timeout=10)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "7")

    def test_source_dir_from_other_cwd_and_custom_space_paths(self):
        source = self.fixture_package()
        config = self.base / "my config.yml"
        config.write_text("tunnel: " + UUID + "\n")
        env = {"CFA_BIN_DIR": str(self.base / "package with spaces"),
               "CFA_LINK_DIR": str(self.base / "links $special"),
               "CFA_CONFIG_DIR": str(self.base / "custom config"),
               "CF_TUNNEL_ACCEL_ENV_FILE": str(self.base / "different env/settings"),
               "CF_TUNNEL_ACCEL_TOKEN_FILE": str(self.base / "separate token/token"),
               "CF_TUNNEL_ACCEL_ACCOUNT_ID": "example-account"}
        proc = self.run_install("--user", "--no-deps", "--source-dir", str(source),
                                "--config", str(config), "--service-name", "chosen.service", env=env)
        self.assert_ok(proc)
        text = Path(env["CF_TUNNEL_ACCEL_ENV_FILE"]).read_text()
        self.assertIn("CF_TUNNEL_ACCEL_CONFIG=" + str(config), text)
        self.assertIn("CF_TUNNEL_ACCEL_SERVICE=chosen.service", text)
        self.assertIn("CF_TUNNEL_ACCEL_ACCOUNT_ID=example-account", text)
        # Launcher fallback paths work without carrying installer overrides.
        launcher = Path(env["CFA_LINK_DIR"]) / "cf-tunnel-accel"
        result = subprocess.run([str(launcher), "--version"], env=self.env, cwd=self.base,
                                capture_output=True, text=True, timeout=10)
        self.assert_ok(result)
        ui_launcher = Path(env["CFA_BIN_DIR"]) / "cf-tunnel-ui"
        self.assertIn('--module "$CF_TUNNEL_ACCEL_SCRIPT"', ui_launcher.read_text())
        self.assert_no_commands()

    def test_no_implicit_config_credentials_unit_selection_or_mutation(self):
        tunnel_dir = self.home / ".cloudflared"
        tunnel_dir.mkdir()
        for name in ("config-old.yml", "config-new.yml", "one.json", "two.json"):
            (tunnel_dir / name).write_text("do not touch\n")
        unit_dir = self.base / "systemd"
        unit_dir.mkdir()
        (unit_dir / "cloudflared-first.service").write_text("existing unit\n")
        old = snapshot(tunnel_dir), snapshot(unit_dir)
        proc = self.run_install("--user", "--no-deps", env={"CFA_CFD_DIR": str(tunnel_dir)})
        self.assert_ok(proc)
        self.assertEqual((snapshot(tunnel_dir), snapshot(unit_dir)), old)
        text = (self.config / "env").read_text()
        for key in ("CF_TUNNEL_ACCEL_CONFIG=", "CF_TUNNEL_ACCEL_TUNNEL_ID=", "CF_TUNNEL_ACCEL_SERVICE="):
            self.assertNotIn(key, text)
        self.assert_no_commands()

    def test_ambiguous_new_tunnel_requires_explicit_inputs(self):
        cases = [
            ([], "--service requires explicit --config"),
            (["--config", str(self.base / "new.yml"), "--service-name", "selected.service"],
             "New config requires --tunnel-id"),
            (["--tunnel-name", "chosen"], "--service requires explicit --config"),
        ]
        for extras, expected in cases:
            with self.subTest(extras=extras):
                before = snapshot(self.base)
                proc = self.run_install("--no-deps", "--service", *extras)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(expected, proc.stderr)
                self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_python_missing_and_old_version_no_writes(self):
        (self.tools / "python3").unlink()
        before = snapshot(self.base)
        proc = self.run_install("--user", "--no-deps")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Python 3.9+", proc.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.tool("python3", '#!/bin/sh\nexit 1\n')
        before = snapshot(self.base)
        proc = self.run_install("--user", "--no-deps")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Python 3.9+", proc.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_no_deps_supports_unsupported_architecture(self):
        self.tool("uname", '#!/bin/sh\necho unsupported-arch\n')
        self.assert_ok(self.run_install("--user", "--no-deps"))
        self.assert_no_commands()

    def test_failed_staged_version_preserves_old_install_and_modes(self):
        self.seed_old()
        source = self.fixture_package(fail_version=True)
        before = snapshot(self.base)
        proc = self.run_install("--user", "--no-deps", "--source-dir", str(source))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Staged CLI failed", proc.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_successful_update_backups_and_preserves_unrelated_local_files(self):
        self.seed_old()
        (self.package / "local-note").write_text("keep me\n")
        oldenv = (self.config / "env").read_text()
        proc = self.run_install("--user", "--no-deps")
        self.assert_ok(proc)
        backups = list(self.package.parent.glob("cf-tunnel-accel.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "cf-tunnel-accel").read_text(), "old-cli\n")
        self.assertEqual((self.package / "local-note").read_text(), "keep me\n")
        self.assertEqual(list(self.config.glob("env.bak.*"))[0].read_text(), oldenv)
        self.assertIn("# local comment", (self.config / "env").read_text())
        self.assert_no_commands()

    def test_ui_unit_loopback_no_secrets_no_start(self):
        self.env["CFA_SYSTEMD_RUNTIME_DIR"] = str(self.base / "runtime")
        (self.base / "runtime").mkdir()
        self.tool("systemctl", '#!/bin/sh\nprintf "systemctl $*\\n" >> "' + str(self.commands) + '"\nexit 0\n')
        proc = self.run_install("--no-deps", "--bin-dir", str(self.base / "pkg"),
                                "--link-dir", str(self.base / "links"), "--ui-service",
                                env={"CF_TUNNEL_ACCEL_API_TOKEN": "fixture-not-a-real-secret"})
        self.assert_ok(proc)
        text = (self.base / "systemd/cf-tunnel-ui.service").read_text()
        self.assertIn('--host 127.0.0.1 --port 8642', text)
        self.assertIn(str(self.base / "pkg/cf-tunnel-ui"), text)
        self.assertNotIn("fixture-not-a-real-secret", text)
        self.assertNotIn("TOKEN", text)
        self.assertNotIn("fixture-not-a-real-secret", (self.config / "env").read_text())
        self.assertEqual(self.commands.read_text(), "systemctl daemon-reload\n")

    def test_explicit_tunnel_service_validation_before_units_no_restart(self):
        config = self.select_service()
        self.tool("systemctl", '#!/bin/sh\nprintf "systemctl $*\\n" >> "' + str(self.commands) + '"\nexit 0\n')
        original = config.read_bytes()
        proc = self.run_install("--no-deps", "--bin-dir", str(self.base / "pkg"),
                                "--link-dir", str(self.base / "links"), "--service", "--config", str(config),
                                "--service-name", "chosen.service")
        self.assert_ok(proc)
        self.assertEqual(config.read_bytes(), original)
        self.assertTrue((self.base / "systemd/chosen.service").is_file())
        lines = self.commands.read_text().splitlines()
        self.assertTrue(lines[0].endswith("tunnel ingress validate"))
        self.assertEqual(lines[-1], "systemctl daemon-reload")
        self.assertFalse(any("restart" in line or "enable" in line or "start" in line for line in lines))

    def test_invalid_ingress_fails_before_writes(self):
        config = self.select_service()
        before = snapshot(self.home)
        proc = self.run_install("--no-deps", "--bin-dir", str(self.base / "pkg"),
                                "--link-dir", str(self.base / "links"), "--service", "--config", str(config),
                                "--service-name", "chosen.service", env={"VALIDATION_EXIT": "1"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ingress validation", proc.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.base / "pkg").exists())
        self.assertFalse((self.base / "systemd").exists())
        self.assertNotIn("systemctl", self.commands.read_text())

    def test_systemd_reload_failure_rolls_back_install_env_links_and_unit(self):
        self.seed_old()
        (self.base / "runtime").mkdir()
        self.env["CFA_SYSTEMD_RUNTIME_DIR"] = str(self.base / "runtime")
        unit_dir = self.base / "systemd"
        unit_dir.mkdir()
        (unit_dir / "cf-tunnel-ui.service").write_text("old unit\n")
        before_home, before_units = snapshot(self.home), snapshot(unit_dir)
        proc = self.run_install("--no-deps", "--bin-dir", str(self.package), "--link-dir", str(self.links), "--ui-service")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("restored", proc.stderr)
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(snapshot(unit_dir), before_units)
        self.assertEqual(self.commands.read_text(), "systemctl daemon-reload\nsystemctl daemon-reload\n")

    def test_no_systemd_degrades_to_foreground_and_user_service_is_error(self):
        proc = self.run_install("--no-deps", "--bin-dir", str(self.package), "--link-dir", str(self.links), "--ui-service")
        self.assert_ok(proc)
        self.assertIn("systemd is not running", proc.stderr)
        self.assert_no_commands()
        before = snapshot(self.base)
        proc = self.run_install("--user", "--no-deps", "--ui-service")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(snapshot(self.base), before)

    def test_credentials_mismatch_zero_writes(self):
        cred = self.base / "credentials.json"
        cred.write_text(json.dumps({"TunnelID": "different", "AccountTag": "fixture", "TunnelSecret": "fixture"}))
        before = snapshot(self.base)
        proc = self.run_install("--no-deps", "--service", "--config", str(self.base / "new.yml"),
                                "--service-name", "selected.service", "--tunnel-id", UUID,
                                "--credentials-file", str(cred), "--hostname", "app.example.com", "--backend", "http://localhost:8080")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match", proc.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_explicit_new_config_from_credentials_offline_no_discovery(self):
        self.select_service()
        self.tool("systemctl", '#!/bin/sh\nprintf "systemctl $*\\n" >> "' + str(self.commands) + '"\nexit 0\n')
        cred = self.base / "chosen credentials.json"
        cred.write_text(json.dumps({"TunnelID": UUID, "AccountTag": "fixture", "TunnelSecret": "fixture"}))
        target = self.base / "tunnel/config.yml"
        proc = self.run_install("--no-deps", "--bin-dir", str(self.base / "pkg"), "--link-dir", str(self.base / "links"),
                                "--service", "--config", str(target), "--service-name", "selected.service", "--tunnel-id", UUID,
                                "--credentials-file", str(cred), "--hostname", "app.example.com", "--backend", "https://localhost:8443")
        self.assert_ok(proc)
        text = target.read_text()
        self.assertIn(UUID, text)
        self.assertIn(str(cred), text)
        self.assertIn("https://localhost:8443", text)
        self.assertNotIn("protocol:", text)
        self.assertNotIn("TunnelSecret", text)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_package_source_collision_and_file_path_collision_zero_writes(self):
        source = self.fixture_package()
        cases = [{"CFA_BIN_DIR": str(source)},
                 {"CFA_BIN_DIR": str(self.base / "pkg"), "CFA_CONFIG_DIR": str(self.base / "pkg/config")},
                 {"CF_TUNNEL_ACCEL_ENV_FILE": str(self.base / "same"), "CF_TUNNEL_ACCEL_TOKEN_FILE": str(self.base / "same")}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                before = snapshot(self.base)
                proc = self.run_install("--user", "--no-deps", "--source-dir", str(source), env=overrides)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def test_mirror_rejected_before_writes(self):
        before = snapshot(self.base)
        proc = self.run_install("--user", env={"CFA_GH_MIRROR": "https://untrusted.invalid/"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("official HTTPS", proc.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assert_no_commands()

    def release_fixture(self, checksum_bad=False, wrong_arch=False):
        binary = self.base / "binary-fixture"
        data = bytearray(64)
        data[:4] = b"\x7fELF"
        data[4:6] = bytes((2, 1))
        data[18:20] = (183 if wrong_arch else 62).to_bytes(2, "little")
        binary.write_bytes(data)
        metadata = self.base / "release-metadata.json"
        metadata.write_text(json.dumps({"tag_name": "2026.1.0", "assets": [{
            "name": "cloudflared-linux-amd64",
            "browser_download_url": "https://github.com/cloudflare/cloudflared/releases/download/2026.1.0/cloudflared-linux-amd64",
            "digest": "sha256:" + ("0" * 64 if checksum_bad else hashlib.sha256(data).hexdigest())}]}))
        self.tool("curl", '#!/bin/sh\nprintf "curl $*\\n" >> "' + str(self.commands) + '"\nout=""\nwhile [ "$#" -gt 0 ]; do\n case "$1" in --output) shift; out="$1";; https://*) url="$1";; esac\n shift\ndone\ncase "$url" in https://api.github.com/*) cp "' + str(metadata) + '" "$out";; https://github.com/cloudflare/cloudflared/releases/*) cp "' + str(binary) + '" "$out";; *) exit 98;; esac\n')

    def test_official_download_checksum_failure_preserves_old_install(self):
        self.seed_old()
        self.release_fixture(checksum_bad=True)
        before = snapshot(self.home)
        proc = self.run_install("--user")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SHA256 mismatch", proc.stderr)
        self.assertEqual(snapshot(self.home), before)
        commands = self.commands.read_text()
        self.assertIn("--proto =https", commands)
        self.assertIn("--proto-redir =https", commands)
        self.assertNotIn("systemctl", commands)
        self.assertNotIn("apt-get", commands)

    def test_official_download_wrong_architecture_rejected(self):
        self.release_fixture(wrong_arch=True)
        proc = self.run_install("--user")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("architecture mismatch", proc.stderr)
        self.assertFalse(self.package.exists())
        self.assertFalse((self.base / "missing-cloudflared").exists())


if __name__ == "__main__":
    unittest.main()
