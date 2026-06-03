"""Timeframe contract on BaseCtaStrategy: bar_interval as the single source of
truth for LIVE bar generation + load_bar warmup, and the live-eligibility guard.

These lock in the fix for the silent drift where strategies were researched at
1h but the live BarGenerator (vn.py default) fed them 1-minute bars, and where
daily research strategies (carry_roll / ma_cross) could be loaded live and have
their daily-OI alpha re-evaluated every minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy_ctastrategy import BarGenerator

from strategies.boll_reversal_strategy import BollReversalStrategy
from strategies.carry_roll_strategy import CarryRollStrategy
from strategies.donchian_strategy import DonchianStrategy
from strategies.double_ma_strategy import DoubleMaStrategy
from strategies.intraday_tick_strategy import IntradayTickStrategy
from strategies.intraday_vwap_signal_strategy import IntradayVwapSignalStrategy
from strategies.ma_cross_rollover_gated_strategy import MaCrossRolloverGatedStrategy
from utils.strategy_base import (
    STR_TO_INTERVAL,
    BaseCtaStrategy,
    install_live_eligibility_guard,
    is_live_eligible,
)

ALL_STRATEGIES = [
    DoubleMaStrategy,
    DonchianStrategy,
    BollReversalStrategy,
    IntradayTickStrategy,
    CarryRollStrategy,
    MaCrossRolloverGatedStrategy,
    IntradayVwapSignalStrategy,
]

# Declared expectations — the contract this PR establishes.
EXPECTED = {
    "DoubleMaStrategy": ("1h", True, True),
    "DonchianStrategy": ("1h", True, True),
    "BollReversalStrategy": ("1h", True, True),
    "IntradayTickStrategy": ("1h", True, False),  # interval moot (tick-native)
    "CarryRollStrategy": ("1d", False, True),
    "MaCrossRolloverGatedStrategy": ("1d", False, True),
    "IntradayVwapSignalStrategy": ("1m", True, True),  # minute-chart design grain
}


def _make(cls):
    return cls(MagicMock(), "T", "rb2410.SHFE", {})


class TestDeclarations:
    @pytest.mark.parametrize("cls", ALL_STRATEGIES, ids=lambda c: c.__name__)
    def test_every_strategy_declares_valid_interval(self, cls):
        interval, live, uses_bg = EXPECTED[cls.__name__]
        assert cls.bar_interval == interval
        assert cls.bar_interval in STR_TO_INTERVAL
        assert cls.live_eligible is live
        assert cls.uses_bar_generator is uses_bg

    @pytest.mark.parametrize("cls", ALL_STRATEGIES, ids=lambda c: c.__name__)
    def test_resolved_interval_matches_declaration(self, cls):
        s = _make(cls)
        assert s.resolved_bar_interval == STR_TO_INTERVAL[cls.bar_interval]

    def test_bad_interval_fails_fast_on_construction(self):
        class _BadStrat(DoubleMaStrategy):
            bar_interval = "5x"  # not in STR_TO_INTERVAL

        with pytest.raises(ValueError, match="bar_interval"):
            _make(_BadStrat)


class TestBarGeneratorWiring:
    def test_hour_strategy_builds_hour_aggregating_bg(self):
        for cls in (DoubleMaStrategy, DonchianStrategy, BollReversalStrategy):
            s = _make(cls)
            assert isinstance(s.bg, BarGenerator)
            assert s.bg.interval == Interval.HOUR

    def test_daily_strategy_builds_daily_bg(self):
        s = _make(CarryRollStrategy)
        assert isinstance(s.bg, BarGenerator)
        assert s.bg.interval == Interval.DAILY  # constructed with daily_end, no raise

    def test_tick_native_strategy_has_no_bg(self):
        s = _make(IntradayTickStrategy)
        assert s.bg is None

    def test_minute_window1_uses_passthrough_bg(self):
        class _MinStrat(DoubleMaStrategy):
            bar_interval = "1m"

        s = _make(_MinStrat)
        # 1m + window=1 → plain BarGenerator(on_bar), interval defaults to MINUTE
        assert s.bg.interval == Interval.MINUTE

    def test_on_tick_forwards_to_bar_generator(self):
        s = _make(DoubleMaStrategy)
        s.bg = MagicMock()
        tick = SimpleNamespace(last_price=100.0)
        s.on_tick(tick)
        s.bg.update_tick.assert_called_once_with(tick)

    def test_hour_strategy_aggregates_minute_bars_to_hour_bars(self):
        """The crux: feeding 1-min bars (the LIVE tick→1min path) yields exactly
        one on_bar call per completed hour — not one per minute."""
        s = _make(DoubleMaStrategy)
        received: list[BarData] = []
        s.on_bar = received.append  # type: ignore[method-assign]
        s.bg = s._build_bar_generator()  # rebind bg to the recording on_bar

        base = datetime(2024, 1, 2, 9, 0)
        for i in range(120):  # 09:00..10:59 → two full hours
            dt = base + timedelta(minutes=i)
            bar = BarData(
                symbol="rb2410",
                exchange=Exchange.SHFE,
                datetime=dt,
                interval=Interval.MINUTE,
                gateway_name="t",
                open_price=100.0,
                high_price=101.0,
                low_price=99.0,
                close_price=100.0 + i,
                volume=1.0,
            )
            s._on_source_bar(bar)

        assert len(received) == 2  # one hour bar per completed hour, not 120


class TestLoadBarWarmup:
    def test_load_bar_defaults_interval_to_declared(self):
        s = _make(DoubleMaStrategy)
        s.cta_engine.load_bar = MagicMock(return_value=[])
        s.load_bar(10)
        # CtaTemplate.load_bar → cta_engine.load_bar(vt_symbol, days, interval, cb, use_db)
        call = s.cta_engine.load_bar.call_args
        assert call.args[1] == 10
        assert call.args[2] == Interval.HOUR  # not vn.py's MINUTE default

    def test_load_bar_explicit_interval_respected(self):
        s = _make(DoubleMaStrategy)
        s.cta_engine.load_bar = MagicMock(return_value=[])
        s.load_bar(5, Interval.DAILY)
        assert s.cta_engine.load_bar.call_args.args[2] == Interval.DAILY


class TestLiveEligibilityGuard:
    def test_is_live_eligible_reads_flag(self):
        assert is_live_eligible(DoubleMaStrategy) is True
        assert is_live_eligible(CarryRollStrategy) is False
        assert is_live_eligible(MaCrossRolloverGatedStrategy) is False

    def test_is_live_eligible_defaults_true_when_unset(self):
        class _Plain:
            pass

        assert is_live_eligible(_Plain) is True

    def _fake_engine(self):
        engine = SimpleNamespace()
        engine.classes = {
            "DoubleMaStrategy": DoubleMaStrategy,
            "CarryRollStrategy": CarryRollStrategy,
        }
        engine.added = []
        engine.add_strategy = lambda cn, sn, vt, st: engine.added.append(cn)
        return engine

    def test_guard_blocks_research_only_strategy(self):
        engine = self._fake_engine()
        install_live_eligibility_guard(engine)
        engine.add_strategy("CarryRollStrategy", "x", "rb2410.SHFE", {})
        assert engine.added == []  # rejected

    def test_guard_allows_live_strategy(self):
        engine = self._fake_engine()
        install_live_eligibility_guard(engine)
        engine.add_strategy("DoubleMaStrategy", "x", "rb2410.SHFE", {})
        assert engine.added == ["DoubleMaStrategy"]

    def test_guard_is_idempotent(self):
        engine = self._fake_engine()
        install_live_eligibility_guard(engine)
        wrapped_once = engine.add_strategy
        install_live_eligibility_guard(engine)  # second call must not double-wrap
        assert engine.add_strategy is wrapped_once

    def test_guard_passes_through_unknown_class_name(self):
        # Unknown class_name (typo / not loaded) is left to vn.py's own handling.
        engine = self._fake_engine()
        install_live_eligibility_guard(engine)
        engine.add_strategy("NotARealStrategy", "x", "rb2410.SHFE", {})
        assert engine.added == ["NotARealStrategy"]


def test_base_default_is_conservative():
    # Base defaults: any strategy that forgets to declare still gets a sane,
    # live-eligible 1h contract rather than silent 1-minute.
    assert BaseCtaStrategy.bar_interval == "1h"
    assert BaseCtaStrategy.live_eligible is True
    assert BaseCtaStrategy.uses_bar_generator is True
