import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "cf-tunnel-accel"


def load_cli(runtime: str):
    """Load the real CLI with an isolated profile/environment snapshot."""
    home = tempfile.TemporaryDirectory()
    root = Path(home.name)
    config = root / "cloudflared.yml"
    config.write_text(
        "tunnel: 00000000-0000-0000-0000-000000000001\n"
        "ingress:\n  - hostname: app.example.com\n    service: http://127.0.0.1:8080\n"
        "  - service: http_status:404\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(root),
        "CF_TUNNEL_ACCEL_RUNTIME": runtime,
        "CF_TUNNEL_ACCEL_CONFIG": str(config),
        "CF_TUNNEL_ACCEL_TUNNEL_ID": "00000000-0000-0000-0000-000000000001",
        "CF_TUNNEL_ACCEL_AUX_ZONE": "example.com",
        "CF_TUNNEL_ACCEL_AUX_ZONE_ID": "a" * 32,
        "CF_TUNNEL_ACCEL_API_TOKEN": "x" * 40,
        "CF_TUNNEL_ACCEL_PREFERRED_TARGET": "cdn.example.net",
    }
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    name = "cf_tunnel_accel_runtime_" + runtime
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return module, home


class ContainerRuntimeTests(unittest.TestCase):
    def test_capabilities_are_snapshot_and_local_mutations_are_blocked_first(self):
        mod, home = load_cli("container")
        self.addCleanup(home.cleanup)
        self.assertEqual(mod.RUNTIME_MODE, "container")
        self.assertEqual(mod.runtime_capabilities(), {
            "runtime": "container",
            "local_tunnel_management": False,
            "service_discovery": False,
            "health_scope": "container-network",
        })
        with mock.patch.dict(os.environ, {"CF_TUNNEL_ACCEL_RUNTIME": "host"}, clear=False), \
             mock.patch.object(mod, "check_token", side_effect=AssertionError("token check")), \
             mock.patch.object(mod, "exclusive_lock", side_effect=AssertionError("lock")):
            for args in ([], ['add', 'app.example.com', '127.0.0.1:80'],
                         ['delete', 'app.example.com'], ['rm', 'app.example.com'],
                         ['schedule', 'install'], ['timer', 'remove'], ['auto', 'status']):
                self.assertEqual(mod.main(['cf-tunnel-accel', *args]), 2)
        before = mod.CONFIG_PATH.read_bytes()
        with mock.patch.object(mod, 'api', side_effect=AssertionError('cloud request')), \
                mock.patch.object(mod, 'run', side_effect=AssertionError('subprocess')):
            for invoke in (lambda: mod.prepare_configuration(require_local=True),
                           lambda: mod.upsert_tunnel_hostname('app.example.com', 'http://127.0.0.1:80'),
                           lambda: mod.remove_tunnel_hostname('app.example.com'),
                           lambda: mod.validate_and_restart_tunnel(),
                           lambda: mod.add_accelerated_service('app.example.com', 'http://127.0.0.1:80'),
                           lambda: mod.remove_service('app.example.com'),
                           lambda: mod.install_cdn_schedule(),
                           lambda: mod.remove_cdn_schedule(),
                           lambda: mod.show_cdn_schedule()):
                with self.assertRaises(mod.CFError):
                    invoke()
        self.assertEqual(mod.CONFIG_PATH.read_bytes(), before)

    def test_container_status_health_never_runs_host_commands(self):
        mod, home = load_cli("container")
        self.addCleanup(home.cleanup)
        with mock.patch.object(mod, "run", side_effect=AssertionError("host command")), \
             mock.patch.object(mod, "_systemd_active", side_effect=AssertionError("systemd")), \
             mock.patch.object(mod, "check_auxiliary_chain", return_value=0), \
             mock.patch.object(mod, "list_custom_hostnames", return_value=[]), \
             mock.patch.object(mod, "probe_local_backend", return_value=(True, "HTTP/1.1 200 OK")):
            mod.show_status()
            self.assertTrue(mod.health_check())

    def test_invalid_runtime_is_a_cli_error(self):
        mod, home = load_cli("invalid")
        self.addCleanup(home.cleanup)
        self.assertEqual(mod.main(["cf-tunnel-accel", "config"]), 2)
        status = mod.configuration_status()
        self.assertFalse(status["ready"])
        self.assertTrue(any("CF_TUNNEL_ACCEL_RUNTIME" in item for item in status["missing"]))

    def test_host_capabilities_remain_unchanged(self):
        mod, home = load_cli("host")
        self.addCleanup(home.cleanup)
        self.assertEqual(mod.runtime_capabilities(), {
            "runtime": "host",
            "local_tunnel_management": True,
            "service_discovery": True,
            "health_scope": "host",
        })


if __name__ == "__main__":
    unittest.main()
