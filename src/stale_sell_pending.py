#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stale SELL pending 탐지·정리 공통 로직.

trader.py, order_reconciler.py, check_trade_db.py에서 공유한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from utils import KST, get_account_snapshot_cached
from settings import settings
from recorder import get_recorder

logger = logging.getLogger("StaleSellPending")

_CLEANUP_REASON_AUTO = "AUTO_CLEANUP_STALE_SELL_PENDING"
_CLEANUP_REASON_MANUAL = "MANUAL_CLEANUP_STALE_SELL_PENDING"


def get_stale_pending_sell_hours() -> int:
    tp = getattr(settings, "trading_params", {}) or {}
    if isinstance(tp, dict):
        return int(tp.get("stale_pending_sell_hours", 24))
    return 24


def is_auto_cleanup_enabled() -> bool:
    tp = getattr(settings, "trading_params", {}) or {}
    if isinstance(tp, dict):
        return bool(tp.get("auto_cleanup_stale_sell_pending", False))
    return False


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _parse_row_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    ts = row.get("timestamp") or row.get("last_status_update_ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None


def _load_holdings_tickers() -> Set[str]:
    tickers: Set[str] = set()
    try:
        _, balance_list, _, _ = get_account_snapshot_cached()
        for h in balance_list or []:
            if not isinstance(h, dict):
                continue
            t = str(h.get("pdno", h.get("ticker", "")) or "").zfill(6)
            qty = _safe_int(h.get("hldg_qty", 0))
            if t and t != "000000" and qty > 0:
                tickers.add(t)
    except Exception as e:
        logger.warning("holdings 로드 실패: %s", e)
    return tickers


def _append_reason_code(existing: str, code: str) -> str:
    existing = str(existing or "").strip()
    code = str(code or "").strip()
    if not code:
        return existing
    if not existing:
        return code
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    if code in parts:
        return existing
    parts.append(code)
    return ",".join(parts)


def is_stale_sell_row(
    row: Dict[str, Any],
    *,
    stale_hours: int,
    now: Optional[datetime] = None,
    kis_open_order_ids: Optional[Set[str]] = None,
    kis_daily_order_ids: Optional[Set[str]] = None,
    holding_tickers: Optional[Set[str]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    stale SELL pending 후보 여부와 진단 정보 반환.
    """
    diag: Dict[str, Any] = {
        "kis_missing": False,
        "holding_missing": False,
        "age_hours": None,
        "reason": "",
    }
    action = str(row.get("action") or "").strip().upper()
    if action != "SELL":
        diag["reason"] = "not_sell"
        return False, diag

    status = str(row.get("order_status") or "").lower()
    if status not in ("pending", "partial"):
        diag["reason"] = f"status={status}"
        return False, diag

    order_id = str(row.get("order_id") or "").strip()
    if not order_id:
        diag["reason"] = "no_order_id"
        return False, diag

    executed_qty = row.get("executed_qty")
    if executed_qty is not None and _safe_int(executed_qty) > 0:
        diag["reason"] = "executed_qty_gt_0"
        return False, diag

    row_ts = _parse_row_timestamp(row)
    now = now or datetime.now(KST)
    if not row_ts:
        diag["reason"] = "no_timestamp"
        return False, diag

    age = now - row_ts
    age_hours = age.total_seconds() / 3600.0
    diag["age_hours"] = round(age_hours, 2)
    if age_hours < stale_hours:
        diag["reason"] = "not_stale_yet"
        return False, diag

    if kis_open_order_ids is not None and order_id in kis_open_order_ids:
        diag["reason"] = "kis_open_exists"
        return False, diag
    if kis_daily_order_ids is not None and order_id in kis_daily_order_ids:
        diag["reason"] = "kis_daily_exists"
        return False, diag

    if kis_open_order_ids is not None or kis_daily_order_ids is not None:
        diag["kis_missing"] = True

    ticker = str(row.get("ticker") or "").zfill(6)
    if holding_tickers is None:
        holding_tickers = _load_holdings_tickers()
    if ticker in holding_tickers:
        diag["reason"] = "holding_exists"
        return False, diag

    diag["holding_missing"] = True
    return True, diag


def find_stale_sell_pending_candidates(
    *,
    stale_hours: Optional[int] = None,
    since_hours: int = 120,
    limit: int = 500,
    kis_open_order_ids: Optional[Set[str]] = None,
    kis_daily_order_ids: Optional[Set[str]] = None,
    holding_tickers: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """DB open SELL 중 stale 후보 목록."""
    stale_hours = stale_hours if stale_hours is not None else get_stale_pending_sell_hours()
    recorder = get_recorder()
    since_dt = datetime.now(KST) - timedelta(hours=since_hours)
    open_rows = recorder.get_open_orders(
        statuses=("pending", "partial"),
        since_ts=since_dt.isoformat(),
        limit=limit,
    )

    candidates: List[Dict[str, Any]] = []
    for row in open_rows:
        is_stale, diag = is_stale_sell_row(
            row,
            stale_hours=stale_hours,
            kis_open_order_ids=kis_open_order_ids,
            kis_daily_order_ids=kis_daily_order_ids,
            holding_tickers=holding_tickers,
        )
        if is_stale:
            candidates.append({**row, "_stale_diag": diag})
    return candidates


def mark_stale_sell_failed(
    row: Dict[str, Any],
    *,
    checked_by: str = "trader",
    manual: bool = False,
    dry_run: bool = False,
) -> bool:
    """stale SELL pending을 failed로 정리. dry_run이면 DB 갱신 없음."""
    order_id = str(row.get("order_id") or "").strip()
    ticker = str(row.get("ticker") or "").zfill(6)
    diag = row.get("_stale_diag") or {}
    age_hours = diag.get("age_hours", "?")
    reason_code = _CLEANUP_REASON_MANUAL if manual else _CLEANUP_REASON_AUTO

    if dry_run:
        logger.warning(
            "[STALE_SELL_PENDING_DETECTED] ticker=%s order_id=%s age_hours=%s "
            "kis_missing=%s holding_missing=%s (dry_run)",
            ticker, order_id, age_hours,
            diag.get("kis_missing"), diag.get("holding_missing"),
        )
        return False

    recorder = get_recorder()
    existing_reason = ""
    existing_ctx = row.get("structured_context") or ""
    try:
        with __import__("sqlite3").connect(recorder.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT reason_code, structured_context FROM trade_records WHERE order_id = ? LIMIT 1",
                (order_id,),
            )
            r = cur.fetchone()
            if r:
                existing_reason = r[0] or ""
                existing_ctx = r[1] or existing_ctx
    except Exception:
        pass

    new_reason = _append_reason_code(existing_reason, reason_code)
    ctx_updates = {
        "cleanup_reason": reason_code,
        "checked_by": checked_by,
        "kis_query_missing": bool(diag.get("kis_missing")),
        "holding_missing": bool(diag.get("holding_missing")),
        "cleanup_ts": datetime.now(KST).isoformat(),
    }
    old_status = str(row.get("order_status") or "pending")

    n = recorder.update_order_status(
        order_id=order_id,
        order_status="failed",
        executed_qty=0,
        reason_code=new_reason,
        structured_context=json.dumps(ctx_updates, ensure_ascii=False),
        merge_context=True,
    )
    if n:
        logger.warning(
            "[STALE_SELL_PENDING_CLEANUP] ticker=%s order_id=%s old_status=%s "
            "new_status=failed reason=%s",
            ticker, order_id, old_status, reason_code,
        )
        return True
    logger.warning(
        "[STALE_SELL_PENDING_KEEP] ticker=%s order_id=%s reason=update_failed",
        ticker, order_id,
    )
    return False


def detect_and_handle_stale_sell_pending(
    *,
    checked_by: str = "trader",
    stale_hours: Optional[int] = None,
    since_hours: int = 120,
    auto_cleanup: Optional[bool] = None,
    kis_open_order_ids: Optional[Set[str]] = None,
    kis_daily_order_ids: Optional[Set[str]] = None,
    holding_tickers: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    stale SELL pending 탐지. auto_cleanup=True일 때만 failed 처리.
    반환: stale 후보 목록.
    """
    stale_hours = stale_hours if stale_hours is not None else get_stale_pending_sell_hours()
    if auto_cleanup is None:
        auto_cleanup = is_auto_cleanup_enabled()

    candidates = find_stale_sell_pending_candidates(
        stale_hours=stale_hours,
        since_hours=since_hours,
        kis_open_order_ids=kis_open_order_ids,
        kis_daily_order_ids=kis_daily_order_ids,
        holding_tickers=holding_tickers,
    )

    for row in candidates:
        order_id = str(row.get("order_id") or "").strip()
        ticker = str(row.get("ticker") or "").zfill(6)
        diag = row.get("_stale_diag") or {}
        logger.warning(
            "[STALE_SELL_PENDING_DETECTED] ticker=%s order_id=%s age_hours=%s "
            "kis_missing=%s holding_missing=%s",
            ticker, order_id, diag.get("age_hours"),
            diag.get("kis_missing"), diag.get("holding_missing"),
        )
        if auto_cleanup:
            mark_stale_sell_failed(row, checked_by=checked_by, manual=False)
        else:
            logger.warning(
                "[STALE_SELL_PENDING_KEEP] ticker=%s order_id=%s reason=auto_cleanup_disabled",
                ticker, order_id,
            )
    return candidates
