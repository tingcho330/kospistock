#!/usr/bin/env python3
"""스크리너 게이트·가산점·패널티·비중 로직 리플레이 및 검증 (오프라인)."""
from __future__ import annotations

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
    return {
        "breakout_gate": {
            "pos52w_solo_min_breakout": 0.05,
            "tiers": [
                {"breakout": 0.25, "pos52w": 0.90},
                {"breakout": 0.20, "pos52w": 0.85},
            ],
        },
        "breakout_params": {"require_breakout_gate": True},
        "top_n": 20,
    }


def _apply_score_adjustments(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()
    base_col = "ScoreBase" if "ScoreBase" in out.columns else "Score"
    if base_col == "Score" and "ScoreBase" not in out.columns:
        out["ScoreBase"] = out["Score"]

    bonuses, penalties, penalty_reasons, convictions = [], [], [], []
    for _, row in out.iterrows():
        br = float(row.get("BreakoutScore", 0) or 0)
        pos = float(row.get("Pos52w", 0) or 0)
        flow = float(row.get("FlowScore", 0) or 0)
        mom = float(row.get("MomentumScore", 0) or 0)
        growth = float(row.get("GrowthScore", 0) or 0)
        bonus = compute_breakout_score_bonus(br, cfg)
        pen, preason = compute_near_high_penalty(pos, br, cfg)
        bonuses.append(bonus)
        penalties.append(pen)
        penalty_reasons.append(preason)
        convictions.append(
            compute_conviction_score(flow, mom, br, growth, cfg)
        )

    out["BreakoutBonus"] = bonuses
    out["NearHighPenalty"] = penalties
    out["penalty_reason"] = penalty_reasons
    out["ConvictionScore"] = convictions
    base = pd.to_numeric(out.get("ScoreBase", out["Score"]), errors="coerce").fillna(0)
    out["Score"] = (base + out["BreakoutBonus"] - out["NearHighPenalty"]).clip(lower=0, upper=1.0)

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


def _print_gate_tier_counts(df: pd.DataFrame, cfg: dict, label: str) -> None:
    tiers = cfg.get("breakout_gate", {}).get("tiers") or cfg.get("breakout_params", {}).get("gate_tiers", [])
    pos_solo = float(
        cfg.get("breakout_gate", {}).get("pos52w_solo_min_breakout")
        or cfg.get("breakout_params", {}).get("pos52w_solo_min_breakout", 0.05)
    )
    print(f"\n[{label}] tier별 통과 후보 수 (n={len(df)})")
    for i, t in enumerate(tiers, start=1):
        br = float(t.get("breakout", t.get("min_breakout_score", 0)))
        pos = float(t.get("pos52w", t.get("min_pos52w", 0)))
        n = (
            (df["BreakoutScore"] >= br)
            | ((df["Pos52w"] >= pos) & (df["BreakoutScore"] >= pos_solo))
        ).sum()
        print(f"  Tier {i}: Br>={br}, Pos>={pos} (solo Br>={pos_solo}) → {n}종")


def _penalty_applied(series: pd.Series) -> pd.Series:
    return series.apply(lambda x: isinstance(x, list) and len(x) > 0)


def _print_validation(top: pd.DataFrame, baseline_br_mean: float | None = None) -> None:
    n = len(top)
    if n == 0:
        print("\n[검증] 최종 후보 0건")
        return

    br_mean = top["BreakoutScore"].mean()
    bonus_n = (top["BreakoutBonus"] > 0).sum()
    penalty_n = _penalty_applied(top.get("penalty_reason", pd.Series(dtype=object))).sum()
    conv_top5 = top.nlargest(min(5, n), "ConvictionScore")["ConvictionScore"].mean()

    print("\n[검증] Top 품질")
    print(f"  BreakoutScore 평균: {br_mean:.3f}", end="")
    if baseline_br_mean is not None and baseline_br_mean > 0:
        pct = (br_mean / baseline_br_mean - 1) * 100
        ok_br = "✓" if pct >= 10 else "✗"
        print(f" (기준 {baseline_br_mean:.3f} 대비 {pct:+.1f}%, 목표 +10% {ok_br})")
    else:
        print()
    print(f"  FlowScore 평균: {top['FlowScore'].mean():.3f}")
    print(f"  MomentumScore 평균: {top['MomentumScore'].mean():.3f}")
    print(f"  BreakoutBonus 적용: {bonus_n}종 (목표 5+ {'✓' if bonus_n >= 5 else '✗'})")
    print(f"  NearHighPenalty 적용: {penalty_n}종 (목표 2~5 {'✓' if 2 <= penalty_n <= 5 else '✗'})")
    print(f"  ConvictionScore 상위5 평균: {conv_top5:.3f} (목표 0.80+ {'✓' if conv_top5 >= 0.80 else '✗'})")

    if "gate_tier" in top.columns:
        print(f"  gate_tier 분포: {dict(Counter(top['gate_tier']))}")

    print("\n[ConvictionScore 상위 5]")
    cols = ["Ticker", "Name", "ConvictionScore", "Score", "BreakoutScore", "target_weight"]
    show = [c for c in cols if c in top.columns]
    print(top.nlargest(5, "ConvictionScore")[show].to_string(index=False))

    if "target_weight" in top.columns:
        print("\n[종목별 최종 비중]")
        for _, r in top.sort_values("Score", ascending=False).iterrows():
            print(
                f"  {r.get('Ticker')} {r.get('Name')}: "
                f"w={float(r.get('target_weight', 0)):.4f} Score={float(r.get('Score', 0)):.4f}"
            )


def main() -> int:
    default_candidates = Path.home() / "Downloads/screener_candidates_full_20260622_KOSPI (2).json"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_candidates
    if not path.exists():
        print(f"파일 없음: {path}")
        return 1

    cfg = _load_cfg()
    rows = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(rows)
    for col in (
        "BreakoutScore", "Pos52w", "Score", "MomentumScore", "FlowScore",
        "GrowthScore", "FinScore", "SectorScore",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # 기존 Score에서 bonus/penalty 역산해 base 추정 (이전 스키마 호환)
    if "BreakoutBonus" in df.columns and "NearHighPenalty" not in df.columns:
        df["ScoreBase"] = df["Score"] - pd.to_numeric(df["BreakoutBonus"], errors="coerce").fillna(0)
    elif "NearHighPenalty" not in df.columns:
        df["ScoreBase"] = df["Score"]

    baseline_br_mean = df["BreakoutScore"].mean()
    print(f"입력: {path.name} ({len(df)}종)")
    _print_gate_tier_counts(df, cfg, "입력 풀")

    df = _apply_score_adjustments(df, cfg)
    gated, tier = filter_breakout_gate_tiered(df, cfg, min_count=int(cfg.get("top_n", 20)))
    print(f"\nTiered Fallback: applied_tier={tier}, 생존={len(gated)}종")

    top_n = int(cfg.get("top_n", 20))
    top = gated.sort_values("Score", ascending=False).head(top_n)

    portfolio_cfg = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
    per_cap = float(portfolio_cfg.get("per_ticker_max_weight", 0.075))
    ranked = allocate_portfolio_weights(
        top.to_dict(orient="records"),
        mode=str(portfolio_cfg.get("weight_mode", "rank_tier")),
        per_ticker_cap=per_cap,
        rank_tiers=portfolio_cfg.get("rank_tiers"),
        normalize_weights=bool(portfolio_cfg.get("normalize_weights", True)),
    )
    top = pd.DataFrame(ranked)

    _print_validation(top, baseline_br_mean=baseline_br_mean)

    if len(top) > 0 and "selection_summary" in top.columns:
        print("\nselection_summary 샘플:")
        for _, r in top.head(3).iterrows():
            print(f"  {r.get('Ticker')}: {r.get('selection_summary')}")

    wsum = sum(float(r.get("target_weight", 0) or 0) for r in ranked)
    wmax = max((float(r.get("target_weight", 0) or 0) for r in ranked), default=0)
    print(f"\nRank weight sum={wsum:.6f}, max={wmax:.4f} (cap={per_cap})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
