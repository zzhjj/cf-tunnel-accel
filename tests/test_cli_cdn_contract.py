"""CLI CDN and scheduler contracts, with only external I/O replaced."""
from contextlib import nullcontext, redirect_stdout
import io
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

try:  # pytest package imports and direct unittest discovery
    from .test_configuration import CLI, load_cli
except ImportError:
    from test_configuration import CLI, load_cli


ACCOUNT = 'a' * 32
ZONE_ID = '1' * 32
AUX_ZONE = 'aux.example.net'
ENTRY = f'cdn.{AUX_ZONE}'
PROBE = 'app.example.org'
CURRENT = 'current.edge.example.net'
FAST = 'fast.edge.example.net'
HEALTHY = 'healthy.edge.example.net'
CANDIDATE_IPS = {CURRENT: '192.0.2.10', FAST: '192.0.2.20', HEALTHY: '2001:db8::30'}


class CdnCliContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = {'HOME': temp.name, 'PATH': '', 'LANG': 'C.UTF-8'}
        self.patch(mock.patch.object(sys, 'path', [str(CLI.parent), *sys.path]))
        self.patch(mock.patch('urllib.request.urlopen', side_effect=AssertionError('unexpected HTTP transport')))
        self.command = self.patch(mock.patch(
            'subprocess.run', side_effect=AssertionError('unexpected command')))
        self.dns = self.patch(mock.patch('socket.getaddrinfo', side_effect=AssertionError('unexpected DNS')))
        self.tcp = self.patch(mock.patch('socket.create_connection', side_effect=AssertionError('unexpected TCP')))
        self.patch(mock.patch('builtins.input', side_effect=AssertionError('unexpected interactive prompt')))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_verify_https_rejects_failed_redirect_and_certificate_error(self):
        cli = load_cli(self.env)
        redirect = 'HTTP/1.1 301 Moved Permanently\r\nLocation: /end\r\n\r\n'
        for label, status, exit_code, expected in (
            ('final 502', 502, 0, False),
            ('certificate failure despite apparent success', 200, 60, False),
            ('successful final response', 200, 0, True),
        ):
            with self.subTest(case=label):
                self.command.reset_mock()
                self.command.side_effect = None
                self.command.return_value = subprocess.CompletedProcess(
                    ['curl'], exit_code,
                    f'{redirect}HTTP/2 {status}\r\n\r\nCFA_STATUS:{status}\n',
                    'certificate verification failed' if exit_code else '',
                )
                ok, _ = cli.verify_https(PROBE)
                self.assertIs(ok, expected)
                self.command.assert_called_once()
                command = self.command.call_args.args[0]
                self.assertEqual(command[0], 'curl')
                self.assertTrue('-L' in command or '--location' in command, 'curl must follow redirects')
                self.assertIn(f'https://{PROBE}/', command)

    def test_normalize_service_accepts_ipv6_and_network_backends(self):
        cli = load_cli(self.env)
        for raw, expected in (
            ('[::1]:8080', 'http://[::1]:8080'),
            ('https://[2001:0db8::002a]/api?view=full', 'https://[2001:db8::2a]:443/api?view=full'),
            ('192.0.2.5:8080', 'http://192.0.2.5:8080'),
            ('http://Backend.Example.NET:9000/status', 'http://backend.example.net:9000/status'),
            (':8080', 'http://127.0.0.1:8080'),
        ):
            with self.subTest(service=raw):
                self.assertEqual(cli.normalize_service(raw), expected)
        for raw in ('https://[2001:db8::1]:65536', 'http://192.0.2.5:0', 'tcp://[::1]:8080'):
            with self.subTest(invalid_service=raw):
                with self.assertRaises(cli.CFError):
                    cli.normalize_service(raw)
        self.command.assert_not_called()
        self.dns.assert_not_called()
        self.tcp.assert_not_called()

    def run_auto(self, sample_results, *, allow_dns_write=False, force=False, post_status=200):
        """Run `cdn best` with fake Cloudflare, resolver, TCP connects and curl."""
        cli = load_cli(dict(
            self.env, CF_TUNNEL_ACCEL_API_TOKEN='cdn-fixture-only',
            CF_TUNNEL_ACCEL_ACCOUNT_ID=ACCOUNT, CF_TUNNEL_ACCEL_AUX_ZONE=AUX_ZONE,
            CF_TUNNEL_ACCEL_AUX_ZONE_ID=ZONE_ID,
            CF_TUNNEL_ACCEL_TUNNEL_ID='11111111-1111-1111-1111-111111111111',
            CF_TUNNEL_ACCEL_CDN_CANDIDATES=f'{FAST},{HEALTHY}',
            CF_TUNNEL_ACCEL_PROBE_HOSTNAME=PROBE,
        ))
        zone = {'id': ZONE_ID, 'name': AUX_ZONE, 'account': {'id': ACCOUNT}, 'status': 'active'}
        cname = {'id': 'preferred-record', 'name': ENTRY, 'type': 'CNAME',
                 'content': CURRENT, 'proxied': False, 'ttl': 120}
        writes = []
        probe_calls = []
        samples_by_ip = {CANDIDATE_IPS[domain]: results for domain, results in sample_results.items()}
        per_ip = {ip: iter(results) for ip, results in samples_by_ip.items()}
        final_https = []
        unexpected = []

        def api(method, raw_path, body=None, **kwargs):
            request = urlsplit(raw_path)
            dns_path = f'/zones/{ZONE_ID}/dns_records'
            if method == 'GET':
                if request.path == '/user/tokens/verify':
                    result = {'status': 'active'}
                elif request.path == '/zones':
                    result = [zone]
                elif request.path == f'/zones/{ZONE_ID}':
                    result = zone
                elif request.path == dns_path and parse_qs(request.query).get('name') == [ENTRY]:
                    result = [dict(cname)]
                else:
                    unexpected.append((method, request.path))
                    raise AssertionError('unexpected Cloudflare read')
            else:
                writes.append((method, request.path, body))
                if not allow_dns_write or (method, request.path) != ('PUT', f'{dns_path}/{cname["id"]}'):
                    raise AssertionError('unexpected Cloudflare mutation')
                cname.update(body)
                result = dict(cname)
            return {'success': True, 'result': result}

        def lookup(host, port, *args, **kwargs):
            ip = CANDIDATE_IPS[host]
            if ':' in ip:
                return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port, 0, 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))]

        def command_fixture(command, **kwargs):
            if command[0] == 'ping':
                return subprocess.CompletedProcess(command, 0, 'time=1.0 ms\n0% packet loss\n', '')
            if command[0] != 'curl':
                unexpected.append(command[0])
                raise AssertionError('unexpected subprocess')
            if '--resolve' not in command:
                final_https.append(command[-1])
                return subprocess.CompletedProcess(command, 0, f'HTTP/2 {post_status}\n\nCFA_STATUS:{post_status}\n', '')
            resolve = command[command.index('--resolve') + 1]
            host, port, raw_ip = resolve.split(':', 2)
            ip = raw_ip.strip('[]')
            probe_calls.append((host, port, raw_ip, command[-1]))
            code, output = next(per_ip[ip])
            return subprocess.CompletedProcess(command, code, output.strip() + f' {ip} https://{PROBE}/', '')

        self.dns.side_effect = lookup
        self.tcp.side_effect = lambda *args, **kwargs: nullcontext()
        self.command.side_effect = command_fixture
        # A functioning fake API makes its writes visible on the next read, so
        # the real confirmation path should never have to sleep or retry.
        with mock.patch.object(cli, 'api', side_effect=api), \
                mock.patch('time.sleep', side_effect=AssertionError('unexpected probe or API retry')), \
                redirect_stdout(io.StringIO()):
            rc = cli.main([str(CLI), 'cdn', 'best', *(['--force'] if force else [])])

        self.assertEqual(unexpected, [])
        self.assertEqual({call.args[0] for call in self.dns.call_args_list}, set(sample_results))
        self.assertEqual({call.args[0] for call in self.tcp.call_args_list},
                         {(CANDIDATE_IPS[domain], 443) for domain in sample_results})
        self.assertCountEqual(probe_calls, [
            (PROBE, '443', f'[{ip}]' if ':' in ip else ip, f'https://{PROBE}/')
            for ip, samples in samples_by_ip.items()
            for _ in samples
        ])
        return rc, writes, cname, final_https

    def test_cdn_best_does_not_set_any_target_when_all_candidates_are_tcp_only(self):
        tls_failure = (60, '200 0.010 0.020\n')
        rc, writes, cname, final_https = self.run_auto(
            {domain: [tls_failure] * 3 for domain in CANDIDATE_IPS}, force=True,
        )
        self.assertEqual(writes, [], 'even --force must not switch with TCP-only evidence')
        self.assertEqual(cname['content'], CURRENT)
        self.assertEqual(final_https, [])
        self.assertNotEqual(rc, 0)

    def test_cdn_best_prefers_slow_reliable_https_over_fast_low_availability(self):
        rc, writes, cname, final_https = self.run_auto({
            CURRENT: [(0, '200 0.500 0.550\n')] * 3,
            FAST: [(0, '200 0.010 0.020\n')] * 2 + [(0, '502 0.010 0.020\n')],
            HEALTHY: [(0, '200 0.200 0.250\n')] * 3,
        }, allow_dns_write=True)
        self.assertEqual([(method, body['content']) for method, _, body in writes], [('PUT', HEALTHY)])
        self.assertEqual(cname['content'], HEALTHY)
        self.assertEqual(final_https, [f'https://{PROBE}/'])
        self.assertEqual(rc, 0)

    def test_real_cli_restores_previous_dns_when_post_switch_business_fails(self):
        rc, writes, cname, final_https = self.run_auto({
            CURRENT: [(0, '200 0.500 0.550')] * 3,
            FAST: [(0, '200 0.050 0.060')] * 3,
            HEALTHY: [(0, '200 0.200 0.250')] * 3,
        }, allow_dns_write=True, post_status=502)
        self.assertEqual([(method, body['content']) for method, _, body in writes], [('PUT', FAST), ('PUT', CURRENT)])
        self.assertEqual(cname['content'], CURRENT)
        self.assertNotEqual(rc, 0)
        self.assertEqual(final_https, [f'https://{PROBE}/', f'https://{PROBE}/'])


    def test_schedule_preserves_selected_env_and_cli_and_binds_custom_service(self):
        env_file = self.home / 'site settings.env'
        env_text = f'CF_TUNNEL_ACCEL_AUX_ZONE={AUX_ZONE}\nCF_TUNNEL_ACCEL_PROBE_HOSTNAME={PROBE}\n'
        env_file.write_text(env_text, encoding='utf-8')
        service = self.home / 'units' / 'team-cdn-optimizer.service'
        timer = service.with_name('team-cdn-nightly.timer')
        fixture_token = 'schedule-credential-fixture-only'
        cli = load_cli(dict(
            self.env, CF_TUNNEL_ACCEL_ENV_FILE=str(env_file), CF_API_TOKEN=fixture_token,
            CF_TUNNEL_ACCEL_CDN_SERVICE_UNIT=str(service), CF_TUNNEL_ACCEL_CDN_TIMER_UNIT=str(timer),
        ))
        self.command.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '', '')
        with mock.patch.object(cli, 'api', side_effect=AssertionError('schedule generation must be local')) as api, \
                redirect_stdout(io.StringIO()):
            cli.install_cdn_schedule(interval_hours=9)
        api.assert_not_called()
        self.assertEqual([call.args[0] for call in self.command.call_args_list], [
            ['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', timer.name],
        ])
        self.assertEqual(env_file.read_text(encoding='utf-8'), env_text)
        service_text = service.read_text(encoding='utf-8')
        timer_text = timer.read_text(encoding='utf-8')
        self.assertTrue(all(fixture_token not in text for text in (service_text, timer_text)),
                        'generated units must not embed credentials')
        self.assertIn(f'Unit={service.name}', timer_text.splitlines())
        self.assertIn('OnUnitActiveSec=9h', timer_text.splitlines())

        directives = [line.split('=', 1) for line in service_text.splitlines() if '=' in line]
        workdirs = [shlex.split(value) for key, value in directives if key == 'WorkingDirectory']
        self.assertNotIn(['/root'], workdirs, 'the service must not require the root home as its cwd')
        exec_start = next((shlex.split(value) for key, value in directives if key == 'ExecStart'), [])
        self.assertIn(str(CLI.resolve()), exec_start)
        self.assertEqual(exec_start[-2:], ['cdn', 'best'])
        assignment = f'CF_TUNNEL_ACCEL_ENV_FILE={env_file}'
        self.assertTrue(
            assignment in exec_start or any(
                (key == 'Environment' and assignment in shlex.split(value))
                or (key == 'EnvironmentFile' and any(
                    path.lstrip('-') == str(env_file) for path in shlex.split(value)
                ))
                for key, value in directives
            ),
            'the scheduled process must receive the explicitly selected env file',
        )


if __name__ == '__main__':
    unittest.main()
