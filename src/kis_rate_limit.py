"""
KIS API rate-limit 설정·재시도·인메모리 캐시 (전역).
screener / account / domestic_stock 에서 init_kis_rate_limits() 후 사용.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ENV_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "kis_paper": {
        "max_rps": 1.0,
        "max_concurrency": 1,
        "screener_workers": 1,
        "request_min_interval_sec": 0.6,
    },
    "vps": {
        "max_rps": 1.0,
        "max_concurrency": 1,
        "screener_workers": 2,
        "request_min_interval_sec": 0.5,
    },
    "prod": {
        "max_rps": 2.0,
        "max_concurrency": 1,
        "screener_workers": 2,
        "request_min_interval_sec": 0.5,
    },
}

_GLOBAL_DEFAULTS: Dict[str, Any] = {
    "max_rps": 2.0,
    "max_concurrency": 1,
    "screener_workers": 2,
    "request_min_interval_sec": 0.5,
    "retry_on_rate_limit": True,
    "rate_limit_max_retries": 3,
    "rate_limit_retry_delays_sec": [1.0, 2.0, 4.0],
    "jitter_sec": 0.2,
}

_LIMITS: Dict[str, Any] = {}
_TRADING_ENV: str = "prod"
_RATE_LIMITER: Optional["KisRateLimiter"] = None
_MAX_CONCURRENCY: int = 1

_MEM_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_STATS: Counter = Counter()
_RATE_LIMIT_HITS: Counter = Counter()
_RATE_LIMIT_BY_API: Counter = Counter()
_RATE_LIMIT_BY_TICKER: Counter = Counter()


class KisRateLimiter:
    """RPS + 최소 요청 간격 이중 제어."""

    def __init__(self, rps: float, min_interval_sec: float = 0.0):
        self.min_interval = max(
            float(min_interval_sec or 0.0),
            1.0 / max(0.1, float(rps)) if rps and rps > 0 else 0.0,
        )
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def _merge_dict(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(base)
    if override:
        for k, v in override.items():
            if v is not None:
                out[k] = v
    return out


def resolve_kis_limits(settings: Optional[Dict[str, Any]], trading_env: str) -> Dict[str, Any]:
    """config kis_limits + env별 기본값 병합."""
    raw = (settings or {}).get("kis_limits", {}) or {}
    if not isinstance(raw, dict):
        raw = {}

    env_key = str(trading_env or "prod").strip() or "prod"
    if env_key not in ("kis_paper", "vps", "prod"):
        env_key = "vps" if env_key in ("mock", "paper") else "prod"

    merged = _merge_dict(_GLOBAL_DEFAULTS, raw.get("default") if isinstance(raw.get("default"), dict) else None)
    merged = _merge_dict(merged, _ENV_DEFAULTS.get(env_key, {}))
    merged = _merge_dict(merged, raw.get(env_key) if isinstance(raw.get(env_key), dict) else None)

    # 레거시 flat 키 (kis_limits.max_rps 등)
    for legacy in ("max_rps", "max_concurrency", "screener_workers", "request_min_interval_sec"):
        if legacy in raw and legacy not in (raw.get(env_key) or {}):
            merged[legacy] = raw[legacy]

    screener_params = (settings or {}).get("screener_params", {}) or {}
    if isinstance(screener_params, dict) and screener_params.get("workers") is not None:
        if "screener_workers" not in raw.get(env_key, {}) and "screener_workers" not in raw:
            merged["screener_workers"] = int(screener_params["workers"])

    merged["trading_env"] = env_key
    return merged


def init_kis_rate_limits(settings: Optional[Dict[str, Any]], trading_env: str) -> Dict[str, Any]:
    """전역 rate limiter·설정 초기화."""
    global _LIMITS, _TRADING_ENV, _RATE_LIMITER, _MAX_CONCURRENCY
    limits = resolve_kis_limits(settings, trading_env)
    _LIMITS = limits
    _TRADING_ENV = limits.get("trading_env", trading_env)
    rps = float(limits.get("max_rps", 1) or 0)
    min_iv = float(limits.get("request_min_interval_sec", 0.5) or 0)
    _RATE_LIMITER = KisRateLimiter(rps, min_iv) if rps > 0 else None
    _MAX_CONCURRENCY = max(1, int(limits.get("max_concurrency", 1) or 1))
    return limits


def get_kis_limits() -> Dict[str, Any]:
    return dict(_LIMITS)


def get_max_concurrency() -> int:
    return _MAX_CONCURRENCY


def rate_limit_wait() -> None:
    if _RATE_LIMITER:
        _RATE_LIMITER.wait()


def is_rate_limit_message(msg_cd: str = "", msg1: str = "", text: str = "") -> bool:
    blob = f"{msg_cd} {msg1} {text}"
    return ("EGW00201" in blob) or ("초당 거래건수" in blob)


def parse_rate_limit_from_response(res: requests.Response) -> Tuple[bool, str]:
    if res is None:
        return False, ""
    text = (getattr(res, "text", None) or "")[:2000]
    try:
        data = res.json()
        if isinstance(data, dict):
            msg_cd = str(data.get("msg_cd", "") or "")
            msg1 = str(data.get("msg1", "") or "")
            if is_rate_limit_message(msg_cd, msg1, text):
                return True, msg_cd or "EGW00201"
    except Exception:
        pass
    if is_rate_limit_message(text=text):
        return True, "EGW00201"
    if res.status_code in (429, 500) and "EGW00201" in text:
        return True, "EGW00201"
    return False, ""


def cache_get(api: str, ticker: str, date_key: str = "") -> Optional[Any]:
    key = f"{api}:{str(ticker).zfill(6)}:{date_key}"
    with _CACHE_LOCK:
        if key in _MEM_CACHE:
            _CACHE_STATS[f"{api}_hit"] += 1
            return _MEM_CACHE[key]
        _CACHE_STATS[f"{api}_miss"] += 1
    return None


def cache_put(api: str, ticker: str, value: Any, date_key: str = "") -> None:
    key = f"{api}:{str(ticker).zfill(6)}:{date_key}"
    with _CACHE_LOCK:
        _MEM_CACHE[key] = value


def log_cache_summary() -> None:
    with _CACHE_LOCK:
        stats = dict(_CACHE_STATS)
    if not stats:
        return
    parts = " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
    logger.info("[KIS_CACHE_SUMMARY] %s", parts)


def reset_cache_stats() -> None:
    with _CACHE_LOCK:
        _CACHE_STATS.clear()
        _MEM_CACHE.clear()
        _RATE_LIMIT_HITS.clear()
        _RATE_LIMIT_BY_API.clear()
        _RATE_LIMIT_BY_TICKER.clear()


def record_rate_limit_hit(api: str = "", ticker: str = "", msg_cd: str = "EGW00201") -> None:
    with _CACHE_LOCK:
        _RATE_LIMIT_HITS[msg_cd or "EGW00201"] += 1
        if api:
            _RATE_LIMIT_BY_API[api] += 1
        if ticker:
            _RATE_LIMIT_BY_TICKER[str(ticker).zfill(6)] += 1


def get_rate_limit_summary() -> Dict[str, Any]:
    with _CACHE_LOCK:
        total = sum(_RATE_LIMIT_HITS.values())
        by_api = dict(_RATE_LIMIT_BY_API)
        by_ticker = dict(_RATE_LIMIT_BY_TICKER)
    return {
        "egw00201_count": total,
        "by_api": by_api,
        "by_ticker": by_ticker,
    }


def call_with_rate_limit_retry(
    fn: Callable[[], T],
    *,
    api: str,
    ticker: str = "",
    max_retries: Optional[int] = None,
    is_rate_limited: Optional[Callable[[T], Tuple[bool, str]]] = None,
) -> T:
    """
    fn 실행 전 rate_limit_wait().
    is_rate_limited(result) → (True, msg_cd) 이면 백오프 재시도.
    """
    limits = _LIMITS or _GLOBAL_DEFAULTS
    retries = int(max_retries if max_retries is not None else limits.get("rate_limit_max_retries", 3))
    delays = list(limits.get("rate_limit_retry_delays_sec") or [1.0, 2.0, 4.0])
    jitter = float(limits.get("jitter_sec", 0.2) or 0.0)
    retry_on_rl = bool(limits.get("retry_on_rate_limit", True))
    last: Any = None

    for attempt in range(max(1, retries + 1)):
        rate_limit_wait()
        last = fn()
        if not retry_on_rl or not is_rate_limited:
            return last
        limited, msg_cd = is_rate_limited(last)
        if not limited:
            return last
        record_rate_limit_hit(api=api, ticker=ticker, msg_cd=msg_cd or "EGW00201")
        if attempt >= retries:
            logger.warning(
                "[KIS_RATE_LIMIT] ticker=%s api=%s retry=%d/%d exhausted msg_cd=%s",
                ticker or "-", api, attempt, retries, msg_cd,
            )
            return last
        base = delays[min(attempt, len(delays) - 1)] if delays else 1.0
        sleep_s = base + random.uniform(0.1, max(0.1, jitter))
        logger.info(
            "[KIS_RATE_LIMIT] ticker=%s api=%s retry=%d/%d sleep=%.1fs msg_cd=%s",
            ticker or "-", api, attempt + 1, retries, sleep_s, msg_cd,
        )
        time.sleep(sleep_s)
    return last


def retry_http_get(
    do_get: Callable[[], requests.Response],
    *,
    api: str,
    ticker: str = "",
) -> requests.Response:
    def _is_limited(res: requests.Response) -> Tuple[bool, str]:
        return parse_rate_limit_from_response(res)

    return call_with_rate_limit_retry(do_get, api=api, ticker=ticker, is_rate_limited=_is_limited)
