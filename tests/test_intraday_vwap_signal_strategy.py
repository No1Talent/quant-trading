"""IntradayVwapSignalStrategy: 均价线门控 + A/B/C 主信号 + ExitPolicy 离场。

用真实 ArrayManager + 确定性 bar 序列驱动真实信号计算（而非 mock am），
以小窗参数缩短 warmup。验证：方向门控、A/B/C 触发、持仓不重复开仓、
ExitPolicy 离场路由、on_trade 生命周期。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import BarData

from strategies.intraday_vwap_signal_strategy import IntradayVwapSignalStrategy

_BASE = datetime(2024, 1, 2, 9, 0)
_SMALL = {
    "trend_window": 5,
    "breakout_window": 3,
    "vol_ma_window": 3,
    "pivot_window": 2,
    "atr_window": 3,
}


def _make(setting: dict | None = None) -> IntradayVwapSignalStrategy:
    cfg = dict(_SMALL)
    if setting:
        cfg.update(setting)
    s = IntradayVwapSignalStrategy(MagicMock(), "TestVwap", "rb2510.SHFE", cfg)
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


@pytest.fixture
def strategy():
    return _make()


def _bar(i: int, close: float, high=None, low=None, volume: float = 100.0) -> BarData:
    return BarData(
        symbol="rb2510",
        exchange=Exchange.SHFE,
        datetime=_BASE + timedelta(minutes=i),
        interval=Interval.MINUTE,
        open_price=close,
        high_price=close + 0.2 if high is None else high,
        low_price=close - 0.2 if low is None else low,
        close_price=close,
        volume=volume,
        turnover=0.0,
        open_interest=0.0,
        gateway_name="TEST",
    )


def _trade(direction: Direction, offset: Offset, price: float = 100.0):
    return SimpleNamespace(direction=direction, offset=offset, price=price, volume=1)


def _warmup(strategy, bars: list[BarData]) -> None:
    for b in bars:
        strategy.on_bar(b)
    for m in (strategy.buy, strategy.sell, strategy.short, strategy.cover):
        m.reset_mock()


def _rising(n: int = 12) -> list[BarData]:
    return [_bar(i, 100.0 + 0.5 * i, volume=100.0) for i in range(n)]


def _falling(n: int = 12) -> list[BarData]:
    return [_bar(i, 110.0 - 0.5 * i, volume=100.0) for i in range(n)]


class TestEntry:
    def test_not_inited_no_orders(self, strategy):
        for b in _rising(6):  # < size(10) → not inited
            strategy.on_bar(b)
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_bullish_setup_opens_long(self, strategy):
        _warmup(strategy, _rising(12))
        # 趋势向上、价在 VWAP 上方、放量破前高 → 做多
        strategy.on_bar(_bar(12, 108.0, high=108.0, low=105.0, volume=800.0))
        strategy.buy.assert_called_once()
        strategy.short.assert_not_called()

    def test_bearish_setup_opens_short(self, strategy):
        _warmup(strategy, _falling(12))
        strategy.on_bar(_bar(12, 102.0, high=105.0, low=102.0, volume=800.0))
        strategy.short.assert_called_once()
        strategy.buy.assert_not_called()

    def test_flat_market_no_orders(self, strategy):
        # 全平：close==vwap==sma → 门控两侧都不成立
        for b in [_bar(i, 100.0, volume=100.0) for i in range(12)]:
            strategy.on_bar(b)
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_breakout_below_vwap_gated_out(self, strategy):
        # 放量破前高，但价在 VWAP 下方 → 多头门控拦截，不做多
        _warmup(strategy, _falling(12))
        # 一根放量但仍在均价线下方、且不创新低的 bar → 既非多(域不符)亦非空(未破低)
        strategy.on_bar(_bar(12, 104.6, high=104.8, low=104.4, volume=800.0))
        strategy.buy.assert_not_called()

    def test_no_entry_while_holding(self, strategy):
        _warmup(strategy, _rising(12))
        strategy.pos = 1
        strategy.on_trade(_trade(Direction.LONG, Offset.OPEN, 105.0))
        strategy.on_bar(_bar(12, 108.0, high=108.0, low=105.0, volume=800.0))
        strategy.buy.assert_not_called()


class TestExitRouting:
    def test_open_close_lifecycle(self, strategy):
        assert not strategy.exit_policy.active
        strategy.on_trade(_trade(Direction.LONG, Offset.OPEN, 105.0))
        assert strategy.exit_policy.active and strategy.exit_policy.direction == 1
        strategy.on_trade(_trade(Direction.LONG, Offset.CLOSE, 110.0))
        assert not strategy.exit_policy.active

    def test_vwap_stop_routes_to_sell(self, strategy):
        _warmup(strategy, _rising(12))  # VWAP ~ 102.x
        strategy.pos = 1
        strategy.on_trade(_trade(Direction.LONG, Offset.OPEN, 105.0))
        # 一根跌破均价线的 bar → 技术位止损 → 平多
        strategy.on_bar(_bar(12, 100.0, high=101.0, low=99.0, volume=100.0))
        strategy.sell.assert_called_once()

    def test_no_phantom_exit_without_fill(self, strategy):
        # 未经 on_trade（无成交）→ policy 不 active → 跌破均价线也不平仓
        _warmup(strategy, _rising(12))
        strategy.pos = 0
        strategy.on_bar(_bar(12, 100.0, high=101.0, low=99.0, volume=100.0))
        strategy.sell.assert_not_called()
        strategy.cover.assert_not_called()
