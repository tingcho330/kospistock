#!/usr/bin/env python3
"""Phase 6: asset_allocation 경로 dry-run — KIS 주문 없이 로그 포맷 검증."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asset_allocator import (  # noqa: E402
    calculate_final_bond_buy_budget,
    compute_allocation,
    is_bond_etf,
)


def _load_config() -> dict:
    cfg_path = Path(
        __import__("os").environ.get("CONFIG_PATH", str(ROOT / "config" / "config.json"))
    )
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


def _holdings(stock_value: int, bond_value: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if stock_value > 0:
        rows.append({"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": 1, "evlu_amt": stock_value})
    if bond_value > 0:
        rows.append({"pdno": "459580", "prdt_name": "KODEX CD금리액티브", "hldg_qty": 1, "evlu_amt": bond_value})
    return rows


def _pct(w: float) -> str:
    return f"{w * 100:.1f}%"


def _print_before(cfg: dict, cash: int, holdings: List[Dict[str, Any]]) -> None:
    tp = cfg.get("trading_params", {}) or {}
    dcm = tp.get("dynamic_cash_management", {}) or {}
    aa = cfg.get("asset_allocation", {}) or {}
    env = cfg.get("trading_environment", "vps")
    print("=== BEFORE ===")
    print(f"trading_environment: {env}")
    print(f"is_real_trading: {env == 'prod'}")
    print(f"asset_allocation.enabled: {aa.get('enabled', False)}")
    print(f"available_cash: {cash:,}원")
    print(f"holdings_count: {len(holdings)}")
    for h in holdings:
        t = str(h.get("pdno", "")).zfill(6)
        tag = " [bond_etf]" if is_bond_etf(t, cfg) else ""
        print(
            f"  - {h.get('prdt_name', t)}({t}) "
            f"qty={int(h.get('hldg_qty', 0) or 0)} "
            f"evlu={int(h.get('evlu_amt', 0) or 0):,}{tag}"
        )
    print(f"dynamic_cash_management.enabled: {dcm.get('enabled', False)}")
    if aa.get("enabled"):
        print("dynamic_cash_management.applied: SKIP (asset_allocation.enabled=true)")
    else:
        print("dynamic_cash_management.applied: YES (trader.get_account_info_from_files 경로)")
    print()


def _print_allocation(cfg: dict, cash: int, holdings: List[Dict[str, Any]]) -> Any:
    aa_cfg = dict(cfg)
    aa_section = dict(aa_cfg.get("asset_allocation") or {})
    aa_section["enabled"] = True
    aa_cfg["asset_allocation"] = aa_section
    r = compute_allocation(cash, holdings, aa_cfg)
    print("=== ALLOCATION (enabled=true 시뮬) ===")
    print("[ASSET_ALLOCATION]")
    print(f"총자산: {r.total_asset:,}원")
    print(f"주식 평가액: {r.stock_value:,}원 ({_pct(r.stock_weight)})")
    print(f"459580 평가액: {r.bond_value:,}원 ({_pct(r.bond_weight)})")
    print(f"현재 가용 현금: {r.cash_value:,}원 ({_pct(r.cash_weight)})")
    print("------------------------------------")
    print(f"목표 주식 금액: {r.target_stock_value:,}원")
    print(f"목표 459580 금액: {r.target_bond_value:,}원")
    print(f"목표 현금: {r.target_cash_value:,}원")
    print(f"최소 보전 현금(5%): {r.min_cash_amount:,}원")
    print(f"stock_buy_budget: {r.stock_buy_budget:,}원")
    print(f"initial_bond_buy_budget: {r.initial_bond_buy_budget:,}원")
    print(f"can_buy_stock: {r.can_buy_stock}")
    print(f"can_buy_bond: {r.can_buy_bond}")
    print(f"reason: {r.reason}")
    if r.stock_buy_budget < cash:
        print(f"CHECK stock_buy_budget < available_cash: {r.stock_buy_budget:,} < {cash:,} OK")
    print()
    return r


def _print_after_stock(
    allocation: Any,
    *,
    post_stock_cash: int,
    bond_price: int = 100_000,
) -> Tuple[int, int, str]:
    final = calculate_final_bond_buy_budget(
        allocation.initial_bond_buy_budget,
        post_stock_cash,
        allocation.min_cash_amount,
    )
    print("=== AFTER STOCK ORDERS (simulated) ===")
    print("[ASSET_ALLOCATION_POST_STOCK_ORDER]")
    print(f"initial_bond_buy_budget: {allocation.initial_bond_buy_budget:,}원")
    print(f"post_stock_cash: {post_stock_cash:,}원")
    print(f"min_cash_amount: {allocation.min_cash_amount:,}원")
    print(f"final_bond_buy_budget: {final:,}원")
    if post_stock_cash < allocation.min_cash_amount:
        print("CHECK cash below min → final_bond_buy_budget should be 0")
    print()

    qty = 0
    result = "skipped"
    if final <= 0:
        result = "skipped_zero_budget"
    elif bond_price <= 0:
        result = "skipped_invalid_price"
    else:
        qty = final // bond_price
        if qty < 1:
            result = "skipped_qty_lt_1"
        else:
            result = "paper_executed (dry-run, no _order_cash_retry)"

    print("=== BOND ORDER (simulated) ===")
    print("ticker: 459580")
    print(f"price: {bond_price:,}원")
    print(f"qty: {qty}주")
    print(f"result: {result}")
    print(f"order_path: _order_cash_retry → kis.order_cash (real) / paper log (vps)")
    print()
    return final, qty, result


def _run_scenario(name: str, cash: int, stock: int, bond: int, post_stock_cash: Optional[int]) -> None:
    cfg = _load_config()
    holdings = _holdings(stock, bond)
    print("=" * 72)
    print(f"SCENARIO: {name}")
    print("=" * 72)
    _print_before(cfg, cash, holdings)
    alloc = _print_allocation(cfg, cash, holdings)
    if post_stock_cash is None:
        spent = min(alloc.stock_buy_budget, max(0, cash - alloc.min_cash_amount))
        post_stock_cash = max(0, cash - spent)
    _print_after_stock(alloc, post_stock_cash=post_stock_cash)


def _load_snapshot() -> Optional[Tuple[int, List[Dict[str, Any]]]]:
    try:
        from utils import get_account_snapshot_cached, extract_cash_from_summary  # noqa: WPS433

        summary, balance, *_ = get_account_snapshot_cached(force=False)
        if not summary and not balance:
            return None
        cash = extract_cash_from_summary(summary or {})
        holdings = balance or []
        return int(cash or 0), holdings
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6 asset_allocation dry-run log")
    parser.add_argument(
        "--scenario",
        choices=["all", "normal", "stock_cap", "low_cash", "snapshot"],
        default="all",
    )
    parser.add_argument("--cash", type=int, help="override available_cash")
    parser.add_argument("--post-stock-cash", type=int, help="override post_stock_cash")
    args = parser.parse_args()

    if args.scenario == "snapshot" or args.cash is not None:
        snap = _load_snapshot()
        cfg = _load_config()
        if snap and args.cash is None:
            cash, holdings = snap
            label = "snapshot (output/ account files)"
        else:
            cash = args.cash if args.cash is not None else 1_000_000
            holdings = _holdings(7_000_000, 2_000_000)
            label = "custom cash override"
        print("=" * 72)
        print(f"SCENARIO: {label}")
        print("=" * 72)
        _print_before(cfg, cash, holdings)
        alloc = _print_allocation(cfg, cash, holdings)
        post = args.post_stock_cash
        if post is None:
            spent = min(alloc.stock_buy_budget, max(0, cash - alloc.min_cash_amount))
            post = max(0, cash - spent)
        _print_after_stock(alloc, post_stock_cash=post)
        return 0

    scenarios = {
        "normal": ("정상 70:20:10", 1_000_000, 7_000_000, 2_000_000, None),
        "stock_cap": ("주식 부족 → stock_buy_budget < cash", 1_700_000, 6_300_000, 2_000_000, None),
        "low_cash": ("현금 5% 미만 → bond skip", 400_000, 7_000_000, 2_000_000, 400_000),
    }
    run = scenarios.keys() if args.scenario == "all" else [args.scenario]
    for key in run:
        if key not in scenarios:
            continue
        label, cash, stock, bond, post = scenarios[key]
        _run_scenario(label, cash, stock, bond, args.post_stock_cash if args.post_stock_cash else post)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
