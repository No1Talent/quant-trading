"""H1.5 主力换月探测（OI 跃迁 + 跳空双确认）。

把原本散落在 ``strategies/carry_roll_strategy.py`` 与
``strategies/ma_cross_rollover_gated_strategy.py`` 里完全相同的一段判断收编为
单一事实源 —— 阈值不一致就会让两条 alpha 在边缘情形上偷偷分裂。

阈值默认值来自 ``research/h1_5_calendar_rollover.py``（``ag_continuous`` 14 年
样本 profile：|ΔOI|>20% 命中 81 天 ≈ 6/年×14 = 期望 84，与教科书"非交割月主力
切换"一致；|gap|>0.3% 过滤掉无价格台阶的 OI churn）。需要更激进/保守时由调用方
显式传 ``oi_pct_threshold`` / ``gap_floor_pct``。

API 设计要点
- 关键字调用强制 ``detect_rollover(prev_oi=..., prev_close=..., curr_oi=..., curr_open=...)``，
  避免历史上 4 个 float 顺序错位的隐患。
- 任一 prev 值缺失（<=0）→ ``RolloverDetection(False, 0)``。调用方第一根 bar
  自然 no-op，下一根才有可能命中。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_OI_PCT_THRESHOLD: float = 20.0
DEFAULT_GAP_FLOOR_PCT: float = 0.3


@dataclass(frozen=True)
class RolloverDetection:
    """单根 bar 的换月判定结果。"""

    is_rollover: bool
    gap_sign: int  # +1 / -1 / 0；非 rollover 时恒为 0


def detect_rollover(
    *,
    prev_oi: float,
    prev_close: float,
    curr_oi: float,
    curr_open: float,
    oi_pct_threshold: float = DEFAULT_OI_PCT_THRESHOLD,
    gap_floor_pct: float = DEFAULT_GAP_FLOOR_PCT,
) -> RolloverDetection:
    """H1.5 双确认：``|ΔOI%| > oi_pct_threshold AND |gap%| > gap_floor_pct``。"""
    if prev_oi <= 0 or prev_close <= 0:
        return RolloverDetection(False, 0)

    oi_pct = abs(curr_oi - prev_oi) / prev_oi * 100.0
    gap_pct = abs(curr_open - prev_close) / prev_close * 100.0

    if oi_pct > oi_pct_threshold and gap_pct > gap_floor_pct:
        gap_sign = 1 if curr_open > prev_close else -1
        return RolloverDetection(True, gap_sign)

    return RolloverDetection(False, 0)
