#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DB open position ledger — trade_records 기준 순보유 계산·불일치·repair 후보 탐지.

check_trade_db.py, performance_review.py에서 공유한다.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from utils import OUTPUT_DIR

OPEN_POSITION_STATUSES = frozenset({"executed", "partial"})
PAPER_STATUS = "paper_executed"

MISMATCH_ACCOUNT_EMPTY_DB_OPEN = "account_empty_db_still_open"
MISMATCH_FAILED_SELL_CLOSE = "failed_sell_should_close_candidate"
MISMATCH_EMPTY_ORDER_BUY = "empty_order_id_buy_candidate"
MISMATCH_PAPER_DUPLICATE = "possible_paper_executed_duplicate"
MISMATCH_DB_GT_ACCOUNT = "db_qty_greater_than_account"


def norm_ticker(t: Any) -> str:
    return str(t or "").strip().zfill(6)


def safe_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def row_executed_qty(row: Dict[str, Any]) -> int:
    return safe_int(row.get("executed_qty") or row.get("quantity") or row.get("requested_qty"))


def load_all_trade_rows(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = db_path or str(OUTPUT_DIR / "trading_data.db")
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, timestamp, ticker, action, quantity, price, amount,
                   order_status, order_id, requested_qty, executed_qty, reason_code
            FROM trade_records
            ORDER BY timestamp ASC, id ASC
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def classify_row_for_open(
    row: Dict[str, Any],
    *,
    include_paper_executed: bool = False,
) -> Tuple[bool, Optional[str], int]:
    """
    open position 포함 여부, 제외 사유, signed qty delta 반환.
    delta: BUY +qty, SELL -qty (포함 시), 미포함 시 0.
    """
    status = str(row.get("order_status") or "").lower()
    action = str(row.get("action") or "").upper()
    qty = row_executed_qty(row)
    if qty <= 0:
        return False, "excluded_zero_qty", 0

    if status in ("pending",):
        return False, "excluded_pending", 0
    if status in ("failed", "cancelled"):
        return False, "excluded_status_failed", 0
    if status == PAPER_STATUS:
        if not include_paper_executed:
            return False, "excluded_status_paper_executed", 0
    elif status not in OPEN_POSITION_STATUSES:
        return False, f"excluded_status_{status or 'unknown'}", 0

    if action == "BUY":
        return True, None, qty
    if action == "SELL":
        return True, None, -qty
    return False, "excluded_unknown_action", 0


def build_position_ledger(
    rows: List[Dict[str, Any]],
    *,
    include_paper_executed: bool = False,
) -> List[Dict[str, Any]]:
    """ticker별 ledger (시간순). running_open_qty는 포함 row만 누적."""
    running: Dict[str, int] = defaultdict(int)
    ledger: List[Dict[str, Any]] = []
    for row in rows:
        ticker = norm_ticker(row.get("ticker"))
        if not ticker or ticker == "000000":
            continue
        included, exclude_reason, delta = classify_row_for_open(
            row, include_paper_executed=include_paper_executed
        )
        order_id = str(row.get("order_id") or "").strip()
        repair_note: Optional[str] = None
        if included and action_is_buy(row) and not order_id:
            repair_note = "empty_order_id_buy_candidate"

        if included:
            running[ticker] += delta

        ledger.append({
            "id": row.get("id"),
            "timestamp": row.get("timestamp"),
            "ticker": ticker,
            "action": str(row.get("action") or "").upper(),
            "order_status": str(row.get("order_status") or "").lower(),
            "executed_qty": row_executed_qty(row),
            "order_id": order_id or None,
            "reason_code": row.get("reason_code") or "",
            "included": included,
            "exclude_reason": exclude_reason,
            "open_qty_delta": delta if included else 0,
            "running_open_qty": running[ticker] if included else running[ticker],
            "repair_note": repair_note,
        })
    return ledger


def action_is_buy(row: Dict[str, Any]) -> bool:
    return str(row.get("action") or "").upper() == "BUY"


def action_is_sell(row: Dict[str, Any]) -> bool:
    return str(row.get("action") or "").upper() == "SELL"


def compute_open_positions(
    rows: List[Dict[str, Any]],
    *,
    include_paper_executed: bool = False,
) -> Dict[str, int]:
    qty_by_ticker: Dict[str, int] = defaultdict(int)
    for row in rows:
        ticker = norm_ticker(row.get("ticker"))
        included, _, delta = classify_row_for_open(
            row, include_paper_executed=include_paper_executed
        )
        if included and delta != 0:
            qty_by_ticker[ticker] += delta
    return {t: q for t, q in qty_by_ticker.items() if q > 0}


def compute_paper_open_positions(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """paper_executed만 별도 bucket."""
    paper_rows = [
        r for r in rows
        if str(r.get("order_status") or "").lower() == PAPER_STATUS
    ]
    return compute_open_positions(paper_rows, include_paper_executed=True)


def _rows_by_ticker(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[norm_ticker(row.get("ticker"))].append(row)
    return dict(out)


def _related_trade_ids(ticker_rows: List[Dict[str, Any]]) -> List[int]:
    return [safe_int(r.get("id")) for r in ticker_rows if r.get("id") is not None]


def _classify_mismatch_reason(
    ticker: str,
    db_qty: int,
    account_qty: int,
    ticker_rows: List[Dict[str, Any]],
    *,
    include_paper_executed: bool,
) -> Tuple[str, List[int]]:
    """mismatch 원인 분류 및 관련 trade id."""
    related = _related_trade_ids(ticker_rows)

    if account_qty == 0 and db_qty > 0:
        failed_sells = [
            r for r in ticker_rows
            if action_is_sell(r) and str(r.get("order_status") or "").lower() == "failed"
        ]
        if failed_sells:
            return MISMATCH_FAILED_SELL_CLOSE, [safe_int(r.get("id")) for r in failed_sells]

        empty_oid_buys = [
            r for r in ticker_rows
            if action_is_buy(r)
            and not str(r.get("order_id") or "").strip()
            and str(r.get("order_status") or "").lower() in OPEN_POSITION_STATUSES
            and row_executed_qty(r) > 0
        ]
        if empty_oid_buys:
            return MISMATCH_EMPTY_ORDER_BUY, [safe_int(r.get("id")) for r in empty_oid_buys]

        return MISMATCH_ACCOUNT_EMPTY_DB_OPEN, related

    if db_qty > account_qty:
        paper_rows = [
            r for r in ticker_rows
            if str(r.get("order_status") or "").lower() == PAPER_STATUS and action_is_buy(r)
        ]
        real_rows = [
            r for r in ticker_rows
            if str(r.get("order_status") or "").lower() in OPEN_POSITION_STATUSES
            and action_is_buy(r)
            and str(r.get("order_id") or "").strip()
        ]
        if paper_rows and real_rows and not include_paper_executed:
            return MISMATCH_PAPER_DUPLICATE, [safe_int(r.get("id")) for r in paper_rows]

        pending_sells = [
            r for r in ticker_rows
            if action_is_sell(r)
            and str(r.get("order_status") or "").lower() in ("pending", "partial")
        ]
        if pending_sells:
            return MISMATCH_DB_GT_ACCOUNT, [safe_int(r.get("id")) for r in pending_sells]

        return MISMATCH_DB_GT_ACCOUNT, related

    return MISMATCH_DB_GT_ACCOUNT, related


def detect_repair_candidates(
    rows: List[Dict[str, Any]],
    open_positions: Dict[str, int],
    account_positions: Dict[str, int],
    *,
    include_paper_executed: bool = False,
) -> List[Dict[str, Any]]:
    """dry-run repair 후보만 반환 (DB 변경 없음)."""
    by_ticker = _rows_by_ticker(rows)
    candidates: List[Dict[str, Any]] = []
    seen_keys: set = set()

    def _add(candidate: Dict[str, Any]) -> None:
        key = (candidate.get("ticker"), candidate.get("action"), tuple(candidate.get("ids") or []))
        if key in seen_keys:
            return
        seen_keys.add(key)
        candidates.append(candidate)

    for ticker in sorted(by_ticker):
        ticker_rows = by_ticker[ticker]
        db_qty = safe_int(open_positions.get(ticker, 0))
        account_qty = safe_int(account_positions.get(ticker, 0))

        paper_buys = [
            r for r in ticker_rows
            if str(r.get("order_status") or "").lower() == PAPER_STATUS and action_is_buy(r)
        ]
        real_buys = [
            r for r in ticker_rows
            if str(r.get("order_status") or "").lower() in OPEN_POSITION_STATUSES
            and action_is_buy(r)
            and str(r.get("order_id") or "").strip()
        ]
        if paper_buys and real_buys:
            primary = max(real_buys, key=lambda r: safe_int(r.get("id")))
            _add({
                "ticker": ticker,
                "action": "exclude_paper_executed",
                "id": None,
                "ids": [r.get("id") for r in paper_buys],
                "qty": sum(row_executed_qty(r) for r in paper_buys),
                "reason": MISMATCH_PAPER_DUPLICATE,
                "detail": f"paper_duplicate_actual_order_exists id={primary.get('id')}",
                "related_real_id": primary.get("id"),
            })

        if db_qty == account_qty:
            continue

        if account_qty == 0 and db_qty > 0:
            failed_sells = [
                r for r in ticker_rows
                if action_is_sell(r) and str(r.get("order_status") or "").lower() == "failed"
            ]
            for fs in failed_sells:
                _add({
                    "ticker": ticker,
                    "action": "mark_failed_sell_executed",
                    "id": fs.get("id"),
                    "ids": [fs.get("id")],
                    "qty": row_executed_qty(fs),
                    "reason": MISMATCH_ACCOUNT_EMPTY_DB_OPEN,
                    "detail": "account_empty_db_still_open",
                })

            empty_buys = [
                r for r in ticker_rows
                if action_is_buy(r)
                and not str(r.get("order_id") or "").strip()
                and str(r.get("order_status") or "").lower() in OPEN_POSITION_STATUSES
                and row_executed_qty(r) > 0
            ]
            for eb in empty_buys:
                _add({
                    "ticker": ticker,
                    "action": "mark_empty_order_buy_failed",
                    "id": eb.get("id"),
                    "ids": [eb.get("id")],
                    "qty": row_executed_qty(eb),
                    "reason": MISMATCH_EMPTY_ORDER_BUY,
                    "detail": "account_empty_no_broker_evidence",
                })

    return candidates


def analyze_account_db_match(
    rows: List[Dict[str, Any]],
    account_positions: Dict[str, int],
    *,
    include_paper_executed: bool = False,
    review_tickers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """open position, mismatch, repair 후보 통합 분석."""
    open_positions = compute_open_positions(rows, include_paper_executed=include_paper_executed)
    paper_positions = compute_paper_open_positions(rows)
    by_ticker = _rows_by_ticker(rows)

    pending_rows = [
        r for r in rows
        if str(r.get("order_status") or "").lower() in ("pending", "partial")
    ]
    pending_by_ticker: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in pending_rows:
        pending_by_ticker[norm_ticker(r.get("ticker"))].append(r)

    relevant = set(open_positions) | set(account_positions) | set(pending_by_ticker)
    if review_tickers:
        relevant |= {norm_ticker(t) for t in review_tickers}

    mismatches: List[Dict[str, Any]] = []
    for ticker in sorted(relevant):
        db_qty = safe_int(open_positions.get(ticker, 0))
        account_qty = safe_int(account_positions.get(ticker, 0))
        if db_qty == account_qty:
            continue
        ticker_rows = by_ticker.get(ticker, [])
        reason, related_ids = _classify_mismatch_reason(
            ticker, db_qty, account_qty, ticker_rows,
            include_paper_executed=include_paper_executed,
        )
        pending_ids = [r.get("id") for r in pending_by_ticker.get(ticker, [])]
        mismatches.append({
            "ticker": ticker,
            "db_qty": db_qty,
            "account_qty": account_qty,
            "mismatch_reason": reason,
            "related_trade_ids": related_ids,
            "pending_sell_ids": [
                r.get("id") for r in pending_by_ticker.get(ticker, [])
                if action_is_sell(r)
            ],
            "all_trade_ids": _related_trade_ids(ticker_rows),
        })

    repair_candidates = detect_repair_candidates(
        rows, open_positions, account_positions,
        include_paper_executed=include_paper_executed,
    )

    return {
        "open_positions": open_positions,
        "paper_positions": paper_positions,
        "include_paper_executed": include_paper_executed,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "repair_candidates": repair_candidates,
        "pending_count": len(pending_rows),
        "pending_rows": pending_rows,
    }


def format_repair_candidate_line(candidate: Dict[str, Any]) -> str:
    ids = candidate.get("ids") or ([candidate.get("id")] if candidate.get("id") else [])
    id_part = ",".join(str(i) for i in ids if i is not None)
    if candidate.get("action") == "exclude_paper_executed":
        return (
            f"[POSITION_REPAIR_CANDIDATE] ticker={candidate.get('ticker')} "
            f"action={candidate.get('action')} ids={id_part} "
            f"reason={candidate.get('detail') or candidate.get('reason')}"
        )
    return (
        f"[POSITION_REPAIR_CANDIDATE] ticker={candidate.get('ticker')} "
        f"action={candidate.get('action')} id={candidate.get('id')} "
        f"qty={candidate.get('qty')} reason={candidate.get('detail') or candidate.get('reason')}"
    )
