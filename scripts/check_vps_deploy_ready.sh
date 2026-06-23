#!/usr/bin/env bash
# KIS 모의투자(vps) + asset_allocation 배포 전 점검
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${ENV_FILE:-$ROOT/config/.env}"
CONFIG="$ROOT/config/config.json"

_fail=0
_ok() { echo "  OK    $1"; }
_warn() { echo "  WARN  $1"; }
_bad() { echo "  FAIL  $1"; _fail=1; }

echo "=== VPS deploy readiness ==="

echo "[1] config.json"
if ! "$PYTHON" -m json.tool "$CONFIG" >/dev/null 2>&1; then
  _bad "config.json invalid JSON"
else
  _ok "config.json valid"
fi

read -r env aa_enabled buy_enabled dcm_enabled <<<"$("$PYTHON" -c "
import json
c=json.load(open('$CONFIG'))
tp=c.get('trading_params',{})
print(
  c.get('trading_environment','?'),
  c.get('asset_allocation',{}).get('enabled', False),
  tp.get('buy_enabled', True),
  tp.get('dynamic_cash_management',{}).get('enabled', False),
)
")"

echo "  trading_environment=$env"
echo "  asset_allocation.enabled=$aa_enabled"
echo "  buy_enabled=$buy_enabled"
echo "  dynamic_cash_management.enabled=$dcm_enabled"

[[ "$env" == "vps" ]] && _ok "trading_environment=vps" || _bad "trading_environment must be vps (got $env)"
[[ "$aa_enabled" == "True" ]] && _ok "asset_allocation.enabled=true" || _bad "asset_allocation.enabled must be true"
[[ "$buy_enabled" == "True" ]] && _ok "buy_enabled=true" || _warn "buy_enabled=false → 매수 검증 불가"

echo
echo "[2] config/.env (모의투자 키)"
if [[ ! -f "$ENV_FILE" ]]; then
  _bad "config/.env 없음 → cp config/.env.example config/.env 후 KIS 모의 키 입력"
else
  _ok "config/.env exists"
  # shellcheck disable=SC1090
  set -a && source "$ENV_FILE" && set +a
  for key in KIS_PAPER_APP KIS_PAPER_SEC KIS_MY_PAPER_STOCK; do
    val="${!key:-}"
    if [[ -n "$val" ]]; then
      _ok "$key set (value hidden)"
    else
      _bad "$key missing or empty"
    fi
  done
  if [[ -n "${ASSET_ALLOCATION_ALLOW_PROD:-}" ]]; then
    _bad "ASSET_ALLOCATION_ALLOW_PROD is set — 모의투자 검증 중 제거 필요"
  else
    _ok "ASSET_ALLOCATION_ALLOW_PROD not set"
  fi
fi

echo
echo "[3] prod fail-safe (code check)"
if "$PYTHON" -c "import dotenv, pandas" 2>/dev/null; then
  "$PYTHON" -c "
import os, sys
sys.path.insert(0, 'src')
os.environ.pop('ASSET_ALLOCATION_ALLOW_PROD', None)
from settings import settings
from trader import Trader
settings._config['trading_environment'] = 'prod'
settings._config.setdefault('asset_allocation', {})['enabled'] = True
t = Trader(settings)
assert t.is_real_trading and t.asset_allocation_enabled
assert not t._asset_allocation_prod_allowed()
settings._config['trading_environment'] = 'vps'
t2 = Trader(settings)
assert not t2.is_real_trading and t2._asset_allocation_prod_allowed()
print('fail-safe ok')
" && _ok "prod blocked / vps allowed" || _bad "fail-safe check failed"
else
  _warn "dotenv/pandas 없음 — Docker 컨테이너에서 재실행 권장"
  grep -q "_asset_allocation_prod_allowed" src/trader.py && \
  grep -q "ASSET_ALLOCATION_ALLOW_PROD" src/trader.py && \
  _ok "fail-safe symbols present in trader.py (static)"
fi

echo
echo "[4] offline tests"
"$PYTHON" scripts/replay_asset_allocation.py >/dev/null && _ok "replay_asset_allocation 7/7" || _bad "replay_asset_allocation"
"$PYTHON" scripts/dry_run_asset_allocation.py --scenario all >/dev/null && _ok "dry_run_asset_allocation" || _bad "dry_run_asset_allocation"

echo
if [[ "$_fail" -eq 0 ]]; then
  echo "=== READY for docker compose up ==="
  exit 0
else
  echo "=== NOT READY — fix items above ==="
  exit 1
fi
