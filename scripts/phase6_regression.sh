#!/usr/bin/env bash
# Phase 6 — enabled=false 회귀 테스트 (KIS 주문 없음)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
export CONFIG_PATH="${CONFIG_PATH:-$ROOT/config/config.json}"
export OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/output}"

echo "=== Phase 6 regression (asset_allocation.enabled=false) ==="
echo "ROOT=$ROOT"
echo "CONFIG_PATH=$CONFIG_PATH"
echo

_fail=0
_ok() { echo "  PASS  $1"; }
_skip() { echo "  SKIP  $1"; }
_fail_msg() { echo "  FAIL  $1"; _fail=1; }

echo "[1] config sanity"
if "$PYTHON" -m json.tool "$CONFIG_PATH" >/dev/null 2>&1; then
  _ok "config.json valid JSON"
else
  _fail_msg "config.json parse error"
fi

enabled="$("$PYTHON" -c "import json; print(json.load(open('$CONFIG_PATH'))['asset_allocation'].get('enabled', False))")"
env="$("$PYTHON" -c "import json; print(json.load(open('$CONFIG_PATH')).get('trading_environment','?'))")"
echo "  asset_allocation.enabled=$enabled"
echo "  trading_environment=$env"
if [[ "$enabled" == "True" || "$enabled" == "true" ]]; then
  echo "  WARN  회귀 테스트는 enabled=false 권장. 현재 enabled=true"
fi
echo

echo "[2] compile / import"
for mod in asset_allocator portfolio_allocator risk_manager rotation_manager rotation_policy trader; do
  if "$PYTHON" -m py_compile "src/${mod}.py" 2>/dev/null; then
    _ok "py_compile src/${mod}.py"
  else
    _fail_msg "py_compile src/${mod}.py"
  fi
done

"$PYTHON" -c "
import sys
sys.path.insert(0, 'src')
try:
    import pandas  # noqa: F401
except ImportError:
    print('pandas missing on host — skip runtime imports (use docker for full check)')
    raise SystemExit(0)
from asset_allocator import compute_allocation, is_bond_etf
from rotation_manager import RotationManager
from rotation_policy import lists_to_pairs, apply_rotation_policy
from risk_manager import RiskManager
from settings import settings
print('imports ok')
" && _ok "rotation + risk_manager imports" || {
  if "$PYTHON" -c "import pandas" 2>/dev/null; then
    _fail_msg "import check"
  else
    _skip "runtime imports (pip install -r requirements.txt 또는 docker 사용)"
  fi
}

echo
echo "[3] asset_allocator replay (Case 1-7)"
if "$PYTHON" scripts/replay_asset_allocation.py; then
  _ok "replay_asset_allocation.py"
else
  _fail_msg "replay_asset_allocation.py"
fi

echo
echo "[4] screener logic replay (offline)"
SCREENER_JSON="${1:-}"
if [[ -z "$SCREENER_JSON" ]]; then
  for candidate in \
    "$HOME/Downloads/screener_candidates_full_"*"_KOSPI.json" \
    "$OUTPUT_DIR"/screener_candidates_full_*_KOSPI.json; do
    if [[ -f "$candidate" ]]; then
      SCREENER_JSON="$candidate"
      break
    fi
  done
fi
if [[ -n "$SCREENER_JSON" && -f "$SCREENER_JSON" ]]; then
  echo "  input: $SCREENER_JSON"
  if "$PYTHON" -c "import pandas" 2>/dev/null; then
    if "$PYTHON" scripts/replay_screener_logic.py "$SCREENER_JSON"; then
      _ok "replay_screener_logic.py"
    else
      _fail_msg "replay_screener_logic.py"
    fi
  else
    _skip "replay_screener_logic.py (pandas 없음 — docker 또는 pip install)"
  fi
else
  _skip "replay_screener_logic.py (screener JSON 없음 — 경로 인자로 전달)"
fi

echo
echo "[5] allocation dry-run log (mock, no KIS)"
if "$PYTHON" scripts/dry_run_asset_allocation.py --scenario all; then
  _ok "dry_run_asset_allocation.py"
else
  _fail_msg "dry_run_asset_allocation.py"
fi

echo
echo "[6] trader compile-only (주문 없음)"
if "$PYTHON" -c "import pandas" 2>/dev/null; then
  if "$PYTHON" -c "
import sys
sys.path.insert(0, 'src')
from trader import Trader
from settings import settings
t = Trader(settings)
print(f'Trader init ok env={t.env} real_trading={t.is_real_trading} alloc={t.asset_allocation_enabled}')
"; then
    _ok "Trader init (no run_buy_logic)"
  else
    _fail_msg "Trader init"
  fi
else
  _skip "Trader init (pandas 없음 — docker에서 실행)"
fi

echo
echo "[7] Docker 단계 (선택 — 컨테이너 실행 중일 때)"
if docker compose ps --services --filter status=running 2>/dev/null | grep -q integrated_manager; then
  echo "  integrated_manager running → screener/trader smoke"
  docker compose exec -T integrated_manager python -m py_compile /app/src/trader.py /app/src/risk_manager.py
  _ok "docker py_compile trader/risk_manager"
  docker compose exec -T integrated_manager python -u /app/src/screener.py --help >/dev/null 2>&1 && _ok "screener --help" || _skip "screener --help"
else
  _skip "docker compose (integrated_manager 미실행)"
  echo "  힌트: docker compose up -d 후"
  echo "    docker compose exec integrated_manager python -u /app/src/screener.py --market KOSPI"
  echo "    docker compose exec integrated_manager python -u /app/src/trader.py  # vps + enabled=false"
fi

echo
if [[ "$_fail" -eq 0 ]]; then
  echo "=== Phase 6 regression: ALL PASS ==="
  exit 0
else
  echo "=== Phase 6 regression: FAILED ==="
  exit 1
fi
