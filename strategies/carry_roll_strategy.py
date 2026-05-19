"""CarryRollStrategy — explicit carry-extraction on rollover events.

Built for H6b after H6a attribution showed I (iron ore) DoubleMa daily
PnL is 60.4% concentrated within ±5 trading days of H1.5-detected
rollovers (concentration ratio 5.27x, ROLL_pm1 bucket Sharpe +4.64).

Logic per daily bar:
  1. Detect today as a rollover day from prev close + prev OI using the
     same H1.5 thresholds (|ΔOI|>20% AND |gap|>0.3%) so the trading
     decision matches the attribution exactly.
  2. If holding a position, count bars_held; close after `hold_days`.
  3. If flat AND today was a rollover: enter in direction of gap sign
     (positive gap → long, negative gap → short). Fill at next bar open.

The strategy is bar-stateful (prev_oi, prev_close, bars_held) but uses
no indicators / ArrayManager — the alpha is mechanically defined by
H1.5 detection rather than any MA window.
"""

from vnpy_ctastrategy import (
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)

from utils.strategy_base import safe_callback


class CarryRollStrategy(CtaTemplate):
    author: str = "Quant Team"

    oi_pct_threshold: float = 20.0
    gap_floor_pct: float = 0.3
    hold_days: int = 5
    fixed_size: int = 1

    bars_held: int = 0

    parameters = ["oi_pct_threshold", "gap_floor_pct", "hold_days", "fixed_size"]
    variables = ["bars_held"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        # Strategy state — not part of vn.py variables (mutable internal-only).
        self._prev_oi: float = 0.0
        self._prev_close: float = 0.0

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        # Only need 1-bar lag, but load a small cushion for the engine.
        self.load_bar(2)

    def on_start(self) -> None:
        params = ", ".join(f"{p}={getattr(self, p)}" for p in self.parameters)
        self.write_log(f"策略启动 参数: {params}")

    def on_stop(self) -> None:
        self.write_log(f"策略停止 当前持仓: {self.pos}")
        self.sync_data()

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        # 1. Rollover detection from yesterday's close + OI to today's open + OI.
        rollover = False
        gap_sign = 0
        if self._prev_oi > 0 and self._prev_close > 0:
            oi_pct = abs(bar.open_interest - self._prev_oi) / self._prev_oi * 100
            gap_pct = abs(bar.open_price - self._prev_close) / self._prev_close * 100
            if oi_pct > self.oi_pct_threshold and gap_pct > self.gap_floor_pct:
                rollover = True
                gap_sign = 1 if bar.open_price > self._prev_close else -1

        # 2. Exit logic FIRST — frees up the slot for a same-bar re-entry if a
        #    new rollover happens to coincide with the last day of a hold cycle.
        if self.pos != 0:
            self.bars_held += 1
            if self.bars_held >= self.hold_days:
                if self.pos > 0:
                    self.sell(bar.close_price, abs(self.pos))
                else:
                    self.cover(bar.close_price, abs(self.pos))
                self.bars_held = 0

        # 3. Entry logic — only if flat (post-exit) and rollover detected.
        if rollover and self.pos == 0:
            if gap_sign > 0:
                self.buy(bar.close_price, self.fixed_size)
            else:
                self.short(bar.close_price, self.fixed_size)
            self.bars_held = 0  # next on_bar increments to 1

        # 4. Update lag for next bar.
        self._prev_oi = bar.open_interest
        self._prev_close = bar.close_price

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        self.write_log(
            f"成交 {trade.direction.value} {trade.offset.value} "
            f"价格={trade.price} 数量={trade.volume}"
        )
        self.put_event()
        self.sync_data()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass
