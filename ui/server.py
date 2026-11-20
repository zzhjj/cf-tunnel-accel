#!/usr/bin/env python3
"""ARC —— cf-tunnel-accel 的 Web 控制台。

单文件、仅标准库：反向复用 cf-tunnel-accel 已有的全部云端逻辑（新增/改/删/
体检/切优选），不重写任何 Cloudflare 交互，避免两套实现互相打脸。

安全设计（首页即闸门，不是装饰）
  * 默认只监听 127.0.0.1；要对外用 --host 0.0.0.0，并知悉 token 是唯一凭据。
  * 支持外部配置的 UI 登录密钥；默认 0600 文件、恒定时间比较、不出现在页面里。
  * 登录失败按 IP 指数退避 + 锁定，全部写审计日志。
  * 会话只用 HttpOnly Cookie 保存，页面 JS 拿不到；写操作要额外带 CSRF 头，
    并校验 Origin / Sec-Fetch-Site，跨站表单打不进来。
  * 静态资源限定在 static/ 内，路径穿越一律 404；CSP 默认只允许同源。
"""

from __future__ import annotations

import argparse
import hmac
import importlib.machinery
import ipaddress
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

UI_DIR = Path(__file__).resolve().parent
STATIC_DIR = UI_DIR / "static"
def default_module_path() -> Path:
    if 'CF_TUNNEL_ACCEL_SCRIPT' in os.environ:
        return Path(os.environ['CF_TUNNEL_ACCEL_SCRIPT']).expanduser()
    adjacent = UI_DIR.parent / 'cf-tunnel-accel'
    if adjacent.is_file():
        return adjacent
    data_dir = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    candidates = (data_dir / 'cf-tunnel-accel/cf-tunnel-accel',
                  Path('/usr/local/lib/cf-tunnel-accel/cf-tunnel-accel'))
    for path in candidates:
        if path.is_file():
            return path
    return Path(shutil.which('cf-tunnel-accel') or adjacent)


DEFAULT_MODULE = default_module_path()
CONFIG_DIR = Path(os.environ.get('CF_TUNNEL_ACCEL_UI_DIR', os.environ.get(
    'CF_TUNNEL_ACCEL_CONFIG_DIR', str(Path(os.environ.get('XDG_CONFIG_HOME') or
                                           Path.home() / '.config') / 'cf-tunnel-accel')))).expanduser()
TOKEN_PATH = CONFIG_DIR / 'ui-token'  # Only the app-managed file may be created or rotated.
CDN_HISTORY_PATH = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_CDN_HISTORY", str(CONFIG_DIR / "ui-cdn-history.json"))
)
AUDIT_PATH = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_AUDIT", str(CONFIG_DIR / "ui-audit.log"))
)

JOBS_PATH = Path(os.environ.get('CF_TUNNEL_ACCEL_UI_JOBS', str(CONFIG_DIR / 'ui-jobs.json')))

HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")

SESSION_TTL = int(os.environ.get("CF_TUNNEL_ACCEL_UI_SESSION_TTL", 12 * 3600))
MAX_JOBS_KEPT = 40
MAX_JOB_LINES = 800
MAX_PENDING_JOBS = 32
SHUTDOWN_DRAIN_SECONDS = 10
MUTATING_OPS = frozenset({'add', 'update', 'delete', 'switch', 'repair', 'cdn-set', 'cdn-best'})
LOCAL_TUNNEL_OPS = frozenset({'add', 'update', 'delete'})


class LocalTunnelUnmanaged(ValueError):
    pass


def runtime_capabilities(accel) -> dict:
    container = getattr(accel, '_container_runtime', False) or getattr(accel, 'RUNTIME_MODE', 'host') == 'container'
    return {'runtime': 'container' if container else 'host',
            'local_tunnel_management': not container, 'service_discovery': not container,
            'health_scope': 'container-network' if container else 'host'}


def local_operation_guard(accel, op: str) -> None:
    if op in LOCAL_TUNNEL_OPS and not runtime_capabilities(accel)['local_tunnel_management']:
        raise LocalTunnelUnmanaged('容器不管理宿主 ingress/服务；请在宿主 CLI 执行此操作')


class JobBusy(ValueError):
    """Atomic admission rejected conflicting or excess work."""


def cache_key(accel, key: str) -> str:
    return f"{getattr(accel, '_profile_id', id(accel))}:{key}"
THROTTLE_FREE_ATTEMPTS = 3
THROTTLE_LOCK_BASE = 30
THROTTLE_LOCK_MAX = 900


# --------------------------------------------------------------------------
# 载入 cf-tunnel-accel（唯一真相来源）
# --------------------------------------------------------------------------
def load_accel(path: Path, name: str = "cf_tunnel_accel"):
    if not path.is_file():
        raise SystemExit(
            f"找不到 cf-tunnel-accel 脚本: {path}\n"
            "用 --module /实际/路径 或环境变量 CF_TUNNEL_ACCEL_SCRIPT 指定。"
        )
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 凭据 / 会话 / 审计
# --------------------------------------------------------------------------
def validate_login_key(value: str) -> str:
    if not 32 <= len(value) <= 1024 or not value.isprintable() or value != value.strip():
        raise ValueError('登录密钥须为 32–1024 个可打印字符，单行且首尾无空白')
    return value


