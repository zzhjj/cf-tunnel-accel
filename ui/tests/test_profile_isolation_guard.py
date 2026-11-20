"""Guards that keep multi-profile isolation honest.

Two invariants make the in-process profile model sound:

1. Every CLI constant the console relies on is fixed at import, when the process
   environment still belongs to that one profile.
2. No CLI function *reachable from the console* reads ``os.environ`` at call time.
   If one did, two profiles would silently share whichever environment the
   loader restored last.

The first is covered by test_profiles.py. The second is enforced here by walking
the CLI call graph, so a future CLI change that starts reading the environment
inside a reachable function fails this test instead of silently leaking between
profiles.
"""

import ast
from pathlib import Path
import re
import unittest

try:
    from .test_ui_server import mod
except ImportError:
    from test_ui_server import mod
import json
import tempfile

try:
    from .test_cli_startup import CLI_STUB, MIN_ENV
except ImportError:
    from test_cli_startup import CLI_STUB, MIN_ENV


CLI = Path(__file__).resolve().parents[2] / 'cf-tunnel-accel'
SERVER = Path(__file__).resolve().parents[1] / 'server.py'


def _functions(tree):
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _reads_environ(node):
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == 'environ'
            and isinstance(sub.value, ast.Name)
            and sub.value.id == 'os'
        ):
            return True
    return False


def _callees(node, known):
    """Same-module or self.* calls that resolve to a known function."""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name) and func.id in known:
            names.add(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == 'self'
            and func.attr in known
        ):
            names.add(func.attr)
    return names


class ProfileIsolationGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli_tree = ast.parse(CLI.read_text(encoding='utf-8'))
        cls.functions = _functions(cls.cli_tree)
        cls.server_src = SERVER.read_text(encoding='utf-8')
        cls.touched = set(re.findall(r'accel\.([A-Za-z_][A-Za-z0-9_]*)', cls.server_src))

    def test_every_console_attribute_exists_in_the_cli(self):
        module_level = set()
        for node in self.cli_tree.body:
            if isinstance(node, ast.Assign):
                module_level.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                module_level.add(node.target.id)
        missing = sorted(
            name for name in self.touched
            if name not in self.functions and name not in module_level
        )
        self.assertEqual(missing, [], 'console references missing CLI attributes')

    def test_no_environment_read_is_reachable_from_the_console(self):
        # Transitive closure over same-module/self calls starting at every
        # attribute the console touches.
        reachable, frontier = set(), [n for n in self.touched if n in self.functions]
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            frontier.extend(_callees(self.functions[name], self.functions) - reachable)
        offenders = sorted(n for n in reachable if _reads_environ(self.functions[n]))
        self.assertEqual(
            offenders, [],
            'these CLI functions read os.environ at call time and are reachable '
            'from the console; profiles would then share one environment. '
            'Run profiles in separate processes before relying on them.',
        )

    def test_runtime_helpers_never_read_the_shared_environment(self):
        # 2.x resolves settings once in cfa_config; profile helpers use snapshots.
        readers = {n for n, f in self.functions.items() if _reads_environ(f)}
        self.assertEqual(readers, set())


class GuardSelfCheck(unittest.TestCase):
    """Negative control: the detector must flag a real leak, not just pass."""

    LEAK = '''
import os
CONFIG = "/tmp/x.yml"
def load_env_file():
    return os.environ.get("CF_TUNNEL_ACCEL_CONFIG")
def read_config_text():
    return load_env_file()          # transitive leak, two calls deep
def build_state():
    return read_config_text()
def safe_helper():
    return CONFIG
'''

    def test_transitive_leak_is_detected(self):
        tree = ast.parse(self.LEAK)
        funcs = _functions(tree)
        self.assertTrue(_reads_environ(funcs['load_env_file']))
        reachable, frontier = set(), ['build_state']
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            frontier.extend(_callees(funcs[name], funcs) - reachable)
        self.assertEqual(reachable, {'build_state', 'read_config_text', 'load_env_file'})
        offenders = sorted(n for n in reachable if _reads_environ(funcs[n]))
        self.assertEqual(offenders, ['load_env_file'])

    def test_clean_function_reports_nothing(self):
        tree = ast.parse(self.LEAK)
        funcs = _functions(tree)
        self.assertFalse(_reads_environ(funcs['safe_helper']))
        self.assertEqual(_callees(funcs['safe_helper'], funcs), set())


class MultiAccountAtImport(unittest.TestCase):
    """Per-profile credentials are fixed at import, so accounts stay separate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'cli.py'
        self.cli.write_text(CLI_STUB, encoding='utf-8')

    def _profile(self, ident, token):
        env = self.root / (ident + '.env')
        env.write_text(
            MIN_ENV.format(config=self.root / (ident + '.yml'), service=ident + '.service')
            + 'CF_TUNNEL_ACCEL_API_TOKEN=%s\n' % token,
            encoding='utf-8',
        )
        return {'id': ident, 'name': ident, 'env_file': str(env)}

    def test_tokens_do_not_leak_between_profiles(self):
        registry = self.root / 'profiles.json'
        registry.write_text(json.dumps({'profiles': [
            self._profile('acct_one', 'TOKEN_ONE_1111'),
            self._profile('acct_two', 'TOKEN_TWO_2222'),
        ]}), encoding='utf-8')
        _, profiles = mod.load_console_profiles(str(self.cli), registry, _Parser())
        self.assertEqual(profiles['acct_one'].CF_API_TOKEN, 'TOKEN_ONE_1111')
        self.assertEqual(profiles['acct_two'].CF_API_TOKEN, 'TOKEN_TWO_2222')

    def test_import_captures_the_profile_not_the_last_loaded_one(self):
        registry = self.root / 'profiles.json'
        registry.write_text(json.dumps({'profiles': [
            self._profile('first', 'TOKEN_FIRST'),
            self._profile('second', 'TOKEN_SECOND'),
        ]}), encoding='utf-8')
        _, profiles = mod.load_console_profiles(str(self.cli), registry, _Parser())
        # Reading them again after both imports proves the first module kept its
        # own import-time values instead of the environment of the second.
        self.assertEqual(profiles['first'].CF_API_TOKEN, 'TOKEN_FIRST')
        self.assertEqual(profiles['first'].SERVICE_NAME, 'first.service')
        self.assertEqual(profiles['second'].SERVICE_NAME, 'second.service')


class _Parser:
    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)
        raise SystemExit(2)


if __name__ == '__main__':
    unittest.main()
