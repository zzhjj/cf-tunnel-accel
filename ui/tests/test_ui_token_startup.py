"""Exercise credential sources with the actual server process and HTTP login."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SERVER = Path(__file__).resolve().parents[1] / 'server.py'
INITIAL_KEY = re.compile(r'^  首次登录密钥: ([^\n]+)$', re.MULTILINE)


class LoginKeyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = {'HOME': str(self.home), 'PATH': os.environ['PATH'],
                    'PYTHONWARNINGS': 'error', 'LC_ALL': 'C.UTF-8'}
        self.path = self.home / '.config/cf-tunnel-accel/ui-token'

    def run_ui(self, *args, env=None):
        return subprocess.run([sys.executable, str(SERVER), *args], env=env or self.env,
                              cwd=self.home, capture_output=True, text=True, timeout=8)

    @contextmanager
    def console(self, *args, env=None):
        with tempfile.TemporaryFile() as log, subprocess.Popen(
            [sys.executable, '-u', str(SERVER), '--port', '0', *args], env=env or self.env,
            cwd=self.home, stdout=log, stderr=log,
        ) as process:
            # Read using a separate offset; seeking the shared FD can disrupt writes.
            def output():
                return os.pread(log.fileno(), 32_768, 0).decode('utf-8')

            try:
                deadline = time.monotonic() + 6
                while process.poll() is None and time.monotonic() < deadline:
                    match = re.search(r'访问\s+http://127\.0\.0\.1:(\d+)/', output())
                    if match:
                        yield int(match[1]), output
                        break
                    time.sleep(0.02)
                else:
                    self.fail(f'UI did not start (exit={process.poll()})')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

    def login(self, port, key):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            conn.request('POST', '/api/session', json.dumps({'token': key}),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_first_successful_start_prints_exactly_once_and_key_survives_restart(self):
        with self.console() as (port, output):
            keys = INITIAL_KEY.findall(output())
            self.assertEqual(len(keys), 1, 'first boot must print exactly one login key')
            key = keys[0]
            self.assertTrue(re.fullmatch(r'[0-9a-f]{48}', key) is not None)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.path.read_text().strip() == key)
            self.assertEqual(self.login(port, 'incorrect')[0], 401)
            status, payload = self.login(port, key)
            self.assertEqual(status, 200)
            self.assertTrue(payload.get('csrf'))
            self.assertTrue(key not in json.dumps(payload))
        with self.console() as (port, output):
            self.assertEqual(self.login(port, key)[0], 200)
            self.assertTrue(key not in output())
            self.assertTrue(not INITIAL_KEY.search(output()))

    def test_environment_key_is_custom_not_persisted_or_logged(self):
        key = 'configured-environment-key-00000123'
        with self.console(env=dict(self.env, CF_TUNNEL_ACCEL_UI_TOKEN=key)) as (port, output):
            self.assertEqual(self.login(port, key)[0], 200)
            self.assertTrue(key not in output())
            self.assertTrue(not INITIAL_KEY.search(output()))
            self.assertFalse(self.path.exists())

    def test_explicit_readonly_file_and_cli_file_precedence(self):
        file = self.home / 'readonly-key'
        key = 'configured-file-key-value-00000123'
        file.write_text(key + '\n')
        file.chmod(0o444)
        env = dict(self.env, CF_TUNNEL_ACCEL_UI_TOKEN_FILE=str(self.home / 'absent'))
        with self.console('--token-file', str(file), env=env) as (port, output):
            self.assertEqual(self.login(port, key)[0], 200)
            self.assertTrue(key not in output())
            self.assertFalse(self.path.exists())
        self.assertEqual(file.stat().st_mode & 0o777, 0o444)
        self.assertTrue(file.read_text() == key + '\n')

    def test_bad_sources_fail_without_fallback_or_value_disclosure(self):
        bad = self.home / 'bad-key'
        bad.write_text('too-short')
        value = 'conflicting-key-value-0000000000001'
        cases = [dict(CF_TUNNEL_ACCEL_UI_TOKEN='short'),
                 dict(CF_TUNNEL_ACCEL_UI_TOKEN='has\nnewline-' + 'x' * 32),
                 dict(CF_TUNNEL_ACCEL_UI_TOKEN_FILE=str(bad)),
                 dict(CF_TUNNEL_ACCEL_UI_TOKEN_FILE=str(self.home / 'absent')),
                 dict(CF_TUNNEL_ACCEL_UI_TOKEN=value, CF_TUNNEL_ACCEL_UI_TOKEN_FILE=str(bad))]
        for extra in cases:
            with self.subTest(source=sorted(extra)):
                result = self.run_ui('--print-token', env=dict(self.env, **extra))
                self.assertEqual(result.returncode, 2)
                self.assertTrue(value not in result.stdout + result.stderr)
                self.assertFalse(self.path.exists())

    def test_port_conflict_does_not_generate_or_rotate_managed_key(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = str(listener.getsockname()[1])
            result = self.run_ui('--port', port)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.path.exists())
            self.run_ui('--print-token')
            before = self.path.read_bytes()
            result = self.run_ui('--port', port, '--rotate-token')
            self.assertEqual(result.returncode, 2)
            self.assertTrue(self.path.read_bytes() == before)

    def test_initial_output_suppression_and_rotation(self):
        with self.console('--no-print-initial-token') as (port, output):
            key = self.path.read_text().strip()
            self.assertTrue(key not in output())
            self.assertEqual(self.login(port, key)[0], 200)
        with self.console('--rotate-token') as (port, output):
            new = self.path.read_text().strip()
            self.assertTrue(new != key)
            self.assertEqual(self.login(port, key)[0], 401)
            self.assertEqual(self.login(port, new)[0], 200)
            self.assertEqual(len(INITIAL_KEY.findall(output())), 1)

    def test_invalid_managed_key_is_not_silently_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('damaged-key\n')
        result = self.run_ui('--print-token')
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.path.read_text() == 'damaged-key\n')

    def test_environment_optout_and_external_rotation_rejected(self):
        with self.console(env=dict(self.env, CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN='0')) as (port, output):
            key = self.path.read_text().strip()
            self.assertTrue(key not in output())
            self.assertEqual(self.login(port, key)[0], 200)
        before = self.path.read_bytes()
        result = self.run_ui('--print-token', '--rotate-token',
                             env=dict(self.env, CF_TUNNEL_ACCEL_UI_TOKEN='custom-key-value-00000000000000000'))
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.path.read_bytes() == before)
