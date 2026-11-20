"""Offline unit tests for CDN HTTPS measurements and selection policy."""
from __future__ import annotations

import importlib.util
import math
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "cfa_cdn.py"
SPEC = importlib.util.spec_from_file_location("cfa_cdn_under_test", MODULE_PATH)
cdn = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(cdn)


class CdnPolicyTests(unittest.TestCase):
    def test_latency_score_requires_successful_https_and_ignores_tcp(self):
        self.assertEqual(cdn.latency_score({"median_https": 12.5, "https_ok": 2}), 12.5)
        self.assertIsNone(cdn.latency_score({"median_tcp": 1, "tcp_ok": 4}))
        self.assertIsNone(cdn.latency_score({"median_https": 12.5, "https_ok": 0}))
        self.assertIsNone(cdn.latency_score({"median_https": float("nan"), "https_ok": 2}))
        self.assertIsNone(cdn.latency_score({"median_https": -1, "https_ok": 2}))

    def test_eligibility_requires_two_successful_samples_rate_and_optional_business(self):
        row = {
            "https_ok": 2,
            "https_tries": 2,
            "https_success_rate": 1,
            "median_https": 15,
            "business_verified": False,
        }
        self.assertTrue(cdn.eligible(row))
        self.assertFalse(cdn.eligible(row, require_business=True))
        row["business_verified"] = True
        self.assertTrue(cdn.eligible(row, require_business=True))
        self.assertFalse(cdn.eligible({**row, "https_ok": 1, "https_tries": 1}))
        self.assertFalse(cdn.eligible({**row, "https_ok": 2, "https_tries": 3}))

    def test_5xx_status_cannot_be_counted_as_success(self):
        row = {
            "https_ok": 2,
            "https_tries": 2,
            "https_success_rate": 1,
            "http_statuses": [200, 502],
            "median_https": 12,
            "business_verified": True,
        }
        self.assertFalse(cdn.eligible(row, require_business=True))

    def test_rank_prefers_eligible_https_then_rate_and_latency(self):
        tcp_only = {"domain": "tcp.example", "median_tcp": 1, "tcp_ok": 4}
        slow = {
            "domain": "slow.example", "https_ok": 2, "https_tries": 2,
            "https_success_rate": 1, "median_https": 45, "business_verified": True,
        }
        fast = {
            "domain": "fast.example", "https_ok": 3, "https_tries": 3,
            "https_success_rate": 1, "median_https": 20, "business_verified": True,
        }
        self.assertEqual([r["domain"] for r in cdn.rank([tcp_only, slow, fast])],
                         ["fast.example", "slow.example", "tcp.example"])


class HttpsSamplingTests(unittest.TestCase):
    def _completed(self, output: str, returncode: int = 0):
        return subprocess.CompletedProcess([], returncode, output, "")

    def test_uses_probe_host_for_url_sni_and_resolve_with_ipv6(self):
        calls = []

        def run(command, check=False, timeout=None):
            calls.append((command, check, timeout))
            return self._completed('302 0.020 0.035 2001:db8::15 https://business.example/')

        tls, ttfb, statuses = cdn.https_samples(
            "network-cdn.example", "2001:db8::15", tries=1, timeout=4,
            probe_hostname="business.example", run=run,
        )
        self.assertEqual(statuses, [302])
        self.assertEqual(tls, [20.0])
        self.assertEqual(ttfb, [35.0])
        command, check, timeout = calls[0]
        self.assertIn("--noproxy", command)
        self.assertEqual(command[command.index("--resolve") + 1],
                         "business.example:443:[2001:db8::15]")
        self.assertEqual(command[-1], "https://business.example/")
        self.assertIn("-L", command)
        self.assertFalse(check)
        self.assertEqual(timeout, 10.0)

    def test_uses_candidate_for_host_when_probe_hostname_is_empty(self):
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            return self._completed('204 0.1 0.2 192.0.2.11 https://edge.example/')

        cdn.https_samples("edge.example", "192.0.2.11", tries=1, run=run)
        self.assertEqual(seen[0][-1], "https://edge.example/")
        self.assertEqual(seen[0][seen[0].index("--resolve") + 1],
                         "edge.example:443:192.0.2.11")

    def test_redirect_leaving_selected_ip_or_hostname_is_not_route_evidence(self):
        for output in ('200 0.02 0.04 192.0.2.99 https://business.example/',
                       '200 0.02 0.04 192.0.2.1 https://other.example/'):
            with self.subTest(output=output):
                result = cdn.https_samples('edge.example', '192.0.2.1', tries=2,
                    probe_hostname='business.example', run=lambda *a, **kw: self._completed(output))
                self.assertEqual(result, ([], [], []))


    def test_curl_failure_redirect_to_5xx_nan_and_zero_timing_are_discarded(self):
        outputs = iter([
            self._completed("200 0.1 0.2", returncode=28),
            self._completed("502 0.1 0.2"),
            self._completed("200 nan 0.2"),
            self._completed("301 0.0 0.2"),
            self._completed('404 0.025 0.050 192.0.2.1 https://edge.example/'),
        ])
        tls, ttfb, statuses = cdn.https_samples(
            "edge.example", "192.0.2.1", tries=5, run=lambda *a, **k: next(outputs)
        )
        self.assertEqual(statuses, [404])
        self.assertEqual(tls, [25.0])
        self.assertEqual(ttfb, [50.0])

    def test_invalid_address_and_probe_hostname_rejected(self):
        with self.assertRaises(ValueError):
            cdn.https_samples("edge.example", "not-an-ip", tries=1)
        with self.assertRaises(ValueError):
            cdn.https_samples("edge.example", "192.0.2.1", tries=1,
                              probe_hostname="bad host.example")


