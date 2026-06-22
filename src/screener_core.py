#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Screener Core Module - 최적화된 스크리너 핵심 기능

주요 기능:
1. 기술적 지표 계산
2. 스크리닝 로직
3. 점수 계산
4. 시장 분석 (기본)
5. 거래 비용 계산
6. 리스크 관리 (기본)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Union
import numpy as np
import pandas as pd

# reviewer.py와 recorder.py에서 import
from reviewer import MarketRegime, MarketState
from recorder import DataRecorder

logger = logging.getLogger(__name__)

_KIS_PRICE_CLIENT: Optional[Any] = None
_INDEX_CLOSE_CACHE: Dict[str, pd.Series] = {}


def set_kis_price_client(kis: Any) -> None:
    """KIS 클라이언트를 과거 시세 조회(get_historical_prices)에 등록."""
    global _KIS_PRICE_CLIENT
    _KIS_PRICE_CLIENT = kis


def cache_index_close_series(key: str, series: pd.Series) -> None:
    """시장 지수 종가 시계열 캐시 (상대 모멘텀용)."""
    if series is not None and not series.empty:
        _INDEX_CLOSE_CACHE[str(key)] = series


def get_cached_index_close(key: str) -> Optional[pd.Series]:
    return _INDEX_CLOSE_CACHE.get(str(key))


def _clip_score(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, val)))


def _to_float_safe(val: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if val is None or (isinstance(val, str) and not str(val).strip()):
            return default
        f = float(val)
        if np.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _growth_rate_to_score(rate_pct: Optional[float], min_pct: float = -30.0, max_pct: float = 80.0) -> float:
    if rate_pct is None or pd.isna(rate_pct):
        return 0.0
    return _clip_score((float(rate_pct) - min_pct) / max(max_pct - min_pct, 1e-9))


def _normalize_kis_period_df(df: pd.DataFrame) -> pd.DataFrame:
    """KIS inquire_period_price 응답 → standardize_ohlcv 호환 형식."""
    if df is None or df.empty:
        return df
    out = df.copy()
    rename_map = {
        "stck_bsop_date": "date",
        "stck_oprc": "open",
        "stck_hgpr": "high",
        "stck_lwpr": "low",
        "stck_clpr": "close",
        "acml_vol": "volume",
    }
    out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "date" in out.columns:
        out["date"] = out["date"].astype(str)
        out = out.sort_values("date")
        out = out.set_index("date")
    return out


def get_kis_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    kis: Optional[Any] = None,
    market_div: str = "J",
    min_bars: int = 60,
    max_pages: int = 6,
) -> Optional[pd.DataFrame]:
    """KIS 기간별 시세(FHKST03010100) 페이지네이션으로 OHLCV 조회."""
    client = kis or _KIS_PRICE_CLIENT
    if client is None or not symbol or not str(symbol).strip():
        return None

    code = str(symbol).zfill(6)
    merged_rows: Dict[str, Dict[str, Any]] = {}
    cur_end = str(end_date)

    for _ in range(max(1, max_pages)):
        start = (datetime.strptime(cur_end, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")
        try:
            df = client.inquire_period_price(
                fid_cond_mrkt_div_code=market_div,
                fid_input_iscd=code,
                fid_input_date_1=start,
                fid_input_date_2=cur_end,
            )
        except Exception as e:
            logger.debug("KIS OHLCV 페이지 조회 실패(%s, end=%s): %s", code, cur_end, e)
            break
        if df is None or df.empty:
            break
        date_col = next((c for c in ["stck_bsop_date", "bsop_date", "date"] if c in df.columns), None)
        if date_col is None:
            break
        prev_n = len(merged_rows)
        for _, row in df.iterrows():
            d = str(row[date_col])
            if d:
                merged_rows[d] = row.to_dict()
        if len(merged_rows) >= min_bars:
            break
        if len(merged_rows) <= prev_n:
            break
        earliest = min(merged_rows.keys())
        try:
            next_end = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        except Exception:
            break
        if next_end >= cur_end:
            break
        cur_end = next_end

    if not merged_rows:
        return None

    out = pd.DataFrame(list(merged_rows.values()))
    out = _normalize_kis_period_df(out)
    if out is None or out.empty:
        return None
    if start_date:
        out = out[out.index >= str(start_date)]
    return out if not out.empty else None


def get_historical_prices(
    symbol: str,
    start_date: str,
    end_date: str,
    retries: int = 3,
    *,
    kis: Optional[Any] = None,
) -> Optional[pd.DataFrame]:
    """과거 시세 조회 (KIS 전용)."""
    if not symbol or not str(symbol).strip():
        logger.debug("get_historical_prices: empty symbol, skipping")
        return None

    client = kis or _KIS_PRICE_CLIENT
    if client is None:
        logger.warning("get_historical_prices: KIS client not set for %s", symbol)
        return None

    kis_data = {}
    min_bars = int(kis_data.get("ohlcv_min_bars", 60)) if isinstance(kis_data, dict) else 60
    max_pages = int(kis_data.get("ohlcv_max_pages", 6)) if isinstance(kis_data, dict) else 6

    for attempt in range(retries):
        try:
            df = get_kis_ohlcv(
                symbol,
                start_date,
                end_date,
                kis=client,
                min_bars=min(min_bars, 60),
                max_pages=max_pages,
            )
            if df is not None and not df.empty:
                logger.debug("KIS OHLCV success for %s: %d rows", symbol, len(df))
                return df
        except Exception as e:
            logger.debug("KIS OHLCV attempt %d failed for %s: %s", attempt + 1, symbol, e)
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))

    logger.warning("KIS OHLCV failed for %s (%s to %s)", symbol, start_date, end_date)
    return None


