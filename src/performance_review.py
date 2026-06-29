#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
일별 전략·시스템 성능 리뷰 자동 생성.

Usage:
  PYTHONPATH=/app/src python /app/src/performance_review.py --date 20260625 --market KOSPI
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import (
    KST,
    OUTPUT_DIR,
    extract_kis_official_summary,
    load_config,
    setup_logging,
)
from settings import settings

logger = logging.getLogger("PerformanceReview")

GPT_ACTION_MAP = {
    "매수": "BUY",
    "보류": "HOLD",
    "미진입": "REJECT",
}
TRACK_HORIZONS = (1, 3, 5, 10)


def _norm_date(s: Optional[str]) -> str:
    if not s:
        return datetime.now(KST).strftime("%Y%m%d")
    t = re.sub(r"\D", "", str(s))
    if len(t) != 8:
        raise ValueError(f"Invalid date: {s!r} (expected YYYYMMDD)")
    return t


def _norm_ticker(t: Any) -> str:
    return str(t or "").strip().zfill(6)


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("JSON 로드 실패 %s: %s", path, e)
        return None


def _artifact_path(name: str, date_str: str, market: str) -> Path:
    if name in ("balance", "summary", "summary_rlz"):
        return OUTPUT_DIR / f"{name}_{date_str}.json"
    return OUTPUT_DIR / f"{name}_{date_str}_{market}.json"


def _parse_balance_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []
    if isinstance(data, list):
        return data
    return []


