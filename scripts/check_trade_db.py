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
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --verify-account-match
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --show-position-ledger
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --repair-candidates
  PYTHONPATH=/app/src python /app/scripts/check_trade_db.py --apply-repair-candidates
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils import KST, OUTPUT_DIR, setup_logging, get_account_snapshot_cached
from stale_sell_pending import find_stale_sell_pending_candidates, get_stale_pending_sell_hours
from position_ledger import (
    analyze_account_db_match,
    apply_repair_candidates_to_db,
    build_position_ledger,
    compute_open_positions,
    compute_paper_open_positions,
    filter_repair_candidates_for_output,
    format_repair_candidate_line,
    load_all_trade_rows,
    norm_ticker,
)

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


def _load_repair_analysis(
    *,
    include_paper_executed: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, int]]]:
    """account snapshot + repair analysis. account 없으면 (None, None)."""
    if not DB_PATH.is_file():
        print(f"DB not found: {DB_PATH}")
        return None, None
    rows = load_all_trade_rows(str(DB_PATH))
    account_positions, _ = _get_account_qty_by_ticker()
    if not account_positions:
        print(
            "[POSITION_REPAIR] status=skip reason=no_balance_snapshot "
            "(KIS account.py 실행 또는 balance_*.json 필요)"
        )
        return None, None
    analysis = analyze_account_db_match(
        rows,
        account_positions,
        include_paper_executed=include_paper_executed,
    )
    return analysis, account_positions


def _print_repair_candidates(
    candidates: List[Dict[str, Any]],
    *,
    title: str = "position repair candidates (dry-run",
) -> None:
    print(f"\n=== {title}, {len(candidates)}건) ===")
    if not candidates:
        print("(none)")
        return
    for c in candidates:
        print(format_repair_candidate_line(c))


def _repair_candidates(*, include_paper_executed: bool = False) -> int:
    analysis, _ = _load_repair_analysis(include_paper_executed=include_paper_executed)
    if analysis is None:
        return 1
    candidates = filter_repair_candidates_for_output(
        analysis.get("repair_candidates") or [],
        include_paper_executed=include_paper_executed,
    )
    _print_repair_candidates(candidates)
    return 0


def _backup_db_before_repair() -> Optional[str]:
    if not DB_PATH.is_file():
        print(f"DB not found: {DB_PATH}")
        return None
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    backup_path = OUTPUT_DIR / f"trading_data_before_position_repair_{ts}.db"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, backup_path)
    print(f"[POSITION_REPAIR] backup_created={backup_path}")
    return str(backup_path)


def _apply_repair_candidates(*, include_paper_executed: bool = False) -> int:
    analysis, account_positions = _load_repair_analysis(
        include_paper_executed=include_paper_executed,
    )
    if analysis is None or account_positions is None:
        return 1

    candidates = filter_repair_candidates_for_output(
        analysis.get("repair_candidates") or [],
        include_paper_executed=include_paper_executed,
    )
    applicable = [
        c for c in candidates
        if str(c.get("action") or "") in ("mark_failed_sell_executed", "mark_empty_order_buy_failed")
    ]

    if not applicable:
        print("[POSITION_REPAIR] no repair candidates applied")
        return 0

    print(f"[POSITION_REPAIR] apply_count={len(applicable)} (dry-run preview)")
    _print_repair_candidates(applicable, title="position repair candidates to apply")

    backup = _backup_db_before_repair()
    if not backup:
        return 1

    result = apply_repair_candidates_to_db(str(DB_PATH), applicable)
    applied = result.get("applied") or []
    skipped = result.get("skipped") or []

    if not applied and not skipped:
        print("[POSITION_REPAIR] no repair candidates applied")
        return 0
    if not applied:
        print("[POSITION_REPAIR] no repair candidates applied (all skipped)")
        return 0

    print(f"\n=== position repair applied ({len(applied)}건) ===")
    if skipped:
        print(f"[POSITION_REPAIR] skipped={len(skipped)}")

    print("\n=== post-repair account match ===")
    post_analysis, _ = _load_repair_analysis(include_paper_executed=include_paper_executed)
    if post_analysis is None:
        return 1
    mismatch_count = post_analysis.get("mismatch_count", 0)
    print(f"[POSITION_REPAIR] final_mismatch_count={mismatch_count}")
    for m in post_analysis.get("mismatches") or []:
        print(
            f"[ACCOUNT_DB_MATCH] ticker={m.get('ticker')} db_qty={m.get('db_qty')} "
            f"account_qty={m.get('account_qty')} status=mismatch "
            f"reason={m.get('mismatch_reason')} related_ids={m.get('related_trade_ids')}"
        )
    if mismatch_count == 0:
        open_positions = post_analysis.get("open_positions") or {}
        for ticker in sorted(set(open_positions) | set(account_positions)):
            print(
                f"[ACCOUNT_DB_MATCH] ticker={ticker} db_qty={open_positions.get(ticker, 0)} "
                f"account_qty={account_positions.get(ticker, 0)} status=ok"
            )
    return 1 if mismatch_count else 0


