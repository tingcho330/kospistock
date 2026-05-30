"""프로젝트 .env 로딩 (config/.env 우선)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

_LOADED = False


def dotenv_candidates() -> List[Path]:
    root = Path(__file__).resolve().parents[1]
    return [
        Path("/app/config/.env"),
        root / "config" / ".env",
        Path.cwd() / "config" / ".env",
        Path.cwd() / ".env",
    ]


def load_project_env(*, override: bool = False) -> List[str]:
    """첫 호출 시 config/.env 등을 로드. 이미 로드됐으면 재로드하지 않음(override=True 제외)."""
    global _LOADED
    if _LOADED and not override:
        return []

    from dotenv import load_dotenv

    loaded: List[str] = []
    for path in dotenv_candidates():
        if path.is_file() and load_dotenv(dotenv_path=path, override=override):
            loaded.append(str(path))

    if loaded:
        _LOADED = True
    return loaded
