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
)

from utils.rollover import detect_rollover
from utils.strategy_base import (
    BaseCtaStrategy,
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class CarryRollStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    # 日线换月套利研究策略：alpha 定义在「当日 OI 跳变 + 跳空」上，按日线 bar 评估。
    # live_eligible=False —— 日线 alpha 不能被实盘 1min bar 流逐分钟重算，由 run.py
    # 的 install_live_eligibility_guard 拦截，杜绝实盘错配粒度自残。
    bar_interval: str = "1d"
    live_eligible: bool = False

    oi_pct_threshold: float = 20.0
    gap_floor_pct: float = 0.3
    hold_days: int = 5
    fixed_size: int = 1

    bars_held: int = 0

    parameters = ["oi_pct_threshold", "gap_floor_pct", "hold_days", "fixed_size"]
    variables = ["bars_held"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # BarGenerator / on_tick 由基类按 bar_interval 处理（研究型，不上线）。
        # Strategy state — not part of vn.py variables (mutable internal-only).
        self._prev_oi: float = 0.0
        self._prev_close: float = 0.0

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        # Only need 1-bar lag, but load a small cushion for the engine.
        self.load_bar(2)

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        # 1. Rollover detection — delegated to utils.rollover so all H1.5-based
        #    strategies share one threshold definition (see module docstring).
        detection = detect_rollover(
            prev_oi=self._prev_oi,
            prev_close=self._prev_close,
            curr_oi=bar.open_interest,
            curr_open=bar.open_price,
            oi_pct_threshold=self.oi_pct_threshold,
            gap_floor_pct=self.gap_floor_pct,
        )
        rollover = detection.is_rollover
        gap_sign = detection.gap_sign

        # 2. Exit logic FIRST — frees up the slot for a same-bar re-entry if a
        #    new rollover happens to coincide with the last day of a hold cycle.
        if self.pos != 0:
            self.bars_held += 1
            if self.bars_held >= self.hold_days:
                if self.pos > 0:
                    safe_sell(self, bar.close_price, abs(self.pos))
                else:
                    safe_cover(self, bar.close_price, abs(self.pos))
                self.bars_held = 0

        # 3. Entry logic — only if flat (post-exit) and rollover detected.
        if rollover and self.pos == 0:
            if gap_sign > 0:
                safe_buy(self, bar.close_price, self.fixed_size)
            else:
                safe_short(self, bar.close_price, self.fixed_size)
            self.bars_held = 0  # next on_bar increments to 1

        # 4. Update lag for next bar.
        self._prev_oi = bar.open_interest
        self._prev_close = bar.close_price

        self.put_event()
