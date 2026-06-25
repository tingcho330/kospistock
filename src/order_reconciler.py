#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Order reconciler

- DB(trade_records)의 pending/partial 레코드를 KIS 당일 주문 조회로 재검증하여
  executed/partial/pending/cancelled 상태를 최신화한다.
- B안: 기존 구조 유지 + 정기 리컨실로 pending 누적 방지
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from settings import settings
from utils import setup_logging, KST, get_account_snapshot_cached
from api.kis_auth import KIS
from recorder import get_recorder, log_trade_record_classify

try:
    from db_debug import log as _db_dbg_log, log_skip as _db_dbg_skip, is_enabled as _db_dbg_enabled
except ImportError:
    def _db_dbg_log(*args, **kwargs):
        pass
    def _db_dbg_skip(*args, **kwargs):
        pass
    def _db_dbg_enabled():
        return False


logger = logging.getLogger("OrderReconciler")

def _mask_account(s: Optional[str]) -> str:
    """계좌/식별자 로그 마스킹(끝 2~3자리만 노출)."""
    if not s:
        return "N/A"
    t = str(s).strip()
    if len(t) <= 3:
        return "***"
    return ("*" * (len(t) - 3)) + t[-3:]


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _normalize_order_row(row: Any) -> Optional[Dict[str, Any]]:
    """KIS 주문 row(Series/dict)를 내부 표준 dict로 정규화."""
    try:
        order_id = str(row.get("odno", "") or "").strip()
        if not order_id:
            return None
        qty = _safe_int(row.get("ord_qty", 0))
        executed_qty = _safe_int(row.get("tot_ccld_qty", 0))
        ticker = str(row.get("pdno", "") or "").zfill(6)
        side = "buy" if str(row.get("sll_buy_dvsn_cd", "")) == "02" else "sell"
        order_time = str(row.get("ord_tmd", "") or "")
        # 취소 여부(주식일별주문체결조회 output1.cncl_yn: Y/N)
        cancelled = str(row.get("cncl_yn", "") or "").strip().upper() == "Y"
        avg_raw = row.get("avg_prvs", row.get("ord_unpr", 0))
        try:
            avg_price = int(float(avg_raw or 0))
        except Exception:
            avg_price = 0
        estimated_fees = _safe_int(row.get("prsm_tlex_smtl", 0))

        if executed_qty <= 0 and cancelled:
            status = "cancelled"
        elif executed_qty <= 0:
            status = "pending"
        elif qty > 0 and executed_qty < qty:
            status = "partial"
        elif qty > 0 and executed_qty >= qty:
            status = "executed"
        else:
            status = "partial"

        return {
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "quantity": qty,
            "executed_qty": executed_qty,
            "status": status,
            "cancelled": cancelled,
            "order_time": order_time,
            "avg_price": avg_price,
            "estimated_fees": estimated_fees,
        }
    except Exception:
        return None


