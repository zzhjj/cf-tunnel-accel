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
if __name__ == "__main__":
    unittest.main(verbosity=2)
