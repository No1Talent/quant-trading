"""
================================================================
双均线趋势策略 (v2 - 解耦版)
================================================================
对比 v1 的改进：
    - 完全不import任何通知模块
    - 信号通过 write_log 输出，NotifyListener独立监听并推送
    - on_bar用@safe_callback装饰，异常自动隔离
    - 类型注解完整（OPT-7）
    - 回测和实盘行为完全一致（OPT-1）

策略代码 = 纯策略逻辑，通知是基础设施，两者解耦
================================================================
"""

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)

from utils.strategy_base import safe_callback


class DoubleMaStrategy(CtaTemplate):
    """双均线策略"""

    author: str = "Quant Team"

    # 参数
    fast_window: int = 10
    slow_window: int = 20
    fixed_size: int = 1

    # 变量
    fast_ma0: float = 0.0
    fast_ma1: float = 0.0
    slow_ma0: float = 0.0
    slow_ma1: float = 0.0

    parameters = ["fast_window", "slow_window", "fixed_size"]
    variables = ["fast_ma0", "fast_ma1", "slow_ma0", "slow_ma1"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager()

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        self.load_bar(10)

    def on_start(self) -> None:
        """启动时只写日志，通知由NotifyListener独立处理"""
        params = ", ".join(f"{p}={getattr(self, p)}" for p in self.parameters)
        self.write_log(f"策略启动 参数: {params}")

    def on_stop(self) -> None:
        self.write_log(f"策略停止 当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        """K线回调 - 用safe_callback自动隔离异常"""
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        # 计算均线
        fast_ma = am.sma(self.fast_window, array=True)
        self.fast_ma0 = fast_ma[-1]
        self.fast_ma1 = fast_ma[-2]

        slow_ma = am.sma(self.slow_window, array=True)
        self.slow_ma0 = slow_ma[-1]
        self.slow_ma1 = slow_ma[-2]

        cross_over = self.fast_ma0 > self.slow_ma0 and self.fast_ma1 <= self.slow_ma1
        cross_below = self.fast_ma0 < self.slow_ma0 and self.fast_ma1 >= self.slow_ma1

        # 信号通过write_log输出，监听器会自动推送
        if cross_over:
            self.write_log(
                f"信号: 金叉做多 快线{self.fast_ma0:.2f} > "
                f"慢线{self.slow_ma0:.2f} 当前价{bar.close_price}"
            )
            if self.pos == 0:
                self.buy(bar.close_price, self.fixed_size)
            elif self.pos < 0:
                self.cover(bar.close_price, abs(self.pos))
                self.buy(bar.close_price, self.fixed_size)

        elif cross_below:
            self.write_log(
                f"信号: 死叉做空 快线{self.fast_ma0:.2f} < "
                f"慢线{self.slow_ma0:.2f} 当前价{bar.close_price}"
            )
            if self.pos == 0:
                self.short(bar.close_price, self.fixed_size)
            elif self.pos > 0:
                self.sell(bar.close_price, abs(self.pos))
                self.short(bar.close_price, self.fixed_size)

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        """成交回报 - 不再写通知代码，监听器会自动推送"""
        self.write_log(
            f"成交 {trade.direction.value} {trade.offset.value} "
            f"价格={trade.price} 数量={trade.volume}"
        )
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass
