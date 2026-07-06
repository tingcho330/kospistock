#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DB open position ledger — trade_records 기준 순보유 계산·불일치·repair 후보 탐지.

check_trade_db.py, performance_review.py에서 공유한다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from utils import OUTPUT_DIR

logger = logging.getLogger("PositionLedger")

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


REPAIR_TAG_SELL_EXECUTED = "REPAIRED_BY_ACCOUNT_ABSENCE_SELL_EXECUTED"
REPAIR_TAG_BUY_FAILED = "REPAIRED_ACCOUNT_ABSENCE_NO_ORDER_ID_BUY_MARK_FAILED"

_APPLY_ACTIONS = frozenset({"mark_failed_sell_executed", "mark_empty_order_buy_failed"})


def filter_repair_candidates_for_output(
    candidates: List[Dict[str, Any]],
    *,
    include_paper_executed: bool = False,
) -> List[Dict[str, Any]]:
    """출력/적용 대상 repair 후보 (paper duplicate는 --include-paper-executed일 때만)."""
    out: List[Dict[str, Any]] = []
    for c in candidates:
        action = str(c.get("action") or "")
        if action == "exclude_paper_executed":
            if include_paper_executed:
                out.append(c)
            continue
        if action in _APPLY_ACTIONS:
            out.append(c)
    return out


def filter_repair_candidates_for_apply(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """DB apply 대상 (paper_executed 제외 — open position에서 이미 제외됨)."""
    return [c for c in candidates if str(c.get("action") or "") in _APPLY_ACTIONS]


def append_reason_code(existing: str, code: str) -> str:
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


def _row_to_dict(cursor: sqlite3.Cursor, row: Any) -> Dict[str, Any]:
    """sqlite3.Row 또는 tuple fetch 결과를 dict로 변환."""
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [desc[0] for desc in (cursor.description or [])]
    if columns and len(columns) == len(row):
        return dict(zip(columns, row))
    return {}


def _fetch_row_by_id(conn: sqlite3.Connection, row_id: int) -> Optional[Dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT id, ticker, action, quantity, requested_qty, executed_qty,
               order_status, order_id, reason_code
        FROM trade_records WHERE id = ?
        """,
        (int(row_id),),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


def _repair_skip(row_id: Any, reason: str) -> Dict[str, Any]:
    print(f"[POSITION_REPAIR_SKIP] id={row_id} reason={reason}")
    return {"status": "skipped", "id": row_id, "reason": reason}


def _is_already_repaired_sell(row: Dict[str, Any]) -> bool:
    reason = str(row.get("reason_code") or "")
    if REPAIR_TAG_SELL_EXECUTED in reason:
        return True
    status = str(row.get("order_status") or "").lower()
    target_qty = safe_int(row.get("quantity") or row.get("requested_qty") or row_executed_qty(row))
    if status == "executed" and safe_int(row.get("executed_qty")) == target_qty and target_qty > 0:
        return True
    return False


def _is_already_repaired_buy_failed(row: Dict[str, Any]) -> bool:
    reason = str(row.get("reason_code") or "")
    if REPAIR_TAG_BUY_FAILED in reason:
        return True
    status = str(row.get("order_status") or "").lower()
    if status == "failed" and safe_int(row.get("executed_qty")) == 0:
        return True
    return False


def apply_single_repair(
    conn: sqlite3.Connection,
    candidate: Dict[str, Any],
    *,
    now_iso: str,
) -> Optional[Dict[str, Any]]:
    """단일 repair 후보 적용. 반환: applied 요약 dict, skipped dict, 또는 None."""
    row_id = candidate.get("id")
    if row_id is None:
        return _repair_skip("?", "missing_row_id")

    before = _fetch_row_by_id(conn, int(row_id))
    if not before:
        return _repair_skip(row_id, "row_not_found")

    action = str(candidate.get("action") or "")
    ticker = norm_ticker(before.get("ticker"))
    qty = row_executed_qty(before)

    if action == "mark_failed_sell_executed":
        if not action_is_sell(before):
            return _repair_skip(row_id, "not_sell")
        if _is_already_repaired_sell(before):
            return _repair_skip(row_id, "already_repaired")
        if str(before.get("order_status") or "").lower() != "failed":
            return _repair_skip(row_id, "status_not_failed")

        target_qty = safe_int(before.get("quantity") or before.get("requested_qty") or qty)
        new_reason = append_reason_code(before.get("reason_code") or "", REPAIR_TAG_SELL_EXECUTED)
        conn.execute(
            """
            UPDATE trade_records
            SET order_status = 'executed',
                executed_qty = ?,
                reason_code = ?,
                last_status_update_ts = ?
            WHERE id = ?
            """,
            (target_qty, new_reason, now_iso, int(row_id)),
        )
        after = {
            **before,
            "order_status": "executed",
            "executed_qty": target_qty,
            "reason_code": new_reason,
        }
        return {
            "status": "applied",
            "id": row_id,
            "ticker": ticker,
            "action": action,
            "before": before,
            "after": after,
        }

    if action == "mark_empty_order_buy_failed":
        if not action_is_buy(before):
            return _repair_skip(row_id, "not_buy")
        if _is_already_repaired_buy_failed(before):
            return _repair_skip(row_id, "already_repaired")
        if str(before.get("order_status") or "").lower() not in OPEN_POSITION_STATUSES:
            return _repair_skip(row_id, "status_not_executed")
        if str(before.get("order_id") or "").strip():
            return _repair_skip(row_id, "order_id_present")

        new_reason = append_reason_code(before.get("reason_code") or "", REPAIR_TAG_BUY_FAILED)
        conn.execute(
            """
            UPDATE trade_records
            SET order_status = 'failed',
                executed_qty = 0,
                reason_code = ?,
                last_status_update_ts = ?
            WHERE id = ?
            """,
            (new_reason, now_iso, int(row_id)),
        )
        after = {
            **before,
            "order_status": "failed",
            "executed_qty": 0,
            "reason_code": new_reason,
        }
        return {
            "status": "applied",
            "id": row_id,
            "ticker": ticker,
            "action": action,
            "before": before,
            "after": after,
        }

    return _repair_skip(row_id, f"unknown_action={action}")


def apply_repair_candidates_to_db(
    db_path: str,
    candidates: List[Dict[str, Any]],
    *,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """repair 후보를 DB에 적용. paper_executed duplicate는 적용하지 않음."""
    from datetime import datetime
    from utils import KST

    applicable = filter_repair_candidates_for_apply(candidates)
    now_iso = now_iso or datetime.now(KST).isoformat()
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    if not applicable:
        return {"applied": applied, "skipped": skipped, "applied_count": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for candidate in applicable:
            result = apply_single_repair(conn, candidate, now_iso=now_iso)
            if result and result.get("status") == "applied":
                applied.append(result)
                before = result.get("before") or {}
                after = result.get("after") or {}
                print(
                    f"[POSITION_REPAIR_APPLY] id={result.get('id')} "
                    f"before_status={before.get('order_status')} "
                    f"after_status={after.get('order_status')} "
                    f"before_qty={before.get('executed_qty')} "
                    f"after_qty={after.get('executed_qty')}"
                )
            elif result:
                skipped.append(result)
            else:
                skipped.append({"id": candidate.get("id"), "reason": "unknown_skip"})
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[POSITION_REPAIR_ROLLBACK] error={e}")
        logger.exception("position repair rollback: %s", e)
        raise
    finally:
        conn.close()

    return {"applied": applied, "skipped": skipped, "applied_count": len(applied)}
