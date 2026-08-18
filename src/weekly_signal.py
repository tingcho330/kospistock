# src/weekly_signal.py
"""주간 GPT signal 이월(deferred weekly trader) 상태 관리."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from utils import (
    KST,
    OUTPUT_DIR,
    count_krx_trading_days_between,
    find_latest_file,
    is_krx_trading_day,
    next_krx_trading_day,
)

logger = logging.getLogger("weekly_signal")

WEEKLY_TRADER_STATE_PATH = OUTPUT_DIR / "weekly_trader_state.json"
_GPT_FILE_DATE_RE = re.compile(r"gpt_trades_(\d{8})_")


def is_weekly_rebalance_mode(config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = config if isinstance(config, dict) else _load_settings_config()
    sp = cfg.get("screener_params", {}) if isinstance(cfg.get("screener_params"), dict) else {}
    pc = sp.get("portfolio", {}) if isinstance(sp.get("portfolio"), dict) else {}
    return str(pc.get("rebalance_frequency", "")).lower() == "weekly"


def get_weekly_trader_params(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else _load_settings_config()
    tp = cfg.get("trading_params", {}) if isinstance(cfg.get("trading_params"), dict) else {}
    return {
        "max_deferred_entry_gap_pct": float(tp.get("max_deferred_entry_gap_pct", 0.03)),
        "weekly_signal_max_trading_days": int(tp.get("weekly_signal_max_trading_days", 2)),
    }


def _load_settings_config() -> Dict[str, Any]:
    try:
        from settings import settings
        cfg = getattr(settings, "_config", {}) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _parse_yyyymmdd(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def parse_gpt_trades_signal_date(gpt_file: Optional[Path]) -> Optional[str]:
    """gpt_trades 파일에서 signal_date(YYYYMMDD)를 추출."""
    if not gpt_file:
        return None
    path = Path(gpt_file)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            d = data.get("date")
            parsed = _parse_yyyymmdd(d)
            if parsed:
                return parsed.strftime("%Y%m%d")
    except Exception:
        pass
    m = _GPT_FILE_DATE_RE.search(path.name)
    if m:
        return m.group(1)
    return None


def find_latest_gpt_trades_file(market: Optional[str] = None) -> Optional[Path]:
    mkt = (market or os.getenv("MARKET", "") or "").upper().strip() or None
    return find_latest_file("gpt_trades_*.json", market=mkt)


def load_weekly_execution_state(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    state_path = path or WEEKLY_TRADER_STATE_PATH
    try:
        if not state_path.exists():
            return None
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("주간 trader 상태 로드 실패: %s", e)
        return None


def save_weekly_execution_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    state_path = path or WEEKLY_TRADER_STATE_PATH
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(state_path)
    except Exception as e:
        logger.error("주간 trader 상태 저장 실패: %s", e)


def pending_weekly_execution(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """실행되지 않은 weekly deferred state. 없으면 None."""
    state = load_weekly_execution_state(path)
    if not state:
        return None
    if state.get("executed") is True:
        return None
    if str(state.get("status") or "").lower() in ("executed", "expired"):
        return None
    return state


def defer_weekly_execution(
    *,
    signal_date: str,
    gpt_file: Optional[Path],
    market: str,
    reason: str = "non_trading_day",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    existing = load_weekly_execution_state(path)
    if (
        existing
        and str(existing.get("signal_date") or "") == str(signal_date)
        and existing.get("executed") is True
    ):
        return existing

    created_at = datetime.now(KST).isoformat()
    if existing and str(existing.get("signal_date") or "") == str(signal_date):
        created_at = str(existing.get("created_at") or created_at)

    state = {
        "signal_date": str(signal_date),
        "gpt_file": str(gpt_file) if gpt_file else "",
        "market": (market or "KOSPI").upper(),
        "deferred_reason": reason,
        "created_at": created_at,
        "executed": False,
        "status": "pending",
        "execution_date": None,
        "executed_at": None,
    }
    save_weekly_execution_state(state, path)
    logger.info(
        "[WEEKLY_SIGNAL_DEFERRED] signal_date=%s gpt_file=%s market=%s reason=%s",
        state["signal_date"],
        state["gpt_file"] or "-",
        state["market"],
        reason,
    )
    return state


def complete_weekly_execution(
    *,
    status: str = "executed",
    execution_date: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    state = load_weekly_execution_state(path)
    if not state:
        return None
    current = str(state.get("status") or "").lower()
    if current in ("executed", "expired") and status == "executed":
        return state
    if current == "expired" and status != "expired":
        return state

    exec_date = execution_date or datetime.now(KST).strftime("%Y%m%d")
    state["executed"] = status == "executed"
    state["status"] = status
    state["execution_date"] = exec_date
    state["executed_at"] = datetime.now(KST).isoformat()
    save_weekly_execution_state(state, path)
    if status == "executed":
        logger.info(
            "[WEEKLY_SIGNAL_EXECUTED] signal_date=%s execution_date=%s gpt_file=%s",
            state.get("signal_date"),
            exec_date,
            state.get("gpt_file") or "-",
        )
    elif status == "expired":
        logger.info(
            "[WEEKLY_SIGNAL_EXPIRED] signal_date=%s execution_date=%s",
            state.get("signal_date"),
            exec_date,
        )
    return state


def is_weekly_signal_expired(
    signal_date: Any,
    execution_date: Any,
    max_trading_days: int = 2,
) -> bool:
    """signal_date 이후 거래일이 max_trading_days를 초과하면 True (2 거래일 초과)."""
    start = _parse_yyyymmdd(signal_date)
    end = _parse_yyyymmdd(execution_date)
    if start is None or end is None:
        return False
    elapsed = count_krx_trading_days_between(start, end)
    return elapsed > int(max_trading_days)


def entry_gap_pct(signal_price: float, current_price: float) -> Optional[float]:
    try:
        sp = float(signal_price)
        cp = float(current_price)
    except (TypeError, ValueError):
        return None
    if sp <= 0 or cp <= 0:
        return None
    return (cp - sp) / sp


def should_skip_gap_up(
    signal_price: float,
    current_price: float,
    max_gap_pct: float = 0.03,
) -> Tuple[bool, Optional[float]]:
    gap = entry_gap_pct(signal_price, current_price)
    if gap is None:
        return False, None
    return gap > float(max_gap_pct), gap


def handle_trader_trading_day_gate(
    *,
    weekly_mode: bool,
    market: str = "KOSPI",
    skip_defer: bool = False,
    today: Optional[date] = None,
) -> bool:
    """
    trader 진입 전 거래일 확인.
    휴장일이면 로그 후 True(즉시 종료)를 반환. 개장일이면 False.
    """
    day = today or datetime.now(KST).date()
    date_str = day.strftime("%Y%m%d")
    trading = is_krx_trading_day(day)
    logger.info(
        "[TRADER_TRADING_DAY_CHECK] date=%s is_trading_day=%s weekly_mode=%s",
        date_str,
        str(trading).lower(),
        str(bool(weekly_mode)).lower(),
    )
    if trading:
        return False

    nxt = next_krx_trading_day(day)
    logger.info(
        "[TRADER_NON_TRADING_DAY] date=%s action=defer next_trading_day=true next_open=%s",
        date_str,
        nxt.strftime("%Y%m%d"),
    )
    if weekly_mode and not skip_defer:
        gpt_file = find_latest_gpt_trades_file(market)
        signal_date = parse_gpt_trades_signal_date(gpt_file) or date_str
        if gpt_file:
            defer_weekly_execution(
                signal_date=signal_date,
                gpt_file=gpt_file,
                market=market,
                reason="non_trading_day",
            )
        else:
            logger.info(
                "[WEEKLY_SIGNAL_DEFERRED] skipped reason=no_gpt_file date=%s",
                date_str,
            )
    return True


def _self_check() -> None:
    """휴장 이월/만료/갭 스킵 단위 검증. import 부작용 없이 호출 가능."""
    from datetime import date as _date
    from utils import is_krx_trading_day, next_krx_trading_day, count_krx_trading_days_between

    assert not is_krx_trading_day(_date(2026, 8, 15)), "광복절"
    assert not is_krx_trading_day(_date(2026, 8, 16)), "일요일"
    assert not is_krx_trading_day(_date(2026, 8, 17)), "광복절 대체공휴일"
    assert is_krx_trading_day(_date(2026, 8, 18)), "화요일 개장"
    assert next_krx_trading_day(_date(2026, 8, 14)) == _date(2026, 8, 18)
    assert count_krx_trading_days_between(_date(2026, 8, 17), _date(2026, 8, 18)) == 1
    assert count_krx_trading_days_between(_date(2026, 8, 17), _date(2026, 8, 19)) == 2
    assert count_krx_trading_days_between(_date(2026, 8, 17), _date(2026, 8, 20)) == 3

    assert not is_weekly_signal_expired("20260817", "20260818", 2)
    assert not is_weekly_signal_expired("20260817", "20260819", 2)
    assert is_weekly_signal_expired("20260817", "20260820", 2)

    skip, gap = should_skip_gap_up(10000, 10301, 0.03)
    assert skip and gap is not None and gap > 0.03
    skip2, gap2 = should_skip_gap_up(10000, 10200, 0.03)
    assert not skip2 and gap2 is not None and gap2 == 0.02
    skip3, _ = should_skip_gap_up(10000, 9700, 0.03)
    assert not skip3

    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "weekly_trader_state.json"
    st = defer_weekly_execution(
        signal_date="20260817",
        gpt_file=Path("/tmp/gpt_trades_20260817_KOSPI.json"),
        market="KOSPI",
        reason="non_trading_day",
        path=tmp,
    )
    assert st["executed"] is False
    assert pending_weekly_execution(tmp) is not None
    complete_weekly_execution(status="executed", execution_date="20260818", path=tmp)
    assert pending_weekly_execution(tmp) is None
    again = complete_weekly_execution(status="executed", execution_date="20260818", path=tmp)
    assert again and again.get("status") == "executed"

    assert handle_trader_trading_day_gate(
        weekly_mode=True, market="KOSPI", skip_defer=True, today=_date(2026, 8, 17)
    ) is True
    assert handle_trader_trading_day_gate(
        weekly_mode=True, market="KOSPI", skip_defer=True, today=_date(2026, 8, 18)
    ) is False
    print("weekly_signal self-check PASS")


if __name__ == "__main__":
    _self_check()
