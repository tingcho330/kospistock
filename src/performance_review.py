#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
일별 전략·시스템 성능 리뷰 자동 생성.

Usage:
  PYTHONPATH=/app/src python /app/src/performance_review.py --date 20260625 --market KOSPI
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import (
    KST,
    OUTPUT_DIR,
    extract_kis_official_summary,
    load_config,
    setup_logging,
)
from settings import settings
from recorder import classify_trade_record, is_real_kis_trade_record

logger = logging.getLogger("PerformanceReview")

GPT_ACTION_MAP = {
    "매수": "BUY",
    "보류": "HOLD",
    "미진입": "REJECT",
}
TRACK_HORIZONS = (1, 3, 5, 10)

_SUMMARY_KEY_MAP = {
    "total_assets": [
        "nass_amt", "total_assets", "total_value", "net_assets", "tot_asst_amt",
    ],
    "tot_evlu_amt": [
        "tot_evlu_amt", "total_eval_amt", "tot_evlu", "tot_eval", "total_value",
    ],
    "dnca_tot_amt": ["dnca_tot_amt", "cash_amt", "dnca_avl_amt"],
    "prvs_rcdl_excc_amt": ["prvs_rcdl_excc_amt", "d2_excc_amt", "rcdl_excc_amt_d2"],
    "nxdy_excc_amt": ["nxdy_excc_amt", "d1_excc_amt", "rcdl_excc_amt_d1"],
    "rlzt_pfls": ["rlzt_pfls", "realized_pnl"],
}

_RECONCILE_HOLDING_RE = re.compile(
    r"\[RECONCILE_BY_HOLDING\]\s+order_id=(\S+)\s+ticker=(\d+)",
    re.I,
)
_RUN_ID_IN_LINE_RE = re.compile(r"\[(\d{8}-\d{6})\]")
_PIPELINE_EXCLUDE_SUBSTR = (
    "performance_review",
    "account",
)

# 수동 분할 파이프라인 로그 스테이지
_MANUAL_STAGE_ORDER = (
    "screener",
    "news",
    "gpt",
    "trader",
    "resume_trader",
    "resume_from_news",
    "resume_from_gpt",
    "resume_from_trader",
    "resume_from_reconciler",
    "resume_from_performance",
    "full_from_screener",
    "reconcile",
    "reconcile_pending_sell",
)
_PRIMARY_MANUAL_STAGES = frozenset({
    "screener", "news", "gpt", "trader", "resume_trader",
    "resume_from_news", "resume_from_gpt", "resume_from_trader",
    "resume_from_reconciler", "resume_from_performance",
})
_CORE_MANUAL_STAGES = frozenset({"screener", "news", "gpt", "trader"})
_RESUME_FROM_STAGES = (
    "resume_from_news",
    "resume_from_gpt",
    "resume_from_trader",
    "resume_from_reconciler",
    "resume_from_performance",
)
_RESUME_EXECUTION_PRIORITY = (
    ("resume_from_news", "resume_from_news_run"),
    ("resume_from_gpt", "resume_from_gpt_run"),
    ("resume_from_trader", "resume_from_trader_run"),
    ("resume_from_reconciler", "resume_from_reconciler_run"),
    ("resume_from_performance", "resume_from_performance_run"),
    ("resume_trader", "resume_trader_run"),
)
_RESUME_EXEC_METHODS = frozenset(m for _, m in _RESUME_EXECUTION_PRIORITY)
_FULL_PIPELINE_PREFIX = "manual_full_pipeline_"


def _norm_date(s: Optional[str]) -> str:
    if not s:
        return datetime.now(KST).strftime("%Y%m%d")
    t = re.sub(r"\D", "", str(s))
    if len(t) != 8:
        raise ValueError(f"Invalid date: {s!r} (expected YYYYMMDD)")
    return t


def _norm_ticker(t: Any) -> str:
    return str(t or "").strip().zfill(6)


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _coalesce_runtime(*values: Any) -> Optional[float]:
    """첫 번째 유효 runtime 값 반환 (None/빈 문자열 스킵)."""
    for v in values:
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_runtime_cell(value: Any) -> str:
    """Markdown 표 셀용 runtime (초). None → '-'."""
    if value is None or value == "":
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num == int(num):
        return str(int(num))
    return str(round(num, 1))


def _fmt_runtime_bullet(value: Any) -> str:
    """Markdown bullet용 runtime. None → '-', 유효값 → '121.8s'."""
    cell = _fmt_runtime_cell(value)
    return cell if cell == "-" else f"{cell}s"


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("JSON 로드 실패 %s: %s", path, e)
        return None


def _artifact_path(name: str, date_str: str, market: str) -> Path:
    if name in ("balance", "summary", "summary_rlz"):
        return OUTPUT_DIR / f"{name}_{date_str}.json"
    return OUTPUT_DIR / f"{name}_{date_str}_{market}.json"


def _parse_balance_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []
    if isinstance(data, list):
        return data
    return []


def _denest_first_record(data_list: Any) -> Dict[str, Any]:
    """payload['data'][0]가 {\"0\": {...}} 형태일 수 있어 전개."""
    if not data_list or not isinstance(data_list, list):
        return {}
    rec = data_list[0]
    if isinstance(rec, dict):
        if 0 in rec and isinstance(rec[0], dict):
            return rec[0]
        if "0" in rec and isinstance(rec["0"], dict):
            return rec["0"]
        return rec
    return {}


def _pick_int_from_dict(d: Dict[str, Any], candidates: List[str]) -> Optional[int]:
    for key in candidates:
        if key in d and d[key] not in (None, ""):
            val = _safe_int(d[key])
            if val != 0 or str(d[key]).strip() in ("0", "0.0"):
                return val
    return None


def _collect_summary_sources(payload: Any) -> List[Dict[str, Any]]:
    """summary/balance JSON에서 계좌 요약 dict 후보 수집."""
    sources: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return sources
    nested_keys = (
        "summary", "account_summary", "raw_summary", "account", "output2", "output",
    )
    for key in nested_keys:
        val = payload.get(key)
        if isinstance(val, dict):
            sources.append(val)
        elif isinstance(val, list) and val:
            row = _denest_first_record(val)
            if row:
                sources.append(row)
    data = payload.get("data")
    if isinstance(data, list) and data:
        row = _denest_first_record(data)
        if row:
            sources.append(row)
    top = {
        k: v for k, v in payload.items()
        if k not in ("comments", "data", "status") and not isinstance(v, (list, dict))
    }
    if top:
        sources.append(top)
    return sources


