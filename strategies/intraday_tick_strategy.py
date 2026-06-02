"""日内 Tick 级动量策略：突破 + 买卖压力比开仓；离场交给 ExitPolicy（止损止盈纪律）。

离场状态机由 :class:`utils.exit_policy.ExitPolicy` 持有。**关键设计：``open()`` / ``close()``
由 ``on_trade``（成交确认）驱动，而不是发单那一刻** —— 见 ``docs/intraday_fenshi_method.md``
第 8 节「单一事实源」。若在 ``safe_buy`` 时就 ``open()``，遇 RiskGuard 拒单 / SIT 合成拒单时
``self.pos`` 仍为 0 但 ExitPolicy 误以为持仓 → 仓位漂移、发幽灵平仓信号。挂在 ``on_trade`` 上，
ExitPolicy 就只是 ``self.pos`` 的镜像。

尾盘强平（session close）ExitPolicy v1 不覆盖，仍留在策略侧 ``on_tick`` 处理。
"""

from collections import deque
from datetime import time

from vnpy.trader.constant import Direction, Offset
from vnpy_ctastrategy import (
    BarData,
    TickData,
)

from utils.exit_policy import ExitConfig, ExitPolicy
from utils.strategy_base import (
    BaseCtaStrategy,
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class IntradayTickStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    price_window: int = 20
    volume_ratio: float = 1.5
    profit_target: float = 10  # → ExitConfig.fixed_target（固定止盈点数）
    stop_loss: float = 5  # → ExitConfig.fixed_stop（定额止损点数）
    breakeven_trigger: float = 0.0  # 浮盈达此点数后武装保本；0 = 关。仅需价格，tick 可用
    breakeven_offset: float = 0.0  # 保本位锁定点数（>0 锁手续费）
    fixed_size: int = 1
    exit_time_hour: int = 14
    exit_time_minute: int = 50

    tick_count: int = 0
    last_price: float = 0.0

    parameters = [
        "price_window",
        "volume_ratio",
        "profit_target",
        "stop_loss",
        "breakeven_trigger",
        "breakeven_offset",
        "fixed_size",
        "exit_time_hour",
        "exit_time_minute",
    ]
    variables = ["tick_count", "last_price"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.price_buffer: deque[float] = deque(maxlen=self.price_window)
        self.exit_time = time(self.exit_time_hour, self.exit_time_minute)
        # super().__init__ 已 apply setting → 此处 self.* 是最终参数值
        self.exit_policy = ExitPolicy(self._build_exit_config())

    def _build_exit_config(self) -> ExitConfig:
        """把策略参数翻译成 ExitConfig。0 视为「关闭该法」（ExitConfig 用 None 表关闭）。"""
        trigger = self.breakeven_trigger or None
        return ExitConfig(
            fixed_stop=self.stop_loss or None,
            fixed_target=self.profit_target or None,
            breakeven_trigger=trigger,
            breakeven_offset=self.breakeven_offset if trigger else 0.0,
        )

    def on_init(self) -> None:
        self.write_log("日内Tick策略初始化")

    def on_trade(self, trade) -> None:
        """成交确认 → 登记/注销 ExitPolicy 的逻辑仓（单一事实源，见模块 docstring）。"""
        super().on_trade(trade)  # 默认：日志 + put_event + sync_data
        if trade.offset == Offset.OPEN:
            direction = 1 if trade.direction == Direction.LONG else -1
            self.exit_policy.open(direction, trade.price)
        else:
            self.exit_policy.close()

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        if not tick.bid_volume_1 or not tick.ask_volume_1:
            return

        self.tick_count += 1
        self.last_price = tick.last_price
        self.price_buffer.append(tick.last_price)

        current_time = tick.datetime.time()

        # 1) 尾盘强平：ExitPolicy v1 不覆盖 session-close，留在策略侧
        if current_time >= self.exit_time:
            self._force_close(tick)
            return

        # 2) 持仓中：离场决策全交给 ExitPolicy（tick 驱动 → advance_bar=False，
        #    否则时间止损的「根数」语义会被 tick 数污染）
        if self.exit_policy.active:
            decision = self.exit_policy.update(tick.last_price, advance_bar=False)
            if decision.should_exit:
                self.write_log(f"离场[{decision.reason.value}] {decision.note}")
                self._close_position(tick)
            return  # 持仓时不找新开仓

        # 3) 空仓：找突破开仓
        if len(self.price_buffer) < self.price_window:
            return

        if self.pos == 0:
            max_price = max(self.price_buffer)
            min_price = min(self.price_buffer)
            buy_pressure = tick.bid_volume_1 / tick.ask_volume_1
            sell_pressure = tick.ask_volume_1 / tick.bid_volume_1

            if tick.last_price >= max_price and buy_pressure >= self.volume_ratio:
                safe_buy(self, tick.ask_price_1, self.fixed_size)
                self.write_log(f"信号: 突破做多 价格{tick.last_price} 买压{buy_pressure:.2f}")
            elif tick.last_price <= min_price and sell_pressure >= self.volume_ratio:
                safe_short(self, tick.bid_price_1, self.fixed_size)
                self.write_log(f"信号: 跌破做空 价格{tick.last_price} 卖压{sell_pressure:.2f}")

        self.put_event()

    def _close_position(self, tick: TickData) -> None:
        """按 ExitPolicy 记录的方向平仓。成交回报会触发 on_trade → exit_policy.close()。"""
        volume = abs(self.pos)
        if not volume:
            return
        if self.exit_policy.direction > 0:
            safe_sell(self, tick.bid_price_1, volume)
        elif self.exit_policy.direction < 0:
            safe_cover(self, tick.ask_price_1, volume)

    def _force_close(self, tick: TickData) -> None:
        """尾盘按 self.pos 强平（不依赖 ExitPolicy，覆盖手工持仓等边界）。"""
        if self.pos > 0:
            safe_sell(self, tick.bid_price_1, abs(self.pos))
        elif self.pos < 0:
            safe_cover(self, tick.ask_price_1, abs(self.pos))

    def on_bar(self, bar: BarData) -> None:
        pass
