"""离场策略模块 ExitPolicy：把「剑客训练营·止损止盈的理念与方法」抽成可配置、可复用、可单测的纯逻辑。

设计原则
--------
- **纯逻辑、不发单**：本模块不 import vn.py、不调 safe_*。策略每根 bar/tick 把
  ``(price, vwap, atr)`` 喂进 ``update()``，拿回一个 :class:`ExitDecision`，再自己走
  ``safe_sell`` / ``safe_cover`` 平仓。离场决策与下单解耦 —— 这样所有日内/波段策略都能
  复用同一套离场纪律，且单测不需要 ``CtaEngine`` / ``MainEngine``。
- **单仓模型**：一个实例只管一笔持仓（``direction`` + ``entry_price``）。多腿/对冲由策略层
  组合多个实例。v1 不内建「分批止盈」（讲义第 6 法），留作 follow-up。
- **规则优先级**（同一根 bar 多条命中时的裁决顺序，止损优先 —— bar 内价格路径未知，按最不利处理）：
  ``定额止损 → 技术位止损(穿均价线) → 底线保本 → 时间止损 → 固定止盈 → 跟随止盈``。

覆盖讲义《止损止盈的理念与方法》
--------------------------------
止损 5 法：

============  ========================  ===============================
讲义          ExitConfig 字段           说明
============  ========================  ===============================
定额止损      ``fixed_stop``            亏损达 N 点
技术位止损    ``use_vwap_stop``         跌破(多)/升破(空) 均价线
时间止损      ``time_stop_bars`` +eps   进场后 N 根仍无方向
空间止损      —                         v2（与时间止损同源，留待）
无条件止损    —                         极端行情对接 RiskGuard，不在本模块
============  ========================  ===============================

止盈 6 法：

============  ===========================  ===============================
讲义          ExitConfig 字段              说明
============  ===========================  ===============================
固定止盈      ``fixed_target``             盈利达 N 点
技术止盈      —                            穿均价线由策略侧给信号
跟随止盈      ``trailing_atr_mult``        自极值回撤 mult×ATR
时间止盈      ``time_stop_bars``           与时间止损复用
底线/保本     ``breakeven_trigger`` +off   浮盈达阈后锁保本
分批止盈      —                            v2，组合多实例实现
============  ===========================  ===============================

约定：所有「点数」单位与 ``price`` 一致（价格点，非金额）；盈亏均以**有利方向为正**
（多头浮盈 = price-entry，空头浮盈 = entry-price），见 :meth:`ExitPolicy.pnl_points`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExitReason(str, Enum):
    """离场原因。继承 ``str`` 便于直接落 JSONL / 写日志，无需 ``.value``。"""

    NONE = "none"
    FIXED_STOP = "fixed_stop"  # 定额止损
    VWAP_STOP = "vwap_stop"  # 技术位止损：穿均价线
    BREAKEVEN_STOP = "breakeven_stop"  # 底线保本
    TIME_STOP = "time_stop"  # 时间止损/止盈：横盘无方向
    FIXED_TARGET = "fixed_target"  # 固定止盈
    TRAILING_STOP = "trailing_stop"  # 跟随移动止盈


@dataclass(frozen=True)
class ExitDecision:
    """``update()`` 的返回：是否离场 + 原因 + 人读注解。

    ``frozen`` 保证决策对象不可变，策略侧可安全持有/记录。``should_exit`` 为 ``False``
    时 ``reason`` 恒为 ``NONE``。
    """

    should_exit: bool
    reason: ExitReason = ExitReason.NONE
    note: str = ""


@dataclass
class ExitConfig:
    """各离场法的参数。``None`` = 关闭该法。点数单位与价格一致。

    设计成「字段即开关」：只填想用的法，其余留 ``None``。例如最常见的
    `定额止损 + 固定止盈`：``ExitConfig(fixed_stop=5, fixed_target=10)``。
    """

    fixed_stop: float | None = None  # 定额止损：亏损达此点数离场
    fixed_target: float | None = None  # 固定止盈：盈利达此点数离场
    trailing_atr_mult: float | None = None  # 跟随止盈：自极值回撤 mult×ATR
    use_vwap_stop: bool = False  # 技术位止损：跌破(多)/升破(空) 均价线
    breakeven_trigger: float | None = None  # 保本触发：浮盈达此点数后武装保本
    breakeven_offset: float = 0.0  # 保本位锁定点数（>0 锁手续费，=0 纯保本）
    time_stop_bars: int | None = None  # 时间止损：持仓达 N 根后
    time_stop_eps: float = 0.0  # 「无方向」判据：|浮盈| ≤ eps 才触发时间止损

    def __post_init__(self) -> None:
        non_negative = {
            "fixed_stop": self.fixed_stop,
            "fixed_target": self.fixed_target,
            "trailing_atr_mult": self.trailing_atr_mult,
            "breakeven_trigger": self.breakeven_trigger,
            "breakeven_offset": self.breakeven_offset,
            "time_stop_eps": self.time_stop_eps,
        }
        for name, value in non_negative.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} 不能为负: {value}")
        if self.time_stop_bars is not None and self.time_stop_bars < 0:
            raise ValueError(f"time_stop_bars 不能为负: {self.time_stop_bars}")
        if self.breakeven_trigger is not None and self.breakeven_offset > self.breakeven_trigger:
            # 保本锁定点数超过触发阈值 → 一武装即触发，逻辑无意义，拦在配置层
            raise ValueError(
                f"breakeven_offset({self.breakeven_offset}) 不应 > "
                f"breakeven_trigger({self.breakeven_trigger})"
            )


class ExitPolicy:
    """单仓离场决策器。``open()`` 建仓 → 每根 bar/tick ``update()`` → 成交平仓后 ``close()``。

    生命周期约定::

        policy = ExitPolicy(ExitConfig(fixed_stop=5, fixed_target=10))
        policy.open(direction=1, entry_price=100.0)   # 开多
        decision = policy.update(price=110.0, vwap=105.0, atr=2.0)
        if decision.should_exit:
            safe_sell(self, bid_price, abs(self.pos))   # 策略侧真正发单
            # 成交确认后（on_trade 里 pos 归零）再：
            policy.close()

    **不在决策命中时自动 reset**：因发单是异步的，``update()`` 返回 should_exit 后持仓未必
    立刻归零；保持 active 让下一根 bar 仍能重发平仓，直到策略确认成交调用 ``close()``。
    """

    def __init__(self, config: ExitConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        """清空持仓状态，回到「空仓」。``__init__`` 与 ``close()`` 均调用。"""
        self.active: bool = False
        self.direction: int = 0  # +1 多, -1 空, 0 空仓
        self.entry_price: float = 0.0
        self.best_price: float = 0.0  # 持仓期间最有利价（多=最高，空=最低）
        self.bars_held: int = 0
        self._breakeven_armed: bool = False

    def open(self, direction: int, entry_price: float) -> None:
        """登记一笔新持仓。``direction``：+1 多 / -1 空。"""
        if direction not in (1, -1):
            raise ValueError(f"direction 必须是 +1(多) 或 -1(空)，收到: {direction}")
        self.active = True
        self.direction = direction
        self.entry_price = entry_price
        self.best_price = entry_price
        self.bars_held = 0
        self._breakeven_armed = False

    def close(self) -> None:
        """平仓成交后调用，回到空仓。等价于 ``reset()``，语义更贴策略侧。"""
        self.reset()

    def pnl_points(self, price: float) -> float:
        """当前浮盈（点数，有利为正）。空仓返回 0。"""
        if not self.active:
            return 0.0
        return (price - self.entry_price) * self.direction

    def update(
        self,
        price: float,
        *,
        vwap: float | None = None,
        atr: float | None = None,
        advance_bar: bool = True,
    ) -> ExitDecision:
        """喂一个市场快照，返回离场决策。

        参数
        ----
        price:
            当前价（多用 last_price / close）。
        vwap:
            当日累计均价线，技术位止损用；``None`` 时跳过该法。
        atr:
            当前 ATR，跟随止盈用；``None`` / ≤0 时跳过该法。
        advance_bar:
            是否把 ``bars_held`` +1。bar 驱动策略每根 bar 调一次用默认 ``True``；
            tick 驱动策略一根 bar 内可能多次 ``update()``，应只在收 bar 时 ``True``、
            盘中 tick 传 ``False``，否则时间止损的「根数」语义会被 tick 数污染。
        """
        if not self.active:
            return ExitDecision(False)

        if advance_bar:
            self.bars_held += 1

        # 更新最有利价（多头取更高，空头取更低）
        if (price - self.best_price) * self.direction > 0:
            self.best_price = price

        pnl = self.pnl_points(price)
        cfg = self.config

        # 浮盈达触发阈 → 武装保本（一旦武装不撤销）
        if cfg.breakeven_trigger is not None and pnl >= cfg.breakeven_trigger:
            self._breakeven_armed = True

        # ---------- 保护性止损（优先） ----------
        if cfg.fixed_stop is not None and pnl <= -cfg.fixed_stop:
            return ExitDecision(
                True, ExitReason.FIXED_STOP, f"定额止损 浮盈{pnl:.2f}≤-{cfg.fixed_stop}"
            )

        if cfg.use_vwap_stop and vwap is not None and (price - vwap) * self.direction < 0:
            return ExitDecision(True, ExitReason.VWAP_STOP, f"穿均价线 价{price}/均价{vwap}")

        if self._breakeven_armed and pnl <= cfg.breakeven_offset:
            return ExitDecision(
                True, ExitReason.BREAKEVEN_STOP, f"回落保本 浮盈{pnl:.2f}≤{cfg.breakeven_offset}"
            )

        if (
            cfg.time_stop_bars is not None
            and self.bars_held >= cfg.time_stop_bars
            and abs(pnl) <= cfg.time_stop_eps
        ):
            return ExitDecision(
                True, ExitReason.TIME_STOP, f"时间止损 {self.bars_held}根无方向 浮盈{pnl:.2f}"
            )

        # ---------- 止盈 ----------
        if cfg.fixed_target is not None and pnl >= cfg.fixed_target:
            return ExitDecision(
                True, ExitReason.FIXED_TARGET, f"固定止盈 浮盈{pnl:.2f}≥{cfg.fixed_target}"
            )

        if cfg.trailing_atr_mult is not None and atr is not None and atr > 0:
            give_back = (self.best_price - price) * self.direction
            if give_back >= cfg.trailing_atr_mult * atr:
                return ExitDecision(
                    True,
                    ExitReason.TRAILING_STOP,
                    f"跟随止盈 自极值回撤{give_back:.2f}≥{cfg.trailing_atr_mult}×ATR({atr})",
                )

        return ExitDecision(False)
