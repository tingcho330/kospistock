#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""조건부 Rebuy 정책.

기존 포지션에 대한 추가매수가 아니라, 전량 청산 이후 새로운 포지션을
다시 여는 재진입만 다룬다. max_legs_per_ticker=1 은 유지한다.

rebuy 설정 블록이 없으면 레거시 allow_rebuy(보유 중 추가매수) 동작을 유지한다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("rebuy_policy")

try:
    from utils import KST, count_krx_trading_days_between as _count_krx_trading_days_between
except Exception:  # pandas 등 미설치 환경의 단위테스트 허용
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    _count_krx_trading_days_between = None  # type: ignore

_FALLBACK_HOLIDAYS = {
    date(2026, 8, 15),
    date(2026, 8, 17),  # 광복절 대체공휴일
}

ENTRY_NORMAL = "NORMAL_BUY"
ENTRY_REBUY = "REBUY"
ENTRY_EXISTING = "EXISTING_POSITION"

EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_ROTATION = "rotation"
EXIT_EMERGENCY_DROP = "emergency_drop"
EXIT_UNKNOWN = "unknown"

REASON_REBUY_AFTER = {
    EXIT_STOP_LOSS: "REBUY_AFTER_STOPLOSS",
    EXIT_TAKE_PROFIT: "REBUY_AFTER_TAKEPROFIT",
    EXIT_ROTATION: "REBUY_AFTER_ROTATION",
    EXIT_EMERGENCY_DROP: "REBUY_AFTER_EMERGENCY_DROP",
}

_FILLED_STATUSES = frozenset({
    "executed", "completed", "partial", "paper_executed",
    "market_executed", "limit_executed", "split_executed",
})
_PENDING_STATUSES = frozenset({"pending", "submitted", "partial"})
_RECONCILE_REASON_RE = re.compile(
    r"RECONCILE|HOLDING_FALLBACK|REPAIR_|STALE_SELL|ORPHAN",
    re.I,
)


def count_trading_days_between(start: date, end: date) -> int:
    if _count_krx_trading_days_between is not None:
        return int(_count_krx_trading_days_between(start, end))
    if end <= start:
        return 0
    n = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5 and cur not in _FALLBACK_HOLIDAYS:
            n += 1
        cur += timedelta(days=1)
    return n


def _norm_ticker(t: Any) -> str:
    return str(t or "").strip().zfill(6)


def _safe_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(s[:19], fmt).date()
            except ValueError:
                continue
    return None


