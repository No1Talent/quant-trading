"""Factor library: alpha158-style formulas for futures cross-sectional research.

Convention.
  - Input: long-format panel DataFrame indexed by MultiIndex(datetime, symbol)
    with columns [open, high, low, close, volume, open_interest].
  - Output: pd.Series same-indexed, name = canonical factor key.
  - Time-series ops (rolling, shift, pct_change) go through `_to_wide` → wide
    panel → op → `_to_long`. This avoids cross-symbol contamination that
    naive `groupby(level="symbol")` can still leak across at concat boundaries.
  - Cross-sectional ops (rank, zscore) use `groupby(level="datetime")` — safe
    because grouping over the OUTER level of the MultiIndex is well-defined.
  - All factor functions are PURE: same panel in, same Series out, no IO.

Factor categories below mirror the alpha158 paper's groupings (rolling-window
TS features + cross-sectional reduce). Names are kept short for IC tables.
NaN warmup is intentional — factor users (alphalens, IC compute) drop NaNs.

FACTORS registry at the bottom is the entry point used by M2 evaluator: each
key resolves to a zero-arg-callable (panel → Series) for batch IC computation.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# ---------- Wide-long shape helpers ----------


def _to_wide(panel: pd.DataFrame, col: str) -> pd.DataFrame:
    """Long panel → wide (datetime × symbol) for a single column."""
    return panel[col].unstack("symbol").sort_index()


def _to_long(wide: pd.DataFrame, name: str) -> pd.Series:
    """Wide DataFrame → long Series with MultiIndex(datetime, symbol)."""
    s = wide.stack(future_stack=True)
    s.index.names = ["datetime", "symbol"]
    s.name = name
    return s


# ---------- Momentum / returns ----------


def ret_n(panel: pd.DataFrame, n: int) -> pd.Series:
    """Log return over the last n bars, per symbol."""
    close = _to_wide(panel, "close")
    return _to_long(np.log(close / close.shift(n)), f"ret_{n}")


def ret_skew(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Skewness of daily log-returns over rolling window. Negative skew often
    means crash risk concentrated; positive means lottery-like tail."""
    close = _to_wide(panel, "close")
    ret1 = np.log(close / close.shift(1))
    return _to_long(ret1.rolling(window).skew(), f"ret_skew_{window}")


# ---------- Volatility ----------


def vol_n(panel: pd.DataFrame, n: int = 20) -> pd.Series:
    """Rolling std of daily log-returns. Standard realised-vol proxy."""
    close = _to_wide(panel, "close")
    ret1 = np.log(close / close.shift(1))
    return _to_long(ret1.rolling(n).std(), f"vol_{n}")


def vol_ratio(panel: pd.DataFrame, short: int = 5, long_: int = 20) -> pd.Series:
    """Short / long realised-vol ratio. >1 means short-term regime shock."""
    close = _to_wide(panel, "close")
    ret1 = np.log(close / close.shift(1))
    v_short = ret1.rolling(short).std()
    v_long = ret1.rolling(long_).std()
    return _to_long(v_short / v_long, f"vol_ratio_{short}_{long_}")


# ---------- Volume ----------


def volume_ratio(panel: pd.DataFrame, short: int = 5, long_: int = 20) -> pd.Series:
    """Short / long volume mean ratio. Surge in recent volume = attention spike."""
    v = _to_wide(panel, "volume")
    return _to_long(
        v.rolling(short).mean() / v.rolling(long_).mean(), f"volume_ratio_{short}_{long_}"
    )


