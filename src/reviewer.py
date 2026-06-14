#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reviewer Module - 성과 분석 및 리뷰 시스템
"""

import logging
import os
import json
import re
import shutil
import subprocess
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from enum import Enum

class MarketRegime(Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    VOLATILE = "volatile"

@dataclass
class MarketState:
    regime: MarketRegime
    volatility_level: str
    trend_direction: str
    confidence: float
    timestamp: datetime

@dataclass
class PerformanceMetrics:
    total_return: float = 0.0
    annualized_return: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    calmar_ratio: float = 0.0
    sortino_ratio: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    turnover_rate: float = 0.0
    transaction_costs: float = 0.0
    net_return: float = 0.0

@dataclass
class TradeRecord:
    timestamp: datetime
    ticker: str
    action: str
    quantity: int
    price: float
    amount: float
    commission: float
    tax: float
    total_cost: float
    net_amount: float
    profit_loss: float = 0.0
    holding_period_days: int = 0
    sector: str = ""
    market_regime: str = ""

@dataclass
class PortfolioSnapshot:
    timestamp: datetime
    total_value: float
    cash: float
    holdings: List[Dict[str, Any]]
    performance_metrics: PerformanceMetrics
    market_state: Optional[MarketState] = None

class PerformanceReviewer:
    def __init__(self, settings: Dict[str, Any]):
        self.settings = settings
        self.logger = logging.getLogger(__name__)
    
    def calculate_advanced_performance_metrics(
        self,
        trade_records: List[TradeRecord],
        portfolio_snapshots: List[PortfolioSnapshot],
        risk_free_rate: float = 0.03
    ) -> PerformanceMetrics:
        """고급 성과 지표 계산"""
        try:
            if not trade_records or not portfolio_snapshots:
                return PerformanceMetrics()
            
            total_return = self._calculate_total_return(portfolio_snapshots)
            annualized_return = self._calculate_annualized_return(portfolio_snapshots)
            volatility = self._calculate_volatility(portfolio_snapshots)
            sharpe_ratio = self._calculate_sharpe_ratio(portfolio_snapshots, risk_free_rate)
            max_drawdown = self._calculate_max_drawdown(portfolio_snapshots)
            win_rate = self._calculate_win_rate(trade_records)
            profit_factor = self._calculate_profit_factor(trade_records)
            calmar_ratio = self._calculate_calmar_ratio(annualized_return, max_drawdown)
            sortino_ratio = self._calculate_sortino_ratio(portfolio_snapshots, risk_free_rate)
            var_95, cvar_95 = self._calculate_var_cvar(portfolio_snapshots)
            turnover_rate = self._calculate_turnover_rate(trade_records, portfolio_snapshots)
            transaction_costs = self._calculate_transaction_costs(trade_records)
            net_return = total_return - transaction_costs
            
            return PerformanceMetrics(
                total_return=total_return,
                annualized_return=annualized_return,
                volatility=volatility,
                sharpe_ratio=sharpe_ratio,
                max_drawdown=max_drawdown,
                win_rate=win_rate,
                profit_factor=profit_factor,
                calmar_ratio=calmar_ratio,
                sortino_ratio=sortino_ratio,
                var_95=var_95,
                cvar_95=cvar_95,
                turnover_rate=turnover_rate,
                transaction_costs=transaction_costs,
                net_return=net_return
            )
            
        except Exception as e:
            self.logger.error(f"성과 지표 계산 실패: {e}")
            return PerformanceMetrics()
    
    def _calculate_total_return(self, snapshots: List[PortfolioSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        initial_value = snapshots[0].total_value
        final_value = snapshots[-1].total_value
        return (final_value - initial_value) / initial_value if initial_value > 0 else 0.0
    
    def _calculate_annualized_return(self, snapshots: List[PortfolioSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        total_return = self._calculate_total_return(snapshots)
        days = (snapshots[-1].timestamp - snapshots[0].timestamp).days
        if days <= 0:
            return 0.0
        return (1 + total_return) ** (365 / days) - 1
    
    def _calculate_volatility(self, snapshots: List[PortfolioSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        values = [s.total_value for s in snapshots]
        returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
        return np.std(returns) * np.sqrt(252) if returns else 0.0
    
    def _calculate_sharpe_ratio(self, snapshots: List[PortfolioSnapshot], risk_free_rate: float) -> float:
        if len(snapshots) < 2:
            return 0.0
        annualized_return = self._calculate_annualized_return(snapshots)
        volatility = self._calculate_volatility(snapshots)
        return (annualized_return - risk_free_rate) / volatility if volatility > 0 else 0.0
    
    def _calculate_max_drawdown(self, snapshots: List[PortfolioSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        values = [s.total_value for s in snapshots]
        peak = values[0]
        max_dd = 0.0
        for value in values:
            if value > peak:
                peak = value
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
        return max_dd
    
    def _calculate_win_rate(self, trade_records: List[TradeRecord]) -> float:
        sell_trades = [t for t in trade_records if t.action == "sell"]
        if not sell_trades:
            return 0.0
        winning_trades = [t for t in sell_trades if t.profit_loss > 0]
        return len(winning_trades) / len(sell_trades)
    
    def _calculate_profit_factor(self, trade_records: List[TradeRecord]) -> float:
        sell_trades = [t for t in trade_records if t.action == "sell"]
        if not sell_trades:
            return 0.0
        total_profit = sum(t.profit_loss for t in sell_trades if t.profit_loss > 0)
        total_loss = abs(sum(t.profit_loss for t in sell_trades if t.profit_loss < 0))
        return total_profit / total_loss if total_loss > 0 else float('inf')
    
    def _calculate_calmar_ratio(self, annualized_return: float, max_drawdown: float) -> float:
        return annualized_return / max_drawdown if max_drawdown > 0 else 0.0
    
    def _calculate_sortino_ratio(self, snapshots: List[PortfolioSnapshot], risk_free_rate: float) -> float:
        if len(snapshots) < 2:
            return 0.0
        annualized_return = self._calculate_annualized_return(snapshots)
        values = [s.total_value for s in snapshots]
        returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
        negative_returns = [r for r in returns if r < 0]
        downside_volatility = np.std(negative_returns) * np.sqrt(252) if negative_returns else 0.0
        return (annualized_return - risk_free_rate) / downside_volatility if downside_volatility > 0 else 0.0
    
    def _calculate_var_cvar(self, snapshots: List[PortfolioSnapshot], confidence_level: float = 0.95) -> tuple:
        if len(snapshots) < 2:
            return 0.0, 0.0
        values = [s.total_value for s in snapshots]
        returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
        if not returns:
            return 0.0, 0.0
        sorted_returns = sorted(returns)
        var_index = int((1 - confidence_level) * len(sorted_returns))
        var = sorted_returns[var_index] if var_index < len(sorted_returns) else sorted_returns[0]
        cvar_returns = [r for r in sorted_returns if r <= var]
        cvar = np.mean(cvar_returns) if cvar_returns else var
        return var, cvar
    
    def _calculate_turnover_rate(self, trade_records: List[TradeRecord], snapshots: List[PortfolioSnapshot]) -> float:
        if not snapshots:
            return 0.0
        total_volume = sum(abs(t.amount) for t in trade_records)
        avg_portfolio_value = np.mean([s.total_value for s in snapshots])
        return total_volume / avg_portfolio_value if avg_portfolio_value > 0 else 0.0
    
    def _calculate_transaction_costs(self, trade_records: List[TradeRecord]) -> float:
        return sum(t.total_cost for t in trade_records)

    def analyze_sector_performance(
        self,
        trade_records: List[TradeRecord],
        portfolio_snapshots: List[PortfolioSnapshot]
    ) -> Dict[str, Dict[str, float]]:
        """섹터별 성과 분석"""
        try:
            sector_analysis = {}
            
            # 섹터별 거래 기록 그룹화
            sector_trades = {}
            for record in trade_records:
                if record.sector not in sector_trades:
                    sector_trades[record.sector] = []
                sector_trades[record.sector].append(record)
            
            # 섹터별 성과 계산
            for sector, trades in sector_trades.items():
                if not trades:
                    continue
                
                total_profit = sum(t.profit_loss for t in trades if t.action == "sell")
                total_volume = sum(t.amount for t in trades if t.action == "sell")
                trade_count = len([t for t in trades if t.action == "sell"])
                win_count = len([t for t in trades if t.action == "sell" and t.profit_loss > 0])
                
                sector_analysis[sector] = {
                    "total_profit": total_profit,
                    "total_volume": total_volume,
                    "trade_count": trade_count,
                    "win_rate": win_count / trade_count if trade_count > 0 else 0,
                    "avg_profit": total_profit / trade_count if trade_count > 0 else 0,
                    "weight": total_volume / sum(sector_trades[s].amount for s in sector_trades for s in sector_trades[s] if s.action == "sell") if any(sector_trades.values()) else 0
                }
            
            return sector_analysis
            
        except Exception as e:
            self.logger.error(f"섹터별 성과 분석 실패: {e}")
            return {}
    
    def analyze_market_regime_performance(
        self,
        trade_records: List[TradeRecord],
        portfolio_snapshots: List[PortfolioSnapshot]
    ) -> Dict[str, Dict[str, float]]:
        """시장 상황별 성과 분석"""
        try:
            regime_analysis = {}
            
            # 시장 상황별 거래 기록 그룹화
            regime_trades = {}
            for record in trade_records:
                if record.market_regime not in regime_trades:
                    regime_trades[record.market_regime] = []
                regime_trades[record.market_regime].append(record)
            
            # 시장 상황별 성과 계산
            for regime, trades in regime_trades.items():
                if not trades:
                    continue
                
                total_profit = sum(t.profit_loss for t in trades if t.action == "sell")
                total_volume = sum(t.amount for t in trades if t.action == "sell")
                trade_count = len([t for t in trades if t.action == "sell"])
                win_count = len([t for t in trades if t.action == "sell" and t.profit_loss > 0])
                
                regime_analysis[regime] = {
                    "total_profit": total_profit,
                    "total_volume": total_volume,
                    "trade_count": trade_count,
                    "win_rate": win_count / trade_count if trade_count > 0 else 0,
                    "avg_profit": total_profit / trade_count if trade_count > 0 else 0
                }
            
            return regime_analysis
            
        except Exception as e:
            self.logger.error(f"시장 상황별 성과 분석 실패: {e}")
            return {}

    def analyze_sector_performance_fixed(
        self,
        trade_records: List[TradeRecord],
        portfolio_snapshots: List[PortfolioSnapshot]
    ) -> Dict[str, Dict[str, float]]:
        """섹터별 성과 분석 (수정된 버전)"""
        try:
            sector_analysis = {}
            
            # 섹터별 거래 기록 그룹화
            sector_trades = {}
            for record in trade_records:
                if record.sector and record.sector not in sector_trades:
                    sector_trades[record.sector] = []
                if record.sector:
                    sector_trades[record.sector].append(record)
            
            # 섹터별 성과 계산
            for sector, trades in sector_trades.items():
                if not trades:
                    continue
                
                total_profit = sum(t.profit_loss for t in trades if t.action == "sell")
                total_volume = sum(t.amount for t in trades if t.action == "sell")
                trade_count = len([t for t in trades if t.action == "sell"])
                win_count = len([t for t in trades if t.action == "sell" and t.profit_loss > 0])
                
                # 전체 거래량 계산
                all_volume = sum(t.amount for s in sector_trades.values() for t in s if t.action == "sell")
                
                sector_analysis[sector] = {
                    "total_profit": total_profit,
                    "total_volume": total_volume,
                    "trade_count": trade_count,
                    "win_rate": win_count / trade_count if trade_count > 0 else 0,
                    "avg_profit": total_profit / trade_count if trade_count > 0 else 0,
                    "weight": total_volume / all_volume if all_volume > 0 else 0
                }
            
            return sector_analysis
            
        except Exception as e:
            self.logger.error(f"섹터별 성과 분석 실패: {e}")
            return {}


# ════════════════════════════════════════════════════════════════════
#  최근 1개월 DB 승패 분석 → config.json 자동 튜닝 (파이프라인 단계)
# ════════════════════════════════════════════════════════════════════
logger = logging.getLogger("reviewer")

# ── 조정 대상 파라미터의 안전 범위(클램프) ──────────────────────────
AUTOSELL_STOP_LOSS_MIN = 0.02
AUTOSELL_STOP_LOSS_MAX = 0.08
AUTOSELL_TARGET_MIN = 0.04
AUTOSELL_TARGET_MAX = 0.15
# 한 회차당 최대 조정 폭(과적합/급변 방지)
ADJUST_STEP = 0.005

# 기본값(설정에 값이 없을 때)
DEFAULT_STOP_LOSS_PCT = 0.045
DEFAULT_TARGET_PCT = 0.07

# GPT 가 수정할 수 있는 config 섹션(화이트리스트)
TUNABLE_SECTIONS = ("screener_params", "risk_params", "strategy_params")
# 한 회차당 숫자 값의 최대 상대 변경 폭(과격한 변경 방지). env 로 조정.
DEFAULT_MAX_REL_CHANGE = 0.30
# 프롬프트에 포함할 코스피 뉴스 최대 개수
NEWS_MAX_ITEMS = 40
DEFAULT_MIN_SELL_TRADES = 10
DEFAULT_MAX_DIGEST = 15
DEFAULT_GIT_REPO = "tingcho330/kospistock"
DEFAULT_GIT_BRANCH = "main"
DEFAULT_DEPLOY_GRACE_DAYS = 3
DEFAULT_GIT_MAX_COMMITS = 50

# 매매·리스크·기록 로직 변경으로 거래 패턴이 바뀔 수 있는 경로
TRADING_RELEVANT_FILE_HINTS = (
    "config/config.json",
    "src/trader.py",
    "src/risk_manager.py",
    "src/strategies.py",
    "src/rotation_",
    "src/gpt_analyzer.py",
    "src/screener",
    "src/recorder.py",
    "src/order_reconciler.py",
    "src/integrated_manager.py",
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_structured_context(trade: Any) -> Dict[str, Any]:
    raw = getattr(trade, "structured_context", "") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}


def _sell_reason_text(trade: Any) -> str:
    sr = str(getattr(trade, "sell_reason", "") or "").strip()
    if sr:
        return sr[:200]
    ctx = _parse_structured_context(trade)
    return str(ctx.get("reason") or ctx.get("type") or "")[:200]


def _sell_reason_code(trade: Any) -> str:
    rc = str(getattr(trade, "reason_code", "") or "").strip()
    if rc:
        return rc
    ctx = _parse_structured_context(trade)
    return str(ctx.get("type") or ctx.get("reason_code") or "UNKNOWN")[:64]


def _sell_type(trade: Any) -> str:
    ctx = _parse_structured_context(trade)
    return str(ctx.get("type") or "UNKNOWN")[:64]


def filter_completed_sells(trade_records: List[Any]) -> List[Any]:
    try:
        from recorder import is_completed_sell
    except ImportError:
        is_completed_sell = lambda t: str(getattr(t, "action", "")).upper() == "SELL"
    return [t for t in trade_records if is_completed_sell(t)]


def analyze_by_reason_code(sells: List[Any]) -> Dict[str, Any]:
    """사유 코드·유형별 건수·승률."""
    by_code: Dict[str, Dict[str, Any]] = {}
    by_type: Dict[str, Dict[str, Any]] = {}
    for t in sells:
        code = _sell_reason_code(t)
        typ = _sell_type(t)
        pnl = float(getattr(t, "profit_loss", 0.0) or 0.0)
        for bucket, key in ((by_code, code), (by_type, typ)):
            if key not in bucket:
                bucket[key] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            bucket[key]["count"] += 1
            if pnl > 0:
                bucket[key]["wins"] += 1
            elif pnl < 0:
                bucket[key]["losses"] += 1
            bucket[key]["net_pnl"] = round(bucket[key]["net_pnl"] + pnl, 2)
    for bucket in (by_code, by_type):
        for k, v in bucket.items():
            c = v["count"]
            v["win_rate"] = round(v["wins"] / c, 4) if c else 0.0
    return {"by_reason_code": by_code, "by_sell_type": by_type}


def summarize_sell_context(sells: List[Any]) -> Dict[str, Any]:
    """GPT·Discord용 매도 사유 요약."""
    reason_stats = analyze_by_reason_code(sells)
    top_codes = sorted(
        reason_stats["by_reason_code"].items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    )[:5]
    top_types = sorted(
        reason_stats["by_sell_type"].items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    )[:5]
    return {
        "completed_sell_count": len(sells),
        "top_reason_codes": [{"code": k, **v} for k, v in top_codes],
        "top_sell_types": [{"type": k, **v} for k, v in top_types],
        **reason_stats,
    }


def build_trade_digest(sells: List[Any], *, max_items: int = DEFAULT_MAX_DIGEST) -> List[Dict[str, Any]]:
    """대표 매도 거래 목록 (최근 + 극단 PnL)."""
    if not sells:
        return []

    def _row(t: Any) -> Dict[str, Any]:
        ts = getattr(t, "timestamp", None)
        date_s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        qty = int(getattr(t, "executed_qty", 0) or getattr(t, "quantity", 0) or 0)
        return {
            "date": date_s,
            "ticker": str(getattr(t, "ticker", "")).zfill(6),
            "qty": qty,
            "price": round(float(getattr(t, "price", 0) or 0), 2),
            "pnl": round(float(getattr(t, "profit_loss", 0.0) or 0.0), 2),
            "reason_code": _sell_reason_code(t),
            "sell_type": _sell_type(t),
            "reason": _sell_reason_text(t),
            "holding_days": int(getattr(t, "holding_period_days", 0) or 0),
        }

    ordered = sorted(sells, key=lambda t: getattr(t, "timestamp", datetime.min))
    recent = ordered[-max(1, max_items // 2):]
    by_pnl = sorted(sells, key=lambda t: float(getattr(t, "profit_loss", 0.0) or 0.0))
    extremes: List[Any] = []
    if by_pnl:
        extremes.append(by_pnl[0])
        if len(by_pnl) > 1:
            extremes.append(by_pnl[-1])

    seen_ts = set()
    digest: List[Dict[str, Any]] = []
    for t in list(recent) + extremes:
        key = (getattr(t, "ticker", ""), getattr(t, "timestamp", ""))
        if key in seen_ts:
            continue
        seen_ts.add(key)
        digest.append(_row(t))
        if len(digest) >= max_items:
            break
    return digest


def summarize_portfolio_period(snapshots: List[Any]) -> Dict[str, Any]:
    """기간 내 포트폴리오 스냅샷 요약."""
    if not snapshots:
        return {"snapshot_count": 0}
    ordered = sorted(snapshots, key=lambda s: getattr(s, "timestamp", datetime.min))
    first, last = ordered[0], ordered[-1]
    v0 = float(getattr(first, "total_value", 0) or 0)
    v1 = float(getattr(last, "total_value", 0) or 0)
    period_return = ((v1 - v0) / v0) if v0 > 0 else 0.0
    peak = v0
    max_dd = 0.0
    for s in ordered:
        v = float(getattr(s, "total_value", 0) or 0)
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
    return {
        "snapshot_count": len(ordered),
        "start_value": round(v0, 0),
        "end_value": round(v1, 0),
        "period_return_pct": round(period_return * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "start_date": getattr(first, "timestamp", datetime.min).strftime("%Y-%m-%d")
        if hasattr(getattr(first, "timestamp", None), "strftime")
        else "",
        "end_date": getattr(last, "timestamp", datetime.min).strftime("%Y-%m-%d")
        if hasattr(getattr(last, "timestamp", None), "strftime")
        else "",
    }


def load_gpt_trade_hints(lookback_days: int) -> Dict[str, Dict[str, Any]]:
    """기간 내 gpt_trades JSON에서 종목별 최신 매수 판단."""
    try:
        from utils import find_latest_file, OUTPUT_DIR
    except ImportError:
        return {}

    hints: Dict[str, Dict[str, Any]] = {}
    cutoff = datetime.now() - timedelta(days=lookback_days)
    try:
        paths = sorted(OUTPUT_DIR.glob("gpt_trades_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        paths = []
    for path in paths[:12]:
        try:
            stem = path.stem
            parts = stem.split("_")
            if len(parts) >= 3 and len(parts[2]) == 8:
                file_dt = datetime.strptime(parts[2], "%Y%m%d")
                if file_dt < cutoff:
                    continue
        except Exception:
            pass
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        plans = raw.get("plans", raw) if isinstance(raw, dict) else raw
        if not isinstance(plans, list):
            continue
        for item in plans:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or item.get("Ticker") or "").zfill(6)
            if not ticker or ticker in hints:
                continue
            hints[ticker] = {
                "decision": item.get("decision") or item.get("action"),
                "confidence": item.get("confidence"),
                "summary": (item.get("summary") or item.get("reason") or "")[:120],
                "source_file": path.name,
            }
    return hints


def join_gpt_outcomes(sells: List[Any], gpt_hints: Dict[str, Dict[str, Any]], *, max_items: int = 8) -> List[Dict[str, Any]]:
    """매도 결과 ↔ 당시 GPT 매수 판단 대조."""
    out: List[Dict[str, Any]] = []
    for t in sorted(sells, key=lambda x: getattr(x, "timestamp", datetime.min), reverse=True):
        ticker = str(getattr(t, "ticker", "")).zfill(6)
        hint = gpt_hints.get(ticker)
        if not hint:
            continue
        ts = getattr(t, "timestamp", None)
        out.append({
            "ticker": ticker,
            "sell_date": ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else "",
            "pnl": round(float(getattr(t, "profit_loss", 0.0) or 0.0), 2),
            "sell_type": _sell_type(t),
            "gpt_decision": hint.get("decision"),
            "gpt_summary": hint.get("summary"),
            "gpt_source": hint.get("source_file"),
        })
        if len(out) >= max_items:
            break
    return out


def refresh_sell_pnl_batch(recorder: Any, sells: List[Any]) -> int:
    """profit_loss=0 인 체결 매도에 FIFO PnL 재계산."""
    n = 0
    for t in sells:
        if float(getattr(t, "profit_loss", 0.0) or 0.0) != 0.0:
            continue
        oid = str(getattr(t, "order_id", "") or "").strip()
        if oid and hasattr(recorder, "recompute_profit_loss_for_order_id"):
            if recorder.recompute_profit_loss_for_order_id(oid):
                n += 1
    return n


def analyze_win_loss(trade_records: List[Any], *, include_pending_for_stats: bool = False) -> Dict[str, Any]:
    """체결 완료 매도 기록으로부터 승패 통계를 계산한다."""
    completed = filter_completed_sells(trade_records)
    all_sells = [t for t in trade_records if str(getattr(t, "action", "")).upper() == "SELL"]
    sells = completed
    used_pending_fallback = False
    if not sells and include_pending_for_stats and all_sells:
        sells = all_sells
        used_pending_fallback = True
    n = len(sells)
    n_completed = len(completed)
    wins = [t for t in sells if (getattr(t, "profit_loss", 0.0) or 0.0) > 0]
    losses = [t for t in sells if (getattr(t, "profit_loss", 0.0) or 0.0) < 0]

    total_profit = sum((getattr(t, "profit_loss", 0.0) or 0.0) for t in wins)
    total_loss = abs(sum((getattr(t, "profit_loss", 0.0) or 0.0) for t in losses))

    win_rate = (len(wins) / n) if n > 0 else 0.0
    if total_loss > 0:
        profit_factor = total_profit / total_loss
    elif total_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    # 최대 연속 손실(시간 오름차순)
    ordered = sorted(sells, key=lambda t: getattr(t, "timestamp", datetime.min))
    max_consec_losses = 0
    cur = 0
    for t in ordered:
        if (getattr(t, "profit_loss", 0.0) or 0.0) < 0:
            cur += 1
            max_consec_losses = max(max_consec_losses, cur)
        else:
            cur = 0

    base = {
        "sell_trades": len(all_sells),
        "completed_sell_trades": n_completed,
        "stats_include_pending": used_pending_fallback,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "profit_factor": (round(profit_factor, 4) if profit_factor != float("inf") else "inf"),
        "total_profit": round(total_profit, 2),
        "total_loss": round(total_loss, 2),
        "net_pnl": round(total_profit - total_loss, 2),
        "max_consecutive_losses": max_consec_losses,
    }
    if sells:
        base.update(analyze_by_reason_code(sells))
    return base


def decide_autosell_adjustments(stats: Dict[str, Any], auto_sell: Dict[str, Any]) -> Tuple[Dict[str, float], List[str]]:
    """승패 통계에 따라 auto_sell.stop_loss_pct / target_pct 를 조정한다.

    반환: (변경된 값 dict, 사람이 읽는 변경 사유 리스트)
    실제 변경이 없으면 빈 dict 를 반환한다.
    """
    cur_stop = float(auto_sell.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT))
    cur_target = float(auto_sell.get("target_pct", DEFAULT_TARGET_PCT))
    new_stop, new_target = cur_stop, cur_target

    win_rate = float(stats.get("win_rate", 0.0))
    pf_raw = stats.get("profit_factor", 0.0)
    profit_factor = float("inf") if pf_raw == "inf" else float(pf_raw)

    reasons: List[str] = []

    # R1) 승률이 낮으면 손절을 더 타이트하게(작게) → 손실 거래 폭 축소
    if win_rate < 0.40:
        new_stop = _clamp(cur_stop - ADJUST_STEP, AUTOSELL_STOP_LOSS_MIN, AUTOSELL_STOP_LOSS_MAX)
        reasons.append(f"승률 낮음({win_rate:.0%}<40%) → 손절 타이트화 {cur_stop:.3f}→{new_stop:.3f}")

    # R2) 승률·손익비가 모두 좋으면 익절 목표를 더 높임 → 수익 추구
    if win_rate > 0.60 and profit_factor > 1.5:
        new_target = _clamp(cur_target + ADJUST_STEP, AUTOSELL_TARGET_MIN, AUTOSELL_TARGET_MAX)
        reasons.append(f"승률·PF 양호({win_rate:.0%},PF={profit_factor:.2f}) → 익절 목표 상향 {cur_target:.3f}→{new_target:.3f}")

    # R3) 손익비가 1 미만(손실 우위)이면 익절을 빨리(작게) → 수익 조기 실현
    if profit_factor < 1.0:
        new_target = _clamp(new_target - ADJUST_STEP, AUTOSELL_TARGET_MIN, AUTOSELL_TARGET_MAX)
        reasons.append(f"손익비 부진(PF={profit_factor:.2f}<1.0) → 익절 목표 하향 {cur_target:.3f}→{new_target:.3f}")

    # R4) 긴급 낙폭 손절 유형이 잦고 성과가 나쁘면 손절 폭 소폭 축소
    by_type = stats.get("by_sell_type") or {}
    em = by_type.get("EmergencyDrop") if isinstance(by_type, dict) else None
    if isinstance(em, dict) and int(em.get("count", 0)) >= 3:
        em_wr = float(em.get("win_rate", 0.0))
        if em_wr < 0.35:
            new_stop = _clamp(cur_stop - ADJUST_STEP, AUTOSELL_STOP_LOSS_MIN, AUTOSELL_STOP_LOSS_MAX)
            reasons.append(
                f"EmergencyDrop 다발({em.get('count')}건,승률{em_wr:.0%}) → 손절 타이트화"
            )

    changes: Dict[str, float] = {}
    if abs(new_stop - cur_stop) > 1e-9:
        changes["stop_loss_pct"] = round(new_stop, 4)
    if abs(new_target - cur_target) > 1e-9:
        changes["target_pct"] = round(new_target, 4)

    return changes, reasons


def _write_config_atomic(config_path, cfg: Dict[str, Any]) -> str:
    """config.json 을 백업 후 원자적으로 덮어쓴다. 백업 경로 반환."""
    config_path = str(config_path)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{config_path}.bak.{ts}"
    shutil.copy2(config_path, backup_path)

    tmp_path = f"{config_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, config_path)
    return backup_path


# ── Git 커밋 이력 (로컬 git → GitHub API 폴백) ───────────────────────
def _parse_repo_slug(repo: str) -> Tuple[str, str]:
    """'owner/repo' 또는 GitHub URL → (owner, repo)."""
    repo = (repo or DEFAULT_GIT_REPO).strip().rstrip("/")
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", repo)
    if m:
        return m.group(1), m.group(2).removesuffix(".git")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name.removesuffix(".git")
    raise ValueError(f"invalid git repo slug: {repo}")


def _find_git_root() -> Optional[Path]:
    """로컬 .git 디렉터리 탐색 (REVIEWER_GIT_ROOT 우선)."""
    explicit = os.getenv("REVIEWER_GIT_ROOT", "").strip()
    if explicit:
        root = Path(explicit)
        return root if (root / ".git").exists() else None
    for base in (
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent.parent,
        Path.cwd(),
        Path("/app"),
    ):
        for candidate in (base, *base.parents):
            if (candidate / ".git").exists():
                return candidate
            if candidate.parent == candidate:
                break
    return None


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _file_affects_trading(filename: str) -> bool:
    fn = _normalize_path(filename)
    return any(h in fn for h in TRADING_RELEVANT_FILE_HINTS)


def _parse_commit_datetime(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _commit_in_range(commit_dt: Optional[datetime], start_dt: datetime, end_dt: datetime) -> bool:
    if commit_dt is None:
        return False
    return start_dt <= commit_dt <= end_dt


def _github_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": "kospistock-reviewer/1.0",
    })
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def _fetch_commits_local(
    repo_root: Path,
    start_dt: datetime,
    end_dt: datetime,
    max_commits: int,
) -> List[Dict[str, Any]]:
    """git log 로 기간 내 커밋·변경 파일 수집."""
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo_root), "log",
                f"--since={start_dt.isoformat()}",
                f"--until={end_dt.isoformat()}",
                f"-n{max_commits}",
                "--pretty=format:@@@%n%H%n%ai%n%s",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except Exception as e:
        logger.debug(f"local git log 실패: {e}")
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    commits: List[Dict[str, Any]] = []
    for block in proc.stdout.split("@@@")[1:]:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        full_sha, date_str, message = lines[0], lines[1], lines[2]
        files = [_normalize_path(f) for f in lines[3:] if f and not f.startswith("@@@")]
        commit_dt = _parse_commit_datetime(date_str)
        if not _commit_in_range(commit_dt, start_dt, end_dt):
            continue
        commits.append({
            "sha": full_sha[:7],
            "full_sha": full_sha,
            "date": date_str,
            "message": message[:200],
            "files": files,
            "source": "local_git",
        })
    return commits


def _fetch_commits_github(
    owner: str,
    repo: str,
    branch: str,
    start_dt: datetime,
    end_dt: datetime,
    max_commits: int,
) -> List[Dict[str, Any]]:
    """GitHub REST API 로 공개 저장소 커밋·변경 파일 수집."""
    session = _github_session()
    since_iso = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    try:
        resp = session.get(
            url,
            params={"sha": branch, "since": since_iso, "per_page": min(100, max_commits)},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"GitHub commits 요청 실패: {e}")
        return []
    if resp.status_code != 200:
        logger.warning(f"GitHub commits 조회 실패: HTTP {resp.status_code}")
        return []

    commits: List[Dict[str, Any]] = []
    for item in resp.json()[:max_commits]:
        commit_meta = item.get("commit") or {}
        author = commit_meta.get("author") or {}
        date_str = author.get("date", "")
        commit_dt = _parse_commit_datetime(date_str)
        if not _commit_in_range(commit_dt, start_dt, end_dt):
            continue

        full_sha = item.get("sha", "")
        message = (commit_meta.get("message") or "").split("\n")[0][:200]
        files: List[str] = []
        try:
            detail = session.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{full_sha}",
                timeout=30,
            )
            if detail.status_code == 200:
                files = [
                    _normalize_path(f.get("filename", ""))
                    for f in detail.json().get("files", [])
                    if f.get("filename")
                ]
        except Exception as e:
            logger.debug(f"GitHub commit detail 실패 ({full_sha[:7]}): {e}")

        commits.append({
            "sha": full_sha[:7],
            "full_sha": full_sha,
            "date": date_str,
            "message": message,
            "files": files,
            "source": "github_api",
        })
    return commits


def _enrich_commit_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    files = raw.get("files") or []
    trading_files = [f for f in files if _file_affects_trading(f)]
    return {
        **raw,
        "trading_relevant": bool(trading_files),
        "trading_files": trading_files,
    }


def summarize_code_changes(commits: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    enriched = [_enrich_commit_record(c) for c in commits]
    deploy_events = [c for c in enriched if c.get("trading_relevant")]
    timeline_lines = []
    for c in enriched[:20]:
        flag = " [매매로직]" if c.get("trading_relevant") else ""
        files = ", ".join((c.get("files") or [])[:8])
        timeline_lines.append(
            f"- {c.get('date', '')[:10]} {c.get('sha', '')} {c.get('message', '')}{flag}"
            + (f" ({files})" if files else "")
        )
    return {
        "source": source,
        "commit_count": len(enriched),
        "trading_relevant_count": len(deploy_events),
        "commits": enriched,
        "deploy_events": deploy_events,
        "timeline_text": "\n".join(timeline_lines) or "(기간 내 커밋 없음)",
    }


def collect_code_changes(start_dt: datetime, end_dt: datetime) -> Dict[str, Any]:
    """분석 기간 내 코드 변경 이력 수집 (로컬 git 우선, 없으면 GitHub API)."""
    if os.getenv("REVIEWER_GIT_DISABLED", "0") == "1":
        return summarize_code_changes([], "disabled")

    max_commits = int(os.getenv("REVIEWER_GIT_MAX_COMMITS", str(DEFAULT_GIT_MAX_COMMITS)))
    repo_slug = os.getenv("REVIEWER_GIT_REPO", DEFAULT_GIT_REPO)
    branch = os.getenv("REVIEWER_GIT_BRANCH", DEFAULT_GIT_BRANCH)

    commits: List[Dict[str, Any]] = []
    source = "none"

    repo_root = _find_git_root()
    if repo_root:
        commits = _fetch_commits_local(repo_root, start_dt, end_dt, max_commits)
        if commits:
            source = "local_git"

    if not commits:
        try:
            owner, repo = _parse_repo_slug(repo_slug)
            commits = _fetch_commits_github(owner, repo, branch, start_dt, end_dt, max_commits)
            if commits:
                source = "github_api"
        except Exception as e:
            logger.warning(f"GitHub 커밋 수집 실패: {e}")

    summary = summarize_code_changes(commits, source)
    logger.info(
        f"[git] source={source} commits={summary['commit_count']} "
        f"trading_relevant={summary['trading_relevant_count']}"
    )
    return summary


def _trade_timestamp(trade: Any) -> datetime:
    ts = getattr(trade, "timestamp", None)
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    if isinstance(ts, str):
        parsed = _parse_commit_datetime(ts)
        if parsed:
            return parsed
    return datetime.min


def _trade_in_deploy_grace(trade_dt: datetime, deploy_dt: datetime, grace_days: int) -> bool:
    if trade_dt is datetime.min or deploy_dt is datetime.min:
        return False
    return deploy_dt.date() <= trade_dt.date() <= (deploy_dt.date() + timedelta(days=grace_days))


def exclude_post_deploy_sells(
    sells: List[Any],
    deploy_events: List[Dict[str, Any]],
    grace_days: int,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """코드 배포 직후(grace) 매도는 분석에서 제외."""
    if not deploy_events or grace_days <= 0:
        return sells, []

    stable: List[Any] = []
    excluded: List[Dict[str, Any]] = []
    for t in sells:
        trade_dt = _trade_timestamp(t)
        matched = None
        for ev in deploy_events:
            deploy_dt = _parse_commit_datetime(ev.get("date", ""))
            if deploy_dt and _trade_in_deploy_grace(trade_dt, deploy_dt, grace_days):
                matched = ev
                break
        if matched:
            excluded.append({
                "date": trade_dt.strftime("%Y-%m-%d") if trade_dt != datetime.min else "",
                "ticker": str(getattr(t, "ticker", "")).zfill(6),
                "pnl": round(float(getattr(t, "profit_loss", 0.0) or 0.0), 2),
                "deploy_sha": matched.get("sha"),
                "deploy_date": (matched.get("date") or "")[:10],
                "deploy_message": matched.get("message", "")[:120],
            })
        else:
            stable.append(t)
    return stable, excluded


def build_stabilized_trade_stats(
    trades: List[Any],
    completed_sells: List[Any],
    deploy_events: List[Dict[str, Any]],
    grace_days: int,
    *,
    allow_partial: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """코드 배포 grace 구간 매도를 제외한 안정 구간 통계."""
    stable_sells, excluded = exclude_post_deploy_sells(completed_sells, deploy_events, grace_days)
    stable_keys = {
        (str(getattr(t, "ticker", "")), str(getattr(t, "timestamp", "")), str(getattr(t, "order_id", "")))
        for t in stable_sells
    }
    stable_trades = [
        t for t in trades
        if str(getattr(t, "action", "")).upper() != "SELL"
        or (str(getattr(t, "ticker", "")), str(getattr(t, "timestamp", "")), str(getattr(t, "order_id", ""))) in stable_keys
    ]
    stats = analyze_win_loss(stable_trades, include_pending_for_stats=allow_partial)
    stats["excluded_post_deploy_sells"] = len(excluded)
    stats["stable_completed_sell_trades"] = len(stable_sells)
    stats["deploy_grace_days"] = grace_days
    return stats, excluded


def select_stats_for_tuning(
    full_stats: Dict[str, Any],
    stable_stats: Dict[str, Any],
    min_trades: int,
) -> Dict[str, Any]:
    """튜닝용 통계: 배포 영향 제외 표본이 충분하면 stable 우선."""
    stable_n = int(stable_stats.get("completed_sell_trades", 0))
    excluded = int(stable_stats.get("excluded_post_deploy_sells", 0))
    threshold = max(5, min_trades // 2)
    if excluded > 0 and stable_n >= threshold:
        chosen = dict(stable_stats)
        chosen["stats_basis"] = "stable_excluding_deploy_grace"
        return chosen
    chosen = dict(full_stats)
    chosen["stats_basis"] = "full"
    return chosen


# ── 코스피 한 달 주요 뉴스 수집 ─────────────────────────────────────
def collect_kospi_news(lookback_days: int, max_items: int = NEWS_MAX_ITEMS) -> List[Dict[str, str]]:
    """최근 lookback_days 일간의 코스피/증시 관련 주요 뉴스를 수집한다.

    news_collector 의 네이버 뉴스 API 헬퍼를 재사용한다. NAVER_ID/SECRET 이
    없거나 실패하면 빈 리스트를 반환한다(GPT 프롬프트에서 뉴스 생략).
    """
    try:
        from news_collector import _fetch_naver_news_api, _parse_pubdate, _dedupe_items_by_title, _clean_text
    except Exception as e:
        logger.warning(f"news_collector import 실패 → 뉴스 생략: {e}")
        return []

    raw: List[Dict] = []
    for kw in ("코스피", "증시", "코스닥"):
        try:
            raw.extend(_fetch_naver_news_api(kw, 100) or [])
        except Exception as e:
            logger.debug(f"뉴스 조회 실패(kw={kw}): {e}")

    if not raw:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    filtered: List[Dict] = []
    for it in raw:
        pub_dt = None
        try:
            pub_dt = _parse_pubdate(it.get("pubDate", ""))
        except Exception:
            pub_dt = None
        if pub_dt is not None and pub_dt < cutoff:
            continue
        it["_pub_dt"] = pub_dt
        filtered.append(it)

    try:
        filtered = _dedupe_items_by_title(filtered)
    except Exception:
        pass

    filtered.sort(key=lambda x: x.get("_pub_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    out: List[Dict[str, str]] = []
    for it in filtered[:max_items]:
        title = _clean_text((it.get("title") or "").strip())
        desc = _clean_text((it.get("description") or "").strip())
        pub_dt = it.get("_pub_dt")
        out.append({
            "title": title,
            "desc": desc[:200],
            "date": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
        })
    return out


# ── GPT 기반 config 변경 제안 ───────────────────────────────────────
def _extract_tunable(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """튜닝 대상 3개 섹션만 추출(GPT 프롬프트용)."""
    return {sec: cfg.get(sec, {}) for sec in TUNABLE_SECTIONS}


def gpt_propose_config_changes(
    stats: Dict[str, Any],
    news: List[Dict[str, str]],
    cfg: Dict[str, Any],
    *,
    sell_summary: Optional[Dict[str, Any]] = None,
    trade_digest: Optional[List[Dict[str, Any]]] = None,
    portfolio_summary: Optional[Dict[str, Any]] = None,
    gpt_comparisons: Optional[List[Dict[str, Any]]] = None,
    code_changes: Optional[Dict[str, Any]] = None,
    stabilized_stats: Optional[Dict[str, Any]] = None,
    excluded_trades: Optional[List[Dict[str, Any]]] = None,
    sample_insufficient: bool = False,
    min_sell_trades: int = DEFAULT_MIN_SELL_TRADES,
) -> Optional[Dict[str, Any]]:
    """GPT 에 승패·사유·포트폴리오·뉴스·코드 변경·설정을 주고 변경안을 받는다."""
    try:
        from gpt_analyzer import _call_openai_json
    except Exception as e:
        logger.warning(f"gpt_analyzer import 실패 → GPT 제안 생략: {e}")
        return None

    current = _extract_tunable(cfg)
    news_lines = "\n".join(
        f"- [{n.get('date','')}] {n.get('title','')} :: {n.get('desc','')}" for n in news
    ) or "(수집된 뉴스 없음)"

    sample_note = ""
    if sample_insufficient:
        sample_note = (
            f"\n## 주의\n"
            f"- 체결 완료 매도가 {stats.get('completed_sell_trades', stats.get('sell_trades', 0))}건으로 "
            f"권장 최소 {min_sell_trades}건 미만입니다. 큰 변경은 하지 말고, reasons에 '표본 부족'을 명시하세요.\n"
        )

    system_prompt = (
        "당신은 한국 주식 자동매매 시스템의 리스크/전략 파라미터를 검토하는 퀀트 전문가입니다. "
        "실제 매매 승패·매도 사유·포트폴리오·시장 뉴스·코드 변경 이력을 함께 고려해 설정을 점진적으로 개선하세요. "
        "분석 기간 중 trader/risk_manager/config 등 매매 로직 커밋 직후 거래는 배포 영향으로 왜곡될 수 있습니다. "
        "stabilized_stats(배포 grace 제외)가 제공되면 튜닝 판단의 주 근거로 사용하고, "
        "full_stats와 불일치하는 이상 패턴은 코드 변경으로 설명 가능하면 파라미터 조정 근거에서 제외하세요. "
        "EmergencyDrop 등 손절 유형이 반복되면 auto_sell 관련 값을 보수적으로 조정할 수 있습니다. "
        "반드시 보수적으로 조정하고(한 번에 큰 변화 금지), 근거가 약하면 변경하지 마세요. "
        "응답은 반드시 단일 JSON 객체여야 합니다."
    )
    digest_json = json.dumps(trade_digest or [], ensure_ascii=False)
    sell_sum_json = json.dumps(sell_summary or {}, ensure_ascii=False)
    port_json = json.dumps(portfolio_summary or {}, ensure_ascii=False)
    gpt_cmp_json = json.dumps(gpt_comparisons or [], ensure_ascii=False)
    code_ctx = code_changes or {}
    git_timeline = code_ctx.get("timeline_text") or "(커밋 이력 없음)"
    excluded_json = json.dumps(excluded_trades or [], ensure_ascii=False)
    stable_json = json.dumps(stabilized_stats or {}, ensure_ascii=False)

    user_prompt = (
        "## 최근 매매 승패 통계 (체결 매도 기준, 전체)\n"
        f"{json.dumps(stats, ensure_ascii=False)}\n\n"
        "## 안정 구간 통계 (코드 배포 grace 직후 매도 제외, 튜닝 우선 근거)\n"
        f"{stable_json}\n\n"
        "## 배포 grace 로 제외된 매도 (코드 변경 영향 가능)\n"
        f"{excluded_json}\n\n"
        "## 분석 기간 Git 커밋 이력\n"
        f"source={code_ctx.get('source', 'none')}, "
        f"commits={code_ctx.get('commit_count', 0)}, "
        f"trading_relevant={code_ctx.get('trading_relevant_count', 0)}\n"
        f"{git_timeline}\n\n"
        "## 매도 사유 분포\n"
        f"{sell_sum_json}\n\n"
        "## 대표 매도 거래\n"
        f"{digest_json}\n\n"
        "## 포트폴리오 기간 요약\n"
        f"{port_json}\n\n"
        "## GPT 매수 판단 vs 실제 매도 결과 (있는 경우만)\n"
        f"{gpt_cmp_json}\n\n"
        "## 코스피 한 달 주요 뉴스\n"
        f"{news_lines}\n"
        f"{sample_note}\n"
        "## 현재 설정값 (이 키들만 조정 가능)\n"
        f"{json.dumps(current, ensure_ascii=False, indent=2)}\n\n"
        "## 지시\n"
        "- 위 3개 섹션(screener_params, risk_params, strategy_params)에 '이미 존재하는' 숫자/불리언 키만 조정하세요.\n"
        "- 새 키 추가, 키 삭제, 리스트/구조 변경은 금지합니다.\n"
        "- 변경이 필요 없는 키는 응답에 포함하지 마세요. 변경할 키만 새 값으로 포함하세요.\n"
        "- 각 숫자 값은 현재값 대비 과도하게 바꾸지 마세요(작은 폭의 점진적 조정).\n"
        "- 코드 배포 직후 거래 이상은 로직 변경으로 해석하고, stabilized_stats 기준으로 튜닝하세요.\n"
        "- 승률이 낮고 손실이 크면 리스크를 줄이고(손절 타이트, 포지션/익절 보수화), 성과가 좋고 시장이 우호적이면 소폭 공격적으로.\n\n"
        "## 출력 JSON 형식 (변경할 키만)\n"
        "{\n"
        '  "screener_params": { "<키>": <새값> },\n'
        '  "risk_params": { "<키>": <새값>, "auto_sell": { "<키>": <새값> } },\n'
        '  "strategy_params": { "<키>": <새값> },\n'
        '  "reasons": ["<한국어 근거 문장>", "..."]\n'
        "}"
    )

    proposal = _call_openai_json(system_prompt, user_prompt, max_retries=3)
    if not isinstance(proposal, dict):
        logger.info("GPT 제안 없음/실패")
        return None
    return proposal


def _sanitize_proposal(proposal: Dict[str, Any], cfg: Dict[str, Any],
                       max_rel_change: float) -> Tuple[Dict[str, Any], List[str]]:
    """GPT 제안을 검증해 안전한 변경만 추려낸다.

    규칙:
      - TUNABLE_SECTIONS 의 '기존 키'만 허용(신규 키/구조 변경 무시).
      - leaf 타입이 현재값과 일치해야 함(숫자↔숫자, bool↔bool).
      - 숫자는 현재값 대비 ±max_rel_change 범위로 클램프. 현재값이 0이면 변경 무시.
      - 1단계 중첩(auto_sell, rsi_sell_strategy, strategy_weights 등)까지만 허용.
    반환: (적용 가능한 변경 dict[section -> {key/path: newval}], 사람이 읽는 사유)
    """
    applied_changes: Dict[str, Any] = {}
    notes: List[str] = []

    def _coerce_numeric(cur, new):
        if isinstance(cur, bool):
            return bool(new) if isinstance(new, bool) else None
        if isinstance(cur, (int, float)) and isinstance(new, (int, float)) and not isinstance(new, bool):
            if cur == 0:
                return None  # 상대 변경폭 계산 불가 → 안전하게 무시
            lo = cur * (1 - max_rel_change)
            hi = cur * (1 + max_rel_change)
            clamped = max(min(float(new), max(lo, hi)), min(lo, hi))
            # int 키는 int 유지
            if isinstance(cur, int) and not isinstance(cur, bool):
                clamped = int(round(clamped))
            else:
                clamped = round(clamped, 6)
            return clamped
        return None

    for section in TUNABLE_SECTIONS:
        prop_sec = proposal.get(section)
        if not isinstance(prop_sec, dict):
            continue
        cur_sec = cfg.get(section, {})
        if not isinstance(cur_sec, dict):
            continue

        sec_changes: Dict[str, Any] = {}
        for key, new_val in prop_sec.items():
            if key not in cur_sec:
                continue  # 신규 키 금지
            cur_val = cur_sec[key]

            # 1단계 중첩 dict
            if isinstance(cur_val, dict) and isinstance(new_val, dict):
                nested: Dict[str, Any] = {}
                for nk, nv in new_val.items():
                    if nk not in cur_val:
                        continue
                    coerced = _coerce_numeric(cur_val[nk], nv)
                    if coerced is not None and coerced != cur_val[nk]:
                        nested[nk] = coerced
                        notes.append(f"{section}.{key}.{nk}: {cur_val[nk]} → {coerced}")
                if nested:
                    sec_changes[key] = nested
                continue

            coerced = _coerce_numeric(cur_val, new_val)
            if coerced is not None and coerced != cur_val:
                sec_changes[key] = coerced
                notes.append(f"{section}.{key}: {cur_val} → {coerced}")

        if sec_changes:
            applied_changes[section] = sec_changes

    return applied_changes, notes


def _apply_changes_to_cfg(cfg: Dict[str, Any], changes: Dict[str, Any]) -> None:
    """검증된 changes 를 cfg 에 in-place 반영(1단계 중첩 지원)."""
    for section, sec_changes in changes.items():
        target = cfg.setdefault(section, {})
        for key, val in sec_changes.items():
            if isinstance(val, dict) and isinstance(target.get(key), dict):
                target[key].update(val)
            else:
                target[key] = val


def _notify_summary(
    stats: Dict[str, Any],
    changes: Dict[str, Any],
    reasons: List[str],
    lookback_days: int,
    applied: bool,
    news_count: int,
    source: str,
    *,
    sell_summary: Optional[Dict[str, Any]] = None,
    portfolio_summary: Optional[Dict[str, Any]] = None,
    skipped: bool = False,
    skip_reason: str = "",
    code_changes: Optional[Dict[str, Any]] = None,
    stabilized_stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Discord 로 분석/튜닝 요약 전송(실패는 조용히 무시)."""
    try:
        from notifier import send_discord_message, WEBHOOK_URL, is_valid_webhook
        if not (WEBHOOK_URL and is_valid_webhook(WEBHOOK_URL)):
            return

        pf = stats.get("profit_factor", 0.0)
        fields = [
            {"name": "📅 분석 기간", "value": f"최근 {lookback_days}일", "inline": True},
            {
                "name": "📊 체결 매도",
                "value": f"{stats.get('completed_sell_trades', stats.get('sell_trades', 0))}건 "
                f"(승 {stats.get('wins', 0)}/패 {stats.get('losses', 0)})",
                "inline": True,
            },
            {"name": "🎯 승률", "value": f"{float(stats.get('win_rate', 0.0)):.1%}", "inline": True},
            {"name": "💹 손익비(PF)", "value": f"{pf}", "inline": True},
            {"name": "💰 순손익", "value": f"{stats.get('net_pnl', 0):,}", "inline": True},
            {"name": "📰 뉴스/판단", "value": f"{news_count}건 / {source}", "inline": True},
        ]
        if code_changes:
            git_src = code_changes.get("source", "none")
            git_n = code_changes.get("commit_count", 0)
            git_tr = code_changes.get("trading_relevant_count", 0)
            excluded_n = int((stabilized_stats or {}).get("excluded_post_deploy_sells", 0))
            git_line = f"{git_src} · 커밋 {git_n} (매매로직 {git_tr})"
            if excluded_n:
                git_line += f" · grace 제외 {excluded_n}건"
            fields.append({"name": "🧩 코드 변경", "value": git_line, "inline": False})
        if stabilized_stats and stabilized_stats.get("stats_basis") == "stable_excluding_deploy_grace":
            fields.append({
                "name": "📊 안정 구간 승률",
                "value": f"{float(stabilized_stats.get('win_rate', 0.0)):.1%} "
                f"({stabilized_stats.get('completed_sell_trades', 0)}건)",
                "inline": True,
            })
        if portfolio_summary and portfolio_summary.get("snapshot_count", 0) > 0:
            fields.append({
                "name": "📈 포트폴리오",
                "value": (
                    f"수익 {portfolio_summary.get('period_return_pct', 0)}% / "
                    f"MDD {portfolio_summary.get('max_drawdown_pct', 0)}%"
                ),
                "inline": True,
            })
        if sell_summary:
            tops = sell_summary.get("top_sell_types") or sell_summary.get("top_reason_codes") or []
            if tops:
                lines = []
                for item in tops[:3]:
                    label = item.get("type") or item.get("code") or "?"
                    lines.append(f"• {label}: {item.get('count', 0)}건")
                fields.append({
                    "name": "🏷️ 매도 사유 Top",
                    "value": "\n".join(lines)[:500],
                    "inline": False,
                })
        if skipped and skip_reason:
            fields.append({"name": "⏭️ 스킵", "value": skip_reason[:500], "inline": False})
        if reasons:
            change_lines = "\n".join(f"• {r}" for r in reasons)
            status = "✅ 적용됨" if applied else "🧪 미적용(드라이런)"
            fields.append({"name": f"⚙️ config 조정 {status}", "value": change_lines[:1000], "inline": False})
        else:
            fields.append({"name": "⚙️ config 조정", "value": "변경 없음", "inline": False})

        embed = {
            "type": "rich",
            "title": "🔎 Reviewer 성과 분석 & 자동 튜닝",
            "fields": fields,
            "color": 0x3498db,
            "timestamp": datetime.now().isoformat(),
        }
        send_discord_message(embeds=[embed])
    except Exception as e:
        logger.debug(f"Discord 요약 전송 실패: {e}")