class FakeContext:
    class CFError(RuntimeError):
        pass

    PROBE_HOSTNAME = "service.example"
    CDN_MIN_SUCCESS_RATE = 0.8
    CDN_SWITCH_MIN_IMPROVEMENT_PERCENT = 15
    CDN_CANDIDATES = []

    def __init__(self, rows, current="current.example", verifies=(True,)):
        self.rows = rows
        self.current = current
        self.verifies = iter(verifies)
        self.writes = []
        self.displayed = []

    def normalize_cdn_target(self, raw):
        value = str(raw).strip().lower().rstrip(".")
        if not value or "/" in value:
            raise ValueError("invalid host")
        return value

    def get_current_preferred_target(self, strict=True):
        return self.current

    def ensure_candidate_list(self):
        return list(self.CDN_CANDIDATES)

    def benchmark_cdn(self, domain):
        return dict(self.rows[domain], domain=domain)

    def print_cdn_benchmark(self, results, current=None):
        self.displayed = list(results)
        return results

    def set_preferred_target(self, target):
        self.writes.append(target)
        self.current = target

    def verify_https(self, hostname):
        self.assert_probe = hostname
        return next(self.verifies)


def row(domain, latency, *, ok=3, tries=3, rate=1.0, business=True, statuses=None):
    value = {
        "domain": domain,
        "https_ok": ok,
        "https_tries": tries,
        "https_success_rate": rate,
        "median_https": latency,
        "avg_ttfb": latency + 5,
        "business_verified": business,
    }
    if statuses is not None:
        value["http_statuses"] = statuses
    return value