def _extract_account_summary(
    summary_data: Any,
    balance_data: Any = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """balance/summary JSON에서 계좌 요약 필드 추출. missing keys → warnings."""
    warnings: List[str] = []
    sources: List[Dict[str, Any]] = []
    sources.extend(_collect_summary_sources(summary_data))
    sources.extend(_collect_summary_sources(balance_data))

    out: Dict[str, Any] = {}
    for field, keys in _SUMMARY_KEY_MAP.items():
        found = None
        for src in sources:
            val = _pick_int_from_dict(src, keys)
            if val is not None:
                found = val
                break
        if found is not None:
            out[field] = found

    if not out.get("total_assets"):
        # tot_evlu_amt 또는 balance 평가합으로 폴백
        if out.get("tot_evlu_amt"):
            out["total_assets"] = out["tot_evlu_amt"]
        else:
            bal_rows = _parse_balance_rows(balance_data)
            evlu_sum = sum(_safe_int(r.get("evlu_amt")) for r in bal_rows)
            if evlu_sum > 0:
                out["total_assets"] = evlu_sum
                out["tot_evlu_amt"] = evlu_sum

    if not out.get("tot_evlu_amt") and out.get("total_assets"):
        out["tot_evlu_amt"] = out["total_assets"]

    official = extract_kis_official_summary(
        next((s for s in sources if s), {}),
    )
    if not out.get("total_assets") and official.get("net_assets"):
        out["total_assets"] = official["net_assets"]
    if not out.get("tot_evlu_amt") and official.get("tot_evlu_amt"):
        out["tot_evlu_amt"] = official["tot_evlu_amt"]
    if not out.get("dnca_tot_amt") and official.get("dnca_tot_amt"):
        out["dnca_tot_amt"] = official["dnca_tot_amt"]
    if not out.get("prvs_rcdl_excc_amt") and official.get("prvs_rcdl_excc_amt"):
        out["prvs_rcdl_excc_amt"] = official["prvs_rcdl_excc_amt"]
    if not out.get("nxdy_excc_amt") and official.get("nxdy_excc_amt"):
        out["nxdy_excc_amt"] = official["nxdy_excc_amt"]

    if not out.get("total_assets"):
        warnings.append("total_assets_not_found")

    return out, list(dict.fromkeys(warnings))


class _TradeRecordAdapter:
    """sqlite dict row → recorder.classify_trade_record 호환."""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data.get(name)


def _trading_env() -> str:
    try:
        return str(getattr(settings, "_config", {}).get("trading_environment", "") or "").lower()
    except Exception:
        return str(__import__("os").getenv("TRADING_ENVIRONMENT", "") or "").lower()


def _is_paper_db_only_row(row: Dict[str, Any]) -> bool:
    return classify_trade_record(
        _TradeRecordAdapter(row), trading_environment=_trading_env()
    ) == "paper_db_only"


def _is_actual_kis_order_row(row: Dict[str, Any]) -> bool:
    return is_real_kis_trade_record(
        _TradeRecordAdapter(row), trading_environment=_trading_env()
    )


def _trade_row_score(row: Dict[str, Any]) -> Tuple[int, int]:
    status = str(row.get("order_status") or "").lower()
    oid = str(row.get("order_id") or "").strip()
    score = 0
    if oid:
        score += 100
    if status in ("executed", "completed"):
        score += 50
    elif status == "partial":
        score += 30
    elif status == "pending":
        score += 10
    return score, _safe_int(row.get("id"))


def _normalize_trade_rows(
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """KIS 실주문만 dedupe, paper_db_only는 별도 반환."""
    paper_db_only: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

    for row in rows:
        if _is_paper_db_only_row(row):
            paper_db_only.append(row)
            continue
        key = (_norm_ticker(row.get("ticker")), str(row.get("action") or "").upper())
        if not key[0] or not key[1]:
            continue
        groups[key].append(row)

    kis_rows: List[Dict[str, Any]] = []
    for group in groups.values():
        kis_rows.append(max(group, key=_trade_row_score))
    kis_rows.sort(key=lambda r: (r.get("timestamp") or "", _safe_int(r.get("id"))))
    return kis_rows, paper_db_only


def _bond_etf_tickers() -> set:
    aa = settings.asset_allocation if hasattr(settings, "asset_allocation") else {}
    tickers: set = set()
    if isinstance(aa, dict):
        for item in aa.get("bond_etfs") or []:
            if isinstance(item, dict) and item.get("ticker"):
                tickers.add(_norm_ticker(item["ticker"]))
            elif item:
                tickers.add(_norm_ticker(item))
    return tickers


def _asset_class(ticker: str, bond_tickers: set) -> str:
    return "bond_etf" if _norm_ticker(ticker) in bond_tickers else "stock"


def _parse_reconcile_holding_map(log_text: str) -> Dict[str, str]:
    """order_id → holding_fallback."""
    out: Dict[str, str] = {}
    for m in _RECONCILE_HOLDING_RE.finditer(log_text):
        out[str(m.group(1)).strip()] = "holding_fallback"
    return out


def _is_pipeline_log_candidate(path: Path, date_str: str) -> bool:
    """full_pipeline / integrated pipeline 계열 로그만 latest_pipeline 후보."""
    name = path.name.lower()
    if not name.endswith(".log") or date_str not in name:
        return False
    for bad in _PIPELINE_EXCLUDE_SUBSTR:
        if bad in name:
            return False
    if "screener" in name and "pipeline" not in name:
        return False
    if "full_pipeline" in name or "integrated_pipeline" in name:
        return True
    if name.startswith(f"pipeline_{date_str}") or name.startswith(f"pipeline-{date_str}"):
        return True
    if re.search(rf"(?:^|[_-])pipeline[_-]{date_str}", name):
        return True
    return False


def _extract_log_run_ts(path: Path, date_str: str) -> Optional[str]:
    """파일명에서 YYYYMMDD-HHMMSS 추출."""
    m = re.search(rf"{date_str}[_-](\d{{6}})", path.name)
    if not m:
        return None
    return f"{date_str}-{m.group(1)}"


def _extract_filename_run_ts(path: Path, date_str: str) -> Optional[str]:
    """_extract_log_run_ts 별칭 (하위 호환)."""
    return _extract_log_run_ts(path, date_str)


def _classify_manual_log_stage(filename: str, date_str: str) -> Optional[str]:
    """수동 파이프라인 로그 파일명 → 스테이지."""
    name = filename.lower()
    if not name.endswith(".log") or date_str not in name:
        return None
    stage_prefixes = [
        ("reconcile_pending_sell", "manual_order_reconcile_pending_sell_"),
        ("reconcile", "manual_order_reconcile_"),
        ("resume_from_performance", "manual_resume_from_performance_"),
        ("resume_from_reconciler", "manual_resume_from_reconciler_"),
        ("resume_from_trader", "manual_resume_from_trader_"),
        ("resume_from_gpt", "manual_resume_from_gpt_"),
        ("resume_from_news", "manual_resume_from_news_"),
        ("full_from_screener", "manual_full_from_screener_"),
        ("screener", "manual_screener_"),
        ("news", "manual_news_collector_"),
        ("gpt", "manual_gpt_after_news_"),
        ("trader", "manual_trader_after_gpt_"),
        ("resume_trader", "manual_resume_trader_"),
        ("full_pipeline", "manual_full_pipeline_"),
    ]
    for stage, prefix in stage_prefixes:
        if name.startswith(prefix):
            return stage
    return None


def _is_split_manual_log(path: Path, date_str: str) -> bool:
    stage = _classify_manual_log_stage(path.name, date_str)
    return stage is not None and stage not in ("full_pipeline", "full_from_screener")


def _is_full_pipeline_log(path: Path, date_str: str) -> bool:
    stage = _classify_manual_log_stage(path.name, date_str)
    if stage in ("full_pipeline", "full_from_screener"):
        return True
    name = path.name.lower()
    if not name.endswith(".log") or date_str not in name:
        return False
    if "integrated_pipeline" in name:
        return True
    if name.startswith(f"manual_full_from_screener_{date_str}"):
        return True
    if name.startswith(f"pipeline_{date_str}") or name.startswith(f"pipeline-{date_str}"):
        return True
    if re.search(rf"(?:^|[_-])pipeline[_-]{date_str}", name):
        return True
    return False


def _discover_date_logs(date_str: str) -> List[Path]:
    """output 디렉토리에서 날짜 포함 로그 검색."""
    found: List[Path] = []
    seen: set = set()
    for base in (OUTPUT_DIR, OUTPUT_DIR / "debug"):
        if not base.is_dir():
            continue
        for p in base.glob("*.log"):
            if p in seen:
                continue
            if date_str in p.name:
                found.append(p)
                seen.add(p)
    return found


def _collect_manual_stage_logs(date_str: str, log_files: List[Path]) -> Dict[str, Path]:
    """같은 날짜 수동 파이프라인 — 스테이지별 최신 로그 파일."""
    latest: Dict[str, Tuple[str, Path]] = {}
    for p in log_files:
        stage = _classify_manual_log_stage(p.name, date_str)
        if not stage or stage in ("full_pipeline", "full_from_screener"):
            continue
        run_ts = _extract_log_run_ts(p, date_str)
        if not run_ts:
            continue
        if stage not in latest or run_ts > latest[stage][0]:
            latest[stage] = (run_ts, p)
    return {k: v[1] for k, v in latest.items()}


def _build_daily_pipeline_group(
    date_str: str,
    stage_files: Dict[str, Path],
) -> Tuple[List[Path], Optional[str], str]:
    """
    당일 screener/news/gpt/trader 분할 로그 또는 screener+resume_from_* 묶음.
    resume_trader는 당일 파이프라인 품질 그룹에서 제외한다.
    """
    resume_present = [s for s in _RESUME_FROM_STAGES if s in stage_files]
    if resume_present:
        resume_stage = resume_present[0]
        files: List[Path] = []
        if "screener" in stage_files:
            files.append(stage_files["screener"])
        files.append(stage_files[resume_stage])
        for aux in ("reconcile", "reconcile_pending_sell"):
            if aux in stage_files:
                files.append(stage_files[aux])
        files = sorted(files, key=lambda p: p.name)
        core_ts = [
            _extract_log_run_ts(p, date_str) or ""
            for p in files
        ]
        group_ts = max(core_ts) if core_ts else None
        group_id = f"{date_str}-daily-{group_ts}" if group_ts else f"{date_str}-daily"
        logger.info(
            "[PERF_LOG_GROUP_CANDIDATE] date=%s group_id=%s files=%s",
            date_str, group_id, ",".join(f.name for f in files),
        )
        for p in files:
            st = _classify_manual_log_stage(p.name, date_str)
            if st:
                logger.info("[PERF_LOG_GROUP_STAGE] stage=%s file=%s", st, p.name)
        return files, group_ts, "split_manual_pipeline_logs"

    daily_stages = {k: v for k, v in stage_files.items() if k != "resume_trader"}
    core = {s for s in _CORE_MANUAL_STAGES if s in daily_stages}
    if not core:
        return [], None, "incomplete"

    files = []
    for stage in _MANUAL_STAGE_ORDER:
        if stage in ("resume_trader",) or stage.startswith("resume_from_") or stage == "full_from_screener":
            continue
        if stage in daily_stages:
            files.append(daily_stages[stage])

    core_ts = [
        _extract_log_run_ts(daily_stages[s], date_str) or ""
        for s in core
    ]
    group_ts = max(core_ts) if core_ts else None
    group_id = f"{date_str}-daily-{group_ts}" if group_ts else f"{date_str}-daily"
    logger.info(
        "[PERF_LOG_GROUP_CANDIDATE] date=%s group_id=%s files=%s",
        date_str, group_id, ",".join(f.name for f in files),
    )
    for p in files:
        st = _classify_manual_log_stage(p.name, date_str)
        if st:
            logger.info("[PERF_LOG_GROUP_STAGE] stage=%s file=%s", st, p.name)
    return files, group_ts, "split_manual_pipeline_logs"


def _build_latest_execution_group(
    date_str: str,
    stage_files: Dict[str, Path],
) -> Tuple[List[Path], Optional[str], str]:
    """
    가장 최근 실행 단위 로그 묶음 (resume_from_* / resume_trader 우선).
    """
    for stage, method in _RESUME_EXECUTION_PRIORITY:
        if stage not in stage_files:
            continue
        run_ts = _extract_log_run_ts(stage_files[stage], date_str) or ""
        files = [stage_files[stage]]
        for aux in ("reconcile", "reconcile_pending_sell"):
            if aux in stage_files:
                aux_ts = _extract_log_run_ts(stage_files[aux], date_str) or ""
                if aux_ts >= run_ts:
                    files.append(stage_files[aux])
        files = sorted(files, key=lambda p: p.name)
        group_id = f"{date_str}-resume-{run_ts}"
        logger.info(
            "[PERF_LOG_GROUP_CANDIDATE] date=%s group_id=%s files=%s",
            date_str, group_id, ",".join(f.name for f in files),
        )
        for p in files:
            st = _classify_manual_log_stage(p.name, date_str)
            if st:
                logger.info("[PERF_LOG_GROUP_STAGE] stage=%s file=%s", st, p.name)
        return files, run_ts or None, method

    primary_present = {s for s in _PRIMARY_MANUAL_STAGES if s in stage_files}
    if not primary_present:
        return [], None, "incomplete"

    core = {s for s in _CORE_MANUAL_STAGES if s in stage_files}
    if not core:
        return [], None, "incomplete"

    files = []
    for stage in _MANUAL_STAGE_ORDER:
        if stage in stage_files and stage not in _RESUME_FROM_STAGES and stage != "full_from_screener":
            if stage == "resume_trader":
                continue
            files.append(stage_files[stage])
    core_ts = [_extract_log_run_ts(stage_files[s], date_str) or "" for s in core]
    group_ts = max(core_ts) if core_ts else None
    group_id = f"{date_str}-exec-{group_ts}" if group_ts else f"{date_str}-exec"
    logger.info(
        "[PERF_LOG_GROUP_CANDIDATE] date=%s group_id=%s files=%s",
        date_str, group_id, ",".join(f.name for f in files),
    )
    return files, group_ts, "split_manual_pipeline_logs"


def _path_in_set(path: Path, paths: Optional[List[Path]]) -> bool:
    if not paths:
        return False
    try:
        resolved = path.resolve()
        return any(resolved == p.resolve() for p in paths)
    except Exception:
        return any(str(path) == str(p) for p in paths)


def _resolve_pipeline_log_groups(
    date_str: str,
    log_files: List[Path],
    run_id: str,
) -> Dict[str, Any]:
    """latest_execution_run + daily_pipeline_group 동시 탐지."""
    all_logs = list(log_files) + _discover_date_logs(date_str)
    seen: set = set()
    unique_logs: List[Path] = []
    for p in all_logs:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        unique_logs.append(p)

    # pipeline_state run_id → latest execution 단일 파일
    if run_id:
        rid_us = run_id.replace("-", "_")
        for path in unique_logs:
            if run_id in path.name or rid_us in path.name:
                run_ts = _extract_log_run_ts(path, date_str) or run_id
                exec_files = [path]
                stage_files = _collect_manual_stage_logs(date_str, unique_logs)
                daily_files, daily_ts, daily_method = _build_daily_pipeline_group(
                    date_str, stage_files,
                )
                return _pack_pipeline_groups(
                    date_str, run_id, exec_files, run_ts, "pipeline_state_run_id",
                    daily_files, daily_ts, daily_method,
                )
        for path in unique_logs:
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:100000]
            except Exception:
                continue
            if f"[{run_id}]" in head:
                run_ts = _extract_log_run_ts(path, date_str) or run_id
                exec_files = [path]
                stage_files = _collect_manual_stage_logs(date_str, unique_logs)
                daily_files, daily_ts, daily_method = _build_daily_pipeline_group(
                    date_str, stage_files,
                )
                return _pack_pipeline_groups(
                    date_str, run_id, exec_files, run_ts, "pipeline_state_run_id",
                    daily_files, daily_ts, daily_method,
                )

    stage_files = _collect_manual_stage_logs(date_str, unique_logs)
    daily_files, daily_ts, daily_method = _build_daily_pipeline_group(date_str, stage_files)
    exec_files, exec_ts, exec_method = _build_latest_execution_group(date_str, stage_files)

    best_full: Optional[Tuple[str, Path]] = None
    for p in unique_logs:
        if not _is_full_pipeline_log(p, date_str):
            continue
        run_ts = _extract_log_run_ts(p, date_str)
        if run_ts and (best_full is None or run_ts > best_full[0]):
            best_full = (run_ts, p)

    if exec_files and best_full and exec_method not in _RESUME_EXEC_METHODS:
        if best_full[0] > (exec_ts or ""):
            exec_files, exec_ts, exec_method = [best_full[1]], best_full[0], "full_pipeline_log"
    elif not exec_files and best_full:
        exec_files, exec_ts, exec_method = [best_full[1]], best_full[0], "full_pipeline_log"
    elif not exec_files:
        candidates = [p for p in unique_logs if _is_pipeline_log_candidate(p, date_str)]
        ts_pairs = [
            (_extract_log_run_ts(p, date_str), p)
            for p in candidates
            if _extract_log_run_ts(p, date_str)
        ]
        if ts_pairs:
            ts_pairs.sort(key=lambda x: x[0], reverse=True)
            exec_ts, path = ts_pairs[0]
            exec_files, exec_method = [path], "filename_timestamp"
        elif candidates:
            path = max(candidates, key=lambda p: p.stat().st_mtime)
            exec_files = [path]
            exec_ts = _extract_log_run_ts(path, date_str)
            exec_method = "fallback_historical"

    if not daily_files and exec_files and exec_method == "full_pipeline_log":
        daily_files, daily_ts, daily_method = exec_files, exec_ts, "full_pipeline_log"

    return _pack_pipeline_groups(
        date_str, run_id, exec_files, exec_ts, exec_method,
        daily_files, daily_ts, daily_method,
    )


def _runtime_from_log_files(paths: List[Path]) -> Tuple[Optional[float], Dict[str, float]]:
    """로그 파일 묶음에서 runtime 집계."""
    text = ""
    for p in paths:
        try:
            text += p.read_text(encoding="utf-8", errors="ignore") + "\n"
        except Exception:
            pass
    if not text:
        return None, {}
    parsed = _parse_system_from_logs(text)
    stage_keys = (
        "screener_runtime_sec",
        "news_collector_runtime_sec",
        "gpt_analyzer_runtime_sec",
        "trader_runtime_sec",
        "order_reconciler_runtime_sec",
    )
    stages = {k: float(parsed[k]) for k in stage_keys if k in parsed}
    total = parsed.get("total_runtime_sec")
    if total is None and stages:
        total = round(sum(stages.values()), 2)
    return (float(total) if total is not None else None), stages


def _pack_pipeline_groups(
    date_str: str,
    run_id: str,
    exec_files: List[Path],
    exec_ts: Optional[str],
    exec_method: str,
    daily_files: List[Path],
    daily_ts: Optional[str],
    daily_method: str,
) -> Dict[str, Any]:
    if exec_files:
        logger.info(
            "[PERF_LOG_GROUP_SELECTED] method=%s files=%s",
            exec_method, ",".join(f.name for f in exec_files),
        )
    if daily_files and daily_files != exec_files:
        logger.info(
            "[PERF_LOG_GROUP_SELECTED] method=%s files=%s",
            daily_method, ",".join(f.name for f in daily_files),
        )
    exec_runtime, _ = _runtime_from_log_files(exec_files)
    daily_runtime, daily_stages = _runtime_from_log_files(daily_files)
    return {
        "latest_execution_run_ts": exec_ts,
        "latest_execution_detect_method": exec_method if exec_files else "incomplete",
        "latest_execution_log_files": [p.name for p in exec_files],
        "latest_execution_runtime_sec": exec_runtime,
        "daily_pipeline_run_ts": daily_ts,
        "daily_pipeline_detect_method": daily_method if daily_files else "incomplete",
        "daily_pipeline_log_files": [p.name for p in daily_files],
        "daily_pipeline_runtime_sec": daily_runtime,
        "daily_pipeline_stage_runtime_sec": daily_stages,
        "latest_pipeline_run_id": run_id or None,
        "_latest_execution_paths": exec_files,
        "_daily_pipeline_paths": daily_files,
    }


def _resolve_latest_pipeline_log(
    date_str: str,
    log_files: List[Path],
    run_id: str,
) -> Tuple[List[Path], Optional[str], str]:
    """하위 호환: latest_execution_run 기준."""
    groups = _resolve_pipeline_log_groups(date_str, log_files, run_id)
    paths = groups.pop("_latest_execution_paths", []) or []
    groups.pop("_daily_pipeline_paths", None)
    return (
        paths,
        groups.get("latest_execution_run_ts"),
        groups.get("latest_execution_detect_method", "fallback_historical"),
    )


def _nearest_run_id(lines: List[str], idx: int, lookback: int = 80) -> Optional[str]:
    for j in range(idx, max(-1, idx - lookback), -1):
        m = _RUN_ID_IN_LINE_RE.search(lines[j])
        if m:
            return m.group(1)
    return None


def _resolve_log_scope(
    line: str,
    lines: List[str],
    idx: int,
    execution_run_ids: set,
    daily_run_ids: set,
    path: Path,
    latest_execution_paths: Optional[List[Path]] = None,
    daily_pipeline_paths: Optional[List[Path]] = None,
) -> Tuple[str, str]:
    """로그 라인 scope/severity 분류."""
    low_path = str(path).lower()
    if "performance_review" in low_path or "PerformanceReview" in line:
        return "current_run", "current_warning"
    if "performance_review.py" in line:
        return "current_run", "current_warning"

    in_execution = _path_in_set(path, latest_execution_paths)
    in_daily = _path_in_set(path, daily_pipeline_paths)

    ctx_run = _nearest_run_id(lines, idx)
    if not in_execution and execution_run_ids:
        if ctx_run in execution_run_ids or any(f"[{rid}]" in line for rid in execution_run_ids):
            in_execution = True
    if not in_daily and daily_run_ids:
        if ctx_run in daily_run_ids or any(f"[{rid}]" in line for rid in daily_run_ids):
            in_daily = True

    if in_execution:
        return "latest_execution", "current_warning"
    if in_daily:
        return "daily_pipeline", "current_warning"
    return "historical_log", "historical_warning"


def _empty_scoped_counts() -> Dict[str, int]:
    return {
        "current_run_egw00201_count": 0,
        "current_pipeline_egw00201_count": 0,
        "latest_execution_egw00201_count": 0,
        "daily_pipeline_egw00201_count": 0,
        "latest_pipeline_egw00201_count": 0,
        "all_related_logs_egw00201_count": 0,
        "historical_egw00201_count": 0,
        "current_run_error_count": 0,
        "latest_execution_error_count": 0,
        "daily_pipeline_error_count": 0,
        "latest_pipeline_error_count": 0,
        "historical_error_count": 0,
        "all_related_logs_error_count": 0,
        "current_run_traceback_count": 0,
        "latest_execution_traceback_count": 0,
        "daily_pipeline_traceback_count": 0,
        "latest_pipeline_traceback_count": 0,
        "historical_traceback_count": 0,
        "traceback_count": 0,
        "performance_review_traceback": 0,
    }


def _parse_scoped_log_events(
    date_str: str,
    log_files: List[Path],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """EGW00201/ERROR/Traceback를 scope별로 집계."""
    state = _load_pipeline_state(date_str)
    latest_run_id = str(state.get("run_id") or "")
    groups = _resolve_pipeline_log_groups(date_str, log_files, latest_run_id)

    latest_execution_paths: List[Path] = groups.pop("_latest_execution_paths", []) or []
    daily_pipeline_paths: List[Path] = groups.pop("_daily_pipeline_paths", []) or []

    execution_run_ids: set = set()
    daily_run_ids: set = set()
    if latest_run_id:
        execution_run_ids.add(latest_run_id)
    exec_ts = groups.get("latest_execution_run_ts")
    if exec_ts:
        execution_run_ids.add(exec_ts)
    for path in latest_execution_paths:
        run_ts = _extract_log_run_ts(path, date_str)
        if run_ts:
            execution_run_ids.add(run_ts)

    daily_ts = groups.get("daily_pipeline_run_ts")
    if daily_ts:
        daily_run_ids.add(daily_ts)
    for path in daily_pipeline_paths:
        run_ts = _extract_log_run_ts(path, date_str)
        if run_ts:
            daily_run_ids.add(run_ts)

    counts = _empty_scoped_counts()
    details: List[Dict[str, Any]] = []

    scan_files: List[Path] = []
    seen_paths: set = set()
    for p in list(log_files) + _discover_date_logs(date_str):
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        scan_files.append(p)

    for path in scan_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            scope, severity = _resolve_log_scope(
                line, lines, i,
                execution_run_ids, daily_run_ids, path,
                latest_execution_paths, daily_pipeline_paths,
            )
            in_execution = _path_in_set(path, latest_execution_paths)
            in_daily = _path_in_set(path, daily_pipeline_paths)

            if "EGW00201" in line:
                counts["all_related_logs_egw00201_count"] += 1
                if scope == "current_run":
                    counts["current_run_egw00201_count"] += 1
                else:
                    if in_execution:
                        counts["latest_execution_egw00201_count"] += 1
                        counts["latest_pipeline_egw00201_count"] += 1
                    if in_daily:
                        counts["daily_pipeline_egw00201_count"] += 1
                    if scope != "current_run" and not in_execution and not in_daily:
                        counts["historical_egw00201_count"] += 1
                details.append({
                    "type": "egw00201",
                    "scope": scope,
                    "severity": severity,
                    "source_file": str(path),
                    "line_no": i + 1,
                    "sample": line[:300],
                })

            if " - ERROR - " in line:
                counts["all_related_logs_error_count"] += 1
                if scope == "current_run":
                    counts["current_run_error_count"] += 1
                else:
                    if in_execution:
                        counts["latest_execution_error_count"] += 1
                        counts["latest_pipeline_error_count"] += 1
                    if in_daily:
                        counts["daily_pipeline_error_count"] += 1
                    if scope != "current_run" and not in_execution and not in_daily:
                        counts["historical_error_count"] += 1
                details.append({
                    "type": "error",
                    "scope": scope,
                    "severity": severity,
                    "source_file": str(path),
                    "line_no": i + 1,
                    "sample": line[:300],
                })

            if "Traceback (most recent call last)" in line:
                counts["traceback_count"] += 1
                sample = line
                if i + 1 < len(lines):
                    sample = f"{line} | next: {lines[i + 1][:120]}"
                if scope == "current_run":
                    counts["current_run_traceback_count"] += 1
                else:
                    if in_execution:
                        counts["latest_execution_traceback_count"] += 1
                        counts["latest_pipeline_traceback_count"] += 1
                    if in_daily:
                        counts["daily_pipeline_traceback_count"] += 1
                    if scope != "current_run" and not in_execution and not in_daily:
                        counts["historical_traceback_count"] += 1
                details.append({
                    "type": "traceback",
                    "scope": scope,
                    "severity": severity,
                    "source_file": str(path),
                    "line_no": i + 1,
                    "sample": sample[:300],
                })

    counts["performance_review_traceback"] = counts["current_run_traceback_count"]
    counts["current_pipeline_egw00201_count"] = counts["latest_execution_egw00201_count"]

    latest_pipeline_log_files = groups.get("latest_execution_log_files") or []
    latest_pipeline_log_file = latest_pipeline_log_files[0] if latest_pipeline_log_files else None
    result: Dict[str, Any] = {
        **counts,
        **groups,
        "latest_pipeline_log_file": latest_pipeline_log_file,
        "latest_pipeline_log_files": latest_pipeline_log_files,
        "latest_pipeline_run_ts": groups.get("latest_execution_run_ts"),
        "latest_pipeline_detect_method": groups.get("latest_execution_detect_method"),
        # 하위 호환: latest_pipeline_* = latest_execution_*
        "egw00201_count": counts["latest_execution_egw00201_count"],
        "kis_rate_limit_count": counts["latest_execution_egw00201_count"],
        "error_count": counts["latest_execution_error_count"],
    }
    return result, details


def _gpt_action(plan: Dict[str, Any]) -> str:
    raw = str(plan.get("결정") or plan.get("decision") or "").strip()
    return GPT_ACTION_MAP.get(raw, raw.upper() or "UNKNOWN")


def _plan_ticker(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return _norm_ticker(info.get("Ticker") or info.get("ticker") or plan.get("ticker"))


def _plan_name(plan: Dict[str, Any]) -> str:
    info = plan.get("stock_info") or {}
    return str(info.get("Name") or info.get("name") or plan.get("name") or "")


def _plan_score(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    for key in ("ConvictionScore", "Score", "score", "conviction_score"):
        val = _safe_float(info.get(key))
        if val is not None:
            return val
    return _safe_float(plan.get("gpt_score"))


def _plan_price(plan: Dict[str, Any]) -> Optional[float]:
    info = plan.get("stock_info") or {}
    return _safe_float(info.get("Price") or info.get("price"))


def _parse_structured_context(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _infer_reconcile_method(
    row: Dict[str, Any],
    reconcile_map: Optional[Dict[str, str]] = None,
) -> str:
    order_id = str(row.get("order_id") or "").strip()
    if reconcile_map and order_id and order_id in reconcile_map:
        return reconcile_map[order_id]

    ctx = _parse_structured_context(row.get("structured_context"))
    src = str(ctx.get("reconciled_price_source") or ctx.get("reconcile_source") or "").lower()
    if "holding" in src:
        return "holding_fallback"
    if src in ("order_query", "daily_query", "manual", "kis_order_query"):
        return "order_query" if src == "order_query" else src
    if ctx.get("reconcile_method"):
        return str(ctx["reconcile_method"])

    status = str(row.get("order_status") or "").lower()
    if order_id and status in ("executed", "partial", "pending", "completed"):
        return "order_query"
    if status == "executed" and not order_id:
        return "manual"
    return "unknown"


def _lookup_name(
    ticker: str,
    candidates: List[Dict[str, Any]],
    balance_rows: List[Dict[str, Any]],
    bond_cfg: Optional[List[Dict[str, Any]]] = None,
) -> str:
    t = _norm_ticker(ticker)
    for row in candidates or []:
        if _norm_ticker(row.get("Ticker") or row.get("ticker")) == t:
            return str(row.get("Name") or row.get("name") or "")
    for row in balance_rows or []:
        if _norm_ticker(row.get("pdno")) == t:
            return str(row.get("prdt_name") or row.get("name") or "")
    for item in bond_cfg or []:
        if isinstance(item, dict) and _norm_ticker(item.get("ticker")) == t:
            return str(item.get("name") or "")
    return ""


def _find_log_files(date_str: str) -> List[Path]:
    patterns = [
        "pipeline_*.log",
        "step_*.log",
        "trader_run.log",
        "integrated_manager.log",
        "*.log",
    ]
    found: List[Path] = []
    seen = set()
    for pat in patterns:
        for p in OUTPUT_DIR.glob(pat):
            if p in seen:
                continue
            seen.add(p)
            found.append(p)
    debug_dir = OUTPUT_DIR / "debug"
    if debug_dir.is_dir():
        for p in debug_dir.glob("*.log"):
            if p not in seen:
                seen.add(p)
                found.append(p)

    relevant: List[Tuple[float, Path]] = []
    for p in found:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if date_str in text or date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:] in text:
            relevant.append((p.stat().st_mtime, p))
    relevant.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in relevant]


def _parse_funnel_from_logs(log_text: str) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {
        "total_universe_count": None,
        "marcap_pass_count": None,
        "amount5d_pass_count": None,
    }
    for line in log_text.splitlines():
        m = re.search(r"│\s+(.+?)\s+(\d+)\s", line)
        if not m:
            continue
        label, cnt = m.group(1).strip(), int(m.group(2))
        if "전체 종목" in label:
            out["total_universe_count"] = cnt
        elif label.startswith("Marcap"):
            out["marcap_pass_count"] = cnt
        elif label.startswith("Amount5D"):
            out["amount5d_pass_count"] = cnt
    return out


def _resolve_screener_funnel_log_text(
    date_str: str,
    latest_pipeline_paths: Optional[List[Path]] = None,
) -> Tuple[str, Optional[str]]:
    """스크리너 funnel 파싱용 로그 텍스트와 출처 파일명."""
    if latest_pipeline_paths:
        for p in latest_pipeline_paths:
            if _classify_manual_log_stage(p.name, date_str) == "screener":
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    logger.info(
                        "[PERF_SCREENER_FUNNEL_SOURCE] source=manual_screener_log file=%s",
                        p.name,
                    )
                    return text, p.name
                except Exception:
                    pass

    best: Optional[Tuple[str, Path]] = None
    for base in (OUTPUT_DIR, OUTPUT_DIR / "debug"):
        if not base.is_dir():
            continue
        for p in base.glob(f"manual_screener_{date_str}_*.log"):
            run_ts = _extract_log_run_ts(p, date_str)
            if run_ts and (best is None or run_ts > best[0]):
                best = (run_ts, p)
    if best:
        try:
            text = best[1].read_text(encoding="utf-8", errors="ignore")
            logger.info(
                "[PERF_SCREENER_FUNNEL_SOURCE] source=manual_screener_log file=%s",
                best[1].name,
            )
            return text, best[1].name
        except Exception:
            pass

    logger.info("[PERF_SCREENER_FUNNEL_SOURCE] source=output_files fallback=true")
    return "", None


def _parse_budget_usage_from_logs(log_text: str) -> Dict[str, Any]:
    m = re.search(
        r"\[STOCK_BUDGET_USAGE\]\s+total_assets=(\S+)\s+stock_target_weight=([\d.]+)\s+"
        r"current_stock_value=(\S+)\s+stock_buy_budget=(\S+)\s+gpt_plan_count=(\S+)\s+"
        r"gpt_buy_count=(\S+)\s+final_buy_count=(\S+)\s+planned_order_amount=(\S+)\s+"
        r"executed_order_amount=(\S+)\s+unused_stock_budget=(\S+)\s+unused_reason=(\S+)",
        log_text,
    )
    if not m:
        return {}
    total_assets = _safe_int(m.group(1))
    stock_target_weight = _safe_float(m.group(2)) or 0.0
    current_stock_value = _safe_int(m.group(3))
    stock_buy_budget = _safe_int(m.group(4))
    executed_amount = _safe_int(m.group(9))
    unused_budget = _safe_int(m.group(10))
    usage_rate = (executed_amount / stock_buy_budget) if stock_buy_budget > 0 else None
    return {
        "total_assets": total_assets,
        "stock_target_weight": stock_target_weight,
        "current_stock_value": current_stock_value,
        "stock_buy_budget": stock_buy_budget,
        "gpt_plan_count": _safe_int(m.group(5)),
        "gpt_buy_count": _safe_int(m.group(6)),
        "final_buy_count": _safe_int(m.group(7)),
        "planned_order_amount": _safe_int(m.group(8)),
        "actual_stock_buy_amount": executed_amount,
        "stock_budget_usage_rate": round(usage_rate, 4) if usage_rate is not None else None,
        "unused_stock_budget": unused_budget,
        "unused_reason": m.group(11),
        "source": "trader_log",
    }


def _parse_system_from_logs(log_text: str) -> Dict[str, Any]:
    step_times: Dict[str, float] = {}
    for m in re.finditer(
        r"STEP OK:\s+([\w_.]+)\s+\|\s+([\d.]+)s",
        log_text,
    ):
        step_times[m.group(1)] = float(m.group(2))

    script_map = {
        "health_check.py": "health_check_runtime_sec",
        "news_collector.py": "news_collector_runtime_sec",
        "gpt_analyzer.py": "gpt_analyzer_runtime_sec",
        "trader.py": "trader_runtime_sec",
        "order_reconciler.py": "order_reconciler_runtime_sec",
        "screener.py": "screener_runtime_sec",
    }
    perf: Dict[str, Any] = {
        "kis_retry_count": len(re.findall(r"재시도|retry", log_text, re.I)),
        "warning_count": len(re.findall(r" - WARNING - ", log_text)),
    }
    for script, key in script_map.items():
        if script in step_times:
            perf[key] = step_times[script]
    if step_times:
        perf["total_runtime_sec"] = round(sum(step_times.values()), 2)
    return perf


def _load_pipeline_state(date_str: str) -> Dict[str, Any]:
    path = OUTPUT_DIR / "pipeline_state.json"
    data = _load_json(path)
    if not isinstance(data, dict):
        return {}
    run_id = str(data.get("run_id") or "")
    if not run_id.startswith(date_str):
        return data if data else {}
    return data


def _load_trade_rows(date_str: str) -> List[Dict[str, Any]]:
    db_path = OUTPUT_DIR / "trading_data.db"
    if not db_path.is_file():
        return []
    prefix = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    alt_prefix = date_str
    rows: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, timestamp, ticker, action, quantity, price, amount,
                   order_status, order_id, requested_qty, executed_qty,
                   structured_context, last_status_update_ts
            FROM trade_records
            WHERE timestamp LIKE ? OR timestamp LIKE ?
            ORDER BY timestamp
            """,
            (f"{prefix}%", f"{alt_prefix}%"),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        logger.warning("trade_records 조회 실패: %s", e)
    return rows


def _build_screener_maps(candidates: List[Dict[str, Any]], scores: List[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, float]]:
    rank_map: Dict[str, int] = {}
    score_map: Dict[str, float] = {}
    for i, row in enumerate(candidates or [], 1):
        t = _norm_ticker(row.get("Ticker") or row.get("ticker"))
        if t:
            rank_map[t] = i
            score_map[t] = _safe_float(row.get("Score") or row.get("score_total")) or 0.0
    for row in scores or []:
        t = _norm_ticker(row.get("ticker") or row.get("Ticker"))
        if t and t not in score_map:
            score_map[t] = _safe_float(row.get("score_total") or row.get("Score")) or 0.0
    return rank_map, score_map


def _build_screener_funnel(
    date_str: str,
    market: str,
    candidates: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    log_text: str,
    *,
    latest_pipeline_paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    screener_log_text, _screener_src = _resolve_screener_funnel_log_text(
        date_str, latest_pipeline_paths,
    )
    funnel_log = _parse_funnel_from_logs(screener_log_text or log_text)
    if screener_log_text and log_text and screener_log_text != log_text:
        extra = _parse_funnel_from_logs(log_text)
        for key in ("total_universe_count", "marcap_pass_count", "amount5d_pass_count"):
            if funnel_log.get(key) is None and extra.get(key) is not None:
                funnel_log[key] = extra[key]
    gpt_buy = sum(1 for p in gpt_plans if _gpt_action(p) == "BUY")
    gpt_hold = sum(1 for p in gpt_plans if _gpt_action(p) == "HOLD")
    gpt_reject = sum(1 for p in gpt_plans if _gpt_action(p) == "REJECT")
    executed_buy = sum(
        1 for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "completed")
        and _safe_int(r.get("executed_qty") or r.get("quantity")) > 0
    )
    final_buy_tickers = {
        _norm_ticker(r.get("ticker"))
        for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "pending", "completed")
    }
    final_buy_tickers.discard("000000")

    funnel = {
        "review_date": date_str,
        "market": market,
        "total_universe_count": funnel_log.get("total_universe_count"),
        "marcap_pass_count": funnel_log.get("marcap_pass_count"),
        "amount5d_pass_count": funnel_log.get("amount5d_pass_count"),
        "final_screener_candidate_count": len(candidates or []),
        "screener_score_count": len(scores or []),
        "gpt_analysis_count": len(gpt_plans),
        "gpt_buy_count": gpt_buy,
        "gpt_hold_count": gpt_hold,
        "gpt_reject_count": gpt_reject,
        "final_buy_count": len(final_buy_tickers),
        "executed_buy_count": executed_buy,
    }

    if funnel["total_universe_count"] is None:
        try:
            from kis_master import load_kis_master
            df = load_kis_master(market, cache_key=date_str)
            if df is not None and not df.empty:
                funnel["total_universe_count"] = len(df)
        except Exception:
            pass

    logger.info(
        "[PERF_SCREENER_FUNNEL] total=%s marcap_pass=%s amount5d_pass=%s "
        "candidates=%s gpt_plans=%s gpt_buy=%s gpt_hold=%s gpt_reject=%s executed_buy=%s",
        funnel["total_universe_count"],
        funnel["marcap_pass_count"],
        funnel["amount5d_pass_count"],
        funnel["final_screener_candidate_count"],
        funnel["gpt_analysis_count"],
        funnel["gpt_buy_count"],
        funnel["gpt_hold_count"],
        funnel["gpt_reject_count"],
        funnel["executed_buy_count"],
    )
    return funnel


def _build_gpt_decision(
    gpt_plans: List[Dict[str, Any]],
    rank_map: Dict[str, int],
    score_map: Dict[str, float],
    trade_rows: List[Dict[str, Any]],
    reconcile_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    trade_by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in trade_rows:
        if str(r.get("action", "")).upper() != "BUY":
            continue
        t = _norm_ticker(r.get("ticker"))
        if t:
            trade_by_ticker[t] = r

    rows: List[Dict[str, Any]] = []
    for plan in gpt_plans:
        ticker = _plan_ticker(plan)
        action = _gpt_action(plan)
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        exe_qty = _safe_int(trade.get("executed_qty") or trade.get("quantity"))
        exe_price = _safe_int(trade.get("price"))
        exe_amount = _safe_int(trade.get("amount")) or (exe_qty * exe_price if exe_qty and exe_price else 0)
        rows.append({
            "ticker": ticker,
            "name": _plan_name(plan),
            "gpt_action": action,
            "gpt_score": _plan_score(plan),
            "gpt_reason": (plan.get("분석") or plan.get("analysis") or "")[:500],
            "screener_rank": plan.get("rank") or rank_map.get(ticker),
            "screener_score": score_map.get(ticker),
            "final_decision": action if action != "REJECT" else "REJECT",
            "executed_qty": exe_qty if status in ("executed", "partial", "completed") else 0,
            "executed_amount": exe_amount if status in ("executed", "partial", "completed") else 0,
            "order_status": status or None,
            "reconcile_method": _infer_reconcile_method(trade, reconcile_map) if trade else None,
        })

    aggregates: Dict[str, Dict[str, Any]] = {}
    for action in ("BUY", "HOLD", "REJECT"):
        subset = [r for r in rows if r["gpt_action"] == action]
        gpt_scores = [r["gpt_score"] for r in subset if r["gpt_score"] is not None]
        scr_scores = [r["screener_score"] for r in subset if r["screener_score"] is not None]
        executed = [r for r in subset if _safe_int(r.get("executed_qty")) > 0]
        aggregates[action] = {
            "count": len(subset),
            "avg_gpt_score": round(sum(gpt_scores) / len(gpt_scores), 4) if gpt_scores else None,
            "avg_screener_score": round(sum(scr_scores) / len(scr_scores), 4) if scr_scores else None,
            "executed_count": len(executed),
            "executed_amount": sum(_safe_int(r.get("executed_amount")) for r in executed),
        }

    executed_buy = sum(1 for r in rows if r["gpt_action"] == "BUY" and _safe_int(r.get("executed_qty")) > 0)
    logger.info(
        "[PERF_GPT_DECISION] plans=%s buy=%s hold=%s reject=%s executed_buy=%s",
        len(rows),
        aggregates.get("BUY", {}).get("count", 0),
        aggregates.get("HOLD", {}).get("count", 0),
        aggregates.get("REJECT", {}).get("count", 0),
        executed_buy,
    )
    return {"rows": rows, "aggregates": aggregates}


def _build_budget_usage(
    date_str: str,
    log_text: str,
    account_summary: Dict[str, Any],
    balance_rows: List[Dict[str, Any]],
    kis_trade_rows: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
) -> Dict[str, Any]:
    budget = _parse_budget_usage_from_logs(log_text)
    aa = settings.asset_allocation if hasattr(settings, "asset_allocation") else {}
    if not isinstance(aa, dict):
        aa = {}

    stock_target = _safe_float(aa.get("stock_target_weight")) or 0.70
    bond_target = _safe_float(aa.get("bond_target_weight")) or 0.20
    cash_target = _safe_float(aa.get("cash_target_weight")) or 0.10

    total_assets = _safe_int(account_summary.get("total_assets"))
    if not total_assets and budget:
        total_assets = _safe_int(budget.get("total_assets"))

    bond_tickers = _bond_etf_tickers()

    current_stock_value = 0
    current_bond_value = 0
    for row in balance_rows:
        t = _norm_ticker(row.get("pdno"))
        val = _safe_int(row.get("evlu_amt"))
        if t in bond_tickers:
            current_bond_value += val
        else:
            current_stock_value += val

    current_cash = _safe_int(
        account_summary.get("prvs_rcdl_excc_amt") or account_summary.get("dnca_tot_amt")
    )
    target_stock_value = int(total_assets * stock_target) if total_assets else 0
    target_bond_value = int(total_assets * bond_target) if total_assets else 0
    target_cash_value = int(total_assets * cash_target) if total_assets else 0

    buy_statuses = ("executed", "partial", "completed")
    actual_stock_buy_amount = 0
    actual_bond_buy_amount = 0
    executed_stock_buy_count = 0
    executed_bond_buy_count = 0
    for r in kis_trade_rows:
        if str(r.get("action", "")).upper() != "BUY":
            continue
        if str(r.get("order_status", "")).lower() not in buy_statuses:
            continue
        amt = _safe_int(r.get("amount"))
        if not amt:
            qty = _safe_int(r.get("executed_qty") or r.get("quantity"))
            price = _safe_int(r.get("price"))
            amt = qty * price if qty and price else 0
        if _norm_ticker(r.get("ticker")) in bond_tickers:
            actual_bond_buy_amount += amt
            executed_bond_buy_count += 1
        else:
            actual_stock_buy_amount += amt
            executed_stock_buy_count += 1

    total_executed_buy_amount = actual_stock_buy_amount + actual_bond_buy_amount
    executed_total_buy_count = executed_stock_buy_count + executed_bond_buy_count
    gpt_buy_count = sum(1 for p in gpt_plans if _gpt_action(p) == "BUY")
    final_buy_count = len({
        _norm_ticker(r.get("ticker"))
        for r in kis_trade_rows
        if str(r.get("action", "")).upper() == "BUY"
    })
    stock_buy_budget = max(0, target_stock_value - current_stock_value) if aa.get("enabled", True) else 0
    unused_stock_budget = max(0, stock_buy_budget - actual_stock_buy_amount)
    stock_budget_usage_rate = (
        round(actual_stock_buy_amount / stock_buy_budget, 4) if stock_buy_budget > 0 else None
    )
    unused_reason = (budget or {}).get("unused_reason") or "derived_from_db"

    out = {
        "total_assets": total_assets,
        "stock_target_weight": stock_target,
        "bond_target_weight": bond_target,
        "cash_target_weight": cash_target,
        "current_stock_value": current_stock_value,
        "current_bond_value": current_bond_value,
        "current_cash": current_cash,
        "target_stock_value": target_stock_value,
        "target_bond_value": target_bond_value,
        "target_cash_value": target_cash_value,
        "stock_buy_budget": stock_buy_budget,
        "gpt_buy_count": gpt_buy_count,
        "final_buy_count": final_buy_count,
        "actual_stock_buy_amount": actual_stock_buy_amount,
        "actual_bond_buy_amount": actual_bond_buy_amount,
        "total_executed_buy_amount": total_executed_buy_amount,
        "executed_stock_buy_count": executed_stock_buy_count,
        "executed_bond_buy_count": executed_bond_buy_count,
        "executed_total_buy_count": executed_total_buy_count,
        "unused_stock_budget": unused_stock_budget,
        "stock_budget_usage_rate": stock_budget_usage_rate,
        "unused_reason": unused_reason,
        "source": (budget or {}).get("source") or "computed",
    }
    logger.info(
        "[STOCK_BUDGET_USAGE] total_assets=%s stock_target_weight=%.2f current_stock_value=%s "
        "stock_buy_budget=%s gpt_buy_count=%s final_buy_count=%s actual_stock_buy=%s actual_bond_buy=%s "
        "total_executed_buy=%s unused_stock_budget=%s usage_rate=%s unused_reason=%s",
        out.get("total_assets"),
        out.get("stock_target_weight", 0.0),
        out.get("current_stock_value"),
        out.get("stock_buy_budget"),
        out.get("gpt_buy_count"),
        out.get("final_buy_count"),
        out.get("actual_stock_buy_amount"),
        out.get("actual_bond_buy_amount"),
        out.get("total_executed_buy_amount"),
        out.get("unused_stock_budget"),
        out.get("stock_budget_usage_rate"),
        out.get("unused_reason"),
    )
    return out


_SUBMITTED_STATUSES = frozenset({
    "executed", "completed", "partial", "pending", "cancelled", "failed",
})


def _build_execution_performance(
    kis_trade_rows: List[Dict[str, Any]],
    reconcile_map: Optional[Dict[str, str]] = None,
    paper_db_only_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    submitted = executed = pending = cancelled = failed = 0
    reconciled = holding_fb = unreconciled = 0
    slippages: List[float] = []
    pending_durations: List[float] = []

    order_details: List[Dict[str, Any]] = []
    for r in kis_trade_rows:
        status = str(r.get("order_status") or "").lower()
        order_id = str(r.get("order_id") or "").strip()
        if order_id or status in _SUBMITTED_STATUSES:
            submitted += 1
        if status in ("executed", "completed"):
            executed += 1
        if status == "pending":
            pending += 1
        if status == "cancelled":
            cancelled += 1
        if status == "failed":
            failed += 1

        method = _infer_reconcile_method(r, reconcile_map)
        if status in ("executed", "partial", "completed"):
            reconciled += 1
            if method == "holding_fallback":
                holding_fb += 1
        elif status in ("pending", "failed") and order_id:
            unreconciled += 1

        ctx = _parse_structured_context(r.get("structured_context"))
        orig = _safe_int(ctx.get("original_order_price") or r.get("price"))
        fill = _safe_int(ctx.get("reconciled_price") or r.get("price"))
        slip_pct = None
        if orig > 0 and fill > 0 and orig != fill:
            slip_pct = round((fill - orig) / orig, 6)
            slippages.append((fill - orig) / orig)

        order_details.append({
            "ticker": _norm_ticker(r.get("ticker")),
            "action": str(r.get("action") or "").upper(),
            "order_status": status,
            "order_id": r.get("order_id"),
            "order_price": orig,
            "executed_price": fill,
            "slippage_amount": fill - orig if orig and fill else None,
            "slippage_pct": slip_pct,
            "reconcile_method": method,
            "is_actual_kis_order": _is_actual_kis_order_row(r),
        })

        ts = str(r.get("timestamp") or "")
        upd = str(r.get("last_status_update_ts") or "")
        if status == "executed" and ts and upd:
            try:
                t0 = datetime.fromisoformat(ts.replace("Z", ""))
                t1 = datetime.fromisoformat(upd.replace("Z", ""))
                pending_durations.append(max(0.0, (t1 - t0).total_seconds()))
            except Exception:
                pass

    paper_db_only_submitted = sum(
        1 for r in (paper_db_only_rows or [])
        if str(r.get("action", "")).upper() == "BUY"
    )
    order_count = len(kis_trade_rows)
    execution_rate = round(executed / submitted, 4) if submitted > 0 else (1.0 if executed else 0.0)
    perf = {
        "order_count": order_count,
        "submitted_order_count": submitted,
        "executed_order_count": executed,
        "pending_order_count": pending,
        "cancelled_order_count": cancelled,
        "failed_order_count": failed,
        "execution_rate": execution_rate,
        "pending_duration_sec": round(sum(pending_durations) / len(pending_durations), 2) if pending_durations else None,
        "slippage_pct_avg": round(sum(slippages) / len(slippages), 6) if slippages else None,
        "reconciled_count": reconciled,
        "reconciled_by_holding_count": holding_fb,
        "unreconciled_count": unreconciled,
        "paper_db_only_submitted": paper_db_only_submitted,
        "orders": order_details,
    }
    logger.info(
        "[PERF_EXECUTION] submitted=%s executed=%s pending=%s execution_rate=%s "
        "reconciled_by_holding=%s unreconciled=%s paper_db_only=%s",
        perf["submitted_order_count"],
        perf["executed_order_count"],
        perf["pending_order_count"],
        perf["execution_rate"],
        perf["reconciled_by_holding_count"],
        perf["unreconciled_count"],
        perf["paper_db_only_submitted"],
    )
    return perf


def _build_system_performance(
    date_str: str,
    log_text: str,
    scoped_counts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    perf = _parse_system_from_logs(log_text)
    if scoped_counts:
        perf.update(scoped_counts)
    state = _load_pipeline_state(date_str)
    durations = state.get("step_durations") or {}
    if isinstance(durations, dict):
        for step, sec in durations.items():
            key = step.replace(".py", "_runtime_sec")
            if key not in perf:
                perf[key] = sec
    if state.get("start_time") and state.get("last_update"):
        try:
            perf["pipeline_start_time"] = state.get("start_time")
            perf["pipeline_end_time"] = state.get("last_update")
        except Exception:
            pass
    if "total_runtime_sec" not in perf and durations:
        try:
            perf["total_runtime_sec"] = round(sum(float(v) for v in durations.values()), 2)
        except Exception:
            pass
    logger.info(
        "[PERF_SYSTEM] current_run_errors=%s latest_execution_egw00201=%s "
        "daily_pipeline_egw00201=%s historical_egw00201=%s "
        "performance_review_traceback=%s latest_execution_log=%s daily_pipeline_log=%s",
        perf.get("current_run_error_count", 0),
        perf.get("latest_execution_egw00201_count", 0),
        perf.get("daily_pipeline_egw00201_count", 0),
        perf.get("historical_egw00201_count", 0),
        perf.get("performance_review_traceback", 0),
        perf.get("latest_execution_log_files") or perf.get("latest_pipeline_log_files"),
        perf.get("daily_pipeline_log_files"),
    )
    return perf


def _fetch_price(ticker: str, price_cache: Dict[str, Optional[float]]) -> Optional[float]:
    if ticker in price_cache:
        return price_cache[ticker]
    try:
        from api.kis_auth import KIS
        env = getattr(settings, "_config", {}).get("trading_environment", "prod")
        kis = KIS(env=env if env in ("prod", "vps", "kis_paper") else "prod")
        df = kis.inquire_price("J", ticker)
        if df is not None and not df.empty:
            val = _safe_float(df["stck_prpr"].iloc[0])
            price_cache[ticker] = val
            return val
    except Exception as e:
        logger.debug("가격 조회 실패 %s: %s", ticker, e)
    price_cache[ticker] = None
    return None


def _build_candidate_tracking_snapshot(
    date_str: str,
    market: str,
    candidates: List[Dict[str, Any]],
    gpt_plans: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    balance_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    gpt_by_ticker = {_plan_ticker(p): p for p in gpt_plans}
    trade_by_ticker = {
        _norm_ticker(r.get("ticker")): r
        for r in trade_rows
        if str(r.get("action", "")).upper() == "BUY"
    }
    close_prices = {
        _norm_ticker(r.get("pdno")): _safe_float(r.get("prpr"))
        for r in balance_rows
    }

    tickers = set()
    for row in candidates or []:
        tickers.add(_norm_ticker(row.get("Ticker") or row.get("ticker")))
    for p in gpt_plans:
        tickers.add(_plan_ticker(p))
    tickers.discard("000000")

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(candidates or [], 1):
        ticker = _norm_ticker(row.get("Ticker") or row.get("ticker"))
        if not ticker:
            continue
        plan = gpt_by_ticker.get(ticker, {})
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        executed = status in ("executed", "partial", "completed") and _safe_int(trade.get("executed_qty") or trade.get("quantity")) > 0
        base_price = _safe_float(row.get("Price") or row.get("price")) or _plan_price(plan)
        base_close = close_prices.get(ticker) or base_price
        out.append({
            "base_date": date_str,
            "ticker": ticker,
            "name": str(row.get("Name") or row.get("name") or _plan_name(plan)),
            "screener_rank": i,
            "screener_score": _safe_float(row.get("Score") or row.get("score_total")),
            "gpt_action": _gpt_action(plan) if plan else None,
            "gpt_score": _plan_score(plan) if plan else None,
            "base_price": base_price,
            "base_close_price": base_close,
            "executed": executed,
            "executed_price": _safe_int(trade.get("price")) if executed else None,
            "executed_qty": _safe_int(trade.get("executed_qty") or trade.get("quantity")) if executed else 0,
            "track_1d_price": None,
            "track_3d_price": None,
            "track_5d_price": None,
            "track_10d_price": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "return_10d": None,
        })

    for ticker, plan in gpt_by_ticker.items():
        if any(r["ticker"] == ticker for r in out):
            continue
        trade = trade_by_ticker.get(ticker, {})
        status = str(trade.get("order_status") or "").lower()
        executed = status in ("executed", "partial", "completed")
        out.append({
            "base_date": date_str,
            "ticker": ticker,
            "name": _plan_name(plan),
            "screener_rank": plan.get("rank"),
            "screener_score": _plan_score(plan),
            "gpt_action": _gpt_action(plan),
            "gpt_score": _plan_score(plan),
            "base_price": _plan_price(plan),
            "base_close_price": close_prices.get(ticker) or _plan_price(plan),
            "executed": executed,
            "executed_price": _safe_int(trade.get("price")) if executed else None,
            "executed_qty": _safe_int(trade.get("executed_qty") or trade.get("quantity")) if executed else 0,
            "track_1d_price": None,
            "track_3d_price": None,
            "track_5d_price": None,
            "track_10d_price": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "return_10d": None,
        })

    buy_n = sum(1 for r in out if r.get("gpt_action") == "BUY")
    hold_n = sum(1 for r in out if r.get("gpt_action") == "HOLD")
    reject_n = sum(1 for r in out if r.get("gpt_action") == "REJECT")
    logger.info(
        "[PERF_TRACKING_SNAPSHOT] saved=%s candidates=%s buy=%s hold=%s reject=%s",
        date_str,
        len(out),
        buy_n,
        hold_n,
        reject_n,
    )
    return out


def _update_tracking_returns(review_date_str: str, market: str, fetch_prices: bool = True) -> int:
    updated_files = 0
    review_dt = datetime.strptime(review_date_str, "%Y%m%d").date()
    price_cache: Dict[str, Optional[float]] = {}

    for path in sorted(OUTPUT_DIR.glob(f"candidate_tracking_*_{market}.json")):
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        base_date = str(data.get("base_date") or "")
        if len(base_date) != 8:
            continue
        try:
            base_dt = datetime.strptime(base_date, "%Y%m%d").date()
        except ValueError:
            continue
        days = (review_dt - base_dt).days
        if days <= 0 or days not in TRACK_HORIZONS:
            continue

        rows = data.get("candidates") or data.get("rows") or []
        if not isinstance(rows, list):
            continue
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            price_key = f"track_{days}d_price"
            ret_key = f"return_{days}d"
            if row.get(price_key) is not None:
                continue
            ticker = _norm_ticker(row.get("ticker"))
            base_price = _safe_float(row.get("base_price"))
            if not ticker or not base_price:
                continue
            current = row.get("base_close_price")
            if fetch_prices:
                current = _fetch_price(ticker, price_cache) or current
            if current is None:
                continue
            row[price_key] = current
            row[ret_key] = round((float(current) - base_price) / base_price, 6)
            changed = True
        if changed:
            data["candidates"] = rows
            data["last_return_update"] = review_date_str
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            updated_files += 1
    return updated_files


def _build_summary(
    date_str: str,
    account_summary: Dict[str, Any],
    balance_rows: List[Dict[str, Any]],
    kis_trade_rows: List[Dict[str, Any]],
    execution: Dict[str, Any],
) -> Dict[str, Any]:
    buy_trades = [
        r for r in kis_trade_rows
        if str(r.get("action", "")).upper() == "BUY"
        and str(r.get("order_status", "")).lower() in ("executed", "partial", "completed")
    ]
    holdings = [r for r in balance_rows if _safe_int(r.get("hldg_qty")) > 0]
    return {
        "date": date_str,
        "total_assets": _safe_int(account_summary.get("total_assets")),
        "tot_evlu_amt": _safe_int(account_summary.get("tot_evlu_amt")),
        "holdings_count": len(holdings),
        "new_buy_count": len(buy_trades),
        "pending_orders": execution.get("pending_order_count", 0),
        "dnca_tot_amt": _safe_int(account_summary.get("dnca_tot_amt")),
        "prvs_rcdl_excc_amt": _safe_int(account_summary.get("prvs_rcdl_excc_amt")),
        "nxdy_excc_amt": _safe_int(account_summary.get("nxdy_excc_amt")),
        "rlzt_pfls": _safe_int(account_summary.get("rlzt_pfls")),
    }


def _build_warnings(
    system: Dict[str, Any],
    execution: Dict[str, Any],
    budget: Dict[str, Any],
    account_warnings: Optional[List[str]] = None,
    warnings_detail: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[str], List[str]]:
    """(current_warnings, historical_warnings) 반환."""
    current: List[str] = list(account_warnings or [])
    historical: List[str] = []

    cre = system.get("current_run_error_count", 0)
    current.append("현재 실행 오류: 없음" if cre == 0 else f"현재 실행 오류: {cre}건")

    perf_tb = system.get("performance_review_traceback", 0)
    current.append(f"performance_review_traceback={perf_tb}")

    lp_egw = system.get("latest_execution_egw00201_count", system.get("latest_pipeline_egw00201_count", 0))
    current.append(f"최신 실행 EGW00201: {lp_egw}회")

    daily_egw = system.get("daily_pipeline_egw00201_count", 0)
    current.append(f"당일 파이프라인 EGW00201: {daily_egw}회")

    hist_tb = system.get("historical_traceback_count", 0)
    if hist_tb == 0 and warnings_detail:
        hist_tb = sum(
            1 for d in warnings_detail
            if d.get("type") == "traceback" and d.get("scope") == "historical_log"
        )
    current.append(f"과거 로그 Traceback: {hist_tb}건")

    hist_egw = system.get("historical_egw00201_count", 0)
    current.append(f"과거 로그 EGW00201: {hist_egw}회")

    if execution.get("pending_order_count", 0) > 0:
        current.append(f"미해결 pending 주문 {execution['pending_order_count']}건")
    if execution.get("unreconciled_count", 0) > 0:
        current.append(f"미리컨실 주문 {execution['unreconciled_count']}건")
    unused = budget.get("unused_reason")
    if unused and unused not in ("budget_utilized", "derived_from_db"):
        current.append(f"주식 예산 미사용: {unused}")

    for detail in warnings_detail or []:
        if detail.get("scope") != "historical_log":
            continue
        src = Path(str(detail.get("source_file", ""))).name
        line_no = detail.get("line_no", "?")
        sample = str(detail.get("sample", ""))[:120]
        typ = detail.get("type", "event")
        historical.append(f"{typ}: {src}:{line_no} — {sample}")

    return current, historical


def _csv_rows(
    date_str: str,
    market: str,
    gpt_rows: List[Dict[str, Any]],
    tracking_rows: List[Dict[str, Any]],
    kis_trade_rows: List[Dict[str, Any]],
    reconcile_map: Optional[Dict[str, str]] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
    balance_rows: Optional[List[Dict[str, Any]]] = None,
    bond_cfg: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    bond_tickers = _bond_etf_tickers()
    track_by_ticker = {r["ticker"]: r for r in tracking_rows}
    trade_by_ticker = {
        _norm_ticker(r.get("ticker")): r
        for r in kis_trade_rows
        if str(r.get("action", "")).upper() == "BUY"
    }
    out: List[Dict[str, Any]] = []
    seen = set()

    def _csv_trade_fields(trade: Dict[str, Any], name: str) -> Dict[str, Any]:
        t = _norm_ticker(trade.get("ticker"))
        status = str(trade.get("order_status") or "").lower()
        exe_qty = _safe_int(trade.get("executed_qty") or trade.get("quantity"))
        tr = track_by_ticker.get(t, {})
        return {
            "asset_class": _asset_class(t, bond_tickers),
            "is_paper_db_only": False,
            "is_actual_kis_order": _is_actual_kis_order_row(trade),
            "order_id": trade.get("order_id"),
            "executed": exe_qty > 0 and status in ("executed", "partial", "completed"),
            "executed_qty": exe_qty,
            "executed_price": _safe_int(trade.get("price")),
            "executed_amount": _safe_int(trade.get("amount")),
            "order_status": status,
            "reconcile_method": _infer_reconcile_method(trade, reconcile_map),
            "name": name or _lookup_name(t, candidates or [], balance_rows or [], bond_cfg),
            "base_price": tr.get("base_price"),
            "return_1d": tr.get("return_1d"),
            "return_3d": tr.get("return_3d"),
            "return_5d": tr.get("return_5d"),
            "return_10d": tr.get("return_10d"),
        }

    for row in gpt_rows:
        t = row.get("ticker")
        seen.add(t)
        tr = track_by_ticker.get(t, {})
        trade = trade_by_ticker.get(t, {})
        fields = _csv_trade_fields(trade, row.get("name") or "") if trade else {
            "asset_class": _asset_class(t, bond_tickers),
            "is_paper_db_only": False,
            "is_actual_kis_order": False,
            "order_id": None,
            "executed": _safe_int(row.get("executed_qty")) > 0,
            "executed_qty": row.get("executed_qty"),
            "executed_price": tr.get("executed_price"),
            "executed_amount": row.get("executed_amount"),
            "order_status": row.get("order_status"),
            "reconcile_method": row.get("reconcile_method"),
            "name": row.get("name"),
            "base_price": tr.get("base_price"),
            "return_1d": tr.get("return_1d"),
            "return_3d": tr.get("return_3d"),
            "return_5d": tr.get("return_5d"),
            "return_10d": tr.get("return_10d"),
        }
        out.append({
            "date": date_str,
            "market": market,
            "ticker": t,
            "screener_rank": row.get("screener_rank"),
            "screener_score": row.get("screener_score"),
            "gpt_action": row.get("gpt_action"),
            "gpt_score": row.get("gpt_score"),
            "final_decision": row.get("final_decision"),
            "reason": (row.get("gpt_reason") or "")[:200],
            **fields,
        })

    for trade in kis_trade_rows:
        if str(trade.get("action", "")).upper() != "BUY":
            continue
        t = _norm_ticker(trade.get("ticker"))
        if t in seen:
            continue
        tr = track_by_ticker.get(t, {})
        fields = _csv_trade_fields(trade, tr.get("name", ""))
        out.append({
            "date": date_str,
            "market": market,
            "ticker": t,
            "screener_rank": tr.get("screener_rank"),
            "screener_score": tr.get("screener_score"),
            "gpt_action": None,
            "gpt_score": None,
            "final_decision": "EXECUTED",
            "reason": "",
            **fields,
        })
    return out


def _render_markdown(report: Dict[str, Any]) -> str:
    date_str = report.get("date", "")
    market = report.get("market", "")
    summary = report.get("summary") or {}
    funnel = report.get("screener_funnel") or {}
    gpt = report.get("gpt_decision") or {}
    budget = report.get("budget_usage") or {}
    execution = report.get("execution_performance") or {}
    system = report.get("system_performance") or {}
    tracking = report.get("candidate_tracking") or []
    warnings = report.get("warnings") or []
    historical_warnings = report.get("historical_warnings") or []
    aggregates = gpt.get("aggregates") or {}
    trade_rows = report.get("_kis_trade_rows") or report.get("_trade_rows") or []
    reconcile_map = report.get("_reconcile_map") or {}
    candidates = report.get("_candidates") or []
    balance_rows = report.get("_balance_rows") or []
    bond_cfg = report.get("_bond_cfg") or []

    lines = [
        f"# Performance Review - {date_str} {market}",
        "",
        "## 1. Summary",
        "",
        f"- 총자산: {summary.get('total_assets', 0):,}원",
        f"- 보유종목 수: {summary.get('holdings_count', 0)}",
        f"- 당일 신규매수 수: {summary.get('new_buy_count', 0)}",
        f"- pending 주문 수: {summary.get('pending_orders', 0)}",
        f"- 총평가금액: {summary.get('tot_evlu_amt', 0):,}원",
        f"- D+2 출금가능금액: {summary.get('prvs_rcdl_excc_amt', 0):,}원",
    ]
    lp_egw = system.get("latest_execution_egw00201_count", system.get("latest_pipeline_egw00201_count", system.get("egw00201_count", 0)))
    if lp_egw > 0:
        lines.append(f"- EGW00201 rate limit: {lp_egw}회 발생 (최신 실행)")
    daily_egw = system.get("daily_pipeline_egw00201_count", 0)
    if daily_egw > 0:
        lines.append(f"- EGW00201 rate limit: {daily_egw}회 발생 (당일 파이프라인)")
    lines.extend([
        "",
        "## 2. Screener Funnel",
        "",
        "| 단계 | 종목 수 |",
        "|------|--------:|",
    ])

    funnel_stages = [
        ("전체 유니버스", funnel.get("total_universe_count"), True),
        ("Marcap 통과", funnel.get("marcap_pass_count"), True),
        ("Amount5D 통과", funnel.get("amount5d_pass_count"), True),
        ("최종 후보", funnel.get("final_screener_candidate_count"), True),
        ("스코어 저장", funnel.get("screener_score_count"), True),
        ("GPT 분석", funnel.get("gpt_analysis_count"), True),
        ("GPT BUY", funnel.get("gpt_buy_count"), False),
        ("GPT HOLD", funnel.get("gpt_hold_count"), False),
        ("GPT REJECT", funnel.get("gpt_reject_count"), False),
        ("체결 BUY", funnel.get("executed_buy_count"), False),
        ("주문 제출", execution.get("submitted_order_count"), False),
        ("Pending", execution.get("pending_order_count"), False),
    ]
    prev = None
    for label, cnt, show_delta in funnel_stages:
        if cnt is None:
            lines.append(f"| {label} | - |")
            if show_delta:
                prev = None
            continue
        drop = ""
        if show_delta and prev is not None:
            drop = f" (−{prev - cnt})"
        lines.append(f"| {label} | {cnt}{drop} |")
        if show_delta:
            prev = cnt

    lines.extend(["", "## 3. GPT Decision", "", "| Action | Count | Avg GPT Score | Avg Screener | Executed | Amount |", "|--------|------:|--------------:|-------------:|---------:|-------:|"])
    for action in ("BUY", "HOLD", "REJECT"):
        agg = aggregates.get(action) or {}
        lines.append(
            f"| {action} | {agg.get('count', 0)} | {agg.get('avg_gpt_score', '-')} | "
            f"{agg.get('avg_screener_score', '-')} | {agg.get('executed_count', 0)} | "
            f"{agg.get('executed_amount', 0):,} |"
        )

    lines.extend(["", "## 4. Executed Trades", "", "| Ticker | Name | Action | Qty | Price | Amount | Status | Reconcile |", "|--------|------|--------|----:|------:|-------:|--------|-----------|"])
    for row in trade_rows:
        if str(row.get("action", "")).upper() != "BUY":
            continue
        status = str(row.get("order_status") or "").lower()
        if status not in ("executed", "partial", "completed"):
            continue
        qty = _safe_int(row.get("executed_qty") or row.get("quantity"))
        price = _safe_int(row.get("price"))
        amount = _safe_int(row.get("amount")) or qty * price
        method = _infer_reconcile_method(row, reconcile_map)
        name = _lookup_name(_norm_ticker(row.get("ticker")), candidates, balance_rows, bond_cfg)
        lines.append(
            f"| {_norm_ticker(row.get('ticker'))} | {name} | BUY | {qty} | {price:,} | {amount:,} | {status} | {method} |"
        )

    lines.extend([
        "",
        "## 5. Budget Usage",
        "",
        "| 항목 | 값 |",
        "|------|-----|",
        f"| Total Assets | {budget.get('total_assets', summary.get('total_assets', 0)):,} |",
        f"| Target Stock | {budget.get('target_stock_value', 0):,} ({(budget.get('stock_target_weight', 0) or 0)*100:.0f}%) |",
        f"| Current Stock | {budget.get('current_stock_value', 0):,} |",
        f"| Stock Buy Budget | {budget.get('stock_buy_budget', 0):,} |",
        f"| Actual Stock Buy | {budget.get('actual_stock_buy_amount', 0):,} |",
        f"| Actual Bond Buy | {budget.get('actual_bond_buy_amount', 0):,} |",
        f"| Total Executed Buy | {budget.get('total_executed_buy_amount', 0):,} |",
        f"| Stock Budget Usage Rate | {budget.get('stock_budget_usage_rate', '-')} |",
        f"| Unused Stock Budget | {budget.get('unused_stock_budget', 0):,} |",
        f"| Unused Reason | {budget.get('unused_reason', '-')} |",
        "",
        "## 6. Execution Performance",
        "",
        "| 항목 | 값 |",
        "|------|-----|",
        f"| Submitted | {execution.get('submitted_order_count', 0)} |",
        f"| Executed | {execution.get('executed_order_count', 0)} |",
        f"| Pending | {execution.get('pending_order_count', 0)} |",
        f"| Execution Rate | {execution.get('execution_rate', '-')} |",
        f"| Holding Fallback Reconciled | {execution.get('reconciled_by_holding_count', 0)} |",
        f"| Unreconciled | {execution.get('unreconciled_count', 0)} |",
        "",
        "## 7. Candidate Tracking",
        "",
        "| Ticker | Name | Rank | GPT | Base | 1D | 3D | 5D |",
        "|--------|------|-----:|-----|-----:|---:|---:|---:|",
    ])
    for row in tracking[:30]:
        lines.append(
            f"| {row.get('ticker')} | {row.get('name', '')} | {row.get('screener_rank', '-')} | "
            f"{row.get('gpt_action', '-')} | {row.get('base_price', '-')} | "
            f"{row.get('return_1d', '-')} | {row.get('return_3d', '-')} | {row.get('return_5d', '-')} |"
        )

    lines.extend([
        "",
        "## 8. System Performance",
        "",
        "### Latest Execution Run",
        "",
        f"- Method: {system.get('latest_execution_detect_method', system.get('latest_pipeline_detect_method', '-'))}",
        f"- Files: {', '.join(system.get('latest_execution_log_files') or system.get('latest_pipeline_log_files') or []) or '-'}",
        f"- EGW00201: {system.get('latest_execution_egw00201_count', system.get('latest_pipeline_egw00201_count', 0))}",
        f"- Traceback: {system.get('latest_execution_traceback_count', system.get('latest_pipeline_traceback_count', 0))}",
        f"- Error: {system.get('latest_execution_error_count', system.get('latest_pipeline_error_count', 0))}",
        f"- Runtime: {_fmt_runtime_bullet(system.get('latest_execution_runtime_sec'))}",
        "",
        "### Daily Pipeline Group",
        "",
        f"- Method: {system.get('daily_pipeline_detect_method', '-')}",
        "- Files:",
    ])
    daily_files = system.get("daily_pipeline_log_files") or []
    if daily_files:
        for fname in daily_files:
            lines.append(f"  - {fname}")
    else:
        lines.append("  - -")
    lines.extend([
        f"- EGW00201: {system.get('daily_pipeline_egw00201_count', 0)}",
        f"- Traceback: {system.get('daily_pipeline_traceback_count', 0)}",
        f"- Error: {system.get('daily_pipeline_error_count', 0)}",
        f"- Runtime: {_fmt_runtime_bullet(system.get('daily_pipeline_runtime_sec'))}",
        "",
        "| Stage | Runtime (s) |",
        "|-------|------------:|",
    ])
    daily_stages = system.get("daily_pipeline_stage_runtime_sec") or {}
    lines.append(f"| Total | {_fmt_runtime_cell(system.get('daily_pipeline_runtime_sec'))} |")
    lines.append(f"| Screener | {_fmt_runtime_cell(daily_stages.get('screener_runtime_sec'))} |")
    lines.append(f"| News | {_fmt_runtime_cell(daily_stages.get('news_collector_runtime_sec'))} |")
    lines.append(f"| GPT | {_fmt_runtime_cell(daily_stages.get('gpt_analyzer_runtime_sec'))} |")
    lines.append(f"| Trader | {_fmt_runtime_cell(daily_stages.get('trader_runtime_sec'))} |")
    lines.append(f"| Reconciler | {_fmt_runtime_cell(daily_stages.get('order_reconciler_runtime_sec'))} |")
    lines.extend([
        "",
        "## 9. Warnings",
        "",
    ])
    if warnings:
        for w in warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- 없음")
    lines.extend([
        "",
        "## 10. Historical Warnings",
        "",
    ])
    if historical_warnings:
        for w in historical_warnings[:30]:
            lines.append(f"- {w}")
        if len(historical_warnings) > 30:
            lines.append(f"- ... 외 {len(historical_warnings) - 30}건 (JSON warnings_detail 참조)")
    else:
        lines.append("- 없음")
    lines.append("")
    return "\n".join(lines)


def build_performance_review(date_str: str, market: str, *, fetch_tracking_prices: bool = True) -> Dict[str, Any]:
    candidates = _load_json(_artifact_path("screener_candidates", date_str, market)) or []
    scores = _load_json(_artifact_path("screener_scores", date_str, market)) or []
    gpt_data = _load_json(_artifact_path("gpt_trades", date_str, market)) or {}
    gpt_plans = gpt_data.get("plans") if isinstance(gpt_data, dict) else (gpt_data or [])
    if not isinstance(gpt_plans, list):
        gpt_plans = []
    balance_data = _load_json(_artifact_path("balance", date_str, market))
    summary_data = _load_json(_artifact_path("summary", date_str, market))
    balance_rows = _parse_balance_rows(balance_data)
    account_summary, account_warnings = _extract_account_summary(summary_data, balance_data)

    raw_trade_rows = _load_trade_rows(date_str)
    kis_trade_rows, paper_db_only_rows = _normalize_trade_rows(raw_trade_rows)
    log_files = _find_log_files(date_str)
    scoped_counts, warnings_detail = _parse_scoped_log_events(date_str, log_files)

    latest_pipeline_paths: List[Path] = []
    daily_pipeline_paths: List[Path] = []
    seen_log_paths: set = set()
    for name in (scoped_counts.get("daily_pipeline_log_files") or []):
        for base in (OUTPUT_DIR, OUTPUT_DIR / "debug"):
            p = base / name
            if p.is_file():
                key = str(p.resolve())
                if key not in seen_log_paths:
                    seen_log_paths.add(key)
                    daily_pipeline_paths.append(p)
                break
    for name in (scoped_counts.get("latest_execution_log_files")
                 or scoped_counts.get("latest_pipeline_log_files") or []):
        for base in (OUTPUT_DIR, OUTPUT_DIR / "debug"):
            p = base / name
            if p.is_file():
                key = str(p.resolve())
                if key not in seen_log_paths:
                    seen_log_paths.add(key)
                    latest_pipeline_paths.append(p)
                break

    log_text = ""
    for p in daily_pipeline_paths + latest_pipeline_paths:
        try:
            log_text += p.read_text(encoding="utf-8", errors="ignore") + "\n"
        except Exception:
            pass
    if not log_text:
        for p in log_files[:5]:
            try:
                log_text += p.read_text(encoding="utf-8", errors="ignore") + "\n"
            except Exception:
                pass

    reconcile_map = _parse_reconcile_holding_map(log_text)

    rank_map, score_map = _build_screener_maps(candidates, scores)
    screener_funnel = _build_screener_funnel(
        date_str, market, candidates, scores, gpt_plans, kis_trade_rows, log_text,
        latest_pipeline_paths=daily_pipeline_paths or latest_pipeline_paths,
    )
    gpt_decision = _build_gpt_decision(gpt_plans, rank_map, score_map, kis_trade_rows, reconcile_map)
    budget_usage = _build_budget_usage(
        date_str, log_text, account_summary, balance_rows, kis_trade_rows, gpt_plans
    )
    execution_performance = _build_execution_performance(
        kis_trade_rows, reconcile_map, paper_db_only_rows
    )
    system_performance = _build_system_performance(date_str, log_text, scoped_counts)
    candidate_tracking = _build_candidate_tracking_snapshot(
        date_str, market, candidates, gpt_plans, kis_trade_rows, balance_rows
    )
    summary = _build_summary(date_str, account_summary, balance_rows, kis_trade_rows, execution_performance)
    warnings, historical_warnings = _build_warnings(
        system_performance, execution_performance, budget_usage, account_warnings, warnings_detail
    )

    if fetch_tracking_prices:
        _update_tracking_returns(date_str, market, fetch_prices=True)

    tracking_path = OUTPUT_DIR / f"candidate_tracking_{date_str}_{market}.json"
    tracking_doc = {
        "base_date": date_str,
        "market": market,
        "generated_at": datetime.now(KST).isoformat(),
        "candidates": candidate_tracking,
    }
    with open(tracking_path, "w", encoding="utf-8") as f:
        json.dump(tracking_doc, f, ensure_ascii=False, indent=2)

    aa = settings.asset_allocation if hasattr(settings, "asset_allocation") else {}
    bond_cfg = aa.get("bond_etfs") if isinstance(aa, dict) else []

    return {
        "date": date_str,
        "market": market,
        "generated_at": datetime.now(KST).isoformat(),
        "summary": summary,
        "screener_funnel": screener_funnel,
        "gpt_decision": gpt_decision,
        "budget_usage": budget_usage,
        "execution_performance": execution_performance,
        "system_performance": system_performance,
        "candidate_tracking": candidate_tracking,
        "warnings": warnings,
        "historical_warnings": historical_warnings,
        "warnings_detail": warnings_detail,
        "paper_db_only_records": [
            {
                "id": r.get("id"),
                "ticker": _norm_ticker(r.get("ticker")),
                "action": r.get("action"),
                "order_status": r.get("order_status"),
                "order_id": r.get("order_id"),
                "amount": r.get("amount"),
                "is_paper_db_only": True,
            }
            for r in paper_db_only_rows
        ],
        "_kis_trade_rows": kis_trade_rows,
        "_trade_rows": kis_trade_rows,
        "_reconcile_map": reconcile_map,
        "_candidates": candidates,
        "_balance_rows": balance_rows,
        "_bond_cfg": bond_cfg,
        "sources": {
            "log_files": [str(p) for p in log_files[:5]],
            "balance": str(_artifact_path("balance", date_str, market)),
            "summary": str(_artifact_path("summary", date_str, market)),
            "gpt_trades": str(_artifact_path("gpt_trades", date_str, market)),
        },
    }


def write_outputs(report: Dict[str, Any]) -> Tuple[Path, Path, Path]:
    date_str = report["date"]
    market = report["market"]
    base = OUTPUT_DIR / f"performance_review_{date_str}_{market}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    csv_path = base.with_suffix(".csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in report.items() if not str(k).startswith("_")}, f, ensure_ascii=False, indent=2)

    md_path.write_text(_render_markdown(report), encoding="utf-8")

    gpt_rows = (report.get("gpt_decision") or {}).get("rows") or []
    tracking_rows = report.get("candidate_tracking") or []
    kis_trade_rows = report.get("_kis_trade_rows") or report.get("_trade_rows") or []
    reconcile_map = report.get("_reconcile_map") or {}
    candidates = report.get("_candidates") or []
    balance_rows = report.get("_balance_rows") or []
    bond_cfg = report.get("_bond_cfg") or []
    csv_rows = _csv_rows(
        date_str, market, gpt_rows, tracking_rows, kis_trade_rows,
        reconcile_map, candidates, balance_rows, bond_cfg,
    )
    fieldnames = [
        "date", "market", "ticker", "name", "asset_class",
        "is_paper_db_only", "is_actual_kis_order", "order_id",
        "screener_rank", "screener_score",
        "gpt_action", "gpt_score", "final_decision", "executed", "executed_qty",
        "executed_price", "executed_amount", "order_status", "reconcile_method",
        "base_price", "return_1d", "return_3d", "return_5d", "return_10d", "reason",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)

    logger.info("저장 완료: %s, %s, %s", json_path, csv_path, md_path)
    return json_path, csv_path, md_path


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="일별 전략·시스템 성능 리뷰 생성")
    parser.add_argument("--date", help="리뷰 날짜 YYYYMMDD (기본: 오늘 KST)")
    parser.add_argument("--market", default="KOSPI", help="시장 (기본: KOSPI)")
    parser.add_argument("--no-fetch-prices", action="store_true", help="추적 수익률 KIS 시세 조회 생략")
    args = parser.parse_args(argv)

    date_str = _norm_date(args.date)
    market = str(args.market or "KOSPI").upper()
    load_config()

    logger.info("성능 리뷰 생성 시작: date=%s market=%s", date_str, market)
    report = build_performance_review(
        date_str,
        market,
        fetch_tracking_prices=not args.no_fetch_prices,
    )
    write_outputs(report)
    return 0


def _smoke_test_accuracy_fixes() -> None:
    """20260625 시나리오 단위 검증 (로컬 fixture 없이 helper 단위)."""
    summary_payload = {
        "comments": {"nass_amt": "순자산"},
        "data": [{"0": {
            "nass_amt": "30,015,930",
            "tot_evlu_amt": "30,015,930",
            "prvs_rcdl_excc_amt": "22,508,720",
            "dnca_tot_amt": "1,000,000",
        }}],
        "status": "ok",
    }
    acct, warns = _extract_account_summary(summary_payload, None)
    assert acct["total_assets"] == 30015930, acct
    assert acct["prvs_rcdl_excc_amt"] == 22508720, acct
    assert "total_assets_not_found" not in warns

    load_config()
    raw_trades = [
        {"id": 85, "ticker": "459580", "action": "BUY", "order_status": "paper_executed",
         "order_id": None, "amount": 5385000, "quantity": 5, "price": 1077000},
        {"id": 86, "ticker": "459580", "action": "BUY", "order_status": "paper_executed",
         "order_id": None, "amount": 5385000, "quantity": 5, "price": 1077000},
        {"id": 87, "ticker": "459580", "action": "BUY", "order_status": "executed",
         "order_id": "0000008839", "amount": 5385000, "quantity": 5, "price": 1077000},
        {"id": 88, "ticker": "004170", "action": "BUY", "order_status": "executed",
         "order_id": "0000014720", "amount": 1057000, "quantity": 1, "price": 1057000},
        {"id": 89, "ticker": "032830", "action": "BUY", "order_status": "executed",
         "order_id": "0000014653", "amount": 1057000, "quantity": 3, "price": 352333},
    ]
    kis, paper = _normalize_trade_rows(raw_trades)
    assert len(paper) == 2
    assert len(kis) == 3
    assert sum(1 for r in kis if _norm_ticker(r["ticker"]) == "459580") == 1

    log = (
        "[RECONCILE_BY_HOLDING] order_id=0000014720 ticker=004170\n"
        "[RECONCILE_BY_HOLDING] order_id=0000014653 ticker=032830\n"
    )
    rmap = _parse_reconcile_holding_map(log)
    assert rmap["0000014720"] == "holding_fallback"
    assert _infer_reconcile_method(
        {"order_id": "0000014720", "order_status": "executed"}, rmap
    ) == "holding_fallback"
    assert _infer_reconcile_method(
        {"order_id": "0000008839", "order_status": "executed"}, rmap
    ) == "order_query"

    exec_perf = _build_execution_performance(kis, rmap, paper)
    assert exec_perf["submitted_order_count"] == 3
    assert exec_perf["executed_order_count"] == 3
    assert exec_perf["reconciled_by_holding_count"] == 2
    assert exec_perf["pending_order_count"] == 0

    budget = _build_budget_usage("20260625", "", acct, [], kis, [])
    assert budget["actual_stock_buy_amount"] == 2114000
    assert budget["actual_bond_buy_amount"] == 5385000
    assert budget["total_executed_buy_amount"] == 7499000
    assert budget["executed_stock_buy_count"] == 2
    assert budget["executed_bond_buy_count"] == 1
    assert budget["stock_buy_budget"] > 0

    # scoped warning 집계 — 파일명 기반 latest pipeline
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        old_output = OUTPUT_DIR
        globals()["OUTPUT_DIR"] = tmp

        hist_log = tmp / "manual_full_pipeline_20260625_095009.log"
        hist_log.write_text(
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n"
            "[20260625-095009] - ERROR - old pipeline error\n",
            encoding="utf-8",
        )
        latest_log = tmp / "manual_full_pipeline_after_fix_20260625_101015.log"
        latest_log.write_text(
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n"
            "[20260625-101015] - ERROR - latest pipeline error\n",
            encoding="utf-8",
        )
        mgr_log = tmp / "integrated_manager.log"
        mgr_log.write_text(
            "[20260625-101015] KIS API EGW00201 rate limit\n"
            "[20260625-101015] KIS API EGW00201 rate limit\n",
            encoding="utf-8",
        )

        orig_loader = _load_pipeline_state
        globals()["_load_pipeline_state"] = lambda _d: {"run_id": ""}
        scoped, details = _parse_scoped_log_events("20260625", [hist_log, latest_log, mgr_log])
        globals()["_load_pipeline_state"] = orig_loader
        globals()["OUTPUT_DIR"] = old_output

        assert scoped["latest_pipeline_log_file"] == "manual_full_pipeline_after_fix_20260625_101015.log"
        assert scoped["latest_pipeline_detect_method"] in ("filename_timestamp", "full_pipeline_log")
        assert scoped["latest_pipeline_egw00201_count"] == 6
        assert scoped["historical_egw00201_count"] == 2
        assert scoped["all_related_logs_egw00201_count"] == 8
        assert scoped["egw00201_count"] == 6
        assert scoped["kis_rate_limit_count"] == 6
        assert scoped["latest_pipeline_error_count"] == 1
        assert scoped["historical_error_count"] == 1
        assert scoped["current_run_error_count"] == 0
        assert scoped["performance_review_traceback"] == 0
        latest_details = [d for d in details if d.get("type") == "egw00201" and d.get("scope") == "latest_execution"]
        hist_details = [d for d in details if d.get("type") == "egw00201" and d.get("scope") == "historical_log"]
        assert len(latest_details) == 6
        assert len(hist_details) == 2
        assert sum(1 for d in latest_details if "manual_full_pipeline_after_fix" in d["source_file"]) == 4
        assert sum(1 for d in latest_details if "integrated_manager.log" in d["source_file"]) == 2

    # 20260630 분할 수동 파이프라인 — daily group + resume_trader execution
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        old_output = OUTPUT_DIR
        globals()["OUTPUT_DIR"] = tmp

        screener = tmp / "manual_screener_20260630_094652.log"
        screener.write_text(
            "│ 전체 종목                   918 \n"
            "│ Marcap Pass                 131 \n"
            "│ Amount5D Pass               104 \n"
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n"
            "KIS API EGW00201 rate limit\n",
            encoding="utf-8",
        )
        (tmp / "manual_news_collector_20260630_100735.log").write_text("news\n", encoding="utf-8")
        (tmp / "manual_gpt_after_news_20260630_100806.log").write_text("gpt\n", encoding="utf-8")
        (tmp / "manual_trader_after_gpt_20260630_101001.log").write_text("trader\n", encoding="utf-8")
        reconcile = tmp / "manual_order_reconcile_pending_sell_20260630_101600.log"
        reconcile.write_text("reconcile\n", encoding="utf-8")
        old_full = tmp / "manual_full_pipeline_20260630_093332.log"
        old_full.write_text(
            "KIS API EGW00201 rate limit\n"
            "[20260630-093332] - ERROR - old full pipeline\n",
            encoding="utf-8",
        )
        resume = tmp / "manual_resume_trader_20260630_20260630_103151.log"
        resume.write_text("resume trader step\n", encoding="utf-8")

        orig_loader = _load_pipeline_state
        globals()["_load_pipeline_state"] = lambda _d: {"run_id": ""}
        scoped3, _ = _parse_scoped_log_events(
            "20260630",
            [screener, reconcile, old_full, resume],
        )
        globals()["_load_pipeline_state"] = orig_loader

        assert scoped3["latest_execution_detect_method"] == "resume_trader_run"
        assert "manual_resume_trader_20260630_20260630_103151.log" in (
            scoped3.get("latest_execution_log_files") or []
        )
        assert scoped3["latest_execution_egw00201_count"] == 0
        assert scoped3["daily_pipeline_detect_method"] == "split_manual_pipeline_logs"
        daily_files = scoped3.get("daily_pipeline_log_files") or []
        assert "manual_screener_20260630_094652.log" in daily_files
        assert "manual_news_collector_20260630_100735.log" in daily_files
        assert scoped3["daily_pipeline_egw00201_count"] == 3
        assert scoped3["historical_egw00201_count"] == 1

        # screener + resume_from_news 분할 재개 (20260701 패턴)
        screener2 = tmp / "manual_screener_20260701_094652.log"
        screener2.write_text("screener only\n", encoding="utf-8")
        resume_news = tmp / "manual_resume_from_news_20260701_120530.log"
        resume_news.write_text(
            "news step\n"
            "gpt step\n"
            "KIS API EGW00201 rate limit\n",
            encoding="utf-8",
        )
        timeout_full = tmp / "manual_full_from_screener_20260701_125848.log"
        timeout_full.write_text(
            "[PIPELINE_STAGE_TIMEOUT] stage=screener timeout_sec=1200\n"
            " - ERROR - screener timeout\n",
            encoding="utf-8",
        )
        scoped4, _ = _parse_scoped_log_events(
            "20260701",
            [screener2, resume_news, timeout_full],
        )
        assert scoped4["latest_execution_detect_method"] == "resume_from_news_run"
        assert "manual_resume_from_news_20260701_120530.log" in (
            scoped4.get("latest_execution_log_files") or []
        )
        daily4 = scoped4.get("daily_pipeline_log_files") or []
        assert "manual_screener_20260701_094652.log" in daily4
        assert "manual_resume_from_news_20260701_120530.log" in daily4
        assert scoped4["daily_pipeline_detect_method"] == "split_manual_pipeline_logs"
        assert scoped4["latest_execution_egw00201_count"] == 1
        assert scoped4["daily_pipeline_egw00201_count"] == 1
        assert scoped4["historical_error_count"] >= 1

        funnel_log, _ = _resolve_screener_funnel_log_text("20260630", [screener])
        parsed = _parse_funnel_from_logs(funnel_log)
        assert parsed["marcap_pass_count"] == 131
        assert parsed["amount5d_pass_count"] == 104

        reconcile_only = tmp / "manual_order_reconcile_pending_sell_20260630_120000.log"
        reconcile_only.write_text("solo reconcile\n", encoding="utf-8")
        groups = _resolve_pipeline_log_groups("20260630", [reconcile_only], "")
        assert groups.get("daily_pipeline_detect_method") == "incomplete" or not groups.get("daily_pipeline_log_files")

        globals()["OUTPUT_DIR"] = old_output

    # bracket run_id fallback
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as tf:
        tf.write(
            "[20260625-100000] KIS API EGW00201 rate limit\n"
            "[20260625-100000] KIS API EGW00201 rate limit\n"
            "[20260625-143022] KIS API EGW00201 rate limit\n"
            "[20260625-143022] KIS API EGW00201 rate limit\n"
            "[20260625-143022] KIS API EGW00201 rate limit\n"
            "[20260625-143022] KIS API EGW00201 rate limit\n"
        )
        bracket_path = Path(tf.name)
    try:
        orig_loader = _load_pipeline_state
        globals()["_load_pipeline_state"] = lambda _d: {"run_id": "20260625-143022"}
        scoped2, _ = _parse_scoped_log_events("20260625", [bracket_path])
        globals()["_load_pipeline_state"] = orig_loader
        assert scoped2["latest_pipeline_egw00201_count"] == 4
        assert scoped2["historical_egw00201_count"] == 2
    finally:
        bracket_path.unlink(missing_ok=True)

    print("smoke_test_accuracy_fixes: OK")


if __name__ == "__main__":
    import sys
    if "--smoke-test" in sys.argv:
        load_config()
        _smoke_test_accuracy_fixes()
        raise SystemExit(0)
    raise SystemExit(main())