def compute_flow_score(df_flow: Optional[pd.DataFrame], cfg: Dict[str, Any]) -> Tuple[float, bool]:
    """외국인·기관 수급 점수 (0~1). (score, data_available)."""
    flow_params = cfg.get("flow_params", {}) if isinstance(cfg.get("flow_params"), dict) else {}
    fw = float(flow_params.get("foreign_weight", 0.5))
    iw = float(flow_params.get("institution_weight", 0.5))

    if df_flow is None or df_flow.empty:
        return 0.0, False

    needed = {"기관합계", "외국인합계"}
    if not needed.issubset(set(df_flow.columns)):
        return 0.0, False

    inst = pd.to_numeric(df_flow["기관합계"], errors="coerce").fillna(0)
    frgn = pd.to_numeric(df_flow["외국인합계"], errors="coerce").fillna(0)
    if inst.abs().sum() == 0 and frgn.abs().sum() == 0:
        return 0.0, False

    inst_sum = float(inst.sum())
    frgn_sum = float(frgn.sum())
    inst_norm = _clip_score(inst_sum / max(abs(inst_sum), 1e9) * 0.5 + 0.5) if inst_sum != 0 else 0.5
    frgn_norm = _clip_score(frgn_sum / max(abs(frgn_sum), 1e9) * 0.5 + 0.5) if frgn_sum != 0 else 0.5
    if inst_sum > 0:
        inst_norm = _clip_score(0.5 + min(inst_sum / 5e10, 0.5))
    elif inst_sum < 0:
        inst_norm = _clip_score(0.5 - min(abs(inst_sum) / 5e10, 0.5))
    if frgn_sum > 0:
        frgn_norm = _clip_score(0.5 + min(frgn_sum / 5e10, 0.5))
    elif frgn_sum < 0:
        frgn_norm = _clip_score(0.5 - min(abs(frgn_sum) / 5e10, 0.5))

    dual_days = int(((inst > 0) & (frgn > 0)).sum())
    dual_bonus = min(0.1, dual_days / max(len(df_flow), 1) * 0.1)
    score = _clip_score(fw * frgn_norm + iw * inst_norm + dual_bonus)
    return score, True


def _period_return(close: pd.Series, days: int) -> Optional[float]:
    if close is None or len(close) < days + 1:
        return None
    base = float(close.iloc[-days - 1])
    if base <= 0:
        return None
    return (float(close.iloc[-1]) - base) / base


def compute_momentum_score(
    close: pd.Series,
    index_close: Optional[pd.Series],
    cfg: Dict[str, Any],
) -> float:
    """중기 모멘텀 점수 (0~1)."""
    mom_params = cfg.get("momentum_params", {}) if isinstance(cfg.get("momentum_params"), dict) else {}
    periods = mom_params.get("periods_days", [20, 60, 120])
    weights = mom_params.get("period_weights", [0.45, 0.35, 0.20])
    if len(weights) != len(periods):
        weights = [1.0 / len(periods)] * len(periods)

    parts: List[float] = []
    wsum = 0.0
    for period, w in zip(periods, weights):
        ret = _period_return(close, int(period))
        if ret is None:
            continue
        parts.append(float(w) * _clip_score(ret * 5 + 0.5))
        wsum += float(w)

    if wsum <= 0:
        return 0.0

    score = sum(parts) / wsum

    if mom_params.get("relative_to_index", False) and index_close is not None and len(index_close) >= 61:
        stock_60 = _period_return(close, 60)
        idx_60 = _period_return(index_close, 60)
        if stock_60 is not None and idx_60 is not None:
            rel = stock_60 - idx_60
            score = _clip_score(0.85 * score + 0.15 * _clip_score(rel * 5 + 0.5))

    mid_long = _period_return(close, 120)
    short = _period_return(close, 20)
    if mid_long is not None and short is not None:
        accel = mid_long - short
        score = _clip_score(0.9 * score + 0.1 * _clip_score(accel * 5 + 0.5))

    return _clip_score(score)


