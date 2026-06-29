# api/kis_errors.py
"""KIS API 공통 예외·응답 파싱 (kis_auth ↔ domestic_stock 순환 import 방지)."""
from __future__ import annotations

from typing import Any, Optional

# KIS OAuth·API 토큰 무효/만료 코드 (EGW00133=발급 rate limit 은 제외)
_KIS_TOKEN_AUTH_MSG_CDS = frozenset({"EGW00121", "EGW00123"})


def is_kis_token_auth_error(
    *,
    msg_cd: Optional[str] = None,
    msg1: Optional[str] = None,
    text: str = "",
) -> bool:
    """서버가 토큰 무효·만료를 알릴 때 True (EGW00121/EGW00123 등)."""
    code = str(msg_cd or "").strip()
    if code in _KIS_TOKEN_AUTH_MSG_CDS:
        return True

    blob = f"{msg1 or ''} {text or ''}"
    upper = blob.upper()
    if not ("TOKEN" in upper or "토큰" in blob):
        return False

    if any(marker in upper for marker in ("EGW00121", "EGW00123", "INVALID", "EXPIRE")):
        return True
    if any(marker in blob for marker in ("유효하지", "만료")):
        return True
    return False


def is_kis_token_auth_error_from_response(resp: Any) -> bool:
    """requests.Response 기반 토큰 무효·만료 감지."""
    status_code = getattr(resp, "status_code", None)
    if status_code == 401:
        return True
    try:
        data = resp.json()
        if isinstance(data, dict):
            return is_kis_token_auth_error(
                msg_cd=data.get("msg_cd") or data.get("error_code"),
                msg1=data.get("msg1") or data.get("error_description"),
                text=str(data),
            )
    except Exception:
        pass
    return is_kis_token_auth_error(text=getattr(resp, "text", None) or str(resp))


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
    if msg_cd == "EGW00123" or (msg1 and "만료" in msg1 and "token" in (msg1 or "").lower()):
        message = f"{prefix}KIS 토큰 만료 (EGW00123)"
    elif msg_cd == "EGW00121" or (msg1 and "유효하지" in msg1 and "token" in (msg1 or "").lower()):
        message = f"{prefix}KIS 토큰 무효 (EGW00121)"
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
