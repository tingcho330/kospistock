#!/usr/bin/env python3
"""asset_allocator 단위 검증 (NumPy-free, KIS 호출 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asset_allocator import calculate_final_bond_buy_budget, compute_allocation  # noqa: E402


def _cfg(enabled: bool = True) -> dict:
    return {
        "asset_allocation": {
            "enabled": enabled,
            "stock_target_weight": 0.70,
            "bond_target_weight": 0.20,
            "cash_target_weight": 0.10,
            "min_cash_weight": 0.05,
            "rebalance_tolerance": 0.03,
            "bond_buy_enabled": True,
            "stock_buy_block_when_overweight": True,
            "bond_etfs": [{"ticker": "459580", "name": "KODEX CD금리액티브(합성)"}],
        }
    }


def _holdings(stock: int, bond: int) -> list:
    rows = []
    if stock > 0:
        rows.append({"pdno": "005930", "hldg_qty": 1, "evlu_amt": stock})
    if bond > 0:
        rows.append({"pdno": "459580", "hldg_qty": 1, "evlu_amt": bond})
    return rows


def _assert_close(name: str, actual: int, expected: int, tol: int = 1) -> None:
    if abs(actual - expected) > tol:
        raise AssertionError(f"{name}: expected {expected:,}, got {actual:,}")


def _assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"{name}: expected True")


def _assert_false(name: str, cond: bool) -> None:
    if cond:
        raise AssertionError(f"{name}: expected False")


def case1() -> None:
    r = compute_allocation(1_000_000, _holdings(7_000_000, 2_000_000), _cfg())
    _assert_close("stock_gap", r.stock_gap, 0)
    _assert_close("bond_gap", r.bond_gap, 0)
    _assert_close("min_cash", r.min_cash_amount, 500_000)


def case2() -> None:
    r = compute_allocation(700_000, _holdings(7_800_000, 1_500_000), _cfg())
    _assert_close("stock_buy_budget", r.stock_buy_budget, 0)
    _assert_true("initial_bond <= 200k", r.initial_bond_buy_budget <= 200_000)
    _assert_false("can_buy_stock", r.can_buy_stock)


def case3() -> None:
    r = compute_allocation(1_700_000, _holdings(6_300_000, 2_000_000), _cfg())
    _assert_close("stock_gap", r.stock_gap, 700_000)
    _assert_close("stock_buy_budget", r.stock_buy_budget, 700_000)
    _assert_close("initial_bond_buy_budget", r.initial_bond_buy_budget, 0)


def case4() -> None:
    r = compute_allocation(2_000_000, _holdings(7_000_000, 1_000_000), _cfg())
    _assert_close("bond_gap", r.bond_gap, 1_000_000)
    _assert_true("initial_bond > 0", r.initial_bond_buy_budget > 0)


def case5() -> None:
    r = compute_allocation(200_000, _holdings(7_200_000, 2_600_000), _cfg())
    _assert_false("can_buy_stock", r.can_buy_stock)
    _assert_false("can_buy_bond", r.can_buy_bond)
    assert r.reason == "below_min_cash"


def case6() -> None:
    final = calculate_final_bond_buy_budget(300_000, 620_000, 500_000)
    _assert_close("final_bond_buy_budget", final, 120_000)


def case7() -> None:
    r = compute_allocation(300_000, _holdings(7_600_000, 2_100_000), _cfg())
    _assert_close("stock_gap", r.stock_gap, 0)
    _assert_close("bond_gap", r.bond_gap, 0)
    _assert_close("stock_buy_budget", r.stock_buy_budget, 0)
    _assert_close("initial_bond_buy_budget", r.initial_bond_buy_budget, 0)


def main() -> int:
    cases = [
        ("Case 1 정상", case1),
        ("Case 2 주식 과다", case2),
        ("Case 3 주식 부족", case3),
        ("Case 4 459580 부족", case4),
        ("Case 5 현금 부족", case5),
        ("Case 6 final_bond", case6),
        ("Case 7 목표 초과", case7),
    ]
    passed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {name}: {e}")
            return 1
    print(f"\n{passed}/{len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
