"""ARC 控制台的离线测试：不起真实服务、不碰 Cloudflare。

覆盖的是安全边界与纯函数：解析、校验、鉴权限速、静态路径穿越、
CSRF / Origin 判定、任务队列的串行语义。
"""

import importlib.machinery
import importlib.util
import io
import json
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve()
SERVER = HERE.parent.parent / "server.py"
loader = importlib.machinery.SourceFileLoader("arc_server", str(SERVER))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class FakeAccel:
    """够用的 cf-tunnel-accel 替身：只实现控制台会用到的入口。"""

    SCRIPT_VERSION = "9.9.9"
    SERVICE_NAME = "cloudflared-test.service"
    FALLBACK_ORIGIN = "origin.example.org"
    PREFERRED_ENTRY = "cdn.example.org"
    CONFIG_PATH = Path("/tmp/arc-test-config.yml")
    _zone_info = {
        "example.org": {"id": "z1", "status": "active"},
        "other.org": {"id": "z2", "status": "active"},
    }

    def __init__(self):
        self.config = (
            "tunnel: t\n"
            "ingress:\n"
            "  - hostname: a.example.org\n"
            "    service: http://127.0.0.1:80\n"
            "  - hostname: origin.example.org\n"
            "    service: http://127.0.0.1:18080\n"
            "  - service: http_status:404\n"
        )
        self.calls = []

    # ---- 读取 ----
    def read_config_text(self):
        return self.config

    def parse_ingress_hostnames(self, text):
        items, cur = [], None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("- hostname:"):
                cur = s.split(":", 1)[1].strip()
            elif s.startswith("service:") and cur:
                items.append((cur, s.split(":", 1)[1].strip()))
                cur = None
        return items

    def ensure_zone_cache(self):
        return None

    def list_custom_hostnames(self):
        return [{"hostname": "a.example.org", "status": "active", "ssl": {"status": "active"}}]

    def _systemd_active(self):
        return True, "active"


    def resolve_zone(self, host):
        return ("example.org", "z1") if host.endswith("example.org") else None

    def get_custom_hostname(self, host):
        return {"id": "ch1", "status": "active", "ssl": {"status": "active"}}

    def get_dns_record_content(self, zone_id, name, rtype="CNAME"):
        return "cdn.example.org"

    def probe_local_backend(self, service, timeout=5, host_header=None):
        return True, "HTTP 200 (HEAD)"

    def verify_https(self, host, timeout=25):
        return True, "HTTP/1.1 200 OK"

    def cprint(self, msg="", color=None):
        pass

    # ---- 写入 ----
    def add_accelerated_service(self, host, service, external=False):
        self.calls.append(("add", host, service, external))

    def upsert_tunnel_hostname(self, host, service):
        self.calls.append(("upsert", host, service))
        return True

    def validate_and_restart_tunnel(self, rollback_text=None):
        self.calls.append(("restart",))

    def remove_service(self, host):
        self.calls.append(("remove", host))

    def switch_to_preferred(self, host):
        self.calls.append(("switch", host))

    def health_check(self, hostname=None, include_https=False):
        self.calls.append(("health", hostname, include_https))
        return True

    # ---- 优选 CDN ----
    CDN_CANDIDATES = ["fast.example", "slow.example"]
    CDN_SWITCH_MIN_IMPROVEMENT_PERCENT = 15.0

    def get_current_preferred_target(self, strict=False):
        return "slow.example"

    def ensure_candidate_list(self, extra=None):
        return ["slow.example", "fast.example"]

    def benchmark_cdn(self, domain, ping_count=4, tcp_tries=4):
        self.calls.append(("bench", domain))
        if domain == "fast.example":
            return {
                "domain": domain, "median_https": 40.0, "median_tcp": 30.0,
                "avg_ttfb": 60.0, "best_ping": 10.0, "https_ok": 3, "https_tries": 3,
                "https_success_rate": 1.0, "ip_count": 2, "best_tcp": 28.0,
            }
        return {
            "domain": domain, "median_https": None, "median_tcp": 15.0,
            "avg_ttfb": None, "best_ping": 8.0, "https_ok": 0, "https_tries": 3,
            "https_success_rate": 0.0, "ip_count": 1, "best_tcp": 14.0,
        }

    def rank_cdn_results(self, results):
        return sorted(results, key=lambda x: (x.get("https_ok", 0) == 0, x.get("median_https") or 1e9))

    def cdn_latency_score(self, row):
        if row.get("median_https") is not None and row.get("https_ok"):
            return float(row["median_https"])
        if row.get("median_tcp") is not None:
            return float(row["median_tcp"])
        return None

    def set_preferred_target(self, target, note=""):
        self.calls.append(("set_cdn", target))
        return {}

    def test_and_maybe_switch_cdn(self, auto_apply_best=False, candidates=None,
                                 interactive=False, force=False):
        self.calls.append(("auto_best", auto_apply_best))
        if auto_apply_best:
            self.calls.append(("set_cdn", "fast.example"))
            return "fast.example"
        return "slow.example"


