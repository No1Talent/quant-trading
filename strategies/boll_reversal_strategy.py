"""Bollinger band mean-reversion: fade extremes, exit at the mean.

Structurally opposite to DoubleMa/Donchian (both time-series momentum). The
v1 plain version (sl_atr_mult=0, cooldown_bars=0) is committed in research
results CSVs; v2 adds opt-in ATR loss cap + post-stop cooldown to address
the fat-tail loss problem (rb2210 F1 -4.71, rb2305 F2 -4.57 in original WFA).

Both extras default to OFF for backward compat — pass non-zero values to
activate.
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
    atr_window: int = 14
    sl_atr_mult: float = 0.0  # 0 = disabled. Otherwise stop at entry +/- mult * ATR
    cooldown_bars: int = 0  # 0 = disabled. Bars after stop-out during which no entries fire
    fixed_size: int = 1

    parameters = [
        "boll_window",
        "boll_dev",
        "atr_window",
        "sl_atr_mult",
        "cooldown_bars",
        "fixed_size",
    ]
    variables: list[str] = ["entry_price", "cooldown_remaining"]

    entry_price: float = 0.0
    cooldown_remaining: int = 0

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # Size must cover whichever window is longest
        needed = max(self.boll_window, self.atr_window) + 5
        self.am = ArrayManager(size=max(50, needed))

    def on_init(self) -> None:
        self.write_log(
            f"BollReversal init: w={self.boll_window} dev={self.boll_dev} "
            f"atr={self.atr_window} sl_mult={self.sl_atr_mult} cd={self.cooldown_bars}"
        )
        self.load_bar(max(self.boll_window, self.atr_window) + 1)

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

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

        up, down = am.boll(self.boll_window, self.boll_dev)
        sma = am.sma(self.boll_window)
        price = bar.close_price

        if self.pos == 0:
            # Suppress new entries during post-stop cooldown
            if self.cooldown_remaining > 0:
                self.put_event()
                return
            if price > up:
                self.short(price, self.fixed_size)
                self.entry_price = price
            elif price < down:
                self.buy(price, self.fixed_size)
                self.entry_price = price
        elif self.pos > 0:
            # 1) Hard stop (if enabled) takes priority over mean-revert exit
            if self.sl_atr_mult > 0 and self.entry_price > 0:
                atr = am.atr(self.atr_window)
                if price <= self.entry_price - self.sl_atr_mult * atr:
                    self.sell(price, abs(self.pos))
                    self.entry_price = 0.0
                    self.cooldown_remaining = self.cooldown_bars
                    self.put_event()
                    return
            # 2) Mean-reversion exit
            if price > sma:
                self.sell(price, abs(self.pos))
                self.entry_price = 0.0
        elif self.pos < 0:
            if self.sl_atr_mult > 0 and self.entry_price > 0:
                atr = am.atr(self.atr_window)
                if price >= self.entry_price + self.sl_atr_mult * atr:
                    self.cover(price, abs(self.pos))
                    self.entry_price = 0.0
                    self.cooldown_remaining = self.cooldown_bars
                    self.put_event()
                    return
            if price < sma:
                self.cover(price, abs(self.pos))
                self.entry_price = 0.0

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