def _fetch_open_orders_inquire_orders(
    kis: KIS, *, start_ymd: Optional[str] = None, end_ymd: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    KIS inquire_orders(미체결/부분체결) 조회 결과를 order_id 기준으로 정규화해서 반환.
    반환: {order_id: {order_id,ticker,side,quantity,executed_qty,status,order_time}}

    - start_ymd/end_ymd로 조회 창을 지정한다. 미지정 시 오늘 하루로 폴백한다.
      (전일 이월 pending 주문을 잡으려면 reconcile 대상 행 범위와 동일하게 넘겨야 한다)
    """
    orders: List[Dict[str, Any]] = []
    today = datetime.now(KST).strftime("%Y%m%d")
    strt = start_ymd or today
    end = end_ymd or today

    # 1) inquire_orders() 기반(open orders) 우선
    # - 이 API는 "미체결/부분체결" 중심이라 executed/cancelled 확정은 보완이 필요할 수 있다.
    try:
        df = None
        if hasattr(kis, "inquire_orders"):
            df = kis.inquire_orders(
                inqr_dvsn="00",
                inqr_strt_ymd=strt,
                inqr_end_ymd=end,
                sll_buy_dvsn_cd="00",
                inqr_dvsn_cd="00",
            )
        elif hasattr(kis, "get_pending_orders"):
            # 하위 호환
            df = kis.get_pending_orders()

        if df is not None and not df.empty:
            for _, row in df.iterrows():
                o = _normalize_order_row(row)
                if o:
                    orders.append(o)
    except Exception as e:
        logger.debug(f"inquire_orders() 조회 실패: {e}")

    # 마지막 값으로 덮어쓰기(시간 역순/중복 가능)
    by_id: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        by_id[o["order_id"]] = o
    _db_dbg_log(
        "reconciler.fetch_today_orders.OK",
        inqr_strt_ymd=strt,
        inqr_end_ymd=end,
        raw_rows=len(orders),
        unique_order_ids=len(by_id),
        sample_ids=list(by_id.keys())[:8],
    )
    return by_id


def _fetch_daily_orders(
    kis: KIS, *, start_ymd: str, end_ymd: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    KIS inquire_daily_order(일자별 주문 전체) 결과를 order_id 기준으로 정규화해서 반환.
    - executed/cancelled 확정 보완용(누락 order_id가 있을 때만 호출 권장)
    - start_ymd~end_ymd 범위로 조회한다(전일 이월 주문 포함).
    """
    orders: List[Dict[str, Any]] = []
    end = end_ymd or start_ymd
    try:
        df = kis.inquire_daily_order(
            cano=kis.cano,
            acnt_prdt_cd=kis.acnt_prdt_cd,
            inqr_strt_dt=start_ymd,
            inqr_end_dt=end,
            sll_buy_dvsn_cd="00",
            inqr_dvsn="00",
            sort_ord="2",
            ord_gnno_yn="N",
            odno="",
            inqr_dvsn_3="00",
            inqr_dvsn_1="",
            tot_ccld_qty_smtl_yn="N",
        )
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                o = _normalize_order_row(row)
                if o:
                    orders.append(o)
    except Exception as e:
        logger.warning(f"inquire_daily_order() 조회 실패: {e}")

    by_id: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        by_id[o["order_id"]] = o
    return by_id


def _db_action_to_kis_side(action: str) -> str:
    """trade_records.action → KIS sll_buy_dvsn_cd 기준 side ('buy'/'sell')."""
    a = str(action or "").strip().upper()
    if a in ("BUY", "B"):
        return "buy"
    if a in ("SELL", "S"):
        return "sell"
    return ""


def _row_target_qty(row: Dict[str, Any]) -> int:
    rq = _safe_int(row.get("requested_qty", 0))
    if rq > 0:
        return rq
    return _safe_int(row.get("quantity", 0))


def _load_holdings_by_ticker() -> Dict[str, Dict[str, Any]]:
    """최신 balance 스냅샷 → ticker → holding row."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        _, balance_list, _, _ = get_account_snapshot_cached()
        for h in balance_list or []:
            if not isinstance(h, dict):
                continue
            t = str(h.get("pdno", h.get("ticker", "")) or "").zfill(6)
            if t and t != "000000":
                out[t] = h
    except Exception as e:
        logger.warning("balance 스냅샷 로드 실패(holding fallback 비활성): %s", e)
    return out


def _holding_fill_price(holding: Dict[str, Any]) -> Optional[int]:
    """평균매입가 또는 pchs_amt/hldg_qty."""
    avg = _safe_int(holding.get("pchs_avg_pric", 0))
    if avg > 0:
        return avg
    qty = _safe_int(holding.get("hldg_qty", 0))
    amt = _safe_int(holding.get("pchs_amt", 0))
    if qty > 0 and amt > 0:
        return int(amt / qty)
    prpr = _safe_int(holding.get("prpr", 0))
    return prpr if prpr > 0 else None


def _try_reconcile_buy_by_holding(
    row: Dict[str, Any],
    holdings_by_ticker: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    KIS 주문조회 miss 시 BUY pending을 계좌 잔고로 executed 판정.
    SELL은 별도 로직(미구현) — None 반환.
    """
    action = str(row.get("action") or row.get("side") or "").strip().upper()
    if action not in ("BUY", "B"):
        return None

    ticker = str(row.get("ticker") or "").zfill(6)
    requested_qty = _row_target_qty(row)
    if not ticker or requested_qty <= 0:
        return None

    holding = holdings_by_ticker.get(ticker)
    if not holding:
        return None

    hldg_qty = _safe_int(holding.get("hldg_qty", 0))
    thdt_buyqty = _safe_int(holding.get("thdt_buyqty", 0))
    if hldg_qty < requested_qty and thdt_buyqty < requested_qty:
        return None

    fill_price = _holding_fill_price(holding)
    return {
        "status": "executed",
        "executed_qty": requested_qty,
        "avg_price": fill_price or 0,
        "holding_qty": hldg_qty,
        "thdt_buyqty": thdt_buyqty,
    }


def _apply_reconcile_update(
    recorder,
    *,
    order_id: str,
    row: Dict[str, Any],
    new_status: str,
    executed_qty: int,
    price: Optional[int],
    reconciled_price_source: Optional[str] = None,
) -> int:
    amount: Optional[int] = None
    ctx_updates: Optional[Dict[str, Any]] = None
    original_price = _safe_int(row.get("price", 0))
    if price and executed_qty > 0:
        amount = int(price) * int(executed_qty)
        if reconciled_price_source and (
            original_price != int(price)
            or _safe_int(row.get("amount", 0)) != amount
        ):
            ctx_updates = {
                "original_order_price": original_price if original_price > 0 else None,
                "reconciled_price_source": reconciled_price_source,
                "reconciled_price": int(price),
                "reconciled_amount": amount,
            }
    return recorder.update_order_status(
        order_id=order_id,
        order_status=new_status,
        executed_qty=executed_qty,
        price=price if price and price > 0 else None,
        amount=float(amount) if amount is not None else None,
        structured_context=json.dumps(ctx_updates, ensure_ascii=False) if ctx_updates else None,
        merge_context=bool(ctx_updates),
    )


def _is_already_reconciled(
    row: Dict[str, Any],
    *,
    new_status: str,
    executed_qty: int,
    price: Optional[int],
) -> bool:
    """이미 executed이고 체결 수량·가격이 일치하면 재갱신 생략."""
    db_status = str(row.get("order_status") or "").lower()
    if db_status not in ("executed", "completed"):
        return False
    db_exe = _safe_int(row.get("executed_qty", 0))
    req = _row_target_qty(row)
    target_exe = executed_qty if executed_qty > 0 else req
    if db_exe < target_exe:
        return False
    if price and price > 0:
        db_price = _safe_int(row.get("price", 0))
        db_amount = _safe_int(row.get("amount", 0))
        expected_amount = int(price) * target_exe
        if db_price == int(price) and (db_amount == expected_amount or db_amount == 0):
            return True
        return False
    return db_exe >= target_exe


def _match_kis_candidates(
    row: Dict[str, Any],
    daily_orders: Dict[str, Dict[str, Any]],
    *,
    used_order_ids: set,
) -> List[Dict[str, Any]]:
    """DB orphan 행에 대응하는 KIS daily 주문 후보(0~N)."""
    ticker = str(row.get("ticker") or "").zfill(6)
    side = _db_action_to_kis_side(row.get("action") or "")
    target_qty = _row_target_qty(row)
    if not ticker or not side or target_qty <= 0:
        return []

    row_ts = str(row.get("timestamp") or "")
    row_date = row_ts[:10].replace("-", "") if row_ts else ""

    candidates: List[Dict[str, Any]] = []
    for oid, kis_o in daily_orders.items():
        if oid in used_order_ids:
            continue
        if str(kis_o.get("ticker") or "").zfill(6) != ticker:
            continue
        if str(kis_o.get("side") or "") != side:
            continue
        kis_qty = _safe_int(kis_o.get("quantity", 0))
        if kis_qty != target_qty:
            continue
        kis_exe = _safe_int(kis_o.get("executed_qty", 0))
        db_status = str(row.get("order_status") or "").lower()
        if db_status in ("executed", "completed", "partial") and kis_exe <= 0:
            continue
        if db_status in ("pending", "partial") and kis_exe <= 0 and not kis_o.get("cancelled"):
            pass  # 미체결 매칭 허용
        elif db_status in ("executed", "completed") and kis_exe < target_qty:
            continue
        if row_date and kis_o.get("order_time"):
            # ord_tmd는 HHMMSS — 날짜는 조회 창(since_hours)으로 이미 제한
            pass
        candidates.append(kis_o)
    return candidates


def backfill_orphan_order_ids(*, since_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """
    order_id가 비어 있는 DB 행을 KIS 일자별 주문과 매칭해 backfill.
    유일 매칭(1건)일 때만 UPDATE.
    """
    setup_logging()
    try:
        return _backfill_orphan_order_ids_impl(since_hours=since_hours, limit=limit)
    except Exception as e:
        logger.exception("orphan backfill 실패: %s", e)
        return {
            "backfill_orphans": 0,
            "backfill_updated": 0,
            "backfill_skipped_ambiguous": 0,
            "backfill_skipped_no_match": 0,
            "backfill_error": str(e),
        }


def _backfill_orphan_order_ids_impl(*, since_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """
    order_id가 비어 있는 DB 행을 KIS 일자별 주문과 매칭해 backfill.
    유일 매칭(1건)일 때만 UPDATE.
    """
    setup_logging()
    env = settings._config.get("trading_environment", "vps")
    kis = KIS(env=env)
    recorder = get_recorder()

    now_kst = datetime.now(KST)
    since_dt = now_kst - timedelta(hours=since_hours)
    since_ts = since_dt.isoformat()
    start_ymd = since_dt.strftime("%Y%m%d")
    end_ymd = now_kst.strftime("%Y%m%d")

    orphans = recorder.get_orphan_trade_records(since_ts=since_ts, limit=limit)
    logger.info(f"orphan backfill 대상 {len(orphans)}건 (since={since_ts})")

    if not orphans:
        logger.info("[OrderReconciler] orphan backfill skip: no orphan records")
        _db_dbg_skip(
            "reconciler.backfill.SKIP",
            reason="no orphan records",
            phase="backfill",
            since_hours=since_hours,
            limit=limit,
            orphan_count=0,
            daily_order_count=0,
        )
        return {
            "backfill_orphans": 0,
            "backfill_updated": 0,
            "backfill_skipped_ambiguous": 0,
            "backfill_skipped_no_match": 0,
            "kis_daily_orders": 0,
        }

    daily_orders = _fetch_daily_orders(kis, start_ymd=start_ymd, end_ymd=end_ymd)
    logger.info(f"KIS 일자별 주문(daily) backfill 조회: {len(daily_orders)}건 ({start_ymd}~{end_ymd})")

    if not daily_orders:
        logger.info("[OrderReconciler] orphan backfill skip: no daily orders found")
        _db_dbg_skip(
            "reconciler.backfill.SKIP",
            reason="no daily orders found",
            phase="backfill",
            since_hours=since_hours,
            limit=limit,
            orphan_count=len(orphans),
            daily_order_count=0,
        )
        return {
            "backfill_orphans": len(orphans),
            "backfill_updated": 0,
            "backfill_skipped_ambiguous": 0,
            "backfill_skipped_no_match": 0,
            "kis_daily_orders": 0,
        }

    updated = 0
    skipped_ambiguous = 0
    skipped_no_match = 0
    used_order_ids = set()

    for row in orphans:
        row_id = row.get("id")
        candidates = _match_kis_candidates(row, daily_orders, used_order_ids=used_order_ids)
        if len(candidates) == 0:
            skipped_no_match += 1
            _db_dbg_skip(
                "reconciler.backfill.NO_MATCH",
                reason="no KIS candidate for orphan row",
                row_id=row_id,
                ticker=row.get("ticker"),
                action=row.get("action"),
                quantity=row.get("quantity"),
            )
            continue
        if len(candidates) > 1:
            skipped_ambiguous += 1
            _db_dbg_skip(
                "reconciler.backfill.AMBIGUOUS",
                reason="multiple KIS candidates for orphan row",
                row_id=row_id,
                ticker=row.get("ticker"),
                candidate_order_ids=[c.get("order_id") for c in candidates[:5]],
                candidate_count=len(candidates),
            )
            continue

        kis_o = candidates[0]
        order_id = str(kis_o.get("order_id") or "").strip()
        kis_exe = _safe_int(kis_o.get("executed_qty", 0))
        kis_status = str(kis_o.get("status") or "executed")
        if kis_status == "executed" and kis_exe <= 0:
            kis_exe = _row_target_qty(row)

        n = recorder.backfill_order_id(
            row_id=int(row_id),
            order_id=order_id,
            executed_qty=kis_exe if kis_exe > 0 else None,
            order_status=kis_status,
        )
        if n:
            updated += n
            used_order_ids.add(order_id)
            _db_dbg_log(
                "reconciler.backfill.OK",
                row_id=row_id,
                order_id=order_id,
                ticker=row.get("ticker"),
                kis_status=kis_status,
                kis_exe=kis_exe,
            )

    summary = {
        "backfill_orphans": len(orphans),
        "backfill_updated": updated,
        "backfill_skipped_ambiguous": skipped_ambiguous,
        "backfill_skipped_no_match": skipped_no_match,
        "kis_daily_orders": len(daily_orders),
    }
    logger.info(f"orphan backfill 결과: {summary}")
    _db_dbg_log("reconciler.backfill.done", **summary)
    return summary


def reconcile_open_orders(*, since_hours: int = 24, limit: int = 500) -> Dict[str, int]:
    """
    DB open(pending/partial) 주문을 KIS 조회 결과로 리컨실.
    """
    setup_logging()
    logger.setLevel(logging.INFO)

    env = settings._config.get("trading_environment", "vps")
    kis = KIS(env=env)
    recorder = get_recorder()

    now_kst = datetime.now(KST)
    since_dt = now_kst - timedelta(hours=since_hours)
    since_ts = since_dt.isoformat()
    # KIS 조회 창: reconcile 대상(DB) 행 범위와 동일하게 since_hours 시작일 ~ 오늘.
    # (오늘 하루로 고정하면 전일 이월 pending 주문이 영원히 해소되지 않음)
    start_ymd = since_dt.strftime("%Y%m%d")
    end_ymd = now_kst.strftime("%Y%m%d")
    _db_dbg_log(
        "reconciler.start",
        since_hours=since_hours,
        since_ts=since_ts,
        kis_inqr_strt_ymd=start_ymd,
        kis_inqr_end_ymd=end_ymd,
        limit=limit,
        env=env,
        kis_cano=_mask_account(getattr(kis, "cano", None)),
        kis_acnt_prdt_cd=str(getattr(kis, "acnt_prdt_cd", "") or ""),
        kis_url_base=str(getattr(kis, "url_base", "") or "")[:80],
    )
    if _db_dbg_enabled():
        try:
            import sqlite3
            with sqlite3.connect(recorder.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT COUNT(*) FROM trade_records
                    WHERE timestamp >= ? AND lower(order_status) IN ('pending','partial')
                      AND (order_id IS NULL OR order_id = '')
                    """,
                    (since_ts,),
                )
                orphan = int(cur.fetchone()[0] or 0)
            _db_dbg_log(
                "reconciler.orphan_pending_without_order_id",
                count=orphan,
                note="these rows are invisible to get_open_orders / reconciler loop",
            )
        except Exception as e:
            _db_dbg_log("reconciler.orphan_count.FAIL", error=str(e))
    open_rows = recorder.get_open_orders(statuses=("pending", "partial"), since_ts=since_ts, limit=limit)
    logger.info(f"리컨실 대상(open) {len(open_rows)}건 (since={since_ts})")
    try:
        recent_buys = recorder.get_trade_records(start_date=since_dt, action="BUY")
        for t in recent_buys[:30]:
            log_trade_record_classify(t, trading_environment=env)
    except Exception as e:
        logger.debug("최근 BUY 분류 로그 스킵: %s", e)
    for row in open_rows[:20]:
        _row_obj = type("_TradeRow", (), {
            "ticker": row.get("ticker"),
            "order_status": row.get("order_status"),
            "order_id": row.get("order_id"),
            "reason_code": "",
            "structured_context": row.get("structured_context", ""),
        })()
        log_trade_record_classify(
            _row_obj, record_id=row.get("id"), trading_environment=env,
        )

    today = end_ymd
    open_orders = _fetch_open_orders_inquire_orders(kis, start_ymd=start_ymd, end_ymd=end_ymd)
    logger.info(
        f"KIS 미체결/부분체결(inquire_orders) 조회: {len(open_orders)}건 "
        f"({start_ymd}~{end_ymd})"
    )
    daily_orders: Optional[Dict[str, Dict[str, Any]]] = None
    holdings_by_ticker = _load_holdings_by_ticker()
    logger.info("holding fallback용 잔고 종목: %d건", len(holdings_by_ticker))

    updated = 0
    to_executed = 0
    to_partial = 0
    to_pending = 0
    to_cancelled = 0
    skipped_no_order_id = 0
    skipped_kis_miss = 0
    updated_by_order_query = 0
    updated_by_daily_query = 0
    updated_by_holding_fallback = 0
    still_missing_after_all = 0

    for r in open_rows:
        order_id = str(r.get("order_id") or "").strip()
        if not order_id:
            skipped_no_order_id += 1
            _db_dbg_skip(
                "reconciler.loop.SKIP_NO_ORDER_ID",
                reason="get_open_orders only returns rows with order_id; this should not happen",
                row_id=r.get("id"),
                ticker=r.get("ticker"),
            )
            continue

        reconcile_source = None
        kis_o = open_orders.get(order_id)
        if kis_o:
            reconcile_source = "order_query"
        else:
            if daily_orders is None:
                daily_orders = _fetch_daily_orders(kis, start_ymd=start_ymd, end_ymd=end_ymd)
                logger.info(
                    f"KIS 일자별 주문(daily) 보완 조회: {len(daily_orders)}건 "
                    f"({start_ymd}~{end_ymd})"
                )
            kis_o = (daily_orders or {}).get(order_id)
            if kis_o:
                reconcile_source = "daily_query"

        holding_match: Optional[Dict[str, Any]] = None
        if not kis_o:
            skipped_kis_miss += 1
            ticker = str(r.get("ticker") or "").zfill(6)
            logger.warning(
                "KIS 주문조회 miss: order_id=%s ticker=%s db_status=%s — holding fallback 시도",
                order_id, ticker, r.get("order_status"),
            )
            holding_match = _try_reconcile_buy_by_holding(r, holdings_by_ticker)
            if not holding_match:
                still_missing_after_all += 1
                _db_dbg_skip(
                    "reconciler.loop.SKIP_KIS_MISS_AFTER_ALL",
                    reason="order_id not in KIS queries and holding fallback failed",
                    order_id=order_id,
                    ticker=r.get("ticker"),
                    db_status=r.get("order_status"),
                )
                _db_dbg_log(
                    "reconciler.miss_after_all.DIAG",
                    hint="KIS miss + holding fallback failed",
                    env=env,
                    order_id=order_id,
                    db_row_id=r.get("id"),
                    db_ticker=r.get("ticker"),
                    db_requested_qty=r.get("requested_qty"),
                )
                continue
            reconcile_source = "holding_fallback"

        if reconcile_source == "holding_fallback":
            assert holding_match is not None
            new_status = str(holding_match.get("status") or "executed")
            kis_exe = _safe_int(holding_match.get("executed_qty", 0))
            fill_price = _safe_int(holding_match.get("avg_price", 0))
            if _is_already_reconciled(
                r, new_status=new_status, executed_qty=kis_exe, price=fill_price or None
            ):
                _db_dbg_skip(
                    "reconciler.loop.SKIP_ALREADY_EXECUTED",
                    reason="holding fallback but row already reconciled",
                    order_id=order_id,
                    ticker=r.get("ticker"),
                )
                continue
            logger.info(
                "[RECONCILE_BY_HOLDING] order_id=%s ticker=%s requested_qty=%s "
                "holding_qty=%s thdt_buyqty=%s status=%s",
                order_id,
                str(r.get("ticker") or "").zfill(6),
                _row_target_qty(r),
                holding_match.get("holding_qty"),
                holding_match.get("thdt_buyqty"),
                new_status,
            )
            n = _apply_reconcile_update(
                recorder,
                order_id=order_id,
                row=r,
                new_status=new_status,
                executed_qty=kis_exe,
                price=fill_price if fill_price > 0 else None,
                reconciled_price_source="holding_fallback_pchs_avg_pric",
            )
            if n:
                updated += n
                updated_by_holding_fallback += n
                if new_status == "executed":
                    to_executed += n
            continue

        requested_qty = _safe_int(r.get("requested_qty", 0))
        kis_qty = _safe_int(kis_o.get("quantity", 0))
        kis_exe = _safe_int(kis_o.get("executed_qty", 0))
        if requested_qty <= 0 and kis_qty > 0:
            requested_qty = kis_qty

        if kis_exe <= 0 and kis_o.get("cancelled"):
            new_status = "cancelled"
        elif kis_exe <= 0:
            new_status = "pending"
        elif requested_qty > 0 and kis_exe < requested_qty:
            new_status = "partial"
        else:
            new_status = "executed"

        fill_price_kis = _safe_int(kis_o.get("avg_price", 0)) or None
        if _is_already_reconciled(
            r, new_status=new_status, executed_qty=kis_exe, price=fill_price_kis
        ):
            _db_dbg_skip(
                "reconciler.loop.SKIP_ALREADY_EXECUTED",
                reason="KIS match but row already reconciled",
                order_id=order_id,
                ticker=r.get("ticker"),
            )
            continue

        _db_dbg_log(
            "reconciler.loop.MATCH",
            order_id=order_id,
            ticker=r.get("ticker"),
            db_status=r.get("order_status"),
            new_status=new_status,
            requested_qty=requested_qty,
            kis_exe=kis_exe,
            reconcile_source=reconcile_source,
        )
        n = _apply_reconcile_update(
            recorder,
            order_id=order_id,
            row=r,
            new_status=new_status,
            executed_qty=kis_exe,
            price=fill_price_kis,
            reconciled_price_source=reconcile_source,
        )
        if n:
            updated += n
            if reconcile_source == "order_query":
                updated_by_order_query += n
            elif reconcile_source == "daily_query":
                updated_by_daily_query += n
            if new_status == "executed":
                to_executed += n
            elif new_status == "partial":
                to_partial += n
            elif new_status == "cancelled":
                to_cancelled += n
            else:
                to_pending += n

    summary = {
        "updated": updated,
        "to_executed": to_executed,
        "to_partial": to_partial,
        "to_pending": to_pending,
        "to_cancelled": to_cancelled,
        "skipped_no_order_id": skipped_no_order_id,
        "skipped_kis_miss": skipped_kis_miss,
        "open_rows_with_order_id": len(open_rows),
        "kis_open_orders_inquire_orders": len(open_orders),
        "kis_daily_orders_fetched": 0 if daily_orders is None else len(daily_orders),
        "holdings_loaded": len(holdings_by_ticker),
        "updated_by_order_query": updated_by_order_query,
        "updated_by_daily_query": updated_by_daily_query,
        "updated_by_holding_fallback": updated_by_holding_fallback,
        "still_missing_after_all": still_missing_after_all,
        # legacy keys
        "resolved_by_daily": updated_by_daily_query,
        "still_missing_after_daily": still_missing_after_all,
    }
    logger.info(f"리컨실 결과: {summary}")
    _db_dbg_log("reconciler.done", **summary)
    if still_missing_after_all > 0:
        logger.warning(
            "리컨실 미해결 주문이 있습니다(still_missing_after_all=%s). "
            "env/계좌/날짜경계/DB order_id 저장 여부를 점검하세요.",
            still_missing_after_all,
        )
    if _db_dbg_enabled() and skipped_kis_miss == 0 and updated == 0 and open_rows:
        _db_dbg_skip(
            "reconciler.HINT_ORPHAN_PENDING",
            reason="open_rows>0 but updated=0; check recorder.get_open_orders orphan_pending_no_order_id count",
        )

    backfill_summary = backfill_orphan_order_ids(since_hours=since_hours, limit=min(limit, 200))
    summary.update(backfill_summary)
    logger.info(f"리컨실+backfill 통합 결과: {summary}")
    _db_dbg_log("reconciler.done_with_backfill", **summary)
    return summary


def main():
    import sys
    import json

    parser = argparse.ArgumentParser(description="DB pending/partial 주문 리컨실")
    parser.add_argument("--since-hours", type=int, default=24, help="리컨실 대상 조회 범위(시간)")
    parser.add_argument("--limit", type=int, default=500, help="최대 조회 건수")
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="order_id orphan backfill만 실행 (open pending 리컨실 생략)",
    )
    args = parser.parse_args()

    try:
        if args.backfill_only:
            summary = backfill_orphan_order_ids(since_hours=args.since_hours, limit=min(args.limit, 200))
        else:
            summary = reconcile_open_orders(since_hours=args.since_hours, limit=args.limit)
        print(json.dumps({"ok": True, **summary}, ensure_ascii=False))
        sys.exit(0)
    except Exception as e:
        logger.exception("order_reconciler 실패: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

