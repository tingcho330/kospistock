#!/usr/bin/env python3
"""스크리너 게이트·가산/감점·Conviction 비중·거래대금 시나리오 리플레이."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from screener_core import (
    filter_breakout_gate_tiered,
    compute_breakout_score_bonus,
    compute_near_high_penalty,
    apply_weak_breakout_score_multiplier,
    compute_conviction_score,
    build_rank_reasons,
    build_selection_summary,
)
from portfolio_allocator import allocate_portfolio_weights


def _load_cfg() -> dict:
    cfg_path = ROOT / "config" / "config.json"
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            full = json.load(f)
        sp = full.get("screener_params", {})
        return sp if isinstance(sp, dict) else {}
    return {"top_n": 20, "breakout_params": {"require_breakout_gate": True}}


def _estimate_score_base(row: pd.Series) -> float:
    if "ScoreBase" in row.index and pd.notna(row.get("ScoreBase")):
        return float(row["ScoreBase"])
    score = float(row.get("Score", 0) or 0)
    bonus = float(row.get("BreakoutBonus", 0) or 0)
    nh = float(row.get("NearHighPenalty", 0) or 0)
    wmult = float(row.get("WeakBreakoutMultiplier", 1) or 1)
    if wmult > 0 and wmult < 1.0 and nh == 0 and bonus == 0:
        return score / wmult
    return score - bonus + nh


def _apply_score_adjustments(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()
    out["ScoreBase"] = out.apply(_estimate_score_base, axis=1)

    bonuses, nh_penalties, weak_mults, penalty_reasons, convictions, scores = [], [], [], [], [], []
    for _, row in out.iterrows():
        br = float(row.get("BreakoutScore", 0) or 0)
        pos = float(row.get("Pos52w", 0) or 0)
        flow = float(row.get("FlowScore", 0) or 0)
        mom = float(row.get("MomentumScore", 0) or 0)
        growth = float(row.get("GrowthScore", 0) or 0)
        base = float(row.get("ScoreBase", 0) or 0)
        bonus = compute_breakout_score_bonus(br, cfg)
        nh_pen, preason = compute_near_high_penalty(pos, br, cfg)
        subtotal = max(0.0, min(1.0, base + bonus - nh_pen))
        final, wmult, wreasons = apply_weak_breakout_score_multiplier(subtotal, br, cfg)
        reasons = list(preason or [])
        reasons.extend(wreasons)
        bonuses.append(bonus)
        nh_penalties.append(nh_pen)
        weak_mults.append(wmult)
        penalty_reasons.append(reasons)
        convictions.append(compute_conviction_score(flow, mom, br, growth, cfg))
        scores.append(final)

    out["BreakoutBonus"] = bonuses
    out["NearHighPenalty"] = nh_penalties
    out["WeakBreakoutMultiplier"] = weak_mults
    out["penalty_reason"] = penalty_reasons
    out["ConvictionScore"] = convictions
    out["Score"] = scores

    if "rank_reason" not in out.columns:
        out["rank_reason"] = out.apply(
            lambda r: build_rank_reasons(
                flow_score=float(r.get("FlowScore", 0) or 0),
                momentum_score=float(r.get("MomentumScore", 0) or 0),
                growth_score=float(r.get("GrowthScore", 0) or 0),
                fin_score=float(r.get("FinScore", 0) or 0),
                breakout_score=float(r.get("BreakoutScore", 0) or 0),
                pos52w=float(r.get("Pos52w", 0) or 0),
                sector_score=float(r.get("SectorScore", 0) or 0),
                op_turnaround=bool(r.get("OpProfitTurnaround", False)),
                growth_bonus=float(r.get("GrowthBonus", 0) or 0),
                cfg=cfg,
            ),
            axis=1,
        )
    out["selection_summary"] = out["rank_reason"].apply(build_selection_summary)
    return out


def _allocate(top: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    portfolio_cfg = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
    max_w = float(
        portfolio_cfg.get("max_weight")
        or portfolio_cfg.get("per_ticker_max_weight", 0.075)
    )
    min_w = float(portfolio_cfg.get("min_weight", 0.02))
    ranked = allocate_portfolio_weights(
        top.to_dict(orient="records"),
        mode=str(portfolio_cfg.get("weight_mode", "conviction")),
        per_ticker_cap=max_w,
        min_ticker_weight=min_w,
        max_ticker_weight=max_w,
        rank_tiers=portfolio_cfg.get("rank_tiers"),
        normalize_weights=bool(portfolio_cfg.get("normalize_weights", True)),
    )
    return pd.DataFrame(ranked)


def _run_pipeline(df: pd.DataFrame, cfg: dict, label: str) -> pd.DataFrame:
    baseline_br = df["BreakoutScore"].mean() if len(df) else 0.0
    print(f"\n{'='*60}\n[{label}] n={len(df)}")
    if len(df) == 0:
        return df
    scored = _apply_score_adjustments(df, cfg)
    gated, tier = filter_breakout_gate_tiered(
        scored, cfg, min_count=int(cfg.get("top_n", 20)),
    )
    print(f"  Breakout Gate: tier={tier}, 생존={len(gated)}")
    top_n = int(cfg.get("top_n", 20))
    top = gated.sort_values("Score", ascending=False).head(top_n)
    top = _allocate(top, cfg)
    _print_validation(top, baseline_br_mean=baseline_br, label=label)
    return top


def _has_weak_penalty(reasons) -> bool:
    return isinstance(reasons, list) and "weak_breakout" in reasons


def _print_validation(
    top: pd.DataFrame,
    *,
    baseline_br_mean: float | None = None,
    label: str = "검증",
) -> None:
    n = len(top)
    if n == 0:
        print(f"  [{label}] 최종 후보 0건")
        return

    br_lt_10 = (top["BreakoutScore"] < 0.10).sum()
    weak_pen_n = top["penalty_reason"].apply(_has_weak_penalty).sum() if "penalty_reason" in top.columns else 0
    br_mean = top["BreakoutScore"].mean()
    flow_mean = top["FlowScore"].mean()
    conv_top5 = top.nlargest(min(5, n), "ConvictionScore")

    print(f"  Breakout<0.10: {br_lt_10}종")
    print(f"  WeakBreakoutPenalty 적용: {weak_pen_n}종")
    print(f"  BreakoutScore 평균: {br_mean:.3f}", end="")
    if baseline_br_mean and baseline_br_mean > 0:
        pct = (br_mean / baseline_br_mean - 1) * 100
        print(f" (입력 대비 {pct:+.1f}%)")
    else:
        print()
    print(f"  FlowScore 평균: {flow_mean:.3f}")
    print(f"  MomentumScore 평균: {top['MomentumScore'].mean():.3f}")

    if "gate_tier" in top.columns:
        print(f"  gate_tier: {dict(Counter(top['gate_tier']))}")

    print("\n  [Conviction 상위 5]")
    cols = ["Ticker", "Name", "ConvictionScore", "Score", "BreakoutScore", "target_weight"]
    show = [c for c in cols if c in top.columns]
    print(conv_top5[show].to_string(index=False))

    if "target_weight" in top.columns:
        wmax = top["target_weight"].max()
        wmin = top["target_weight"].min()
        print(f"\n  비중 분포: min={wmin:.4f} max={wmax:.4f} sum={top['target_weight'].sum():.4f}")
        top_conv = conv_top5.iloc[0] if len(conv_top5) else None
        if top_conv is not None and "target_weight" in top_conv:
            print(
                f"  Conviction 1위({top_conv.get('Ticker')}) 비중={float(top_conv.get('target_weight', 0)):.4f}"
            )


def _compare_amount5d(df: pd.DataFrame, cfg: dict) -> None:
    if "Amount5D" not in df.columns:
        print("\n[Amount5D 시나리오] Amount5D 컬럼 없음 — 전체 풀 JSON 필요")
        return
    amt = pd.to_numeric(df["Amount5D"], errors="coerce").fillna(0)
    cases = [
        ("Case A (200억)", 20_000_000_000),
        ("Case B (300억)", 30_000_000_000),
    ]
    print("\n[Amount5D 시나리오 비교]")
    for name, threshold in cases:
        sub = df.loc[amt >= threshold].copy()
        print(f"  {name}: 후보 {len(sub)}종 (≥{threshold:,})")
    for name, threshold in cases:
        sub = df.loc[amt >= threshold].copy()
        if sub.empty:
            continue
        _run_pipeline(sub, cfg, label=name)


def main() -> int:
    parser = argparse.ArgumentParser(description="스크리너 리플레이·검증")
    parser.add_argument("path", nargs="?", help="screener JSON 경로")
    parser.add_argument(
        "--amount5d-test",
        action="store_true",
        help="Amount5D 200억 vs 300억 시나리오 비교",
    )
    args = parser.parse_args()

    defaults = [
        Path.home() / "Downloads/screener_candidates_full_20260622_KOSPI.json",
        Path.home() / "Downloads/screener_candidates_full_20260622_KOSPI (2).json",
    ]
    path = Path(args.path) if args.path else next((p for p in defaults if p.exists()), defaults[0])
    if not path.exists():
        print(f"파일 없음: {path}")
        return 1

    cfg = _load_cfg()
    rows = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(rows)
    for col in (
        "BreakoutScore", "Pos52w", "Score", "MomentumScore", "FlowScore",
        "GrowthScore", "FinScore", "SectorScore", "Amount5D",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    print(f"입력: {path.name} ({len(df)}종)")
    print(
        f"  운영 Amount5D 기준: {cfg.get('min_trading_value_5d_avg', 20_000_000_000):,} "
        f"(config 변경 없음, 리플레이만)"
    )
    print(f"  weight_mode: {cfg.get('portfolio', {}).get('weight_mode', 'conviction')}")

    if args.amount5d_test:
        _compare_amount5d(df, cfg)
        return 0

    _run_pipeline(df, cfg, label="기본 리플레이")

    if "Amount5D" in df.columns and len(df) >= 50:
        print("\n[참고] --amount5d-test 로 200억/300억 시나리오 비교 가능")
    elif len(df) < 50:
        print(
            f"\n[참고] 입력 {len(df)}종 — 1차 풀(118종) 대비 후보군 축소 검증은 "
            "전체 스코어 풀 JSON 또는 Amount5D 포함 데이터 필요"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