class ValidatorsTest(unittest.TestCase):
    def test_hostname_accepts_and_lowercases(self):
        self.assertEqual(mod.validate_hostname("APP.Example.ORG."), "app.example.org")
        for bad in ("", "no-dot", "-bad.example.org", "a b.example.org", "x" * 300):
            with self.assertRaises(ValueError):
                mod.validate_hostname(bad)

    def test_service_normalization(self):
        self.assertEqual(mod.validate_service("8080"), "http://127.0.0.1:8080")
        self.assertEqual(mod.validate_service("127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(mod.validate_service("https://h.example:443/x"), "https://h.example:443/x")
        for bad in ("", "0", "70000", "http://h.example", "ftp://h:21"):
            with self.assertRaises(ValueError):
                mod.validate_service(bad)


class SsParsingTest(unittest.TestCase):
    SAMPLE = (
        'State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\n'
        'LISTEN 0      511          0.0.0.0:80         0.0.0.0:*    users:(("nginx",pid=1504,fd=18))\n'
        'LISTEN 0      1024      127.0.0.1:6379     0.0.0.0:*    users:(("redis-server",pid=1199,fd=6))\n'
        'LISTEN 0      511               [::]:80            [::]:*    users:(("nginx",pid=1503,fd=18))\n'
        'LISTEN 0      5            127.0.0.53%lo:53     0.0.0.0:*    users:(("systemd-resolve",pid=700,fd=13))\n'
        'ESTAB  0      0            127.0.0.1:5555      127.0.0.1:43512\n'
    )

    def test_parse_listen_rows(self):
        rows = mod.parse_ss_listen(self.SAMPLE)
        ports = sorted(r["port"] for r in rows)
        self.assertEqual(ports, [53, 80, 80, 6379])
        nginx = [r for r in rows if r["port"] == 80][0]
        self.assertEqual(nginx["process"], "nginx")
        self.assertEqual(nginx["pid"], 1504)

    def test_discover_merges_dual_stack(self):
        accel = FakeAccel()
        with mock.patch.object(mod.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=self.SAMPLE, stderr="")
            rows = mod.discover_services(accel, with_probe=False)
        by_port = {r["port"]: r for r in rows}
        self.assertEqual(len(rows), 3)  # 80 双栈合并成一行
        self.assertEqual(by_port[80]["addr"], "any")
        self.assertEqual(by_port[6379]["loopback_only"], True)
        self.assertEqual(by_port[80]["loopback_only"], False)
        self.assertEqual(by_port[80]["mapped"], ["a.example.org"])


class TokenFileTest(unittest.TestCase):
    def test_token_is_created_0600_and_stable(self):
        with tempfile.TemporaryDirectory() as td:
            original = mod.TOKEN_PATH
            mod.TOKEN_PATH = Path(td) / "token"
            try:
                token, created = mod.load_or_create_token()
                self.assertTrue(created)
                self.assertGreaterEqual(len(token), 32)
                self.assertEqual(mod.TOKEN_PATH.stat().st_mode & 0o777, 0o600)
                again, created2 = mod.load_or_create_token()
                self.assertEqual(again, token)
                self.assertFalse(created2)
                mod.TOKEN_PATH.chmod(0o644)
                mod.load_or_create_token()
                self.assertEqual(mod.TOKEN_PATH.stat().st_mode & 0o777, 0o600)
            finally:
                mod.TOKEN_PATH = original


class ThrottleTest(unittest.TestCase):
    def test_free_attempts_then_exponential_lock(self):
        t = mod.Throttle()
        for _ in range(mod.THROTTLE_FREE_ATTEMPTS):
            self.assertEqual(t.fail("1.2.3.4"), 0.0)
            self.assertEqual(t.check("1.2.3.4"), 0.0)
        wait1 = t.fail("1.2.3.4")
        self.assertGreater(wait1, 0)
        self.assertGreater(t.check("1.2.3.4"), 0)
        # 另一个 IP 不受影响
        self.assertEqual(t.check("5.6.7.8"), 0.0)
        t.reset("1.2.3.4")
        self.assertEqual(t.check("1.2.3.4"), 0.0)


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = mod.Audit(Path(self.tmp.name) / "audit.log")
        self.accel = FakeAccel()
        self.runner = mod.JobRunner(self.accel, self.audit)

    def tearDown(self):
        self.tmp.cleanup()

    def _wait(self, job, timeout=5.0):
        import time
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end and job.ended is None:
            _t.sleep(0.02)
        return job

    def test_dispatch_add_and_update(self):
        job = self.runner.submit("add", {"hostname": "b.example.org", "service": "http://127.0.0.1:9"}, "test")
        self._wait(job)
        self.assertEqual(job.status, "done", job.error)
        self.assertIn(("add", "b.example.org", "http://127.0.0.1:9", False), self.accel.calls)

        job2 = self.runner.submit(
            "update", {"hostname": "a.example.org", "service": "http://127.0.0.1:9090"}, "test"
        )
        self._wait(job2)
        self.assertEqual(job2.status, "done", job2.error)
        self.assertIn(("restart",), self.accel.calls)

    def test_dispatch_error_is_reported_not_raised(self):
        self.accel.remove_service = mock.Mock(side_effect=RuntimeError("boom"))
        job = self.runner.submit("delete", {"hostname": "a.example.org"}, "test")
        self._wait(job)
        self.assertEqual(job.status, "error")
        self.assertIn("boom", job.error)
        snap = job.snapshot()
        self.assertEqual(snap["status"], "error")

    def test_logs_are_captured_from_cprint(self):
        def noisy(msg="", color=None):
            self.accel.cprint(msg, color)

        self.accel.upsert_tunnel_hostname = lambda h, s: (noisy("改了 ingress"), True)[1]
        job = self.runner.submit(
            "update", {"hostname": "a.example.org", "service": "http://127.0.0.1:8080"}, "test"
        )
        self._wait(job)
        texts = [l["m"] for l in job.snapshot()["lines"]]
        self.assertTrue(any("改了 ingress" in t for t in texts))
        # 任务结束后必须恢复原来的 cprint，不能污染进程
        self.assertEqual(self.accel.cprint.__name__, "cprint")


class CdnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = mod.Audit(Path(self.tmp.name) / "audit.log")
        self.accel = FakeAccel()
        self.runner = mod.JobRunner(self.accel, self.audit)

    def tearDown(self):
        self.tmp.cleanup()

    def _wait(self, job, timeout=5.0):
        import time
        end = time.time() + timeout
        while time.time() < end and job.ended is None:
            time.sleep(0.02)
        return job

    def test_build_cdn_lists_candidates_and_current(self):
        cdn = mod.build_cdn(self.accel)
        self.assertEqual(cdn["entry"], "cdn.example.org")
        self.assertEqual(cdn["target"], "slow.example")
        domains = [c["domain"] for c in cdn["candidates"]]
        self.assertEqual(domains[0], "slow.example")
        self.assertTrue(cdn["candidates"][0]["current"])
        self.assertEqual(cdn["min_improvement"], 15.0)

    def test_cdn_test_never_writes(self):
        job = self.runner.submit("cdn-test", {}, "t")
        self._wait(job)
        self.assertEqual(job.status, "done", job.error)
        self.assertFalse([c for c in self.accel.calls if c[0] in ("set_cdn", "auto_best")])
        result = job.result
        self.assertEqual(result["best"], "fast.example")
        by_domain = {r["domain"]: r for r in result["ranked"]}
        self.assertTrue(by_domain["fast.example"]["usable"])
        # HTTPS 全挂的候选即使 TCP 更快，也不能被当成可用
        self.assertFalse(by_domain["slow.example"]["usable"])
        self.assertEqual(result["ranked"][-1]["domain"], "slow.example")

    def test_cdn_set_switches_target(self):
        job = self.runner.submit("cdn-set", {"target": "fast.example"}, "t")
        self._wait(job)
        self.assertEqual(job.status, "done", job.error)
        self.assertIn(("set_cdn", "fast.example"), self.accel.calls)
        self.assertEqual(job.result["changed"], True)

    def test_cdn_best_goes_through_auto_apply(self):
        job = self.runner.submit("cdn-best", {}, "t")
        self._wait(job)
        self.assertEqual(job.status, "done", job.error)
        self.assertIn(("auto_best", True), self.accel.calls)
        self.assertEqual(job.result["target"], "fast.example")


class StaticGuardTest(unittest.TestCase):
    def test_static_path_resolution_stays_inside(self):
        base = mod.STATIC_DIR.resolve()
        for evil in ("../server.py", "../../etc/passwd", "a/../../server.py", "sub/../../../server.py"):
            target = (mod.STATIC_DIR / evil.lstrip("/")).resolve()
            inside = True
            try:
                target.relative_to(base)
            except ValueError:
                inside = False
            self.assertFalse(inside, f"{evil} 不应落在 static 内")

    def test_static_dir_has_expected_assets(self):
        for name in ("index.html", "style.css", "app.js", "mark.svg"):
            self.assertTrue((mod.STATIC_DIR / name).is_file(), f"缺少 {name}")


class StateShapeTest(unittest.TestCase):
    def test_build_state_hides_fallback_and_reports_meta(self):
        accel = FakeAccel()
        state = mod.build_state(accel)
        hosts = [r["hostname"] for r in state["routes"]]
        self.assertEqual(hosts, ["a.example.org"])  # 回退源被隐藏
        self.assertEqual(state["counts"]["routes"], 1)
        self.assertEqual(state["preferred"]["entry"], "cdn.example.org")
        self.assertEqual(state["version"], "9.9.9")

    def test_build_host_includes_probe_and_saas(self):
        accel = FakeAccel()
        host = mod.build_host(accel, "a.example.org", with_https=True)
        self.assertEqual(host["service"], "http://127.0.0.1:80")
        self.assertEqual(host["saas"]["status"], "active")
        self.assertTrue(host["probe"]["ok"])
        self.assertTrue(host["https"]["ok"])


class SessionTest(unittest.TestCase):
    def test_session_lifecycle_and_expiry(self):
        sessions = mod.Sessions(ttl=1)
        sess = sessions.create("127.0.0.1")
        self.assertEqual(len(sess["csrf"]), 32)
        self.assertIsNotNone(sessions.get(sess["sid"]))
        sessions.drop(sess["sid"])
        self.assertIsNone(sessions.get(sess["sid"]))

    def test_expired_session_is_collected(self):
        sessions = mod.Sessions(ttl=-1)
        sess = sessions.create("127.0.0.1")
        self.assertIsNone(sessions.get(sess["sid"]))


class OriginCheckTest(unittest.TestCase):
    def _handler(self, headers):
        h = mod.Handler.__new__(mod.Handler)
        h.headers = headers
        return h

    def test_missing_origin_is_allowed_for_cli(self):
        self.assertTrue(self._handler({})._same_origin())

    def test_cross_site_fetch_is_blocked(self):
        self.assertFalse(self._handler({"Sec-Fetch-Site": "cross-site"})._same_origin())

    def test_origin_must_match_host(self):
        self.assertTrue(
            self._handler({"Origin": "http://a.b:8642", "Host": "a.b:8642"})._same_origin()
        )
        self.assertFalse(
            self._handler({"Origin": "http://evil.example", "Host": "a.b:8642"})._same_origin()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
