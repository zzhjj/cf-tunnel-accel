#!/usr/bin/env python3
"""Container console E2E using only the Python standard library.

Run explicitly, after Docker interfaces are ready:
    python3 tests/docker_e2e.py
    python3 tests/docker_e2e.py --no-build  # use cf-tunnel-accel:local

Requires Docker Engine and Compose v2, not pytest, browser automation, host
cloudflared, or host credentials. The real Compose console and CLI run unchanged.
A peer HTTP contract runs from the same image on an INTERNAL project bridge;
all apparent cloud reads/writes are local fixture traffic. Image construction
may need registry/package-network access, but runtime containers have no external
route. This tests container-console scope, not connector deployment, live cloud
permissions/propagation, browser rendering, or host-service management.

Only a fresh random Compose project is started/recreated/removed. No default
host port, Docker prune, global teardown, privileged mode, or host runtime mount
is used. Effective UI keys and command/log/HTTP response bodies are never printed,
including on failure. Cleanup runs in finally; exit 1 means failure, including
cleanup failure. Ctrl-C also cleans up. No Docker commands run on import.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "cf-tunnel-accel"
ZONE = "b" * 32
ACCOUNT = "a" * 32
BOOTSTRAP = re.compile(r"^  首次登录密钥: ([0-9a-fA-F]{48})$", re.MULTILINE)

# This is a standalone stdlib HTTP peer, not a replacement UI, CLI, or transport.
# Its durable journal records method/path/body and auth verdict, NEVER headers.
FIXTURE = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json, threading
root = Path('/data')
lock = threading.Lock()
zone = 'b' * 32
account = 'a' * 32
record_path = root / 'record.json'
journal = root / 'requests.jsonl'
if not record_path.exists():
    record_path.write_text(json.dumps(dict(id='entry', type='CNAME',
        name='cdn.example.test', content='initial.example.test', ttl=120, proxied=False)))
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def respond(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def route(self):
        parsed = urlsplit(self.path)
        with lock:
            if parsed.path == '/__status' and self.command == 'GET':
                rows = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
                return self.respond(200, dict(rows=rows, record=json.loads(record_path.read_text())))
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            auth = self.headers.get('Authorization') == 'Bearer fixture-only-not-a-cloud-credential'
            row = dict(method=self.command, path=parsed.path, query=parse_qs(parsed.query),
                       body=body, authorized=auth, host=self.headers.get('Host'))
            with journal.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
                stream.flush()
            if parsed.path == '/backend' and self.command in ('GET', 'HEAD'):
                return self.respond(200, dict(ok=True, source='local-fixture'))
            if not auth:
                return self.respond(403, dict(success=False, errors=[dict(message='fixture authentication rejected')]))
            info = dict(id=zone, name='example.test', status='active', paused=False,
                        type='full', account=dict(id=account, name='Fixture'), plan=dict(name='Free'))
            prefix = '/zones/' + zone
            record = json.loads(record_path.read_text())
            if self.command == 'GET':
                if parsed.path == '/user/tokens/verify': result = dict(status='active')
                elif parsed.path == '/zones': result = [info]
                elif parsed.path == prefix: result = info
                elif parsed.path == prefix + '/custom_hostnames': result = []
                elif parsed.path == prefix + '/dns_records':
                    name = parse_qs(parsed.query).get('name', [''])[0]
                    result = [record] if not name or name == record['name'] else []
                else: return self.respond(404, dict(success=False, errors=[dict(message='unrecognized fixture route')]))
            elif self.command == 'PUT' and parsed.path == prefix + '/dns_records/entry':
                if (set(body) - {'type', 'name', 'content', 'ttl', 'proxied', 'comment'} or
                    body.get('name') != 'cdn.example.test' or body.get('type') != 'CNAME' or
                    body.get('proxied') is not False or
                    body.get('content') not in ('ui.example.test', 'cli.example.test')):
                    return self.respond(422, dict(success=False, errors=[dict(message='unexpected fixture mutation')]))
                record.update(body)
                record_path.write_text(json.dumps(record))
                result = record
            else:
                return self.respond(405, dict(success=False, errors=[dict(message='unexpected fixture mutation')]))
            self.respond(200, dict(success=True, result=result, result_info=dict(total_pages=1)))
    do_GET = route
    do_HEAD = route
    do_POST = route
    do_PUT = route
    do_PATCH = route
    do_DELETE = route
ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
'''


class Failure(Exception):
    """Only fixed, credential-free messages may be exposed to the terminal."""


def require(condition, label):
    if not condition:
        raise Failure(label)


def run(args, env, timeout=60, check=True, input_text=None):
    # Never echo commands or subprocess output: docker logs/exec can contain keys.
    try:
        result = subprocess.run(args, env=env, cwd=ROOT, text=True, input=input_text,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure("Docker operation exceeded its deadline") from None
    except OSError:
        raise Failure("Docker executable could not be started") from None
    if check and result.returncode:
        raise Failure("Docker operation failed (output suppressed)")
    return result


class Client:
    def __init__(self, port, scenario=None):
        self.port, self.scenario, self.cookie, self.csrf = port, scenario, "", ""

    def request(self, path, body=None, *, csrf=True, method=None):
        origin_port = self.port if self.port is not None else 8642
        headers = {"Origin": f"http://127.0.0.1:{origin_port}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf and self.csrf:
            headers["X-CSRF"] = self.csrf
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request_data = {"method": method or ("POST" if body is not None else "GET"),
                        "path": path, "headers": headers, "body": body}
        if self.scenario is not None:
            status, response_headers, raw = self.scenario.request_from_inside(request_data)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request(request_data["method"], path, payload, headers)
                response = conn.getresponse()
                status, response_headers = response.status, response.getheaders()
                raw = response.read(1024 * 1024)
            finally:
                conn.close()
        cookies = [value.split(";", 1)[0] for key, value in response_headers
                   if key.lower() == "set-cookie"]
        if cookies:
            self.cookie = "; ".join(cookies)
        content_type = next((value for key, value in response_headers
                             if key.lower() == "content-type"), "")
        if content_type.startswith("application/json"):
            data = json.loads(raw)
        else:
            data = raw.decode("utf-8", errors="replace")
        return status, data

    def get(self, path):
        status, data = self.request(path)
        require(status == 200 and isinstance(data, dict), "API GET contract failed")
        return data

    def login(self, key, expected=200):
        status, data = self.request("/api/session", {"token": key})
        require(status == expected, "Session authentication contract failed")
        if expected == 200:
            require(data.get("ok") is True and bool(data.get("csrf")), "Session lacks CSRF token")
            self.csrf = data["csrf"]


class Scenario:
    def __init__(self, directory, build, *, internal_network=True, smoke=False):
        self.directory = Path(directory)
        self.project = "cfa-e2e-" + secrets.token_hex(8)
        self.build = build
        self.internal_network = internal_network
        self.smoke = smoke
        self.image = 'cf-tunnel-accel:local'
        self.config = self.directory / "config"
        self.config.mkdir(mode=0o755, parents=True)
        self.config.chmod(0o755)
        # Intentionally do not inherit application/Compose settings, API credentials,
        # Python overrides or proxies. Keep only Docker-client/platform essentials.
        keep = {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR",
                "SSH_AUTH_SOCK", "SYSTEMROOT", "USERPROFILE"}
        self.env = {key: value for key, value in os.environ.items() if key in keep}
        self.env.update({"LANG": "C.UTF-8", "COMPOSE_ANSI": "never", "COMPOSE_PROGRESS": "plain",
                         "CFA_DOCKER_PORT": "0", "CFA_DOCKER_CONFIG_DIR": str(self.config),
                         "CF_TUNNEL_ACCEL_UI_TOKEN": "", "CF_TUNNEL_ACCEL_UI_TOKEN_FILE": "",
                         "CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN": "1",
                         "CF_TUNNEL_ACCEL_UI_PROFILES": "/config/profiles.json"})
        self.empty_env = self.directory / "empty.env"
        self.empty_env.write_text("")
        self.override = self.directory / "override.json"
        override = {
            "services": {
                SERVICE: {"image": self.image},
                "fixture": {"image": self.image, "pull_policy": "never", "user": "1000:1000",
                            "command": ["python", "-u", "-c", FIXTURE], "restart": "no",
                            "read_only": True, "cap_drop": ["ALL"],
                            "security_opt": ["no-new-privileges:true"],
                            "healthcheck": {"disable": True},
                            "volumes": ["fixture-data:/data"],
                            "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=16m"],
                            "networks": ["default"]}},
            "volumes": {"fixture-data": {}},
            "networks": {"default": {"internal": self.internal_network}}}
        self.override.write_text(json.dumps(override))
        self.compose = ["docker", "compose", "--project-name", self.project,
                        "--project-directory", str(ROOT), "--env-file", str(self.empty_env),
                        "-f", str(ROOT / "compose.yaml")]
        if not self.smoke:
            self.compose += ["-f", str(self.override)]
        self.started = False
        self.custom_key = secrets.token_hex(32)
        if self.smoke:
            self.env['CF_TUNNEL_ACCEL_UI_PROFILES'] = ''
            (self.config / 'env').write_text('')
            (self.config / 'env').chmod(0o444)
        if not self.smoke:
            self.write_configuration()

    def write_configuration(self):
        for ident in ("alpha", "beta"):
            rows = {
                "TUNNEL_ID": "11111111-1111-4111-8111-111111111111",
                "TUNNEL_NAME": ident, "CONFIG": f"/config/{ident}.yml",
                "SERVICE": f"fixture-{ident}.service", "ACCOUNT_ID": ACCOUNT,
                "AUX_ZONE": "example.test", "AUX_ZONE_ID": ZONE,
                "API_BASE": "http://fixture:8080", "API_RETRIES": "1",
                "TOKEN_FILE": "/config/cloud-token", "PREFERRED_ENTRY": "cdn.example.test",
                "PREFERRED_TARGET": "initial.example.test",
                "FALLBACK_ORIGIN": "origin.example.test", "CDN_CANDIDATES": "ui.example.test,cli.example.test",
                "ZONE_CACHE": f"/data/{ident}-zones.json", "LOCK": f"/data/{ident}.lock"}
            text = "".join(f"CF_TUNNEL_ACCEL_{key}={value}\n" for key, value in rows.items())
            (self.config / ("env" if ident == "alpha" else "beta.env")).write_text(text)
            (self.config / f"{ident}.yml").write_text(
                "tunnel: 11111111-1111-4111-8111-111111111111\ningress:\n"
                f"  - hostname: {ident}.example.test\n    service: http://fixture:8080/backend\n"
                "  - service: http_status:404\n")
        (self.config / "cloud-token").write_text("fixture-only-not-a-cloud-credential\n")
        (self.config / "custom-ui-key").write_text(self.custom_key + "\n")
        (self.config / "profiles.json").write_text(json.dumps({"profiles": [
            {"id": ident, "name": ident.title(), "env_file": "/config/" + name}
            for ident, name in (("alpha", "env"), ("beta", "beta.env"))]}))
        for path in self.config.iterdir():
            path.chmod(0o444)
        self.original_config = self.config_digest()

    def config_digest(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.config.iterdir()}

    def dc(self, *args, timeout=60, check=True, input_text=None):
        return run(self.compose + list(args), self.env, timeout, check, input_text)

    def exec(self, *args, service=SERVICE, timeout=30, check=True, input_text=None):
        return run(self.compose + ["exec", "-T", service] + list(args), self.env,
                   timeout=timeout, check=check, input_text=input_text)

    def fixture_status(self):
        result = self.exec("python", "-c", "import urllib.request; print(urllib.request.urlopen("
                           "'http://fixture:8080/__status', timeout=3).read().decode())")
        return json.loads(result.stdout)

    def container_id(self):
        ident = self.dc("ps", "-q", SERVICE).stdout.strip()
        require(bool(re.fullmatch(r"[a-f0-9]{12,64}", ident)), "Expected one isolated console container")
        return ident

    def inspect(self):
        # Do not inspect Config.Env, process args, or entire objects: they may
        # contain effective credentials. Only non-secret fields leave Engine.
        fields = {"user": ".Config.User", "readonly": ".HostConfig.ReadonlyRootfs",
                  "privileged": ".HostConfig.Privileged", "pid": ".HostConfig.PidMode",
                  "ipc": ".HostConfig.IpcMode", "network_mode": ".HostConfig.NetworkMode",
                  "cap_drop": ".HostConfig.CapDrop", "cap_add": ".HostConfig.CapAdd",
                  "security": ".HostConfig.SecurityOpt", "tmpfs": ".HostConfig.Tmpfs",
                  "mounts": ".Mounts", "ports": ".NetworkSettings.Ports",
                  "port_bindings": ".HostConfig.PortBindings",
                  "project": '(index .Config.Labels "com.docker.compose.project")',
                  "networks": ".NetworkSettings.Networks"}
        template = "{" + ",".join(json.dumps(key) + ":{{json " + value + "}}"
                                     for key, value in fields.items()) + "}"
        return json.loads(run(["docker", "inspect", "--format", template, self.container_id()], self.env).stdout)

    def check_isolation(self):
        info = self.inspect()
        require(info["project"] == self.project, "Console project ownership mismatch")
        require(info["user"] == "1000:1000", "Expected configured unprivileged UID/GID")
        require(info["readonly"] is True and info["privileged"] is False, "Rootfs/privilege contract failed")
        require(info["pid"] != "host" and info["ipc"] != "host" and
                info["network_mode"] != "host", "Host namespace must not be shared")
        require("ALL" in (info["cap_drop"] or []) and not info["cap_add"], "Capabilities are not fully dropped")
        require(any(value.startswith("no-new-privileges") for value in info["security"] or []),
                "Missing no-new-privileges")
        require("/tmp" in (info["tmpfs"] or {}), "Missing temporary writable filesystem")
        mounts = info["mounts"]
        binds = [item for item in mounts if item["Type"] == "bind"]
        require(len(binds) == 1 and binds[0]["Destination"] == "/config" and
                Path(binds[0]["Source"]).resolve() == self.config.resolve() and not binds[0]["RW"],
                "Only the temporary read-only configuration bind is allowed")
        require(all(item["Destination"] in ("/config", "/data", "/tmp") for item in mounts),
                "Unexpected container mount")
        data = [item for item in mounts if item["Destination"] == "/data"]
        require(len(data) == 1 and data[0]["Type"] == "volume" and data[0]["RW"],
                "State must use a writable named volume")
        volume = json.loads(run(["docker", "volume", "inspect", "--format", "{{json .Labels}}",
                                 data[0]["Name"]], self.env).stdout)
        require(volume.get("com.docker.compose.project") == self.project, "Data volume is not project-owned")
        bindings = info["port_bindings"].get("8642/tcp") or []
        require(len(bindings) == 1 and bindings[0].get('HostIp') == '127.0.0.1' and
                str(bindings[0].get('HostPort', '')).isdigit() and
                int(bindings[0]['HostPort']) != 8642,
                'UI must configure only an ephemeral IPv4-loopback binding')
        ports = info["ports"].get("8642/tcp")
        if self.internal_network:
            require(not ports or len(ports) == 1 and ports[0].get('HostIp') == '127.0.0.1' and
                    0 < int(ports[0]['HostPort']) < 65536 and int(ports[0]['HostPort']) != 8642,
                    'Internal bridge must not expose a non-loopback UI port')
            port = None
        else:
            require(isinstance(ports, list) and len(ports) == 1 and
                    ports[0].get("HostIp") == "127.0.0.1",
                    "Normal bridge must publish UI only on IPv4 loopback")
            port = int(ports[0]["HostPort"])
            require(0 < port < 65536 and port != 8642,
                    "UI must use an ephemeral non-default host port")
        for network in info["networks"].values():
            metadata = json.loads(run(["docker", "network", "inspect", "--format",
                                      '{"internal":{{json .Internal}},"driver":{{json .Driver}},"labels":{{json .Labels}}}',
                                      network["NetworkID"]], self.env).stdout)
            require(metadata["internal"] is self.internal_network and metadata["driver"] == "bridge" and
                    metadata["labels"].get("com.docker.compose.project") == self.project,
                    "Runtime network isolation/project ownership mismatch")
        identity = json.loads(self.exec("python", "-c", "import os,json; print(json.dumps([os.getuid(),os.getgid()]))").stdout)
        require(identity == [1000, 1000], "Effective UID/GID mismatch")
        return port

    def request_from_inside(self, request_data):
        script = r'''import http.client, json, sys
r = json.load(sys.stdin)
conn = http.client.HTTPConnection('127.0.0.1', 8642, timeout=5)
try:
    body = None if r['body'] is None else json.dumps(r['body']).encode()
    conn.request(r['method'], r['path'], body, r['headers'])
    response = conn.getresponse()
    raw = response.read(1048576)
    print(json.dumps({'status': response.status, 'headers': response.getheaders(),
                      'body': raw.decode('utf-8', 'replace')}))
finally:
    conn.close()
'''
        result = self.exec("python", "-c", script, input_text=json.dumps(request_data))
        data = json.loads(result.stdout)
        return data["status"], data["headers"], data["body"].encode("utf-8")

    def wait_ready(self):
        port = self.check_isolation()
        client = Client(port, self if port is None else None)
        deadline = time.monotonic() + 45
        last_status = None
        while time.monotonic() < deadline:
            try:
                status, data = client.request("/api/health")
                last_status = status
                if status == 200 and data == {"ok": True, "status": "ok"} and data.get("ok") is True:
                    if not self.smoke:
                        self.fixture_status()
                    return client
            except (OSError, ValueError, http.client.HTTPException, Failure):
                pass
            time.sleep(0.25)
        raise Failure("Console/fixture did not become healthy within the deadline (HTTP status " +
                      str(last_status) + ")")

    def managed_key(self):
        result = self.exec("python", "-c", "from pathlib import Path; import json,os,stat; "
                           "p=Path('/data/ui-token'); s=p.stat(); "
                           "print(json.dumps({'key':p.read_text().strip(),'mode':stat.S_IMODE(s.st_mode),"
                           "'uid':s.st_uid,'gid':s.st_gid}))")
        data = json.loads(result.stdout)
        require(data["mode"] == 0o600 and data["uid"] == 1000 and data["gid"] == 1000,
                "Managed key file ownership/mode mismatch")
        require(bool(re.fullmatch(r"[a-fA-F0-9]{48}", data["key"])), "Managed key format mismatch")
        return data["key"]

    def check_readonly(self):
        # Verify mount flags as well as failed writes, not just UNIX mode bits.
        script = """import errno, os, pathlib