def _parse_ctx(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _row_qty(row: Dict[str, Any]) -> int:
    return _safe_int(row.get("executed_qty") or row.get("quantity") or row.get("requested_qty"))


def _row_status(row: Dict[str, Any]) -> str:
    return str(row.get("order_status") or "").strip().lower()


def _row_action(row: Dict[str, Any]) -> str:
    return str(row.get("action") or row.get("side") or "").strip().upper()


@dataclass
class RebuyConfig:
    allow_rebuy: bool = False
    has_rebuy_section: bool = False
    enabled: bool = False
    require_new_signal: bool = True
    require_gpt_buy: bool = True
    cooldown_trading_days: int = 5
    max_rebuy_count_per_ticker: int = 1
    min_recovery_pct: float = 0.03
    after_stop_loss: bool = True
    after_take_profit: bool = True
    after_rotation: bool = True
    after_emergency_drop: bool = False
    emergency_drop_cooldown_trading_days: int = 10

    @property
    def policy_enabled(self) -> bool:
        """조건부 Rebuy 정책 사용 여부 (레거시 추가매수와 구분)."""
        return bool(self.allow_rebuy and self.has_rebuy_section and self.enabled)

    @property
    def legacy_scale_in(self) -> bool:
        """rebuy 블록 없이 allow_rebuy=true 인 기존 보유중 추가매수."""
        return bool(self.allow_rebuy and not self.has_rebuy_section)


def load_rebuy_config(trading_params: Optional[Dict[str, Any]] = None) -> RebuyConfig:
    tp = trading_params if isinstance(trading_params, dict) else {}
    allow = bool(tp.get("allow_rebuy", False))
    raw = tp.get("rebuy")
    cfg = RebuyConfig(allow_rebuy=allow, has_rebuy_section=isinstance(raw, dict))
    if not isinstance(raw, dict):
        return cfg
    cfg.enabled = bool(raw.get("enabled", True))
    cfg.require_new_signal = bool(raw.get("require_new_signal", True))
    cfg.require_gpt_buy = bool(raw.get("require_gpt_buy", True))
    cfg.cooldown_trading_days = int(raw.get("cooldown_trading_days", 5) or 5)
    cfg.max_rebuy_count_per_ticker = int(raw.get("max_rebuy_count_per_ticker", 1) or 1)
    cfg.min_recovery_pct = float(raw.get("min_recovery_pct", 0.03) or 0.0)
    cfg.after_stop_loss = bool(raw.get("after_stop_loss", True))
    cfg.after_take_profit = bool(raw.get("after_take_profit", True))
    cfg.after_rotation = bool(raw.get("after_rotation", True))
    cfg.after_emergency_drop = bool(raw.get("after_emergency_drop", False))
    cfg.emergency_drop_cooldown_trading_days = int(
        raw.get("emergency_drop_cooldown_trading_days", 10) or 10
    )
    return cfg


def classify_exit_reason(
    sell_reason: Any = "",
    reason_code: Any = "",
    structured_context: Any = None,
) -> str:
    """매도 유형 분류. sell_reason → strategy type → reason_code → unknown.

    RECONCILED_BY_HOLDING_FALLBACK_SELL 같은 정합 코드만으로는 판단하지 않는다.
    """
    ctx = _parse_ctx(structured_context)
    ctx_type = str(ctx.get("type") or "").strip()
    reason_text = str(sell_reason or "").strip()
    if not reason_text:
        reason_text = str(ctx.get("reason") or "").strip()
    code = str(reason_code or ctx.get("reason_code") or "").strip()

    blob_reason = f"{reason_text} {ctx_type}".strip()
    classified = _classify_exit_blob(blob_reason)
    if classified != EXIT_UNKNOWN:
        return classified

    code_for_type = code
    if _RECONCILE_REASON_RE.search(code_for_type):
        code_for_type = ""
    classified = _classify_exit_blob(code_for_type)
    if classified != EXIT_UNKNOWN:
        return classified
    return EXIT_UNKNOWN


def _classify_exit_blob(blob: str) -> str:
    s = str(blob or "")
    sl = s.lower()
    if not s.strip():
        return EXIT_UNKNOWN
    if "emergencydrop" in sl or "긴급 낙폭" in s or "emergency_drop" in sl:
        return EXIT_EMERGENCY_DROP
    if (
        "고정 손절" in s
        or "손절가 도달" in s
        or "전략=stoploss" in sl
        or "fixedstoploss" in sl
        or "stop_loss" in sl
        or "stoploss" in sl
        or "stop_loss_hit" in sl
    ):
        return EXIT_STOP_LOSS
    if (
        "고정 이익실현" in s
        or "목표가 도달" in s
        or "전략=takeprofit" in sl
        or "takeprofit" in sl
        or "take_profit" in sl
        or "take_profit_hit" in sl
        or "고정 익절" in s
    ):
        return EXIT_TAKE_PROFIT
    if (
        "rotation" in sl
        or "rebalance_swap" in sl
        or "rotation_swap" in sl
        or "전략=rotation" in sl
        or "회전" in s
    ):
        return EXIT_ROTATION
    return EXIT_UNKNOWN


@dataclass
class TickerTradeState:
    ticker: str
    open_qty: int = 0
    has_pending_sell: bool = False
    has_pending_buy: bool = False
    has_any_buy: bool = False
    completed_cycles: int = 0
    last_sell_date: Optional[date] = None
    last_sell_price: float = 0.0
    last_sell_reason: str = ""
    last_sell_reason_code: str = ""
    last_sell_context: Any = None
    last_buy_date: Optional[date] = None
    exit_type: str = EXIT_UNKNOWN

    @property
    def rebuy_count(self) -> int:
        return max(0, int(self.completed_cycles) - 1)

    @property
    def is_open_or_pending(self) -> bool:
        return self.open_qty > 0 or self.has_pending_sell or self.has_pending_buy


def analyze_ticker_trades(
    trades: Iterable[Dict[str, Any]],
    ticker: str,
    *,
    include_paper_executed: bool = True,
) -> TickerTradeState:
    """trade_records 에서 ticker 의 완결 사이클·미체결·최근 SELL 을 계산."""
    state = TickerTradeState(ticker=_norm_ticker(ticker))
    rows = [r for r in (trades or []) if _norm_ticker(r.get("ticker")) == state.ticker]
    rows.sort(key=lambda r: (
        str(r.get("timestamp") or ""),
        _safe_int(r.get("id")),
    ))

    running = 0
    cycle_open = False
    for row in rows:
        action = _row_action(row)
        status = _row_status(row)
        qty = _row_qty(row)
        ts = _parse_date(row.get("timestamp"))
        if action == "BUY":
            if status in _PENDING_STATUSES and status != "partial":
                if qty > 0:
                    state.has_pending_buy = True
                continue
            if status == "partial" and qty <= 0:
                state.has_pending_buy = True
                continue
            if status not in _FILLED_STATUSES:
                continue
            if status == "paper_executed" and not include_paper_executed:
                continue
            if qty <= 0:
                continue
            state.has_any_buy = True
            running += qty
            cycle_open = True
            state.last_buy_date = ts or state.last_buy_date
        elif action == "SELL":
            if status in ("pending", "submitted"):
                state.has_pending_sell = True
                continue
            if status == "partial":
                if qty <= 0:
                    state.has_pending_sell = True
                    continue
                # 부분 체결: 수량만큼 차감하되 잔여 주문이 있으면 pending 취급
                req = _safe_int(row.get("requested_qty") or row.get("quantity") or 0)
                running = max(0, running - qty)
                state.last_sell_date = ts or state.last_sell_date
                state.last_sell_price = _safe_float(row.get("price")) or state.last_sell_price
                state.last_sell_reason = str(row.get("sell_reason") or "") or state.last_sell_reason
                state.last_sell_reason_code = str(row.get("reason_code") or "") or state.last_sell_reason_code
                state.last_sell_context = row.get("structured_context") or state.last_sell_context
                if req > qty:
                    state.has_pending_sell = True
                if running == 0 and cycle_open and not state.has_pending_sell:
                    state.completed_cycles += 1
                    cycle_open = False
                continue
            if status not in _FILLED_STATUSES:
                continue
            if status == "paper_executed" and not include_paper_executed:
                continue
            if qty <= 0:
                continue
            running = max(0, running - qty)
            state.last_sell_date = ts or state.last_sell_date
            px = _safe_float(row.get("price"))
            if px > 0:
                state.last_sell_price = px
            sr = str(row.get("sell_reason") or "").strip()
            if sr:
                state.last_sell_reason = sr
            rc = str(row.get("reason_code") or "").strip()
            if rc:
                state.last_sell_reason_code = rc
            if row.get("structured_context"):
                state.last_sell_context = row.get("structured_context")
            if running == 0 and cycle_open:
                state.completed_cycles += 1
                cycle_open = False
        # failed/cancelled 무시
    state.open_qty = max(0, running)
    state.exit_type = classify_exit_reason(
        state.last_sell_reason,
        state.last_sell_reason_code,
        state.last_sell_context,
    )
    return state


def classify_buy_entry(
    *,
    account_qty: int,
    state: TickerTradeState,
) -> str:
    if int(account_qty or 0) > 0:
        return ENTRY_EXISTING
    if state.has_pending_sell or state.has_pending_buy:
        return ENTRY_REBUY
    if state.open_qty > 0:
        return ENTRY_EXISTING
    if state.has_any_buy or state.completed_cycles >= 1 or state.last_sell_date:
        return ENTRY_REBUY
    return ENTRY_NORMAL


@dataclass
class RebuyEvalResult:
    ticker: str
    entry_type: str
    eligible: bool
    reason: str
    skip_tag: Optional[str] = None
    signal_date: Optional[str] = None
    last_sell_date: Optional[str] = None
    last_sell_reason: str = ""
    last_sell_price: float = 0.0
    exit_type: str = EXIT_UNKNOWN
    is_new_signal: Optional[bool] = None
    elapsed_trading_days: Optional[int] = None
    required_trading_days: Optional[int] = None
    recovery_pct: Optional[float] = None
    required_recovery_pct: Optional[float] = None
    current_price: float = 0.0
    completed_cycles: int = 0
    rebuy_count: int = 0
    max_rebuy_count: int = 1
    reason_code: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "eligible": self.eligible,
            "reason": self.reason,
            "entry_type": self.entry_type,
            "signal_date": self.signal_date,
            "last_sell_date": self.last_sell_date,
            "last_sell_reason": self.last_sell_reason,
            "exit_type": self.exit_type,
            "cooldown_days": self.elapsed_trading_days,
            "recovery_pct": self.recovery_pct,
            "rebuy_count": self.rebuy_count,
        }


