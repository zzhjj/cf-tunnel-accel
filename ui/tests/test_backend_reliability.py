"""Backend reliability, with filesystem and network boundaries held in fixtures."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest import mock

try:
    from .test_ui_server import FakeAccel, mod
except ImportError:
    from test_ui_server import FakeAccel, mod


class RunnerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'jobs.json'
        self.audit = mod.Audit(self.root / 'audit.jsonl')
        self.accel = FakeAccel()

    def runner(self, path=None):
        runner = mod.JobRunner(self.accel, self.audit, jobs_path=path or self.path)
        self.addCleanup(lambda: self.assertTrue(runner.close(timeout=3)))
        return runner

    def test_lock_precedes_preflight_and_dispatch(self):
        @contextmanager
        def held():
            raise RuntimeError('lock held')
            yield

        self.accel.exclusive_lock = held
        self.accel.prepare_configuration = mock.Mock()
        runner = self.runner()
        job = runner.submit('delete', {'hostname': 'a.example.org'}, 'test')
        self.assertTrue(runner.close(drain=True, timeout=3))
        self.assertEqual(job.status, 'error')
        self.assertEqual(job.error, 'lock held')
        self.assertEqual(self.accel.calls, [])
        self.accel.prepare_configuration.assert_not_called()
        with self.assertRaises(mod.JobBusy):
            runner.submit('health', {}, 'test')

    def test_structured_failure_invalidates_state_and_health_is_distinct(self):
        @contextmanager
        def locked():
            order.append('lock')
            yield
            order.append('unlock')

        order = []
        self.accel.exclusive_lock = locked
        self.accel.prepare_configuration = lambda **kw: order.append('prepare')
        def failed(host):
            order.append('mutate')
            return {'ok': False, 'error': 'partial change', 'changed': True}
        self.accel.remove_service = failed
        self.accel.health_check = mock.Mock(return_value={'ok': False, 'details': ['DNS']})
        runner = self.runner()
        with mock.patch.object(mod, 'CACHE', mod.TTLCache()):
            key = mod.cache_key(self.accel, 'state')
            mod.CACHE.get(key, 100, lambda: 'old')
            job = runner.submit('delete', {'hostname': 'a.example.org'}, 'test')
            health = runner.submit('health', {}, 'test')
            self.assertTrue(runner.close(drain=True, timeout=3))
            self.assertNotIn(key, mod.CACHE.data)
        self.assertEqual(order, ['lock', 'prepare', 'mutate', 'unlock'])
        self.assertEqual(job.status, 'error')
        self.assertEqual(job.error, 'partial change')
        self.assertTrue(job.result['changed'])
        self.assertEqual(health.status, 'done')
        self.assertEqual(health.snapshot()['outcome'], 'unhealthy')
        self.assertEqual(health.result['details'], ['DNS'])

    def test_restore_interrupted_jobs_without_replay(self):
        runner = self.runner()
        entered, release = threading.Event(), threading.Event()
        def block(*args, **kw):
            entered.set()
            return release.wait(5)
        self.accel.health_check = block
        self.addCleanup(release.set)
        started = runner.submit('health', {}, 'tester')
        self.assertTrue(entered.wait(3))
        queued = runner.submit('delete', {'hostname': 'a.example.org'}, 'tester')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        started.log('line already seen by client')
        cursor = runner.detail(started.id)['job']['next']
        backup = self.root / 'crash.json'
        backup.write_bytes(self.path.read_bytes())
        self.assertNotIn('args', json.loads(backup.read_text())['jobs'][1])
        restored = self.runner(backup)
        self.assertTrue(restored.close(drain=True, timeout=3))
        self.assertEqual(restored.get(started.id).status, 'interrupted')
        self.assertEqual([row['m'] for row in restored.get(started.id).snapshot()['lines']][:1],
                         ['line already seen by client'])
        self.assertGreater(restored.detail(started.id, cursor)['job']['next'], cursor)
        self.assertEqual(restored.get(queued.id).status, 'interrupted')
        self.assertEqual(restored.get(queued.id).snapshot()['profile'], 'default')
        self.assertEqual(self.accel.calls, [])
        release.set()
        self.assertTrue(runner.close(drain=False, timeout=3))

    def test_store_failure_remains_visible_without_breaking_jobs(self):
        path = self.root / 'not-a-directory'
        path.touch()
        runner = self.runner(path / 'jobs.json')
        job = runner.submit('health', {}, 'tester')
        self.assertTrue(runner.close(timeout=3))
        self.assertEqual(job.status, 'done')
        self.assertTrue(runner.list_jobs()['store_error'])
        self.assertTrue(any(r['event'] == 'job.store_error' for r in self.audit.tail()))

    def test_shared_cdn_eligibility_is_authoritative(self):
        self.accel.cdn_eligible = mock.Mock(return_value=False)
        self.accel.benchmark_cdn = mock.Mock(return_value={
            'domain': 'fast.example', 'https_ok': 1, 'median_https': float('nan')})
        self.accel.CDN_CANDIDATES = ['fast.example']
        self.accel.get_current_preferred_target = mock.Mock(return_value=None)
        runner = self.runner()
        job = runner.submit('cdn-test', {}, 'tester')
        self.assertTrue(runner.close(timeout=3))
        self.assertEqual(job.status, 'done', job.error)
        self.assertIsNone(job.result['best'])
        self.assertIsNone(job.result['ranked'][0]['median_https'])
        self.assertFalse(job.result['ranked'][0]['eligible'])
        self.accel.cdn_eligible.assert_called_once_with(self.accel.benchmark_cdn.return_value)
        self.assertEqual(self.accel.calls, [])

    def test_log_window_advances_past_old_length(self):
        job = mod.Job('health', {}, 'test')
        for i in range(805):
            job.log(str(i))
        snap = job.snapshot(2)
        self.assertEqual((snap['offset'], snap['next'], len(snap['lines'])), (5, 805, 800))
        job.log('new')
        self.assertEqual([r['m'] for r in job.snapshot(805)['lines']], ['new'])
        self.assertEqual(job.snapshot(805)['next'], 806)


class PartialReadTests(unittest.TestCase):
    def test_api_failures_do_not_hide_routes_or_fake_confirmed_target(self):
        accel = FakeAccel()
        accel.ensure_zone_cache = mock.Mock(side_effect=RuntimeError('zones denied'))
        accel.list_custom_hostnames = mock.Mock(side_effect=RuntimeError('SaaS denied'))
        accel.PREFERRED_TARGET = 'unverified.example.org'
        def target(strict=False):
            if strict:
                raise RuntimeError('DNS denied')
            return accel.PREFERRED_TARGET
        accel.get_current_preferred_target = target
        with mock.patch.object(mod.subprocess, 'run', return_value=mock.Mock(stdout='1', returncode=0)):
            state = mod.build_state(accel, force=True)
        self.assertEqual([r['hostname'] for r in state['routes']], ['a.example.org'])
        self.assertIsNone(state['routes'][0]['external'])
        self.assertEqual(set(state['errors']), {'zones', 'saas', 'preferred'})
        self.assertIsInstance(state['updated_at'], (int, float))
        self.assertIsNone(state['preferred']['target'])
        self.assertIs(state['preferred']['verified'], False)
        cdn = mod.build_cdn(accel, force=True)
        self.assertIsNone(cdn['target'])
        self.assertIs(cdn['verified'], False)
        self.assertTrue(cdn['errors'])

    def test_same_port_distinct_endpoints_keep_mapping_and_probe_scope(self):
        first, second = FakeAccel(), FakeAccel()
        first._profile_id, second._profile_id = 'alpha', 'beta'
        first.config = second.config = ('ingress:\n  - hostname: mapped.example.org\n'
                                        '    service: https://[::1]:8888/path\n')
        stdout = ('LISTEN 0 1 127.0.0.1:8888 0.0.0.0:* users:(("http",pid=3,fd=1))\n'
                  'LISTEN 0 1 [::1]:8888 [::]:* users:(("ssl",pid=4,fd=1))\n')
        with mock.patch.object(mod.shutil, 'which', return_value='/fixture/ss'), \
                mock.patch.object(mod.subprocess, 'run', return_value=mock.Mock(stdout=stdout, returncode=0)), \
                mock.patch.object(mod, 'CACHE', mod.TTLCache()):
            for accel in (first, second):
                accel.probe_local_backend = mock.Mock(return_value=(True, 'OK'))
                rows = {r['addr']: r for r in mod.discover_services(accel)}
                self.assertEqual(rows['::1']['mapped'], ['mapped.example.org'])
                self.assertEqual(rows['127.0.0.1']['mapped'], [])
                self.assertEqual(rows['::1']['cmd'], 'ssl')
                urls = {call.args[0] for call in accel.probe_local_backend.call_args_list}
                self.assertEqual(urls, {'https://[::1]:8888/path', 'http://127.0.0.1:8888'})

    def test_missing_record_is_not_reported_as_preferred_default(self):
        accel = FakeAccel()
        accel.AUX_ZONE_ID = ''
        accel.resolve_aux_zone_id = mock.Mock(return_value='resolved-zone')
        accel.list_dns = mock.Mock(return_value=[])
        result = mod.build_cdn(accel, force=True)
        self.assertIsNone(result['target'])
        self.assertFalse(result['verified'])
        self.assertEqual(result['errors'], {})
        accel.list_dns.assert_called_once_with('resolved-zone', accel.PREFERRED_ENTRY)

    def test_missing_config_keeps_other_state_sections(self):
        accel = FakeAccel()
        accel.config = ''
        with mock.patch.object(mod.subprocess, 'run', return_value=mock.Mock(stdout='1', returncode=0)):
            state = mod.build_state(accel, force=True)
        self.assertEqual(state['routes'], [])
        self.assertIsNone(state['counts']['routes'])
        self.assertIn('config', state['errors'])
        self.assertIsNone(state['freshness']['config'])
        self.assertTrue(state['preferred']['verified'])
        self.assertEqual(state['service']['mainpid'], 1)


class StartupShutdownTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.root),
                    'XDG_CONFIG_HOME': str(self.root / 'xdg'),
                    'PYTHONWARNINGS': 'error'}
        self.server_path = Path(mod.__file__).resolve()

    def test_print_token_follows_xdg_and_explicit_override(self):
        for override in (None, self.root / 'explicit' / 'token'):
            env = dict(self.env)
            if override is not None:
                env['CF_TUNNEL_ACCEL_UI_TOKEN_FILE'] = str(override)
            result = subprocess.run([sys.executable, str(self.server_path), '--print-token'],
                                    env=env, cwd=self.root, capture_output=True, timeout=5,
                                    text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = override or self.root / 'xdg/cf-tunnel-accel/ui-token'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.stdout.strip(), path.read_text().strip())

    def test_missing_module_has_actionable_startup_error(self):
        env = dict(self.env, CF_TUNNEL_ACCEL_SCRIPT=str(self.root / 'absent'))
        result = subprocess.run([sys.executable, str(self.server_path)], env=env, cwd=self.root,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--module', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root / 'xdg').exists())

    def test_sigterm_drains_active_task_in_subprocess(self):
        cli = self.root / 'fixture.py'
        entered, released, done = (self.root / n for n in ('enter', 'release', 'done'))
        cli.write_text('from pathlib import Path\nimport time\n'
                       'def cprint(msg="", color=None):\n    pass\n'
                       'def health_check(*args, **kwargs):\n'
                       f'    Path({str(entered)!r}).touch()\n'
                       f'    while not Path({str(released)!r}).exists():\n'
                       '        time.sleep(0.01)\n'
                       f'    Path({str(done)!r}).touch()\n'
                       '    return True\n')
        env = dict(self.env, CF_TUNNEL_ACCEL_SCRIPT=str(cli),
                   CF_TUNNEL_ACCEL_UI_JOBS=str(self.root / 'custom-jobs.json'))
        output = self.root / 'console.log'
        with output.open('wb') as fh, subprocess.Popen(
                [sys.executable, '-u', str(self.server_path), '--port', '0'], env=env,
                cwd=self.root, stdout=fh, stderr=fh) as proc:
            try:
                import re
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and proc.poll() is None:
                    match = re.search(r'http://127\.0\.0\.1:(\d+)/', output.read_text())
                    if match:
                        break
                    time.sleep(0.01)
                else:
                    self.fail(output.read_text())
                token = (self.root / 'xdg/cf-tunnel-accel/ui-token').read_text().strip()
                from http.cookies import SimpleCookie
                conn = http.client.HTTPConnection('127.0.0.1', int(match.group(1)), timeout=3)
                try:
                    conn.request('POST', '/api/session', json.dumps({'token': token}),
                                 {'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    cookie = SimpleCookie()
                    for key, value in response.getheaders():
                        if key.lower() == 'set-cookie':
                            cookie.load(value)
                    csrf = json.loads(response.read())['csrf']
                    conn.request('POST', '/api/jobs', json.dumps({'op': 'health'}), {
                        'Content-Type': 'application/json', 'X-CSRF': csrf,
                        'Cookie': 'arc_sid=' + cookie['arc_sid'].value})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    ident = json.loads(response.read())['job']['id']
                finally:
                    conn.close()
                deadline = time.monotonic() + 3
                while not entered.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(entered.exists())
                proc.send_signal(signal.SIGTERM)
                released.touch()
                self.assertEqual(proc.wait(timeout=5), 0, output.read_text())
                self.assertTrue(done.exists())
                self.assertIn('停止 HTTP', output.read_text())
                with (self.root / 'custom-jobs.json').open() as state:
                    self.assertEqual([(job['id'], job['status']) for job in json.load(state)['jobs']],
                                     [(ident, 'done')])
            finally:
                released.touch()
                if proc.poll() is None:
                    proc.kill()
