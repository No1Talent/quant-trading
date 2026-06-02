"""IntradayTickStrategy: 突破开仓 + ExitPolicy 离场 + 尾盘强平。

离场现由 ExitPolicy 决策，open()/close() 由 on_trade 驱动 —— 测试通过模拟 on_trade
（合成成交）登记逻辑仓，而非直接塞 entry_price，以反映真实生命周期。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from vnpy.trader.constant import Direction, Offset

from strategies.intraday_tick_strategy import IntradayTickStrategy

_NORMAL_DT = datetime(2024, 1, 1, 10, 0, 0)
_EXIT_DT = datetime(2024, 1, 1, 14, 50, 0)


def _make_strategy(setting: dict | None = None) -> IntradayTickStrategy:
    engine = MagicMock()
    s = IntradayTickStrategy(engine, "TestIntraday", "rb2510.SHFE", setting or {})
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


@pytest.fixture
def strategy():
    return _make_strategy()


def _tick(
    last: float,
    bid_vol: int = 100,
    ask_vol: int = 100,
    dt: datetime = _NORMAL_DT,
) -> SimpleNamespace:
    return SimpleNamespace(
        last_price=last,
        bid_volume_1=bid_vol,
        ask_volume_1=ask_vol,
        bid_price_1=last - 0.5,
        ask_price_1=last + 0.5,
        datetime=dt,
    )


def _trade(direction: Direction, offset: Offset, price: float = 100.0, volume: int = 1):
    return SimpleNamespace(direction=direction, offset=offset, price=price, volume=volume)


def _open(strategy, direction: int, entry: float = 100.0) -> None:
    """模拟一次开仓成交：set pos + 派发 on_trade，使 ExitPolicy 登记逻辑仓。"""
    strategy.pos = direction
    d = Direction.LONG if direction > 0 else Direction.SHORT
    strategy.on_trade(_trade(d, Offset.OPEN, entry))


def _fill_buffer(strategy, prices: list[float]) -> None:
    """喂满价格缓冲且不触发开仓（平衡买卖量 → 压力比=1.0 < 1.5）。"""
    for p in prices:
        strategy.on_tick(_tick(p))
    strategy.buy.reset_mock()
    strategy.sell.reset_mock()
    strategy.short.reset_mock()
    strategy.cover.reset_mock()


class TestEntry:
    def test_buffer_not_full_no_orders(self, strategy):
        for i in range(10):  # price_window=20; only half filled
            strategy.on_tick(_tick(100.0 + i))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_breakout_long_with_buy_pressure(self, strategy):
        _fill_buffer(strategy, [90.0 + i for i in range(20)])  # max=109
        strategy.on_tick(_tick(109.0, bid_vol=200, ask_vol=100))  # 买压2.0 ≥ 1.5
        strategy.buy.assert_called_once()

    def test_breakout_short_with_sell_pressure(self, strategy):
        _fill_buffer(strategy, [90.0 + i for i in range(20)])  # min=90
        strategy.on_tick(_tick(90.0, bid_vol=100, ask_vol=200))  # 卖压2.0 ≥ 1.5
        strategy.short.assert_called_once()

    def test_insufficient_buy_pressure_no_entry(self, strategy):
        _fill_buffer(strategy, [90.0 + i for i in range(20)])
        strategy.on_tick(_tick(109.0, bid_vol=100, ask_vol=100))  # 压力1.0 < 1.5
        strategy.buy.assert_not_called()

    def test_no_entry_while_holding(self, strategy):
        # 持仓中即便又一次满足突破，也不应再开仓（持仓时 return）
        _fill_buffer(strategy, [90.0 + i for i in range(20)])
        _open(strategy, 1, 100.0)
        strategy.on_tick(_tick(109.0, bid_vol=200, ask_vol=100))
        strategy.buy.assert_not_called()

    def test_missing_bid_ask_volume_skipped(self, strategy):
        tick = SimpleNamespace(
            last_price=100.0,
            bid_volume_1=0,  # falsy → early return
            ask_volume_1=100,
            bid_price_1=99.5,
            ask_price_1=100.5,
            datetime=_NORMAL_DT,
        )
        strategy.on_tick(tick)
        strategy.buy.assert_not_called()


class TestExitPolicyLifecycle:
    def test_open_trade_registers_policy(self, strategy):
        assert not strategy.exit_policy.active
        _open(strategy, 1, 100.0)
        assert strategy.exit_policy.active
        assert strategy.exit_policy.direction == 1
        assert strategy.exit_policy.entry_price == 100.0

    def test_close_trade_clears_policy(self, strategy):
        _open(strategy, 1, 100.0)
        strategy.pos = 0
        strategy.on_trade(_trade(Direction.LONG, Offset.CLOSE, 110.0))
        assert not strategy.exit_policy.active

    def test_no_phantom_exit_without_fill(self, strategy):
        # 单一事实源：发单未成交（无 on_trade）→ policy 不 active → 大幅波动也不离场
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 0
        strategy.on_tick(_tick(80.0))
        strategy.sell.assert_not_called()
        strategy.cover.assert_not_called()


class TestThresholdExits:
    def test_profit_target_exits_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, 1, 100.0)
        strategy.on_tick(_tick(110.0))  # 浮盈10 ≥ profit_target(10)
        strategy.sell.assert_called_once()

    def test_stop_loss_exits_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, 1, 100.0)
        strategy.on_tick(_tick(94.0))  # 浮盈-6 ≤ -stop_loss(-5)
        strategy.sell.assert_called_once()

    def test_profit_target_exits_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, -1, 100.0)
        strategy.on_tick(_tick(90.0))  # 空头浮盈10 ≥ 10
        strategy.cover.assert_called_once()

    def test_stop_loss_exits_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, -1, 100.0)
        strategy.on_tick(_tick(106.0))  # 空头浮盈-6 ≤ -5
        strategy.cover.assert_called_once()

    def test_hold_within_band_no_exit(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, 1, 100.0)
        strategy.on_tick(_tick(103.0))  # 浮盈3：未达止盈、未触止损
        strategy.sell.assert_not_called()

    def test_breakeven_exit(self):
        # 武装保本后回落到保本位 → 离场（即便远未到固定止盈/止损）
        s = _make_strategy(
            {
                "profit_target": 100,
                "stop_loss": 100,
                "breakeven_trigger": 8,
                "breakeven_offset": 1,
            }
        )
        _fill_buffer(s, [100.0] * 20)
        _open(s, 1, 100.0)
        s.on_tick(_tick(110.0))  # 浮盈10 ≥ 8 → 武装；未达止盈100
        s.sell.assert_not_called()
        s.on_tick(_tick(100.5))  # 浮盈0.5 ≤ offset(1) → 保本离场
        s.sell.assert_called_once()


class TestForcedClose:
    def test_exit_time_closes_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, 1, 100.0)
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.sell.assert_called_once()

    def test_exit_time_closes_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        _open(strategy, -1, 100.0)
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.cover.assert_called_once()

    def test_exit_time_with_no_position_silent(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 0
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.sell.assert_not_called()
        strategy.cover.assert_not_called()
