"""Real loopback HTTP tests; all backend and persistence effects stay in fixtures.

Run with: python3 -m unittest discover -s ui/tests -v
"""

import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest import mock

try:  # Both unittest discovery and package-qualified invocation.
    from .test_ui_server import FakeAccel, mod
except ImportError:
    from test_ui_server import FakeAccel, mod


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for patcher in (
            mock.patch.object(mod, 'AUDIT_PATH', root / 'audit.jsonl'),
            mock.patch.object(mod, 'JOBS_PATH', root / 'jobs.json', create=True),
            mock.patch.object(mod, 'CDN_HISTORY_PATH', root / 'cdn.json'),
            mock.patch.object(mod, 'CDN_HISTORY', mod.CdnHistory(root / 'cdn.json')),
            mock.patch.object(mod, 'CACHE', mod.TTLCache()),
            mock.patch.object(mod.subprocess, 'run', return_value=mock.Mock(stdout='0', returncode=0)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.profiles = {'alpha': FakeAccel(), 'beta': FakeAccel()}
        for ident, accel in self.profiles.items():
            accel._profile_id = ident
            accel._profile_name = ident.title() + ' console'
            accel._profile_metadata = {'id': ident, 'name': accel._profile_name,
                                       'env_file': None}
            accel.SERVICE_NAME = ident + '.service'
            accel.CONFIG_PATH = root / (ident + '.yml')
            accel.config = accel.config.replace('a.example.org', ident + '.example.org')
        self.token = secrets.token_hex(24)
        self.app = mod.App(self.profiles['alpha'], self.token, '127.0.0.1', 0,
                           False, self.profiles)
        self.addCleanup(lambda: self.assertTrue(self.app.runner.close(timeout=3)))
        self.httpd = mod.make_server(self.app)
        self.port = self.httpd.server_address[1]
        self.origin = 'http://127.0.0.1:' + str(self.port)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = ''
        self.csrf = ''

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, data=None, *, raw=None, headers=None,
                authenticated=True, length=True):
        body = raw if raw is not None else (
            json.dumps(data).encode() if data is not None else None)
        request_headers = {'Connection': 'close'}
        if authenticated:
            request_headers.update({'Cookie': self.cookie, 'X-CSRF': self.csrf})
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
            if length:
                request_headers['Content-Length'] = str(len(body))
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            # Low-level framing deliberately avoids http.client auto-inserting
            # Content-Length in the missing-length negative test.
            conn.putrequest(method, path)
            for key, value in request_headers.items():
                conn.putheader(key, value)
            conn.endheaders(body)
            response = conn.getresponse()
            payload = response.read()
            return (response.status, response.getheaders(),
                    json.loads(payload) if payload and response.getheader('Content-Type', '').startswith('application/json') else payload or None)
        finally:
            conn.close()

    def login(self):
        status, headers, body = self.request('POST', '/api/session',
                                             {'token': self.token}, authenticated=False)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        jar = SimpleCookie()
        for key, value in headers:
            if key.lower() == 'set-cookie':
                jar.load(value)
        self.assertTrue(jar['arc_sid']['httponly'])
        self.assertEqual(jar['arc_sid']['samesite'], 'Strict')
        self.cookie = '; '.join(key + '=' + value.value for key, value in jar.items())
        self.csrf = body['csrf']

    def submit(self, op='repair', profile='alpha', **args):
        return self.request('POST', '/api/jobs?profile=' + profile,
                            {'op': op, 'hostname': 'app.example.org', **args})

    def wait_job(self, ident):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, body = self.request('GET', '/api/jobs/' + ident)
            self.assertEqual(status, 200)
            if body['job']['status'] in ('done', 'error'):
                self.assertEqual(body['job']['status'], 'done', body['job']['error'])
                return body['job']
            time.sleep(0.005)
        self.fail('job did not complete before deadline')

    def assert_bad(self, result, status=400):
        code, _, body = result
        self.assertEqual(code, status)
        self.assertIs(body['ok'], False)
        self.assertTrue(body['error'])

    def block_backend(self, method='switch_to_preferred'):
        entered, release = threading.Event(), threading.Event()
        original = getattr(self.profiles['alpha'], method)

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test backend release timed out')
            return original(*args, **kwargs)

        patcher = mock.patch.object(self.profiles['alpha'], method, blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        # LIFO: release before stopping the worker, even on assertion failure.
        self.addCleanup(release.set)
        return entered, release

    def test_login_authentication_and_logout(self):
        self.assertFalse(self.request('GET', '/api/session')[2]['authenticated'])
        for path in ('/api/state', '/api/profiles', '/api/jobs/missing'):
            self.assert_bad(self.request('GET', path), 401)
        self.assert_bad(self.request('POST', '/api/session', {'token': 'incorrect'}), 401)
        self.login()
        self.assertTrue(self.request('GET', '/api/session')[2]['authenticated'])
        self.assertEqual(self.request('DELETE', '/api/session')[0], 204)
        self.assert_bad(self.request('GET', '/api/state'), 401)

    def test_login_rejects_cross_origin(self):
        self.assert_bad(self.request('POST', '/api/session', {'token': self.token},
                                    headers={'Origin': 'https://foreign.example'}), 403)
        self.assertEqual(len(self.app.sessions.data), 0)

    def test_csrf_and_origin_negative(self):
        self.login()
        for headers in ({'X-CSRF': ''}, {'X-CSRF': 'wrong'},
                        {'Origin': 'https://foreign.example'},
                        {'Origin': self.origin, 'Sec-Fetch-Site': 'cross-site'},
                        {'Sec-Fetch-Site': 'same-site'}):
            with self.subTest(headers=headers.keys()):
                self.assert_bad(self.request('POST', '/api/jobs',
                    {'op': 'repair', 'hostname': 'app.example.org'}, headers=headers), 403)
        self.assertEqual(len(self.app.runner.jobs), 0)
        accepted = self.request('POST', '/api/jobs',
            {'op': 'repair', 'hostname': 'app.example.org'},
            headers={'Origin': self.origin, 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(accepted[0], 200)
        self.wait_job(accepted[2]['job']['id'])

    def test_invalid_request_bodies(self):
        self.login()
        cases = [({'raw': b'{'}, 'malformed'),
                 ({'raw': b'[]'}, 'array'),
                 ({'raw': b'\xff'}, 'invalid UTF-8'),
                 ({'raw': b'x' * (65536 + 1)}, 'oversize'),
                 ({'raw': b'{}', 'length': False}, 'missing length'),
                 ({'raw': b'{}', 'headers': {'Content-Type': 'text/plain'}}, 'content type')]
        for options, name in cases:
            with self.subTest(name=name):
                self.assert_bad(self.request('POST', '/api/jobs', **options))
        self.assertEqual(len(self.app.runner.jobs), 0)

    def test_repair_hostname_validation_and_normalization(self):
        self.login()
        for host in (None, '', 'no-dot', '-bad.example.org', 'a b.example.org', 42):
            with self.subTest(host=host):
                self.assert_bad(self.submit(hostname=host))
        result = self.submit(hostname='APP.Example.ORG.')
        self.assertEqual(result[0], 200)
        self.wait_job(result[2]['job']['id'])
        self.assertEqual(self.profiles['alpha'].calls, [('switch', 'app.example.org')])

    def test_boolean_strings_are_rejected(self):
        self.login()
        for key in ('external', 'no_tls_verify', 'force', 'https'):
            for value in ('false', 'true', 0, 1):
                with self.subTest(key=key, value=value):
                    self.assert_bad(self.submit(**{key: value}))
        self.assertEqual(len(self.app.runner.jobs), 0)

    def test_bad_numeric_queries_are_400(self):
        self.login()
        for value in ('abc', '1.5'):
            self.assert_bad(self.request('GET', '/api/audit?limit=' + value))
        result = self.submit(op='health')
        self.assertEqual(result[0], 200)
        ident = result[2]['job']['id']
        self.wait_job(ident)
        self.assert_bad(self.request('GET', '/api/jobs/' + ident + '?since=abc'))

    def test_profile_list_and_name_metadata(self):
        self.login()
        status, _, body = self.request('GET', '/api/profiles')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'default': 'alpha', 'profiles': [
            {'id': 'alpha', 'name': 'Alpha console'},
            {'id': 'beta', 'name': 'Beta console'}]})

    def test_unknown_and_repeated_profiles_fail_without_dispatch(self):
        self.login()
        for query in ('profile=missing', 'profile=alpha&profile=beta',
                      'profile=alpha&profile=alpha'):
            with self.subTest(query=query):
                self.assert_bad(self.request('GET', '/api/state?' + query))
                self.assert_bad(self.request('POST', '/api/jobs?' + query,
                    {'op': 'repair', 'hostname': 'app.example.org'}))
        self.assertEqual(len(self.app.runner.jobs), 0)
        self.assertFalse(any(accel.calls for accel in self.profiles.values()))

    def test_state_cache_is_profile_isolated(self):
        self.login()
        reads = {}
        for ident, accel in self.profiles.items():
            reads[ident] = mock.Mock(wraps=accel.read_config_text)
            accel.read_config_text = reads[ident]
        for ident in ('alpha', 'beta', 'alpha', 'beta'):
            status, _, body = self.request('GET', '/api/state?profile=' + ident)
            self.assertEqual(status, 200)
            self.assertEqual(body['service']['name'], ident + '.service')
            self.assertEqual([r['hostname'] for r in body['routes']], [ident + '.example.org'])
        for read in reads.values():
            self.assertEqual(read.call_count, 1)
        self.assertIn(mod.cache_key(self.profiles['alpha'], 'state'), mod.CACHE.data)
        self.assertIn(mod.cache_key(self.profiles['beta'], 'state'), mod.CACHE.data)
        self.assertEqual(self.request('GET', '/api/state?profile=beta&fresh=1')[0], 200)
        self.assertEqual(reads['alpha'].call_count, 1)
        self.assertEqual(reads['beta'].call_count, 2)

    def test_mutation_dispatch_is_profile_isolated(self):
        self.login()
        for ident in ('beta', 'alpha'):
            result = self.submit(profile=ident, hostname=ident + '.example.org')
            self.assertEqual(result[0], 200)
            job = self.wait_job(result[2]['job']['id'])
            self.assertEqual(job['profile'], ident)
            self.assertEqual(self.profiles[ident].calls,
                             [('switch', ident + '.example.org')])
            if ident == 'beta':
                self.assertEqual(self.profiles['alpha'].calls, [])

    def test_concurrent_mutation_admission_is_atomic(self):
        self.login()
        entered, release = self.block_backend()
        count = 8
        barrier = threading.Barrier(count)
        results, errors = [], []
        lock = threading.Lock()

        def submit():
            try:
                barrier.wait(timeout=5)
                result = self.submit()
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(type(exc).__name__)

        threads = [threading.Thread(target=submit) for _ in range(count)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(5))
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(r[0] for r in results), [200] + [409] * (count - 1))
            self.assertEqual(len(self.app.runner.jobs), 1)
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
        accepted = next(r for r in results if r[0] == 200)
        self.wait_job(accepted[2]['job']['id'])
        self.assertEqual(self.profiles['alpha'].calls, [('switch', 'app.example.org')])

    def test_pending_capacity_bounds(self):
        self.login()
        entered, release = self.block_backend('health_check')
        with mock.patch.object(mod, 'MAX_PENDING_JOBS', 3):
            first = self.submit(op='health')
            self.assertEqual(first[0], 200)
            self.assertTrue(entered.wait(5))
            accepted = [first]
            for _ in range(2):
                result = self.submit(op='health')
                self.assertEqual(result[0], 200)
                accepted.append(result)
            self.assert_bad(self.submit(op='health'), 409)
            with self.app.runner.lock:
                self.assertEqual(len(self.app.runner.jobs), 3)
                self.assertEqual(len(self.app.runner.queue), 2)
            release.set()
            for result in accepted:
                self.wait_job(result[2]['job']['id'])
            result = self.submit(op='health')
            self.assertEqual(result[0], 200)
            self.wait_job(result[2]['job']['id'])

    def test_completed_job_and_log_capacity_bounds(self):
        self.login()
        with mock.patch.object(mod, 'MAX_JOBS_KEPT', 3), \
                mock.patch.object(mod, 'MAX_JOB_LINES', 4):
            ids = []
            for _ in range(6):
                result = self.submit(op='health')
                self.assertEqual(result[0], 200)
                ident = result[2]['job']['id']
                ids.append(ident)
                self.wait_job(ident)
            self.assertEqual(list(self.app.runner.jobs), ids[-3:])
            self.assert_bad(self.request('GET', '/api/jobs/' + ids[0]), 404)
            job = self.app.runner.get(ids[-1])
            for index in range(10):
                job.log('line-' + str(index))
            body = self.request('GET', '/api/jobs/' + job.id)[2]['job']
            self.assertEqual(body['next'], 10)
            self.assertEqual(body['offset'], 6)
            self.assertEqual([row['m'] for row in body['lines']],
                             ['line-' + str(i) for i in range(6, 10)])


    def test_exact_routes_and_methods(self):
        self.login()
        for method, path, expected in (
                ('POST', '/', 405), ('POST', '/app.js', 405),
                ('GET', '/api/host/app.example.org/extra', 404),
                ('GET', '/api/jobs/', 404), ('GET', '/api//state', 404),
                ('PATCH', '/api/state', 405), ('GET', '/api/no-such-route', 404)):
            with self.subTest(method=method, path=path):
                self.assert_bad(self.request(method, path), expected)
        self.assertEqual(self.request('HEAD', '/app.js')[0], 200)

    def test_environment_is_profile_local_and_jobs_list_is_global(self):
        self.login()
        report = {'ready': False, 'missing': ['config'], 'config': {},
                  'credentials': {'configured': False, 'source': None}, 'dependencies': {}}
        self.profiles['beta'].configuration_status = mock.Mock(return_value=report)
        self.assertEqual(self.request('GET', '/api/environment?profile=beta')[2],
                         {'ok': True, 'environment': {**report, 'capabilities': {
                             'runtime': 'host', 'local_tunnel_management': True,
                             'service_discovery': True, 'health_scope': 'host'}}})
        self.profiles['beta'].configuration_status.assert_called_once_with()
        self.assertIsInstance(self.request('GET', '/api/environment?profile=alpha')[2]
                              ['environment']['ready'], bool)
        entered, release = self.block_backend()
        rc, _, submitted = self.submit()
        self.assertEqual(rc, 200)
        self.assertTrue(entered.wait(3))
        rc, _, result = self.request('GET', '/api/jobs?profile=beta')
        self.assertEqual(rc, 200)
        self.assertEqual(result['active'], submitted['job']['id'])
        self.assertEqual([(j['id'], j['profile'], j['status']) for j in result['jobs']],
                         [(submitted['job']['id'], 'alpha', 'running')])
        release.set()
        self.wait_job(submitted['job']['id'])
        events = self.request('GET', '/api/audit')[2]['rows']
        self.assertEqual({(row['event'], row['profile']) for row in events if row['event'].startswith('job.')},
                         {('job.submit', 'alpha'), ('job.done', 'alpha')})

    def test_external_add_rejected_before_submission(self):
        self.login()
        result = self.submit(op='add', external=True, service='8080')
        self.assert_bad(result)
        self.assertIn('CLI', result[2]['error'])
        self.assertEqual(len(self.app.runner.jobs), 0)
        self.assertFalse(self.profiles['alpha'].calls)

    def test_services_command_missing_is_reported(self):
        self.login()
        with mock.patch.object(mod.shutil, 'which', return_value=None):
            rc, _, result = self.request('GET', '/api/services?probe=0')
        self.assertEqual(rc, 200)
        self.assertEqual(result['services'], [])
        self.assertIn('ss', result['warnings'][0])


    def test_anonymous_liveness_has_no_configuration_or_credential_data(self):
        code, _, payload = self.request('GET', '/api/health', authenticated=False)
        self.assertEqual(code, 200)
        self.assertEqual(payload, {'ok': True, 'status': 'ok'})
        self.assertEqual(self.request('POST', '/api/health', {}, authenticated=False)[0], 405)

    def test_container_local_operations_are_rejected_before_admission(self):
        self.login()
        accel = self.profiles['alpha']
        accel._container_runtime = True
        before = accel.config
        for op in ('add', 'update', 'delete'):
            code, _, payload = self.request('POST', '/api/jobs?profile=alpha', {
                'op': op, 'hostname': 'alpha.example.org', 'service': 'http://127.0.0.1:8080',
                'confirm': 'alpha.example.org'})
            self.assertEqual(code, 409)
            self.assertEqual(payload['code'], 'local_tunnel_unmanaged')
        self.assertEqual(self.request('GET', '/api/jobs')[2]['jobs'], [])
        self.assertEqual(accel.calls, [])
        self.assertEqual(accel.config, before)
        with self.assertRaises(mod.LocalTunnelUnmanaged):
            self.app.runner.submit('delete', {'hostname': 'alpha.example.org'}, 'test', profile='alpha')

    def test_container_state_is_unmanaged_and_diagnostic_result_is_partial(self):
        self.login()
        accel = self.profiles['alpha']
        accel._container_runtime = True
        with mock.patch.object(mod.subprocess, 'run', side_effect=AssertionError('host command')):
            state = self.request('GET', '/api/state?profile=alpha&fresh=1')[2]
            services = self.request('GET', '/api/services?profile=alpha')[2]
        self.assertIsNone(state['service']['active'])
        self.assertEqual(state['service']['state'], 'unmanaged')
        self.assertFalse(state['capabilities']['local_tunnel_management'])
        self.assertNotIn('service', state['errors'])
        self.assertEqual(services['services'], [])
        self.assertTrue(services['warnings'])
        submitted = self.submit('health')
        self.assertEqual(submitted[0], 200)
        result = self.wait_job(submitted[2]['job']['id'])['result']
        self.assertTrue(result['partial'])
        self.assertEqual(result['scope'], 'container-network')


if __name__ == '__main__':
    unittest.main()