for mount in ('/config', '/app'):
    if not os.statvfs(mount).f_flag & os.ST_RDONLY: raise SystemExit(1)
for path in ('/config/env', '/config/custom-ui-key', '/config/new-file'):
    try:
        with open(path, 'a') as stream: stream.write('unexpected')
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES): raise SystemExit(2)
    else: raise SystemExit(3)
try:
    pathlib.Path('/app/should-not-exist').write_text('unexpected')
except OSError as exc:
    if exc.errno not in (errno.EROFS, errno.EACCES): raise SystemExit(4)
else: raise SystemExit(5)
"""
        self.exec("python", "-c", script)
        require(self.config_digest() == self.original_config, "Mounted source configuration changed")

    def check_rejections(self, client):
        baseline_jobs = client.get("/api/jobs")["jobs"]
        baseline_requests = self.fixture_status()["rows"]
        for op in ("add", "update", "delete"):
            body = {"op": op, "hostname": "alpha.example.test", "service": "http://fixture:8080/backend",
                    "confirm": "alpha.example.test"}
            anonymous = Client(client.port, client.scenario)
            require(anonymous.request("/api/jobs?profile=alpha", body)[0] == 401,
                    "Ingress guard must still require authentication")
            require(client.request("/api/jobs?profile=alpha", body, csrf=False)[0] == 403,
                    "Ingress guard must still require CSRF")
            code, data = client.request("/api/jobs?profile=alpha", body)
            require(code == 409 and data.get("code") == "local_tunnel_unmanaged",
                    "Container ingress mutations must return explicit unmanaged conflict")
        require(client.get("/api/jobs")["jobs"] == baseline_jobs,
                "Blocked ingress operation reached job admission")
        require(self.fixture_status()["rows"] == baseline_requests,
                "Blocked ingress operation reached backend/cloud network")
        self.check_readonly()

    def positive_operations(self, client):
        query = urlencode({"profile": "alpha", "service": "http://fixture:8080/backend",
                           "hostname": "alpha.example.test"})
        probe = client.get("/api/probe?" + query)
        require(probe.get("probe", {}).get("ok") is True, "Real backend HTTP probe failed")
        rows = self.fixture_status()["rows"]
        require(any(row["path"] == "/backend" and row["host"] == "alpha.example.test"
                    for row in rows), "Backend fixture did not observe the real probe")
        status, submitted = client.request("/api/jobs?profile=alpha", {
            "op": "cdn-set", "target": "ui.example.test", "confirm": "ui.example.test"})
        require(status == 200 and submitted.get("ok") is True, "Supported cloud job was not admitted")
        ident = submitted["job"]["id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            job = client.get("/api/jobs/" + ident)["job"]
            if job["status"] in ("done", "error", "interrupted"):
                break
            time.sleep(0.15)
        require(job["status"] == "done" and job.get("outcome") == "ok" and
                job.get("result", {}).get("target") == "ui.example.test", "Supported cloud job did not succeed")
        require(self.fixture_status()["record"]["content"] == "ui.example.test",
                "UI cloud job did not change the isolated contract state")
        # Actual installed image CLI, no shell shim, PYTHONPATH or monkeypatch.
        self.exec("python", "/app/cf-tunnel-accel", "cdn", "set", "cli.example.test", timeout=45)
        snapshot = self.fixture_status()
        require(snapshot["record"]["content"] == "cli.example.test", "Real CLI cloud operation failed")
        mutations = [row for row in snapshot["rows"] if row["method"] not in ("GET", "HEAD")]
        require(len(mutations) == 2, "Unexpected fixture mutation count")
        require([row["body"].get("content") for row in mutations] == ["ui.example.test", "cli.example.test"],
                "Persistent cloud mutation sequence differs from contract")
        require(all(row["method"] == "PUT" and row["path"] == f"/zones/{ZONE}/dns_records/entry"
                    and row["authorized"] and row["body"].get("proxied") is False for row in mutations),
                "Cloud writes escaped the permitted local API contract")
        require(all(row["authorized"] for row in snapshot["rows"] if row["path"] != "/backend"),
                "Fixture saw invalid cloud credentials")
        return ident, snapshot

    def recreate(self):
        previous = self.container_id()
        self.dc("up", "-d", "--no-build", "--no-deps", "--force-recreate", SERVICE, timeout=60)
        require(self.container_id() != previous, "Console was not recreated")
        return self.wait_ready()

    def execute(self):
        run(["docker", "compose", "version"], self.env)
        run(["docker", "info", "--format", "{{.ServerVersion}}"], self.env)
        # A collision must fail closed, never adopt another project's resources.
        for kind, args in (("container", ["ps", "-aq"]), ("network", ["network", "ls", "-q"]),
                           ("volume", ["volume", "ls", "-q"])):
            result = run(["docker"] + args + ["--filter", "label=com.docker.compose.project=" + self.project], self.env)
            require(not result.stdout.strip(), "Random project name already owns resources")
        # Fail CLOSED before any up/build if the coordinated Compose interface is
        # not ready; never fall back to the repository's occupied default port or
        # real host configuration. Never expose the resolved environment values.
        resolved = json.loads(self.dc("config", "--format", "json").stdout)
        ui = resolved["services"][SERVICE]
        ports = ui.get("ports", [])
        require(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1" and
                str(ports[0].get("published")) == "0" and int(ports[0]["target"]) == 8642,
                "Compose ephemeral-loopback port interface is not ready")
        mounts = ui.get("volumes", [])
        require(len(mounts) == 2, "Compose has unexpected persistent mounts")
        config_mount = next((item for item in mounts if item["target"] == "/config"), {})
        data_mount = next((item for item in mounts if item["target"] == "/data"), {})
        require(config_mount.get("type") == "bind" and config_mount.get("read_only") is True and
                Path(config_mount.get("source", "/missing")).resolve() == self.config.resolve(),
                "Compose temporary read-only config interface is not ready")
        require(data_mount.get("type") == "volume" and data_mount.get("source") in resolved.get("volumes", {}),
                "Compose named data-volume interface is not ready")
        for value in resolved.get("volumes", {}).values():
            require(not value.get("external") and value.get("name", "").startswith(self.project + "_"),
                    "Compose must not adopt external or fixed-name volumes")
        for value in resolved.get("networks", {}).values():
            require(not value.get("external") and value.get("name", "").startswith(self.project + "_"),
                    "Compose must not adopt external or fixed-name networks")
        require(not any(value.get("container_name") for value in resolved["services"].values()),
                "Compose must not use fixed container names")
        if self.smoke:
            require(ui.get("image") == "cf-tunnel-accel:local",
                    "Production smoke must use the prebuilt application image")
            require(ui.get("privileged") is not True and not ui.get("container_name") and
                    ui.get("network_mode") != "host",
                    "Production Compose smoke resolved unsafe or unexpected settings")
            require(not any(key.startswith(("CF_TUNNEL_ACCEL_API", "CF_TUNNEL_ACCEL_TOKEN",
                                           "CLOUDFLARE_API", "CLOUDFLARE_TOKEN"))
                            for key in ui.get("environment", {})),
                    "Production smoke must not receive cloud credential environment")
            self.started = True
            self.dc("up", "-d", "--no-build", "--pull", "never", SERVICE, timeout=90)
            client = self.wait_ready()
            require(client.get("/api/health") == {"ok": True, "status": "ok"},
                    "Production Compose health response mismatch")
            client.login(self.managed_key())
            report = client.get('/api/environment')['environment']
            require(report['ready'] is False and report['missing'],
                    'Empty configuration must explain missing setup after successful login')
            require(report['capabilities']['local_tunnel_management'] is False,
                    'Empty configuration lost the container capability boundary')
            health = run(['docker', 'inspect', '--format', '{{.State.Health.Status}}',
                          self.container_id()], self.env)
            deadline = time.monotonic() + 40
            while health.stdout.strip() != 'healthy' and time.monotonic() < deadline:
                time.sleep(1)
                health = run(['docker', 'inspect', '--format', '{{.State.Health.Status}}',
                              self.container_id()], self.env)
            require(health.stdout.strip() == 'healthy', 'Docker healthcheck did not reach healthy')
            print("PASS: production Compose normal-bridge loopback ephemeral port and exact health response", flush=True)
            return
        require(ui.get("environment", {}).get("CF_TUNNEL_ACCEL_UI_PROFILES") == "/config/profiles.json",
                "Compose profile-passthrough interface is not ready")
        require(ui.get("image") == resolved["services"]["fixture"].get("image") == self.image,
                "Console and fixture must use the same image")
        if self.build:
            self.dc("build", SERVICE, timeout=900)
        self.started = True  # finally covers partially successful Compose up, too.
        self.dc("up", "-d", "--no-build", "--pull", "never", SERVICE, "fixture", timeout=90)
        client = self.wait_ready()
        require(client.get("/api/session").get("authenticated") is False, "Fresh session was already authenticated")
        require(client.request("/api/state")[0] == 401, "State endpoint must not be anonymous")
        status, html = client.request("/")
        require(status == 200 and isinstance(html, str) and "<html" in html.lower(), "Real UI page was not served")
        logs = self.dc("logs", "--no-color", "--no-log-prefix", SERVICE).stdout
        matches = BOOTSTRAP.findall(logs)
        require(len(matches) == 1, "First boot must print exactly one managed bootstrap key")
        key = self.managed_key()
        require(matches[0] == key and logs.count(key) == 1, "Bootstrap key does not match managed key or was repeated")
        client.login(key)
        profiles = client.get("/api/profiles")
        require([row["id"] for row in profiles["profiles"]] == ["alpha", "beta"], "Multi-profile passthrough failed")
        for profile in ("alpha", "beta"):
            state = client.get("/api/state?fresh=1&profile=" + profile)
            require(state.get("capabilities", {}).get("local_tunnel_management") is False,
                    "Container management capability mismatch")
            require(state.get("service", {}).get("active") is None and
                    state.get("service", {}).get("state") == "unmanaged", "Tunnel service must be explicitly unmanaged")
            require(any(row["hostname"] == profile + ".example.test" for row in state["routes"]),
                    "Profile ingress read did not use mounted configuration")
            require(not any(name in state.get("errors", {}) for name in ("config", "zones", "saas", "preferred")),
                    "Mounted profile/local cloud reads returned section errors")
        self.check_rejections(client)
        job_id, snapshot = self.positive_operations(client)
        audit_before = client.get("/api/audit?limit=200")["rows"]
        require(any(row.get("event") == "job.done" and row.get("job") == job_id for row in audit_before),
                "Successful job lacks durable audit event")
        print("PASS: isolated Compose, real HTTP sessions/probe, scoped cloud job and CLI contract", flush=True)
        client = self.recreate()
        require(self.managed_key() == key, "Managed key changed on recreation")
        fresh_logs = self.dc("logs", "--no-color", "--no-log-prefix", SERVICE).stdout
        require(not BOOTSTRAP.search(fresh_logs) and key not in fresh_logs,
                "Recreated console disclosed a managed key")
        client.login(key)
        audit_after = client.get("/api/audit?limit=200")["rows"]
        require(all(row in audit_after for row in audit_before), "Audit did not survive container recreation")
        require(client.get("/api/jobs/" + job_id)["job"]["status"] == "done", "Completed job was not persisted")
        require(self.fixture_status() == snapshot, "Fixture contract state/journal did not persist")
        self.env["CF_TUNNEL_ACCEL_UI_TOKEN_FILE"] = "/config/custom-ui-key"
        client = self.recreate()
        logs = self.dc("logs", "--no-color", "--no-log-prefix", SERVICE).stdout
        require(not BOOTSTRAP.search(logs) and key not in logs and self.custom_key not in logs,
                "Explicit key startup disclosed a login key")
        Client(client.port, client.scenario).login(key, expected=401)
        client.login(self.custom_key)
        require(self.managed_key() == key, "Explicit source overwrote managed key")
        require(any(row.get("event") == "job.done" and row.get("job") == job_id
                    for row in client.get("/api/audit?limit=200")["rows"]), "Custom-key startup lost audit")
        self.check_readonly()
        print("PASS: recreation retains key/audit/jobs; read-only mounted custom key overrides managed key", flush=True)

    def cleanup(self):
        if not self.started:
            return
        # Project-specific only: no prune, --all, host cleanup, or image removal.
        result = self.dc("down", "--volumes", "--remove-orphans", "--timeout", "5", timeout=60, check=False)
        require(result.returncode == 0, "Isolated Compose cleanup failed")
        for args in (["ps", "-aq"], ["network", "ls", "-q"], ["volume", "ls", "-q"]):
            result = run(["docker"] + args + ["--filter", "label=com.docker.compose.project=" + self.project], self.env)
            require(not result.stdout.strip(), "Isolated Compose resources remain after cleanup")


def main():
    parser = argparse.ArgumentParser(description="Real container console E2E (local contract only)")
    parser.add_argument("--no-build", action="store_true", help="use existing cf-tunnel-accel:local image")
    args = parser.parse_args()
    if not shutil.which("docker"):
        print("FAIL: Docker CLI is required", file=sys.stderr)
        return 1
    failed = False
    with tempfile.TemporaryDirectory(prefix="cfa-docker-e2e-") as directory:
        scenarios = [Scenario(Path(directory) / "internal", build=not args.no_build),
                     Scenario(Path(directory) / "normal", build=False,
                              internal_network=False, smoke=True)]
        for scenario in scenarios:
            try:
                scenario.execute()
            except KeyboardInterrupt:
                print("FAIL: interrupted; cleaning isolated project", file=sys.stderr)
                failed = True
            except Failure as exc:
                print("FAIL: " + str(exc), file=sys.stderr)
                failed = True
            except Exception:
                # Never dump traceback/locals/response bodies: keys exist in memory.
                print("FAIL: unexpected contract or runtime error (details suppressed)", file=sys.stderr)
                failed = True
            finally:
                try:
                    scenario.cleanup()
                except BaseException:
                    print("FAIL: isolated Compose cleanup incomplete; project=" + scenario.project, file=sys.stderr)
                    failed = True
    if not failed:
        print("PASS: all project containers/networks/volumes removed")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
