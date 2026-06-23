# src/portfolio_allocator.py
"""스크리너 리플레이 전용 — 주식 후보별 target_weight 산정 (자산군 70:20:10 아님)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_weight(w: float, min_w: float, max_w: float) -> float:
    return max(min_w, min(max_w, w))


def allocate_portfolio_weights(
    candidates: List[Dict[str, Any]],
    *,
    mode: str = "conviction",
    per_ticker_cap: float = 0.075,
    min_ticker_weight: float = 0.02,
    max_ticker_weight: Optional[float] = None,
    rank_tiers: Optional[List[Dict[str, Any]]] = None,
    normalize_weights: bool = True,
) -> List[Dict[str, Any]]:
    if not candidates:
        return []

    max_w = float(max_ticker_weight if max_ticker_weight is not None else per_ticker_cap)
    min_w = float(min_ticker_weight)
    cap = float(per_ticker_cap)
    mode_l = str(mode or "conviction").lower()

    ranked = [dict(c) for c in candidates]
    n = len(ranked)

    if mode_l == "equal":
        raw = 1.0 / n
        for row in ranked:
            row["target_weight"] = _clamp_weight(raw, min_w, min(max_w, cap))
    elif mode_l == "score_proportional":
        scores = [max(0.0, _to_float(r.get("Score", 0))) for r in ranked]
        total = sum(scores)
        if total <= 0:
            raw = 1.0 / n
            weights = [raw] * n
        else:
            weights = [s / total for s in scores]
        for row, w in zip(ranked, weights):
            row["target_weight"] = _clamp_weight(w, min_w, min(max_w, cap))
    elif mode_l == "rank_tier":
        tiers = rank_tiers or []
        for i, row in enumerate(ranked):
            rank = i + 1
            w = cap
            for tier in tiers:
                r_from = int(tier.get("rank_from", 0))
                r_to = int(tier.get("rank_to", 0))
                if r_from <= rank <= r_to:
                    w = float(tier.get("weight", cap))
                    break
            row["target_weight"] = _clamp_weight(w, min_w, min(max_w, cap))
    else:
        # conviction (default)
        convictions = [max(0.0, _to_float(r.get("ConvictionScore", r.get("Score", 0)))) for r in ranked]
        total = sum(convictions)
        if total <= 0:
            raw = 1.0 / n
            weights = [raw] * n
        else:
            weights = [c / total for c in convictions]
        for row, w in zip(ranked, weights):
            row["target_weight"] = _clamp_weight(w, min_w, min(max_w, cap))

    if normalize_weights:
        total_w = sum(_to_float(r.get("target_weight", 0)) for r in ranked)
        if total_w > 0:
            scale = min(1.0, total_w) / total_w if total_w > 1.0 else 1.0
            if total_w > 1.0:
                for row in ranked:
                    row["target_weight"] = round(_to_float(row.get("target_weight", 0)) * scale, 6)

    return ranked
