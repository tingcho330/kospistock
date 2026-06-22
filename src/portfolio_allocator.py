#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""포트폴리오 목표 비중 배분 (equal / score_proportional / rank_tier / conviction)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _rank_tier_weight(rank: int, tiers: List[Dict[str, Any]]) -> float:
    for tier in tiers:
        r_from = int(tier.get("rank_from", 1))
        r_to = int(tier.get("rank_to", rank))
        if r_from <= rank <= r_to:
            return float(tier.get("weight", 0.05))
    return 0.05


def _apply_per_ticker_cap(weights: List[float], cap: float) -> List[float]:
    """상한 초과분을 미달 종목에 비례 재분배 (반복)."""
    if cap <= 0 or not weights:
        return weights
    w = [float(x) for x in weights]
    for _ in range(len(w) + 2):
        over_idx = [i for i, x in enumerate(w) if x > cap + 1e-12]
        if not over_idx:
            break
        excess = sum(w[i] - cap for i in over_idx)
        for i in over_idx:
            w[i] = cap
        under_idx = [i for i, x in enumerate(w) if x < cap - 1e-12]
        if not under_idx or excess <= 0:
            break
        under_sum = sum(w[i] for i in under_idx)
        if under_sum <= 0:
            share = excess / len(under_idx)
            for i in under_idx:
                w[i] += share
        else:
            for i in under_idx:
                w[i] += excess * (w[i] / under_sum)
    wsum = sum(w)
    if wsum > 0:
        w = [x / wsum for x in w]
    return w


def _apply_weight_bounds(
    weights: List[float],
    min_w: float,
    max_w: float,
) -> List[float]:
    """min/max 클립 후 합=1 재정규화 (반복)."""
    if not weights:
        return weights
    min_w = max(0.0, float(min_w))
    max_w = max(min_w, float(max_w))
    w = [float(x) for x in weights]
    n = len(w)
    for _ in range(n + 3):
        w = _apply_per_ticker_cap(w, max_w)
        below = [i for i, x in enumerate(w) if x < min_w - 1e-12]
        if not below:
            break
        deficit = sum(min_w - w[i] for i in below)
        for i in below:
            w[i] = min_w
        above = [i for i, x in enumerate(w) if w[i] > min_w + 1e-12]
        if not above or deficit <= 0:
            break
        above_sum = sum(w[i] for i in above)
        if above_sum <= 0:
            break
        for i in above:
            w[i] = max(min_w, w[i] - deficit * (w[i] / above_sum))
        wsum = sum(w)
        if wsum > 0:
            w = [x / wsum for x in w]
    return w


def allocate_portfolio_weights(
    candidates: List[Dict[str, Any]],
    *,
    mode: str = "equal",
    per_ticker_cap: Optional[float] = None,
    min_ticker_weight: Optional[float] = None,
    max_ticker_weight: Optional[float] = None,
    rank_tiers: Optional[List[Dict[str, Any]]] = None,
    normalize_weights: bool = True,
) -> List[Dict[str, Any]]:
    """
    후보 리스트에 target_weight 필드를 부여해 반환.

    mode:
      - equal: 1/N
      - score_proportional: Score 비례 (합=1)
      - rank_tier: Score 순위 구간별 고정 비중
      - conviction: ConvictionScore 비례 (min/max_weight 적용)
    """
    if not candidates:
        return []

    n = len(candidates)
    out: List[Dict[str, Any]] = [dict(c) for c in candidates]
    max_w = max_ticker_weight if max_ticker_weight is not None else per_ticker_cap
    min_w = min_ticker_weight

    if mode == "conviction":
        convictions = [
            max(0.0, float(c.get("ConvictionScore", 0) or 0))
            for c in out
        ]
        total = sum(convictions)
        if total <= 0:
            weights = [1.0 / n] * n
        else:
            weights = [c / total for c in convictions]
        floor = float(min_w) if min_w is not None else 0.0
        cap = float(max_w) if max_w is not None and max_w > 0 else 1.0
        if floor > 0 or (max_w is not None and cap < 1.0):
            weights = _apply_weight_bounds(weights, floor, cap)
        for c, w in zip(out, weights):
            c["target_weight"] = round(float(w), 6)
    elif mode == "rank_tier":
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

    if mode != "conviction" and max_w is not None and max_w > 0:
        weights = [float(c.get("target_weight", 0)) for c in out]
        weights = _apply_per_ticker_cap(weights, float(max_w))
        for c, w in zip(out, weights):
            c["target_weight"] = round(float(w), 6)

    return out
