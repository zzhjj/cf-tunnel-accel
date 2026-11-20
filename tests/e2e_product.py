"""Installed-package E2E: real CLI + real UI + Chromium, all cloud/OS writes local.

Run: python3 tests/e2e_product.py
Cloudflare is an independent loopback HTTP contract; systemctl/cloudflared/curl
and candidate DNS are explicit external-command fixtures. No operator config is
read, and no actual Tunnel, DNS record, or systemd service is changed.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'a' * 32
ZONE = 'b' * 32
TUNNEL = '11111111-1111-1111-1111-111111111111'


class CloudContract:
    def __init__(self, credential):
        self.credential = credential
        self.requests = []
        self.bad_auth = 0
        self.record = {'id': 'entry-record', 'type': 'CNAME', 'name': 'cdn.example.net',
                       'content': 'slow.edge.example', 'ttl': 120, 'proxied': False}
        self.records = {self.record['id']: self.record}
        for ident in ('alpha', 'beta'):
            self.records[ident] = {'id': ident, 'name': ident + '.example.net', 'type': 'CNAME',
                                   'content': 'cdn.example.net', 'proxied': False, 'ttl': 120}
        self.custom = {ident: {'id': ident, 'hostname': ident + '.example.net',
                             'status': 'active', 'ssl': {'status': 'active'}} for ident in ('alpha', 'beta')}
        self.fallback = {'origin': 'origin.example.net', 'status': 'active'}

    def handler(self):
        contract = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def route(self):
                parsed = urlsplit(self.path)
                contract.requests.append((self.command, parsed.path))
                if parsed.path == '/backend':
                    self.respond(200, {'ok': True})
                    return
                if self.headers.get('Authorization') != 'Bearer ' + contract.credential:
                    contract.bad_auth += 1
                    self.respond(403, {'success': False, 'errors': [{'message': 'fixture credential mismatch'}]})
                    return
                zone = {'id': ZONE, 'name': 'example.net', 'status': 'active',
                        'paused': False, 'type': 'full', 'plan': {'name': 'Free'},
                        'account': {'id': ACCOUNT, 'name': 'Fixture account'}}
                if self.command == 'GET':
                    if parsed.path == '/user/tokens/verify':
                        result = {'status': 'active'}
                    elif parsed.path == '/zones':
                        result = [zone]
                    elif parsed.path == '/zones/' + ZONE:
                        result = zone
                    elif parsed.path == f'/zones/{ZONE}/custom_hostnames':
                        result = list(contract.custom.values())
                    elif parsed.path == f'/zones/{ZONE}/custom_hostnames/fallback_origin':
                        result = dict(contract.fallback)
                    elif parsed.path.startswith(f'/zones/{ZONE}/custom_hostnames/'):
                        result = contract.custom[parsed.path.rsplit('/', 1)[1]]
                    elif parsed.path == f'/zones/{ZONE}/dns_records':
                        name = parse_qs(parsed.query).get('name', [''])[0]
                        result = [dict(r) for r in contract.records.values() if not name or r['name'] == name]
                    else:
                        self.respond(404, {'success': False, 'errors': [{'message': 'contract route missing'}]})
                        return
                elif self.command in ('PUT', 'POST', 'PATCH', 'DELETE'):
                    body = {}
                    if self.headers.get('Content-Length'):
                        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if parsed.path == f'/zones/{ZONE}/custom_hostnames/fallback_origin':
                        contract.fallback.update(body)
                        result = contract.fallback
                    elif parsed.path == f'/zones/{ZONE}/custom_hostnames' and self.command == 'POST':
                        ident = body['hostname'].split('.')[0]
                        result = {**body, 'id': ident, 'status': 'active', 'ssl': {'status': 'active'}}
                        contract.custom[ident] = result
                    elif parsed.path.startswith(f'/zones/{ZONE}/custom_hostnames/') and self.command == 'DELETE':
                        result = contract.custom.pop(parsed.path.rsplit('/', 1)[1])
                    elif parsed.path == f'/zones/{ZONE}/dns_records' and self.command == 'POST':
                        result = {**body, 'id': 'record-' + str(len(contract.records))}
                        contract.records[result['id']] = result
                    elif parsed.path.startswith(f'/zones/{ZONE}/dns_records/'):
                        ident = parsed.path.rsplit('/', 1)[1]
                        if self.command == 'DELETE':
                            result = contract.records.pop(ident)
                        else:
                            contract.records[ident].update(body)
                            result = dict(contract.records[ident])
                    else:
                        self.respond(405, {'success': False, 'errors': [{'message': 'unexpected cloud mutation'}]})
                        return
                else:
                    self.respond(405, {'success': False, 'errors': [{'message': 'unexpected cloud mutation'}]})
                    return
                self.respond(200, {'success': True, 'result': result, 'result_info': {'total_pages': 1}})

            def respond(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = route
            do_PUT = route
            do_POST = route
            do_DELETE = route
            do_PATCH = route
        return Handler


@contextmanager
def http_server(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class InstalledProductE2E(unittest.TestCase):
    def test_clean_install_init_ui_real_cli_mutation_and_cdn(self):
        with tempfile.TemporaryDirectory(prefix='cfa-product-e2e-') as td:
            base = Path(td)
            home, tools, overrides = (base / name for name in ('home', 'tools', 'overrides'))
            for folder in (home, tools, overrides):
                folder.mkdir()
            events = base / 'commands.jsonl'
            pids = base / 'pids.json'
            pids.write_text(json.dumps({'alpha.service': 101, 'beta.service': 201}))
            credential = 'fixture-e2e-local-not-a-real-token'
            token_file = base / 'cloud-token'
            token_file.write_text(credential)
            token_file.chmod(0o600)
            env = {'HOME': str(home), 'PATH': str(tools) + ':/usr/bin:/bin',
                   'LANG': 'C.UTF-8', 'PYTHONPATH': str(overrides),
                   'CFA_E2E_COMMANDS': str(events), 'CFA_E2E_PIDS': str(pids),
                   'CFA_E2E_CLI_ROOT': str(ROOT)}
            executable = f'#!{sys.executable}\n'
            systemctl = executable + '''import json, os, sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CFA_E2E_COMMANDS'],'a') as out: out.write(json.dumps(['systemctl']+args)+'\\n')
p=Path(os.environ['CFA_E2E_PIDS']); state=json.loads(p.read_text())
if args[0]=='show': print(state.get(args[1],0))
elif args[0]=='restart': state[args[1]]=state.get(args[1],0)+1; p.write_text(json.dumps(state))
elif args[0]=='is-active': print('active')
'''
            curl = executable + '''import sys
args=sys.argv[1:]
if '--resolve' in args:
    target=args[args.index('--resolve')+1]; ip=target.split(':',2)[2].strip('[]')
    host=target.split(':',1)[0]; fast=ip.endswith('.20')
    print('200', '0.05' if fast else '0.2', '0.06' if fast else '0.25', ip, 'https://'+host+'/')
else: print('HTTP/2 200\\n\\nCFA_STATUS:200')
'''
            cloudflared = executable + '''import sys
from pathlib import Path
if '--version' in sys.argv: print('cloudflared version 2026.7.1'); sys.exit(0)
if '--config' in sys.argv:
    p=Path(sys.argv[sys.argv.index('--config')+1]); text=p.read_text()
    if 'ingress:' not in text or 'http_status:404' not in text: sys.exit(2)
print('fixture cloudflared config valid')
'''
            for name, code in [('systemctl', systemctl), ('cloudflared', cloudflared), ('curl', curl),
                               ('ss', '#!/bin/sh\nprintf "State Recv-Q Send-Q Local Address:Port Peer Address:Port\\n"\n')]:
                if name == 'ss':
                    (tools / 'ping').write_text('#!/bin/sh\nprintf "time=1.0 ms\\n0%% packet loss\\n"\n')
                    (tools / 'ping').chmod(0o755)
                path = tools / name
                path.write_text(code)
                path.chmod(0o755)
            (overrides / 'sitecustomize.py').write_text('''import socket
original=socket.getaddrinfo
def lookup(host, port, *args, **kwargs):
    if host in ('slow.edge.example','fast.edge.example'):
        ip='192.0.2.10' if host.startswith('slow') else '192.0.2.20'
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(ip,port))]
    return original(host,port,*args,**kwargs)
socket.getaddrinfo=lookup
class LocalTCP:
    def __enter__(self): return self
    def __exit__(self,*args): pass
original_connect=socket.create_connection
def connect(address,*args,**kwargs):
    if address[0] in ('192.0.2.10','192.0.2.20'): return LocalTCP()
    return original_connect(address,*args,**kwargs)
socket.create_connection=connect
''')
            # The installer and installed launchers are real, not module doubles.
            install = subprocess.run(['bash', str(ROOT / 'install.sh'), '--user', '--no-deps'],
                                     env=env, cwd=base, capture_output=True, text=True, timeout=30)
            self.assertEqual(install.returncode, 0, install.stderr)
            cli = home / '.local/bin/cf-tunnel-accel'
            self.assertTrue(cli.is_file())
            package = home / '.local/share/cf-tunnel-accel'
            self.assertTrue((package / 'cfa_ingress.py').is_file())
            configs = {}
            for ident, port in [('alpha', 8081), ('beta', 8082)]:
                config = base / (ident + '.yml')
                config.write_text(f'tunnel: {TUNNEL}\ningress:\n  - hostname: {ident}.example.net\n'
                                  f'    service: http://127.0.0.1:{port}\n'
                                  '    originRequest:\n      connectTimeout: 10s\n  - service: http_status:404\n')
                configs[ident] = config
            # The installed CLI must support the documented one-answer first run.
            default_tunnel = home / '.cloudflared/config.yml'
            default_tunnel.parent.mkdir()
            default_tunnel.write_text(configs['alpha'].read_text())
            default_token = home / '.config/cf-tunnel-accel/token'
            default_token.parent.mkdir(parents=True, exist_ok=True)
            default_token.write_text(credential)
            default_token.chmod(0o600)
            minimal = subprocess.run([str(cli), 'init'], input='example.net\n\n',
                                     env=env, cwd=base, capture_output=True, text=True, timeout=15)
            self.assertEqual(minimal.returncode, 0, minimal.stdout + minimal.stderr)
            minimal_env = home / '.config/cf-tunnel-accel/env'
            minimal_text = minimal_env.read_text()
            self.assertIn('CF_TUNNEL_ACCEL_AUX_ZONE=example.net', minimal_text)
            self.assertIn('CF_TUNNEL_ACCEL_SERVICE=cloudflared.service', minimal_text)
            self.assertIn('CF_TUNNEL_ACCEL_CONFIG=' + str(default_tunnel), minimal_text)
            self.assertIn('CF_TUNNEL_ACCEL_TUNNEL_ID=' + TUNNEL, minimal_text)
            self.assertNotIn('CF_TUNNEL_ACCEL_ACCOUNT_ID=', minimal_text)
            self.assertNotIn('CF_TUNNEL_ACCEL_AUX_ZONE_ID=', minimal_text)
            cloud = CloudContract(credential)
            with http_server(cloud.handler()) as external:
                origin = f'http://127.0.0.1:{external.server_port}'
                profiles = []
                for ident in ('alpha', 'beta'):
                    settings = base / (ident + '.env')
                    init = subprocess.run([str(cli), 'init', '--non-interactive', '--output', str(settings),
                        '--config', str(configs[ident]), '--service', ident + '.service',
                        '--aux-zone', 'example.net', '--aux-zone-id', ZONE, '--account-id', ACCOUNT,
                        '--preferred-target', 'slow.edge.example', '--cdn-candidates', 'slow.edge.example,fast.edge.example',
                        '--probe-hostname', ident + '.example.net',
                        '--token-file', str(token_file)], env=env, cwd=base, capture_output=True, text=True, timeout=15)
                    self.assertEqual(init.returncode, 0, init.stderr)
                    with settings.open('a') as out:
                        out.write(f'CF_TUNNEL_ACCEL_API_BASE={origin}\nCF_TUNNEL_ACCEL_CLOUDFLARED={tools / "cloudflared"}\n')
                    profiles.append({'id': ident, 'name': ident.title(), 'env_file': str(settings)})
                registry = base / 'profiles.json'
                registry.write_text(json.dumps({'profiles': profiles}))
                ui_env = dict(env, CF_TUNNEL_ACCEL_UI_DIR=str(base / 'ui-state'))
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    port = listener.getsockname()[1]
                command = [sys.executable, str(package / 'ui/server.py'), '--module', str(package / 'cf-tunnel-accel'),
                           '--profiles', str(registry), '--port', str(port)]
                server_log = (base / 'ui-server.log').open('w')
                process = subprocess.Popen(command, env=ui_env, cwd=base, stdout=server_log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail('UI startup failed; bootstrap log withheld because it may contain a login key')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                                break
                        except OSError:
                            time.sleep(.1)
                    management = (base / 'ui-state/ui-token').read_text().strip()
                    alpha_before = configs['alpha'].read_bytes()
                    beta_before = configs['beta'].read_bytes()
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
                        try:
                            page = browser.new_page(viewport={'width': 1440, 'height': 950})
                            errors = []
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            page.goto(f'http://127.0.0.1:{port}/')
                            page.locator('#token').fill(management)
                            page.locator('#token').press('Enter')
                            page.wait_for_timeout(1500)
                            print('UI baseline notices:', page.locator('#profile-status').inner_text(), page.locator('#routes-notice').inner_text())
                            expect(page.locator('#route-grid')).to_contain_text('alpha.example.net')
                            page.locator('#profile-select').select_option('beta')
                            expect(page.locator('#route-grid')).to_contain_text('beta.example.net')
                            page.locator('[data-host="beta.example.net"] [data-route-action="edit"]').click()
                            page.locator('#f-service').fill('http://127.0.0.1:9099/api')
                            page.locator('#modal-ok').click()
                            expect(page.locator('#task-status')).to_contain_text('执行结束', timeout=15000)
                            expect(page.locator('#route-grid')).to_contain_text('http://127.0.0.1:9099/api')
                            self.assertEqual(configs['alpha'].read_bytes(), alpha_before)
                            self.assertNotEqual(configs['beta'].read_bytes(), beta_before)
                            self.assertIn('connectTimeout: 10s', configs['beta'].read_text())
                            commands = [json.loads(line) for line in events.read_text().splitlines()]
                            self.assertEqual([c for c in commands if c[:2] == ['systemctl', 'restart']],
                                             [['systemctl', 'restart', 'beta.service']])
                            page.locator('.nav a[href="#cdn"]').click()
                            page.locator('#cdn-test').click()
                            expect(page.locator('#task-status')).to_contain_text('执行结束', timeout=20000)
                            expect(page.locator('#cdn-candidates')).to_contain_text('业务验证通过')
                            self.assertFalse(any(method != 'GET' for method, _ in cloud.requests))
                            page.locator('#cdn-best').click()
                            page.locator('#modal-ok').click()
                            expect(page.locator('#task-title')).to_contain_text('cdn-best')
                            expect(page.locator('#task-status')).to_contain_text('执行结束', timeout=20000)
                            self.assertEqual(cloud.record['content'], 'fast.edge.example')
                            page.locator('.nav a[href="#routes"]').click()
                            page.locator('#route-add').click()
                            page.locator('#f-host').fill('created.example.net')
                            page.locator('#f-service').fill(origin + '/backend')
                            page.locator('#modal-ok').click()
                            expect(page.locator('#task-title')).to_contain_text('add')
                            expect(page.locator('#task-status')).to_contain_text('执行结束', timeout=25000)
                            expect(page.locator('#route-grid')).to_contain_text('created.example.net')
                            self.assertIn('created', cloud.custom)
                            self.assertTrue(any(r['name'] == 'created.example.net' and r['content'] == 'cdn.example.net' for r in cloud.records.values()))
                            page.locator('[data-host="created.example.net"] [data-route-action="delete"]').click()
                            page.locator('#f-confirm').fill('created.example.net')
                            page.locator('#modal-ok').click()
                            expect(page.locator('#task-title')).to_contain_text('delete')
                            expect(page.locator('#task-status')).to_contain_text('执行结束', timeout=15000)
                            expect(page.locator('#route-grid')).not_to_contain_text('created.example.net')
                            self.assertNotIn('created', cloud.custom)
                            self.assertFalse(any(r['name'] == 'created.example.net' for r in cloud.records.values()))
                            self.assertEqual(configs['alpha'].read_bytes(), alpha_before)
                            expect(page.locator('#route-grid')).to_contain_text('9099/api')
                            page.screenshot(path='/tmp/cfa-product-desktop.png', full_page=True)
                            page.set_viewport_size({'width': 375, 'height': 900})
                            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                            page.screenshot(path='/tmp/cfa-product-mobile.png', full_page=True)
                            self.assertEqual(errors, [])
                            self.assertEqual(cloud.bad_auth, 0)
                            self.assertTrue(any(method == 'PUT' for method, _ in cloud.requests))
                            print('E2E verified: clean install -> init -> real auth/profile -> actual CLI config update -> '
                                  'only beta restart -> business Host/SNI benchmark (read-only) -> confirmed CDN switch')
                        finally:
                            browser.close()
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    server_log.close()
                self.assertEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