class AutomaticSelectionTests(unittest.TestCase):
    def test_current_route_is_measured_and_hysteresis_blocks_small_gain(self):
        ctx = FakeContext({
            "current.example": row("current.example", 100),
            "faster.example": row("faster.example", 90),
        })
        result = cdn.select_and_switch(ctx, auto_apply_best=True,
                                       candidates=["faster.example"])
        self.assertEqual(result, "current.example")
        self.assertEqual(ctx.writes, [])
        self.assertEqual({r["domain"] for r in ctx.displayed},
                         {"current.example", "faster.example"})

    def test_force_bypasses_only_latency_hysteresis(self):
        ctx = FakeContext({
            "current.example": row("current.example", 100),
            "faster.example": row("faster.example", 90),
        })
        result = cdn.select_and_switch(ctx, auto_apply_best=True, force=True,
                                       candidates=["faster.example"])
        self.assertEqual(result, "faster.example")
        self.assertEqual(ctx.writes, ["faster.example"])
        self.assertEqual(ctx.assert_probe, "service.example")

    def test_force_does_not_make_ineligible_target_writable(self):
        ctx = FakeContext({
            "current.example": row("current.example", 100),
            "tcp-only.example": {"domain": "tcp-only.example", "median_tcp": 1,
                                  "tcp_ok": 4, "business_verified": True},
        })
        result = cdn.select_and_switch(ctx, auto_apply_best=True, force=True,
                                       candidates=["tcp-only.example"])
        self.assertEqual(result, "current.example")
        self.assertEqual(ctx.writes, [])

    def test_current_unavailable_triggers_recovery_even_when_best_is_slower(self):
        ctx = FakeContext({
            "current.example": row("current.example", 50, ok=0, tries=3, rate=0),
            "restored.example": row("restored.example", 200),
        })
        result = cdn.select_and_switch(ctx, auto_apply_best=True,
                                       candidates=["restored.example"])
        self.assertEqual(result, "restored.example")
        self.assertEqual(ctx.writes, ["restored.example"])

    def test_business_verification_is_mandatory_for_automatic_selection(self):
        ctx = FakeContext({
            "current.example": row("current.example", 100, business=False),
            "network-only.example": row("network-only.example", 10, business=False),
        })
        with self.assertRaises(FakeContext.CFError):
            cdn.select_and_switch(ctx, auto_apply_best=True,
                                  candidates=["network-only.example"])
        self.assertEqual(ctx.writes, [])

    def test_business_verified_winner_can_be_compared_with_nonverified_current(self):
        ctx = FakeContext({
            "current.example": row("current.example", 10, business=False),
            "business.example": row("business.example", 200, business=True),
        })
        result = cdn.select_and_switch(ctx, auto_apply_best=True,
                                       candidates=["business.example"])
        self.assertEqual(result, "business.example")
        self.assertEqual(ctx.writes, ["business.example"])

    def test_all_unavailable_and_explicit_empty_candidate_list_never_write(self):
        ctx = FakeContext({
            "current.example": row("current.example", 5, ok=0, tries=3, rate=0),
            "broken.example": row("broken.example", float("nan"), ok=0, tries=3, rate=0),
        })
        with self.assertRaises(FakeContext.CFError):
            cdn.select_and_switch(ctx, auto_apply_best=True, force=True,
                                  candidates=["broken.example"])
        with self.assertRaises(FakeContext.CFError):
            cdn.select_and_switch(ctx, auto_apply_best=True, candidates=[])
        self.assertEqual(ctx.writes, [])

    def test_requires_two_samples_and_minimum_rate_even_with_force(self):
        ctx = FakeContext({
            "current.example": row("current.example", 5),
            "one-sample.example": row("one-sample.example", 1, ok=1, tries=1, rate=1),
            "low-rate.example": row("low-rate.example", 1, ok=2, tries=3, rate=2 / 3),
        })
        self.assertEqual(
            cdn.select_and_switch(ctx, auto_apply_best=True, force=True,
                                 candidates=["one-sample.example"]),
            "current.example",
        )
        self.assertEqual(
            cdn.select_and_switch(ctx, auto_apply_best=True, force=True,
                                 candidates=["low-rate.example"]),
            "current.example",
        )
        self.assertEqual(ctx.writes, [])

    def test_switch_verification_failure_rolls_back_previous_target(self):
        ctx = FakeContext({
            "current.example": row("current.example", 100),
            "new.example": row("new.example", 10),
        }, verifies=(False, True))
        with self.assertRaisesRegex(FakeContext.CFError, "回滚"):
            cdn.select_and_switch(ctx, auto_apply_best=True,
                                  candidates=["new.example"])
        self.assertEqual(ctx.writes, ["new.example", "current.example"])
        self.assertEqual(ctx.current, "current.example")

    def test_no_prior_target_prevents_automatic_write_without_rollback_point(self):
        ctx = FakeContext({"candidate.example": row("candidate.example", 10)}, current="")
        with self.assertRaises(FakeContext.CFError):
            cdn.select_and_switch(ctx, auto_apply_best=True,
                                  candidates=["candidate.example"])
        self.assertEqual(ctx.writes, [])

    def test_candidate_list_is_bounded_and_normalized(self):
        ctx = FakeContext({"current.example": row("current.example", 10)})
        with self.assertRaises(FakeContext.CFError):
            cdn.select_and_switch(ctx, candidates=[f"cdn{i}.example" for i in range(33)])
        self.assertEqual(ctx.writes, [])


if __name__ == "__main__":
    unittest.main()
