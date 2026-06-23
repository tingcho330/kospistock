# src/asset_allocator.py
"""자산군(주식/459580/현금) 배분 예산 계산 — 주문 실행 없음."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set, Tuple, Union


def _normalize_ticker(ticker: Any) -> str:
    return str(ticker or "").strip().zfill(6)


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _asset_allocation_cfg(settings: Union[Dict[str, Any], Any]) -> Dict[str, Any]:
    if hasattr(settings, "get"):
        cfg = settings.get("asset_allocation")  # type: ignore[union-attr]
    elif isinstance(settings, dict):
        cfg = settings.get("asset_allocation")
    else:
        cfg = None
    return cfg if isinstance(cfg, dict) else {}


def bond_etf_tickers(settings: Union[Dict[str, Any], Any]) -> Set[str]:
    cfg = _asset_allocation_cfg(settings)
    tickers: Set[str] = set()
    for item in cfg.get("bond_etfs") or []:
        if isinstance(item, dict):
            t = item.get("ticker")
            if t:
                tickers.add(_normalize_ticker(t))
        elif item:
            tickers.add(_normalize_ticker(item))
    return tickers


def is_bond_etf(ticker: Any, settings: Union[Dict[str, Any], Any]) -> bool:
    return _normalize_ticker(ticker) in bond_etf_tickers(settings)


def _holding_qty(h: Dict[str, Any]) -> int:
    return _to_int(h.get("hldg_qty", 0))


def _holding_value(h: Dict[str, Any]) -> int:
    qty = _holding_qty(h)
    if qty <= 0:
        return 0
    evlu = _to_int(h.get("evlu_amt", 0))
    if evlu > 0:
        return evlu
    price = _to_int(h.get("prpr", 0))
    if price > 0:
        return price * qty
    return 0


def classify_holdings(
    holdings: List[Dict[str, Any]],
    settings: Union[Dict[str, Any], Any],
) -> Tuple[int, int]:
    """Returns (stock_value, bond_value)."""
    bond_set = bond_etf_tickers(settings)
    stock_value = 0
    bond_value = 0
    for h in holdings or []:
        if _holding_qty(h) <= 0:
            continue
        ticker = _normalize_ticker(h.get("pdno", ""))
        value = _holding_value(h)
        if ticker in bond_set:
            bond_value += value
        else:
            stock_value += value
    return stock_value, bond_value


@dataclass
class AllocationResult:
    enabled: bool = False
    total_asset: int = 0
    stock_value: int = 0
    bond_value: int = 0
    cash_value: int = 0
    stock_weight: float = 0.0
    bond_weight: float = 0.0
    cash_weight: float = 0.0
    target_stock_value: int = 0
    target_bond_value: int = 0
    target_cash_value: int = 0
    min_cash_amount: int = 0
    buyable_cash: int = 0
    stock_gap: int = 0
    bond_gap: int = 0
    stock_buy_budget: int = 0
    initial_bond_buy_budget: int = 0
    can_buy_stock: bool = False
    can_buy_bond: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def calculate_final_bond_buy_budget(
    initial_bond_buy_budget: int,
    post_stock_cash: int,
    min_cash_amount: int,
) -> int:
    return max(
        0,
        min(
            max(0, int(initial_bond_buy_budget)),
            max(0, int(post_stock_cash)) - max(0, int(min_cash_amount)),
        ),
    )


# backward-compatible alias (internal callers may still use either name)
compute_final_bond_buy_budget = calculate_final_bond_buy_budget


def compute_allocation(
    available_cash: int,
    holdings: List[Dict[str, Any]],
    settings: Union[Dict[str, Any], Any],
) -> AllocationResult:
    cfg = _asset_allocation_cfg(settings)
    cash = max(0, _to_int(available_cash))
    enabled = bool(cfg.get("enabled", False))

    if not enabled:
        stock_value, bond_value = classify_holdings(holdings or [], settings)
        total = stock_value + bond_value + cash
        return AllocationResult(
            enabled=False,
            total_asset=total,
            stock_value=stock_value,
            bond_value=bond_value,
            cash_value=cash,
            stock_buy_budget=cash,
            can_buy_stock=cash > 0,
            can_buy_bond=False,
            reason="disabled",
        )

    stock_target = float(cfg.get("stock_target_weight", 0.70))
    bond_target = float(cfg.get("bond_target_weight", 0.20))
    cash_target = float(cfg.get("cash_target_weight", 0.10))
    min_cash_weight = float(cfg.get("min_cash_weight", 0.05))
    rebalance_tolerance = float(cfg.get("rebalance_tolerance", 0.03))
    stock_block_when_over = bool(cfg.get("stock_buy_block_when_overweight", True))
    bond_buy_enabled = bool(cfg.get("bond_buy_enabled", True))

    stock_value, bond_value = classify_holdings(holdings or [], settings)
    total_asset = stock_value + bond_value + cash

    if total_asset <= 0:
        return AllocationResult(
            enabled=True,
            cash_value=cash,
            reason="zero_total_asset",
        )

    target_stock_value = int(total_asset * stock_target)
    target_bond_value = int(total_asset * bond_target)
    target_cash_value = int(total_asset * cash_target)
    min_cash_amount = int(total_asset * min_cash_weight)

    stock_weight = stock_value / total_asset
    bond_weight = bond_value / total_asset
    cash_weight = cash / total_asset

    reasons: List[str] = []

    if cash < min_cash_amount:
        return AllocationResult(
            enabled=True,
            total_asset=total_asset,
            stock_value=stock_value,
            bond_value=bond_value,
            cash_value=cash,
            stock_weight=stock_weight,
            bond_weight=bond_weight,
            cash_weight=cash_weight,
            target_stock_value=target_stock_value,
            target_bond_value=target_bond_value,
            target_cash_value=target_cash_value,
            min_cash_amount=min_cash_amount,
            buyable_cash=0,
            stock_gap=max(0, target_stock_value - stock_value),
            bond_gap=max(0, target_bond_value - bond_value),
            stock_buy_budget=0,
            initial_bond_buy_budget=0,
            can_buy_stock=False,
            can_buy_bond=False,
            reason="below_min_cash",
        )

    buyable_cash = max(0, cash - min_cash_amount)
    stock_gap = max(0, target_stock_value - stock_value)
    bond_gap = max(0, target_bond_value - bond_value)

    stock_buy_budget = min(buyable_cash, stock_gap)
    initial_bond_buy_budget = min(max(0, buyable_cash - stock_buy_budget), bond_gap)

    can_buy_stock = stock_buy_budget > 0
    can_buy_bond = initial_bond_buy_budget > 0 and bond_buy_enabled

    if stock_block_when_over and stock_weight > stock_target + rebalance_tolerance:
        stock_buy_budget = 0
        initial_bond_buy_budget = min(buyable_cash, bond_gap)
        can_buy_stock = False
        can_buy_bond = initial_bond_buy_budget > 0 and bond_buy_enabled
        reasons.append("stock_overweight")

    if bond_weight >= bond_target:
        initial_bond_buy_budget = 0
        can_buy_bond = False
        reasons.append("bond_at_target")

    if not bond_buy_enabled:
        initial_bond_buy_budget = 0
        can_buy_bond = False
        reasons.append("bond_buy_disabled")

    if can_buy_stock and can_buy_bond:
        reason = "ok"
    elif can_buy_stock:
        reason = "stock_only"
    elif can_buy_bond:
        reason = "bond_only"
    elif reasons:
        reason = ";".join(reasons)
    else:
        reason = "no_buy_budget"

    return AllocationResult(
        enabled=True,
        total_asset=total_asset,
        stock_value=stock_value,
        bond_value=bond_value,
        cash_value=cash,
        stock_weight=stock_weight,
        bond_weight=bond_weight,
        cash_weight=cash_weight,
        target_stock_value=target_stock_value,
        target_bond_value=target_bond_value,
        target_cash_value=target_cash_value,
        min_cash_amount=min_cash_amount,
        buyable_cash=buyable_cash,
        stock_gap=stock_gap,
        bond_gap=bond_gap,
        stock_buy_budget=stock_buy_budget,
        initial_bond_buy_budget=initial_bond_buy_budget,
        can_buy_stock=can_buy_stock,
        can_buy_bond=can_buy_bond,
        reason=reason,
    )
