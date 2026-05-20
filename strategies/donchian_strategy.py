"""Donchian channel breakout: enter on N-bar high/low break, exit on M-bar opposite break.

Comparator to DoubleMaStrategy for WFA analysis. Both are time-series momentum, but
parameterized differently (absolute price levels vs MA relative position) — useful for
asking "is rb 60min momentum-untradable, or is just DoubleMa untradable?"
"""

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
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


class DonchianStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    entry_window: int = 20
    exit_window: int = 10
    fixed_size: int = 1

    parameters = ["entry_window", "exit_window", "fixed_size"]
    variables: list[str] = []

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # See DoubleMa for rationale — default size=100 starves short daily backtests.
        self.am = ArrayManager(size=max(50, self.entry_window + 5))

    def on_init(self) -> None:
        self.write_log(f"Donchian init: entry={self.entry_window} exit={self.exit_window}")
        self.load_bar(self.entry_window + 1)

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        # bar-driven only
        pass

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        # Levels from PRIOR bars (exclude current). am arrays end with the just-added bar.
        entry_up = am.high_array[-self.entry_window - 1 : -1].max()
        entry_dn = am.low_array[-self.entry_window - 1 : -1].min()
        exit_up = am.high_array[-self.exit_window - 1 : -1].max()
        exit_dn = am.low_array[-self.exit_window - 1 : -1].min()

        price = bar.close_price

        if self.pos == 0:
            if price > entry_up:
                safe_buy(self, price, self.fixed_size)
            elif price < entry_dn:
                safe_short(self, price, self.fixed_size)
        elif self.pos > 0:
            if price < exit_dn:
                safe_sell(self, price, abs(self.pos))
                if price < entry_dn:
                    safe_short(self, price, self.fixed_size)
        elif self.pos < 0:
            if price > exit_up:
                safe_cover(self, price, abs(self.pos))
                if price > entry_up:
                    safe_buy(self, price, self.fixed_size)

        self.put_event()