def compute_growth_score(fin_ratio: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> float:
    """실적 성장률 점수 (KIS financial-ratio)."""
    if not fin_ratio:
        return 0.0
    gp = cfg.get("growth_params", {}) if isinstance(cfg.get("growth_params"), dict) else {}
    rw = float(gp.get("revenue_yoy_weight", 0.40))
    ow = float(gp.get("op_profit_yoy_weight", 0.35))
    ew = float(gp.get("eps_yoy_weight", 0.25))
    grs = _to_float_safe(fin_ratio.get("grs"))
    bsop = _to_float_safe(fin_ratio.get("bsop_prfi_inrt"))
    ntin = _to_float_safe(fin_ratio.get("ntin_inrt"))
    return _clip_score(
        rw * _growth_rate_to_score(grs, -20.0, 50.0)
        + ow * _growth_rate_to_score(bsop, -30.0, 80.0)
        + ew * _growth_rate_to_score(ntin, -30.0, 80.0)
    )


def compute_fin_score_extended(
    per_val: Optional[float],
    pbr_val: Optional[float],
    roe_val: Optional[float],
    cfg: Dict[str, Any],
    marcap: float = 0.0,
) -> float:
    """PER/PBR/ROE 기반 품질·밸류에이션 점수."""
    fp = cfg.get("fin_params", {}) if isinstance(cfg.get("fin_params"), dict) else {}
    per_w = float(fp.get("per_weight", 0.40))
    pbr_w = float(fp.get("pbr_weight", 0.30))
    roe_w = float(fp.get("roe_weight", 0.30))

    per = _to_float_safe(per_val, 20.0)
    pbr = _to_float_safe(pbr_val, 1.5)
    roe = _to_float_safe(roe_val)

    if per is None:
        per = 20.0
    if pbr is None:
        pbr = 1.5

    if per < 0:
        per_term = 0.1
    else:
        per_term = max(0.0, min(1.0, (50 - per) / 50))
    if pbr < 0:
        pbr_term = 0.1
    else:
        pbr_term = max(0.0, min(1.0, (5 - pbr) / 5))
    if roe is None or roe <= 0:
        roe_term = 0.2
    else:
        roe_term = max(0.0, min(1.0, roe / 25.0))

    return _clip_score(per_w * per_term + pbr_w * pbr_term + roe_w * roe_term)


def compute_breakout_score(
    df_price: pd.DataFrame,
    price_info: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> float:
    """신고가 돌파·거래량 확대 점수."""
    bp = cfg.get("breakout_params", {}) if isinstance(cfg.get("breakout_params"), dict) else {}
    lookback = int(bp.get("lookback_high_days", 252))
    vol_ratio_min = float(bp.get("volume_ratio_min", 1.5))
    buffer_pct = float(bp.get("breakout_buffer_pct", 0.005))

    if df_price is None or df_price.empty:
        return 0.0

    close = pd.to_numeric(df_price["Close"], errors="coerce")
    high = pd.to_numeric(df_price["High"], errors="coerce")
    volume = pd.to_numeric(df_price["Volume"], errors="coerce").fillna(0)
    if close.empty:
        return 0.0

    last_close = float(close.iloc[-1])
    window = min(lookback, len(high))
    high_52 = float(high.tail(window).max())
    high_20 = float(high.tail(min(20, len(high))).max())

    breakout_52 = 1.0 if last_close >= high_52 * (1.0 + buffer_pct) else 0.0
    breakout_20 = 1.0 if last_close >= high_20 * (1.0 + buffer_pct) else 0.0

    vol_ma20 = float(volume.tail(min(20, len(volume))).mean()) if len(volume) else 0.0
    last_vol = float(volume.iloc[-1]) if len(volume) else 0.0
    vol_score = 1.0 if vol_ma20 > 0 and last_vol >= vol_ma20 * vol_ratio_min else 0.0

    if price_info:
        w52 = _to_float_safe(price_info.get("w52_hgpr") or price_info.get("W52_HGPR"))
        if w52 and w52 > 0 and last_close >= w52 * (1.0 + buffer_pct):
            breakout_52 = max(breakout_52, 1.0)

    return _clip_score(0.50 * breakout_52 + 0.30 * vol_score + 0.20 * breakout_20)


def compute_total_score_8axis(
    flow: float,
    momentum: float,
    tech: float,
    growth: float,
    fin: float,
    breakout: float,
    mkt: float,
    sector: float,
    cfg: Dict[str, Any],
) -> float:
    """8축 가중 합산."""
    w_flow = float(cfg.get("flow_weight", 0.20))
    w_mom = float(cfg.get("momentum_weight", 0.20))
    w_tech = float(cfg.get("tech_weight", 0.15))
    w_growth = float(cfg.get("growth_weight", 0.15))
    w_fin = float(cfg.get("fin_weight", 0.15))
    w_break = float(cfg.get("breakout_weight", 0.05))
    w_mkt = float(cfg.get("mkt_weight", 0.05))
    w_sector = float(cfg.get("sector_weight", 0.05))
    total = (
        flow * w_flow
        + momentum * w_mom
        + tech * w_tech
        + growth * w_growth
        + fin * w_fin
        + breakout * w_break
        + mkt * w_mkt
        + sector * w_sector
    )
    return _clip_score(total)

def _compute_levels(ticker: str, current_price: float, date_str: str, risk_params: Dict[str, Any], strategy_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    손절/목표가 계산 (통합 함수)
    우선순위: strategy_params → risk_params → 기본값
    
    Args:
        ticker: 종목 코드
        current_price: 현재 가격
        date_str: 날짜 문자열 (YYYYMMDD)
        risk_params: 위험 관리 파라미터
        strategy_params: 전략 파라미터 (선택적)
    
    Returns:
        Dict with 손절가, 목표가, source
    """
    try:
        # Phase 1: 설정값 우선순위 적용 (strategy_params → risk_params → 기본값)
        if strategy_params:
            stop_loss_pct = (
                strategy_params.get("stop_loss_pct") or 
                risk_params.get("stop_pct") or 
                risk_params.get("auto_sell", {}).get("stop_loss_pct") or 
                0.03
            )
            target_pct = (
                strategy_params.get("take_profit_pct") or 
                risk_params.get("auto_sell", {}).get("target_pct") or 
                0.08
            )
            atr_k_stop = strategy_params.get("atr_k_stop", 2.0)
            atr_k_profit = strategy_params.get("atr_k_profit", 4.0)
        else:
            stop_loss_pct = (
                risk_params.get("stop_pct") or 
                risk_params.get("auto_sell", {}).get("stop_loss_pct") or 
                0.03
            )
            target_pct = (
                risk_params.get("auto_sell", {}).get("target_pct") or 
                0.08
            )
            atr_k_stop = risk_params.get("atr_k_stop", 2.0)
            atr_k_profit = risk_params.get("atr_k_profit", 4.0)  # 수정: risk_params에서도 읽기
        
        # 현재 가격을 float로 변환
        price = float(current_price)
        
        # 기본 손절/목표가 계산
        stop_loss = price * (1 - stop_loss_pct)
        target = price * (1 + target_pct)
        
        # 과거 데이터를 통한 고급 계산 시도
        try:
            end_date = date_str
            start_dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=60)
            start_date = start_dt.strftime("%Y%m%d")
            
            df = get_historical_prices(ticker, start_date, end_date)
            if df is not None and len(df) > 20:
                # ATR 계산
                atr = calculate_atr(df, period=14)
                
                # 스윙 고저점 계산 (한국어 우선)
                close_col = None
                high_col = None
                low_col = None
                
                if '종가' in df.columns:
                    close_col = '종가'
                elif 'close' in df.columns:
                    close_col = 'close'
                elif 'Close' in df.columns:
                    close_col = 'Close'
                    
                if '고가' in df.columns:
                    high_col = '고가'
                elif 'high' in df.columns:
                    high_col = 'high'
                elif 'High' in df.columns:
                    high_col = 'High'
                    
                if '저가' in df.columns:
                    low_col = '저가'
                elif 'low' in df.columns:
                    low_col = 'low'
                elif 'Low' in df.columns:
                    low_col = 'Low'
                
                if not all([close_col, high_col, low_col]):
                    logger.debug(f"필요한 컬럼을 찾을 수 없음: {df.columns.tolist()}")
                    raise ValueError("Required columns not found")
                
                recent_high = df[high_col].tail(20).max()
                recent_low = df[low_col].tail(20).min()
                
                if atr > 0 and recent_high > 0 and recent_low > 0:
                    # Phase 1: ATR 기반 계산 (하드코딩 제거, config 값 사용)
                    atr_stop_loss = price - (atr * atr_k_stop)
                    atr_target = price + (atr * atr_k_profit)
                    
                    # 스윙 기반 계산
                    swing_stop_loss = recent_low * 0.95
                    swing_target = recent_high * 1.15
                    
                    # 가장 보수적인 값 선택
                    stop_loss = max(atr_stop_loss, swing_stop_loss, stop_loss)
                    target = min(atr_target, swing_target, target)
                    
                    return {
                        "손절가": stop_loss,
                        "목표가": target,
                        "source": "atr_swing"
                    }
                    
        except Exception as e:
            logger.debug(f"고급 손절/목표가 계산 실패 ({ticker}): {e}")
        
        return {
            "손절가": stop_loss,
            "목표가": target,
            "source": "percent_backup"
        }
        
    except Exception as e:
        logger.error(f"손절/목표가 계산 실패 ({ticker}): {e}")
        # Phase 1: 최종 백업 (하드코딩 제거, config 값 사용)
        try:
            price = float(current_price)
            return {
                "손절가": price * (1 - stop_loss_pct),
                "목표가": price * (1 + target_pct),
                "source": "fallback"
            }
        except:
            return {
                "손절가": 0,
                "목표가": 0,
                "source": "error"
            }

class MarketAnalyzer:
    """시장 분석기 (기본 기능)"""
    
    def __init__(
        self,
        settings: Dict[str, Any],
        *,
        kis: Optional[Any] = None,
        market: str = "KOSPI",
        date_str: Optional[str] = None,
    ):
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.kis = kis
        self.market = (market or "KOSPI").upper()
        self.date_str = date_str

    def _paginated_close(self, fetch_fn, end_dt: str, *, min_bars: int = 260, max_pages: int = 6) -> Optional[pd.Series]:
        """fetch_fn(start, end)->df 를 종료일을 과거로 밀며 여러 번 호출해 종가를 누적,
        '오름차순(과거→현재)' Series로 반환한다.

        KIS 기간별시세는 1회 응답이 ~100봉으로 제한되므로, MA200 산출을 위해
        end_dt를 과거로 이동시키며 min_bars 이상을 모은다(YYYYMMDD 문자열 인덱스).
        """
        merged: Dict[str, float] = {}
        cur_end = str(end_dt)
        for _ in range(max(1, max_pages)):
            start = (datetime.strptime(cur_end, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")
            try:
                df = fetch_fn(start, cur_end)
            except Exception as e:
                self.logger.debug("지수 종가 페이지 조회 실패(end=%s): %s", cur_end, e)
                break
            if df is None or getattr(df, "empty", True):
                break
            date_col = next((c for c in ["stck_bsop_date", "bsop_date", "date", "Date"] if c in df.columns), None)
            close_col = next((c for c in ["bstp_nmix_prpr", "stck_clpr", "clspr", "close", "Close", "종가"] if c in df.columns), None)
            if close_col is None:
                break
            if date_col is None:
                # 날짜가 없으면 더 거슬러 올라갈 수 없음 → 단일 페이지 종가만 사용
                vals = pd.to_numeric(df[close_col], errors="coerce").dropna()
                return vals.reset_index(drop=True) if len(vals) else None
            prev_n = len(merged)
            for d, c in zip(df[date_col].astype(str), pd.to_numeric(df[close_col], errors="coerce")):
                if d and pd.notna(c):
                    merged[d] = float(c)
            if len(merged) >= min_bars:
                break
            if len(merged) <= prev_n:
                break  # 새 데이터 없음(중복만 수신)
            earliest = min(merged.keys())
            try:
                next_end = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            except Exception:
                break
            if next_end >= cur_end:
                break
            cur_end = next_end
        if not merged:
            return None
        return pd.Series(merged).sort_index()

    def analyze_market_state(self) -> MarketState:
        """시장 상태 분석"""
        try:
            current_time = datetime.now()

            # KIS 업종지수 일자별(일봉) 기반 결정적 시장판단
            if self.kis is not None:
                end_dt = self.date_str or current_time.strftime("%Y%m%d")
                idx_code = "0001" if self.market == "KOSPI" else "1001"

                # 업종지수 일봉을 페이지네이션으로 200봉+ 누적(MA200 산출 목적).
                close = self._paginated_close(
                    lambda s, e: self.kis.inquire_industry_period_price(
                        fid_input_iscd=idx_code,
                        fid_input_date_1=s,
                        fid_input_date_2=e,
                        fid_period_div_code="D",
                    ),
                    end_dt,
                    min_bars=260,
                )
                # 업종지수가 부실하면 지수추종 ETF(KODEX200/KODEX코스닥150)로 폴백.
                if close is None or len(close) < 60:
                    proxy = "069500" if self.market == "KOSPI" else "229200"
                    close = self._paginated_close(
                        lambda s, e: self.kis.inquire_period_price(
                            fid_cond_mrkt_div_code="J",
                            fid_input_iscd=proxy,
                            fid_input_date_1=s,
                            fid_input_date_2=e,
                            fid_period_div_code="D",
                            fid_org_adj_prc="0",
                        ),
                        end_dt,
                        min_bars=260,
                    )
                if close is None or len(close) < 60:
                    self.logger.warning(
                        "KIS index history insufficient (industry_code=%s, end=%s) → sideways fallback",
                        idx_code,
                        end_dt,
                    )
                    return MarketState(
                        regime=MarketRegime.SIDEWAYS,
                        volatility_level="medium",
                        trend_direction="sideways",
                        confidence=0.5,
                        timestamp=current_time,
                    )

                ma20 = close.rolling(20).mean()
                ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else close.rolling(20).mean().iloc[-1]
                ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")

                rsi_val = calculate_rsi(close)
                rsi = (
                    float(rsi_val.iloc[-1])
                    if isinstance(rsi_val, pd.Series) and len(rsi_val)
                    else float(rsi_val) if rsi_val is not None else 50.0
                )

                # 변동성(연율화) + 레벨
                returns = close.pct_change().dropna()
                vol_ann = float(returns.std() * (252 ** 0.5)) if len(returns) else 0.0
                if vol_ann < 0.15:
                    volatility_level = "low"
                elif vol_ann < 0.25:
                    volatility_level = "medium"
                else:
                    volatility_level = "high"

                # 추세 방향: MA20 5영업일 변화
                trend_direction = "sideways"
                try:
                    ma20_last = float(ma20.iloc[-1])
                    ma20_prev = float(ma20.iloc[-6])
                    delta = (ma20_last - ma20_prev) / ma20_prev if ma20_prev else 0.0
                    if delta > 0.003:
                        trend_direction = "up"
                    elif delta < -0.003:
                        trend_direction = "down"
                except Exception:
                    pass

                # 레짐 결정 (ma200이 없으면 중립 처리)
                last_close = float(close.iloc[-1])
                ma50_gt_ma200 = (ma50 > ma200) if not pd.isna(ma200) else None
                is_bull = (last_close > ma50) and ((ma50_gt_ma200 is None) or ma50_gt_ma200) and (rsi >= 55)
                is_bear = (last_close < ma50) and ((ma50_gt_ma200 is None) or (not ma50_gt_ma200)) and (rsi <= 45)
                if vol_ann >= 0.35:
                    regime = MarketRegime.VOLATILE
                elif is_bull:
                    regime = MarketRegime.BULL
                elif is_bear:
                    regime = MarketRegime.BEAR
                else:
                    regime = MarketRegime.SIDEWAYS

                # 신뢰도: (MA/RSI 일치 정도 + 변동성 페널티)
                rsi_term = max(0.0, 1 - abs(rsi - 50) / 50)
                ma_term = 0.5 if (ma50_gt_ma200 is None) else (1.0 if ma50_gt_ma200 else 0.0)
                score = ((1 if last_close > ma50 else 0) + ma_term + rsi_term) / 3.0
                confidence = 0.50 + min(0.40, abs(score - 0.5) * 0.8)
                if regime == MarketRegime.VOLATILE:
                    confidence = max(0.50, confidence - 0.10)

                return MarketState(
                    regime=regime,
                    volatility_level=volatility_level,
                    trend_direction=trend_direction,
                    confidence=float(confidence),
                    timestamp=current_time,
                )

            # KIS가 없으면 보수적으로 sideways로 폴백(결정적)
            return MarketState(
                regime=MarketRegime.SIDEWAYS,
                volatility_level="medium",
                trend_direction="sideways",
                confidence=0.5,
                timestamp=current_time,
            )
            
        except Exception as e:
            self.logger.error(f"시장 상태 분석 실패: {e}")
            return MarketState(
                regime=MarketRegime.SIDEWAYS,
                volatility_level="medium",
                trend_direction="sideways",
                confidence=0.5,
                timestamp=datetime.now()
            )
    
    def calculate_dynamic_threshold(self, base_threshold: float, market_state: MarketState) -> float:
        """동적 임계값 계산"""
        try:
            regime_multiplier = {
                MarketRegime.BULL: 0.8,
                MarketRegime.BEAR: 1.5,
                MarketRegime.SIDEWAYS: 1.0,
                MarketRegime.VOLATILE: 2.0
            }
            
            volatility_multiplier = {
                "low": 0.8,
                "medium": 1.0,
                "high": 1.5
            }
            
            regime_factor = regime_multiplier.get(market_state.regime, 1.0)
            volatility_factor = volatility_multiplier.get(market_state.volatility_level, 1.0)
            
            adjusted_threshold = base_threshold * regime_factor * volatility_factor
            return max(0.01, min(0.5, adjusted_threshold))
            
        except Exception as e:
            self.logger.error(f"동적 임계값 계산 실패: {e}")
            return base_threshold
    
    def get_market_summary(self, market_state: MarketState) -> str:
        """시장 요약"""
        return f"시장상황: {market_state.regime.value} | 변동성: {market_state.volatility_level} | 추세: {market_state.trend_direction} | 신뢰도: {market_state.confidence:.2f}"

def calculate_atr(df_price: pd.DataFrame, period: int = 14) -> float:
    """ATR (Average True Range) 계산"""
    try:
        if len(df_price) < period + 1:
            return 0.0
        
        # 컬럼명 대소문자 및 한국어 처리
        high_col = None
        low_col = None
        close_col = None
        
        # 한국어 컬럼명 우선 확인
        if '고가' in df_price.columns:
            high_col = '고가'
        elif 'high' in df_price.columns:
            high_col = 'high'
        elif 'High' in df_price.columns:
            high_col = 'High'
            
        if '저가' in df_price.columns:
            low_col = '저가'
        elif 'low' in df_price.columns:
            low_col = 'low'
        elif 'Low' in df_price.columns:
            low_col = 'Low'
            
        if '종가' in df_price.columns:
            close_col = '종가'
        elif 'close' in df_price.columns:
            close_col = 'close'
        elif 'Close' in df_price.columns:
            close_col = 'Close'
        
        if not all([high_col, low_col, close_col]):
            logger.error(f"필요한 컬럼을 찾을 수 없음: {df_price.columns.tolist()}")
            return 0.0
        
        high = pd.to_numeric(df_price[high_col], errors="coerce")
        low = pd.to_numeric(df_price[low_col], errors="coerce")
        close = pd.to_numeric(df_price[close_col], errors="coerce")
        
        # True Range 계산
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # ATR 계산 (단순 이동평균)
        atr = true_range.rolling(window=period).mean().iloc[-1]
        
        return float(atr) if not pd.isna(atr) else 0.0
        
    except Exception as e:
        logger.error(f"ATR 계산 실패: {e}")
        return 0.0

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """RSI 계산"""
    try:
        if hasattr(prices, "iloc"):
            prices = pd.to_numeric(prices, errors="coerce").dropna().tolist()
        else:
            prices = [float(p) for p in prices if p is not None and pd.notna(p)]

        if len(prices) < period + 1:
            return 50.0
        
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        if len(gains) < period:
            return 50.0
            
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        # avg_loss가 0이면 RSI 100.0 (14일 연속 상승)
        # 이는 매우 드문 상황이므로 경고 로그 추가
        if avg_loss == 0:
            if avg_gain > 0:
                logger.warning(
                    f"RSI 계산: avg_loss=0 (14일 연속 상승) → RSI=100.0. "
                    f"이는 비정상적으로 높은 값입니다. 데이터를 확인하세요."
                )
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # RSI 값 검증 (99.0 이상은 극단값)
        if rsi >= 99.0:
            logger.warning(
                f"RSI 극단값 감지: {rsi:.2f} (≥99.0). "
                f"데이터 문제 또는 실제로 매우 강한 상승 추세일 수 있습니다."
            )
        
        return rsi
        
    except Exception as e:
        logger.error(f"RSI 계산 실패: {e}")
        return 50.0

def calculate_macd(prices: List[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Dict[str, float]:
    """MACD 계산"""
    try:
        if len(prices) < slow_period:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        
        prices_array = np.array(prices)
        
        # EMA 계산
        def calculate_ema(data, period):
            alpha = 2.0 / (period + 1)
            ema = np.zeros_like(data)
            ema[0] = data[0]
            for i in range(1, len(data)):
                ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
            return ema
        
        fast_ema = calculate_ema(prices_array, fast_period)
        slow_ema = calculate_ema(prices_array, slow_period)
        
        macd_line = fast_ema - slow_ema
        signal_line = calculate_ema(macd_line, signal_period)
        histogram = macd_line - signal_line
        
        return {
            "macd": macd_line[-1],
            "signal": signal_line[-1],
            "histogram": histogram[-1]
        }
        
    except Exception as e:
        logger.error(f"MACD 계산 실패: {e}")
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

def calculate_bollinger_bands(prices: List[float], period: int = 20, std_dev: float = 2.0) -> Dict[str, float]:
    """볼린저 밴드 계산"""
    try:
        if len(prices) < period:
            return {"upper": 0.0, "middle": 0.0, "lower": 0.0}
        
        recent_prices = prices[-period:]
        middle = np.mean(recent_prices)
        std = np.std(recent_prices)
        
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        return {
            "upper": upper,
            "middle": middle,
            "lower": lower
        }
        
    except Exception as e:
        logger.error(f"볼린저 밴드 계산 실패: {e}")
        return {"upper": 0.0, "middle": 0.0, "lower": 0.0}

def calculate_technical_score(ticker: str, prices: List[float], volumes: List[float]) -> float:
    """기술적 점수 계산"""
    try:
        if len(prices) < 20:
            return 0.0
        
        score = 0.0
        
        # RSI 점수 (30-70 범위에서 선호)
        rsi = calculate_rsi(prices)
        if 30 <= rsi <= 70:
            score += 0.2
        elif rsi < 30:  # 과매도
            score += 0.3
        elif rsi > 70:  # 과매수
            score += 0.1
        
        # MACD 점수
        macd_data = calculate_macd(prices)
        if macd_data["macd"] > macd_data["signal"]:
            score += 0.2
        
        # 볼린저 밴드 점수
        bb_data = calculate_bollinger_bands(prices)
        current_price = prices[-1]
        if bb_data["lower"] < current_price < bb_data["upper"]:
            score += 0.2
        elif current_price < bb_data["lower"]:  # 하단 터치
            score += 0.3
        
        # 가격 모멘텀 점수
        if len(prices) >= 5:
            short_ma = np.mean(prices[-5:])
            long_ma = np.mean(prices[-20:])
            if short_ma > long_ma:
                score += 0.2
        
        # 거래량 점수
        if len(volumes) >= 20:
            recent_volume = np.mean(volumes[-5:])
            avg_volume = np.mean(volumes[-20:])
            if recent_volume > avg_volume * 1.2:  # 거래량 증가
                score += 0.1
        
        return min(1.0, max(0.0, score))
        
    except Exception as e:
        logger.error(f"기술적 점수 계산 실패: {e}")
        return 0.0

def calculate_market_adjusted_score(base_score: float, market_state: MarketState) -> float:
    """시장 상황에 따른 점수 조정"""
    try:
        # 시장 상황에 따른 가중치
        regime_weights = {
            MarketRegime.BULL: 1.1,
            MarketRegime.BEAR: 0.9,
            MarketRegime.SIDEWAYS: 1.0,
            MarketRegime.VOLATILE: 0.95
        }
        
        # 변동성에 따른 가중치
        volatility_weights = {
            "low": 1.05,
            "medium": 1.0,
            "high": 0.95
        }
        
        regime_weight = regime_weights.get(market_state.regime, 1.0)
        volatility_weight = volatility_weights.get(market_state.volatility_level, 1.0)
        
        adjusted_score = base_score * regime_weight * volatility_weight
        return min(1.0, max(0.0, adjusted_score))
        
    except Exception as e:
        logger.error(f"시장 조정 점수 계산 실패: {e}")
        return base_score

def get_market_aware_screening_params(market_state: MarketState, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """시장 상황별 스크리닝 파라미터 (기본값에 base 오버레이 가능)"""
    try:
        base_params = {
            "min_volume": 100000,
            "min_price": 1000,
            "max_price": 1000000,
            "min_market_cap": 100000000000,  # 1000억
            "min_rsi": 20,
            "max_rsi": 80
        }
        if isinstance(base, dict) and base:
            # caller가 가진 screener_params를 기본값 위에 얹는다.
            base_params.update(base)
        
        # 시장 상황별 조정
        if market_state.regime == MarketRegime.BULL:
            base_params["min_rsi"] = 30
            base_params["max_rsi"] = 85
        elif market_state.regime == MarketRegime.BEAR:
            base_params["min_rsi"] = 15
            base_params["max_rsi"] = 70
        elif market_state.regime == MarketRegime.VOLATILE:
            base_params["min_rsi"] = 25
            base_params["max_rsi"] = 75
        
        return base_params
        
    except Exception as e:
        logger.error(f"시장 상황별 스크리닝 파라미터 생성 실패: {e}")
        return {
            "min_volume": 100000,
            "min_price": 1000,
            "max_price": 1000000,
            "min_market_cap": 100000000000,
            "min_rsi": 20,
            "max_rsi": 80
        }

# 거래 비용 계산 (기본)
def calculate_transaction_costs(sell_amount: int, buy_amount: int, settings: Dict[str, Any]) -> Dict[str, Any]:
    """거래 비용 계산"""
    try:
        commission_rate = settings.get("trading_params", {}).get("commission_rate", 0.00015)
        securities_tax_rate = settings.get("trading_params", {}).get("securities_tax_rate", 0.0015)
        agricultural_tax_rate = settings.get("trading_params", {}).get("agricultural_tax_rate", 0.0008)
        slippage_rate = settings.get("trading_params", {}).get("slippage_rate", 0.001)
        
        # 수수료 계산
        commission_sell = int(sell_amount * commission_rate)
        commission_buy = int(buy_amount * commission_rate)
        
        # 세금 계산
        tax_sell = int(sell_amount * securities_tax_rate)
        tax_buy = int(buy_amount * agricultural_tax_rate)
        
        # 슬리피지 계산
        slippage_sell = int(sell_amount * slippage_rate)
        slippage_buy = int(buy_amount * slippage_rate)
        
        total_cost = commission_sell + commission_buy + tax_sell + tax_buy + slippage_sell + slippage_buy
        
        return {
            "commission_sell": commission_sell,
            "commission_buy": commission_buy,
            "tax_sell": tax_sell,
            "tax_buy": tax_buy,
            "slippage_sell": slippage_sell,
            "slippage_buy": slippage_buy,
            "total_cost": total_cost
        }
        
    except Exception as e:
        logger.error(f"거래 비용 계산 실패: {e}")
        return {
            "commission_sell": 0,
            "commission_buy": 0,
            "tax_sell": 0,
            "tax_buy": 0,
            "slippage_sell": 0,
            "slippage_buy": 0,
            "total_cost": 0
        }

def calculate_net_profit_rotation(
    sell_ticker: str,
    buy_ticker: str,
    sell_amount: int,
    buy_amount: int,
    expected_gain: float,
    settings: Dict[str, Any]
) -> Dict[str, Any]:
    """순수익 기반 회전 매매 판단"""
    try:
        # 거래 비용 계산
        costs = calculate_transaction_costs(sell_amount, buy_amount, settings)
        
        # 예상 수익 계산
        expected_profit = int(expected_gain)
        net_profit = expected_profit - costs["total_cost"]
        
        # 최소 수익률 및 비용 효과성 확인
        min_profit_rate = settings.get("rotation", {}).get("min_profit_rate", 0.02)
        min_cost_effectiveness = settings.get("rotation", {}).get("min_cost_effectiveness", 2.0)
        
        profit_rate = net_profit / sell_amount if sell_amount > 0 else 0
        cost_effectiveness = expected_profit / costs["total_cost"] if costs["total_cost"] > 0 else 0
        
        should_rotate = (
            net_profit > 0 and
            profit_rate >= min_profit_rate and
            cost_effectiveness >= min_cost_effectiveness
        )
        
        return {
            "should_rotate": should_rotate,
            "net_profit": net_profit,
            "expected_profit": expected_profit,
            "total_costs": costs["total_cost"],
            "profit_rate": profit_rate,
            "cost_effectiveness": cost_effectiveness
        }
        
    except Exception as e:
        logger.error(f"순수익 회전 매매 판단 실패: {e}")
        return {
            "should_rotate": False,
            "net_profit": 0,
            "expected_profit": 0,
            "total_costs": 0,
            "profit_rate": 0,
            "cost_effectiveness": 0
        }

# ── RSI 개선 전략용 추가 지표 계산 함수 ────────────────────────────────
def calculate_ma20(prices: pd.Series, period: int = 20) -> float:
    """
    20일 이동평균 계산
    
    Args:
        prices: 종가 시리즈
        period: 이동평균 기간 (기본 20)
    
    Returns:
        MA20 값 (계산 실패 시 0.0)
    """
    try:
        if len(prices) < period:
            return 0.0
        
        ma20 = prices.rolling(window=period, min_periods=period).mean().iloc[-1]
        return float(ma20) if not pd.isna(ma20) else 0.0
    except Exception as e:
        logger.error(f"MA20 계산 실패: {e}")
        return 0.0

def calculate_ma20_slope(prices: pd.Series, period: int = 20, lookback_days: int = 5) -> float:
    """
    MA20 기울기 계산 (현재 MA20 - N일 전 MA20)
    
    Args:
        prices: 종가 시리즈
        period: 이동평균 기간 (기본 20)
        lookback_days: 기울기 계산을 위한 이전 일수 (기본 5)
    
    Returns:
        MA20 기울기 (양수=상승, 음수=하락, 계산 실패 시 0.0)
    """
    try:
        if len(prices) < period + lookback_days:
            return 0.0
        
        ma20_series = prices.rolling(window=period, min_periods=period).mean()
        if len(ma20_series) < lookback_days + 1:
            return 0.0
        
        current_ma20 = ma20_series.iloc[-1]
        prev_ma20 = ma20_series.iloc[-(lookback_days + 1)]
        
        if pd.isna(current_ma20) or pd.isna(prev_ma20):
            return 0.0
        
        slope = float(current_ma20 - prev_ma20) / lookback_days
        return slope
    except Exception as e:
        logger.error(f"MA20 기울기 계산 실패: {e}")
        return 0.0

def calculate_volume_ratio(df: pd.DataFrame, short_period: int = 3, long_period: int = 10) -> float:
    """
    거래량 비율 계산 (최근 N일 평균 / 최근 M일 평균)
    
    Args:
        df: OHLCV 데이터프레임
        short_period: 단기 기간 (기본 3일)
        long_period: 장기 기간 (기본 10일)
    
    Returns:
        거래량 비율 (단기/장기, 계산 실패 시 1.0)
    """
    try:
        # 거래량 컬럼 찾기
        volume_col = None
        if '거래량' in df.columns:
            volume_col = '거래량'
        elif 'Volume' in df.columns:
            volume_col = 'Volume'
        elif 'volume' in df.columns:
            volume_col = 'volume'
        else:
            logger.warning(f"거래량 컬럼을 찾을 수 없음: {df.columns.tolist()}")
            return 1.0
        
        if len(df) < long_period:
            return 1.0
        
        volumes = df[volume_col].dropna()
        if len(volumes) < long_period:
            return 1.0
        
        short_avg = volumes.tail(short_period).mean()
        long_avg = volumes.tail(long_period).mean()
        
        if long_avg == 0:
            return 1.0
        
        ratio = float(short_avg / long_avg)
        return ratio
    except Exception as e:
        logger.error(f"거래량 비율 계산 실패: {e}")
        return 1.0

def detect_bearish_divergence(df: pd.DataFrame, lookback_period: int = 10) -> bool:
    """
    약세 다이버전스 감지 (가격은 상승고점, RSI는 하락고점)
    
    Args:
        df: OHLCV 데이터프레임 (Close 컬럼 필요)
        lookback_period: 확인 기간 (기본 10일)
    
    Returns:
        약세 다이버전스 감지 여부
    """
    try:
        if len(df) < lookback_period + 5:  # RSI 계산을 위한 최소 데이터
            return False
        
        # 종가 컬럼 찾기
        close_col = None
        if '종가' in df.columns:
            close_col = '종가'
        elif 'Close' in df.columns:
            close_col = 'Close'
        elif 'close' in df.columns:
            close_col = 'close'
        else:
            return False
        
        prices = df[close_col].dropna()
        if len(prices) < lookback_period + 5:
            return False
        
        # RSI 계산
        rsi_values = []
        for i in range(14, len(prices)):
            window = prices.iloc[i-14:i+1]
            rsi = calculate_rsi(window)
            rsi_values.append(rsi)
        
        if len(rsi_values) < lookback_period:
            return False
        
        # 최근 lookback_period 동안의 가격 고점과 RSI 고점 찾기
        recent_prices = prices.tail(lookback_period)
        recent_rsi = pd.Series(rsi_values).tail(lookback_period)
        
        # 가격 고점 (최근 5일 중)
        price_window = recent_prices.tail(5)
        price_high_idx = price_window.idxmax()
        price_high = price_window.max()
        
        # 이전 고점 (나머지 기간 중)
        price_prev_window = recent_prices.head(-5) if len(recent_prices) > 5 else recent_prices
        if len(price_prev_window) > 0:
            price_prev_high = price_prev_window.max()
            
            # RSI 고점
            rsi_window = recent_rsi.tail(5)
            rsi_high = rsi_window.max()
            
            rsi_prev_window = recent_rsi.head(-5) if len(recent_rsi) > 5 else recent_rsi
            if len(rsi_prev_window) > 0:
                rsi_prev_high = rsi_prev_window.max()
                
                # 약세 다이버전스: 가격은 상승고점, RSI는 하락고점
                price_higher = price_high > price_prev_high
                rsi_lower = rsi_high < rsi_prev_high
                
                if price_higher and rsi_lower:
                    logger.debug(f"약세 다이버전스 감지: 가격 고점 {price_prev_high:.0f} → {price_high:.0f}, RSI 고점 {rsi_prev_high:.1f} → {rsi_high:.1f}")
                    return True
        
        return False
    except Exception as e:
        logger.error(f"약세 다이버전스 감지 실패: {e}")
        return False
