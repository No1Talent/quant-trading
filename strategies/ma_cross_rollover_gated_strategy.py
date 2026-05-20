"""MaCrossRolloverGatedStrategy — DoubleMa direction filtered by post-rollover window.

Built for H6c after H6a showed I.raw's PnL is 60.4% concentrated within ±5
trading days of H1.5-detected rollovers (FAR Sharpe +0.04, ROLL_pm1 +4.64),
and H6b showed pure `sign(gap)` carry collapses post-2018 (+0.053 OOS).

Hypothesis: the value lives at the **trend × rollover intersection**. Use
the MA cross to pick direction (richer than gap-sign — integrates pre-roll
info, adapts to regime shifts), but only act when a rollover has happened
recently (causal filter — no peeking at future rollovers).

Logic per daily bar:
  1. Detect if TODAY is a rollover via H1.5 thresholds
     (|ΔOI|>oi_pct_threshold AND |gap|>gap_floor_pct). Same detection as
     H6a/H6b, comparing this bar's open+OI to last bar's close+OI.
  2. Increment `bars_since_rollover`; reset to 0 if rollover detected today.
  3. Update ArrayManager + compute MA cross signal (same as DoubleMa).
  4. ENTRY gated: on cross, only open a new position if
     `bars_since_rollover <= post_roll_window` (we're still in the window).
  5. EXIT ungated: always close opposite positions on opposite cross (no
     orphan positions stuck because the rollover window closed).

Gating is on entries only; this preserves trend-follow exit discipline.
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

from utils.rollover import detect_rollover
from utils.strategy_base import safe_callback


class MaCrossRolloverGatedStrategy(CtaTemplate):
    author: str = "Quant Team"

    fast_window: int = 10
    slow_window: int = 40
    post_roll_window: int = 5
    oi_pct_threshold: float = 20.0
    gap_floor_pct: float = 0.3
    fixed_size: int = 1

    fast_ma0: float = 0.0
    fast_ma1: float = 0.0
    slow_ma0: float = 0.0
    slow_ma1: float = 0.0

    parameters = [
        "fast_window",
        "slow_window",
        "post_roll_window",
        "oi_pct_threshold",
        "gap_floor_pct",
        "fixed_size",
    ]
    variables = ["fast_ma0", "fast_ma1", "slow_ma0", "slow_ma1"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=max(50, self.slow_window + 5))
        # Internal state (not vn.py variables — mutable, no need to persist for live).
        self._prev_oi: float = 0.0
        self._prev_close: float = 0.0
        # Large sentinel = "no rollover seen yet" → gated_now is False.
        self._bars_since_rollover: int = 10**9

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        self.load_bar(self.slow_window + 1)

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
        # 1. Rollover detection — delegated to utils.rollover (single source of
        #    truth for H1.5 thresholds across this strategy and carry_roll).
        rollover_today = detect_rollover(
            prev_oi=self._prev_oi,
            prev_close=self._prev_close,
            curr_oi=bar.open_interest,
            curr_open=bar.open_price,
            oi_pct_threshold=self.oi_pct_threshold,
            gap_floor_pct=self.gap_floor_pct,
        ).is_rollover

        # 2. Counter update — increment first, then reset if today is a rollover
        #    (so today reads as bars_since_rollover == 0).
        self._bars_since_rollover += 1
        if rollover_today:
            self._bars_since_rollover = 0

        gated_now = self._bars_since_rollover <= self.post_roll_window

        # 3. MA computation (mirror DoubleMa exactly).
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            self._prev_oi = bar.open_interest
            self._prev_close = bar.close_price
            self.put_event()
            return

        fast_ma = am.sma(self.fast_window, array=True)
        self.fast_ma0 = fast_ma[-1]
        self.fast_ma1 = fast_ma[-2]
        slow_ma = am.sma(self.slow_window, array=True)
        self.slow_ma0 = slow_ma[-1]
        self.slow_ma1 = slow_ma[-2]

        cross_over = self.fast_ma0 > self.slow_ma0 and self.fast_ma1 <= self.slow_ma1
        cross_below = self.fast_ma0 < self.slow_ma0 and self.fast_ma1 >= self.slow_ma1

        # 4/5. Exit always; enter only if gated.
        if cross_over:
            if self.pos < 0:
                self.cover(bar.close_price, abs(self.pos))
            if self.pos == 0 and gated_now:
                self.buy(bar.close_price, self.fixed_size)
        elif cross_below:
            if self.pos > 0:
                self.sell(bar.close_price, abs(self.pos))
            if self.pos == 0 and gated_now:
                self.short(bar.close_price, self.fixed_size)

        # 6. Update lag for next bar.
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
