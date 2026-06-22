#!/usr/bin/env python3
"""6/22 스크리너 결과 JSON으로 게이트·비중 로직 리플레이 (오프라인)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from screener_core import filter_breakout_gate_tiered
from portfolio_allocator import allocate_portfolio_weights


def main() -> int:
    default_path = Path.home() / "Downloads/screener_candidates_full_20260622_KOSPI (1).json"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    if not path.exists():
        print(f"파일 없음: {path}")
        return 1

    rows = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(rows)
    cfg = {
        "top_n": 20,
        "breakout_params": {
            "require_breakout_gate": True,
            "gate_tiers": [
                {"tier": 1, "min_breakout_score": 0.25, "min_pos52w": 0.90},
                {"tier": 2, "min_breakout_score": 0.20, "min_pos52w": 0.85},
                {"tier": 3, "min_breakout_score": 0.15, "min_pos52w": 0.80},
            ],
        },
    }

    # 원본 50종목이 없으므로 Top20만으로 tier별 통과 수 추정
    print(f"입력: {len(df)}종 (기존 최종 후보)")
    for tier_def in cfg["breakout_params"]["gate_tiers"]:
        t = tier_def["tier"]
        br, pos = tier_def["min_breakout_score"], tier_def["min_pos52w"]
        n = ((df["BreakoutScore"] >= br) | (df["Pos52w"] >= pos)).sum()
        print(f"  Tier {t} (Br>={br}, Pos>={pos}): {n}종 통과")

    gated, tier = filter_breakout_gate_tiered(df, cfg, min_count=20)
    print(f"\nTiered Fallback 적용: gate_tier={tier}, 생존={len(gated)}종")
    if not gated.empty:
        print(gated[["Ticker", "Name", "Score", "BreakoutScore", "Pos52w", "gate_tier", "gate_reason"]].to_string(index=False))

    ranked = allocate_portfolio_weights(
        gated.sort_values("Score", ascending=False).to_dict(orient="records"),
        mode="rank_tier",
        rank_tiers=[
            {"rank_from": 1, "rank_to": 5, "weight": 0.07},
            {"rank_from": 6, "rank_to": 10, "weight": 0.05},
            {"rank_from": 11, "rank_to": 20, "weight": 0.035},
        ],
        normalize_weights=True,
    )
    wsum = sum(r["target_weight"] for r in ranked)
    print(f"\nRank weight (normalized): sum={wsum:.6f}")
    for i, r in enumerate(sorted(ranked, key=lambda x: -x["Score"]), 1):
        print(f"  {i:2}. {r['Ticker']} w={r['target_weight']:.4f} Score={r['Score']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
