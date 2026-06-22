#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""포트폴리오 목표 비중 배분 (equal / score_proportional / rank_tier)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _rank_tier_weight(rank: int, tiers: List[Dict[str, Any]]) -> float:
    for tier in tiers:
        r_from = int(tier.get("rank_from", 1))
        r_to = int(tier.get("rank_to", rank))
        if r_from <= rank <= r_to:
            return float(tier.get("weight", 0.05))
    return 0.05


def allocate_portfolio_weights(
    candidates: List[Dict[str, Any]],
    *,
    mode: str = "equal",
    per_ticker_cap: Optional[float] = None,
    rank_tiers: Optional[List[Dict[str, Any]]] = None,
    normalize_weights: bool = True,
) -> List[Dict[str, Any]]:
    """
    후보 리스트에 target_weight 필드를 부여해 반환.

    mode:
      - equal: 1/N
      - score_proportional: Score 비례 (합=1), per_ticker_cap 적용 후 재정규화
      - rank_tier: Score 순위 구간별 고정 비중 (normalize_weights로 100% 정규화)
    """
    if not candidates:
        return []

    n = len(candidates)
    out: List[Dict[str, Any]] = [dict(c) for c in candidates]

    if mode == "rank_tier":
        tiers = rank_tiers or [
            {"rank_from": 1, "rank_to": 5, "weight": 0.07},
            {"rank_from": 6, "rank_to": 10, "weight": 0.05},
            {"rank_from": 11, "rank_to": 20, "weight": 0.035},
        ]
        ranked = sorted(out, key=lambda c: -(float(c.get("Score", 0) or 0)))
        raw_weights = [_rank_tier_weight(i + 1, tiers) for i in range(len(ranked))]
        if normalize_weights:
            wsum = sum(raw_weights)
            weights = [w / wsum for w in raw_weights] if wsum > 0 else [1.0 / n] * n
        else:
            weights = raw_weights
        ticker_weight = {
            str(c.get("Ticker", "")): weights[i]
            for i, c in enumerate(ranked)
        }
        for c in out:
            t = str(c.get("Ticker", ""))
            c["target_weight"] = round(float(ticker_weight.get(t, weights[-1] if weights else 1.0 / n)), 6)
    elif mode == "score_proportional":
        scores = [max(0.0, float(c.get("Score", 0) or 0)) for c in out]
        total = sum(scores)
        if total <= 0:
            weights = [1.0 / n] * n
        else:
            weights = [s / total for s in scores]
        for c, w in zip(out, weights):
            c["target_weight"] = round(float(w), 6)
    else:
        weights = [1.0 / n] * n
        for c, w in zip(out, weights):
            c["target_weight"] = round(float(w), 6)

    if per_ticker_cap is not None and per_ticker_cap > 0 and mode != "rank_tier":
        cap = float(per_ticker_cap)
        weights = [min(float(c.get("target_weight", 0)), cap) for c in out]
        wsum = sum(weights)
        if wsum > 0:
            weights = [w / wsum for w in weights]
        for c, w in zip(out, weights):
            c["target_weight"] = round(float(w), 6)

    return out
