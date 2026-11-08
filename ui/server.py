#!/usr/bin/env python3
"""ARC —— cf-tunnel-accel 的 Web 控制台。

单文件、仅标准库：反向复用 cf-tunnel-accel 已有的全部云端逻辑（新增/改/删/
体检/切优选），不重写任何 Cloudflare 交互，避免两套实现互相打脸。

安全设计（首页即闸门，不是装饰）
  * 默认只监听 127.0.0.1；要对外用 --host 0.0.0.0，并知悉 token 是唯一凭据。
  * 访问凭据是 0600 文件的 48 位 hex token，恒定时间比较，从不出现在页面里。
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
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UI_DIR = Path(__file__).resolve().parent
STATIC_DIR = UI_DIR / "static"
DEFAULT_MODULE = Path(os.environ.get("CF_TUNNEL_ACCEL_SCRIPT", "/root/bin/cf-tunnel-accel"))
CONFIG_DIR = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_DIR", "/root/.config/cf-tunnel-accel")
)
TOKEN_PATH = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_TOKEN_FILE", str(CONFIG_DIR / "ui-token"))
)
CDN_HISTORY_PATH = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_CDN_HISTORY", str(CONFIG_DIR / "ui-cdn-history.json"))
)
AUDIT_PATH = Path(
    os.environ.get("CF_TUNNEL_ACCEL_UI_AUDIT", str(CONFIG_DIR / "ui-audit.log"))
)

HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")
SERVICE_RE = re.compile(r"^(https?)://([A-Za-z0-9.\-\[\]:]+):(\d{1,5})(/.*)?$")

SESSION_TTL = int(os.environ.get("CF_TUNNEL_ACCEL_UI_SESSION_TTL", 12 * 3600))
MAX_JOBS_KEPT = 40
MAX_JOB_LINES = 800
THROTTLE_FREE_ATTEMPTS = 3
THROTTLE_LOCK_BASE = 30
THROTTLE_LOCK_MAX = 900


# --------------------------------------------------------------------------
# 载入 cf-tunnel-accel（唯一真相来源）
# --------------------------------------------------------------------------
def load_accel(path: Path):
    if not path.is_file():
        raise SystemExit(
            f"找不到 cf-tunnel-accel 脚本: {path}\n"
            "用 --module /实际/路径 或环境变量 CF_TUNNEL_ACCEL_SCRIPT 指定。"
        )
    loader = importlib.machinery.SourceFileLoader("cf_tunnel_accel", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 凭据 / 会话 / 审计
# --------------------------------------------------------------------------
def load_or_create_token() -> Tuple[str, bool]:
    """返回 (token, 是否新建)。文件权限必须 0600。"""
    try:
        raw = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(raw) >= 32:
            mode = TOKEN_PATH.stat().st_mode & 0o777
            if mode != 0o600:
                os.chmod(TOKEN_PATH, 0o600)
            return raw, False
    except OSError:
        pass
    token = secrets.token_hex(24)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


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
        except OSError:
            return
        for line in lines:
            try:
                self.recent.append(json.loads(line))
            except ValueError:
                continue

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with self.lock:
            self.recent.append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
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


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()[:240]


def discover_services(accel, with_probe: bool = True) -> List[dict]:
    """本机监听端口 + 进程 + 已映射域名 [+ 快速 HTTP 探活]。"""
    text = ""
    if shutil.which("ss"):
        try:
            text = subprocess.run(
                ["ss", "-ltnp"], capture_output=True, text=True, timeout=6
            ).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
    raw = parse_ss_listen(text)
    # 双栈监听会在 ss 里出现两次（*:3000 / [::]:3000），按端口合并成一行
    merged: Dict[int, dict] = {}
    for row in raw:
        cur = merged.get(row["port"])
        if cur is None:
            merged[row["port"]] = dict(row, addrs=[row["addr"]])
        else:
            if row["addr"] not in cur["addrs"]:
                cur["addrs"].append(row["addr"])
            if not cur["process"] and row["process"]:
                cur["process"], cur["pid"] = row["process"], row["pid"]
    rows = list(merged.values())
    for row in rows:
        addrs = [("0.0.0.0" if a == "*" else a.split("%")[0]) for a in row["addrs"]]
        row["addrs"] = addrs
        row["addr"] = addrs[0] if len(addrs) == 1 else "any"
    routes = accel.parse_ingress_hostnames(accel.read_config_text() or "")

    def mapped_for(port: int) -> List[str]:
        hits = []
        for host, service in routes:
            m = SERVICE_RE.match(service or "")
            if m and int(m.group(3)) == port:
                hits.append(host)
        return sorted(hits)

    for row in rows:
        row["friendly"] = _friendly_name(row["process"], row["port"])
        row["cmd"] = _proc_cmdline(row["pid"]) if row["pid"] else ""
        row["mapped"] = mapped_for(row["port"])
        row["loopback_only"] = all(_is_loopback(a) for a in row.get("addrs", [row["addr"]]))
    rows.sort(key=lambda r: (not r["mapped"], r["port"]))

    if with_probe and rows:
        targets = [r for r in rows if r["port"] > 0][:220]

        def probe(row: dict) -> None:
            host = (row["mapped"][0] if row["mapped"] else "localhost")
            try:
                ok, detail = accel.probe_local_backend(
                    f"http://127.0.0.1:{row['port']}", timeout=0.8, host_header=host
                )
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            row["probe"] = {"ok": bool(ok), "detail": detail}

        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(probe, targets))
    return rows


# --------------------------------------------------------------------------
# 任务队列（单一 worker，保证隧道重启不会互相踩）
# --------------------------------------------------------------------------
def _ms(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}ms"


class Job:
    def __init__(self, op: str, args: dict, actor: str):
        self.id = secrets.token_hex(8)
        self.op = op
        self.args = args
        self.actor = actor
        self.status = "queued"
        self.lines: List[dict] = []
        self.result: Any = None
        self.error: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.lock = threading.Lock()

    def log(self, text: str) -> None:
        text = re.sub(r"\x1b\[[0-9;]*m", "", str(text)).rstrip()
        if not text:
            return
        with self.lock:
            self.lines.append({"t": round(time.time() - self.created, 2), "m": text})
            if len(self.lines) > MAX_JOB_LINES:
                del self.lines[: len(self.lines) - MAX_JOB_LINES]

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            lines = self.lines[since:]
            total = len(self.lines)
        return {
            "id": self.id,
            "op": self.op,
            "status": self.status,
            "lines": lines,
            "next": total,
            "result": self.result,
            "error": self.error,
            "elapsed": round((self.ended or time.time()) - (self.started or self.created), 1),
        }


class JobRunner:
    def __init__(self, accel, audit: Audit):
        self.accel = accel
        self.audit = audit
        self.jobs: "OrderedDict[str, Job]" = OrderedDict()
        self.queue: deque = deque()
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self._orig_cprint = accel.cprint
        self.worker = threading.Thread(target=self._loop, name="arc-jobs", daemon=True)
        self.worker.start()

    # ---- 提交 ----------------------------------------------------------
    def submit(self, op: str, args: dict, actor: str) -> Job:
        job = Job(op, args, actor)
        with self.lock:
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS_KEPT:
                _, old = self.jobs.popitem(last=False)
                if old.status in ("queued", "running"):
                    self.jobs[old.id] = old
                    break
            self.queue.append(job.id)
            self.cv.notify()
        self.audit.write("job.submit", op=op, job=job.id, actor=actor, args=args)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def active(self) -> Optional[str]:
        with self.lock:
            for job in self.jobs.values():
                if job.status == "running":
                    return job.id
        return None

    # ---- 执行 ----------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self.lock:
                while not self.queue:
                    self.cv.wait()
                job_id = self.queue.popleft()
                job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            job.status = "running"
            job.started = time.time()
            accel = self.accel
            original = self._orig_cprint

            def tee(msg: str = "", color: Optional[str] = None) -> None:
                job.log(msg)
                original(msg, color)

            accel.cprint = tee  # type: ignore[assignment]
            status, result, error = "done", None, None
            try:
                result = self._dispatch(job)
            except Exception as exc:  # noqa: BLE001 - 任何失败都要回流到界面
                status = "error"
                error = str(exc) or exc.__class__.__name__
                job.log(f"错误: {error}")
                if os.environ.get("ARC_DEBUG"):
                    job.log(traceback.format_exc())
            finally:
                accel.cprint = original  # type: ignore[assignment]
            # 先落审计、再宣告状态：观察者看到 done/error 时，cprint 已还原、
            # job.ended 有值、审计也已写完，不留任何“状态已到但副作用未完”的窗口。
            if status == "done":
                self.audit.write("job.done", op=job.op, job=job.id, actor=job.actor)
            else:
                self.audit.write(
                    "job.error", op=job.op, job=job.id, actor=job.actor, error=error
                )
            job.result = result
            job.error = error
            job.ended = time.time()
            job.status = status

    def _cdn_bench(self, job: Job) -> dict:
        """只测速，绝不改 DNS。"""
        accel = self.accel
        try:
            current = accel.get_current_preferred_target(strict=False)
        except Exception:  # noqa: BLE001
            current = ""
        domains = accel.ensure_candidate_list()
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
            score = accel.cdn_latency_score(row)
            # cdn_latency_score 会退化到 TCP：HTTPS 全挂时也有“分数”，
            # 但那种候选切过去等于把全部域名弄挂，必须单独标不可用。
            usable = bool(row.get("https_ok")) and score is not None
            out.append(
                {
                    "domain": row["domain"],
                    "current": row["domain"] == current,
                    "usable": usable,
                    "score": score,
                    "median_https": row.get("median_https"),
                    "median_tcp": row.get("median_tcp"),
                    "avg_ttfb": row.get("avg_ttfb"),
                    "best_ping": row.get("best_ping"),
                    "success_rate": row.get("https_success_rate"),
                    "https_ok": row.get("https_ok"),
                    "https_tries": row.get("https_tries"),
                    "ip_count": row.get("ip_count"),
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
        accel = self.accel
        a = job.args
        if job.op == "add":
            accel.add_accelerated_service(
                a["hostname"], a["service"], external=bool(a.get("external"))
            )
            return {"hostname": a["hostname"]}
        if job.op == "update":
            before = accel.read_config_text()
            changed = accel.upsert_tunnel_hostname(a["hostname"], a["service"])
            if changed:
                accel.validate_and_restart_tunnel(rollback_text=before)
            else:
                job.log("隧道配置已是这个后端，跳过重启。")
            return {"hostname": a["hostname"], "changed": bool(changed)}
        if job.op == "delete":
            accel.remove_service(a["hostname"])
            return {"hostname": a["hostname"]}
        if job.op == "switch":
            accel.switch_to_preferred(a["hostname"])
            return {"hostname": a["hostname"]}
        if job.op == "health":
            ok = accel.health_check(a.get("hostname") or None, include_https=bool(a.get("https")))
            return {"ok": bool(ok), "hostname": a.get("hostname")}
        if job.op == "cdn-test":
            return self._cdn_bench(job)
        if job.op == "cdn-set":
            before = accel.get_current_preferred_target(strict=True)
            CDN_HISTORY.note(before)
            accel.set_preferred_target(a["target"])
            CDN_HISTORY.note(a["target"])
            return {"before": before, "target": a["target"], "changed": before != a["target"]}
        if job.op == "cdn-best":
            before = accel.get_current_preferred_target(strict=True)
            CDN_HISTORY.note(before)
            chosen = accel.test_and_maybe_switch_cdn(
                auto_apply_best=True, interactive=False, force=bool(a.get("force"))
            )
            CDN_HISTORY.note(chosen)
            return {"before": before, "target": chosen, "changed": chosen != before}
        if job.op == "repair":
            # 一键把某个域名的入口 CNAME 拉回优选入口
            accel.switch_to_preferred(a["hostname"])
            return {"hostname": a["hostname"]}
        raise ValueError(f"未知任务类型: {job.op}")


# --------------------------------------------------------------------------
# 状态聚合
# --------------------------------------------------------------------------
def build_state(accel) -> dict:
    accel.ensure_zone_cache()
    routes = accel.parse_ingress_hostnames(accel.read_config_text() or "")
    custom: Dict[str, dict] = {}
    try:
        custom = {
            str(i.get("hostname")).lower(): i for i in accel.list_custom_hostnames()
        }
    except Exception:  # noqa: BLE001
        custom = {}
    active, _state = accel._systemd_active()  # noqa: SLF001 - 复用同一份判定
    mainpid = 0
    try:
        proc = subprocess.run(
            ["systemctl", "show", accel.SERVICE_NAME, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=6,
        )
        mainpid = int((proc.stdout or "0").strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        mainpid = 0
    try:
        preferred = accel.get_current_preferred_target(strict=False)
    except Exception:  # noqa: BLE001
        preferred = None

    rows = []
    for host, service in routes:
        if host == accel.FALLBACK_ORIGIN:
            continue
        ch = custom.get(host.lower()) or {}
        zone = accel.resolve_zone(host)
        rows.append(
            {
                "hostname": host,
                "service": service,
                "zone": zone[0] if zone else None,
                "external": zone is None,
                "saas": {
                    "status": ch.get("status"),
                    "ssl": (ch.get("ssl") or {}).get("status"),
                }
                if ch
                else None,
            }
        )
    rows.sort(key=lambda r: r["hostname"])
    return {
        "version": getattr(accel, "SCRIPT_VERSION", "?"),
        "service": {"name": accel.SERVICE_NAME, "active": active, "mainpid": mainpid},
        "config_path": str(accel.CONFIG_PATH),
        "preferred": {"entry": accel.PREFERRED_ENTRY, "target": preferred},
        "fallback": accel.FALLBACK_ORIGIN,
        "zones": [
            {"name": k, "id": str(v.get("id") or ""), "status": v.get("status")}
            for k, v in sorted(accel._zone_info.items())  # noqa: SLF001
        ],
        "routes": rows,
        "counts": {
            "routes": len(rows),
            "zones": len(accel._zone_info),  # noqa: SLF001
            "saas": len(custom),
        },
    }


CDN_HISTORY = CdnHistory(CDN_HISTORY_PATH)


def build_cdn(accel) -> dict:
    """优选入口的当前目标与候选清单（只读，不发写请求）。"""
    try:
        current = accel.get_current_preferred_target(strict=False)
    except Exception:  # noqa: BLE001
        current = getattr(accel, "PREFERRED_TARGET", "")
    try:
        candidates = accel.ensure_candidate_list()
    except Exception:  # noqa: BLE001
        candidates = list(getattr(accel, "CDN_CANDIDATES", []))
    if current and current not in candidates:
        candidates = [current] + candidates
    builtin = set(candidates)
    # 当前目标第一次出现就记进历史，免得重启后列表里没有它
    if current and current not in CDN_HISTORY.domains():
        CDN_HISTORY.note(current)
    for domain in CDN_HISTORY.domains():
        if domain not in builtin and domain not in candidates:
            candidates.append(domain)
    return {
        "entry": accel.PREFERRED_ENTRY,
        "target": current,
        "candidates": [
            {"domain": d, "current": d == current, "builtin": d in builtin}
            for d in candidates
        ],
        "min_improvement": getattr(accel, "CDN_SWITCH_MIN_IMPROVEMENT_PERCENT", 15.0),
    }


def build_host(accel, hostname: str, with_https: bool = False) -> dict:
    routes = dict(accel.parse_ingress_hostnames(accel.read_config_text() or ""))
    zone = accel.resolve_zone(hostname)
    out: Dict[str, Any] = {
        "hostname": hostname,
        "service": routes.get(hostname),
        "zone": zone[0] if zone else None,
        "zone_id": zone[1] if zone else None,
    }
    ch = None
    try:
        ch = accel.get_custom_hostname(hostname)
    except Exception as exc:  # noqa: BLE001
        out["saas_error"] = str(exc)
    if ch:
        out["saas"] = {
            "id": ch.get("id"),
            "status": ch.get("status"),
            "ssl": (ch.get("ssl") or {}).get("status"),
            "errors": [e.get("message") for e in (ch.get("verification_errors") or [])][:4],
        }
    if zone:
        try:
            cname = accel.get_dns_record_content(zone[1], hostname, "CNAME")
            a = accel.get_dns_record_content(zone[1], hostname, "A")
            out["dns"] = {"cname": cname, "a": a}
        except Exception as exc:  # noqa: BLE001
            out["dns_error"] = str(exc)
    if out.get("service"):
        ok, detail = accel.probe_local_backend(
            out["service"], timeout=3, host_header=hostname
        )
        out["probe"] = {"ok": bool(ok), "detail": detail}
    if with_https:
        try:
            ok, detail = accel.verify_https(hostname, timeout=12)
            out["https"] = {"ok": bool(ok), "detail": detail[-1200:]}
        except Exception as exc:  # noqa: BLE001
            out["https"] = {"ok": False, "detail": str(exc)}
    return out


def validate_hostname(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    if not HOSTNAME_RE.match(host):
        raise ValueError("域名格式不合法")
    return host


def validate_service(value: str) -> str:
    service = (value or "").strip()
    if not service:
        raise ValueError("后端地址不能为空")
    if service.isdigit():
        service = f"http://127.0.0.1:{service}"
    if "://" not in service:
        service = f"http://{service}"
    m = SERVICE_RE.match(service)
    if not m:
        raise ValueError("后端要写成 http://127.0.0.1:8080")
    port = int(m.group(3))
    if not 1 <= port <= 65535:
        raise ValueError("端口超出范围")
    scheme, host, tail = m.group(1), m.group(2), m.group(4) or ""
    return f"{scheme}://{host}:{port}{tail}"


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
    def __init__(self, accel, token: str, host: str, port: int, token_created: bool):
        self.accel = accel
        self.token = token
        self.host = host
        self.port = port
        self.token_created = token_created
        self.sessions = Sessions(SESSION_TTL)
        self.throttle = Throttle()
        self.audit = Audit(AUDIT_PATH)
        self.runner = JobRunner(accel, self.audit)
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 64 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

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

    def _route(self, method: str) -> None:
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        try:
            if path.startswith("/api/"):
                self._api(method, path[5:], params)
            else:
                self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("ARC_DEBUG"):
                traceback.print_exc()
            self._error(500, f"内部错误: {exc}")

    # ---- 静态资源 ------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
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
        parts = [p for p in path.split("/") if p]

        # 会话建立不需要已有会话
        if parts[:1] == ["session"]:
            return self._session_api(method)

        session = self._session()
        if not session:
            return self._error(HTTPStatus.UNAUTHORIZED, "需要先验证访问凭据")
        if method in ("POST", "PATCH", "DELETE", "PUT"):
            if not self._same_origin():
                return self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
            csrf = self.headers.get("X-CSRF", "")
            if not hmac.compare_digest(csrf, session["csrf"]):
                return self._error(HTTPStatus.FORBIDDEN, "CSRF 校验失败")

        if method == "GET" and parts == ["state"]:
            return self._json(200, {"ok": True, **build_state(accel)})
        if method == "GET" and parts == ["services"]:
            probe = params.get("probe", ["1"])[0] != "0"
            return self._json(200, {"ok": True, "services": discover_services(accel, probe)})
        if method == "GET" and parts == ["cdn"]:
            return self._json(200, {"ok": True, "cdn": build_cdn(accel)})
        if method == "GET" and parts == ["audit"]:
            limit = min(200, int(params.get("limit", ["50"])[0] or 50))
            return self._json(200, {"ok": True, "rows": app.audit.tail(limit)})
        if method == "GET" and parts[:1] == ["jobs"] and len(parts) == 2:
            job_id = parts[1]
            job = app.runner.get(job_id)
            if not job:
                return self._error(404, "任务不存在")
            since = int(params.get("since", ["0"])[0] or 0)
            return self._json(200, {"ok": True, "job": job.snapshot(since), "active": app.runner.active()})
        if method == "GET" and parts[:1] == ["host"] and len(parts) > 1:
            hostname = urllib.parse.unquote(parts[1])
            with_https = params.get("https", ["0"])[0] == "1"
            return self._json(200, {"ok": True, "host": build_host(accel, hostname, with_https)})
        if method == "GET" and parts == ["probe"]:
            service = urllib.parse.unquote(params.get("service", [""])[0])
            hostname = params.get("hostname", [""])[0]
            if not service:
                return self._error(400, "缺少 service")
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
                if op in ("add", "update", "delete", "switch"):
                    body["hostname"] = validate_hostname(body.get("hostname"))
                if op in ("add", "update"):
                    body["service"] = validate_service(body.get("service"))
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
            if op in ("add", "update", "delete", "switch", "repair", "cdn-set", "cdn-best"):
                if app.runner.active():
                    return self._error(409, "已有变更任务在执行，稍后重试")
            job = app.runner.submit(op, body, actor=self._ip())
            return self._json(200, {"ok": True, "job": job.snapshot(0)})
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
        if not token or not hmac.compare_digest(token, app.token):
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


def make_server(app: App) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((app.host, app.port), handler)
    httpd.daemon_threads = True
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


def banner(app: App) -> None:
    shown_host = "127.0.0.1" if app.host in ("0.0.0.0", "") else app.host
    print("\n  ARC · cf-tunnel-accel console")
    print(f"  ────────────────────────────────────────────")
    print(f"  访问   http://{shown_host}:{app.port}/")
    if app.host in ("0.0.0.0", ""):
        print(f"  局域网 http://{_lan_ip()}:{app.port}/   (绑定 0.0.0.0，凭据是唯一门槛)")
    print(f"  凭据   {TOKEN_PATH}")
    if app.token_created:
        print(f"  新凭据 {app.token}")
        print("         仅本次打印，之后只存在 0600 文件里")
    print(f"  审计   {AUDIT_PATH}")
    print(f"  模块   cf-tunnel-accel {getattr(app.accel, 'SCRIPT_VERSION', '?')}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="cf-tunnel-accel Web 控制台")
    parser.add_argument("--host", default=os.environ.get("ARC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARC_PORT", "8642")))
    parser.add_argument("--module", default=str(DEFAULT_MODULE))
    parser.add_argument("--print-token", action="store_true", help="打印当前凭据后退出")
    parser.add_argument("--rotate-token", action="store_true", help="重置凭据")
    args = parser.parse_args(argv)

    if args.rotate_token:
        try:
            TOKEN_PATH.unlink()
        except OSError:
            pass
    token, created = load_or_create_token()
    if args.print_token:
        print(token)
        return 0

    accel = load_accel(Path(args.module))
    app = App(accel, token, args.host, args.port, created)
    httpd = make_server(app)
    banner(app)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  收工。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
