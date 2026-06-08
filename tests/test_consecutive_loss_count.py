#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""연속 손실 집계·중복 SELL 방지 검증."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import recorder as rec_mod
from recorder import (
    DataRecorder,
    TradeRecord,
    record_trade,
    count_consecutive_losses,
    is_countable_loss_sell,
)


def _reset_recorder(db_path: str) -> DataRecorder:
    rec_mod._recorder_instance = DataRecorder(db_path=db_path)
    return rec_mod._recorder_instance


class ConsecutiveLossCountTests(unittest.TestCase):
    """2026-06-08 로그 재현: risk_manager SELL + trader 중복 SELL."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_trading_data.db")
        self.recorder = _reset_recorder(self.db_path)
        self.base_ts = datetime(2026, 6, 8, 9, 25, 0)

        # 선행 매수 (FIFO 손익 계산용)
        for ticker, price, qty in [
            ("005380", 651000, 1),
            ("000270", 155400, 6),
            ("105560", 168800, 5),
        ]:
            self.recorder.save_trade_record(
                TradeRecord(
                    timestamp=self.base_ts - timedelta(days=1),
                    ticker=ticker,
                    action="BUY",
                    quantity=qty,
                    price=price,
                    amount=price * qty,
                    commission=0,
                    tax=0,
                    total_cost=price * qty,
                    net_amount=price * qty,
                    order_status="executed",
                    order_id=f"BUY-{ticker}",
                    executed_qty=qty,
                )
            )

        # 1) risk_manager direct_execute: pending SELL + order_id
        self.risk_sells = [
            ("005380", 1, 631000, "0011638200", -20000.0),
            ("000270", 6, 150600, "0013707800", -28800.0),
            ("105560", 5, 162900, "0013913300", -29500.0),
        ]
        for i, (ticker, qty, price, oid, pnl) in enumerate(self.risk_sells):
            record_trade({
                "side": "sell",
                "ticker": ticker,
                "qty": qty,
                "price": price,
                "trade_status": "pending",
                "order_id": oid,
                "requested_qty": qty,
                "executed_qty": 0,
                "pnl_amount": pnl,
            })

    def tearDown(self):
        rec_mod._recorder_instance = None
        self._tmpdir.cleanup()

    def _today_sells_newest_first(self):
        trades = self.recorder.get_trade_records(action="SELL")
        trades.sort(key=lambda t: t.timestamp, reverse=True)
        return trades

    def test_pending_only_counts_zero_losses(self):
        """pending SELL(손실 PnL 포함)은 연속 손실에 포함되지 않음."""
        losses = count_consecutive_losses(self._today_sells_newest_first())
        self.assertEqual(losses, 0)

    def test_trader_duplicate_failed_skipped_not_inserted(self):
        """trader run_sell_logic failed SELL(order_id 없음)은 중복 skip → row 증가 없음."""
        before = len(self.recorder.get_trade_records(action="SELL"))

        for ticker, qty, price, _, pnl in self.risk_sells[:2]:
            ok = record_trade({
                "side": "sell",
                "ticker": ticker,
                "qty": qty,
                "price": price,
                "trade_status": "failed",
                "order_id": None,
                "requested_qty": qty,
                "executed_qty": 0,
                "pnl_amount": pnl,
            })
            self.assertTrue(ok)

        after = len(self.recorder.get_trade_records(action="SELL"))
        self.assertEqual(after, before)
        self.assertEqual(count_consecutive_losses(self._today_sells_newest_first()), 0)

    def test_trader_duplicate_completed_updates_existing_not_double_count(self):
        """order_id 없는 completed SELL은 기존 pending 행 UPDATE, 신규 INSERT 없음."""
        before = len(self.recorder.get_trade_records(action="SELL"))

        record_trade({
            "side": "sell",
            "ticker": "105560",
            "qty": 5,
            "price": 163400,
            "trade_status": "completed",
            "order_id": None,
            "requested_qty": 5,
            "executed_qty": 5,
            "pnl_amount": -27000.0,
        })

        after = len(self.recorder.get_trade_records(action="SELL"))
        self.assertEqual(after, before)

        rows = [t for t in self.recorder.get_trade_records(ticker="105560", action="SELL")]
        executed = [t for t in rows if t.order_id == "0013913300"]
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0].order_status, "executed")
        self.assertEqual(executed[0].executed_qty, 5)

    def test_after_reconcile_three_countable_losses_not_six(self):
        """reconciler pending→executed 후 연속 손실 3건 (6건 아님)."""
        # trader 중복 failed/completed 시뮬레이션
        for ticker, qty, price, _, pnl in self.risk_sells:
            record_trade({
                "side": "sell",
                "ticker": ticker,
                "qty": qty,
                "price": price,
                "trade_status": "failed",
                "order_id": None,
                "requested_qty": qty,
                "executed_qty": 0,
                "pnl_amount": pnl,
            })
        record_trade({
            "side": "sell",
            "ticker": "105560",
            "qty": 5,
            "price": 163400,
            "trade_status": "completed",
            "order_id": None,
            "requested_qty": 5,
            "executed_qty": 5,
            "pnl_amount": -27000.0,
        })

        # reconciler: pending → executed
        for _, qty, price, oid, _ in self.risk_sells:
            self.recorder.update_order_status(
                order_id=oid,
                order_status="executed",
                executed_qty=qty,
                price=price,
            )

        sells = self.recorder.get_trade_records(action="SELL")
        self.assertEqual(len(sells), 3, "중복 INSERT 없이 고유 SELL 3건만 유지")

        countable = [t for t in sells if is_countable_loss_sell(t)]
        self.assertEqual(len(countable), 3)

        losses = count_consecutive_losses(self._today_sells_newest_first())
        self.assertEqual(losses, 3)

    def test_failed_sell_never_counted_even_if_negative_pnl(self):
        """failed SELL은 profit_loss<0 이어도 집계 제외."""
        record_trade({
            "side": "sell",
            "ticker": "999999",
            "qty": 1,
            "price": 1000,
            "trade_status": "failed",
            "order_id": "",
            "requested_qty": 1,
            "executed_qty": 0,
            "pnl_amount": -500.0,
        })
        rows = self.recorder.get_trade_records(ticker="999999", action="SELL")
        self.assertEqual(len(rows), 1)
        self.assertFalse(is_countable_loss_sell(rows[0]))


if __name__ == "__main__":
    unittest.main()
