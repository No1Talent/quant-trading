"""BollReversalStrategy signal logic: band-fade entry, ATR stop, post-stop cooldown interlock."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.boll_reversal_strategy import BollReversalStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    # 默认开启 ATR 止损 + 冷却以覆盖互锁路径；不写入 setting 字段是因为 vn.py
    # CtaTemplate.__init__ 接受 setting={} 即用类属性默认值。
    s = BollReversalStrategy(engine, "TestBoll", "rb2510.SHFE", {})
    s.sl_atr_mult = 2.0
    s.cooldown_bars = 3
    s.am = MagicMock()
    s.am.inited = True
    # 默认 sma / atr 返回；具体测试里再覆盖
    s.am.boll.return_value = (110.0, 90.0)  # (up, down)
    s.am.sma.return_value = 100.0
    s.am.atr.return_value = 5.0
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


def _bar(close: float) -> SimpleNamespace:
    return SimpleNamespace(close_price=close)


class TestEntry:
    def test_not_inited_no_orders(self, strategy):
        strategy.am.inited = False
        strategy.on_bar(_bar(120.0))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_break_upper_band_fades_short(self, strategy):
        strategy.pos = 0
        strategy.on_bar(_bar(115.0))  # > up=110
        strategy.short.assert_called_once()
        assert strategy.entry_price == 115.0

    def test_break_lower_band_fades_long(self, strategy):
        strategy.pos = 0
        strategy.on_bar(_bar(85.0))  # < down=90
        strategy.buy.assert_called_once()
        assert strategy.entry_price == 85.0

    def test_within_band_no_entry(self, strategy):
        strategy.pos = 0
        strategy.on_bar(_bar(100.0))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()


class TestMeanRevertExit:
    def test_long_exits_when_price_above_sma(self, strategy):
        strategy.pos = 1
        strategy.entry_price = 85.0
        strategy.on_bar(_bar(101.0))  # > sma=100
        strategy.sell.assert_called_once()
        assert strategy.entry_price == 0.0

    def test_short_exits_when_price_below_sma(self, strategy):
        strategy.pos = -1
        strategy.entry_price = 115.0
        strategy.on_bar(_bar(99.0))  # < sma=100
        strategy.cover.assert_called_once()
        assert strategy.entry_price == 0.0


class TestAtrStopInterlock:
    """止损 → 冷却激活 → 冷却期内禁止入场——这是 v2 最复杂的路径，必须显式覆盖。"""

    def test_long_atr_stop_triggers_and_sets_cooldown(self, strategy):
        strategy.pos = 1
        strategy.entry_price = 100.0
        # 止损线 = 100 - 2 * 5 = 90；价格 89 触发止损
        strategy.on_bar(_bar(89.0))
        strategy.sell.assert_called_once()
        assert strategy.entry_price == 0.0
        assert strategy.cooldown_remaining == strategy.cooldown_bars

    def test_short_atr_stop_triggers_and_sets_cooldown(self, strategy):
        strategy.pos = -1
        strategy.entry_price = 100.0
        # 止损线 = 100 + 10 = 110；价格 111 触发
        strategy.on_bar(_bar(111.0))
        strategy.cover.assert_called_once()
        assert strategy.entry_price == 0.0
        assert strategy.cooldown_remaining == strategy.cooldown_bars

    def test_cooldown_blocks_new_entry_even_on_band_break(self, strategy):
        """止损后立刻又遇到突破信号，冷却期内不应入场——核心互锁。"""
        strategy.pos = 0
        strategy.cooldown_remaining = 3
        strategy.on_bar(_bar(115.0))  # 突破上轨
        strategy.short.assert_not_called()
        strategy.buy.assert_not_called()
        # 冷却计数应已减 1
        assert strategy.cooldown_remaining == 2

    def test_cooldown_decrements_then_allows_entry(self, strategy):
        """语义：先减 1 再检查 `> 0`。所以 cd=2 时第 1 根仍阻止，第 2 根恰好放行。"""
        strategy.pos = 0
        strategy.cooldown_remaining = 2
        # 第 1 根 bar：cd 2 → 1，仍 > 0，阻止
        strategy.on_bar(_bar(115.0))
        strategy.short.assert_not_called()
        assert strategy.cooldown_remaining == 1
        # 第 2 根 bar：cd 1 → 0，放行
        strategy.on_bar(_bar(115.0))
        strategy.short.assert_called_once()

    def test_stop_takes_priority_over_mean_revert_exit(self, strategy):
        """同根 bar 既触发 ATR 止损又跨越 SMA 中线，应走止损路径（带冷却）。"""
        strategy.pos = 1
        strategy.entry_price = 100.0
        # 价格 88 同时满足 < 止损线 90 和 < sma 100；止损优先
        strategy.on_bar(_bar(88.0))
        strategy.sell.assert_called_once()
        assert strategy.cooldown_remaining == strategy.cooldown_bars

    def test_atr_stop_disabled_when_sl_mult_zero(self, strategy):
        """sl_atr_mult=0 是 v1 兼容路径，止损必须不触发，冷却也不应被设置。"""
        strategy.sl_atr_mult = 0.0
        strategy.pos = 1
        strategy.entry_price = 100.0
        strategy.on_bar(_bar(89.0))  # 即使大幅亏损也不止损
        strategy.sell.assert_not_called()
        assert strategy.cooldown_remaining == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
