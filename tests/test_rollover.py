"""Tests for utils.rollover — H1.5 主力换月探测单元测试。

注：覆盖入口、缺失先验、单条件、双条件、边界等价类。
"""

from __future__ import annotations

import pytest

from utils.rollover import (
    DEFAULT_GAP_FLOOR_PCT,
    DEFAULT_OI_PCT_THRESHOLD,
    RolloverDetection,
    detect_rollover,
)


class TestDetectRollover:
    def test_no_prior_state_returns_no_rollover(self) -> None:
        # 第一根 bar 没有上一根 OI/close，调用方必须能拿到 no-op
        det = detect_rollover(prev_oi=0, prev_close=0, curr_oi=100000, curr_open=3000)
        assert det == RolloverDetection(False, 0)

    def test_only_oi_jump_not_enough(self) -> None:
        # OI 跳了 50% 但价格几乎没动 → 不算 rollover（典型 OI churn）
        det = detect_rollover(prev_oi=100, prev_close=3000, curr_oi=150, curr_open=3001)
        assert det.is_rollover is False
        assert det.gap_sign == 0

    def test_only_gap_not_enough(self) -> None:
        # 大跳空但 OI 没动 → 通常是宏观消息（NFP/CPI），不是换月
        det = detect_rollover(prev_oi=100, prev_close=3000, curr_oi=101, curr_open=3100)
        assert det.is_rollover is False
        assert det.gap_sign == 0

    def test_both_thresholds_crossed_long_gap(self) -> None:
        # OI 50% + 价格 +3.3% → rollover，gap_sign=+1
        det = detect_rollover(prev_oi=100, prev_close=3000, curr_oi=150, curr_open=3100)
        assert det.is_rollover is True
        assert det.gap_sign == 1

    def test_both_thresholds_crossed_short_gap(self) -> None:
        det = detect_rollover(prev_oi=100, prev_close=3000, curr_oi=150, curr_open=2900)
        assert det.is_rollover is True
        assert det.gap_sign == -1

    def test_oi_drop_also_counts(self) -> None:
        # |ΔOI| — 老主力被切换走时 OI 会暴跌；同样属于换月
        det = detect_rollover(prev_oi=200, prev_close=3000, curr_oi=100, curr_open=3050)
        assert det.is_rollover is True
        assert det.gap_sign == 1

    def test_exact_threshold_not_strict_above(self) -> None:
        # `oi_pct > threshold`（严格大于）。OI 涨幅恰好 = 阈值 → 不算
        det = detect_rollover(
            prev_oi=100,
            prev_close=3000,
            curr_oi=120,
            curr_open=3010,
            oi_pct_threshold=20.0,
            gap_floor_pct=0.3,
        )
        # 20.0 == 20.0 → 不严格大于，不命中
        assert det.is_rollover is False

    def test_custom_thresholds_loosen(self) -> None:
        # 默认阈值不命中的情形，调低阈值后命中
        baseline = detect_rollover(prev_oi=100, prev_close=3000, curr_oi=115, curr_open=3005)
        assert baseline.is_rollover is False

        loosened = detect_rollover(
            prev_oi=100,
            prev_close=3000,
            curr_oi=115,
            curr_open=3005,
            oi_pct_threshold=10.0,
            gap_floor_pct=0.1,
        )
        assert loosened.is_rollover is True
        assert loosened.gap_sign == 1

    def test_negative_prev_close_treated_as_missing(self) -> None:
        # 数据错误兜底 — 任何非正数都视作缺失
        det = detect_rollover(prev_oi=100, prev_close=-3000, curr_oi=200, curr_open=3100)
        assert det == RolloverDetection(False, 0)

    def test_defaults_match_h15_research(self) -> None:
        # 阈值默认值是 H1.5 研究脚本固化的物理常数，别在重构里偷偷漂移
        assert DEFAULT_OI_PCT_THRESHOLD == 20.0
        assert DEFAULT_GAP_FLOOR_PCT == 0.3

    def test_keyword_only_signature(self) -> None:
        # 4 个 float 顺序最容易写错，强制 kwargs
        with pytest.raises(TypeError):
            detect_rollover(100, 3000, 150, 3100)  # type: ignore[misc]
