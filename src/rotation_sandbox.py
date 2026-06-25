"""
GPT RotationSandbox 제안 생성 (집행 없음).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import KST, OUTPUT_DIR, get_account_snapshot_cached

logger = logging.getLogger(__name__)


def _normalize_holdings(holdings: Any) -> List[Dict[str, Any]]:
    if isinstance(holdings, list):
        return [h for h in holdings if isinstance(h, dict)]
    return []


def _normalize_summary(summary: Any) -> Dict[str, Any]:
    return summary if isinstance(summary, dict) else {}


def build_rotation_sandbox_suggestions(
    cfg: Dict[str, Any],
    *,
    holdings: Optional[List[Dict[str, Any]]] = None,
    summary: Optional[Dict[str, Any]] = None,
    fixed_date: str = "",
    market: str = "KOSPI",
    load_snapshot: bool = True,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    회전(리밸런스) 샌드박스 SELL 제안 생성.
    반환: (suggestions, status_message)
    """
    ia = (cfg or {}).get("integrated_analysis", {}) or {}
    if not ia.get("log_gpt_rotation_suggestions", True):
        return [], "disabled"

    if holdings is None or summary is None:
        if not load_snapshot:
            return [], "no_snapshot"
        snap = get_account_snapshot_cached()
        if not isinstance(snap, (tuple, list)):
            return [], f"unexpected_snapshot_type:{type(snap).__name__}"
        if len(snap) < 2:
            return [], f"snapshot_unpack_len={len(snap)}"
        summary = _normalize_summary(snap[0])
        holdings = _normalize_holdings(snap[1])
    else:
        summary = _normalize_summary(summary)
        holdings = _normalize_holdings(holdings)

    total_value = float(summary.get("tot_evlu_amt", 0) or summary.get("nass_amt", 0) or 0)
    if total_value <= 0:
        return [], "zero_total_value"

    if not holdings:
        return [], "no_holdings"

    tp = (cfg or {}).get("trading_params", {}) or {}
    per_ticker_max = float(tp.get("per_ticker_max_weight", 0.15))
    min_conf = float(ia.get("min_confidence_for_rotation", 0.7))

    suggestions: List[Dict[str, Any]] = []
    for h in holdings:
        t = str(h.get("pdno", h.get("ticker", ""))).zfill(6)
        if not t or t == "000000":
            continue
        try:
            from asset_allocator import is_bond_etf
            if is_bond_etf(t, cfg):
                continue
        except ImportError:
            pass
        n = h.get("prdt_name", h.get("name", "N/A"))
        qty = int(h.get("hldg_qty", h.get("quantity", 0)) or 0)
        px = float(h.get("prpr", h.get("price", 0)) or 0)
        if qty <= 0 or px <= 0:
            continue
        cur_val = qty * px
        cur_w = cur_val / total_value if total_value > 0 else 0.0
        if cur_w > per_ticker_max:
            target_w = per_ticker_max
            overflow = min(0.5, max(0.0, cur_w - per_ticker_max))
            confidence = max(min_conf, min(1.0, 0.6 + overflow * 0.8))
            suggestions.append({
                "ticker": t,
                "name": n,
                "current_weight": round(cur_w, 6),
                "target_weight": round(target_w, 6),
                "decision": "SELL",
                "confidence": round(confidence, 3),
                "reasons": [
                    "overweight_breach",
                    f"current_weight={cur_w:.4f}",
                    f"max_weight={per_ticker_max:.4f}",
                ],
                "priority": round(cur_w - per_ticker_max, 6),
            })

    if not suggestions:
        return [], "no_actionable_candidates"

    return suggestions, "ok"


def save_rotation_sandbox_suggestions(
    suggestions: List[Dict[str, Any]],
    *,
    fixed_date: str,
    market: str,
) -> Path:
    rot_path = OUTPUT_DIR / f"gpt_rotations_{fixed_date}_{market}.json"
    json_payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(KST).isoformat(),
        "date": fixed_date,
        "market": market,
        "decision_source": "gpt_rotation_sandbox",
        "suggestions": suggestions,
    }
    with open(rot_path, "w", encoding="utf-8") as rf:
        json.dump(json_payload, rf, ensure_ascii=False, indent=2)
    return rot_path


def run_rotation_sandbox(
    cfg: Dict[str, Any],
    *,
    fixed_date: str,
    market: str = "KOSPI",
) -> None:
    """GPT analyzer 후처리: 제안 생성·저장·로그."""
    try:
        suggestions, status = build_rotation_sandbox_suggestions(
            cfg, fixed_date=fixed_date, market=market,
        )
        if status == "disabled":
            return
        if status == "zero_total_value":
            logger.info("[RotationSandbox] 총 평가액이 0이어서 제안 생성을 건너뜁니다.")
            return
        if status == "no_actionable_candidates":
            logger.info("[RotationSandbox] no actionable rotation candidates; skip")
            return
        if status != "ok":
            logger.warning("[RotationSandbox] skip status=%s", status)
            return

        rot_path = save_rotation_sandbox_suggestions(suggestions, fixed_date=fixed_date, market=market)
        logger.info("[RotationSandbox] proposals saved: %d → %s", len(suggestions), rot_path)
        top = sorted(suggestions, key=lambda s: s.get("priority", 0), reverse=True)[:3]
        for s in top:
            logger.info(
                "[RotationSandbox] SELL 제안: %s(%s) w=%.3f→%.3f conf=%.2f",
                s.get("name"), s.get("ticker"),
                s.get("current_weight", 0), s.get("target_weight", 0),
                s.get("confidence", 0),
            )
    except Exception as e:
        logger.warning("[RotationSandbox] 제안 생성 중 오류: %s", e, exc_info=True)
