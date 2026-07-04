"""Vol-targeted MA-cross trend strategy (live form of the M3.7 research result).

Research arc (see project_research_layer2_status): the DoubleMa daily trend
signal on AG/CU is real but the naive fixed/inverse-train-vol sizing left a
fat-tailed, only-marginally-significant return stream (M3.5 +0.526, kurt 63).
Re-sizing each day by a *causal* trailing-volatility target (M3.7) flipped it
to a significant, cost- and capital-robust result (AG-solo Sharpe ~1.2, DSR
0.999; survives 5× cost; scales from ~500k). This class is the live embodiment.

Difference from DoubleMaStrategy: that one flips a *fixed* lot count on each
crossover. This one holds `sign(fast−slow) × target_lots` and **re-sizes every
bar**, where

    target_lots = round( clip(target_vol / per_lot_daily_$vol, 0, max_leverage)
                         × size_scale )
    per_lot_daily_$vol = std(last `vol_window` daily close-to-close moves)
                         × contract_size      # causal: uses bars up to this close

so the position grows in calm regimes and is cut in turbulent ones (this is what
down-sized AG out of the 2026 silver vol spike).

Sizing knobs:
  - target_vol      : desired daily PnL volatility per "1× unit" (CNY). 5,000 ≈
                      0.5% of 1M capital at the research calibration.
  - size_scale      : capital multiplier. 1.0 = ~1M base; set capital/1e6 for more.
  - max_leverage    : cap on the continuous weight before rounding (bounds the
                      position when trailing vol collapses).
  - contract_size   : exchange multiplier (AG=15, CU=5) — converts a price move to
                      per-lot PnL. MUST match the live contract.

Daily bar contract: `bar_interval="1d"`; the base class aggregates 1min→daily at
`daily_end`. NOTE for live validation: AG/CU have night sessions — confirm the
live daily bar boundary matches the research's exchange-daily bars before trusting
the live signal (logged as a pre-flight item, not assumed here).
"""

from __future__ import annotations

import numpy as np
from vnpy_ctastrategy import ArrayManager, BarData

from utils.strategy_base import (
    BaseCtaStrategy,
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class VolTargetMaStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    # Research (M3.x) is daily continuous; the base class makes LIVE aggregate to
    # daily too (single-source bar contract) rather than vn.py's default 1min.
    bar_interval: str = "1d"
    live_eligible: bool = True

    # --- signal ---
    fast_window: int = 20
    slow_window: int = 40
    # --- vol-target sizing ---
    vol_window: int = 63
    target_vol: float = 5000.0
    max_leverage: float = 4.0
    size_scale: float = 1.0
    contract_size: int = 15  # AG default; set 5 for CU

    # --- state (synced) ---
    fast_ma0: float = 0.0
    slow_ma0: float = 0.0
    direction: int = 0
    per_lot_vol: float = 0.0
    target_lots: int = 0

    parameters = [
        "fast_window",
        "slow_window",
        "vol_window",
        "target_vol",
        "max_leverage",
        "size_scale",
        "contract_size",
    ]
    variables = ["fast_ma0", "slow_ma0", "direction", "per_lot_vol", "target_lots"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # Need enough history for both the slow MA and the vol window (+ headroom).
        warmup = max(self.slow_window, self.vol_window + 1)
        self.am = ArrayManager(size=warmup + 10)

    def on_init(self) -> None:
        self.write_log(f"策略初始化：{self.strategy_name}")
        self.load_bar(max(self.slow_window, self.vol_window + 1) + 2)

    # ------------------------------------------------------------------ #
    # sizing
    # ------------------------------------------------------------------ #
    def _compute_target_lots(self) -> int:
        """Causal vol-target lot count from the trailing per-lot daily $ vol.

        Uses close-to-close moves over the last `vol_window` bars (through the
        bar that just closed) — no look-ahead. Returns a non-negative integer."""
        closes = self.am.close[-(self.vol_window + 1) :]
        if len(closes) < 2:
            return 0
        price_vol = float(np.std(np.diff(closes)))
        self.per_lot_vol = price_vol * self.contract_size
        if self.per_lot_vol <= 0:
            return 0
        weight = min(self.target_vol / self.per_lot_vol, self.max_leverage)
        return max(0, int(round(weight * self.size_scale)))

    def _rebalance_to(self, desired: int, price: float) -> None:
        """Move the current signed position to `desired` using only safe_* sends.

        Two steps: (1) reduce/close any exposure beyond `desired` on the current
        side, (2) open the remainder in the desired direction. Correctly handles
        flips through zero (close-then-open)."""
        current = int(self.pos)
        if desired == current:
            return

        # Step 1: trim the opposite/excess side back toward `desired`.
        if current > 0 and desired < current:
            sell_to = max(desired, 0)
            safe_sell(self, price, current - sell_to)
            current = sell_to
        elif current < 0 and desired > current:
            cover_to = min(desired, 0)
            safe_cover(self, price, cover_to - current)
            current = cover_to

        # Step 2: open the remainder in the target direction.
        if desired > current:
            safe_buy(self, price, desired - current)
        elif desired < current:
            safe_short(self, price, current - desired)

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        self.fast_ma0 = float(am.sma(self.fast_window, array=True)[-1])
        self.slow_ma0 = float(am.sma(self.slow_window, array=True)[-1])
        self.direction = 1 if self.fast_ma0 > self.slow_ma0 else -1

        self.target_lots = self._compute_target_lots()
        desired = self.direction * self.target_lots

        if desired != int(self.pos):
            self.write_log(
                f"调仓 dir={self.direction} target_lots={self.target_lots} "
                f"(per_lot_vol={self.per_lot_vol:.0f}) pos={self.pos}→{desired} @{bar.close_price}"
            )
        self._rebalance_to(desired, bar.close_price)
        self.put_event()