def run_review() -> Dict[str, Any]:
    """파이프라인 진입점: 최근 N일 DB 승패 + 코스피 뉴스를 GPT 가 리뷰해
    config.json 의 screener_params/risk_params/strategy_params 를 조정한다."""
    from utils import setup_logging, CONFIG_PATH, OUTPUT_DIR
    setup_logging()

    lookback_days = int(os.getenv("REVIEWER_LOOKBACK_DAYS", "30"))
    min_trades = int(
        os.getenv("REVIEWER_MIN_SELL_TRADES", os.getenv("REVIEWER_MIN_TRADES", str(DEFAULT_MIN_SELL_TRADES)))
    )
    allow_partial = os.getenv("REVIEWER_ALLOW_PARTIAL", "0") == "1"
    max_digest = int(os.getenv("REVIEWER_MAX_DIGEST", str(DEFAULT_MAX_DIGEST)))
    dry_run = os.getenv("REVIEWER_DRY_RUN", "0") == "1"
    max_rel_change = float(os.getenv("REVIEWER_MAX_REL_CHANGE", str(DEFAULT_MAX_REL_CHANGE)))

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=lookback_days)

    logger.info(
        f"=== reviewer start (lookback={lookback_days}d, min_sell_trades={min_trades}, "
        f"allow_partial={allow_partial}, dry_run={dry_run}, max_rel_change={max_rel_change}) ==="
    )

    # 1) DB 에서 기간 내 거래 조회
    recorder = None
    try:
        from recorder import get_recorder
        recorder = get_recorder()
        trades = recorder.get_trade_records(start_date=start_dt, end_date=end_dt)
    except Exception as e:
        logger.error(f"거래 기록 조회 실패: {e}")
        trades = []

    completed_sells = filter_completed_sells(trades)
    if recorder and completed_sells:
        refreshed = refresh_sell_pnl_batch(recorder, completed_sells)
        if refreshed:
            logger.info(f"[pnl] FIFO 재계산 {refreshed}건")
            trades = recorder.get_trade_records(start_date=start_dt, end_date=end_dt)
            completed_sells = filter_completed_sells(trades)

    # 2) 승패·사유 분석 (보수 모드: 체결 매도 없으면 pending 포함해 사유만 GPT에 전달)
    review_sells = completed_sells
    if not review_sells and allow_partial:
        review_sells = [t for t in trades if str(getattr(t, "action", "")).upper() == "SELL"]
    stats = analyze_win_loss(trades, include_pending_for_stats=allow_partial)
    sell_summary = summarize_sell_context(review_sells)
    trade_digest = build_trade_digest(review_sells, max_items=max_digest)
    logger.info(f"[stats] {stats}")
    logger.info(f"[sell_summary] completed={sell_summary.get('completed_sell_count', 0)}")

    # 2b) Git 커밋 이력 + 배포 grace 구간 거래 제외 통계
    deploy_grace_days = int(os.getenv("REVIEWER_DEPLOY_GRACE_DAYS", str(DEFAULT_DEPLOY_GRACE_DAYS)))
    code_changes = collect_code_changes(start_dt, end_dt)
    deploy_events = code_changes.get("deploy_events") or []
    stabilized_stats, excluded_trades = build_stabilized_trade_stats(
        trades,
        completed_sells,
        deploy_events,
        deploy_grace_days,
        allow_partial=allow_partial,
    )
    tuning_stats = select_stats_for_tuning(stats, stabilized_stats, min_trades)
    if excluded_trades:
        logger.info(
            f"[git] deploy grace 제외 매도 {len(excluded_trades)}건 "
            f"(grace={deploy_grace_days}d, basis={tuning_stats.get('stats_basis')})"
        )

    # 3) 포트폴리오·GPT 대조 (Phase B)
    portfolio_summary: Dict[str, Any] = {}
    gpt_comparisons: List[Dict[str, Any]] = []
    if recorder:
        try:
            snaps = recorder.get_portfolio_snapshots(start_date=start_dt, end_date=end_dt)
            portfolio_summary = summarize_portfolio_period(snaps)
            logger.info(f"[portfolio] {portfolio_summary}")
        except Exception as e:
            logger.debug(f"스냅샷 요약 실패: {e}")
    gpt_hints = load_gpt_trade_hints(lookback_days)
    gpt_comparisons = join_gpt_outcomes(review_sells, gpt_hints)
    if gpt_comparisons:
        logger.info(f"[gpt_compare] {len(gpt_comparisons)}건")

    # 4) 코스피 한 달 주요 뉴스 수집
    news = collect_kospi_news(lookback_days)
    logger.info(f"[news] 수집 {len(news)}건")

    # 4) config 로드
    config_path = CONFIG_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.error(f"config 로드 실패({config_path}): {e}")
        cfg = None

    n_completed = int(stats.get("completed_sell_trades", 0))
    n_stable_completed = int(stabilized_stats.get("completed_sell_trades", n_completed))
    n_effective = n_stable_completed if excluded_trades else n_completed
    sample_insufficient = n_effective < min_trades

    result: Dict[str, Any] = {
        "timestamp": end_dt.isoformat(),
        "lookback_days": lookback_days,
        "stats": stats,
        "stabilized_stats": stabilized_stats,
        "tuning_stats_basis": tuning_stats.get("stats_basis"),
        "code_changes": {
            "source": code_changes.get("source"),
            "commit_count": code_changes.get("commit_count"),
            "trading_relevant_count": code_changes.get("trading_relevant_count"),
            "deploy_grace_days": deploy_grace_days,
            "timeline_text": code_changes.get("timeline_text"),
            "commits": code_changes.get("commits"),
            "excluded_trades": excluded_trades,
        },
        "sell_summary": sell_summary,
        "trade_digest": trade_digest,
        "digest_count": len(trade_digest),
        "portfolio_summary": portfolio_summary,
        "gpt_comparisons": gpt_comparisons,
        "news_count": len(news),
        "changes": {},
        "reasons": [],
        "source": "none",
        "applied": False,
        "skipped": False,
        "config_path": str(config_path),
        "min_sell_trades": min_trades,
        "allow_partial": allow_partial,
    }

    # 5) 표본 부족/설정 로드 실패 시 스킵
    if sample_insufficient and not allow_partial:
        result["skipped"] = True
        result["skip_reason"] = f"체결 매도 {n_effective}건 < 최소 {min_trades}건"
        logger.info(f"[skip] {result['skip_reason']} → config 변경 없음")
    elif cfg is None:
        result["skipped"] = True
        result["skip_reason"] = "config 로드 실패"
    else:
        changes: Dict[str, Any] = {}
        reasons: List[str] = []
        source = "none"

        if sample_insufficient:
            reasons.append(f"표본 부족: 체결 매도 {n_effective}건 < 권장 {min_trades}건 (보수 모드)")

        # 6) GPT 리뷰 → 변경 제안
        proposal = gpt_propose_config_changes(
            stats,
            news,
            cfg,
            sell_summary=sell_summary,
            trade_digest=trade_digest,
            portfolio_summary=portfolio_summary,
            gpt_comparisons=gpt_comparisons,
            code_changes=code_changes,
            stabilized_stats=stabilized_stats,
            excluded_trades=excluded_trades,
            sample_insufficient=sample_insufficient,
            min_sell_trades=min_trades,
        )
        if proposal:
            result["gpt_raw_proposal"] = proposal
            changes, notes = _sanitize_proposal(proposal, cfg, max_rel_change)
            gpt_reasons = proposal.get("reasons") if isinstance(proposal.get("reasons"), list) else []
            reasons = [str(r) for r in gpt_reasons][:10] + notes
            if changes:
                source = "gpt"

        # 7) GPT 불가/무변경 시 규칙 기반 auto_sell 폴백 (안정 구간 통계 우선)
        if not changes:
            auto_sell = cfg.setdefault("risk_params", {}).setdefault("auto_sell", {})
            fb_changes, fb_reasons = decide_autosell_adjustments(tuning_stats, auto_sell)
            if fb_changes and tuning_stats.get("stats_basis") == "stable_excluding_deploy_grace":
                fb_reasons = [f"[안정구간] {r}" for r in fb_reasons]
            if fb_changes:
                changes = {"risk_params": {"auto_sell": fb_changes}}
                reasons = fb_reasons
                source = "rule_fallback"

        result["changes"] = changes
        result["reasons"] = reasons
        result["source"] = source

        if changes:
            _apply_changes_to_cfg(cfg, changes)
            if dry_run:
                logger.info(f"[dry-run] source={source} 조정안: {changes} (config 미수정)")
            else:
                try:
                    backup = _write_config_atomic(config_path, cfg)
                    result["applied"] = True
                    result["backup"] = backup
                    logger.info(f"[applied] source={source} config 수정 완료: {changes} | backup={backup}")
                except Exception as e:
                    result["apply_error"] = str(e)
                    logger.error(f"config 수정 실패: {e}")
        else:
            logger.info("[no-change] 적용할 변경 없음")

    # 8) review_log.json 저장
    try:
        out_path = OUTPUT_DIR / "review_log.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"[review_log] 저장: {out_path}")
    except Exception as e:
        logger.error(f"review_log 저장 실패: {e}")

    # 9) Discord 요약
    _notify_summary(
        stats,
        result.get("changes", {}),
        result.get("reasons", []),
        lookback_days,
        result.get("applied", False),
        len(news),
        result.get("source", "none"),
        sell_summary=result.get("sell_summary"),
        portfolio_summary=result.get("portfolio_summary"),
        skipped=result.get("skipped", False),
        skip_reason=result.get("skip_reason", ""),
        code_changes=code_changes,
        stabilized_stats=stabilized_stats,
    )

    logger.info("=== reviewer done ===")
    return result


if __name__ == "__main__":
    run_review()
