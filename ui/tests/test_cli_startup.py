"""Startup contract: argument gates and profile wiring, without binding a port.

`load_console_profiles` is exercised with a throwaway CLI stub so the test never
reads the real installation, token file or systemd units.
"""

import json
from pathlib import Path
import unittest
import unittest.mock as mock

try:
    from .test_ui_server import mod
except ImportError:
    from test_ui_server import mod


CLI_STUB = '''
import os

SCRIPT_VERSION = "stub"
SERVICE_NAME = os.environ.get("CF_TUNNEL_ACCEL_SERVICE", "stub.service")
CONFIG_PATH = os.environ.get("CF_TUNNEL_ACCEL_CONFIG", "/tmp/stub.yml")
PREFERRED_ENTRY = "cdn.example.org"
FALLBACK_ORIGIN = "origin.example.org"
TUNNEL_ID = os.environ.get("CF_TUNNEL_ACCEL_TUNNEL_ID", "")
CF_API_TOKEN = (os.environ.get("CF_TUNNEL_ACCEL_API_TOKEN")
                or os.environ.get("CF_API_TOKEN") or "")
CANDIDATES = []


def cprint(msg="", color=None):
    print(msg)
'''

MIN_ENV = '''CF_TUNNEL_ACCEL_TUNNEL_ID=11111111-1111-1111-1111-111111111111
CF_TUNNEL_ACCEL_CONFIG={config}
CF_TUNNEL_ACCEL_SERVICE={service}
'''


class Parser:
    """Stands in for argparse.ArgumentParser: records the fatal message."""

    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)
        raise SystemExit(2)


class StartupGates(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'cli.py'
        self.cli.write_text(CLI_STUB, encoding='utf-8')

    def _env(self, name, service, config=None):
        path = self.root / (name + '.env')
        path.write_text(
            MIN_ENV.format(config=config or self.root / (name + '.yml'), service=service),
            encoding='utf-8',
        )
        return str(path)

    def _registry(self, entries, name='profiles.json'):
        path = self.root / name
        path.write_text(json.dumps({'profiles': entries}), encoding='utf-8')
        return path

    def test_no_registry_keeps_single_default_profile(self):
        with mock.patch.object(mod, 'CONFIG_DIR', self.root / 'absent'):
            accel, profiles = mod.load_console_profiles(str(self.cli), None, Parser())
        self.assertEqual(list(profiles), ['default'])
        self.assertIs(accel, profiles['default'])
        self.assertEqual(profiles['default']._profile_name, 'Default')

    def test_registry_assigns_ids_and_display_names(self):
        registry = self._registry([
            {'id': 'home', 'name': 'Home tunnel', 'env_file': self._env('home', 'a.service')},
            {'id': 'edge', 'name': 'Edge tunnel', 'env_file': self._env('edge', 'b.service')},
        ])
        accel, profiles = mod.load_console_profiles(str(self.cli), registry, Parser())
        self.assertEqual(list(profiles), ['home', 'edge'])
        self.assertIs(accel, profiles['home'])
        self.assertEqual(profiles['edge']._profile_id, 'edge')
        self.assertEqual(profiles['edge']._profile_name, 'Edge tunnel')
        # Independent CLI globals, not a shared module object.
        self.assertIsNot(profiles['home'], profiles['edge'])
        self.assertEqual(profiles['home'].SERVICE_NAME, 'a.service')
        self.assertEqual(profiles['edge'].SERVICE_NAME, 'b.service')
        self.assertNotEqual(profiles['home'].CONFIG_PATH, profiles['edge'].CONFIG_PATH)

    def test_explicit_registry_failures_are_fatal_not_downgraded(self):
        parser = Parser()
        with self.assertRaises(SystemExit):
            mod.load_console_profiles(str(self.cli), self.root / 'missing.json', parser)
        self.assertIn('profile 加载失败', parser.errors[-1])

    def test_malformed_and_conflicting_registries_fail(self):
        cases = {
            'wrong keys': self._registry([{'id': 'a', 'name': 'A'}], 'wrong.json'),
            'duplicate id': self._registry([
                {'id': 'a', 'name': 'A', 'env_file': self._env('a', 'a.service')},
                {'id': 'a', 'name': 'B', 'env_file': self._env('b', 'b.service')},
            ], 'dup.json'),
            'relative env': self._registry([
                {'id': 'a', 'name': 'A', 'env_file': 'relative.env'}], 'rel.json'),
            'shared service': self._registry([
                {'id': 'a', 'name': 'A', 'env_file': self._env('a', 'same.service')},
                {'id': 'b', 'name': 'B', 'env_file': self._env('b', 'same')},
            ], 'svc.json'),
            'shared config': self._registry([
                {'id': 'a', 'name': 'A',
                 'env_file': self._env('a', 'a.service', self.root / 'one.yml')},
                {'id': 'b', 'name': 'B',
                 'env_file': self._env('b', 'b.service', self.root / 'one.yml')},
            ], 'cfg.json'),
            'missing required key': self._registry([
                {'id': 'a', 'name': 'A',
                 'env_file': str(self._write_raw('raw.env', 'CF_TUNNEL_ACCEL_CONFIG=/tmp/x.yml\n'))},
            ], 'raw.json'),
        }
        for label, registry in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                mod.load_console_profiles(str(self.cli), registry, Parser())

    def _write_raw(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_config_dir_registry_is_picked_up_implicitly(self):
        config_dir = self.root / 'conf'
        config_dir.mkdir()
        registry = self._registry([
            {'id': 'solo', 'name': 'Solo', 'env_file': self._env('solo', 'solo.service')},
        ], 'profiles.json')
        (config_dir / 'profiles.json').write_bytes(registry.read_bytes())
        with mock.patch.object(mod, 'CONFIG_DIR', config_dir):
            _, profiles = mod.load_console_profiles(str(self.cli), None, Parser())
        self.assertEqual(list(profiles), ['solo'])

    def test_remote_bind_and_port_range_are_rejected(self):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            mod.main(['--host', '0.0.0.0', '--port', '8699'])
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            mod.main(['--port', '70000'])
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            mod.main(['--port', '-1'])


if __name__ == '__main__':
    unittest.main()
