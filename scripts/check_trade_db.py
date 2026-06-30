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
from datetime import datetime
from typing import Any, Dict, List

from utils import KST, OUTPUT_DIR, setup_logging
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
    parser.add_argument("--limit", type=int, default=100, help="최대 조회 건수")
    args = parser.parse_args()

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