def read_login_key(path: Path, *, managed: bool = False) -> str:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if managed:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as fh:
        mode = os.fstat(fh.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError('登录密钥文件必须是普通文件')
        raw = fh.read(4099)
        if len(raw) > 4098:
            raise ValueError('登录密钥文件过长')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise ValueError('登录密钥文件必须为 UTF-8') from None
        key = validate_login_key(text[:-2] if text.endswith('\r\n') else text.removesuffix('\n'))
        if managed and stat.S_IMODE(mode) != 0o600:
            os.fchmod(fh.fileno(), 0o600)
        return key


def load_or_create_token(*, rotate: bool = False) -> Tuple[str, bool]:
    """Atomically publish a private managed token; retain the winner on concurrent creation."""
    if not rotate:
        try:
            return read_login_key(TOKEN_PATH, managed=True), False
        except FileNotFoundError:
            pass
    elif os.path.lexists(TOKEN_PATH) and not stat.S_ISREG(TOKEN_PATH.lstat().st_mode):
        raise ValueError('仅能轮换应用管理的普通密钥文件')

    token = secrets.token_hex(24)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.ui-token-', dir=TOKEN_PATH.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(token + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        if rotate:
            os.replace(name, TOKEN_PATH)
        else:
            try:
                os.link(name, TOKEN_PATH)
            except FileExistsError:
                return read_login_key(TOKEN_PATH, managed=True), False
    finally:
        Path(name).unlink(missing_ok=True)
    return token, True


def _no_verify_for(args: dict) -> bool:
    return args.get('no_tls_verify') is True and str(args.get('service', '')).startswith('https://')


class TTLCache:
    """极简 TTL 缓存。

    控制台的 /api/state 会顺带读 zone 清单、自定义主机名、优选 CNAME，
    每次都打一遍 API 要 2–4s，页面就有“半天不出数”的体感。这里按秒缓存，
    变更尝试结束后由 JobRunner 主动失效。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: Dict[str, Tuple[float, Any]] = {}
        self.generation = 0

    def get(self, key: str, ttl: float, factory, force: bool = False):
        now = time.time()
        with self.lock:
            generation = self.generation
            if not force:
                hit = self.data.get(key)
                if hit and hit[0] > now:
                    return hit[1]
        value = factory()
        with self.lock:
            # Do not refill a cache invalidated while a read was in flight.
            if generation == self.generation:
                self.data[key] = (time.time() + ttl, value)
        return value

    def invalidate(self, *keys: str) -> None:
        with self.lock:
            self.generation += 1
            if not keys:
                self.data.clear()
                return
            for key in keys:
                self.data.pop(key, None)


CACHE = TTLCache()


class Audit:
    """JSONL 审计，永不阻塞主流程。"""

    def __init__(self, path: Path, limit: int = 300):
        self.path = path
        self.lock = threading.Lock()
        self.recent: deque = deque(maxlen=limit)
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-200:]
        except (OSError, UnicodeError):
            return
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    self.recent.append(row)
            except ValueError:
                continue

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with self.lock:
            self.recent.append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(json.dumps(row, ensure_ascii=False) + '\n')
            except OSError:
                pass

    def tail(self, limit: int = 50) -> List[dict]:
        with self.lock:
            rows = list(self.recent)
        return rows[-limit:][::-1]


class CdnHistory:
    """记住用过/见过的优选目标。

    内置候选是写死的公共列表，一旦切到列表外的目标（比如 www.shopify.com），
    下次 build_cdn 就拿不到它，界面里就再也切不回去了——这个文件正是补这个洞。
    """

    MAX = 8

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.items: List[dict] = self._load()

    def _load(self) -> List[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("targets") or []
        out = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and row.get("domain"):
                out.append({"domain": str(row["domain"]), "ts": row.get("ts") or 0})
            elif isinstance(row, str):
                out.append({"domain": row, "ts": 0})
        return out[: self.MAX]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"targets": self.items}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def note(self, domain: str) -> None:
        if not domain:
            return
        with self.lock:
            self.items = [i for i in self.items if i["domain"] != domain]
            self.items.insert(0, {"domain": domain, "ts": int(time.time())})
            self.items = self.items[: self.MAX]
            self._save()

    def domains(self) -> List[str]:
        with self.lock:
            return [i["domain"] for i in self.items]


class Sessions:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.data: Dict[str, dict] = {}

    def create(self, ip: str) -> dict:
        sid = secrets.token_hex(32)
        sess = {
            "csrf": secrets.token_hex(16),
            "exp": time.time() + self.ttl,
            "ip": ip,
            "born": time.time(),
        }
        with self.lock:
            self._gc()
            self.data[sid] = sess
        return {"sid": sid, **sess}

    def get(self, sid: Optional[str]) -> Optional[dict]:
        if not sid:
            return None
        with self.lock:
            self._gc()
            return self.data.get(sid)

    def drop(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self.lock:
            self.data.pop(sid, None)

    def _gc(self) -> None:
        now = time.time()
        for key in [k for k, v in self.data.items() if v["exp"] < now]:
            self.data.pop(key, None)


class Throttle:
    """登录失败按 IP 退避：前 3 次免费，之后 30s 起翻倍，最长 15 分钟。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: Dict[str, dict] = {}

    def check(self, ip: str) -> float:
        with self.lock:
            row = self.state.get(ip)
            if not row:
                return 0.0
            return max(0.0, row.get("until", 0.0) - time.time())

    def fail(self, ip: str) -> float:
        with self.lock:
            row = self.state.setdefault(ip, {"fails": 0, "until": 0.0})
            row["fails"] += 1
            extra = row["fails"] - THROTTLE_FREE_ATTEMPTS
            if extra <= 0:
                return 0.0
            wait = min(THROTTLE_LOCK_MAX, THROTTLE_LOCK_BASE * (2 ** (extra - 1)))
            row["until"] = time.time() + wait
            return wait

    def reset(self, ip: str) -> None:
        with self.lock:
            self.state.pop(ip, None)


# --------------------------------------------------------------------------
# 本机服务发现
# --------------------------------------------------------------------------
SS_USER_RE = re.compile(r'\("([^"]+)",pid=(\d+)')


def parse_ss_listen(text: str) -> List[dict]:
    """解析 `ss -ltnp` 输出为 [{addr,port,process,pid}]。"""
    rows: List[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].startswith("LISTEN"):
            continue
        local = parts[3]
        if local.startswith("[") and "]:" in local:
            addr, _, port = local.rpartition("]:")
            addr = addr.lstrip("[")
        elif ":" in local:
            addr, _, port = local.rpartition(":")
        else:
            continue
        if not port.isdigit():
            continue
        proc, pid = "", 0
        m = SS_USER_RE.search(line)
        if m:
            proc, pid = m.group(1), int(m.group(2))
        rows.append({"addr": addr, "port": int(port), "process": proc, "pid": pid})
    return rows


def _friendly_name(proc: str, port: int) -> str:
    table = {
        "nginx": "nginx",
        "autman": "autMan",
        "cloudflared": "cloudflared",
        "python": "python",
        "node": "node",
        "docker-proxy": "docker",
        "redis-server": "redis",
        "mysqld": "mysql",
        "postgres": "postgres",
        "java": "java",
        "xray": "xray",
        "x-ui": "x-ui",
        "sshd": "ssh",
        "caddy": "caddy",
        "php-fpm": "php-fpm",
    }
    base = os.path.basename(proc or "").lower()
    for key, nice in table.items():
        if base.startswith(key):
            return nice
    return base or f"port {port}"


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _listener_addr(addr: str) -> str:
    if addr == '*':
        return '0.0.0.0'
    try:
        return str(ipaddress.ip_address(addr))
    except ValueError:
        return addr


def _addr_family(addr: str) -> int:
    try:
        return ipaddress.ip_address(addr).version
    except ValueError:
        return 0


def _local_match(host: str, addr: str) -> bool:
    # Do not resolve arbitrary ingress hosts: they may be remote upstreams,
    # and a wildcard listener alone does not make a remote IP local.
    host = {'localhost': '127.0.0.1'}.get(host, host)
    if host == addr or host == addr.partition('%')[0]:
        return True
    if addr in ('0.0.0.0', '::'):
        try:
            return _addr_family(addr) == _addr_family(host) and ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return False


def _http_address(addr: str) -> str:
    return f'[{addr.replace("%", "%25")}]' if ':' in addr else addr


def discover_services(accel, with_probe: bool = True, *, warnings: Optional[List[str]] = None) -> List[dict]:
    """List local listeners, preserving endpoint and process identity."""
    warnings = warnings if warnings is not None else []
    if not runtime_capabilities(accel)['service_discovery']:
        warnings.append('容器视角不枚举宿主监听；请使用服务 DNS 名或宿主网关进行显式后端探测。')
        return []
    ss = shutil.which('ss')
    if not ss:
        warnings.append('缺少 ss（iproute2），无法读取本机监听服务。')
        return []
    try:
        proc = subprocess.run([ss, '-ltnp'], capture_output=True, text=True, timeout=6)
        if proc.returncode:
            warnings.append(f'ss 读取监听服务失败（退出码 {proc.returncode}）')
            return []
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f'ss 读取监听服务失败: {exc}')
        return []

    merged: Dict[tuple, dict] = {}
    for row in parse_ss_listen(proc.stdout):
        addr = _listener_addr(row['addr'])
        # Only coalesce wildcard dual-stack rows when they share a known owner.
        # Equal port alone (or equal executable name across PIDs) is not identity.
        endpoint = 'wildcard' if row['pid'] and addr in ('0.0.0.0', '::') else addr
        key = (endpoint, row['port'], row['pid'], row['process'])
        cur = merged.setdefault(key, dict(row, addr=addr, addrs=[]))
        if addr not in cur['addrs']:
            cur['addrs'].append(addr)
    rows = list(merged.values())

    try:
        ingress = read_ingress(accel)
    except Exception as exc:
        warnings.append(f'无法读取 ingress 映射: {exc}')
        ingress = []
    routes = []
    for entry in ingress:
        try:
            host = validate_hostname(entry['hostname'])
            service = validate_service(entry['service'])
            parsed = urllib.parse.urlsplit(service)
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        except (KeyError, ValueError):
            continue
        routes.append((host, service, _listener_addr(urllib.parse.unquote(parsed.hostname)),
                       port, bool(entry.get('no_tls_verify'))))

    for row in rows:
        matches = [(host, service, skip) for host, service, addr, port, skip in routes
                   if port == row['port'] and any(_local_match(addr, a) for a in row['addrs'])]
        row['mapped'] = sorted({host for host, _, _ in matches})
        row['addr'] = row['addrs'][0] if len(row['addrs']) == 1 else 'any'
        row['process'] = re.sub(r'[^a-zA-Z0-9_.+-]', '_', os.path.basename(row['process']))[:64]
        row['friendly'] = _friendly_name(row['process'], row['port'])
        # Do not read /proc/<pid>/cmdline: arguments may contain credentials.
        row['cmd'] = row['process']
        row['loopback_only'] = all(_is_loopback(a) for a in row['addrs'])
        row['endpoints'] = sorted({url for _, url, _ in matches})
        if not matches:
            addr = row['addrs'][0]
            addr = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(addr, addr)
            matches = [(None, f"http://{_http_address(addr)}:{row['port']}", False)]
        row['_probe_targets'] = list(dict.fromkeys(matches))[:4]
    rows.sort(key=lambda r: (not r['mapped'], r['port'], r['addr']))

    if with_probe and rows:
        def probe_one(host, url, skip):
            try:
                ok, detail = accel.probe_local_backend(url, timeout=0.7, host_header=host,
                                                       no_tls_verify=skip)
            except Exception as exc:
                ok, detail = False, str(exc)
            return {'ok': bool(ok), 'detail': detail, 'service': url, 'hostname': host}

        def probe(row):
            hits = [CACHE.get(cache_key(accel, f"probe:{row['pid']}:{url}:{host}:{skip}"),
                              12.0, lambda: probe_one(host, url, skip))
                    for host, url, skip in row['_probe_targets']]
            row['probe'] = next((hit for hit in hits if hit['ok']), hits[0])
            row['probes'] = hits

        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(probe, rows[:220]))
    for row in rows:
        row.pop('_probe_targets')
    return rows

