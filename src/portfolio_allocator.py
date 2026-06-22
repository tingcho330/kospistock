#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""포트폴리오 목표 비중 배분 (equal / score_proportional)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def allocate_portfolio_weights(
    candidates: List[Dict[str, Any]],
    *,
    mode: str = "equal",
    per_ticker_cap: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    후보 리스트에 target_weight 필드를 부여해 반환.

    mode:
      - equal: 1/N
      - score_proportional: Score 비례 (합=1), per_ticker_cap 적용 후 재정규화
    """
    if not candidates:
        return []

    n = len(candidates)
    out: List[Dict[str, Any]] = [dict(c) for c in candidates]

    if mode == "score_proportional":
        scores = [max(0.0, float(c.get("Score", 0) or 0)) for c in out]
        total = sum(scores)
        if total <= 0:
            weights = [1.0 / n] * n
        else:
            weights = [s / total for s in scores]
    else:
        weights = [1.0 / n] * n

    if per_ticker_cap is not None and per_ticker_cap > 0:
        cap = float(per_ticker_cap)
        weights = [min(w, cap) for w in weights]
        wsum = sum(weights)
        if wsum > 0:
            weights = [w / wsum for w in weights]

    for c, w in zip(out, weights):
        c["target_weight"] = round(float(w), 6)
    return out
