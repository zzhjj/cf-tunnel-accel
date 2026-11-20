"""Real Chromium + loopback contract server. No cloud, DNS, subprocess or live service writes.

Run: python3 ui/tests/ui_e2e.py
Requires the already-installed playwright Python package and Chromium.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'ui' / 'static'


class Fixture:
    def __init__(self):
        self.auth = True
        self.session_get_delay = 0.0
        self.requests = []
        self.posts = []
        self.errors = {}
        self.delays = {}
        self.jobs = {}
        self.reject_post = False
        self.login_posts = 0
        self.job_counter = 0
        self.job_reads = []
        self.offset = 0
        self.next_cursor = 2
        self.state_errors = False
        self.environment_ready = True
        self.hang = set()
        self.submit_unknown = False
        self.state_verified = True
        self.services_warnings = []
        self.omit_submit_job = False
        self.capabilities = None

    def job(self, ident='restored', op='health', profile='beta', status='running'):
        return dict(id=ident, op=op, profile=profile, status=status,
                    created=1800000000, ended=None, offset=self.offset,
                    next=self.next_cursor, elapsed=2.5,
                    lines=[dict(t=0.1, m='fixture log <not html>')],
                    error=None, result=None)

    def state(self, profile):
        return dict(ok=True, version='fixture',
                    capabilities=self.capabilities,
                    service=dict(name=profile + '.service', active=True),
                    preferred=dict(entry='cdn.example.org', target=None if not self.state_verified else 'edge.example.net', verified=self.state_verified),
                    counts=dict(routes=2, zones=1, saas=1),
                    config_path='/fixture/' + profile + '.yml',
                    readiness=dict(ready=not self.state_errors),
                    errors={'cloudflare': 'API permission denied'} if self.state_errors else [],
                    updated_at=1800000000,
                    zones=[dict(name='example.org', status='active')],
                    routes=[dict(hostname=profile + '.example.org', service='https://127.0.0.1:8443',
                                 zone='example.org', no_tls_verify=False, external=False,
                                 saas=dict(status='active', ssl='active')),
                            dict(hostname='external.example.com', service='http://127.0.0.1:8080',
                                 zone=None, no_tls_verify=False, external=True, saas=None)])

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, data=None, headers=None):
                raw = b'' if data is None else json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                u = urlsplit(self.path)
                if not u.path.startswith('/api/'):
                    file = STATIC / ('index.html' if u.path == '/' else u.path.lstrip('/'))
                    if file not in [STATIC / n for n in ['index.html', 'app.js', 'style.css']]:
                        return self.send(404, {'ok': False})
                    raw = file.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', {'.html':'text/html', '.js':'text/javascript', '.css':'text/css'}[file.suffix])
                    self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'")
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                profile = parse_qs(u.query).get('profile', [''])[0]
                fixture.requests.append(('GET', u.path, profile, self.path))
                if u.path == '/api/session':
                    authenticated = fixture.auth
                    if fixture.session_get_delay:
                        time.sleep(fixture.session_get_delay)
                    return self.send(200, dict(ok=True, authenticated=authenticated))
                if not fixture.auth:
                    return self.send(401, dict(ok=False, error='session expired'))
                delay = fixture.delays.get((u.path, profile), 0)
                # Capture old profile state before waiting: genuine stale response race.
                state = fixture.state(profile) if u.path == '/api/state' else None
                if delay:
                    time.sleep(delay)
                if u.path in fixture.hang:
                    time.sleep(14)
                    return self.send(200, state or {'ok':True})
                code = fixture.errors.get(u.path)
                if code:
                    return self.send(code, dict(ok=False, error='fixture error', retry_after=7))
                if u.path == '/api/profiles':
                    return self.send(200, dict(ok=True, default='alpha', profiles=[dict(id=p, name=p.upper()) for p in ['alpha','beta']]))
                if u.path == '/api/state':
                    return self.send(200, state)
                if u.path == '/api/services':
                    rows = [dict(port=8080, addr='127.0.0.1', friendly='fixture HTTP', mapped=[], cmd='fixture process'),
                            dict(port=8443, addr='::1', friendly='fixture TLS', mapped=[profile+'.example.org'], probe=dict(ok=False,detail='fixture 502'))]
                    if parse_qs(u.query).get('probe') == ['1']:
                        rows[0]['probe'] = dict(ok=True,detail='HTTP 200')
                    return self.send(200,dict(ok=True,services=rows,warnings=fixture.services_warnings))
                if u.path == '/api/cdn':
                    return self.send(200, dict(ok=True,cdn=dict(entry='cdn.example.org',target='edge.example.net',verified=True,
                        candidates=[dict(domain='edge.example.net'),dict(domain='next.example.net')], min_improvement=15)))
                if u.path == '/api/environment':
                    return self.send(200,dict(ok=True,environment=dict(ready=fixture.environment_ready,
                        capabilities=fixture.capabilities,
                        missing=[] if fixture.environment_ready else [dict(name='curl',reason='missing',recovery='install curl')],
                        config=dict(path='/fixture/config.yml',account=dict(value='fixture account',source='profile env')),
                        dependencies=dict(curl=True,systemd=False),credentials=dict(configured=True,source='profile token file',token='MUST_NOT_RENDER'))))
                if u.path == '/api/audit':
                    return self.send(200, dict(ok=True,rows=[dict(event='job.done',profile='beta',op='health',ts='2026-11-20T10:00:00Z')]))
                if u.path == '/api/jobs':
                    return self.send(200,dict(ok=True,jobs=list(fixture.jobs.values()),active=next((j['id'] for j in fixture.jobs.values() if j['status']=='running'),None)))
                if u.path.startswith('/api/jobs/'):
                    ident = u.path.rsplit('/',1)[1]
                    since = int(parse_qs(u.query).get('since',['0'])[0])
                    fixture.job_reads.append((ident,since))
                    if ident not in fixture.jobs:
                        return self.send(404,dict(ok=False,error='task not found'))
                    job = dict(fixture.jobs[ident])
                    job['lines'] = [] if since >= job['next'] else job['lines']
                    return self.send(200,dict(ok=True,job=job))
                if u.path.startswith('/api/host/'):
                    return self.send(200,dict(ok=True,host=dict(service='https://127.0.0.1:8443',dns=dict(cname='cdn.example.org'),https=dict(ok=False,detail='fixture TLS failed'),probe=dict(ok=True,detail='HTTP 200'))))
                self.send(404,dict(ok=False,error='missing endpoint'))

            def do_POST(self):
                u = urlsplit(self.path)
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if u.path == '/api/session':
                    fixture.login_posts += 1
                    if data.get('token') != 'fixture-access':
                        return self.send(401,dict(ok=False,error='invalid fixture credential'))
                    fixture.auth = True
                    return self.send(200,dict(ok=True,csrf='fixture-csrf'),{'Set-Cookie':'arc_csrf=fixture-csrf; Path=/; SameSite=Strict'})
                if not fixture.auth:
                    return self.send(401,dict(ok=False,error='session expired'))
                if u.path == '/api/jobs':
                    profile = parse_qs(u.query).get('profile',[''])[0]
                    fixture.posts.append((profile,data,self.headers.get('X-CSRF')))
                    if fixture.reject_post:
                        return self.send(400,dict(ok=False,error='fixture rejected: retain your input'))
                    fixture.job_counter += 1
                    job = fixture.job('job-'+str(fixture.job_counter),data['op'],profile)
                    fixture.jobs[job['id']] = job
                    if fixture.submit_unknown:
                        self.connection.shutdown(2)
                        return
                    return self.send(200,dict(ok=True) if fixture.omit_submit_job else dict(ok=True,job=job))
                self.send(404,dict(ok=False))

            def do_DELETE(self):
                fixture.auth = False
                self.send(204)
        return Handler


@contextmanager
def running_fixture():
    fixture = Fixture()
    server = ThreadingHTTPServer(('127.0.0.1',0),fixture.handler())
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield fixture, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        chromium = os.environ.get('UI_CHROMIUM')
        if not chromium:
            candidates = sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux/chrome'))
            chromium = str(candidates[-1]) if candidates else None
        cls.browser = cls.pw.chromium.launch(executable_path=chromium,headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.fixture_context = running_fixture()
        self.f, self.origin = self.fixture_context.__enter__()
        self.context = self.browser.new_context(viewport={'width':1440,'height':1000})
        self.page = self.context.new_page()
        self.js_errors = []
        self.external = []
        self.page.on('pageerror',lambda e:self.js_errors.append(str(e)))
        self.page.on('request',lambda r:self.external.append(r.url) if not r.url.startswith(self.origin) else None)

    def tearDown(self):
        self.context.close()
        self.fixture_context.__exit__(None,None,None)
        self.assertEqual(self.js_errors,[])
        self.assertEqual(self.external,[])

    def open(self):
        self.page.goto(self.origin)
        expect(self.page.locator('#route-grid')).to_contain_text('alpha.example.org')
        expect(self.page.locator('#route-add')).to_be_enabled()

    def nav(self,id):
        self.page.locator(f'.nav a[href="#{id}"]').click()
        expect(self.page.locator('#'+id)).to_be_visible()

    def finish(self,op=None,ok=True,status='done'):
        job = next(reversed(self.f.jobs.values()))
        job.update(status=status,ended=1800000010,result={'ok':ok} if (op or job['op'])=='health' else {'changed':True},next=job['next']+1)
        return job

    def test_container_capabilities_disable_only_local_controls_and_mark_partial_health(self):
        self.f.capabilities = dict(runtime='container', local_tunnel_management=False,
                                   service_discovery=False, health_scope='container-network')
        self.page.goto(self.origin)
        route = self.page.locator('[data-host="alpha.example.org"]')
        expect(route).to_be_visible()
        expect(self.page.locator('#route-add')).to_be_disabled()
        expect(route.locator('[data-route-action="edit"]')).to_be_disabled()
        expect(route.locator('[data-route-action="delete"]')).to_be_disabled()
        expect(route.locator('[data-route-action="switch"]')).to_be_enabled()
        expect(route.locator('[data-route-action="health"]')).to_be_enabled()
        expect(self.page.locator('#state-summary')).to_contain_text('未管理（容器）')
        self.nav('services')
        expect(self.page.locator('#listener-list')).to_contain_text('不发现宿主监听')
        self.assertEqual(self.page.locator('[data-bind]').count(), 0)
        self.nav('cdn')
        expect(self.page.locator('#cdn-test')).to_be_enabled()
        expect(self.page.locator('#cdn-best')).to_be_enabled()
        self.nav('environment')
        expect(self.page.locator('#environment-content')).to_contain_text('container-network')
        self.nav('routes')
        route.locator('[data-route-action="health"]').click()
        expect(self.page.locator('#task-title')).to_contain_text('health')
        job = self.finish(op='health')
        job['result'].update(partial=True, scope='container-network',
                             unchecked=['host-systemd','host-connector-config'])
        self.nav('activity')
        expect(self.page.locator('#job-detail-notice')).to_contain_text('有限范围检查', timeout=8000)
        expect(self.page.locator('#job-detail-notice')).to_contain_text('不代表完整健康')
        expect(self.page.locator('#job-detail-notice')).to_contain_text('host-systemd')


    def test_default_navigation_independent_failures_and_environment(self):
        self.f.errors['/api/audit'] = 500
        self.open()
        expect(self.page.locator('#routes')).to_be_visible()
        expect(self.page.locator('#route-grid')).to_contain_text('校验证书（不等同于异常）')
        expect(self.page.locator('#audit-notice')).to_contain_text('读取失败')
        self.nav('services')
        expect(self.page.locator('#listener-list')).to_contain_text('未探测')
        self.page.locator('#services-refresh').click()
        expect(self.page.locator('#listener-list')).to_contain_text('探测通过')
        self.nav('environment')
        expect(self.page.locator('#environment-content')).to_contain_text('profile token file')
        expect(self.page.locator('#environment-content')).not_to_contain_text('MUST_NOT_RENDER')
        self.f.environment_ready = False
        self.page.locator('#environment-refresh').click()
        expect(self.page.locator('#environment-content')).to_contain_text('install curl')
        self.nav('routes')
        expect(self.page.locator('#route-add')).to_be_disabled()

    def test_native_form_single_submit_inline_retention_and_keyboard(self):
        self.open()
        self.page.locator('#route-add').click()
        expect(self.page.locator('#f-host')).to_be_focused()
        self.page.locator('#f-host').fill('new.example.org')
        self.page.locator('#f-service').fill('127.0.0.1:8080')
        self.f.reject_post = True
        self.page.locator('#f-service').press('Enter')
        expect(self.page.locator('#modal-error')).to_contain_text('fixture rejected')
        self.assertEqual(len(self.f.posts),1)
        expect(self.page.locator('#f-host')).to_have_value('new.example.org')
        expect(self.page.locator('#f-service')).to_have_value('127.0.0.1:8080')
        for _ in range(12):
            self.page.keyboard.press('Tab')
            self.assertTrue(self.page.evaluate('document.activeElement.closest("dialog")?.id === "modal"'))
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#modal')).not_to_be_visible()
        expect(self.page.locator('#route-add')).to_be_focused()
        self.f.reject_post = False
        self.page.locator('#route-add').click()
        self.page.locator('#f-host').fill('new.example.org')
        self.page.locator('#f-service').fill('8080')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(len(self.f.posts),2)
        profile,payload,_ = self.f.posts[-1]
        self.assertEqual(profile,'alpha')
        self.assertEqual(payload,dict(op='add',hostname='new.example.org',service='http://127.0.0.1:8080',no_tls_verify=False))

    def test_auth_password_clear_401_gate_and_logout(self):
        self.f.auth = False
        self.page.goto(self.origin)
        expect(self.page.locator('#gate')).to_be_visible()
        self.page.locator('#token').fill('wrong')
        self.page.locator('#token').press('Enter')
        expect(self.page.locator('#gate-msg')).to_contain_text('invalid fixture credential')
        expect(self.page.locator('#token')).to_have_value('')
        self.page.locator('#token').fill('fixture-access')
        self.page.locator('#token').press('Enter')
        expect(self.page.locator('#gate')).not_to_be_visible()
        expect(self.page.locator('#route-add')).to_be_enabled()
        self.assertEqual(self.f.login_posts,2)
        self.assertNotIn('fixture-access',self.page.evaluate('JSON.stringify([localStorage,sessionStorage])'))
        self.f.auth = False
        self.page.locator('#routes-refresh').click()
        expect(self.page.locator('#gate')).to_be_visible()
        self.page.locator('#token').fill('fixture-access')
        self.page.locator('#gate-submit').click()
        expect(self.page.locator('#gate')).not_to_be_visible()
        self.page.locator('#logout').click()
        expect(self.page.locator('#gate')).to_be_visible()
        self.assertFalse(self.f.auth)


    def test_login_during_slow_initial_session_probe_is_not_cancelled(self):
        self.f.auth = False
        self.f.session_get_delay = 1.2
        self.page.goto(self.origin)
        expect(self.page.locator('#gate')).to_be_visible()
        self.page.locator('#token').fill('fixture-access')
        self.page.locator('#token').press('Enter')
        expect(self.page.locator('#gate')).not_to_be_visible()
        expect(self.page.locator('#route-grid')).to_contain_text('alpha.example.org')
        self.page.wait_for_timeout(1600)
        expect(self.page.locator('#gate')).not_to_be_visible()
        expect(self.page.locator('#route-add')).to_be_enabled()
    def test_expired_mutation_reopens_gate_without_false_ambiguous_lock(self):
        self.open()
        self.page.locator('#route-add').click()
        self.page.locator('#f-host').fill('new.example.org')
        self.page.locator('#f-service').fill('8080')
        self.f.auth = False
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#gate')).to_be_visible()
        self.page.locator('#token').fill('fixture-access')
        self.page.locator('#token').press('Enter')
        expect(self.page.locator('#gate')).not_to_be_visible()
        expect(self.page.locator('#route-add')).to_be_enabled()
        self.assertEqual(self.f.posts,[])
        self.assertFalse(self.page.locator('#task-banner').is_visible())

    def test_profile_race_scoping_and_preferences(self):
        self.f.delays[('/api/state','alpha')] = 1.5
        self.page.goto(self.origin)
        expect(self.page.locator('#profile-select')).to_be_enabled()
        self.page.locator('#profile-select').select_option('beta')
        expect(self.page.locator('#route-grid')).to_contain_text('beta.example.org')
        self.page.wait_for_timeout(1800)
        expect(self.page.locator('#route-grid')).not_to_contain_text('alpha.example.org')
        self.page.reload()
        expect(self.page.locator('#profile-select')).to_have_value('beta')
        expect(self.page.locator('#route-grid')).to_contain_text('beta.example.org')
        for _,path,profile,_ in self.f.requests:
            if path in ['/api/session','/api/profiles','/api/audit','/api/jobs'] or path.startswith('/api/jobs/'):
                self.assertEqual(profile,'',path)
            else:
                self.assertIn(profile,['alpha','beta'],path)

    def test_reload_active_global_health_false_and_log_cursor_truncation(self):
        self.f.offset = 50
        self.f.next_cursor = 52
        job = self.f.job()
        self.f.jobs[job['id']] = job
        self.page.goto(self.origin)
        expect(self.page.locator('#task-title')).to_contain_text('BETA')
        self.nav('activity')
        expect(self.page.locator('#log-omitted')).to_contain_text('50')
        expect(self.page.locator('#job-log')).to_contain_text('fixture log <not html>')
        self.assertEqual(self.page.locator('#job-log not').count(),0)
        self.f.errors['/api/jobs/restored'] = 503
        expect(self.page.locator('#job-detail-notice')).to_contain_text('状态待同步',timeout=8000)
        self.assertEqual(self.f.posts,[])
        del self.f.errors['/api/jobs/restored']
        job.update(offset=60,next=62,lines=[dict(t=5,m='new retained log')])
        self.page.locator('#task-reconnect').click()
        expect(self.page.locator('#job-log')).to_contain_text('new retained log')
        self.assertIn(('restored',52),self.f.job_reads)
        self.page.reload()
        expect(self.page.locator('#task-title')).to_contain_text('BETA')
        job.update(status='done',result={'ok':False},ended=1800000005,next=63)
        expect(self.page.locator('#task-status')).to_contain_text('执行结束，检查未通过',timeout=8000)
        self.nav('activity')
        expect(self.page.locator('#job-detail-notice')).to_contain_text('检查未通过')
        self.assertFalse(any('profile=' in r[3] for r in self.f.requests if r[1].startswith('/api/jobs')))

    def test_mutations_confirm_and_external_cli_only(self):
        self.open()
        route = self.page.locator('[data-host="alpha.example.org"]')
        route.locator('[data-route-action="delete"]').click()
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#f-confirm-error')).to_contain_text('原样输入')
        self.assertEqual(self.f.posts,[])
        self.page.locator('#f-confirm').fill('alpha.example.org')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1],dict(op='delete',hostname='alpha.example.org',confirm='alpha.example.org'))
        self.finish()
        expect(self.page.locator('#route-add')).to_be_enabled(timeout=8000)
        route.locator('[data-route-action="edit"]').click()
        expect(self.page.locator('#f-noverify')).not_to_be_checked()
        self.page.locator('#f-service').fill('https://127.0.0.1:9443')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1]['op'],'update')
        self.assertIs(self.f.posts[-1][1]['no_tls_verify'],False)
        self.finish()
        expect(self.page.locator('#route-add')).to_be_enabled(timeout=8000)
        route.locator('[data-route-action="switch"]').click()
        self.assertEqual(len(self.f.posts),2)
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1]['op'],'switch')
        self.finish()
        expect(self.page.locator('#route-add')).to_be_enabled(timeout=8000)
        for action in ['delete','switch']:
            self.page.locator('[data-host="external.example.com"]').locator(f'[data-route-action="{action}"]').click()
            expect(self.page.locator('#modal-body')).to_contain_text('--external')
            self.page.keyboard.press('Escape')
        self.assertEqual(len(self.f.posts),3)
        self.nav('cdn')
        self.page.locator('[data-cdn-set="next.example.net"]').click()
        self.assertEqual(len(self.f.posts),3)
        expect(self.page.locator('#modal-body')).to_contain_text('全部业务域名')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1],dict(op='cdn-set',target='next.example.net',confirm='next.example.net'))
        self.finish()
        expect(self.page.locator('#cdn-best')).to_be_enabled(timeout=8000)
        self.page.locator('#cdn-best').click()
        expect(self.page.locator('#modal-body')).to_contain_text('业务 Host/SNI')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1],dict(op='cdn-best'))
        self.finish()
        expect(self.page.locator('#cdn-test')).to_be_enabled(timeout=8000)
        self.page.locator('#cdn-test').click()
        expect(self.page.locator('#task-title')).to_contain_text('cdn-test')
        self.assertEqual(self.f.posts[-1][1],dict(op='cdn-test'))

    def test_timeout_rate_limit_and_missing_planned_endpoints(self):
        self.f.errors.update({'/api/jobs':404,'/api/environment':404,'/api/audit':429})
        self.open()
        expect(self.page.locator('#jobs-notice')).to_contain_text('未提供任务列表')
        expect(self.page.locator('#environment-notice')).to_contain_text('未提供环境诊断')
        expect(self.page.locator('#audit-notice')).to_contain_text('等待 7 秒')
        self.f.hang.add('/api/services')
        self.nav('services')
        self.page.locator('#services-refresh').click()
        expect(self.page.locator('#services-notice')).to_contain_text('请求超时',timeout=15000)
        self.nav('routes')
        expect(self.page.locator('#route-add')).to_be_enabled()

    def test_state_errors_no_fake_target_and_form_external_validation(self):
        self.f.state_errors = True
        self.f.state_verified = False
        self.page.goto(self.origin)
        expect(self.page.locator('#routes-notice')).to_contain_text('API permission denied')
        expect(self.page.locator('#state-summary')).to_contain_text('未知')
        expect(self.page.locator('#state-summary')).not_to_contain_text('已验证')
        expect(self.page.locator('#route-add')).to_be_disabled()
        self.f.state_errors = False
        self.page.locator('#routes-refresh').click()
        expect(self.page.locator('#route-add')).to_be_enabled()
        self.page.locator('#route-add').click()
        self.page.locator('#f-host').fill('elsewhere.example.com')
        self.page.locator('#f-service').fill('8080')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#f-host-error')).to_contain_text('外部 DNS')
        self.assertEqual(self.f.posts,[])

    def test_ambiguous_post_is_not_replayed(self):
        self.open()
        self.f.submit_unknown = True
        self.page.locator('#route-add').click()
        self.page.locator('#f-host').fill('new.example.org')
        self.page.locator('#f-service').fill('8080')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal-error')).to_contain_text('回执未确认')
        self.page.keyboard.press('Escape')
        self.page.locator('#task-reconnect').click()
        expect(self.page.locator('#task-title')).to_contain_text('add')
        self.assertEqual(len(self.f.posts),1)
        self.page.reload()
        expect(self.page.locator('#task-title')).to_contain_text('add')
        self.assertEqual(len(self.f.posts),1)

    def test_failed_mutation_refreshes_without_success_and_warnings_are_visible(self):
        self.f.services_warnings = ['ss missing; install iproute2']
        self.open()
        expect(self.page.locator('#services-notice')).to_contain_text('ss missing')
        self.page.locator('[data-host="alpha.example.org"] [data-route-action="edit"]').click()
        self.page.locator('#f-service').fill('https://[::1]/api')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal')).not_to_be_visible()
        self.assertEqual(self.f.posts[-1][1]['service'],'https://[::1]/api')
        before = sum(path == '/api/state' for _,path,_,_ in self.f.requests)
        job = self.finish(status='error')
        job['error'] = 'fixture partial mutation; rollback incomplete'
        self.nav('activity')
        expect(self.page.locator('#job-detail-notice')).to_contain_text('任务失败',timeout=8000)
        expect(self.page.locator('#job-detail-notice')).to_contain_text('rollback incomplete')
        self.assertGreater(sum(path == '/api/state' for _,path,_,_ in self.f.requests),before)
        expect(self.page.locator('#route-add')).to_be_enabled()

    def test_success_response_without_task_id_is_ambiguous_not_resubmitted(self):
        self.open()
        self.f.omit_submit_job = True
        self.page.locator('#route-add').click()
        self.page.locator('#f-host').fill('new.example.org')
        self.page.locator('#f-service').fill(':8080')
        self.page.locator('#modal-ok').click()
        expect(self.page.locator('#modal-error')).to_contain_text('回执未确认')
        self.page.keyboard.press('Escape')
        self.page.locator('#task-reconnect').click()
        expect(self.page.locator('#task-title')).to_contain_text('add')
        self.assertEqual(len(self.f.posts),1)

    def test_search_responsive_all_pages_and_touch_targets(self):
        self.open()
        self.page.locator('#routes-filter').fill('NO MATCH')
        expect(self.page.locator('#route-grid')).to_contain_text('没有匹配')
        self.page.locator('#routes-filter').fill('alpha')
        expect(self.page.locator('#route-count')).to_contain_text('1 / 2')
        for width in [320,375,768,1440]:
            self.page.set_viewport_size({'width':width,'height':900})
            for section in ['routes','services','cdn','diagnostics','activity','environment']:
                self.nav(section)
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,section))
                small = self.page.locator('button:visible, input:not([type=checkbox]):visible, select:visible, .nav a:visible').evaluate_all('(els)=>els.filter(e=>e.getBoundingClientRect().height < 44).map(e=>e.id || e.textContent)')
                self.assertEqual(small,[],(width,section))
            self.nav('routes')
            self.page.locator('#route-add').click()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertTrue(self.page.locator('#modal').evaluate('(e)=>e.scrollWidth <= e.clientWidth'))
            self.page.keyboard.press('Escape')


class ProductionHTTPBrowserTests(unittest.TestCase):
    """Use the real production Handler, authentication, App and JobRunner.

    Only the external CLI boundary and subprocess discovery are fixtures; no API
    route interception. Reuses existing HTTPIntegrationTests setup/cleanup.
    """
    def test_real_server_browser_login_profile_mutation_and_health_false(self):
        # Import CLI-backed test helpers under an empty HOME, never host credentials.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with mock.patch.dict(os.environ, {'HOME':home.name,'PATH':'/usr/bin:/bin'}, clear=True):
            from test_http_integration import HTTPIntegrationTests
        fixture = HTTPIntegrationTests(methodName='runTest')
        try:
            fixture.setUp()
            for profile in fixture.profiles.values():
                profile.health_check = lambda *args, **kwargs: False
            with sync_playwright() as pw:
                candidates = sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux/chrome'))
                executable = os.environ.get('UI_CHROMIUM') or (str(candidates[-1]) if candidates else None)
                browser = pw.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
                try:
                    page = browser.new_page(viewport={'width':375,'height':900})
                    errors = []
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(fixture.origin)
                    expect(page.locator('#gate')).to_be_visible()
                    page.locator('#token').fill(fixture.token)
                    page.locator('#token').press('Enter')
                    expect(page.locator('#route-grid')).to_contain_text('alpha.example.org')
                    expect(page.locator('#route-add')).to_be_enabled()
                    page.locator('#profile-select').select_option('beta')
                    expect(page.locator('#route-grid')).to_contain_text('beta.example.org')
                    page.locator('[data-host="beta.example.org"] [data-route-action="edit"]').click()
                    page.locator('#f-service').fill('http://127.0.0.1:9090')
                    page.locator('#modal-ok').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    expect(page.locator('#task-status')).to_contain_text('执行结束',timeout=10000)
                    self.assertTrue(any(c[0]=='upsert' for c in fixture.profiles['beta'].calls))
                    self.assertEqual(fixture.profiles['alpha'].calls,[])
                    page.locator('[data-host="beta.example.org"] [data-route-action="health"]').click()
                    expect(page.locator('#task-status')).to_contain_text('执行结束，检查未通过',timeout=10000)
                    page.reload()
                    expect(page.locator('#profile-select')).to_have_value('beta')
                    expect(page.locator('#task-status')).to_contain_text('检查未通过',timeout=10000)
                    page.locator('.nav a[href="#activity"]').click()
                    expect(page.locator('#job-list')).to_contain_text('Beta console')
                    expect(page.locator('#job-detail-notice')).to_contain_text('检查未通过')
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertEqual(errors,[])
                    page.locator('#logout').click()
                    expect(page.locator('#gate')).to_be_visible()
                finally:
                    browser.close()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main(verbosity=2)