# --------------------------------------------------------------------------
# 任务队列（单一 worker，保证隧道重启不会互相踩）
# --------------------------------------------------------------------------
def _finite(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _ms(value: Any) -> str:
    value = _finite(value)
    return '-' if value is None else f'{value:.2f}ms'


class Job:
    def __init__(self, op: str, args: dict, actor: str, profile: str = 'default'):
        self.id = secrets.token_hex(8)
        self.op = op
        self.profile = profile
        self.args = args
        self.actor = actor
        self.status = 'queued'
        self.outcome: Optional[str] = None
        self.lines: List[dict] = []
        self.next_line = 0
        self.result: Any = None
        self.error: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.lock = threading.Lock()

    def log(self, text: str) -> None:
        text = re.sub(r'\x1b\[[0-9;]*m', '', str(text)).rstrip()[:4000]
        if not text:
            return
        with self.lock:
            self.lines.append({'t': round(time.time() - self.created, 2), 'm': text})
            self.next_line += 1
            if len(self.lines) > MAX_JOB_LINES:
                del self.lines[: len(self.lines) - MAX_JOB_LINES]

    def snapshot(self, since: int = 0, *, include_lines: bool = True) -> dict:
        with self.lock:
            offset = self.next_line - len(self.lines)
            return {
                'id': self.id, 'op': self.op, 'profile': self.profile, 'actor': self.actor,
                'status': self.status, 'outcome': self.outcome,
                'lines': self.lines[max(0, since - offset):] if include_lines else [],
                'offset': offset, 'next': self.next_line,
                'result': self.result, 'error': self.error,
                'created': self.created, 'started': self.started, 'ended': self.ended,
                'elapsed': round((self.ended or time.time()) - (self.started or self.created), 1),
            }

    @classmethod
    def restore(cls, row: dict) -> Job:
        # Only display snapshots are retained. In particular no dispatch args
        # are loaded, even if a previous store happened to contain them.
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k]
                                                  for k in ('id', 'op', 'profile', 'status')):
            raise ValueError('任务快照格式错误')
        if row['status'] not in {'queued', 'running', 'done', 'error', 'interrupted'}:
            raise ValueError('任务快照状态未知')
        lines, cursor = row['lines'], row['next']
        if (not isinstance(lines, list) or not isinstance(cursor, int) or cursor < len(lines)
                or not all(isinstance(l, dict) and isinstance(l.get('m'), str)
                           and isinstance(l.get('t'), (int, float)) for l in lines)):
            raise ValueError('任务快照日志格式错误')
        for key in ('created', 'started', 'ended'):
            if (key == 'created' or row.get(key) is not None) and not isinstance(row.get(key), (int, float)):
                raise ValueError('任务快照时间格式错误')
        job = cls(row['op'], {}, row.get('actor') or '', row['profile'])
        for key in ('id', 'status', 'outcome', 'created', 'started', 'ended', 'result', 'error'):
            setattr(job, key, row.get(key))
        job.lines, job.next_line = lines[-MAX_JOB_LINES:], cursor
        return job


def _interrupt_job(job: Job, *, restarted: bool = False) -> None:
    message = ('控制台重启，任务结果未确认；请核对实际状态。任务不会自动重试。'
               if restarted else '控制台停止，排队任务未执行；任务不会自动重试。')
    job.log(message)
    with job.lock:
        job.error = message
        job.outcome = 'interrupted'
        job.ended = time.time()
        job.status = 'interrupted'