def _parse_summary_row(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        rows = data.get("data") or []
        if rows and isinstance(rows[0], dict):
            return rows[0]
    return {}


def _gpt_action(plan: Dict[str, Any]) -> str:
    raw = str(plan.get("결정") or plan.get("decision") or "").strip()
    return GPT_ACTION_MAP.get(raw, raw.upper() or "UNKNOWN")


def _plan_ticker(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return _norm_ticker(info.get("Ticker") or info.get("ticker") or plan.get("ticker"))


def _plan_name(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return str(info.get("Name") or info.get("name") or plan.get("name") or "")


def _plan_score(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    for key in ("ConvictionScore", "Score", "score", "conviction_score"):
        val = _safe_float(info.get(key))
        if val is not None:
            return val
    return _safe_float(plan.get("gpt_score"))


def _plan_price(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    return _safe_float(info.get("Price") or info.get("price"))


def _parse_structured_context(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _infer_reconcile_method(row: Dict[str, Any]) -> str:
    ctx = _parse_structured_context(row.get("structured_context"))
    src = str(ctx.get("reconciled_price_source") or ctx.get("reconcile_source") or "").lower()
    if "holding" in src:
        return "holding_fallback"
    if src in ("order_query", "daily_query", "manual"):
        return src
    if ctx.get("reconcile_method"):
        return str(ctx["reconcile_method"])
    order_id = str(row.get("order_id") or "").strip()
    status = str(row.get("order_status") or "").lower()
    if order_id and status in ("executed", "partial", "pending"):
        return "order_query"
    if status == "executed" and not order_id:
        return "manual"
    return "unknown"


def _find_log_files(date_str: str) -> List[Path]:
    patterns = [
        "pipeline_*.log",
        "step_*.log",
        "trader_run.log",
        "integrated_manager.log",
        "*.log",
    ]
    found: List[Path] = []
    seen = set()
    for pat in patterns:
        for p in OUTPUT_DIR.glob(pat):
            if p in seen:
                continue
            seen.add(p)
            found.append(p)
    debug_dir = OUTPUT_DIR / "debug"
    if debug_dir.is_dir():
        for p in debug_dir.glob("*.log"):
            if p not in seen:
                seen.add(p)
                found.append(p)

    relevant: List[Tuple[float, Path]] = []
    for p in found:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if date_str in text or date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:] in text:
            relevant.append((p.stat().st_mtime, p))
    relevant.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in relevant]


def _parse_funnel_from_logs(log_text: str) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {
        "total_universe_count": None,
        "marcap_pass_count": None,
        "amount5d_pass_count": None,
    }
    for line in log_text.splitlines():
        m = re.search(r"│\s+(.+?)\s+(\d+)\s", line)
        if not m:
            continue
        label, cnt = m.group(1).strip(), int(m.group(2))
        if "전체 종목" in label:
            out["total_universe_count"] = cnt
        elif label.startswith("Marcap"):
            out["marcap_pass_count"] = cnt
        elif label.startswith("Amount5D"):
            out["amount5d_pass_count"] = cnt
    return out


def _parse_budget_usage_from_logs(log_text: str) -> Dict[str, Any]:
    m = re.search(
        r"\[STOCK_BUDGET_USAGE\]\s+total_assets=(\S+)\s+stock_target_weight=([\d.]+)\s+"
        r"current_stock_value=(\S+)\s+stock_buy_budget=(\S+)\s+gpt_plan_count=(\S+)\s+"
        r"gpt_buy_count=(\S+)\s+final_buy_count=(\S+)\s+planned_order_amount=(\S+)\s+"
        r"executed_order_amount=(\S+)\s+unused_stock_budget=(\S+)\s+unused_reason=(\S+)",
        log_text,
    )
    if not m:
        return {}
    total_assets = _safe_int(m.group(1))
    stock_target_weight = _safe_float(m.group(2)) or 0.0
    current_stock_value = _safe_int(m.group(3))
    stock_buy_budget = _safe_int(m.group(4))
    executed_amount = _safe_int(m.group(9))
    unused_budget = _safe_int(m.group(10))
    usage_rate = (executed_amount / stock_buy_budget) if stock_buy_budget > 0 else None
    return {
        "total_assets": total_assets,
        "stock_target_weight": stock_target_weight,
        "current_stock_value": current_stock_value,
        "stock_buy_budget": stock_buy_budget,
        "gpt_plan_count": _safe_int(m.group(5)),
        "gpt_buy_count": _safe_int(m.group(6)),
        "final_buy_count": _safe_int(m.group(7)),
        "planned_order_amount": _safe_int(m.group(8)),
        "actual_stock_buy_amount": executed_amount,
        "stock_budget_usage_rate": round(usage_rate, 4) if usage_rate is not None else None,
        "unused_stock_budget": unused_budget,
        "unused_reason": m.group(11),
        "source": "trader_log",
    }


def _parse_system_from_logs(log_text: str) -> Dict[str, Any]:
    step_times: Dict[str, float] = {}
    for m in re.finditer(
        r"STEP OK:\s+([\w_.]+)\s+\|\s+([\d.]+)s",
        log_text,
    ):
        step_times[m.group(1)] = float(m.group(2))

    script_map = {
        "health_check.py": "health_check_runtime_sec",
        "news_collector.py": "news_collector_runtime_sec",
        "gpt_analyzer.py": "gpt_analyzer_runtime_sec",
        "trader.py": "trader_runtime_sec",
        "order_reconciler.py": "order_reconciler_runtime_sec",
        "screener.py": "screener_runtime_sec",
    }
    perf: Dict[str, Any] = {
        "kis_rate_limit_count": len(re.findall(r"EGW00201|rate_limited", log_text, re.I)),
        "egw00201_count": len(re.findall(r"EGW00201", log_text)),
        "kis_retry_count": len(re.findall(r"재시도|retry", log_text, re.I)),
        "traceback_count": len(re.findall(r"Traceback \(most recent call last\)", log_text)),
        "error_count": len(re.findall(r" - ERROR - ", log_text)),
        "warning_count": len(re.findall(r" - WARNING - ", log_text)),
    }
    for script, key in script_map.items():
        if script in step_times:
            perf[key] = step_times[script]
    if step_times:
        perf["total_runtime_sec"] = round(sum(step_times.values()), 2)
    return perf


def _load_pipeline_state(date_str: str) -> Dict[str, Any]:
    path = OUTPUT_DIR / "pipeline_state.json"
    data = _load_json(path)
    if not isinstance(data, dict):
        return {}
    run_id = str(data.get("run_id") or "")
    if not run_id.startswith(date_str):
        return data if data else {}
    return data


def _load_trade_rows(date_str: str) -> List[Dict[str, Any]]:
    db_path = OUTPUT_DIR / "trading_data.db"
    if not db_path.is_file():
        return []
    prefix = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    alt_prefix = date_str
    rows: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, timestamp, ticker, action, quantity, price, amount,
                   order_status, order_id, requested_qty, executed_qty,
                   structured_context, last_status_update_ts
            FROM trade_records
            WHERE timestamp LIKE ? OR timestamp LIKE ?
            ORDER BY timestamp
            """,
            (f"{prefix}%", f"{alt_prefix}%"),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        logger.warning("trade_records 조회 실패: %s", e)
    return rows


def _build_screener_maps(candidates: List[Dict[str, Any]], scores: List[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, float]]:
    rank_map: Dict[str, int] = {}
    score_map: Dict[str, float] = {}
    for i, row in enumerate(candidates or [], 1):
        t = _norm_ticker(row.get("Ticker") or row.get("ticker"))
        if t:
            rank_map[t] = i
            score_map[t] = _safe_float(row.get("Score") or row.get("score_total")) or 0.0
    for row in scores or []:
        t = _norm_ticker(row.get("ticker") or row.get("Ticker"))
        if t and t not in score_map:
            score_map[t] = _safe_float(row.get("score_total") or row.get("Score")) or 0.0
    return rank_map, score_map


def _build_screener_funnel(
    date_str: str,
    market: str,
    candidates: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    log_text: str,
) -> Dict[str, Any]:
    funnel_log = _parse_funnel_from_logs(log_text)
    gpt_buy = sum(1 for p in gpt_plans if _gpt_action(p) == "BUY")
    gpt_hold = sum(1 for p in gpt_plans if _gpt_action(p) == "HOLD")
    gpt_reject = sum(1 for p in gpt_plans if _gpt_action(p) == "REJECT")
    executed_buy = sum(
        1 for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "completed")
        and _safe_int(r.get("executed_qty") or r.get("quantity")) > 0
    )
    final_buy_tickers = {
        _norm_ticker(r.get("ticker"))
        for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "pending", "completed")
    }
    final_buy_tickers.discard("000000")

    funnel = {
        "review_date": date_str,
        "market": market,
        "total_universe_count": funnel_log.get("total_universe_count"),
        "marcap_pass_count": funnel_log.get("marcap_pass_count"),
        "amount5d_pass_count": funnel_log.get("amount5d_pass_count"),
        "final_screener_candidate_count": len(candidates or []),
        "screener_score_count": len(scores or []),
        "gpt_analysis_count": len(gpt_plans),
        "gpt_buy_count": gpt_buy,
        "gpt_hold_count": gpt_hold,
        "gpt_reject_count": gpt_reject,
        "final_buy_count": len(final_buy_tickers),
        "executed_buy_count": executed_buy,
    }

    if funnel["total_universe_count"] is None:
        try:
            from kis_master import load_kis_master
            df = load_kis_master(market, cache_key=date_str)
            if df is not None and not df.empty:
                funnel["total_universe_count"] = len(df)
        except Exception:
            pass

    logger.info(
        "[PERF_SCREENER_FUNNEL] total=%s marcap_pass=%s amount5d_pass=%s "
        "candidates=%s gpt_plans=%s gpt_buy=%s gpt_hold=%s gpt_reject=%s executed_buy=%s",
        funnel["total_universe_count"],
        funnel["marcap_pass_count"],
        funnel["amount5d_pass_count"],
        funnel["final_screener_candidate_count"],
        funnel["gpt_analysis_count"],
        funnel["gpt_buy_count"],
        funnel["gpt_hold_count"],
        funnel["gpt_reject_count"],
        funnel["executed_buy_count"],
    )
    return funnel


def _build_gpt_decision(
    gpt_plans: List[Dict[str, Any]],
    rank_map: Dict[str, int],
    score_map: Dict[str, float],
    trade_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    trade_by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in trade_rows:
        if str(r.get("action", "")).upper() != "BUY":
            continue
        t = _norm_ticker(r.get("ticker"))
        if t:
            trade_by_ticker[t] = r

    rows: List[Dict[str, Any]] = []
    for plan in gpt_plans:
        ticker = _plan_ticker(plan)
        action = _gpt_action(plan)
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        exe_qty = _safe_int(trade.get("executed_qty") or trade.get("quantity"))
        exe_price = _safe_int(trade.get("price"))
        exe_amount = _safe_int(trade.get("amount")) or (exe_qty * exe_price if exe_qty and exe_price else 0)
        rows.append({
            "ticker": ticker,
            "name": _plan_name(plan),
            "gpt_action": action,
            "gpt_score": _plan_score(plan),
            "gpt_reason": (plan.get("분석") or plan.get("analysis") or "")[:500],
            "screener_rank": plan.get("rank") or rank_map.get(ticker),
            "screener_score": score_map.get(ticker),
            "final_decision": action if action != "REJECT" else "REJECT",
            "executed_qty": exe_qty if status in ("executed", "partial", "completed") else 0,
            "executed_amount": exe_amount if status in ("executed", "partial", "completed") else 0,
            "order_status": status or None,
            "reconcile_method": _infer_reconcile_method(trade) if trade else None,
        })

    aggregates: Dict[str, Dict[str, Any]] = {}
    for action in ("BUY", "HOLD", "REJECT"):
        subset = [r for r in rows if r["gpt_action"] == action]
        gpt_scores = [r["gpt_score"] for r in subset if r["gpt_score"] is not None]
        scr_scores = [r["screener_score"] for r in subset if r["screener_score"] is not None]
        executed = [r for r in subset if _safe_int(r.get("executed_qty")) > 0]
        aggregates[action] = {
            "count": len(subset),
            "avg_gpt_score": round(sum(gpt_scores) / len(gpt_scores), 4) if gpt_scores else None,
            "avg_screener_score": round(sum(scr_scores) / len(scr_scores), 4) if scr_scores else None,
            "executed_count": len(executed),
            "executed_amount": sum(_safe_int(r.get("executed_amount")) for r in executed),
        }

    executed_buy = sum(1 for r in rows if r["gpt_action"] == "BUY" and _safe_int(r.get("executed_qty")) > 0)
    logger.info(
        "[PERF_GPT_DECISION] plans=%s buy=%s hold=%s reject=%s executed_buy=%s",
        len(rows),
        aggregates.get("BUY", {}).get("count", 0),
        aggregates.get("HOLD", {}).get("count", 0),
        aggregates.get("REJECT", {}).get("count", 0),
        executed_buy,
    )
    return {"rows": rows, "aggregates": aggregates}


def _build_budget_usage(
    date_str: str,
    log_text: str,
    summary_row: Dict[str, Any],
    balance_rows: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
) -> Dict[str, Any]:
    budget = _parse_budget_usage_from_logs(log_text)
    aa = settings.asset_allocation if hasattr(settings, "asset_allocation") else {}
    if not isinstance(aa, dict):
        aa = {}

    stock_target = _safe_float(aa.get("stock_target_weight")) or 0.70
    bond_target = _safe_float(aa.get("bond_target_weight")) or 0.20
    cash_target = _safe_float(aa.get("cash_target_weight")) or 0.10

    total_assets = _safe_int(summary_row.get("nass_amt") or summary_row.get("tot_evlu_amt"))
    if not total_assets and budget:
        total_assets = _safe_int(budget.get("total_assets"))

    bond_tickers = set()
    for item in aa.get("bond_etfs") or []:
        if isinstance(item, dict) and item.get("ticker"):
            bond_tickers.add(_norm_ticker(item["ticker"]))

    current_stock_value = 0
    current_bond_value = 0
    for row in balance_rows:
        t = _norm_ticker(row.get("pdno"))
        val = _safe_int(row.get("evlu_amt"))
        if t in bond_tickers:
            current_bond_value += val
        else:
            current_stock_value += val

    current_cash = _safe_int(summary_row.get("prvs_rcdl_excc_amt") or summary_row.get("dnca_tot_amt"))
    target_stock_value = int(total_assets * stock_target) if total_assets else 0
    target_bond_value = int(total_assets * bond_target) if total_assets else 0
    target_cash_value = int(total_assets * cash_target) if total_assets else 0

    if not budget:
        gpt_buy_count = sum(1 for p in gpt_plans if _gpt_action(p) == "BUY")
        executed_amount = sum(
            _safe_int(r.get("amount"))
            for r in trade_rows
            if str(r.get("action", "")).upper() == "BUY"
            and str(r.get("order_status", "")).lower() in ("executed", "partial", "completed")
        )
        stock_buy_budget = max(0, target_stock_value - current_stock_value) if aa.get("enabled") else 0
        unused = max(0, stock_buy_budget - executed_amount)
        budget = {
            "total_assets": total_assets,
            "stock_target_weight": stock_target,
            "current_stock_value": current_stock_value,
            "stock_buy_budget": stock_buy_budget,
            "gpt_buy_count": gpt_buy_count,
            "final_buy_count": len({
                _norm_ticker(r.get("ticker"))
                for r in trade_rows
                if str(r.get("action", "")).upper() == "BUY"
            }),
            "actual_stock_buy_amount": executed_amount,
            "unused_stock_budget": unused,
            "stock_budget_usage_rate": round(executed_amount / stock_buy_budget, 4) if stock_buy_budget > 0 else None,
            "unused_reason": "derived_from_db",
            "source": "computed",
        }

    out = {
        **budget,
        "bond_target_weight": bond_target,
        "cash_target_weight": cash_target,
        "current_bond_value": current_bond_value,
        "current_cash": current_cash,
        "target_stock_value": target_stock_value,
        "target_bond_value": target_bond_value,
        "target_cash_value": target_cash_value,
    }
    logger.info(
        "[STOCK_BUDGET_USAGE] total_assets=%s stock_target_weight=%.2f current_stock_value=%s "
        "stock_buy_budget=%s gpt_buy_count=%s final_buy_count=%s executed_order_amount=%s "
        "unused_stock_budget=%s usage_rate=%s unused_reason=%s",
        out.get("total_assets"),
        out.get("stock_target_weight", 0.0),
        out.get("current_stock_value"),
        out.get("stock_buy_budget"),
        out.get("gpt_buy_count"),
        out.get("final_buy_count"),
        out.get("actual_stock_buy_amount"),
        out.get("unused_stock_budget"),
        out.get("stock_budget_usage_rate"),
        out.get("unused_reason"),
    )
    return out


def _build_execution_performance(trade_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    submitted = executed = pending = cancelled = failed = 0
    reconciled = holding_fb = unreconciled = 0
    slippages: List[float] = []
    pending_durations: List[float] = []

    order_details: List[Dict[str, Any]] = []
    for r in trade_rows:
        status = str(r.get("order_status") or "").lower()
        if status in ("pending", "partial", "submitted"):
            submitted += 1
        if status in ("executed", "completed"):
            executed += 1
        if status == "pending":
            pending += 1
        if status == "cancelled":
            cancelled += 1
        if status == "failed":
            failed += 1

        method = _infer_reconcile_method(r)
        if status in ("executed", "partial", "completed"):
            reconciled += 1
            if method == "holding_fallback":
                holding_fb += 1
        elif status in ("pending", "failed") and r.get("order_id"):
            unreconciled += 1

        ctx = _parse_structured_context(r.get("structured_context"))
        orig = _safe_int(ctx.get("original_order_price") or r.get("price"))
        fill = _safe_int(ctx.get("reconciled_price") or r.get("price"))
        slip_pct = None
        if orig > 0 and fill > 0 and orig != fill:
            slip_pct = round((fill - orig) / orig, 6)
            slippages.append((fill - orig) / orig)

        order_details.append({
            "ticker": _norm_ticker(r.get("ticker")),
            "action": str(r.get("action") or "").upper(),
            "order_status": status,
            "order_id": r.get("order_id"),
            "order_price": orig,
            "executed_price": fill,
            "slippage_amount": fill - orig if orig and fill else None,
            "slippage_pct": slip_pct,
            "reconcile_method": _infer_reconcile_method(r),
        })

        ts = str(r.get("timestamp") or "")
        upd = str(r.get("last_status_update_ts") or "")
        if status == "executed" and ts and upd:
            try:
                t0 = datetime.fromisoformat(ts.replace("Z", ""))
                t1 = datetime.fromisoformat(upd.replace("Z", ""))
                pending_durations.append(max(0.0, (t1 - t0).total_seconds()))
            except Exception:
                pass

    order_count = len(trade_rows)
    execution_rate = round(executed / submitted, 4) if submitted else (1.0 if executed else 0.0)
    perf = {
        "order_count": order_count,
        "submitted_order_count": submitted,
        "executed_order_count": executed,
        "pending_order_count": pending,
        "cancelled_order_count": cancelled,
        "failed_order_count": failed,
        "execution_rate": execution_rate,
        "pending_duration_sec": round(sum(pending_durations) / len(pending_durations), 2) if pending_durations else None,
        "slippage_pct_avg": round(sum(slippages) / len(slippages), 6) if slippages else None,
        "reconciled_count": reconciled,
        "reconciled_by_holding_count": holding_fb,
        "unreconciled_count": unreconciled,
        "orders": order_details,
    }
    logger.info(
        "[PERF_EXECUTION] submitted=%s executed=%s pending=%s execution_rate=%s "
        "reconciled_by_holding=%s unreconciled=%s",
        perf["submitted_order_count"],
        perf["executed_order_count"],
        perf["pending_order_count"],
        perf["execution_rate"],
        perf["reconciled_by_holding_count"],
        perf["unreconciled_count"],
    )
    return perf


def _build_system_performance(date_str: str, log_text: str) -> Dict[str, Any]:
    perf = _parse_system_from_logs(log_text)
    state = _load_pipeline_state(date_str)
    durations = state.get("step_durations") or {}
    if isinstance(durations, dict):
        for step, sec in durations.items():
            key = step.replace(".py", "_runtime_sec")
            if key not in perf:
                perf[key] = sec
    if state.get("start_time") and state.get("last_update"):
        try:
            perf["pipeline_start_time"] = state.get("start_time")
            perf["pipeline_end_time"] = state.get("last_update")
        except Exception:
            pass
    if "total_runtime_sec" not in perf and durations:
        try:
            perf["total_runtime_sec"] = round(sum(float(v) for v in durations.values()), 2)
        except Exception:
            pass
    logger.info(
        "[PERF_SYSTEM] total_runtime_sec=%s screener_sec=%s news_sec=%s gpt_sec=%s "
        "trader_sec=%s kis_rate_limit=%s egw00201=%s errors=%s",
        perf.get("total_runtime_sec"),
        perf.get("screener_runtime_sec"),
        perf.get("news_collector_runtime_sec"),
        perf.get("gpt_analyzer_runtime_sec"),
        perf.get("trader_runtime_sec"),
        perf.get("kis_rate_limit_count"),
        perf.get("egw00201_count"),
        perf.get("error_count"),
    )
    return perf


def _fetch_price(ticker: str, price_cache: Dict[str, Optional[float]]) -> Optional[float]:
    if ticker in price_cache:
        return price_cache[ticker]
    try:
        from api.kis_auth import KIS
        env = getattr(settings, "_config", {}).get("trading_environment", "prod")
        kis = KIS(env=env if env in ("prod", "vps", "kis_paper") else "prod")
        df = kis.inquire_price("J", ticker)
        if df is not None and not df.empty:
            val = _safe_float(df["stck_prpr"].iloc[0])
            price_cache[ticker] = val
            return val
    except Exception as e:
        logger.debug("가격 조회 실패 %s: %s", ticker, e)
    price_cache[ticker] = None
    return None


def _build_candidate_tracking_snapshot(
    date_str: str,
    market: str,
    candidates: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    balance_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    gpt_by_ticker = {_plan_ticker(p): p for p in gpt_plans}
    trade_by_ticker = {
        _norm_ticker(r.get("ticker")): r
        for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
    }
    close_prices = {
        _norm_ticker(r.get("pdno")): _safe_float(r.get("prpr"))
        for r in balance_rows
    }

    tickers = set()
    for row in candidates or []:
        tickers.add(_norm_ticker(row.get("Ticker") or row.get("ticker")))
    for p in gpt_plans:
        tickers.add(_plan_ticker(p))
    tickers.discard("000000")

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(candidates or [], 1):
        ticker = _norm_ticker(row.get("Ticker") or row.get("ticker"))
        if not ticker:
            continue
        plan = gpt_by_ticker.get(ticker, {})
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        executed = status in ("executed", "partial", "completed") and _safe_int(trade.get("executed_qty") or trade.get("quantity")) > 0
        base_price = _safe_float(row.get("Price") or row.get("price")) or _plan_price(plan)
        base_close = close_prices.get(ticker) or base_price
        out.append({
            "base_date": date_str,
            "ticker": ticker,
            "name": str(row.get("Name") or row.get("name") or _plan_name(plan)),
            "screener_rank": i,
            "screener_score": _safe_float(row.get("Score") or row.get("score_total")),
            "gpt_action": _gpt_action(plan) if plan else None,
            "gpt_score": _plan_score(plan) if plan else None,
            "base_price": base_price,
            "base_close_price": base_close,
            "executed": executed,
            "executed_price": _safe_int(trade.get("price")) if executed else None,
            "executed_qty": _safe_int(trade.get("executed_qty") or trade.get("quantity")) if executed else 0,
            "track_1d_price": None,
            "track_3d_price": None,
            "track_5d_price": None,
            "track_10d_price": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "return_10d": None,
        })

    for ticker, plan in gpt_by_ticker.items():
        if any(r["ticker"] == ticker for r in out):
            continue
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        executed = status in ("executed", "partial", "completed")
        out.append({
            "base_date": date_str,
            "ticker": ticker,
            "name": _plan_name(plan),
            "screener_rank": plan.get("rank"),
            "screener_score": _plan_score(plan),
            "gpt_action": _gpt_action(plan),
            "gpt_score": _plan_score(plan),
            "base_price": _plan_price(plan),
            "base_close_price": close_prices.get(ticker) or _plan_price(plan),
            "executed": executed,
            "executed_price": _safe_int(trade.get("price")) if executed else None,
            "executed_qty": _safe_int(trade.get("executed_qty") or trade.get("quantity")) if executed else 0,
            "track_1d_price": None,
            "track_3d_price": None,
            "track_5d_price": None,
            "track_10d_price": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "return_10d": None,
        })

    buy_n = sum(1 for r in out if r.get("gpt_action") == "BUY")
    hold_n = sum(1 for r in out if r.get("gpt_action") == "HOLD")
    reject_n = sum(1 for r in out if r.get("gpt_action") == "REJECT")
    logger.info(
        "[PERF_TRACKING_SNAPSHOT] saved=%s candidates=%s buy=%s hold=%s reject=%s",
        date_str,
        len(out),
        buy_n,
        hold_n,
        reject_n,
    )
    return out


def _update_tracking_returns(review_date_str: str, market: str, fetch_prices: bool = True) -> int:
    updated_files = 0
    review_dt = datetime.strptime(review_date_str, "%Y%m%d").date()
    price_cache: Dict[str, Optional[float]] = {}

    for path in sorted(OUTPUT_DIR.glob(f"candidate_tracking_*_{market}.json")):
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        base_date = str(data.get("base_date") or "")
        if len(base_date) != 8:
            continue
        try:
            base_dt = datetime.strptime(base_date, "%Y%m%d").date()
        except ValueError:
            continue
        days = (review_dt - base_dt).days
        if days <= 0 or days not in TRACK_HORIZONS:
            continue

        rows = data.get("candidates") or data.get("rows") or []
        if not isinstance(rows, list):
            continue
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            price_key = f"track_{days}d_price"
            ret_key = f"return_{days}d"
            if row.get(price_key) is not None:
                continue
            ticker = _norm_ticker(row.get("ticker"))
            base_price = _safe_float(row.get("base_price"))
            if not ticker or not base_price:
                continue
            current = row.get("base_close_price")
            if fetch_prices:
                current = _fetch_price(ticker, price_cache) or current
            if current is None:
                continue
            row[price_key] = current
            row[ret_key] = round((float(current) - base_price) / base_price, 6)
            changed = True
        if changed:
            data["candidates"] = rows
            data["last_return_update"] = review_date_str
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            updated_files += 1
    return updated_files


def _build_summary(
    date_str: str,
    summary_row: Dict[str, Any],
    balance_rows: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    execution: Dict[str, Any],
) -> Dict[str, Any]:
    official = extract_kis_official_summary(summary_row)
    buy_trades = [
        r for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "completed")
    ]
    holdings = [r for r in balance_rows if _safe_int(r.get("hldg_qty")) > 0]
    return {
        "date": date_str,
        "total_assets": _safe_int(summary_row.get("nass_amt") or official.get("net_assets")),
        "tot_evlu_amt": _safe_int(summary_row.get("tot_evlu_amt") or official.get("tot_evlu_amt")),
        "holdings_count": len(holdings),
        "new_buy_count": len(buy_trades),
        "pending_orders": execution.get("pending_order_count", 0),
        "dnca_tot_amt": _safe_int(summary_row.get("dnca_tot_amt")),
        "prvs_rcdl_excc_amt": _safe_int(summary_row.get("prvs_rcdl_excc_amt")),
        "nxdy_excc_amt": _safe_int(summary_row.get("nxdy_excc_amt")),
        "rlzt_pfls": _safe_int(summary_row.get("rlzt_pfls")),
    }


def _build_warnings(
    system: Dict[str, Any],
    execution: Dict[str, Any],
    budget: Dict[str, Any],
) -> List[str]:
    warnings: List[str] = []
    if system.get("egw00201_count", 0) > 0:
        warnings.append(f"EGW00201 rate limit {system['egw00201_count']}회 발생")
    if system.get("traceback_count", 0) > 0:
        warnings.append(f"Traceback {system['traceback_count']}건 감지")
    if execution.get("pending_order_count", 0) > 0:
        warnings.append(f"미해결 pending 주문 {execution['pending_order_count']}건")
    if execution.get("unreconciled_count", 0) > 0:
        warnings.append(f"미리컨실 주문 {execution['unreconciled_count']}건")
    unused = budget.get("unused_reason")
    if unused and unused not in ("budget_utilized", "derived_from_db"):
        warnings.append(f"주식 예산 미사용: {unused}")
    return warnings


def _csv_rows(
    date_str: str,
    market: str,
    gpt_rows: List[Dict[str, Any]],
    tracking_rows: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    track_by_ticker = {r["ticker"]: r for r in tracking_rows}
    trade_by_ticker = {
        _norm_ticker(r.get("ticker")): r
        for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
    }
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in gpt_rows:
        t = row.get("ticker")
        seen.add(t)
        tr = track_by_ticker.get(t, {})
        trade = trade_by_ticker.get(t, {})
        out.append({
            "date": date_str,
            "market": market,
            "ticker": t,
            "name": row.get("name"),
            "screener_rank": row.get("screener_rank"),
            "screener_score": row.get("screener_score"),
            "gpt_action": row.get("gpt_action"),
            "gpt_score": row.get("gpt_score"),
            "final_decision": row.get("final_decision"),
            "executed": _safe_int(row.get("executed_qty")) > 0,
            "executed_qty": row.get("executed_qty"),
            "executed_price": _safe_int(trade.get("price")) or tr.get("executed_price"),
            "executed_amount": row.get("executed_amount"),
            "order_status": row.get("order_status"),
            "reconcile_method": row.get("reconcile_method") or _infer_reconcile_method(trade),
            "base_price": tr.get("base_price"),
            "return_1d": tr.get("return_1d"),
            "return_3d": tr.get("return_3d"),
            "return_5d": tr.get("return_5d"),
            "return_10d": tr.get("return_10d"),
            "reason": (row.get("gpt_reason") or "")[:200],
        })
    for trade in trade_rows:
        if str(trade.get("action", "")).upper() != "BUY":
            continue
        t = _norm_ticker(trade.get("ticker"))
        if t in seen:
            continue
        tr = track_by_ticker.get(t, {})
        status = str(trade.get("order_status") or "").lower()
        exe_qty = _safe_int(trade.get("executed_qty") or trade.get("quantity"))
        out.append({
            "date": date_str,
            "market": market,
            "ticker": t,
            "name": "",
            "screener_rank": tr.get("screener_rank"),
            "screener_score": tr.get("screener_score"),
            "gpt_action": None,
            "gpt_score": None,
            "final_decision": "EXECUTED",
            "executed": exe_qty > 0 and status in ("executed", "partial", "completed"),
            "executed_qty": exe_qty,
            "executed_price": _safe_int(trade.get("price")),
            "executed_amount": _safe_int(trade.get("amount")),
            "order_status": status,
            "reconcile_method": _infer_reconcile_method(trade),
            "base_price": tr.get("base_price"),
            "return_1d": tr.get("return_1d"),
            "return_3d": tr.get("return_3d"),
            "return_5d": tr.get("return_5d"),
            "return_10d": tr.get("return_10d"),
            "reason": "",
        })
    return out


def _render_markdown(report: Dict[str, Any]) -> str:
    date_str = report.get("date", "")
    market = report.get("market", "")
    summary = report.get("summary") or {}
    funnel = report.get("screener_funnel") or {}
    gpt = report.get("gpt_decision") or {}
    budget = report.get("budget_usage") or {}
    execution = report.get("execution_performance") or {}
    system = report.get("system_performance") or {}
    tracking = report.get("candidate_tracking") or []
    warnings = report.get("warnings") or []
    aggregates = gpt.get("aggregates") or {}
    trade_rows = report.get("_trade_rows") or []

    lines = [
        f"# Performance Review - {date_str} {market}",
        "",
        "## 1. Summary",
        "",
        f"- 총자산: {summary.get('total_assets', 0):,}원",
        f"- 보유종목 수: {summary.get('holdings_count', 0)}",
        f"- 당일 신규매수 수: {summary.get('new_buy_count', 0)}",
        f"- pending 주문 수: {summary.get('pending_orders', 0)}",
        f"- 총평가금액: {summary.get('tot_evlu_amt', 0):,}원",
        f"- D+2 출금가능금액: {summary.get('prvs_rcdl_excc_amt', 0):,}원",
        "",
        "## 2. Screener Funnel",
        "",
        "| 단계 | 종목 수 |",
        "|------|--------:|",
    ]

    funnel_stages = [
        ("전체 유니버스", funnel.get("total_universe_count")),
        ("Marcap 통과", funnel.get("marcap_pass_count")),
        ("Amount5D 통과", funnel.get("amount5d_pass_count")),
        ("최종 후보", funnel.get("final_screener_candidate_count")),
        ("스코어 저장", funnel.get("screener_score_count")),
        ("GPT 분석", funnel.get("gpt_analysis_count")),
        ("GPT BUY", funnel.get("gpt_buy_count")),
        ("GPT HOLD", funnel.get("gpt_hold_count")),
        ("GPT REJECT", funnel.get("gpt_reject_count")),
        ("체결 BUY", funnel.get("executed_buy_count")),
    ]
    prev = None
    for label, cnt in funnel_stages:
        if cnt is None:
            lines.append(f"| {label} | - |")
            continue
        drop = "" if prev is None or prev == 0 else f" (−{prev - cnt})"
        lines.append(f"| {label} | {cnt}{drop} |")
        prev = cnt

    lines.extend(["", "## 3. GPT Decision", "", "| Action | Count | Avg GPT Score | Avg Screener | Executed | Amount |", "|--------|------:|--------------:|-------------:|---------:|-------:|"])
    for action in ("BUY", "HOLD", "REJECT"):
        agg = aggregates.get(action) or {}
        lines.append(
            f"| {action} | {agg.get('count', 0)} | {agg.get('avg_gpt_score', '-')} | "
            f"{agg.get('avg_screener_score', '-')} | {agg.get('executed_count', 0)} | "
            f"{agg.get('executed_amount', 0):,} |"
        )

    lines.extend(["", "## 4. Executed Trades", "", "| Ticker | Name | Action | Qty | Price | Amount | Status | Reconcile |", "|--------|------|--------|----:|------:|-------:|--------|-----------|"])
    for row in trade_rows:
        if str(row.get("action", "")).upper() != "BUY":
            continue
        status = str(row.get("order_status") or "").lower()
        if status not in ("executed", "partial", "completed"):
            continue
        qty = _safe_int(row.get("executed_qty") or row.get("quantity"))
        price = _safe_int(row.get("price"))
        amount = _safe_int(row.get("amount")) or qty * price
        method = _infer_reconcile_method(row)
        lines.append(
            f"| {_norm_ticker(row.get('ticker'))} | | BUY | {qty} | {price:,} | {amount:,} | {status} | {method} |"
        )

    lines.extend([
        "",
        "## 5. Budget Usage",
        "",
        "| 항목 | 값 |",
        "|------|-----|",
        f"| Target Stock | {budget.get('target_stock_value', 0):,} ({(budget.get('stock_target_weight', 0) or 0)*100:.0f}%) |",
        f"| Current Stock | {budget.get('current_stock_value', 0):,} |",
        f"| Stock Buy Budget | {budget.get('stock_buy_budget', 0):,} |",
        f"| Executed | {budget.get('actual_stock_buy_amount', 0):,} |",
        f"| Usage Rate | {budget.get('stock_budget_usage_rate', '-')} |",
        f"| Unused Reason | {budget.get('unused_reason', '-')} |",
        "",
        "## 6. Candidate Tracking",
        "",
        "| Ticker | Name | Rank | GPT | Base | 1D | 3D | 5D |",
        "|--------|------|-----:|-----|-----:|---:|---:|---:|",
    ])
    for row in tracking[:30]:
        lines.append(
            f"| {row.get('ticker')} | {row.get('name', '')} | {row.get('screener_rank', '-')} | "
            f"{row.get('gpt_action', '-')} | {row.get('base_price', '-')} | "
            f"{row.get('return_1d', '-')} | {row.get('return_3d', '-')} | {row.get('return_5d', '-')} |"
        )

    lines.extend([
        "",
        "## 7. System Performance",
        "",
        "| Stage | Runtime (s) |",
        "|-------|------------:|",
        f"| Total | {system.get('total_runtime_sec', '-')} |",
        f"| Screener | {system.get('screener_runtime_sec', '-')} |",
        f"| News | {system.get('news_collector_runtime_sec', '-')} |",
        f"| GPT | {system.get('gpt_analyzer_runtime_sec', '-')} |",
        f"| Trader | {system.get('trader_runtime_sec', '-')} |",
        f"| Reconciler | {system.get('order_reconciler_runtime_sec', '-')} |",
        "",
        "## 8. Warnings",
        "",
    ])
    if warnings:
        for w in warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- 없음")
    lines.append("")
    return "\n".join(lines)


def build_performance_review(date_str: str, market: str, *, fetch_tracking_prices: bool = True) -> Dict[str, Any]:
    candidates = _load_json(_artifact_path("screener_candidates", date_str, market)) or []
    scores = _load_json(_artifact_path("screener_scores", date_str, market)) or []
    gpt_data = _load_json(_artifact_path("gpt_trades", date_str, market)) or {}
    gpt_plans = gpt_data.get("plans") if isinstance(gpt_data, dict) else (gpt_data or [])
    if not isinstance(gpt_plans, list):
        gpt_plans = []
    balance_data = _load_json(_artifact_path("balance", date_str, market))
    summary_data = _load_json(_artifact_path("summary", date_str, market))
    balance_rows = _parse_balance_rows(balance_data)
    summary_row = _parse_summary_row(summary_data)

    trade_rows = _load_trade_rows(date_str)
    log_files = _find_log_files(date_str)
    log_text = ""
    for p in log_files[:5]:
        try:
            log_text += p.read_text(encoding="utf-8", errors="ignore") + "\n"
        except Exception:
            pass

    rank_map, score_map = _build_screener_maps(candidates, scores)
    screener_funnel = _build_screener_funnel(
        date_str, market, candidates, scores, gpt_plans, trade_rows, log_text
    )
    gpt_decision = _build_gpt_decision(gpt_plans, rank_map, score_map, trade_rows)
    budget_usage = _build_budget_usage(
        date_str, log_text, summary_row, balance_rows, trade_rows, gpt_plans
    )
    execution_performance = _build_execution_performance(trade_rows)
    system_performance = _build_system_performance(date_str, log_text)
    candidate_tracking = _build_candidate_tracking_snapshot(
        date_str, market, candidates, gpt_plans, trade_rows, balance_rows
    )
    summary = _build_summary(date_str, summary_row, balance_rows, trade_rows, execution_performance)
    warnings = _build_warnings(system_performance, execution_performance, budget_usage)

    if fetch_tracking_prices:
        _update_tracking_returns(date_str, market, fetch_prices=True)

    tracking_path = OUTPUT_DIR / f"candidate_tracking_{date_str}_{market}.json"
    tracking_doc = {
        "base_date": date_str,
        "market": market,
        "generated_at": datetime.now(KST).isoformat(),
        "candidates": candidate_tracking,
    }
    with open(tracking_path, "w", encoding="utf-8") as f:
        json.dump(tracking_doc, f, ensure_ascii=False, indent=2)

    return {
        "date": date_str,
        "market": market,
        "generated_at": datetime.now(KST).isoformat(),
        "summary": summary,
        "screener_funnel": screener_funnel,
        "gpt_decision": gpt_decision,
        "budget_usage": budget_usage,
        "execution_performance": execution_performance,
        "system_performance": system_performance,
        "candidate_tracking": candidate_tracking,
        "warnings": warnings,
        "_trade_rows": trade_rows,
        "sources": {
            "log_files": [str(p) for p in log_files[:5]],
            "balance": str(_artifact_path("balance", date_str, market)),
            "summary": str(_artifact_path("summary", date_str, market)),
            "gpt_trades": str(_artifact_path("gpt_trades", date_str, market)),
        },
    }


def write_outputs(report: Dict[str, Any]) -> Tuple[Path, Path, Path]:
    date_str = report["date"]
    market = report["market"]
    base = OUTPUT_DIR / f"performance_review_{date_str}_{market}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    csv_path = base.with_suffix(".csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in report.items() if not str(k).startswith("_")}, f, ensure_ascii=False, indent=2)

    md_path.write_text(_render_markdown(report), encoding="utf-8")

    gpt_rows = (report.get("gpt_decision") or {}).get("rows") or []
    tracking_rows = report.get("candidate_tracking") or []
    trade_rows = report.get("_trade_rows") or []
    csv_rows = _csv_rows(date_str, market, gpt_rows, tracking_rows, trade_rows)
    fieldnames = [
        "date", "market", "ticker", "name", "screener_rank", "screener_score",
        "gpt_action", "gpt_score", "final_decision", "executed", "executed_qty",
        "executed_price", "executed_amount", "order_status", "reconcile_method",
        "base_price", "return_1d", "return_3d", "return_5d", "return_10d", "reason",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)

    logger.info("저장 완료: %s, %s, %s", json_path, csv_path, md_path)
    return json_path, csv_path, md_path


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="일별 전략·시스템 성능 리뷰 생성")
    parser.add_argument("--date", help="리뷰 날짜 YYYYMMDD (기본: 오늘 KST)")
    parser.add_argument("--market", default="KOSPI", help="시장 (기본: KOSPI)")
    parser.add_argument("--no-fetch-prices", action="store_true", help="추적 수익률 KIS 시세 조회 생략")
    args = parser.parse_args(argv)

    date_str = _norm_date(args.date)
    market = str(args.market or "KOSPI").upper()
    load_config()

    logger.info("성능 리뷰 생성 시작: date=%s market=%s", date_str, market)
    report = build_performance_review(
        date_str,
        market,
        fetch_tracking_prices=not args.no_fetch_prices,
    )
    write_outputs(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
