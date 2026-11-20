"""CDN HTTPS measurement and fail-safe automatic selection policy.

This module has no dependency on the CLI or network configuration.  Network,
DNS, and write operations are supplied by the caller through a small context
object; that keeps the policy independently testable and makes sampling easy
to stub in offline tests.
"""

from __future__ import annotations

import ipaddress
import math
import re
import subprocess
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from urllib.parse import urlsplit

DEFAULT_MIN_SUCCESS_RATE = 0.8
MIN_HTTPS_SAMPLES = 2
MAX_CANDIDATES = 32
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _valid_http_status(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if str(value).strip() not in (str(status), f"{status}.0"):
        return None
    return status if 200 <= status < 500 else None


def _https_metrics(row: Mapping[str, Any]) -> tuple[int, Optional[int], float]:
    """Return (successful samples, attempts, success rate) conservatively."""
    statuses_present = "http_statuses" in row
    statuses = row.get("http_statuses")
    valid_statuses: Optional[list[int]] = None
    if statuses_present and isinstance(statuses, (list, tuple)):
        valid_statuses = [
            code for item in statuses
            if (code := _valid_http_status(item)) is not None
        ]

    successful = _count(row.get("https_ok"))
    if successful is None:
        successful = _count(row.get("https_samples"))
    if successful is None and valid_statuses is not None:
        successful = len(valid_statuses)
    if successful is None:
        successful = 0
    if valid_statuses is not None:
        # Do not let a stale aggregate count make a recorded 5xx look successful.
        successful = min(successful, len(valid_statuses))

    attempts = _count(row.get("https_tries"))
    if attempts is None and statuses_present and isinstance(statuses, (list, tuple)):
        attempts = len(statuses)
    if attempts is None and successful:
        attempts = successful

    explicit_rate = _finite_number(row.get("https_success_rate"))
    if explicit_rate is not None and 0.0 <= explicit_rate <= 1.0:
        rate = explicit_rate
    elif attempts:
        rate = successful / attempts
    else:
        rate = 0.0

    if attempts:
        rate = min(rate, successful / attempts)
    return successful, attempts, rate


def latency_score(row: Mapping[str, Any]) -> Optional[float]:
    """Return the measured HTTPS median in milliseconds, never a TCP fallback."""
    if not isinstance(row, Mapping):
        return None
    score = _finite_number(row.get("median_https"))
    if score is None or score <= 0:
        return None
    successes, _, _ = _https_metrics(row)
    if successes < 1:
        return None
    return score


def _eligible(
    row: Mapping[str, Any], require_business: bool, min_success_rate: float
) -> bool:
    if not isinstance(row, Mapping):
        return False
    if require_business and row.get("business_verified") is not True:
        return False
    successes, _, rate = _https_metrics(row)
    return (
        successes >= MIN_HTTPS_SAMPLES
        and rate >= min_success_rate
        and latency_score(row) is not None
    )


def eligible(row: Mapping[str, Any], require_business: bool = False) -> bool:
    """Whether a benchmark row has enough successful, timed HTTPS evidence."""
    return _eligible(row, require_business, DEFAULT_MIN_SUCCESS_RATE)


def _sort_number(value: Any, default: float = math.inf) -> float:
    number = _finite_number(value)
    return number if number is not None and number > 0 else default


def rank(results: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Rank usable HTTPS routes first, then reliability and HTTPS latency."""
    rows = list(results or [])

    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        if not isinstance(row, Mapping):
            return (True, 0.0, math.inf, math.inf, "")
        _, _, rate = _https_metrics(row)
        return (
            not eligible(row),
            -rate,
            latency_score(row) or math.inf,
            _sort_number(row.get("avg_ttfb")),
            str(row.get("domain") or "").lower(),
        )

    return sorted(rows, key=key)


def _default_run(command: Sequence[str], check: bool = False, timeout: Any = None) -> Any:
    return subprocess.run(
        list(command),
        check=check,
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _hostname(value: str) -> str:
    host = (value or "").strip().rstrip(".").lower()
    if not host or len(host) > 253:
        raise ValueError("HTTPS 探测主机名无效")
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("HTTPS 探测主机名无效")
    return host


def https_samples(
    domain: str,
    ip: str,
    tries: int = 3,
    timeout: float = 6,
    probe_hostname: str = "",
    run: Callable[..., Any] = _default_run,
) -> tuple[list[float], list[float], list[int]]:
    """Probe an IP with curl while retaining the requested Host header and SNI.

    Redirects are followed, so the recorded HTTP code and timings belong to the
    final response.  Only curl-successful requests ending in 2xx--4xx with
    finite, positive TLS and TTFB measurements are included in the result.
    """
    try:
        count = int(tries)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("HTTPS 探测次数必须是非负整数") from None
    if isinstance(tries, bool) or count < 0 or str(tries).strip() not in (str(count), f"{count}.0"):
        raise ValueError("HTTPS 探测次数必须是非负整数")
    limit = _finite_number(timeout)
    if limit is None or limit <= 0:
        raise ValueError("HTTPS 探测超时必须是正数")

    address = str(ipaddress.ip_address(str(ip).strip()))
    resolve_address = f"[{address}]" if ":" in address else address
    request_host = _hostname(probe_hostname or domain)
    tls_samples: list[float] = []
    ttfb_samples: list[float] = []
    statuses: list[int] = []

    for _ in range(count):
        command = [
            "curl", "-sS", "-L", "--max-redirs", "10",
            "--noproxy", "*", "-o", "/dev/null",
            "--connect-timeout", str(limit), "--max-time", str(limit),
            "--resolve", f"{request_host}:443:{resolve_address}",
            '-w', '%{http_code} %{time_appconnect} %{time_starttransfer} %{remote_ip} %{url_effective}',
            f"https://{request_host}/",
        ]
        try:
            completed = run(command, check=False, timeout=max(10.0, limit + 5.0))
        except Exception:
            continue
        if getattr(completed, "returncode", 1) != 0:
            continue
        output = getattr(completed, "stdout", "") or ""
        if isinstance(output, bytes):
            output = output.decode("ascii", errors="replace")
        fields = str(output).strip().split()
        if len(fields) < 3:
            continue
        if len(fields) != 5:
            continue  # No endpoint evidence -> never attribute a sample to this route.
        try:
            final_address = str(ipaddress.ip_address(fields[3]))
            final_url = urlsplit(fields[4])
            if (final_address != address or final_url.scheme != 'https'
                    or final_url.hostname != request_host or final_url.port not in (None, 443)):
                continue
        except ValueError:
            continue
        try:
            status = int(fields[0])
        except (TypeError, ValueError, OverflowError):
            continue
        tls_seconds = _finite_number(fields[1])
        ttfb_seconds = _finite_number(fields[2])
        if not (200 <= status < 500):
            continue
        if tls_seconds is None or ttfb_seconds is None or tls_seconds <= 0 or ttfb_seconds <= 0:
            continue
        tls_ms = tls_seconds * 1000.0
        ttfb_ms = ttfb_seconds * 1000.0
        if not (math.isfinite(tls_ms) and math.isfinite(ttfb_ms)):
            continue
        statuses.append(status)
        tls_samples.append(tls_ms)
        ttfb_samples.append(ttfb_ms)

    return tls_samples, ttfb_samples, statuses


def _make_error(ctx: Any, message: str) -> Exception:
    error_type = getattr(ctx, "CFError", ValueError) if ctx is not None else ValueError
    if not isinstance(error_type, type) or not issubclass(error_type, Exception):
        error_type = ValueError
    return error_type(message)


def _normalize_candidates(ctx: Any, values: Iterable[Any], current: str) -> list[str]:
    if values is None or isinstance(values, (str, bytes)):
        raise _make_error(ctx, "CDN 候选必须是非空域名列表")
    try:
        raw_items = list(values)
    except TypeError:
        raise _make_error(ctx, "CDN 候选必须是非空域名列表") from None
    if len(raw_items) > MAX_CANDIDATES:
        raise _make_error(ctx, f"CDN 候选最多 {MAX_CANDIDATES} 个")
    normalize = getattr(ctx, "normalize_cdn_target", None)
    if not callable(normalize):
        raise _make_error(ctx, "CDN 目标规范化函数未配置")

    normalized: list[str] = []
    for raw in raw_items:
        try:
            target = normalize(raw)
        except Exception as exc:
            raise _make_error(ctx, f"CDN 候选无效: {exc}") from None
        if not isinstance(target, str) or not target.strip():
            raise _make_error(ctx, "CDN 候选无效")
        target = target.strip().lower().rstrip(".")
        if target not in normalized:
            normalized.append(target)
    if current and current not in normalized:
        if len(normalized) >= MAX_CANDIDATES:
            raise _make_error(ctx, f"加入当前 CDN 后候选超过 {MAX_CANDIDATES} 个")
        normalized.insert(0, current)
    if not normalized:
        raise _make_error(ctx, "没有可测速的 CDN 候选")
    return normalized


def _verify_result(value: Any) -> bool:
    if isinstance(value, tuple):
        return bool(value[0]) if value else False
    if isinstance(value, Mapping):
        return bool(value.get("ok", value.get("success", False)))
    return bool(value)


def _set_target(ctx: Any, target: str) -> None:
    ctx.set_preferred_target(target)


def _rollback(ctx: Any, previous: str) -> tuple[bool, str]:
    try:
        _set_target(ctx, previous)
    except Exception as exc:
        return False, f"回滚写入失败: {exc}"
    try:
        verified = _verify_result(ctx.verify_https(ctx.PROBE_HOSTNAME))
    except Exception as exc:
        return False, f"已恢复目标 {previous}，但回滚验收异常: {exc}"
    if verified:
        return True, f"已回滚至 {previous}，HTTPS 验收通过"
    return False, f"已恢复目标 {previous}，但回滚后的 HTTPS 验收未通过"


def select_and_switch(
    ctx: Any,
    auto_apply_best: bool = False,
    candidates: Optional[Sequence[str]] = None,
    interactive: bool = False,
    force: bool = False,
) -> Optional[str]:
    """Benchmark bounded CDN candidates and optionally switch with rollback.

    Automatic changes require explicit business-host verification and at least
    two successful HTTPS samples at the configured success-rate threshold.
    ``force`` bypasses only the latency hysteresis; it never bypasses evidence,
    availability, or post-change verification requirements.
    """
    current_reader = getattr(ctx, "get_current_preferred_target", None)
    current_raw = current_reader(strict=True) if callable(current_reader) else ""
    if current_raw:
        try:
            current = ctx.normalize_cdn_target(current_raw)
        except Exception as exc:
            raise _make_error(ctx, f"当前 CDN 目标无效: {exc}") from None
        current = current.strip().lower().rstrip(".")
    else:
        current = ""

    if candidates is not None:
        if not candidates:
            raise _make_error(ctx, "显式 CDN 候选列表为空")
        source = candidates
    else:
        builder = getattr(ctx, "ensure_candidate_list", None)
        if callable(builder):
            source = builder()
        else:
            source = getattr(ctx, "CDN_CANDIDATES", ())
    domains = _normalize_candidates(ctx, source, current)

    results: list[Mapping[str, Any]] = []
    benchmark = getattr(ctx, "benchmark_cdn", None)
    if not callable(benchmark):
        raise _make_error(ctx, "CDN 测速函数未配置")
    for domain in domains:
        row = benchmark(domain)
        if not isinstance(row, Mapping):
            continue
        # The context is the authority for the row; normalize its key to avoid
        # mismatches caused by case or a terminal DNS dot.
        item = dict(row)
        item["domain"] = domain
        results.append(item)

    display = getattr(ctx, "print_cdn_benchmark", None)
    if callable(display):
        display(results, current=current or None)

    rate = _finite_number(getattr(ctx, "CDN_MIN_SUCCESS_RATE", DEFAULT_MIN_SUCCESS_RATE))
    if rate is None or not 0.0 <= rate <= 1.0:
        rate = DEFAULT_MIN_SUCCESS_RATE
    require_business = bool(auto_apply_best)
    ranked = sorted(
        results,
        key=lambda row: (
            not _eligible(row, require_business, rate),
            -_https_metrics(row)[2],
            latency_score(row) or math.inf,
            _sort_number(row.get("avg_ttfb")),
            str(row.get("domain") or "").lower(),
        ),
    )
    usable = [row for row in ranked if _eligible(row, require_business, rate)]
    if not usable:
        raise _make_error(ctx, "没有满足业务 HTTPS、至少 2 个样本及可用率阈值的 CDN；配置保持不变")

    def row_domain(row: Mapping[str, Any]) -> str:
        value = row.get("domain")
        if not isinstance(value, str):
            return ""
        try:
            return ctx.normalize_cdn_target(value).strip().lower().rstrip(".")
        except Exception:
            return value.strip().lower().rstrip(".")

    current_row = next((row for row in results if row_domain(row) == current), None) if current else None
    best = usable[0]
    best_target = row_domain(best)
    if not auto_apply_best:
        return current or best_target

    if not current:
        # No known prior value means an automatic write cannot be rolled back.
        raise _make_error(ctx, "当前 CDN 目标为空，自动切换未执行（缺少可回滚目标）")
    if best_target == current:
        return current

    current_is_eligible = current_row is not None and _eligible(current_row, True, rate)
    if current_is_eligible and not force:
        current_score = latency_score(current_row)
        best_score = latency_score(best)
        minimum = _finite_number(
            getattr(ctx, "CDN_SWITCH_MIN_IMPROVEMENT_PERCENT", 15.0)
        )
        if minimum is None or minimum < 0:
            minimum = 15.0
        if current_score and best_score:
            improvement = (current_score - best_score) / current_score * 100.0
            if improvement < minimum:
                return current

    verify = getattr(ctx, "verify_https", None)
    if not callable(verify) or not getattr(ctx, "PROBE_HOSTNAME", ""):
        raise _make_error(ctx, "自动切换需要配置业务探测主机名及 HTTPS 验收函数")

    try:
        _set_target(ctx, best_target)
    except Exception as exc:
        rollback_ok, rollback_note = _rollback(ctx, current)
        raise _make_error(
            ctx, f"切换写入失败: {exc}; {rollback_note}"
        ) from None

    try:
        verified = _verify_result(verify(ctx.PROBE_HOSTNAME))
    except Exception as exc:
        verified = False
        verify_detail = str(exc)
    else:
        verify_detail = "HTTPS 验收未通过"
    if verified:
        return best_target

    _, rollback_note = _rollback(ctx, current)
    raise _make_error(
        ctx,
        f"新 CDN {best_target} 切换后 {verify_detail}; {rollback_note}",
    )
