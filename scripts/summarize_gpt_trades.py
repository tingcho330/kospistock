#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gpt_trades JSON 요약 유틸.

Usage:
  PYTHONPATH=/app/src python /app/scripts/summarize_gpt_trades.py --date 20260630 --market KOSPI
  PYTHONPATH=/app/src python /app/scripts/summarize_gpt_trades.py --date 20260630 --market KOSPI --buy-only
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import OUTPUT_DIR, setup_logging

GPT_ACTION_MAP = {
    "매수": "BUY",
    "보류": "HOLD",
    "미진입": "REJECT",
}


def _norm_date(s: Optional[str]) -> str:
    if not s:
        raise ValueError("--date YYYYMMDD required")
    t = re.sub(r"\D", "", str(s))
    if len(t) != 8:
        raise ValueError(f"Invalid date: {s!r}")
    return t


def _gpt_action(plan: Dict[str, Any]) -> str:
    raw = str(plan.get("결정") or plan.get("decision") or "").strip()
    return GPT_ACTION_MAP.get(raw, raw.upper() or "UNKNOWN")


def _plan_ticker(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return str(info.get("Ticker") or info.get("ticker") or plan.get("ticker") or "").zfill(6)


def _plan_name(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return str(info.get("Name") or info.get("name") or plan.get("name") or "")


def _plan_score(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    for key in ("Score", "score", "ConvictionScore", "conviction_score"):
        v = info.get(key)
        if v is not None and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _plan_price(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    v = info.get("Price") or info.get("price")
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="gpt_trades JSON 요약")
    parser.add_argument("--date", required=True, help="YYYYMMDD")
    parser.add_argument("--market", default="KOSPI", help="KOSPI/KOSDAQ/KONEX")
    parser.add_argument("--buy-only", action="store_true", help="BUY 후보만 출력")
    args = parser.parse_args()

    date_str = _norm_date(args.date)
    market = str(args.market or "KOSPI").upper()
    path = OUTPUT_DIR / f"gpt_trades_{date_str}_{market}.json"

    if not path.is_file():
        print(f"File not found: {path}")
        return 1

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    plans: List[Dict[str, Any]] = data.get("plans") if isinstance(data, dict) else (data or [])
    if not isinstance(plans, list):
        plans = []

    buy = hold = reject = 0
    rows: List[Dict[str, Any]] = []
    for i, plan in enumerate(plans, start=1):
        action = _gpt_action(plan)
        if action == "BUY":
            buy += 1
        elif action == "HOLD":
            hold += 1
        elif action == "REJECT":
            reject += 1
        rows.append({
            "rank": plan.get("rank") or i,
            "ticker": _plan_ticker(plan),
            "name": _plan_name(plan),
            "decision": action,
            "score": _plan_score(plan),
            "price": _plan_price(plan),
            "strategy": plan.get("전략_클래스") or plan.get("strategy_class") or "",
            "tactic": plan.get("매매전술") or plan.get("tactic") or "",
        })

    print(f"file: {path.name}")
    print(f"count: {len(plans)}")
    print(f"buy: {buy}  hold: {hold}  reject: {reject}")
    print()
    print(f"{'rank':>4}  {'ticker':<6}  {'name':<16}  {'decision':<6}  {'score':>6}  {'price':>10}  strategy / tactic")
    print("-" * 90)

    for r in rows:
        if args.buy_only and r["decision"] != "BUY":
            continue
        score_s = f"{r['score']:.2f}" if r["score"] is not None else "-"
        price_s = f"{int(r['price']):,}" if r["price"] is not None else "-"
        strat = f"{r['strategy']} / {r['tactic']}".strip(" /")
        print(
            f"{r['rank']:>4}  {r['ticker']:<6}  {r['name'][:16]:<16}  "
            f"{r['decision']:<6}  {score_s:>6}  {price_s:>10}  {strat}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
