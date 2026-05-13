"""
================================================================
日内Tick级动量策略 (v2 - 修复版)
================================================================
修复：
    OPT-8:  price_buffer从list改deque（O(1)滚动）
    OPT-7:  完整类型注解
    SEVERE-6: 不再import通知模块，用write_log输出
================================================================
"""

from collections import deque
from datetime import time

from vnpy_ctastrategy import (
    BarData,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)

from utils.strategy_base import safe_callback


class IntradayTickStrategy(CtaTemplate):
    """日内Tick动量策略"""

    author: str = "Quant Team"

    price_window: int = 20
    volume_ratio: float = 1.5
    profit_target: float = 10
    stop_loss: float = 5
    fixed_size: int = 1
    exit_time_hour: int = 14
    exit_time_minute: int = 50

    tick_count: int = 0
    last_price: float = 0.0
    entry_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0

    parameters = [
        "price_window",
        "volume_ratio",
        "profit_target",
        "stop_loss",
        "fixed_size",
        "exit_time_hour",
        "exit_time_minute",
    ]
    variables = ["tick_count", "last_price", "entry_price", "high_price", "low_price"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # OPT-8: 用deque替代list，pop(0)从O(n)变O(1)
        self.price_buffer: deque[float] = deque(maxlen=self.price_window)
        self.exit_time = time(self.exit_time_hour, self.exit_time_minute)

    def on_init(self) -> None:
        self.write_log("日内Tick策略初始化")

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log(f"策略停止 持仓: {self.pos}")

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        if not tick.bid_volume_1 or not tick.ask_volume_1:
            return

        self.tick_count += 1
        self.last_price = tick.last_price

        # deque(maxlen=N)自动丢弃最旧元素，无需手动pop
        self.price_buffer.append(tick.last_price)

        if len(self.price_buffer) < self.price_window:
            return

        max_price = max(self.price_buffer)
        min_price = min(self.price_buffer)

        bid_volume = tick.bid_volume_1
        ask_volume = tick.ask_volume_1
        buy_pressure = bid_volume / ask_volume if ask_volume > 0 else 0
        sell_pressure = ask_volume / bid_volume if bid_volume > 0 else 0

        current_time = tick.datetime.time()

        # 强平时段
        if current_time >= self.exit_time:
            if self.pos > 0:
                self.sell(tick.bid_price_1, abs(self.pos))
            elif self.pos < 0:
                self.cover(tick.ask_price_1, abs(self.pos))
            return

        # 持仓管理
        if self.pos > 0:
            self.high_price = max(self.high_price, tick.last_price)
            profit = tick.last_price - self.entry_price
            if profit >= self.profit_target:
                self.write_log(f"止盈平多 盈利{profit:.1f}点")
                self.sell(tick.bid_price_1, abs(self.pos))
                return
            if profit <= -self.stop_loss:
                self.write_log(f"止损平多 亏损{profit:.1f}点")
                self.sell(tick.bid_price_1, abs(self.pos))
                return

        elif self.pos < 0:
            self.low_price = min(self.low_price, tick.last_price)
            profit = self.entry_price - tick.last_price
            if profit >= self.profit_target:
                self.write_log(f"止盈平空 盈利{profit:.1f}点")
                self.cover(tick.ask_price_1, abs(self.pos))
                return
            if profit <= -self.stop_loss:
                self.write_log(f"止损平空 亏损{profit:.1f}点")
                self.cover(tick.ask_price_1, abs(self.pos))
                return

        # 开仓信号
        if self.pos == 0:
            if tick.last_price >= max_price and buy_pressure >= self.volume_ratio:
                self.buy(tick.ask_price_1, self.fixed_size)
                self.entry_price = tick.ask_price_1
                self.high_price = tick.last_price
                self.write_log(f"信号: 突破做多 价格{tick.last_price} 买压{buy_pressure:.2f}")

            elif tick.last_price <= min_price and sell_pressure >= self.volume_ratio:
                self.short(tick.bid_price_1, self.fixed_size)
                self.entry_price = tick.bid_price_1
                self.low_price = tick.last_price
                self.write_log(f"信号: 跌破做空 价格{tick.last_price} 卖压{sell_pressure:.2f}")

        self.put_event()

    def on_bar(self, bar: BarData) -> None:
        pass

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        self.write_log(
            f"成交 {trade.direction.value} {trade.offset.value} @{trade.price} x{trade.volume}"
        )
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass
