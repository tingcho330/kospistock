#!/usr/bin/env python3
"""RotationSandbox 단위 검증 (GPT 0 buy / hold / reject 시나리오)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_sandbox import build_rotation_sandbox_suggestions, run_rotation_sandbox


def test_no_actionable_candidates():
    cfg = {
        "integrated_analysis": {"log_gpt_rotation_suggestions": True},
        "trading_params": {"per_ticker_max_weight": 0.15},
        "asset_allocation": {"bond_etfs": [{"ticker": "459580"}]},
    }
    holdings = [
        {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": 10, "prpr": 70000},
    ]
    summary = {"tot_evlu_amt": 10_000_000}
    suggestions, status = build_rotation_sandbox_suggestions(
        cfg, holdings=holdings, summary=summary, load_snapshot=False,
    )
    assert suggestions == [], suggestions
    assert status == "no_actionable_candidates", status
    print("OK test_no_actionable_candidates")


def test_gpt_plans_zero_buy_skip():
    """GPT plans 0 buy — sandbox는 holdings overweight 없으면 skip."""
    cfg = {"integrated_analysis": {"log_gpt_rotation_suggestions": True}}
    run_rotation_sandbox(cfg, fixed_date="20260625", market="KOSPI")
    print("OK test_gpt_plans_zero_buy_skip (no exception)")


def test_overweight_suggestion():
    cfg = {
        "integrated_analysis": {"log_gpt_rotation_suggestions": True, "min_confidence_for_rotation": 0.7},
        "trading_params": {"per_ticker_max_weight": 0.10},
    }
    holdings = [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": 100, "prpr": 70000}]
    summary = {"tot_evlu_amt": 5_000_000}
    suggestions, status = build_rotation_sandbox_suggestions(
        cfg, holdings=holdings, summary=summary, load_snapshot=False,
    )
    assert status == "ok", status
    assert len(suggestions) >= 1, suggestions
    print("OK test_overweight_suggestion")


if __name__ == "__main__":
    test_no_actionable_candidates()
    test_overweight_suggestion()
    test_gpt_plans_zero_buy_skip()
    print("All RotationSandbox tests passed.")