def evaluate_rebuy(
    *,
    ticker: str,
    cfg: RebuyConfig,
    account_qty: int,
    trades: Iterable[Dict[str, Any]],
    signal_date: Optional[str] = None,
    gpt_decision: str = "BUY",
    current_price: float = 0.0,
    as_of: Optional[date] = None,
    is_bond_etf: bool = False,
    include_paper_executed: bool = True,
) -> RebuyEvalResult:
    """Rebuy 자격 판정. NORMAL_BUY 는 eligible=True (정책 비대상)."""
    tk = _norm_ticker(ticker)
    as_of = as_of or datetime.now(KST).date()
    state = analyze_ticker_trades(
        trades, tk, include_paper_executed=include_paper_executed,
    )
    entry = classify_buy_entry(account_qty=account_qty, state=state)
    sig_s = None
    sig_d = _parse_date(signal_date)
    if sig_d:
        sig_s = sig_d.strftime("%Y%m%d")
    sell_s = state.last_sell_date.strftime("%Y%m%d") if state.last_sell_date else None

    base = RebuyEvalResult(
        ticker=tk,
        entry_type=entry if entry != ENTRY_REBUY else ENTRY_REBUY,
        eligible=False,
        reason="",
        signal_date=sig_s,
        last_sell_date=sell_s,
        last_sell_reason=state.last_sell_reason,
        last_sell_price=state.last_sell_price,
        exit_type=state.exit_type,
        current_price=float(current_price or 0.0),
        completed_cycles=state.completed_cycles,
        rebuy_count=state.rebuy_count,
        max_rebuy_count=cfg.max_rebuy_count_per_ticker,
    )

    if is_bond_etf:
        base.entry_type = ENTRY_NORMAL
        base.eligible = False
        base.reason = "bond_etf"
        base.skip_tag = None
        return base

    if entry == ENTRY_EXISTING:
        base.entry_type = ENTRY_EXISTING
        base.reason = "current_holding"
        base.skip_tag = "REBUY_SKIP_CURRENT_HOLDING"
        return base

    if entry == ENTRY_NORMAL:
        base.entry_type = ENTRY_NORMAL
        base.eligible = True
        base.reason = "never_bought"
        return base

    # REBUY_CANDIDATE
    base.entry_type = ENTRY_REBUY
    if not cfg.policy_enabled:
        # 레거시: 청산 후 종목은 일반 신규매수 경로로 통과
        base.entry_type = ENTRY_NORMAL
        base.eligible = True
        base.reason = "legacy_new_entry"
        return base

    if state.is_open_or_pending:
        base.reason = "open_or_pending"
        base.skip_tag = "REBUY_SKIP_OPEN_OR_PENDING_POSITION"
        return base

    if int(account_qty or 0) > 0:
        base.reason = "current_holding"
        base.skip_tag = "REBUY_SKIP_CURRENT_HOLDING"
        return base

    decision = str(gpt_decision or "").strip().upper()
    if decision in ("매수",):
        decision = "BUY"
    if cfg.require_gpt_buy and decision != "BUY":
        base.reason = "not_gpt_buy"
        base.skip_tag = "REBUY_SKIP_OLD_SIGNAL"
        base.is_new_signal = False
        return base

    is_new = False
    if sig_d and state.last_sell_date:
        is_new = sig_d > state.last_sell_date
    elif sig_d and not state.last_sell_date:
        is_new = True
    base.is_new_signal = is_new
    if cfg.require_new_signal and not is_new:
        base.reason = "old_signal"
        base.skip_tag = "REBUY_SKIP_OLD_SIGNAL"
        return base

    exit_type = state.exit_type
    base.exit_type = exit_type
    if exit_type == EXIT_UNKNOWN:
        base.reason = "unknown_exit"
        base.skip_tag = "REBUY_SKIP_UNKNOWN_EXIT_REASON"
        return base

    if exit_type == EXIT_EMERGENCY_DROP and not cfg.after_emergency_drop:
        base.reason = "emergency_drop"
        base.skip_tag = "REBUY_SKIP_PREVIOUS_EMERGENCY_DROP"
        return base
    if exit_type == EXIT_STOP_LOSS and not cfg.after_stop_loss:
        base.reason = "after_stop_loss_disabled"
        base.skip_tag = "REBUY_SKIP_UNKNOWN_EXIT_REASON"
        return base
    if exit_type == EXIT_TAKE_PROFIT and not cfg.after_take_profit:
        base.reason = "after_take_profit_disabled"
        base.skip_tag = "REBUY_SKIP_UNKNOWN_EXIT_REASON"
        return base
    if exit_type == EXIT_ROTATION and not cfg.after_rotation:
        base.reason = "after_rotation_disabled"
        base.skip_tag = "REBUY_SKIP_UNKNOWN_EXIT_REASON"
        return base

    required_cd = 0
    if exit_type == EXIT_EMERGENCY_DROP:
        required_cd = int(cfg.emergency_drop_cooldown_trading_days)
    elif exit_type in (EXIT_STOP_LOSS, EXIT_ROTATION):
        required_cd = int(cfg.cooldown_trading_days)
    # take_profit: 쿨다운 강제하지 않음
    base.required_trading_days = required_cd
    elapsed = 0
    if required_cd > 0:
        if not state.last_sell_date:
            base.reason = "unknown_exit"
            base.skip_tag = "REBUY_SKIP_UNKNOWN_EXIT_REASON"
            return base
        elapsed = count_trading_days_between(state.last_sell_date, as_of)
        base.elapsed_trading_days = elapsed
        if elapsed < required_cd:
            base.reason = "cooldown"
            base.skip_tag = "REBUY_SKIP_COOLDOWN"
            return base
    elif state.last_sell_date:
        base.elapsed_trading_days = count_trading_days_between(state.last_sell_date, as_of)

    if state.rebuy_count >= int(cfg.max_rebuy_count_per_ticker):
        base.reason = "max_count"
        base.skip_tag = "REBUY_SKIP_MAX_COUNT"
        return base

    recovery = None
    if exit_type == EXIT_STOP_LOSS:
        last_px = float(state.last_sell_price or 0.0)
        cur = float(current_price or 0.0)
        req = float(cfg.min_recovery_pct or 0.0)
        base.required_recovery_pct = req
        if last_px <= 0 or cur <= 0:
            base.reason = "insufficient_recovery"
            base.skip_tag = "REBUY_SKIP_INSUFFICIENT_RECOVERY"
            base.recovery_pct = None
            return base
        recovery = (cur / last_px) - 1.0
        base.recovery_pct = recovery
        if recovery < req:
            base.reason = "insufficient_recovery"
            base.skip_tag = "REBUY_SKIP_INSUFFICIENT_RECOVERY"
            return base
    else:
        last_px = float(state.last_sell_price or 0.0)
        cur = float(current_price or 0.0)
        if last_px > 0 and cur > 0:
            base.recovery_pct = (cur / last_px) - 1.0

    base.eligible = True
    base.reason = "approved"
    base.reason_code = REASON_REBUY_AFTER.get(exit_type, "REBUY")
    return base


