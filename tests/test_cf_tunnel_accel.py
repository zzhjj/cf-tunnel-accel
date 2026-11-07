import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_HERE = Path(__file__).resolve()
_CANDIDATES = [
    Path(os.environ["CF_TUNNEL_ACCEL_SCRIPT"])
    if os.environ.get("CF_TUNNEL_ACCEL_SCRIPT")
    else None,
    _HERE.parent.parent / "cf-tunnel-accel",  # 仓库根目录
    Path("/root/bin/cf-tunnel-accel"),
]
SCRIPT = next(str(p) for p in _CANDIDATES if p is not None and p.is_file())
loader = importlib.machinery.SourceFileLoader("cf_tunnel_accel", SCRIPT)
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class CfTunnelAccelTests(unittest.TestCase):
    def test_normalizers(self):
        self.assertEqual(mod.normalize_hostname("https://APP.Example.com/a"), "app.example.com")
        self.assertEqual(mod.normalize_service(":8080"), "http://127.0.0.1:8080")
        self.assertEqual(mod.normalize_service("https://localhost:443"), "https://127.0.0.1:443")
        with self.assertRaises(mod.CFError):
            mod.normalize_service("127.0.0.1:99999")

    def test_ingress_parse_and_fallback(self):
        lines = [
            "ingress:",
            "  - hostname: app.example.com # keep", 
            "    service: http://127.0.0.1:8080",
            "", "  - service: http_status:404",
        ]
        self.assertTrue(mod._has_ingress_hostname(lines, "app.example.com"))
        self.assertTrue(mod._ensure_fallback_ingress(lines))
        self.assertTrue(mod._has_ingress_hostname(lines, mod.FALLBACK_ORIGIN))
        parsed = dict(mod.parse_ingress_hostnames("\n".join(lines)))
        self.assertEqual(parsed["app.example.com"], "http://127.0.0.1:8080")
        self.assertEqual(parsed[mod.FALLBACK_ORIGIN], "http://127.0.0.1:8317")
        self.assertFalse(mod._ensure_fallback_ingress(lines))

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "config.yml"
            target.write_text("old\n", encoding="utf-8")
            target.chmod(0o640)
            mod.atomic_write_text(target, "new\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(Path(td).glob(".*.tmp.*")), [])

    def test_cdn_ranking_prefers_https_availability_then_latency(self):
        ranked = mod.rank_cdn_results([
            {"domain": "tcp-only.example", "https_ok": 0, "https_success_rate": 0, "median_https": None, "avg_ttfb": None, "median_tcp": 1, "best_ping": 1},
            {"domain": "slow-https.example", "https_ok": 1, "https_success_rate": 1, "median_https": 50, "avg_ttfb": 60, "median_tcp": 10, "best_ping": 2},
            {"domain": "fast-https.example", "https_ok": 1, "https_success_rate": 1, "median_https": 20, "avg_ttfb": 30, "median_tcp": 12, "best_ping": 3},
        ])
        self.assertEqual([x["domain"] for x in ranked], [
            "fast-https.example", "slow-https.example", "tcp-only.example"
        ])
        self.assertEqual(mod.cdn_latency_score(ranked[0]), 20.0)

    def test_api_without_token_is_local_failure(self):
        original = mod.CF_API_TOKEN
        try:
            mod.CF_API_TOKEN = ""
            response = mod.api("GET", "/zones")
            self.assertFalse(response["success"])
            self.assertEqual(response["errors"][0]["code"], "token_missing")
        finally:
            mod.CF_API_TOKEN = original



class ZoneDiscoveryTests(unittest.TestCase):
    """域名清单/可用性判定：全部离线，不碰真实账号。"""

    def setUp(self):
        self._cache = dict(mod._zone_cache)
        self._info = {k: dict(v) for k, v in mod._zone_info.items()}
        self._path = mod.ZONE_CACHE_PATH

    def tearDown(self):
        mod._zone_cache.clear()
        mod._zone_cache.update(self._cache)
        mod._zone_info.clear()
        mod._zone_info.update(self._info)
        mod.ZONE_CACHE_PATH = self._path

    def test_zone_of_host_prefers_longest_match(self):
        mod._zone_cache.clear()
        mod._zone_cache.update({"example.com": "z1", "sub.example.com": "z2"})
        self.assertEqual(mod.zone_of_host("a.sub.example.com"), "sub.example.com")
        self.assertEqual(mod.zone_of_host("a.example.com"), "example.com")
        self.assertIsNone(mod.zone_of_host("notlisted.test"))
        # 根域名本身也是合法 zone（apex 可作为业务域名），不是必须带子域
        self.assertEqual(mod.zone_of_host("example.com"), "example.com")

    def test_resolve_zone_uses_cache_without_network(self):
        mod._zone_cache.clear()
        mod._zone_cache.update({"example.com": "z1"})
        original = mod.api
        try:
            mod.api = lambda *a, **kw: self.fail("缓存命中时不应发请求")
            self.assertEqual(mod.resolve_zone("app.example.com"), ("example.com", "z1"))
        finally:
            mod.api = original

    def test_zone_cache_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "zones.json"
            mod.ZONE_CACHE_PATH = target
            mod._zone_info.clear()
            mod._zone_cache.clear()
            mod._zone_info["example.com"] = {
                "name": "example.com",
                "id": "z1",
                "status": "active",
                "type": "full",
                "plan": "Free Website",
                "paused": False,
                "account": "acct",
                "cached": False,
            }
            mod._zone_cache["example.com"] = "z1"
            mod.save_zone_cache_file()
            self.assertTrue(target.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            mod._zone_info.clear()
            mod._zone_cache.clear()
            mod.load_zone_cache_file()
            self.assertEqual(mod._zone_cache, {"example.com": "z1"})
            self.assertTrue(mod._zone_info["example.com"]["cached"])
            self.assertEqual(mod._zone_info["example.com"]["status"], "active")

    def test_build_zone_inventory_flags_unusable_zones_offline(self):
        mod._zone_info.clear()
        mod._zone_cache.clear()
        mod._zone_cache.update({"ok.example": "z1", "pending.example": "z2"})
        mod._zone_info.update({
            "ok.example": {
                "name": "ok.example", "id": "z1", "status": "active",
                "type": "full", "plan": "Free Website", "paused": False, "cached": True,
            },
            "pending.example": {
                "name": "pending.example", "id": "z2", "status": "pending",
                "type": "full", "plan": "Free Website", "paused": False, "cached": True,
            },
        })
        rows = {r["name"]: r for r in mod.build_zone_inventory(offline=True)}
        self.assertTrue(rows["ok.example"]["usable"])
        self.assertFalse(rows["pending.example"]["usable"])
        self.assertTrue(any("pending" in x for x in rows["pending.example"]["reasons"]))

    def test_read_line_handles_eof(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertIsNone(mod.read_line("输入: "))
        with mock.patch("sys.stdin", io.StringIO("abc\n")):
            self.assertEqual(mod.read_line("输入: "), "abc")

    def test_print_domains_offline_uses_cache_only(self):
        mod._zone_info.clear()
        mod._zone_cache.clear()
        mod._zone_cache["example.com"] = "z1"
        mod._zone_info["example.com"] = {
            "name": "example.com", "id": "z1", "status": "active",
            "type": "full", "plan": "Free Website", "paused": False, "cached": True,
        }
        original = mod.api
        try:
            mod.api = lambda *a, **kw: self.fail("离线模式不应发请求")
            self.assertEqual(mod.print_domains(offline=True), 0)
        finally:
            mod.api = original


class MissingConfigTests(unittest.TestCase):
    """隧道配置缺失（新服务器常见）时不能抛 FileNotFoundError。"""

    def setUp(self):
        self._original = mod.CONFIG_PATH
        mod.CONFIG_PATH = Path(tempfile.gettempdir()) / "cfa-not-exist-9f8a.yml"
        if mod.CONFIG_PATH.exists():
            mod.CONFIG_PATH.unlink()

    def tearDown(self):
        mod.CONFIG_PATH = self._original

    def test_read_config_text_returns_empty_string(self):
        self.assertEqual(mod.read_config_text(), "")

    def test_missing_hint_mentions_path_and_fixes(self):
        hint = mod.config_missing_hint()
        self.assertIn(str(mod.CONFIG_PATH), hint)
        self.assertIn("CF_TUNNEL_ACCEL_CONFIG", hint)
        self.assertIn("--service", hint)

    def test_health_check_fails_gracefully(self):
        self.assertFalse(mod.health_check())

    def test_upsert_tunnel_hostname_raises_cf_error(self):
        with self.assertRaises(mod.CFError):
            mod.upsert_tunnel_hostname("x.example.com", "http://127.0.0.1:1")


class SlowRestartTests(unittest.TestCase):
    """慢重启不能误判成“新配置未生效”而触发回滚（现场事故复现）。"""

    def test_run_timeout_raises_command_timeout(self):
        with self.assertRaises(mod.CommandTimeout):
            mod.run(["sleep", "5"], check=False, timeout=0.05)
        # 仍然是 CFError，老调用点的 except CFError 不受影响
        self.assertTrue(issubclass(mod.CommandTimeout, mod.CFError))

    def test_restart_command_timeout_is_not_fatal(self):
        calls = []

        def fake_run(cmd, check=True, timeout=None):
            calls.append((cmd, check, timeout))
            if cmd[:2] == ["systemctl", "restart"]:
                raise mod.CommandTimeout("命令超时（180s）: systemctl restart x")
            raise AssertionError(f"unexpected: {cmd}")

        original = mod.run
        try:
            mod.run = fake_run
            mod._restart_service()  # 不应抛异常
        finally:
            mod.run = original
        self.assertTrue(any(c[0][:2] == ["systemctl", "restart"] for c in calls))
        # 命令超时要给足余量，不能再用 60s 默认值
        restart_timeout = [c[2] for c in calls if c[0][:2] == ["systemctl", "restart"]][0]
        self.assertGreaterEqual(restart_timeout, 90)

    def test_restart_nonzero_exit_is_not_the_verdict(self):
        # stop 被 SIGKILL 时 systemctl 返回 1，但服务已经拉起；这里只提示不抛错，
        # 成败由 active 轮询 + MainPID 校验判定。
        def fake_run(cmd, check=True, timeout=None):
            return mock.Mock(returncode=1, stdout="", stderr="Job failed")

        original = mod.run
        try:
            mod.run = fake_run
            mod._restart_service()  # 不应抛异常
        finally:
            mod.run = original

    def test_wait_tolerates_transient_failed_state(self):
        """failed 只是 Restart=on-failure 的中间态，不能立刻判死。"""
        states = iter(["activating", "failed", "activating", "active"])
        original_run = mod.run
        original_grace = mod.SERVICE_FAILED_GRACE

        def fake_run(cmd, check=True, timeout=None):
            if cmd[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=3, stdout=next(states) + "\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        try:
            mod.run = fake_run
            mod.SERVICE_FAILED_GRACE = 25
            mod._wait_service_active(timeout=10)  # 不应抛异常
        finally:
            mod.run = original_run
            mod.SERVICE_FAILED_GRACE = original_grace

    def test_wait_gives_up_after_failed_grace(self):
        original_run = mod.run
        original_grace = mod.SERVICE_FAILED_GRACE

        def fake_run(cmd, check=True, timeout=None):
            if cmd[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=3, stdout="failed\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        try:
            mod.run = fake_run
            mod.SERVICE_FAILED_GRACE = 1
            with self.assertRaises(mod.CFError):
                mod._wait_service_active(timeout=10)
        finally:
            mod.run = original_run
            mod.SERVICE_FAILED_GRACE = original_grace

    def test_restart_that_keeps_old_pid_is_fatal(self):
        """配置写盘了但进程没换 —— 这才是真失败，必须回滚。"""

        def fake_run(cmd, check=True, timeout=None):
            if cmd[:2] == ["systemctl", "show"]:
                return mock.Mock(returncode=0, stdout="4242\n", stderr="")
            if cmd[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        original_run = mod.run
        try:
            mod.run = fake_run
            with self.assertRaises(mod.CFError):
                mod._validate_and_restart_once()
        finally:
            mod.run = original_run

    def test_validate_and_restart_survives_slow_stop(self):
        """校验通过 + 重启命令超时 + 随后 active ⇒ 视为成功，不回滚。"""
        original_run = mod.run
        original_bin = mod.CLOUDFLARED_BIN

        pids = iter([111, 222])  # 重启前 / 重启后：进程确实换了

        def fake_run(cmd, check=True, timeout=None):
            if cmd[:2] == ["systemctl", "restart"]:
                raise mod.CommandTimeout("命令超时（180s）: systemctl restart x")
            if cmd[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            if cmd[:2] == ["systemctl", "show"]:
                return mock.Mock(returncode=0, stdout=f"{next(pids)}\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        try:
            mod.run = fake_run
            mod._validate_and_restart_once()  # 不抛异常即通过
        finally:
            mod.run = original_run
            mod.CLOUDFLARED_BIN = original_bin


class StaleSeedZoneTests(unittest.TestCase):
    """脚本内置的过期 zone ID 不能污染解析结果（现场 23 条假 FAIL）。"""

    def setUp(self):
        self._cache = dict(mod._zone_cache)
        self._info = {k: dict(v) for k, v in mod._zone_info.items()}
        self._from_disk = set(mod._zone_cache_from_disk)
        self._path = mod.ZONE_CACHE_PATH
        self._api = mod.api

    def tearDown(self):
        mod._zone_cache.clear()
        mod._zone_cache.update(self._cache)
        mod._zone_info.clear()
        mod._zone_info.update(self._info)
        mod._zone_cache_from_disk.clear()
        mod._zone_cache_from_disk.update(self._from_disk)
        mod.ZONE_CACHE_PATH = self._path
        mod.api = self._api

    def test_refresh_drops_zones_the_account_cannot_read(self):
        with tempfile.TemporaryDirectory() as td:
            mod.ZONE_CACHE_PATH = Path(td) / "zones.json"
            mod._zone_cache.clear()
            mod._zone_info.clear()
            mod._zone_cache_from_disk.clear()
            # 模拟：内置种子 + 磁盘缓存里都有账号里已不存在的 dead.example
            mod._zone_cache.update({"live.example": "z1", "dead.example": "z9"})
            mod._zone_cache_from_disk.update({"dead.example"})
            mod._zone_info["dead.example"] = {
                "name": "dead.example", "id": "z9", "status": "active",
                "type": "full", "plan": "Free", "paused": False, "cached": True,
            }

            def fake_api(method, path, body=None, timeout=45):
                self.assertEqual(path.split("?")[0], "/zones")
                return {
                    "success": True,
                    "result": [{"id": "z1", "name": "live.example", "status": "active",
                                "type": "full", "paused": False,
                                "plan": {"name": "Free"}, "account": {"name": "acct"}}],
                    "result_info": {"total_pages": 1},
                }

            mod.api = fake_api
            mod.refresh_zone_cache()
            self.assertEqual(mod._zone_cache, {"live.example": "z1"})
            self.assertNotIn("dead.example", mod._zone_info)
            self.assertIsNone(mod.zone_of_host("a.dead.example"))
            # 解析失败要退到 INFO（None），而不是拿死 ID 去发 DNS 请求
            self.assertIsNone(mod.resolve_zone("a.dead.example"))
if __name__ == "__main__":
    unittest.main(verbosity=2)
