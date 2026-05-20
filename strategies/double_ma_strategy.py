"""双均线趋势策略：金叉做多 / 死叉做空。"""

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    TickData,
)

from utils.strategy_base import (
    BaseCtaStrategy,
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class DoubleMaStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    fast_window: int = 10
    slow_window: int = 20
    fixed_size: int = 1

    fast_ma0: float = 0.0
    fast_ma1: float = 0.0
    slow_ma0: float = 0.0
    slow_ma1: float = 0.0

    parameters = ["fast_window", "slow_window", "fixed_size"]
    variables = ["fast_ma0", "fast_ma1", "slow_ma0", "slow_ma1"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        # Size = slow_window + headroom. Default ArrayManager(100) requires 100 bars
        # to .inited, which starves short daily backtests of trading time.
        self.am = ArrayManager(size=max(50, self.slow_window + 5))

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        self.load_bar(self.slow_window + 1)

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        fast_ma = am.sma(self.fast_window, array=True)
        self.fast_ma0 = fast_ma[-1]
        self.fast_ma1 = fast_ma[-2]

        slow_ma = am.sma(self.slow_window, array=True)
        self.slow_ma0 = slow_ma[-1]
        self.slow_ma1 = slow_ma[-2]

        cross_over = self.fast_ma0 > self.slow_ma0 and self.fast_ma1 <= self.slow_ma1
        cross_below = self.fast_ma0 < self.slow_ma0 and self.fast_ma1 >= self.slow_ma1

        if cross_over:
            self.write_log(
                f"信号: 金叉做多 快线{self.fast_ma0:.2f} > "
                f"慢线{self.slow_ma0:.2f} 当前价{bar.close_price}"
            )
            if self.pos == 0:
                safe_buy(self, bar.close_price, self.fixed_size)
            elif self.pos < 0:
                safe_cover(self, bar.close_price, abs(self.pos))
                safe_buy(self, bar.close_price, self.fixed_size)

        elif cross_below:
            self.write_log(
                f"信号: 死叉做空 快线{self.fast_ma0:.2f} < "
                f"慢线{self.slow_ma0:.2f} 当前价{bar.close_price}"
            )
            if self.pos == 0:
                safe_short(self, bar.close_price, self.fixed_size)
            elif self.pos > 0:
                safe_sell(self, bar.close_price, abs(self.pos))
                safe_short(self, bar.close_price, self.fixed_size)

        self.put_event()