def log_buy_entry_classify(ticker: str, entry_type: str, logger_: Optional[logging.Logger] = None) -> None:
    log = logger_ or logger
    mapped = entry_type
    if mapped == ENTRY_REBUY:
        mapped = "REBUY"
    log.info("[BUY_ENTRY_CLASSIFY] ticker=%s entry_type=%s", _norm_ticker(ticker), mapped)


def log_rebuy_evaluation(result: RebuyEvalResult, logger_: Optional[logging.Logger] = None) -> None:
    log = logger_ or logger
    tk = result.ticker
    log.info(
        "[BUY_ENTRY_CLASSIFY] ticker=%s entry_type=%s",
        tk,
        result.entry_type if result.entry_type != ENTRY_REBUY else "REBUY",
    )
    if result.entry_type == ENTRY_NORMAL and result.reason in ("never_bought", "legacy_new_entry"):
        return
    if result.entry_type == ENTRY_EXISTING:
        log.info("[REBUY_SKIP_CURRENT_HOLDING] ticker=%s", tk)
        log.info("[REBUY_EVALUATION] ticker=%s eligible=false reason=current_holding", tk)
        return

    log.info(
        "[REBUY_COUNT] ticker=%s completed_cycles=%s rebuy_count=%s max_rebuy_count=%s",
        tk, result.completed_cycles, result.rebuy_count, result.max_rebuy_count,
    )
    if result.is_new_signal is not None or result.signal_date or result.last_sell_date:
        log.info(
            "[REBUY_NEW_SIGNAL_CHECK] ticker=%s signal_date=%s last_sell_date=%s is_new_signal=%s",
            tk,
            result.signal_date or "-",
            result.last_sell_date or "-",
            "true" if result.is_new_signal else "false",
        )

    if result.skip_tag == "REBUY_SKIP_CURRENT_HOLDING":
        log.info("[REBUY_SKIP_CURRENT_HOLDING] ticker=%s", tk)
    elif result.skip_tag == "REBUY_SKIP_OPEN_OR_PENDING_POSITION":
        log.info("[REBUY_SKIP_OPEN_OR_PENDING_POSITION] ticker=%s", tk)
    elif result.skip_tag == "REBUY_SKIP_COOLDOWN":
        log.info(
            "[REBUY_SKIP_COOLDOWN] ticker=%s last_sell_date=%s elapsed_trading_days=%s required_trading_days=%s",
            tk,
            result.last_sell_date or "-",
            result.elapsed_trading_days if result.elapsed_trading_days is not None else "-",
            result.required_trading_days if result.required_trading_days is not None else "-",
        )
    elif result.skip_tag == "REBUY_SKIP_OLD_SIGNAL":
        log.info("[REBUY_SKIP_OLD_SIGNAL] ticker=%s", tk)
    elif result.skip_tag == "REBUY_SKIP_INSUFFICIENT_RECOVERY":
        rec = result.recovery_pct
        rec_s = f"{rec:.4f}" if rec is not None else "na"
        log.info(
            "[REBUY_SKIP_INSUFFICIENT_RECOVERY] ticker=%s last_sell_price=%s current_price=%s "
            "recovery_pct=%s required_pct=%s",
            tk,
            int(result.last_sell_price) if result.last_sell_price else 0,
            int(result.current_price) if result.current_price else 0,
            rec_s,
            result.required_recovery_pct if result.required_recovery_pct is not None else 0.03,
        )
    elif result.skip_tag == "REBUY_SKIP_PREVIOUS_EMERGENCY_DROP":
        log.info("[REBUY_SKIP_PREVIOUS_EMERGENCY_DROP] ticker=%s", tk)
    elif result.skip_tag == "REBUY_SKIP_UNKNOWN_EXIT_REASON":
        log.info("[REBUY_SKIP_UNKNOWN_EXIT_REASON] ticker=%s", tk)
    elif result.skip_tag == "REBUY_SKIP_MAX_COUNT":
        log.info("[REBUY_SKIP_MAX_COUNT] ticker=%s", tk)

    if result.eligible:
        rec = result.recovery_pct
        rec_s = f"{rec:.4f}" if rec is not None else "na"
        log.info(
            "[REBUY_APPROVED] ticker=%s signal_date=%s last_sell_date=%s last_sell_reason=%s "
            "cooldown_days=%s recovery_pct=%s rebuy_count=%s",
            tk,
            result.signal_date or "-",
            result.last_sell_date or "-",
            result.last_sell_reason or result.exit_type,
            result.elapsed_trading_days if result.elapsed_trading_days is not None else 0,
            rec_s,
            result.rebuy_count,
        )
        log.info("[REBUY_EVALUATION] ticker=%s eligible=true reason=approved", tk)
    else:
        log.info(
            "[REBUY_EVALUATION] ticker=%s eligible=false reason=%s",
            tk,
            result.reason or (result.skip_tag or "skip").replace("REBUY_SKIP_", "").lower(),
        )


