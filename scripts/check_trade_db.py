#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_records DB 점검 유틸 (sqlite3 CLI 없이 사용).

Usage:
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --pending
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --today
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --latest 20
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --stale-sell-pending --stale-hours 24
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --ticker 032830
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --order-status failed
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple

from utils import KST, OUTPUT_DIR, setup_logging, get_account_snapshot_cached
from stale_sell_pending import find_stale_sell_pending_candidates, get_stale_pending_sell_hours

DB_PATH = OUTPUT_DIR / "trading_data.db"


def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
    return {col: row[i] for i, col in enumerate(columns)}


def _fetch_rows(
    where: str = "",
    params: tuple = (),
    limit: int = 100,
    order: str = "timestamp DESC",
) -> List[Dict[str, Any]]:
    if not DB_PATH.is_file():
        print(f"DB not found: {DB_PATH}")
        return []
    q = f"SELECT * FROM trade_records {where} ORDER BY {order} LIMIT ?"
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(q, (*params, limit))
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_row_to_dict(columns, r) for r in rows]


def _print_rows(rows: List[Dict[str, Any]], title: str) -> None:
    print(f"\n=== {title} ({len(rows)}건) ===")
    if not rows:
        print("(none)")
        return
    for r in rows:
        print(
            f"id={r.get('id')} ts={r.get('timestamp')} ticker={r.get('ticker')} "
            f"action={r.get('action')} status={r.get('order_status')} "
            f"order_id={r.get('order_id')} executed_qty={r.get('executed_qty')} "
            f"reason={r.get('reason_code') or ''}"
        )


def _compute_db_open_positions() -> Dict[str, int]:
    """DB executed 체결 기준 순보유 수량."""
    rows = _fetch_rows(limit=10000, order="timestamp ASC")
    qty_by_ticker: Dict[str, int] = defaultdict(int)
    executed_statuses = {"executed", "partial", "completed", "paper_executed"}
    for r in rows:
        status = str(r.get("order_status") or "").lower()
        if status not in executed_statuses:
            continue
        exe = int(r.get("executed_qty") or r.get("quantity") or 0)
        if exe <= 0:
            continue
        ticker = str(r.get("ticker") or "").zfill(6)
        action = str(r.get("action") or "").upper()
        if action == "BUY":
            qty_by_ticker[ticker] += exe
        elif action == "SELL":
            qty_by_ticker[ticker] -= exe
    return {t: q for t, q in qty_by_ticker.items() if q > 0}


def _get_account_qty_by_ticker() -> Tuple[Dict[str, int], str]:
    """balance 스냅샷 기준 계좌 보유 수량."""
    _, holdings, _, balance_path = get_account_snapshot_cached(
        summary_pattern="summary_*.json",
        balance_pattern="balance_*.json",
        ttl_sec=5,
    )
    if not holdings:
        return {}, "balance_snapshot_missing"
    qty_map: Dict[str, int] = {}
    for h in holdings:
        qty = int(str(h.get("hldg_qty", 0)).replace(",", "") or 0)
        if qty <= 0:
            continue
        ticker = str(h.get("pdno", "")).zfill(6)
        qty_map[ticker] = qty
    source = f"balance_file:{balance_path.name}" if balance_path else "balance_snapshot"
    return qty_map, source


def _verify_account_match() -> int:
    db_positions = _compute_db_open_positions()
    account_positions, source = _get_account_qty_by_ticker()
    if not account_positions:
        print(
            "[ACCOUNT_DB_MATCH] status=skip reason=no_balance_snapshot "
            "(KIS account.py 실행 또는 balance_*.json 필요)"
        )
        return 1
    print(f"[ACCOUNT_DB_MATCH] account_source={source}")
    all_tickers = sorted(set(db_positions) | set(account_positions))
    mismatches = 0
    for ticker in all_tickers:
        db_qty = int(db_positions.get(ticker, 0))
        account_qty = int(account_positions.get(ticker, 0))
        status = "ok" if db_qty == account_qty else "mismatch"
        if status == "mismatch":
            mismatches += 1
        print(
            f"[ACCOUNT_DB_MATCH] ticker={ticker} db_qty={db_qty} "
            f"account_qty={account_qty} status={status}"
        )
    return 1 if mismatches else 0


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="trade_records DB 점검")
    parser.add_argument("--latest", type=int, metavar="N", help="최근 N건 출력")
    parser.add_argument("--today", action="store_true", help="오늘(KST) 기록 출력")
    parser.add_argument("--pending", action="store_true", help="pending/partial 기록 출력")
    parser.add_argument("--stale-sell-pending", action="store_true", help="stale SELL pending 후보")
    parser.add_argument("--stale-hours", type=int, default=None, help="stale 판정 시간(기본 config)")
    parser.add_argument("--ticker", help="특정 ticker 검색")
    parser.add_argument("--order-status", dest="order_status", help="order_status 필터")
    parser.add_argument(
        "--verify-account-match",
        action="store_true",
        help="DB executed open position과 account balance 보유 수량 비교",
    )
    parser.add_argument("--limit", type=int, default=100, help="최대 조회 건수")
    args = parser.parse_args()

    if args.verify_account_match:
        return _verify_account_match()

    if args.stale_sell_pending:
        stale_hours = args.stale_hours if args.stale_hours is not None else get_stale_pending_sell_hours()
        candidates = find_stale_sell_pending_candidates(
            stale_hours=stale_hours,
            since_hours=120,
            limit=args.limit,
        )
        print(f"\n=== stale SELL pending 후보 (stale_hours={stale_hours}, {len(candidates)}건) ===")
        if not candidates:
            print("(none)")
        for r in candidates:
            diag = r.get("_stale_diag") or {}
            print(
                f"id={r.get('id')} ticker={r.get('ticker')} order_id={r.get('order_id')} "
                f"status={r.get('order_status')} age_hours={diag.get('age_hours')} "
                f"kis_missing={diag.get('kis_missing')} holding_missing={diag.get('holding_missing')}"
            )
        return 0

    if args.pending:
        rows = _fetch_rows(
            "WHERE lower(order_status) IN ('pending','partial')",
            limit=args.limit,
        )
        _print_rows(rows, "pending/partial")
        return 0

    if args.today:
        today = datetime.now(KST).strftime("%Y-%m-%d")
        rows = _fetch_rows(
            "WHERE timestamp >= ?",
            (today,),
            limit=args.limit,
        )
        _print_rows(rows, f"today ({today})")
        return 0

    if args.ticker:
        t = str(args.ticker).zfill(6)
        rows = _fetch_rows("WHERE ticker = ?", (t,), limit=args.limit)
        _print_rows(rows, f"ticker={t}")
        return 0

    if args.order_status:
        st = str(args.order_status).lower()
        rows = _fetch_rows(
            "WHERE lower(order_status) = ?",
            (st,),
            limit=args.limit,
        )
        _print_rows(rows, f"order_status={st}")
        return 0

    n = args.latest if args.latest else 20
    rows = _fetch_rows(limit=n)
    _print_rows(rows, f"latest {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
