"""DonchianStrategy signal logic: N-bar breakout entry, M-bar opposite exit, same-bar reversal."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from strategies.donchian_strategy import DonchianStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = DonchianStrategy(engine, "TestDonchian", "rb2510.SHFE", {})
    s.am = MagicMock()
    s.am.inited = True
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


def _bar(close: float) -> SimpleNamespace:
    return SimpleNamespace(close_price=close)


def _configure_arrays(
    strategy,
    entry_window: int = 20,
    exit_window: int = 10,
    *,
    entry_high: float,
    entry_low: float,
    exit_high: float,
    exit_low: float,
) -> None:
    """构造 am.high_array/low_array，使得指定切片的 max/min 等于参数指定值。

    策略读切片 `[-entry_window-1 : -1]`，于是数组结构必须足够长。把所需窗口内
    的最高/最低锚定到中间位置即可。
    """
    n = max(entry_window, exit_window) + 5
    high = np.full(n, 50.0)
    low = np.full(n, 50.0)
    # entry_window 切片：[-21:-1] 即最后 20 个但排除最末位
    high[-entry_window - 1 : -1] = entry_high
    low[-entry_window - 1 : -1] = entry_low
    # exit_window 切片：[-11:-1]
    high[-exit_window - 1 : -1] = exit_high
    low[-exit_window - 1 : -1] = exit_low
    strategy.am.high_array = high
    strategy.am.low_array = low


class TestNotInited:
    def test_not_inited_no_orders(self, strategy):
        strategy.am.inited = False
        strategy.on_bar(_bar(100.0))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()


class TestEntryFromFlat:
    def test_break_entry_high_opens_long(self, strategy):
        _configure_arrays(strategy, entry_high=100.0, entry_low=90.0, exit_high=98.0, exit_low=92.0)
        strategy.pos = 0
        strategy.on_bar(_bar(101.0))  # > entry_up=100
        strategy.buy.assert_called_once()
        strategy.short.assert_not_called()

    def test_break_entry_low_opens_short(self, strategy):
        _configure_arrays(strategy, entry_high=100.0, entry_low=90.0, exit_high=98.0, exit_low=92.0)
        strategy.pos = 0
        strategy.on_bar(_bar(89.0))  # < entry_dn=90
        strategy.short.assert_called_once()
        strategy.buy.assert_not_called()

    def test_no_break_no_entry(self, strategy):
        _configure_arrays(strategy, entry_high=100.0, entry_low=90.0, exit_high=98.0, exit_low=92.0)
        strategy.pos = 0
        strategy.on_bar(_bar(95.0))  # inside the channel
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()


class TestPlainExit:
    def test_long_exits_when_break_exit_low(self, strategy):
        _configure_arrays(strategy, entry_high=100.0, entry_low=80.0, exit_high=98.0, exit_low=92.0)
        strategy.pos = 1
        # 价格 91 < exit_dn=92 → 平多；但 91 > entry_dn=80 → 不反手开空
        strategy.on_bar(_bar(91.0))
        strategy.sell.assert_called_once()
        strategy.short.assert_not_called()

    def test_short_exits_when_break_exit_high(self, strategy):
        _configure_arrays(strategy, entry_high=120.0, entry_low=90.0, exit_high=98.0, exit_low=92.0)
        strategy.pos = -1
        # 价格 99 > exit_up=98 → 平空；但 99 < entry_up=120 → 不反手开多
        strategy.on_bar(_bar(99.0))
        strategy.cover.assert_called_once()
        strategy.buy.assert_not_called()


class TestSameBarReversal:
    """同根 K 线既触发出场又触发反向入场——关键路径，advisor 点名要求覆盖。"""

    def test_long_to_short_reversal_in_one_bar(self, strategy):
        _configure_arrays(
            strategy, entry_high=110.0, entry_low=95.0, exit_high=108.0, exit_low=96.0
        )
        strategy.pos = 1
        # 价格 94 同时满足 < exit_dn=96 (平多) 和 < entry_dn=95 (开空)
        strategy.on_bar(_bar(94.0))
        strategy.sell.assert_called_once()
        strategy.short.assert_called_once()
        # 平的数量 = abs(pos)，开新仓数量 = fixed_size；都用收盘价
        assert strategy.sell.call_args.args == (94.0, 1)
        assert strategy.short.call_args.args == (94.0, strategy.fixed_size)

    def test_short_to_long_reversal_in_one_bar(self, strategy):
        _configure_arrays(
            strategy, entry_high=105.0, entry_low=80.0, exit_high=100.0, exit_low=82.0
        )
        strategy.pos = -1
        # 价格 106 同时 > exit_up=100 (平空) 和 > entry_up=105 (开多)
        strategy.on_bar(_bar(106.0))
        strategy.cover.assert_called_once()
        strategy.buy.assert_called_once()

    def test_exit_without_reverse_when_only_inner_break(self, strategy):
        """价格只穿过 exit 通道但未穿过更宽的 entry 通道：只平不反手。"""
        _configure_arrays(
            strategy, entry_high=110.0, entry_low=85.0, exit_high=108.0, exit_low=96.0
        )
        strategy.pos = 1
        strategy.on_bar(_bar(95.0))  # < exit_dn=96 but > entry_dn=85
        strategy.sell.assert_called_once()
        strategy.short.assert_not_called()

    def test_holding_long_with_price_inside_channel_no_action(self, strategy):
        _configure_arrays(
            strategy, entry_high=110.0, entry_low=90.0, exit_high=108.0, exit_low=92.0
        )
        strategy.pos = 1
        strategy.on_bar(_bar(100.0))  # 通道内
        strategy.sell.assert_not_called()
        strategy.buy.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
