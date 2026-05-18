"""IntradayTickStrategy: breakout entries, P&L exits, intraday forced close."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.intraday_tick_strategy import IntradayTickStrategy

_NORMAL_DT = datetime(2024, 1, 1, 10, 0, 0)
_EXIT_DT = datetime(2024, 1, 1, 14, 50, 0)


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = IntradayTickStrategy(engine, "TestIntraday", "rb2510.SHFE", {})
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


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


def _fill_buffer(strategy, prices: list[float]) -> None:
    """Feed ticks to saturate the price buffer without triggering entries (balanced volumes)."""
    for p in prices:
        strategy.on_tick(_tick(p))
    strategy.buy.reset_mock()
    strategy.sell.reset_mock()
    strategy.short.reset_mock()
    strategy.cover.reset_mock()


class TestIntradayTickStrategy:
    def test_buffer_not_full_no_orders(self, strategy):
        for i in range(10):  # price_window=20; only half filled
            strategy.on_tick(_tick(100.0 + i))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_breakout_long_with_buy_pressure(self, strategy):
        # buffer: prices 90–109, max=109
        _fill_buffer(strategy, [90.0 + i for i in range(20)])
        # bid/ask = 2.0 >= volume_ratio(1.5), price at the buffer max
        strategy.on_tick(_tick(109.0, bid_vol=200, ask_vol=100))
        strategy.buy.assert_called_once()

    def test_breakout_short_with_sell_pressure(self, strategy):
        # buffer: prices 90–109, min=90
        _fill_buffer(strategy, [90.0 + i for i in range(20)])
        # ask/bid = 2.0 >= volume_ratio(1.5), price at the buffer min
        strategy.on_tick(_tick(90.0, bid_vol=100, ask_vol=200))
        strategy.short.assert_called_once()

    def test_insufficient_buy_pressure_no_entry(self, strategy):
        _fill_buffer(strategy, [90.0 + i for i in range(20)])
        # at max price but balanced volumes → buy_pressure=1.0 < 1.5
        strategy.on_tick(_tick(109.0, bid_vol=100, ask_vol=100))
        strategy.buy.assert_not_called()

    def test_profit_target_exits_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 1
        strategy.entry_price = 100.0
        strategy.on_tick(_tick(110.0))  # profit=10 >= profit_target(10)
        strategy.sell.assert_called_once()

    def test_stop_loss_exits_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 1
        strategy.entry_price = 100.0
        strategy.on_tick(_tick(94.0))  # profit=-6 <= -stop_loss(-5)
        strategy.sell.assert_called_once()

    def test_profit_target_exits_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = -1
        strategy.entry_price = 100.0
        strategy.on_tick(_tick(90.0))  # profit=10 >= profit_target(10)
        strategy.cover.assert_called_once()

    def test_stop_loss_exits_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = -1
        strategy.entry_price = 100.0
        strategy.on_tick(_tick(106.0))  # profit=-6 <= -stop_loss(-5)
        strategy.cover.assert_called_once()

    def test_exit_time_closes_long(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 1
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.sell.assert_called_once()

    def test_exit_time_closes_short(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = -1
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.cover.assert_called_once()

    def test_exit_time_with_no_position_silent(self, strategy):
        _fill_buffer(strategy, [100.0] * 20)
        strategy.pos = 0
        strategy.on_tick(_tick(100.0, dt=_EXIT_DT))
        strategy.sell.assert_not_called()
        strategy.cover.assert_not_called()

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
