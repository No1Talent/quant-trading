"""RiskGuard 单元测试：日内回撤 / 持仓 / 成交频次 / 熔断标志 / 并发。"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils.notifier import NullNotifier
from utils.risk_guard import RiskGuard, check_breach_flag


@pytest.fixture
def event_engine():
    ee = MagicMock()
    ee.register = MagicMock()
    ee.unregister = MagicMock()
    return ee


@pytest.fixture
def main_engine():
    me = MagicMock()
    me.cancel_all_active_orders = MagicMock()
    return me


@pytest.fixture
def tmp_flag(tmp_path):
    return tmp_path / "risk_breach.flag"


@pytest.fixture
def guard(main_engine, event_engine, tmp_flag):
    return RiskGuard(
        main_engine=main_engine,
        event_engine=event_engine,
        notifier=NullNotifier(),
        max_daily_loss_pct=0.05,
        max_position_per_symbol=3,
        max_trades_per_minute=5,
        breach_flag_path=tmp_flag,
    )


def _account_event(balance: float):
    return SimpleNamespace(data=SimpleNamespace(balance=balance, accountid="TEST"))


def _trade_event(
    vt_symbol: str, direction: str, offset: str, volume: int, ts: datetime | None = None
):
    return SimpleNamespace(
        data=SimpleNamespace(
            vt_symbol=vt_symbol,
            direction=SimpleNamespace(value=direction),
            offset=SimpleNamespace(value=offset),
            volume=volume,
            datetime=ts or datetime.now(),
        )
    )


class TestDailyLoss:
    def test_daily_baseline_locked_on_first_event(self, guard):
        guard.on_account(_account_event(100_000))
        assert guard.daily_start_balance == 100_000
        assert guard.tripped is False

    def test_loss_below_threshold_not_tripped(self, guard):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(96_000))  # -4% < 5%
        assert guard.tripped is False

    def test_loss_above_threshold_trips(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(94_000))  # -6%
        assert guard.tripped is True
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_zero_baseline_does_not_div_by_zero(self, guard):
        guard.on_account(_account_event(0))
        guard.on_account(_account_event(0))
        assert guard.tripped is False


class TestPositionLimit:
    def test_position_under_limit_not_tripped(self, guard):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        assert guard.tripped is False
        assert guard.position["rb2510.SHFE"] == 3

    def test_position_over_limit_trips(self, guard, main_engine):
        for _ in range(4):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        assert guard.tripped is True
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_close_reduces_position(self, guard):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 2))
        assert guard.position["rb2510.SHFE"] == 1


class TestTradeFrequency:
    def test_under_freq_not_tripped(self, guard):
        # 避开持仓上限：交替开平，净持仓最高 1
        for _ in range(2):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
            guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 1))
        # 4 笔 < 阈值 5
        assert guard.tripped is False

    def test_over_freq_trips(self, guard, main_engine):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
            guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 1))
        # 6 笔 > 阈值 5
        assert guard.tripped is True


class TestTrippedBehavior:
    def test_cancel_called_on_trip(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_subsequent_events_dont_recancel(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        guard.on_account(_account_event(85_000))
        guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 10))
        assert main_engine.cancel_all_active_orders.call_count == 1

    def test_cancel_fallback_to_legacy_api(self, event_engine, tmp_flag):
        legacy = MagicMock(spec=["cancel_all_orders"])
        legacy.cancel_all_orders = MagicMock()
        g = RiskGuard(
            main_engine=legacy,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_daily_loss_pct=0.05,
            max_position_per_symbol=3,
            max_trades_per_minute=5,
            breach_flag_path=tmp_flag,
        )
        g.on_account(_account_event(100_000))
        g.on_account(_account_event(90_000))
        legacy.cancel_all_orders.assert_called_once()


class TestBreachFlag:
    def test_flag_written_on_trip(self, guard, tmp_flag):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        assert tmp_flag.exists()

        loaded = check_breach_flag(tmp_flag)
        assert loaded is not None
        assert "tripped_at" in loaded
        assert "日内亏损" in loaded["reason"]

    def test_check_returns_none_when_no_flag(self, tmp_path):
        assert check_breach_flag(tmp_path / "nope.flag") is None

    def test_reset_removes_flag(self, guard, tmp_flag):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        assert tmp_flag.exists()
        guard.reset()
        assert not tmp_flag.exists()
        assert guard.tripped is False


class TestConcurrency:
    def test_concurrent_trade_events_dont_crash(self, guard):
        errors: list[str] = []

        def worker(i: int):
            try:
                for _ in range(20):
                    guard.on_trade(_trade_event(f"sym{i}.SHFE", "多", "开", 1))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestRegister:
    def test_register_called_on_init(self, event_engine, main_engine, tmp_flag):
        RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            breach_flag_path=tmp_flag,
        )
        # EVENT_TRADE + EVENT_ACCOUNT
        assert event_engine.register.call_count == 2

    def test_unregister(self, guard, event_engine):
        guard.unregister()
        assert event_engine.unregister.call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