def dollar_volume_zscore(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Z-score of (close × volume) over rolling window — extreme liquidity events."""
    close = _to_wide(panel, "close")
    vol = _to_wide(panel, "volume")
    dv = close * vol
    z = (dv - dv.rolling(window).mean()) / dv.rolling(window).std()
    return _to_long(z, f"dv_zscore_{window}")


# ---------- Open Interest (futures-specific) ----------


def oi_pct_change(panel: pd.DataFrame, n: int = 5) -> pd.Series:
    """Percent change in open interest over n bars. Rising OI + rising price =
    new longs entering (bullish). Rising OI + falling price = new shorts."""
    oi = _to_wide(panel, "open_interest")
    return _to_long(oi.pct_change(n), f"oi_pct_change_{n}")


def oi_price_corr(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling corr between daily ΔOI and daily return. Positive = trend-following
    money. Negative = mean-reverting / hedging flow."""
    close = _to_wide(panel, "close")
    oi = _to_wide(panel, "open_interest")
    ret1 = np.log(close / close.shift(1))
    doi = oi.pct_change(1)
    # Rolling pairwise correlation, per symbol (column-wise)
    corr = ret1.rolling(window).corr(doi)
    return _to_long(corr, f"oi_price_corr_{window}")


# ---------- Structure (range / position) ----------


def high_low_range(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling (max_high - min_low) / close — width of recent range, as % of price."""
    high = _to_wide(panel, "high")
    low = _to_wide(panel, "low")
    close = _to_wide(panel, "close")
    rng = high.rolling(window).max() - low.rolling(window).min()
    return _to_long(rng / close, f"hl_range_{window}")


def close_to_range_pos(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Where current close sits in rolling [low_min, high_max], scaled to [0, 1].
    0 = at recent low, 1 = at recent high. Donchian-flavored position factor."""
    high = _to_wide(panel, "high")
    low = _to_wide(panel, "low")
    close = _to_wide(panel, "close")
    rmax = high.rolling(window).max()
    rmin = low.rolling(window).min()
    return _to_long((close - rmin) / (rmax - rmin), f"close_pos_{window}")


def max_drawdown_n(panel: pd.DataFrame, window: int = 20) -> pd.Series:
    """Current close vs. rolling max over window, as a negative fraction.
    0 = at recent peak, -0.1 = 10% below recent peak."""
    close = _to_wide(panel, "close")
    rmax = close.rolling(window).max()
    return _to_long((close - rmax) / rmax, f"max_dd_{window}")


# ---------- Cross-sectional reducers ----------


def cs_rank(factor: pd.Series) -> pd.Series:
    """Cross-sectional rank across symbols at each datetime, scaled to [0, 1].
    NaN-aware: symbols with NaN factor are excluded from that day's rank."""
    return factor.groupby(level="datetime").rank(pct=True)


def cs_zscore(factor: pd.Series) -> pd.Series:
    """Cross-sectional z-score across symbols at each datetime.
    NaN propagates through, but the per-day mean/std ignore NaN."""
    grouped = factor.groupby(level="datetime")
    return (factor - grouped.transform("mean")) / grouped.transform("std")


# ---------- Registry (entry point for M2 IC evaluator) ----------

# Default factor set. Add tuned parameter variants here as research surfaces them.
# Naming convention: `<family>_<param>` so the M2 table sorts cleanly.
FACTORS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    # Momentum / returns
    "ret_1": lambda p: ret_n(p, 1),
    "ret_5": lambda p: ret_n(p, 5),
    "ret_20": lambda p: ret_n(p, 20),
    "ret_60": lambda p: ret_n(p, 60),
    "ret_skew_20": lambda p: ret_skew(p, 20),
    # Volatility
    "vol_20": lambda p: vol_n(p, 20),
    "vol_ratio_5_20": lambda p: vol_ratio(p, 5, 20),
    # Volume
    "volume_ratio_5_20": lambda p: volume_ratio(p, 5, 20),
    "dv_zscore_20": lambda p: dollar_volume_zscore(p, 20),
    # OI
    "oi_pct_5": lambda p: oi_pct_change(p, 5),
    "oi_pct_20": lambda p: oi_pct_change(p, 20),
    "oi_price_corr_20": lambda p: oi_price_corr(p, 20),
    # Structure
    "hl_range_20": lambda p: high_low_range(p, 20),
    "close_pos_20": lambda p: close_to_range_pos(p, 20),
    "max_dd_20": lambda p: max_drawdown_n(p, 20),
}