def _verify_account_match(*, include_paper_executed: bool = False) -> int:
    rows = load_all_trade_rows(str(DB_PATH))
    account_positions, source = _get_account_qty_by_ticker()
    if not account_positions:
        print(
            "[ACCOUNT_DB_MATCH] status=skip reason=no_balance_snapshot "
            "(KIS account.py 실행 또는 balance_*.json 필요)"
        )
        return 1

    analysis = analyze_account_db_match(
        rows,
        account_positions,
        include_paper_executed=include_paper_executed,
    )
    open_positions = analysis["open_positions"]
    paper_positions = analysis["paper_positions"]

    print(f"[ACCOUNT_DB_MATCH] account_source={source}")
    print(
        f"[ACCOUNT_DB_MATCH] open_basis=executed,partial "
        f"include_paper_executed={include_paper_executed}"
    )
    print(
        f"[ACCOUNT_DB_MATCH] db_open_tickers={len(open_positions)} "
        f"account_tickers={len(account_positions)} "
        f"pending_count={analysis['pending_count']} "
        f"paper_bucket_tickers={len(paper_positions)}"
    )

    all_tickers = sorted(set(open_positions) | set(account_positions))
    mismatches = 0
    for ticker in all_tickers:
        db_qty = int(open_positions.get(ticker, 0))
        account_qty = int(account_positions.get(ticker, 0))
        status = "ok" if db_qty == account_qty else "mismatch"
        note = ""
        if status == "mismatch":
            mismatches += 1
            mm = next(
                (m for m in analysis["mismatches"] if m["ticker"] == ticker),
                None,
            )
            if mm:
                note = (
                    f" reason={mm.get('mismatch_reason')} "
                    f"related_ids={mm.get('related_trade_ids')}"
                )
        print(
            f"[ACCOUNT_DB_MATCH] ticker={ticker} db_qty={db_qty} "
            f"account_qty={account_qty} status={status}{note}"
        )

    if paper_positions:
        print(f"\n=== paper_executed bucket ({len(paper_positions)} tickers) ===")
        for ticker, qty in sorted(paper_positions.items()):
            print(f"  ticker={ticker} paper_qty={qty}")

    repair_candidates = filter_repair_candidates_for_output(
        analysis.get("repair_candidates") or [],
        include_paper_executed=include_paper_executed,
    )
    if repair_candidates:
        _print_repair_candidates(repair_candidates)

    pending_rows = analysis.get("pending_rows") or []
    if pending_rows:
        print(f"\n=== pending/partial ({len(pending_rows)}건, open qty 미포함) ===")
        for r in pending_rows:
            print(
                f"  id={r.get('id')} ticker={norm_ticker(r.get('ticker'))} "
                f"action={r.get('action')} status={r.get('order_status')} "
                f"order_id={r.get('order_id')} qty={r.get('executed_qty') or r.get('quantity')}"
            )

    return 1 if mismatches else 0


def _show_position_ledger(*, include_paper_executed: bool = False, ticker: str = "") -> int:
    rows = load_all_trade_rows(str(DB_PATH))
    if ticker:
        t = norm_ticker(ticker)
        rows = [r for r in rows if norm_ticker(r.get("ticker")) == t]

    ledger = build_position_ledger(rows, include_paper_executed=include_paper_executed)
    open_positions = compute_open_positions(rows, include_paper_executed=include_paper_executed)
    paper_positions = compute_paper_open_positions(rows)

    print(
        f"[POSITION_LEDGER] rows={len(ledger)} include_paper_executed={include_paper_executed}"
    )
    print(f"[POSITION_LEDGER] open_positions={dict(sorted(open_positions.items()))}")
    if paper_positions:
        print(f"[POSITION_LEDGER] paper_bucket={dict(sorted(paper_positions.items()))}")

    by_ticker: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in ledger:
        by_ticker[entry["ticker"]].append(entry)

    for t in sorted(by_ticker):
        entries = by_ticker[t]
        print(f"\n--- ticker={t} open_qty={open_positions.get(t, 0)} ---")
        for e in entries:
            note = ""
            if e.get("repair_note"):
                note = f" repair_note={e['repair_note']}"
            excl = e.get("exclude_reason") or "-"
            print(
                f"  id={e.get('id')} action={e.get('action')} status={e.get('order_status')} "
                f"qty={e.get('executed_qty')} order_id={e.get('order_id') or ''} "
                f"included={e.get('included')} exclude_reason={excl} "
                f"delta={e.get('open_qty_delta')} running_open={e.get('running_open_qty')}"
                f"{note}"
            )
    return 0


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
        help="DB open position(executed/partial)과 account balance 비교",
    )
    parser.add_argument(
        "--show-position-ledger",
        action="store_true",
        help="ticker별 position ledger (included/exclude_reason)",
    )
    parser.add_argument(
        "--include-paper-executed",
        action="store_true",
        help="open position 계산에 paper_executed 포함",
    )
    parser.add_argument(
        "--repair-candidates",
        action="store_true",
        help="DB 수정 없이 position repair 후보만 출력",
    )
    parser.add_argument(
        "--apply-repair-candidates",
        action="store_true",
        help="repair 후보를 DB에 적용 (실행 전 자동 백업)",
    )
    parser.add_argument("--limit", type=int, default=100, help="최대 조회 건수")
    args = parser.parse_args()

    if args.repair_candidates:
        return _repair_candidates(include_paper_executed=args.include_paper_executed)

    if args.apply_repair_candidates:
        return _apply_repair_candidates(include_paper_executed=args.include_paper_executed)

    if args.verify_account_match:
        return _verify_account_match(include_paper_executed=args.include_paper_executed)

    if args.show_position_ledger:
        return _show_position_ledger(
            include_paper_executed=args.include_paper_executed,
            ticker=args.ticker or "",
        )

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