def log_rebuy_order_submitted(ticker: str, logger_: Optional[logging.Logger] = None, **extra: Any) -> None:
    log = logger_ or logger
    extra_s = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
    if extra_s:
        log.info("[REBUY_ORDER_SUBMITTED] ticker=%s %s", _norm_ticker(ticker), extra_s)
    else:
        log.info("[REBUY_ORDER_SUBMITTED] ticker=%s", _norm_ticker(ticker))


def trade_record_to_row(trade: Any) -> Dict[str, Any]:
    """TradeRecord 또는 dict → 정책 입력 row."""
    if isinstance(trade, dict):
        return {
            "id": trade.get("id") or trade.get("record_id"),
            "timestamp": trade.get("timestamp"),
            "ticker": _norm_ticker(trade.get("ticker")),
            "action": str(trade.get("action") or trade.get("side") or "").upper(),
            "quantity": _safe_int(trade.get("quantity") or trade.get("qty")),
            "price": _safe_float(trade.get("price")),
            "order_status": str(trade.get("order_status") or trade.get("trade_status") or ""),
            "requested_qty": _safe_int(trade.get("requested_qty") or trade.get("quantity") or trade.get("qty")),
            "executed_qty": _safe_int(trade.get("executed_qty") or trade.get("quantity") or trade.get("qty")),
            "sell_reason": trade.get("sell_reason") or "",
            "reason_code": trade.get("reason_code") or "",
            "structured_context": trade.get("structured_context") or trade.get("strategy_details") or "",
        }
    return {
        "id": getattr(trade, "record_id", None),
        "timestamp": getattr(trade, "timestamp", None),
        "ticker": _norm_ticker(getattr(trade, "ticker", "")),
        "action": str(getattr(trade, "action", "") or "").upper(),
        "quantity": _safe_int(getattr(trade, "quantity", 0)),
        "price": _safe_float(getattr(trade, "price", 0)),
        "order_status": str(getattr(trade, "order_status", "") or ""),
        "requested_qty": _safe_int(getattr(trade, "requested_qty", 0) or getattr(trade, "quantity", 0)),
        "executed_qty": _safe_int(getattr(trade, "executed_qty", 0) or getattr(trade, "quantity", 0)),
        "sell_reason": getattr(trade, "sell_reason", "") or "",
        "reason_code": getattr(trade, "reason_code", "") or "",
        "structured_context": getattr(trade, "structured_context", "") or "",
    }


