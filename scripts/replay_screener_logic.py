#!/usr/bin/env python3
"""스크리너 게이트·가산점·비중 로직 리플레이 및 검증 (오프라인)."""
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


def _print_validation(top: pd.DataFrame) -> None:
    n = len(top)
    if n == 0:
        print("\n[검증] 최종 후보 0건")
        return
    br_ge_20 = (top["BreakoutScore"] >= 0.20).mean() * 100
    pos_ge_90 = (top["Pos52w"] >= 0.90).mean() * 100
    gt = Counter(top.get("gate_tier", pd.Series(dtype=int)))
    print("\n[검증] Top20 품질")
    print(f"  gate_tier 분포: {dict(gt)}")
    print(f"  BreakoutScore 평균: {top['BreakoutScore'].mean():.3f}")
    print(f"  Pos52w 평균: {top['Pos52w'].mean():.3f}")
    print(f"  Breakout>=0.20 비율: {br_ge_20:.1f}% (목표 70%+)")
    print(f"  Pos52w>=0.90 비율: {pos_ge_90:.1f}% (목표 50%+)")
    ok_br = "✓" if br_ge_20 >= 70 else "✗"
    ok_pos = "✓" if pos_ge_90 >= 50 else "✗"
    print(f"  목표 달성: Breakout {ok_br}  Pos52w {ok_pos}")


def main() -> int:
    default_scores = Path.home() / "Downloads/screener_scores_20260622_KOSPI.json"
    default_candidates = Path.home() / "Downloads/screener_candidates_full_20260622_KOSPI (2).json"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        default_scores if default_scores.exists() else default_candidates
    )
    if not path.exists():
        print(f"파일 없음: {path}")
        return 1

    cfg = _load_cfg()
    rows = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(rows)
    for col in ("BreakoutScore", "Pos52w", "Score", "MomentumScore", "FlowScore"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    print(f"입력: {path.name} ({len(df)}종)")
    _print_gate_tier_counts(df, cfg, "입력 풀")

    gated, tier = filter_breakout_gate_tiered(df, cfg, min_count=int(cfg.get("top_n", 20)))
    print(f"\nTiered Fallback: applied_tier={tier}, 생존={len(gated)}종")
    if not gated.empty:
        print(gated[["Ticker", "Name", "Score", "BreakoutScore", "Pos52w", "gate_tier", "gate_reason"]].head(25).to_string(index=False))

    # Breakout bonus preview on gated pool
    if "BreakoutScore" in gated.columns:
        gated = gated.copy()
        gated["BreakoutBonus"] = gated["BreakoutScore"].apply(
            lambda b: compute_breakout_score_bonus(float(b), cfg)
        )
        gated["ScoreAdj"] = (gated["Score"] + gated["BreakoutBonus"]).clip(upper=1.0)

    top_n = int(cfg.get("top_n", 20))
    top = gated.sort_values("ScoreAdj" if "ScoreAdj" in gated.columns else "Score", ascending=False).head(top_n)
    _print_validation(top)

    if len(top) > 0 and "rank_reason" in top.columns:
        print("\nrank_reason 샘플:")
        for _, r in top.head(5).iterrows():
            print(f"  {r.get('Ticker')} {r.get('Name')}: {r.get('rank_reason')}")

    ranked = allocate_portfolio_weights(
        top.to_dict(orient="records"),
        mode="rank_tier",
        rank_tiers=cfg.get("portfolio", {}).get("rank_tiers"),
        normalize_weights=True,
    )
    wsum = sum(r.get("target_weight", 0) for r in ranked)
    print(f"\nRank weight sum={wsum:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