class JobRunner:
    def __init__(self, accel, audit: Audit, profiles=None, *, jobs_path: Optional[Path] = None):
        self.profiles = profiles or {'default': accel}
        self.audit = audit
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.queue: deque = deque()
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.closed = False
        self.jobs_path = Path(jobs_path) if jobs_path is not None else JOBS_PATH
        self.store_error: Optional[str] = None
        self.saved_cursors: Dict[str, int] = {}
        self._restore()
        self.worker = threading.Thread(target=self._loop, name='arc-jobs', daemon=True)
        self.worker.start()

    def _audit(self, event: str, job: Job, **fields: Any) -> None:
        self.audit.write(event, profile=job.profile, op=job.op, job=job.id,
                         actor=job.actor, **fields)

    def _report_store_error(self, action: str, exc: Exception) -> None:
        message = f'任务记录{action}失败 ({self.jobs_path}): {exc}'
        if self.store_error != message:
            self.store_error = message
            self.audit.write('job.store_error', error=message)
            print(f'ARC: {message}', file=sys.stderr)

    def _restore(self) -> None:
        try:
            with self.jobs_path.open(encoding='utf-8') as fh:
                os.fchmod(fh.fileno(), 0o600)
                data = json.load(fh)
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('jobs'), list):
                raise ValueError('不支持的任务快照格式')
            loaded = [Job.restore(row) for row in data['jobs'][-MAX_JOBS_KEPT:]]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._report_store_error('读取', exc)
            return
        changed = False
        for job in loaded:
            self.jobs[job.id] = job
            self.saved_cursors[job.id] = job.next_line
            if job.status in ('queued', 'running'):
                _interrupt_job(job, restarted=True)
                self._audit('job.interrupted', job, error=job.error)
                changed = True
        if changed:
            self._persist()

    def _persist(self) -> None:
        """Atomic, private snapshots at lifecycle transitions; hold runner lock."""
        path, temp = self.jobs_path, None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshots = [job.snapshot() for job in self.jobs.values()]
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=f'.{path.name}.', delete=False) as fh:
                temp = Path(fh.name)  # NamedTemporaryFile starts at mode 0600.
                json.dump({'version': 1, 'jobs': snapshots}, fh, ensure_ascii=False, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            self.saved_cursors = {snap['id']: snap['next'] for snap in snapshots}
            self.store_error = None
        except (OSError, ValueError, TypeError) as exc:
            self._report_store_error('保存', exc)
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def submit(self, op: str, args: dict, actor: str, profile: str = 'default') -> Job:
        if profile not in self.profiles:
            raise ValueError('未知 profile ID')
        local_operation_guard(self.profiles[profile], op)
        job = Job(op, dict(args), actor, profile)
        with self.cv:
            if self.closed:
                raise JobBusy('控制台正在停止，已停止接收任务')
            if profile not in self.profiles:
                raise ValueError('未知 profile ID')
            pending = [j for j in self.jobs.values() if j.status in ('queued', 'running')]
            if len(pending) >= MAX_PENDING_JOBS:
                raise JobBusy('任务队列已满，请稍后重试')
            if op in MUTATING_OPS and any(j.op in MUTATING_OPS for j in pending):
                raise JobBusy('已有变更任务进行中，请等待完成')
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS_KEPT:
                old = next((j.id for j in self.jobs.values() if j.status not in ('queued', 'running')), None)
                if not old:
                    break
                del self.jobs[old]
            self.queue.append(job.id)
            # Audit only selected non-secret identifiers, never raw request args.
            context = {k: args[k] for k in ('hostname', 'target') if k in args}
            self._audit('job.submit', job, **context)
            self._persist()
            self.cv.notify()
        return job

    def close(self, drain: bool = True, timeout: Optional[float] = None) -> bool:
        """Stop admission; optionally finish queued work. Return whether joined."""
        with self.cv:
            self.closed = True
            cancelled = bool(self.queue) and not drain
            if cancelled:
                while self.queue:
                    job = self.jobs[self.queue.popleft()]
                    _interrupt_job(job)
                    self._audit('job.interrupted', job, error=job.error)
                self._persist()
            self.cv.notify_all()
        self.worker.join(timeout)
        return not self.worker.is_alive()

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def _active(self) -> Optional[str]:
        return next((job.id for job in self.jobs.values() if job.status == 'running'), None)

    def active(self) -> Optional[str]:
        with self.lock:
            return self._active()

    def _checkpoint_visible_logs(self, snapshots) -> None:
        # Persist cursors before publishing them: the browser may reconnect to
        # this task after a crash and ask for lines using the last seen cursor.
        if any(snap['next'] > self.saved_cursors.get(snap['id'], 0) for snap in snapshots):
            self._persist()

    def detail(self, job_id: str, since: int = 0) -> Optional[dict]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            snap = job.snapshot(since)
            self._checkpoint_visible_logs([snap])
            return {'job': snap, 'active': self._active(), 'store_error': self.store_error}

    def list_jobs(self) -> dict:
        with self.lock:
            snapshots = [job.snapshot(include_lines=False) for job in reversed(self.jobs.values())]
            self._checkpoint_visible_logs(snapshots)
            return {'jobs': snapshots, 'active': self._active(), 'store_error': self.store_error}

    def _loop(self) -> None:
        while True:
            with self.cv:
                self.cv.wait_for(lambda: self.closed or self.queue)
                if not self.queue:
                    return
                job = self.jobs[self.queue.popleft()]
                with job.lock:
                    job.started = time.time()
                    job.status = 'running'
                self._persist()
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        accel = self.profiles[job.profile]
        original = accel.cprint

        def tee(msg: str = '', color: Optional[str] = None) -> None:
            if threading.current_thread() is self.worker:
                job.log(msg)
            original(msg, color)

        accel.cprint = tee
        status, outcome, result, error = 'done', 'ok', None, None
        try:
            if job.op in MUTATING_OPS:
                # The only no-lock path is for old lightweight test doubles.
                lock = getattr(accel, 'exclusive_lock', None)
                prepare = getattr(accel, 'prepare_configuration', None)
                if isinstance(accel, ModuleType) and (lock is None or prepare is None):
                    raise RuntimeError('CLI 缺少变更锁或配置预检；请更新配套 CLI')
                if job.op == 'add':
                    reject_external_add(job.args)
                with lock() if lock is not None else nullcontext():
                    if prepare is not None:
                        result = prepare(require_local=job.op in {'add', 'update', 'delete'})
                    if not isinstance(result, dict) or result.get('ok') is not False:
                        result = self._dispatch(job)
            else:
                result = self._dispatch(job)
            if isinstance(result, dict) and result.get('ok') is False:
                if job.op == 'health':
                    outcome = 'unhealthy'
                else:
                    status, outcome = 'error', 'failed'
                    error = str(result.get('error') or result.get('message') or '操作未完成；请检查日志和实际状态')
        except (Exception, SystemExit) as exc:
            status, outcome = 'error', 'failed'
            error = str(exc) or exc.__class__.__name__
            if os.environ.get('ARC_DEBUG'):
                job.log(traceback.format_exc())
        finally:
            accel.cprint = original
            if job.op in MUTATING_OPS:
                # A failure can still have changed a DNS record or config file.
                CACHE.invalidate()
        if error:
            job.log(f'错误: {error}')
        with self.cv:
            self._audit(f'job.{status}', job, outcome=outcome, **({'error': error} if error else {}))
            with job.lock:
                job.result, job.error, job.outcome = result, error, outcome
                job.ended = time.time()
                job.status = status
            self._persist()

    def _cdn_bench(self, job: Job) -> dict:
        """只测速，绝不改 DNS。"""
        accel = self.profiles[getattr(job, 'profile', 'default')]
        try:
            current = read_preferred_target(accel)
        except Exception as exc:
            current = None
            job.log(f'优选入口未确认: {exc}')
        domains = candidate_domains(accel, current)
        if not domains:
            raise ValueError('没有配置候选 CDN；请检查 cf-tunnel-accel env')
        job.log(f"当前优选目标: {current or '(未知)'}")
        job.log(f"候选 {len(domains)} 个: {', '.join(domains)}")
        raw = []
        for i, domain in enumerate(domains, 1):
            job.log(f"[{i}/{len(domains)}] 测速 {domain} …")
            try:
                raw.append(accel.benchmark_cdn(domain))
            except Exception as exc:  # noqa: BLE001
                job.log(f"    跳过 {domain}: {exc}")
        if not raw:
            raise RuntimeError("没有测出任何候选 CDN 的可用样本")
        ranked = accel.rank_cdn_results(raw)
        out = []
        for row in ranked:
            score = _finite(accel.cdn_latency_score(row))
            eligible = getattr(accel, 'cdn_eligible', None)
            usable = (bool(eligible(row)) if eligible else
                      bool(row.get('https_ok')) and score is not None and row.get('median_https') is not None)
            out.append(
                {
                    "domain": row["domain"],
                    "current": row["domain"] == current,
                    "usable": usable, "eligible": usable,
                    'business_verified': row.get('business_verified') is True,
                    'probe_hostname': row.get('probe_hostname'),
                    "score": score,
                    "median_https": _finite(row.get("median_https")),
                    "median_tcp": _finite(row.get("median_tcp")),
                    "avg_ttfb": _finite(row.get("avg_ttfb")),
                    "best_ping": _finite(row.get("best_ping")),
                    "success_rate": _finite(row.get("https_success_rate")),
                    "https_ok": _finite(row.get("https_ok")),
                    "https_tries": _finite(row.get("https_tries")),
                    "ip_count": _finite(row.get("ip_count")),
                }
            )
            mark = " [当前]" if row["domain"] == current else ""
            job.log(
                f"    {row['domain']}{mark}  "
                f"https={_ms(row.get('median_https'))}  "
                f"ttfb={_ms(row.get('avg_ttfb'))}  "
                f"ok={row.get('https_ok')}/{row.get('https_tries')}  "
                f"ips={row.get('ip_count')}"
            )
        best = next((r["domain"] for r in out if r["usable"]), None)
        job.log(f"本机测速最优: {best or '(无)'}")
        limit = getattr(accel, "CDN_SWITCH_MIN_IMPROVEMENT_PERCENT", 15.0)
        job.log(f"自动切换阈值: 需改善 ≥ {limit:.1f}%")
        return {"current": current, "best": best, "ranked": out, "min_improvement": limit}

    def _dispatch(self, job: Job) -> Any:
        accel = self.profiles[getattr(job, 'profile', 'default')]
        local_operation_guard(accel, job.op)
        a = job.args
        if job.op == "add":
            result = accel.add_accelerated_service(
                a["hostname"],
                a["service"],
                external=False,
                no_tls_verify=_no_verify_for(a),
            )
            return merge_result(result, hostname=a['hostname'])
        if job.op == "update":
            before = accel.read_config_text()
            changed = accel.upsert_tunnel_hostname(
                a["hostname"], a["service"], _no_verify_for(a)
            )
            if changed:
                result = accel.validate_and_restart_tunnel(rollback_text=before)
                return merge_result(result, hostname=a['hostname'], changed=True)
            else:
                job.log("隧道配置已是这个后端，跳过重启。")
            return {"hostname": a["hostname"], "changed": bool(changed)}
        if job.op == "delete":
            return merge_result(accel.remove_service(a['hostname']), hostname=a['hostname'])
        if job.op in ('switch', 'repair'):
            return merge_result(accel.switch_to_preferred(a['hostname']), hostname=a['hostname'])
        if job.op == "health":
            ok = accel.health_check(a.get("hostname") or None, include_https=bool(a.get("https")))
            result = merge_result(ok, ok=bool(ok), hostname=a.get('hostname'))
            if runtime_capabilities(accel)['runtime'] == 'container':
                result.update(partial=True, scope='container-network',
                              unchecked=['host-systemd', 'host-connector-config'])
            return result
        if job.op == "cdn-test":
            return self._cdn_bench(job)
        if job.op == "cdn-set":
            history = cdn_history(accel)
            before = read_preferred_target(accel)
            history.note(before)
            result = accel.set_preferred_target(a['target'])
            out = merge_result(result, before=before, target=a['target'], changed=before != a['target'])
            if out.get('ok') is not False:
                history.note(out.get('target'))
            return out
        if job.op == "cdn-best":
            history = cdn_history(accel)
            before = read_preferred_target(accel)
            history.note(before)
            result = accel.test_and_maybe_switch_cdn(
                auto_apply_best=True, interactive=False, force=bool(a.get("force"))
            )
            chosen = result.get('target', before) if isinstance(result, dict) else result
            out = merge_result(result, before=before, target=chosen, changed=chosen != before)
            if out.get('ok') is not False:
                history.note(chosen)
            return out
        raise ValueError(f"未知任务类型: {job.op}")


# --------------------------------------------------------------------------
# 状态聚合
# --------------------------------------------------------------------------
def environment_status(accel) -> dict:
    status = getattr(accel, 'configuration_status', None)
    if status is not None:
        report = status()  # Pure local, intentionally independent of the API.
        return {**report, 'capabilities': runtime_capabilities(accel)}
    # Legacy test doubles/CLI expose less metadata; never infer API access.
    return {'ready': True, 'missing': [],
            'config': {'path': str(getattr(accel, 'CONFIG_PATH', '') or ''),
                       'service': getattr(accel, 'SERVICE_NAME', None)},
            'credentials': {'configured': None, 'source': None},
            'dependencies': {'ss': bool(shutil.which('ss'))}}


def read_ingress(accel) -> List[dict]:
    text = accel.read_config_text()
    if not text:
        hint = getattr(accel, 'config_missing_hint', None)
        raise ValueError(hint() if hint else '隧道配置缺失或为空；请检查 cf-tunnel-accel env')
    return accel.parse_ingress_entries(text)


def require_cloud_read(accel, *, auxiliary: bool = False) -> None:
    if hasattr(accel, 'CF_API_TOKEN') and not accel.CF_API_TOKEN:
        raise ValueError('Cloudflare 凭据未配置；运行 cf-tunnel-accel env 检查来源')
    if (auxiliary and not hasattr(accel, 'resolve_aux_zone_id') and
            hasattr(accel, 'AUX_ZONE_ID') and not accel.AUX_ZONE_ID):
        raise ValueError('辅助域 zone ID 未配置；运行 cf-tunnel-accel env 检查配置')


def read_preferred_target(accel) -> Optional[str]:
    require_cloud_read(accel, auxiliary=True)
    if hasattr(accel, 'list_dns'):
        # Legacy strict getter distinguishes API failures but returns its
        # fallback when records are absent. The UI must show the actual DNS.
        entry = accel.PREFERRED_ENTRY
        if not entry:
            raise ValueError('优选入口未配置；运行 cf-tunnel-accel env 检查配置')
        resolver = getattr(accel, 'resolve_aux_zone_id', None)
        zone_id = resolver() if resolver is not None else accel.AUX_ZONE_ID
        records = accel.list_dns(zone_id, entry)
        targets = {str(rec['content']).lower().rstrip('.') for rec in records
                   if rec.get('type') == 'CNAME' and rec.get('content')
                   and str(rec.get('name') or '').lower().rstrip('.') == entry.lower().rstrip('.')}
        if len(targets) > 1:
            raise ValueError('优选入口存在多个不同的 CNAME 目标')
        return next(iter(targets), None)
    return accel.get_current_preferred_target(strict=True) or None


def candidate_domains(accel, current: Optional[str]) -> list:
    if hasattr(accel, 'CDN_CANDIDATES'):
        # Network-only testing and candidate display must not depend on API
        # reads. The generalized CLI validates candidates at configuration.
        candidates = list(getattr(accel, 'CDN_CANDIDATES', []))
    else:
        candidates = accel.ensure_candidate_list()
    return list(dict.fromkeys(([current] if current else []) + list(candidates)))[:64]


def _zone_info(accel) -> dict:
    require_cloud_read(accel)
    if hasattr(accel, 'zone_cache_age') and hasattr(accel, 'refresh_zone_cache'):
        age = accel.zone_cache_age()
        if age is None or age > accel.ZONE_CACHE_TTL:
            # ensure_zone_cache can silently fall back to stale zones; here
            # propagate a refresh error instead of confirming zone ownership.
            accel.refresh_zone_cache()
    else:
        accel.ensure_zone_cache()
    return dict(accel._zone_info)


def _zone_for(host: str, zones: dict) -> Optional[str]:
    labels = host.lower().rstrip('.').split('.')
    return next((domain for i in range(len(labels) - 1)
                 if (domain := '.'.join(labels[i:])) in zones), None)


def _check_section(errors: dict, freshness: dict, name: str, reader, default=None):
    try:
        value = reader()
    except Exception as exc:
        errors[name] = str(exc) or type(exc).__name__
        freshness[name] = None
        return default
    freshness[name] = time.time()
    return value


def _service_status(accel) -> dict:
    if not runtime_capabilities(accel)['local_tunnel_management']:
        return {'name': accel.SERVICE_NAME, 'active': None, 'state': 'unmanaged', 'mainpid': None}
    name = accel.SERVICE_NAME
    if not name:
        raise ValueError('未配置 Tunnel 服务名称；请检查 cf-tunnel-accel env')
    active, state = accel._systemd_active()
    p = subprocess.run(['systemctl', 'show', name, '-p', 'MainPID', '--value'],
                       capture_output=True, text=True, timeout=6)
    if p.returncode:
        raise RuntimeError(f'无法读取 systemd 服务 {name}: {p.stderr.strip() or state}')
    return {'name': name, 'active': active, 'state': state, 'mainpid': int(p.stdout.strip() or 0)}


def build_state(accel, force: bool = False) -> dict:
    return CACHE.get(cache_key(accel, 'state'), 5.0, lambda: _build_state(accel), force=force)


def _build_state(accel) -> dict:
    errors: Dict[str, str] = {}
    freshness: dict = {}

    def section(name, reader, default=None):
        return _check_section(errors, freshness, name, reader, default)

    def custom_hostnames():
        require_cloud_read(accel, auxiliary=True)
        return {str(i.get('hostname')).lower(): i for i in accel.list_custom_hostnames()}

    entries = section('config', lambda: read_ingress(accel), [])
    zones = section('zones', lambda: _zone_info(accel), {})
    custom = section('saas', custom_hostnames, {})
    service = section('service', lambda: _service_status(accel),
                      {'name': accel.SERVICE_NAME, 'active': None, 'mainpid': None})
    preferred = section('preferred', lambda: read_preferred_target(accel))
    readiness = section('environment', lambda: environment_status(accel), {'ready': False})
    rows = []
    for entry in entries:
        host = entry['hostname']
        if host == accel.FALLBACK_ORIGIN:
            continue
        ch = custom.get(host.lower())
        zone = _zone_for(host, zones)
        rows.append({'hostname': host, 'service': entry['service'],
                     'no_tls_verify': bool(entry.get('no_tls_verify')), 'zone': zone,
                     'external': zone is None if 'zones' not in errors else None,
                     'saas': {'status': ch.get('status'), 'ssl': (ch.get('ssl') or {}).get('status')} if ch else None})
    rows.sort(key=lambda r: r['hostname'])
    return {
        'capabilities': runtime_capabilities(accel),
        'version': getattr(accel, 'SCRIPT_VERSION', '?'),
        'service': service, 'config_path': str(accel.CONFIG_PATH or ''),
        'preferred': {'entry': accel.PREFERRED_ENTRY or None, 'target': preferred,
                      'verified': preferred is not None and 'preferred' not in errors},
        'fallback': accel.FALLBACK_ORIGIN or None,
        'zones': [{'name': k, 'id': str(v.get('id') or ''), 'status': v.get('status')}
                  for k, v in sorted(zones.items())],
        'routes': rows,
        'counts': {'routes': len(rows) if 'config' not in errors else None,
                   'zones': len(zones) if 'zones' not in errors else None,
                   'saas': len(custom) if 'saas' not in errors else None},
        'errors': errors, 'freshness': freshness, 'updated_at': time.time(),
        'readiness': {'ready': bool(readiness['ready']), 'missing': readiness.get('missing', [])},
    }


CDN_HISTORY = CdnHistory(CDN_HISTORY_PATH)


def cdn_history(accel):
    return getattr(accel, '_cdn_history', CDN_HISTORY)


def build_cdn(accel, force: bool = False) -> dict:
    return CACHE.get(cache_key(accel, 'cdn'), 10.0, lambda: _build_cdn(accel), force=force)


def _build_cdn(accel) -> dict:
    """Read actual DNS and configured candidates independently."""
    errors: dict = {}
    freshness: dict = {}
    current = _check_section(errors, freshness, 'target', lambda: read_preferred_target(accel))
    candidates = _check_section(errors, freshness, 'candidates', lambda: candidate_domains(accel, current), [])
    builtin = set(candidates)
    for domain in cdn_history(accel).domains():
        if domain not in builtin:
            candidates.append(domain)
    return {
        'entry': accel.PREFERRED_ENTRY or None, 'target': current,
        'verified': current is not None and 'target' not in errors,
        'candidates': [{'domain': d, 'current': d == current, 'builtin': d in builtin} for d in candidates],
        'min_improvement': getattr(accel, 'CDN_SWITCH_MIN_IMPROVEMENT_PERCENT', 15.0),
        'errors': errors, 'freshness': freshness, 'updated_at': time.time(),
    }


def build_host(accel, hostname: str, with_https: bool = False) -> dict:
    errors: dict = {}
    freshness: dict = {}

    def section(name, reader, default=None):
        return _check_section(errors, freshness, name, reader, default)

    def saas():
        require_cloud_read(accel, auxiliary=True)
        return accel.get_custom_hostname(hostname)

    entries = section('config', lambda: read_ingress(accel), [])
    entry = next((e for e in entries if e['hostname'].lower() == hostname), {})
    zones = section('zones', lambda: _zone_info(accel), {})
    zone = _zone_for(hostname, zones)
    zone_id = zones[zone].get('id') if zone else None
    out = {'hostname': hostname, 'service': entry.get('service'),
           'no_tls_verify': bool(entry.get('no_tls_verify')), 'zone': zone, 'zone_id': zone_id}
    ch = section('saas', saas)
    if ch:
        out['saas'] = {'id': ch.get('id'), 'status': ch.get('status'),
                       'ssl': (ch.get('ssl') or {}).get('status'),
                       'errors': [e.get('message') for e in (ch.get('verification_errors') or [])][:4]}
    if zone_id:
        def dns():
            return {'cname': accel.get_dns_record_content(zone_id, hostname, 'CNAME'),
                    'a': accel.get_dns_record_content(zone_id, hostname, 'A')}
        out['dns'] = section('dns', dns)
    if out['service']:
        def probe():
            ok, detail = accel.probe_local_backend(out['service'], timeout=3, host_header=hostname,
                                                   no_tls_verify=out['no_tls_verify'])
            return {'ok': bool(ok), 'detail': detail}
        out['probe'] = section('probe', probe)
    if with_https:
        def https():
            ok, detail = accel.verify_https(hostname, timeout=12)
            return {'ok': bool(ok), 'detail': detail[-1200:]}
        out['https'] = section('https', https)
    out.update({f'{section}_error': error for section, error in errors.items()})
    return {**out, 'errors': errors, 'freshness': freshness, 'updated_at': time.time()}


def validate_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('域名必须为字符串')
    host = value.strip().lower().rstrip('.')
    if not HOSTNAME_RE.fullmatch(host):
        raise ValueError('域名格式不合法')
    return host


def validate_service(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('后端地址必须为字符串')
    service = value.strip()
    if not service or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in service):
        raise ValueError('后端地址为空或包含空白字符')
    if re.fullmatch(r':?[0-9]{1,5}', service):
        service = f"127.0.0.1:{service.lstrip(':')}"
    if '://' not in service:
        service = f'http://{service}'
    try:
        url = urllib.parse.urlsplit(service)
        host, port = url.hostname, url.port
        if url.scheme not in ('http', 'https') or not host or '@' in url.netloc or '#' in service:
            raise ValueError('后端要写成 http://127.0.0.1:8080 或 https://[::1]/path')
        if url.netloc.endswith(':') or port is not None and not 1 <= port <= 65535:
            raise ValueError('端口超出范围')
        if ':' in host:
            if not re.fullmatch(r'\[[^\]]+\](?::[0-9]+)?', url.netloc):
                raise ValueError('IPv6 地址必须使用括号')
            address, scoped, scope = host.partition('%25')
            host = ipaddress.IPv6Address(address).compressed
            if scoped:
                if not re.fullmatch(r'[a-zA-Z0-9_.~-]+', scope):
                    raise ValueError('IPv6 scope 格式不合法')
                host += '%25' + scope
            host = f'[{host}]'
        elif re.fullmatch(r'[a-zA-Z0-9.-]+', host) and '[' not in url.netloc:
            host = '127.0.0.1' if host == 'localhost' else host
        else:
            raise ValueError('后端主机格式不合法')
        path = url.path
        netloc = host if port is None else f'{host}:{port}'
        return urllib.parse.urlunsplit((url.scheme, netloc, path, url.query, ''))
    except ValueError as exc:
        raise ValueError(f'后端地址不合法: {exc}') from None


def reject_external_add(args: dict) -> None:
    if args.get('external'):
        raise ValueError('外部域名需要在终端手动完成 DNS 验证；请使用 CLI: cf-tunnel-accel add HOST SERVICE --external')


def merge_result(value, **defaults) -> dict:
    return {**defaults, **value} if isinstance(value, dict) else defaults

# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".png": "image/png",
}

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