def default_policy_config(**overrides: Any) -> RebuyConfig:
    cfg = RebuyConfig(
        allow_rebuy=True,
        has_rebuy_section=True,
        enabled=True,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _row(
    *,
    ticker: str,
    action: str,
    ts: str,
    qty: int,
    price: float,
    status: str = "executed",
    sell_reason: str = "",
    reason_code: str = "",
    requested_qty: Optional[int] = None,
    ctx: Any = None,
    row_id: int = 0,
) -> Dict[str, Any]:
    return {
        "id": row_id,
        "timestamp": ts,
        "ticker": ticker,
        "action": action,
        "quantity": qty,
        "price": price,
        "order_status": status,
        "requested_qty": requested_qty if requested_qty is not None else qty,
        "executed_qty": qty if status not in ("pending", "submitted") else 0,
        "sell_reason": sell_reason,
        "reason_code": reason_code,
        "structured_context": ctx or "",
    }


KRAFTON_TICKER = "259960"
KRAFTON_TRADES = [
    _row(ticker=KRAFTON_TICKER, action="BUY", ts="2026-08-03T10:00:00", qty=6, price=235583, row_id=1),
    _row(
        ticker=KRAFTON_TICKER, action="SELL", ts="2026-08-05T10:00:00", qty=6, price=228000,
        sell_reason="고정 손절 (-3.01%)", reason_code="STOP_LOSS_HIT",
        ctx={"type": "StopLoss"}, row_id=2,
    ),
]


def _self_check() -> None:
    cfg = default_policy_config()
    tk = KRAFTON_TICKER

    # Case 1. 최초 매수
    r1 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=[],
        signal_date="20260807", gpt_decision="BUY", current_price=240000,
        as_of=date(2026, 8, 7),
    )
    assert r1.entry_type == ENTRY_NORMAL and r1.eligible, r1
    assert r1.reason == "never_bought"

    # Case 2. 현재 보유 중
    holding_trades = [_row(ticker=tk, action="BUY", ts="2026-08-03T10:00:00", qty=6, price=235583)]
    r2 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=6, trades=holding_trades,
        signal_date="20260807", gpt_decision="BUY", current_price=240000,
        as_of=date(2026, 8, 7),
    )
    assert r2.entry_type == ENTRY_EXISTING and not r2.eligible, r2
    assert r2.skip_tag == "REBUY_SKIP_CURRENT_HOLDING"

    # Case 3. StopLoss 후 2거래일 (크래프톤 실사례)
    r3 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=KRAFTON_TRADES,
        signal_date="20260807", gpt_decision="BUY", current_price=240000,
        as_of=date(2026, 8, 7),
    )
    assert r3.entry_type == ENTRY_REBUY and not r3.eligible, r3
    assert r3.skip_tag == "REBUY_SKIP_COOLDOWN", r3
    assert r3.elapsed_trading_days == 2, r3.elapsed_trading_days
    assert r3.required_trading_days == 5

    # Case 4. StopLoss 후 5거래일 + 회복 2%
    # 2026-08-05 SELL 이후 5 거래일 = 2026-08-12
    r4 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=KRAFTON_TRADES,
        signal_date="20260812", gpt_decision="BUY",
        current_price=228000 * 1.02,
        as_of=date(2026, 8, 12),
    )
    assert r4.skip_tag == "REBUY_SKIP_INSUFFICIENT_RECOVERY", r4
    assert r4.elapsed_trading_days == 5, r4.elapsed_trading_days

    # Case 5. StopLoss 후 5거래일 + 회복 4% + 신규 BUY
    r5 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=KRAFTON_TRADES,
        signal_date="20260812", gpt_decision="BUY",
        current_price=228000 * 1.04,
        as_of=date(2026, 8, 12),
    )
    assert r5.eligible and r5.skip_tag is None, r5
    assert r5.reason_code == "REBUY_AFTER_STOPLOSS"
    assert r5.recovery_pct is not None and r5.recovery_pct >= 0.03

    # Case 6. EmergencyDrop
    em_trades = [
        _row(ticker=tk, action="BUY", ts="2026-07-01T10:00:00", qty=6, price=240000, row_id=1),
        _row(
            ticker=tk, action="SELL", ts="2026-07-02T10:00:00", qty=6, price=220000,
            sell_reason="긴급 낙폭 손절(-8%) | 전략=EmergencyDrop",
            reason_code="EMERGENCY_DROP", ctx={"type": "EmergencyDrop"}, row_id=2,
        ),
    ]
    r6 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=em_trades,
        signal_date="20260812", gpt_decision="BUY", current_price=250000,
        as_of=date(2026, 8, 12),
    )
    assert r6.skip_tag == "REBUY_SKIP_PREVIOUS_EMERGENCY_DROP", r6

    # Case 7. TakeProfit 후 신규 BUY (쿨다운/recovery 비강제)
    tp_trades = [
        _row(ticker=tk, action="BUY", ts="2026-08-03T10:00:00", qty=6, price=235583, row_id=1),
        _row(
            ticker=tk, action="SELL", ts="2026-08-05T10:00:00", qty=6, price=250000,
            sell_reason="목표가 도달 | 전략=TakeProfit", reason_code="TAKE_PROFIT_HIT",
            ctx={"type": "TakeProfit"}, row_id=2,
        ),
    ]
    r7 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=tp_trades,
        signal_date="20260806", gpt_decision="BUY", current_price=248000,
        as_of=date(2026, 8, 6),
    )
    assert r7.eligible, r7
    assert r7.reason_code == "REBUY_AFTER_TAKEPROFIT"

    # Case 8. 과거 signal 재사용
    r8 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=KRAFTON_TRADES,
        signal_date="20260803", gpt_decision="BUY",
        current_price=228000 * 1.04,
        as_of=date(2026, 8, 12),
    )
    assert r8.skip_tag == "REBUY_SKIP_OLD_SIGNAL", r8

    # Case 9. Rebuy 1회 후 다시 청산 → 세 번째 진입 금지
    two_cycles = list(KRAFTON_TRADES) + [
        _row(ticker=tk, action="BUY", ts="2026-08-12T10:00:00", qty=6, price=237000, row_id=3),
        _row(
            ticker=tk, action="SELL", ts="2026-08-13T10:00:00", qty=6, price=230000,
            sell_reason="고정 손절 (-3.00%)", reason_code="STOP_LOSS_HIT",
            ctx={"type": "StopLoss"}, row_id=4,
        ),
    ]
    r9 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=two_cycles,
        signal_date="20260821", gpt_decision="BUY",
        current_price=230000 * 1.05,
        as_of=date(2026, 8, 21),
    )
    assert r9.skip_tag == "REBUY_SKIP_MAX_COUNT", r9
    assert r9.completed_cycles == 2 and r9.rebuy_count == 1

    # Case 10. Pending SELL
    pending_open = [
        _row(ticker=tk, action="BUY", ts="2026-08-03T10:00:00", qty=6, price=235583, row_id=1),
        _row(
            ticker=tk, action="SELL", ts="2026-08-05T10:00:00", qty=6, price=228000,
            status="pending", sell_reason="고정 손절 (-3.01%)", requested_qty=6, row_id=2,
        ),
    ]
    r10 = evaluate_rebuy(
        ticker=tk, cfg=cfg, account_qty=0, trades=pending_open,
        signal_date="20260812", gpt_decision="BUY", current_price=240000,
        as_of=date(2026, 8, 12),
    )
    assert r10.skip_tag == "REBUY_SKIP_OPEN_OR_PENDING_POSITION", r10

    # 레거시: rebuy 블록 없으면 청산 후 종목은 일반 신규로 통과
    legacy = load_rebuy_config({"allow_rebuy": False})
    assert not legacy.policy_enabled and not legacy.legacy_scale_in
    r_legacy = evaluate_rebuy(
        ticker=tk, cfg=legacy, account_qty=0, trades=KRAFTON_TRADES,
        signal_date="20260803", gpt_decision="BUY", current_price=240000,
        as_of=date(2026, 8, 7),
    )
    assert r_legacy.entry_type == ENTRY_NORMAL and r_legacy.eligible

    # reconcile 코드만 있고 sell_reason 이 손절이면 stop_loss
    recon = classify_exit_reason(
        "고정 손절 (-3.01%)",
        "RECONCILED_BY_HOLDING_FALLBACK_SELL",
        {"type": ""},
    )
    assert recon == EXIT_STOP_LOSS, recon
    recon2 = classify_exit_reason("", "RECONCILED_BY_HOLDING_FALLBACK_SELL", {})
    assert recon2 == EXIT_UNKNOWN

    print("rebuy_policy self-check PASS")


