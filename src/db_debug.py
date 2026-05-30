#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DB 기록 디버깅 전용 유틸 (비즈니스 로직 변경 없음).

활성화:
  export DB_RECORD_DEBUG=1

선택:
  export DB_DEBUG_LOG_FILE=output/debug/db_record_debug.log

로그 필터:
  grep '\\[DB_DEBUG\\]' ...
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_PREFIX = "[DB_DEBUG]"
_LOGGER_NAME = "DB_RECORD_DEBUG"
_file_handler_attached = False


def is_enabled() -> bool:
    return os.getenv("DB_RECORD_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def _ensure_handlers() -> None:
    global _file_handler_attached
    log = _logger()
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        log.propagate = True
        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(sh)

    log_file = os.getenv("DB_DEBUG_LOG_FILE", "output/debug/db_record_debug.log").strip()
    if _file_handler_attached or not log_file:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
        _file_handler_attached = True
    except Exception as e:
        log.warning(f"{_PREFIX} file handler attach failed: {e}")


def caller(depth: int = 2) -> str:
    """호출 위치 (파일:함수:라인)."""
    try:
        frame = inspect.stack()[depth]
        fname = Path(frame.filename).name
        return f"{fname}:{frame.function}:{frame.lineno}"
    except Exception:
        return "unknown"


def _safe_json(obj: Any, max_len: int = 1200) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + "...(truncated)"
    return s


def log(step: str, level: int = logging.INFO, **fields: Any) -> None:
    if not is_enabled():
        return
    _ensure_handlers()
    payload = {"step": step, "ts": datetime.now().isoformat(timespec="seconds"), **fields}
    msg = f"{_PREFIX} {_safe_json(payload)}"
    _logger().log(level, msg)


def log_trade_in(step: str, trade_data: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
    td = trade_data or {}
    log(
        step,
        caller=caller(3),
        ticker=td.get("ticker"),
        side=td.get("side"),
        qty=td.get("qty"),
        price=td.get("price"),
        trade_status=td.get("trade_status"),
        order_id=td.get("order_id") or td.get("odno") or td.get("ODNO"),
        requested_qty=td.get("requested_qty"),
        executed_qty=td.get("executed_qty"),
        debug_context=td.get("_debug_context"),
        res_status_keys=list((extra.get("res") or {}).keys()) if isinstance(extra.get("res"), dict) else None,
        **{k: v for k, v in extra.items() if k != "res"},
    )


def log_trade_out(step: str, ok: bool, path: str = "", row_id: Optional[int] = None, **extra: Any) -> None:
    log(step, ok=ok, persist_path=path, row_id=row_id, **extra)


def log_skip(step: str, reason: str, **fields: Any) -> None:
    log(step, level=logging.WARNING, skipped=True, reason=reason, **fields)