class App:
    def __init__(self, accel, token: str, host: str, port: int, token_created: bool, profiles=None, *, container: bool = False):
        self.accel = accel
        self.profiles = profiles or {'default': accel}
        self.container = container or runtime_capabilities(accel)['runtime'] == 'container'
        for key, module in self.profiles.items():
            module._container_runtime = self.container
            module._profile_id = key
            metadata = getattr(module, '_profile_metadata', {})
            module._profile_name = metadata.get('name', key)
            if len(self.profiles) > 1:
                history = CDN_HISTORY_PATH.with_name(f'{CDN_HISTORY_PATH.stem}-{key}{CDN_HISTORY_PATH.suffix}')
                module._cdn_history = CdnHistory(history)
        self.token = token
        self.host = host
        self.port = port
        self.token_created = token_created
        self.sessions = Sessions(SESSION_TTL)
        self.throttle = Throttle()
        self.audit = Audit(AUDIT_PATH)
        self.runner = JobRunner(accel, self.audit, self.profiles)
        self.started = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "ARC"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App  # 由 make_server 注入

    # ---- 基础工具 ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音：自己写审计
        if os.environ.get("ARC_DEBUG"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _ip(self) -> str:
        return self.client_address[0]

    def _cookies(self) -> SimpleCookie:
        raw = self.headers.get("Cookie", "")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001
            return SimpleCookie()
        return jar

    def _sid(self) -> Optional[str]:
        jar = self._cookies()
        return jar["arc_sid"].value if "arc_sid" in jar else None

    def _session(self) -> Optional[dict]:
        return self.app.sessions.get(self._sid())

    def _send(self, status: int, body: bytes, headers=None) -> None:
        self.send_response(status)
        items = headers.items() if isinstance(headers, dict) else (headers or [])
        for key, value in items:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        base = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Security-Policy", CSP),
        ]
        extra = headers.items() if isinstance(headers, dict) else (headers or [])
        self._send(status, body, base + list(extra))

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._json(status, {"ok": False, "error": message, **extra})

    def _body(self) -> dict:
        self.close_connection = True  # Never reuse a connection with unread/rejected bytes.
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('不支持 Transfer-Encoding')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1:
            raise ValueError('需要唯一 Content-Length')
        try:
            length = int(lengths[0])
        except ValueError:
            raise ValueError('Content-Length 格式错误') from None
        if not 0 < length <= 64 * 1024:
            raise ValueError('请求体长度须为 1–65536 字节')
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('需要 application/json')
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError('JSON 格式错误') from None
        if not isinstance(data, dict):
            raise ValueError('JSON 请求体必须为对象')
        return data

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        if not origin:
            return True  # 非浏览器客户端（curl）没有 Origin
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._route("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def send_error(self, code, message=None, explain=None) -> None:
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self.close_connection = True
            return self._error(405, 'method not allowed')
        super().send_error(code, message, explain)

    def _route(self, method: str) -> None:
        path, _, query = self.path.partition('?')
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        if method != 'GET':
            self.close_connection = True  # A rejected mutation might carry unread bytes.
        try:
            if path.startswith('/api/'):
                self._api(method, path[5:], params)
            elif path == '/api':
                self._error(404, 'not found')
            elif method != 'GET':
                self._json(405, {'ok': False, 'error': 'method not allowed'}, {'Allow': 'GET, HEAD'})
            else:
                self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("ARC_DEBUG"):
                traceback.print_exc()
            self._error(500, f"内部错误: {exc}")

    # ---- 静态资源 ------------------------------------------------------
    def _static(self, path: str) -> None:
        if path != '/' and (not path.startswith('/') or any(part in ('', '.', '..')
                                                              for part in path[1:].split('/'))):
            return self._error(404, 'not found')
        rel = 'index.html' if path == '/' else path[1:]
        if rel.startswith("api"):
            return self._error(404, "not found")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._error(404, "not found")
        if not target.is_file() or target.name.startswith("."):
            return self._error(404, "not found")
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=300"
        self._send(
            200,
            body,
            {
                "Content-Type": ctype,
                "Cache-Control": cache,
                "Content-Security-Policy": CSP,
            },
        )

    # ---- API -----------------------------------------------------------
    def _api(self, method: str, path: str, params: dict) -> None:
        app = self.app
        accel = app.accel
        parts = path.split('/')
        methods = {
            'health': ('GET',),
            'session': ('GET', 'POST', 'DELETE'),
            'profiles': ('GET',), 'environment': ('GET',), 'state': ('GET',),
            'services': ('GET',), 'cdn': ('GET',), 'audit': ('GET',),
            'jobs': ('GET', 'POST'), 'probe': ('GET',),
        }
        allowed = methods.get(path)
        if len(parts) == 2 and parts[0] in ('host', 'jobs') and parts[1]:
            allowed = ('GET',)
        if not allowed:
            return self._error(404, 'not found')
        if method not in allowed:
            allow = ', '.join((*allowed, 'HEAD'))
            return self._json(405, {'ok': False, 'error': 'method not allowed'}, {'Allow': allow})

        if parts == ['health']:
            return self._json(200, {'ok': True, 'status': 'ok'})

        # 会话建立不需要已有会话
        if parts == ['session']:
            if method != 'GET' and not self._same_origin():
                return self._error(403, '跨站请求被拒绝')
            return self._session_api(method)

        session = self._session()
        if not session:
            return self._error(HTTPStatus.UNAUTHORIZED, "需要先验证访问凭据")
        if method in ("POST", "PATCH", "DELETE", "PUT"):
            if not self._same_origin():
                return self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
            csrf = self.headers.get("X-CSRF", "")
            if not hmac.compare_digest(csrf.encode('utf-8'), session['csrf'].encode('utf-8')):
                return self._error(HTTPStatus.FORBIDDEN, "CSRF 校验失败")

        profiles = getattr(app, 'profiles', {'default': app.accel})
        default = next(iter(profiles))
        if method == 'GET' and parts == ['profiles']:
            return self._json(200, {'ok': True, 'default': default, 'profiles': [
                {'id': key, 'name': getattr(module, '_profile_name', key)}
                for key, module in profiles.items()]})
        if method == 'GET' and parts == ['audit']:
            limit = max(1, min(200, int(params.get('limit', ['50'])[0] or 50)))
            return self._json(200, {'ok': True, 'rows': app.audit.tail(limit)})
        if method == 'GET' and parts[0] == 'jobs':
            if len(parts) == 1:
                return self._json(200, {'ok': True, **app.runner.list_jobs()})
            since = max(0, int(params.get('since', ['0'])[0] or 0))
            detail = app.runner.detail(parts[1], since)
            if detail is None:
                return self._error(404, '任务不存在')
            return self._json(200, {'ok': True, **detail})
        values = params.get('profile', [default])
        if len(values) != 1 or values[0] not in profiles:
            return self._error(400, '未知或重复的 profile ID')
        profile_id = values[0]
        accel = profiles[profile_id]
        if method == 'GET' and parts == ['environment']:
            return self._json(200, {'ok': True, 'environment': environment_status(accel)})
        if method == 'GET' and parts == ['state']:
            fresh = params.get('fresh', ['0'])[0] == '1'
            return self._json(200, {'ok': True, **build_state(accel, force=fresh)})
        if method == 'GET' and parts == ['services']:
            probe = params.get('probe', ['1'])[0] != '0'
            warnings: List[str] = []
            services = discover_services(accel, probe, warnings=warnings)
            return self._json(200, {'ok': True, 'services': services, 'warnings': warnings})
        if method == 'GET' and parts == ['cdn']:
            return self._json(200, {'ok': True, 'cdn': build_cdn(accel)})
        if method == "GET" and parts[:1] == ["host"] and len(parts) == 2:
            hostname = validate_hostname(urllib.parse.unquote(parts[1]))
            with_https = params.get("https", ["0"])[0] == "1"
            return self._json(200, {"ok": True, "host": build_host(accel, hostname, with_https)})
        if method == "GET" and parts == ["probe"]:
            service = validate_service(params.get('service', [''])[0])
            hostname = params.get('hostname', [''])[0]
            if hostname:
                hostname = validate_hostname(hostname)
            t0 = time.time()
            ok, detail = accel.probe_local_backend(
                service, timeout=3, host_header=hostname or None
            )
            return self._json(
                200,
                {
                    "ok": True,
                    "probe": {"ok": bool(ok), "detail": detail},
                    "ms": round((time.time() - t0) * 1000),
                },
            )
        if method == "POST" and parts == ["jobs"]:
            body = self._body()
            op = str(body.get("op") or "")
            if op not in (
                "add", "update", "delete", "switch", "health", "repair",
                "cdn-test", "cdn-set", "cdn-best",
            ):
                return self._error(400, "未知操作")
            try:
                if op in ('add', 'update', 'delete', 'switch', 'repair') or (op == 'health' and body.get('hostname')):
                    body['hostname'] = validate_hostname(body.get('hostname'))
                for key in ('external', 'no_tls_verify', 'force', 'https'):
                    if key in body and body[key] is not None and not isinstance(body[key], bool):
                        raise ValueError(f'{key} 必须为布尔值')
                if op == 'add':
                    reject_external_add(body)
                if op in ('add', 'update'):
                    body['service'] = validate_service(body.get('service'))
                    body['no_tls_verify'] = _no_verify_for(body)
            except ValueError as exc:
                return self._error(400, str(exc))
            if op == "delete":
                if body.get("confirm") != body["hostname"]:
                    return self._error(400, "删除确认不匹配（需原样输入域名）")
                if body["hostname"] in (accel.FALLBACK_ORIGIN, accel.PREFERRED_ENTRY):
                    return self._error(400, "系统入口不允许删除")
            if op in ("cdn-set", "cdn-best"):
                if op == "cdn-set":
                    try:
                        target = validate_hostname(body.get("target"))
                    except ValueError as exc:
                        return self._error(400, f"CDN 目标不合法: {exc}")
                    if body.get("confirm") != target:
                        return self._error(400, "切换确认不匹配（需原样输入 CDN 域名）")
                    body["target"] = target
                body["force"] = bool(body.get("force"))
            try:
                local_operation_guard(accel, op)
            except LocalTunnelUnmanaged as exc:
                return self._error(409, str(exc), code='local_tunnel_unmanaged')
            try:
                job = app.runner.submit(op, body, actor=self._ip(), profile=profile_id)
            except JobBusy as exc:
                return self._error(409, str(exc))
            # An unusually fast stream of submissions may evict this job
            # before the current handler obtains its response snapshot.
            detail = app.runner.detail(job.id) or {'job': job.snapshot()}
            return self._json(200, {'ok': True, **detail})
        return self._error(404, "not found")

    def _session_api(self, method: str) -> None:
        app = self.app
        if method == "GET":
            session = self._session()
            return self._json(
                200,
                {
                    "ok": True,
                    "authenticated": bool(session),
                    "expires": round(session["exp"] - time.time()) if session else 0,
                    "boot": round(time.time() - app.started),
                    "version": getattr(app.accel, "SCRIPT_VERSION", "?"),
                },
            )
        if method == "DELETE":
            app.sessions.drop(self._sid())
            self.send_response(204)
            self.send_header('Connection', 'close')
            self.send_header(
                "Set-Cookie", "arc_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            )
            self.send_header("Set-Cookie", "arc_csrf=; Path=/; SameSite=Strict; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method != "POST":
            return self._error(405, "method not allowed")

        ip = self._ip()
        wait = app.throttle.check(ip)
        if wait > 0:
            app.audit.write("auth.locked", ip=ip, wait=round(wait))
            return self._error(429, "尝试过于频繁，已临时锁定", retry_after=round(wait))
        body = self._body()
        token = str(body.get("token") or "").strip()
        if not token or not hmac.compare_digest(token.encode('utf-8'), app.token.encode('utf-8')):
            wait = app.throttle.fail(ip)
            app.audit.write("auth.fail", ip=ip, wait=round(wait))
            return self._error(
                401,
                "凭据不正确" + (f"，已锁定 {round(wait)}s" if wait else ""),
                retry_after=round(wait),
            )
        app.throttle.reset(ip)
        session = app.sessions.create(ip)
        app.audit.write("auth.ok", ip=ip)
        cookie = (
            f"arc_sid={session['sid']}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_TTL}"
        )
        self._json(
            200,
            {
                "ok": True,
                "csrf": session["csrf"],
                "expires": round(session["exp"] - time.time()),
            },
            # 双提交：sid 只走 HttpOnly，csrf 单独给一份可读 cookie 供页面回填，
            # 这样刷新页面不用重新输凭据，跨站请求也伪造不出这个头。
            [
                ("Set-Cookie", cookie),
                (
                    "Set-Cookie",
                    f"arc_csrf={session['csrf']}; Path=/; SameSite=Strict; Max-Age={SESSION_TTL}",
                ),
            ],
        )


def bind_http_server(host: str, port: int) -> ThreadingHTTPServer:
    handler = type('BoundHandler', (Handler,), {})
    server_type = ThreadingHTTPServer
    if ':' in host:
        server_type = type('IPv6HTTPServer', (ThreadingHTTPServer,), {'address_family': socket.AF_INET6})
    httpd = server_type((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def make_server(app: App) -> ThreadingHTTPServer:
    httpd = bind_http_server(app.host, app.port)
    httpd.RequestHandlerClass.app = app
    return httpd


def _lan_ip() -> str:
    """取本机对外网卡 IP（不真的发包），失败退回回环。"""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        try:
            probe.connect(("8.8.8.8", 53))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"


def banner(app: App, key_source: str, *, print_initial: bool) -> None:
    shown_host = '127.0.0.1' if app.host in ('0.0.0.0', '') else app.host
    lines = ['', '  ARC · cf-tunnel-accel console', f'  凭据   {key_source}']
    if app.token_created and print_initial:
        lines.append('  首次登录密钥: ' + app.token)
        lines.append('  已保存到 0600 文件；重启将复用密钥，不再打印。')
    if app.host in ('0.0.0.0', '') and not app.container:
        lines.append(f'  局域网 http://{_lan_ip()}:{app.port}/（请限制访问范围）')
    lines.extend((f'  审计   {AUDIT_PATH}', f'  任务   {JOBS_PATH}',
                  f"  模块   cf-tunnel-accel {getattr(app.accel, 'SCRIPT_VERSION', '?')}",
                  f'  访问   http://{_http_address(shown_host)}:{app.port}/', ''))
    print('\n'.join(lines), flush=True)

def load_console_profiles(module: str, registry, parser) -> Tuple[Any, Dict[str, Any]]:
    """Load the CLI module(s) backing the console.

    No registry (and no config-dir default) keeps the original single-profile
    behaviour; an explicitly requested registry that fails is fatal rather than
    silently downgraded to the default instance.
    """
    if registry is None:
        candidate = CONFIG_DIR / 'profiles.json'
        registry = candidate if candidate.exists() else None
    path = Path(module).expanduser()
    if not path.is_file():
        parser.error(f'找不到 CLI 模块: {path}；请检查 --module / CF_TUNNEL_ACCEL_SCRIPT')
    try:
        loader = load_accel(UI_DIR / 'profiles.py', 'arc_profiles')
        profiles = loader.load_profiles(path, registry)
    except (OSError, ValueError, ImportError, SyntaxError) as exc:
        parser.error(f'profile 加载失败 ({path}): {exc}')
    for profile_id, loaded in profiles.items():
        loaded._profile_id = profile_id  # noqa: SLF001 - 缓存与历史按 profile 分键
        loaded._profile_name = loaded._profile_metadata.get('name', profile_id)
    return profiles[next(iter(profiles))], profiles


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="cf-tunnel-accel Web 控制台")
    parser.add_argument("--host", default=os.environ.get("ARC_HOST", "127.0.0.1"))
    parser.add_argument('--port', type=int, default=os.environ.get('ARC_PORT', '8642'))
    parser.add_argument("--module", default=str(DEFAULT_MODULE))
    parser.add_argument('--profiles', type=Path, default=os.environ.get('CF_TUNNEL_ACCEL_UI_PROFILES') or None)
    parser.add_argument('--allow-remote-http', action='store_true',
                        help='仅用于受信 TLS 反向代理后的非回环监听')
    parser.add_argument('--token-file', help='从指定文件只读加载 UI 登录密钥')
    parser.add_argument('--no-print-initial-token', action='store_true', help='首次生成时也不在启动日志打印密钥')
    parser.add_argument('--print-token', action='store_true', help='从受保护的本地终端读取当前密钥后退出')
    parser.add_argument('--rotate-token', action='store_true', help='原子替换应用托管的密钥文件（外部配置不适用）')
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65535:
        parser.error('port 必须在 0–65535 之间')
    if not args.print_token and not (_is_loopback(args.host) or args.host == 'localhost') and not args.allow_remote_http:
        parser.error('非回环 HTTP 监听需显式 --allow-remote-http，并限制监听范围')
    runtime = os.environ.get('CF_TUNNEL_ACCEL_RUNTIME') or 'host'
    if runtime not in ('host', 'container'):
        parser.error('CF_TUNNEL_ACCEL_RUNTIME 须为 host 或 container')
    initial_log = os.environ.get('CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN') or '1'
    if initial_log not in ('0', '1'):
        parser.error('CF_TUNNEL_ACCEL_UI_PRINT_INITIAL_TOKEN 须为 0 或 1')

    key_value = os.environ.pop('CF_TUNNEL_ACCEL_UI_TOKEN', '')
    key_file = args.token_file if args.token_file is not None else os.environ.get('CF_TUNNEL_ACCEL_UI_TOKEN_FILE') or None
    if key_file == '':
        parser.error('--token-file 需要非空路径')
    if key_value and key_file is not None:
        parser.error('CF_TUNNEL_ACCEL_UI_TOKEN 与密钥文件只能选一种')
    if args.rotate_token and (key_value or key_file is not None):
        parser.error('--rotate-token 仅支持应用托管的密钥；请更新显式配置来源')
    key_path = Path(key_file).expanduser() if key_file is not None else TOKEN_PATH
    key_source = 'CF_TUNNEL_ACCEL_UI_TOKEN' if key_value else str(key_path)

    def select_key() -> Tuple[str, bool]:
        try:
            if key_value:
                return validate_login_key(key_value), False
            if key_file is not None:
                return read_login_key(key_path), False
            return load_or_create_token(rotate=args.rotate_token)
        except (OSError, ValueError) as exc:
            parser.error(f'登录密钥配置错误 ({key_source}): {exc}')

    if args.print_token:
        print(select_key()[0])
        return 0

    accel, profiles = load_console_profiles(args.module, args.profiles, parser)
    try:
        httpd = bind_http_server(args.host, args.port)
    except OSError as exc:
        parser.error(f'监听 {args.host}:{args.port} 失败: {exc}；请检查地址或更换 --port')
    try:
        token, created = select_key()
        app = App(accel, token, args.host, httpd.server_address[1], created, profiles,
                  container=(runtime == 'container'))
        httpd.RequestHandlerClass.app = app
    except BaseException:
        httpd.server_close()
        raise

    # Poll accept with a short bound. The handler only flips a flag: calling
    # BaseServer.shutdown() here would wait on the same serving thread.
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        banner(app, key_source, print_initial=(initial_log == '1' and not args.no_print_initial_token))
        httpd.timeout = 0.25
        while not stopping:
            httpd.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
            print('\n  停止 HTTP，等待已接收的任务…')
            if not app.runner.close(drain=True, timeout=SHUTDOWN_DRAIN_SECONDS):
                print('ARC: 等待任务结束超时；下次启动会标记未完成的任务并且不会重试。', file=sys.stderr)
        finally:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