def _eval_krafton_db_readonly() -> None:
    """실제 DB를 읽기만 해서 크래프톤 사례를 평가. 기록 수정 없음."""
    try:
        from pathlib import Path
        candidates = [
            Path("output/trading_data.db"),
            Path("/app/output/trading_data.db"),
            Path(__file__).resolve().parent.parent / "output" / "trading_data.db",
        ]
        db_path = next((p for p in candidates if p.is_file()), None)
        if not db_path:
            print("krafton db eval SKIP (no trading_data.db)")
            return
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, timestamp, ticker, action, quantity, price,
                   order_status, requested_qty, executed_qty,
                   sell_reason, reason_code, structured_context
            FROM trade_records
            WHERE ticker IN (?, ?)
            ORDER BY timestamp ASC, id ASC
            """,
            (KRAFTON_TICKER, "259960"),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(f"krafton db rows={len(rows)} path={db_path} (read-only)")
        cfg = default_policy_config()
        for as_of in (date(2026, 8, 7), datetime.now(KST).date()):
            r = evaluate_rebuy(
                ticker=KRAFTON_TICKER,
                cfg=cfg,
                account_qty=0,
                trades=rows,
                signal_date=as_of.strftime("%Y%m%d"),
                gpt_decision="BUY",
                current_price=228000 * 1.04,
                as_of=as_of,
            )
            print(
                f"krafton as_of={as_of} eligible={r.eligible} reason={r.reason} "
                f"skip={r.skip_tag} elapsed={r.elapsed_trading_days}"
            )
    except Exception as e:
        print(f"krafton db eval SKIP ({e})")


if __name__ == "__main__":
    _self_check()
    _eval_krafton_db_readonly()
