# api/kis_errors.py
"""KIS API 공통 예외·응답 파싱 (kis_auth ↔ domestic_stock 순환 import 방지)."""
from __future__ import annotations

from typing import Any, Dict, Optional


class KISAPIError(Exception):
    """KIS API 호출 실패."""

    def __init__(
        self,
        message: str,
        *,
        msg_cd: Optional[str] = None,
        msg1: Optional[str] = None,
        rt_cd: Optional[str] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.rt_cd = rt_cd
        self.status_code = status_code

    def summary(self) -> str:
        parts = [str(self)]
        if self.msg_cd:
            parts.append(f"msg_cd={self.msg_cd}")
        if self.msg1:
            parts.append(f"msg1={self.msg1}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " | ".join(parts)


def parse_kis_api_error(resp: Any, context: str = "") -> KISAPIError:
    """requests.Response 또는 유사 객체에서 KISAPIError 생성."""
    status_code = getattr(resp, "status_code", None)
    msg_cd = msg1 = rt_cd = None
    body_snippet = ""

    try:
        data = resp.json()
        if isinstance(data, dict):
            msg_cd = str(data.get("msg_cd") or data.get("error_code") or "").strip() or None
            msg1 = str(data.get("msg1") or data.get("error_description") or data.get("message") or "").strip() or None
            rt_cd = str(data.get("rt_cd") or "").strip() or None
            body_snippet = str(data)[:200]
        else:
            body_snippet = str(data)[:200]
    except Exception:
        body_snippet = (getattr(resp, "text", None) or str(resp))[:200]

    prefix = f"{context}: " if context else ""
    if msg_cd == "EGW00123" or (msg1 and "만료" in msg1 and "token" in msg1.lower()):
        message = f"{prefix}KIS 토큰 만료 (EGW00123)"
    elif msg_cd == "EGW00133" or (msg1 and "1분당 1회" in msg1):
        message = f"{prefix}KIS 토큰 발급 rate limit (EGW00133)"
    elif msg_cd or msg1:
        message = f"{prefix}{msg_cd or 'KIS_ERROR'} — {msg1 or body_snippet}"
    else:
        message = f"{prefix}HTTP {status_code}: {body_snippet or 'unknown error'}"

    return KISAPIError(
        message,
        msg_cd=msg_cd,
        msg1=msg1,
        rt_cd=rt_cd,
        status_code=status_code,
    )
