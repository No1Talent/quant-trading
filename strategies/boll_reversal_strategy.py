"""Bollinger band mean-reversion: fade extremes, exit at the mean.

Structurally opposite to DoubleMa/Donchian (both time-series momentum). If this
strategy class shows positive IS-OOS Sharpe correlation on rb/ag 60min while
momentum showed negative, the conclusion sharpens to "60min on these instruments
is mean-reverting, not trending" rather than "60min has no alpha at all".
"""

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)

from utils.strategy_base import safe_callback


class BollReversalStrategy(CtaTemplate):
    author: str = "Quant Team"

    boll_window: int = 20
    boll_dev: float = 2.0
    fixed_size: int = 1

    parameters = ["boll_window", "boll_dev", "fixed_size"]
    variables: list[str] = []

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.am = ArrayManager(size=max(50, self.boll_window + 5))

    def on_init(self) -> None:
        self.write_log(f"BollReversal init: w={self.boll_window} dev={self.boll_dev}")
        self.load_bar(self.boll_window + 1)

    def on_start(self) -> None:
        params = ", ".join(f"{p}={getattr(self, p)}" for p in self.parameters)
        self.write_log(f"BollReversal start {params}")

    def on_stop(self) -> None:
        self.write_log(f"BollReversal stop pos={self.pos}")
        self.sync_data()

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        pass

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        up, down = am.boll(self.boll_window, self.boll_dev)
        sma = am.sma(self.boll_window)
        price = bar.close_price

        if self.pos == 0:
            # Fade extremes
            if price > up:
                self.short(price, self.fixed_size)
            elif price < down:
                self.buy(price, self.fixed_size)
        elif self.pos > 0:
            # Long: exit when price reverts back to/above mean
            if price > sma:
                self.sell(price, abs(self.pos))
        elif self.pos < 0:
            # Short: exit when price reverts back to/below mean
            if price < sma:
                self.cover(price, abs(self.pos))

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        self.write_log(
            f"trade {trade.direction.value} {trade.offset.value} @{trade.price} x{trade.volume}"
        )
        self.put_event()
        self.sync_data()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass
