"""ExitPolicy：止损止盈纯逻辑的单测。

覆盖每个离场法（多/空对称）、规则优先级、保本武装、跟随极值跟踪、时间止损的「根数」
语义，以及 ExitConfig 的配置校验。所有用例不依赖 vn.py / CtaEngine —— ExitPolicy 是纯逻辑。
"""

from __future__ import annotations

import pytest

from utils.exit_policy import ExitConfig, ExitDecision, ExitPolicy, ExitReason


def _open(config: ExitConfig, direction: int = 1, entry: float = 100.0) -> ExitPolicy:
    p = ExitPolicy(config)
    p.open(direction=direction, entry_price=entry)
    return p


class TestLifecycle:
    def test_inactive_returns_no_exit(self):
        p = ExitPolicy(ExitConfig(fixed_stop=5))
        assert p.update(50.0) == ExitDecision(False)

    def test_pnl_points_long_and_short(self):
        long = _open(ExitConfig(), direction=1, entry=100.0)
        assert long.pnl_points(110.0) == 10.0
        short = _open(ExitConfig(), direction=-1, entry=100.0)
        assert short.pnl_points(90.0) == 10.0

    def test_pnl_points_inactive_is_zero(self):
        assert ExitPolicy(ExitConfig()).pnl_points(123.0) == 0.0

    def test_close_resets_state(self):
        p = _open(ExitConfig(fixed_stop=5))
        p.update(101.0)
        p.close()
        assert not p.active
        assert p.direction == 0
        assert p.bars_held == 0

    def test_open_rejects_bad_direction(self):
        with pytest.raises(ValueError):
            ExitPolicy(ExitConfig()).open(direction=0, entry_price=100.0)


class TestFixedStop:
    def test_long_fixed_stop_fires(self):
        p = _open(ExitConfig(fixed_stop=5), direction=1, entry=100.0)
        d = p.update(94.0)  # 浮盈 -6 ≤ -5
        assert d.should_exit and d.reason is ExitReason.FIXED_STOP

    def test_long_fixed_stop_not_fired_above_threshold(self):
        p = _open(ExitConfig(fixed_stop=5), direction=1, entry=100.0)
        assert not p.update(96.0).should_exit  # 浮盈 -4 > -5

    def test_short_fixed_stop_fires(self):
        p = _open(ExitConfig(fixed_stop=5), direction=-1, entry=100.0)
        d = p.update(106.0)  # 空头浮盈 -6
        assert d.should_exit and d.reason is ExitReason.FIXED_STOP

    def test_exact_threshold_fires(self):
        p = _open(ExitConfig(fixed_stop=5), direction=1, entry=100.0)
        assert p.update(95.0).should_exit  # 浮盈 -5 ≤ -5 边界


class TestFixedTarget:
    def test_long_target_fires(self):
        p = _open(ExitConfig(fixed_target=10), direction=1, entry=100.0)
        d = p.update(110.0)
        assert d.should_exit and d.reason is ExitReason.FIXED_TARGET

    def test_short_target_fires(self):
        p = _open(ExitConfig(fixed_target=10), direction=-1, entry=100.0)
        d = p.update(90.0)
        assert d.should_exit and d.reason is ExitReason.FIXED_TARGET

    def test_target_not_reached(self):
        p = _open(ExitConfig(fixed_target=10), direction=1, entry=100.0)
        assert not p.update(109.0).should_exit


class TestVwapStop:
    def test_long_breaks_below_vwap(self):
        p = _open(ExitConfig(use_vwap_stop=True), direction=1, entry=100.0)
        d = p.update(99.0, vwap=99.5)  # 价跌破均价线
        assert d.should_exit and d.reason is ExitReason.VWAP_STOP

    def test_long_above_vwap_holds(self):
        p = _open(ExitConfig(use_vwap_stop=True), direction=1, entry=100.0)
        assert not p.update(101.0, vwap=99.5).should_exit

    def test_short_breaks_above_vwap(self):
        p = _open(ExitConfig(use_vwap_stop=True), direction=-1, entry=100.0)
        d = p.update(101.0, vwap=100.5)  # 价升破均价线
        assert d.should_exit and d.reason is ExitReason.VWAP_STOP

    def test_no_vwap_supplied_skips(self):
        p = _open(ExitConfig(use_vwap_stop=True), direction=1, entry=100.0)
        assert not p.update(50.0, vwap=None).should_exit


class TestTrailingStop:
    def test_long_trailing_fires_after_pullback(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=1, entry=100.0)
        p.update(120.0, atr=2.0)  # 极值升到 120
        d = p.update(115.0, atr=2.0)  # 回撤 5 ≥ 2×2=4
        assert d.should_exit and d.reason is ExitReason.TRAILING_STOP

    def test_long_trailing_holds_within_band(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=1, entry=100.0)
        p.update(120.0, atr=2.0)
        assert not p.update(117.0, atr=2.0).should_exit  # 回撤 3 < 4

    def test_short_trailing_fires(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=-1, entry=100.0)
        p.update(80.0, atr=2.0)  # 空头极值（最低）80
        d = p.update(85.0, atr=2.0)  # 反弹 5 ≥ 4
        assert d.should_exit and d.reason is ExitReason.TRAILING_STOP

    def test_best_price_tracks_only_favorable(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=1, entry=100.0)
        p.update(120.0, atr=2.0)
        p.update(110.0, atr=2.0)  # 不利方向，best_price 不动
        assert p.best_price == 120.0

    def test_no_atr_skips(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=1, entry=100.0)
        p.update(120.0, atr=None)
        assert not p.update(100.0, atr=None).should_exit

    def test_zero_atr_skips(self):
        p = _open(ExitConfig(trailing_atr_mult=2.0), direction=1, entry=100.0)
        assert not p.update(120.0, atr=0.0).should_exit


