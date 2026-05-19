"""safe_callback decorator + safe_* order wrappers (RiskGuard pre-gate)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from utils.strategy_base import (
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class TestSafeCallback:
    def test_normal_execution_returns_value(self):
        class S:
            @safe_callback
            def on_bar(self, x):
                return x * 2

        assert S().on_bar(5) == 10

    def test_exception_invokes_write_log(self):
        class S:
            def __init__(self):
                self.write_log = MagicMock()

            @safe_callback
            def on_bar(self, x):
                raise ValueError("bad bar")

        s = S()
        assert s.on_bar(1) is None
        s.write_log.assert_called_once()
        msg = s.write_log.call_args[0][0]
        assert "on_bar" in msg
        assert "bad bar" in msg

    def test_exception_falls_back_to_module_logger_when_no_write_log(self):
        class S:
            @safe_callback
            def on_tick(self, x):
                raise RuntimeError("boom")

        with patch("utils.strategy_base.logger") as mock_logger:
            assert S().on_tick(1) is None
        mock_logger.error.assert_called_once()

    def test_wraps_preserves_function_name(self):
        class S:
            @safe_callback
            def on_bar(self, x):
                return x

        assert S.on_bar.__name__ == "on_bar"


class _FakeStrategy:
    """Minimal stand-in for CtaTemplate — captures buy/sell/short/cover calls."""

    def __init__(self, vt_symbol: str = "rb2510.SHFE"):
        self.vt_symbol = vt_symbol
        self.write_log = MagicMock()
        self.buy = MagicMock(return_value=["vt_orderid_1"])
        self.sell = MagicMock(return_value=["vt_orderid_2"])
        self.short = MagicMock(return_value=["vt_orderid_3"])
        self.cover = MagicMock(return_value=["vt_orderid_4"])


@pytest.fixture
def allowing_guard():
    g = MagicMock()
    g.check_order_pre = MagicMock(return_value=(True, "ok"))
    return g


@pytest.fixture
def rejecting_guard():
    g = MagicMock()
    g.check_order_pre = MagicMock(return_value=(False, "price_deviation_exceeded"))
    return g


class TestSafeOrderWrappers:
    def test_buy_passes_through_when_guard_allows(self, allowing_guard):
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=allowing_guard):
            result = safe_buy(strategy, 4100.0, 1)
        assert result == ["vt_orderid_1"]
        strategy.buy.assert_called_once_with(4100.0, 1)
        allowing_guard.check_order_pre.assert_called_once_with("rb2510.SHFE", "buy", 4100.0, 1)

    def test_buy_blocked_when_guard_rejects(self, rejecting_guard):
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=rejecting_guard):
            result = safe_buy(strategy, 8000.0, 1)
        assert result == []
        strategy.buy.assert_not_called()
        strategy.write_log.assert_called_once()
        msg = strategy.write_log.call_args[0][0]
        assert "RISK_GATE" in msg
        assert "buy" in msg
        assert "price_deviation_exceeded" in msg

    def test_short_passes_through_when_allowed(self, allowing_guard):
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=allowing_guard):
            safe_short(strategy, 4000.0, 2)
        strategy.short.assert_called_once_with(4000.0, 2)
        assert allowing_guard.check_order_pre.call_args[0][1] == "short"

    def test_sell_and_cover_propagate_method_name(self, allowing_guard):
        """每个 safe_* 包装把自己的方法名透传给 RiskGuard.check_order_pre 作 direction 标签。"""
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=allowing_guard):
            safe_sell(strategy, 4000.0, 1)
            safe_cover(strategy, 4000.0, 1)
        strategy.sell.assert_called_once()
        strategy.cover.assert_called_once()
        directions = [c.args[1] for c in allowing_guard.check_order_pre.call_args_list]
        assert directions == ["sell", "cover"]

    def test_no_guard_in_backtest_falls_through(self):
        """RiskGuard 未挂载（回测）时不阻塞，透传到 strategy.buy()。"""
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=None):
            result = safe_buy(strategy, 4100.0, 1)
        assert result == ["vt_orderid_1"]
        strategy.buy.assert_called_once_with(4100.0, 1)

    def test_extra_kwargs_forwarded(self, allowing_guard):
        """stop=True / lock=True 这类 CtaTemplate 关键字参数原样转发。"""
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=allowing_guard):
            safe_buy(strategy, 4100.0, 1, stop=True, lock=False)
        strategy.buy.assert_called_once_with(4100.0, 1, stop=True, lock=False)