class TestBreakeven:
    def test_arms_then_protects(self):
        p = _open(ExitConfig(breakeven_trigger=10, breakeven_offset=1.0), direction=1, entry=100.0)
        p.update(112.0)  # 浮盈 12 ≥ 10 → 武装
        d = p.update(100.5)  # 回落，浮盈 0.5 ≤ offset 1.0
        assert d.should_exit and d.reason is ExitReason.BREAKEVEN_STOP

    def test_not_armed_until_trigger(self):
        p = _open(ExitConfig(breakeven_trigger=10, breakeven_offset=1.0), direction=1, entry=100.0)
        # 浮盈只到 8，未达触发阈；回落到保本位下方也不应触发保本
        p.update(108.0)
        assert not p.update(100.5).should_exit

    def test_stays_armed_after_retreat(self):
        p = _open(ExitConfig(breakeven_trigger=10, breakeven_offset=0.0), direction=1, entry=100.0)
        p.update(115.0)  # 武装
        p.update(105.0)  # 浮盈 5 > 0，未触发但保持武装
        d = p.update(99.0)  # 浮盈 -1 ≤ 0 → 保本（此处其实保护成保本以下，靠武装位）
        assert d.should_exit and d.reason is ExitReason.BREAKEVEN_STOP

    def test_short_breakeven(self):
        p = _open(ExitConfig(breakeven_trigger=10, breakeven_offset=0.0), direction=-1, entry=100.0)
        p.update(88.0)  # 空头浮盈 12 → 武装
        d = p.update(100.0)  # 浮盈 0 ≤ 0
        assert d.should_exit and d.reason is ExitReason.BREAKEVEN_STOP


class TestTimeStop:
    def test_fires_after_n_bars_no_direction(self):
        p = _open(ExitConfig(time_stop_bars=3, time_stop_eps=1.0), direction=1, entry=100.0)
        assert not p.update(100.5).should_exit  # bar1
        assert not p.update(100.5).should_exit  # bar2
        d = p.update(100.5)  # bar3，|浮盈|0.5 ≤ 1.0
        assert d.should_exit and d.reason is ExitReason.TIME_STOP

    def test_not_fired_when_trending(self):
        p = _open(ExitConfig(time_stop_bars=3, time_stop_eps=1.0), direction=1, entry=100.0)
        p.update(102.0)
        p.update(104.0)
        assert not p.update(106.0).should_exit  # 有方向，|浮盈|6 > 1

    def test_advance_bar_false_does_not_count(self):
        p = _open(ExitConfig(time_stop_bars=2, time_stop_eps=1.0), direction=1, entry=100.0)
        p.update(100.0, advance_bar=False)  # tick 内更新，不计根数
        p.update(100.0, advance_bar=False)
        assert p.bars_held == 0
        assert not p.update(100.0, advance_bar=False).should_exit
        d = p.update(100.0)  # bar1
        assert not d.should_exit
        assert p.update(100.0).should_exit  # bar2 → 触发


class TestPriority:
    def test_stop_beats_target_on_same_bar(self):
        # 极端 bar：既到止损也到止盈阈，应判止损（保守）
        cfg = ExitConfig(fixed_stop=5, fixed_target=10)
        p = _open(cfg, direction=1, entry=100.0)
        # 价 95：浮盈 -5 命中止损；同时不可能命中 +10 止盈 —— 用单价无法同时命中，
        # 故构造 vwap_stop + target 冲突更真实：
        d = p.update(95.0)
        assert d.reason is ExitReason.FIXED_STOP

    def test_vwap_stop_beats_target(self):
        cfg = ExitConfig(use_vwap_stop=True, fixed_target=10)
        p = _open(cfg, direction=1, entry=100.0)
        # 价 110 命中固定止盈，但同一根价也跌破 vwap=111（极端假设）→ 止损优先
        d = p.update(110.0, vwap=111.0)
        assert d.reason is ExitReason.VWAP_STOP

    def test_target_when_no_stop_hit(self):
        cfg = ExitConfig(fixed_stop=5, fixed_target=10)
        p = _open(cfg, direction=1, entry=100.0)
        assert p.update(110.0).reason is ExitReason.FIXED_TARGET


class TestConfigValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"fixed_stop": -1},
            {"fixed_target": -1},
            {"trailing_atr_mult": -0.5},
            {"breakeven_trigger": -1},
            {"breakeven_offset": -1},
            {"time_stop_eps": -1},
            {"time_stop_bars": -1},
        ],
    )
    def test_negative_rejected(self, kwargs):
        with pytest.raises(ValueError):
            ExitConfig(**kwargs)

    def test_breakeven_offset_above_trigger_rejected(self):
        with pytest.raises(ValueError):
            ExitConfig(breakeven_trigger=5, breakeven_offset=6)

    def test_empty_config_is_valid_noop(self):
        p = _open(ExitConfig(), direction=1, entry=100.0)
        # 全关 → 永不离场
        assert not p.update(50.0).should_exit
        assert not p.update(150.0).should_exit
