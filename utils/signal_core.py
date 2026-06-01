"""utils/signal_core.py — pure-Python signal cores (zero vnpy dependency).

These mirror the entry/exit rules of ``strategies/*.py`` so a trading signal can
be produced from a plain bar series *anywhere* — the standalone signal service,
CI, research notebooks — without importing the vnpy / CTP stack (Windows-only
DLLs). The same code path therefore works on Linux and in the GitHub CI runner.

Conventions replicated from vn.py's ``ArrayManager`` (which wraps TA-Lib):
  * ``sma(n)``  : simple mean of the last ``n`` closes
  * ``std(n)``  : *population* standard deviation (ddof=0), matching TA-Lib STDDEV
  * ``boll(n)`` : ``sma(n) ± dev * std(n)``
  * ``atr(n)``  : Wilder's ATR

Parity with the live strategies is asserted in ``tests/test_signal_core.py``.

Each ``replay_*`` walks the full bar series maintaining a synthetic position
(filled at every bar close — exactly like the SIGNAL_ONLY / REPLAY gateways) and
returns a :class:`SignalReplay`:
  * ``actions``  — every buy / sell / short / cover, with bar index + timestamp
  * ``position`` — target position (in lots) after each bar
  * ``stance``   — current position sign after the last bar (-1 / 0 / +1)

The side labels ("buy" / "sell" / "short" / "cover") match the ``safe_*`` calls
in ``strategies/`` so downstream readers see identical semantics across
backtest, replay and live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np

Side = Literal["buy", "sell", "short", "cover"]

# Human labels for messaging. buy/short open; sell/cover close.
SIDE_LABEL_CN: dict[str, str] = {
    "buy": "开多",
    "sell": "平多",
    "short": "开空",
    "cover": "平空",
}


@dataclass(frozen=True)
class Action:
    """One order intent emitted by a strategy on a single bar."""

    index: int
    dt: datetime | None
    side: Side
    price: float

    @property
    def label_cn(self) -> str:
        return SIDE_LABEL_CN.get(self.side, self.side)


@dataclass
class SignalReplay:
    """Result of replaying a strategy over a full bar series."""

    actions: list[Action] = field(default_factory=list)
    position: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    stance: int = 0
    indicators: dict = field(default_factory=dict)

    @property
    def last_index(self) -> int:
        return self.position.size - 1 if self.position.size else -1

    @property
    def last_bar_actions(self) -> list[Action]:
        """Actions that fired on the most recent bar (a fresh, actionable signal)."""
        if not self.actions or self.last_index < 0:
            return []
        return [a for a in self.actions if a.index == self.last_index]

    @property
    def fired_on_last_bar(self) -> bool:
        return bool(self.last_bar_actions)

    @property
    def stance_label_cn(self) -> str:
        return {1: "持多", -1: "持空", 0: "空仓"}[self.stance]


# ---------------------------------------------------------------------------
# Indicators (TA-Lib-compatible conventions)
# ---------------------------------------------------------------------------
def sma(values: np.ndarray, n: int) -> np.ndarray:
    """Trailing simple moving average; NaN until ``n`` samples are available."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if n <= 0 or values.size < n:
        return out
    csum = np.cumsum(values)
    out[n - 1] = csum[n - 1] / n
    out[n:] = (csum[n:] - csum[:-n]) / n
    return out


def rolling_std_pop(values: np.ndarray, n: int) -> np.ndarray:
    """Trailing population std (ddof=0), matching TA-Lib STDDEV used by vn.py."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if n <= 0 or values.size < n:
        return out
    for i in range(n - 1, values.size):
        out[i] = values[i - n + 1 : i + 1].std()  # ddof=0
    return out


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """Wilder's ATR (TA-Lib ATR convention): seed = mean TR over first n, then RMA."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    m = close.size
    tr = np.full(m, np.nan)
    atr = np.full(m, np.nan)
    if m == 0:
        return atr
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    if m > n:
        atr[n] = float(np.mean(tr[1 : n + 1]))
        for i in range(n + 1, m):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def _warmup(min_bars: int | None, default: int) -> int:
    return default if min_bars is None else min_bars


# ---------------------------------------------------------------------------
# Strategy replays — mirror strategies/*.py exactly
# ---------------------------------------------------------------------------
def replay_double_ma(
    close,
    *,
    fast_window: int = 10,
    slow_window: int = 20,
    fixed_size: int = 1,
    datetimes: list | None = None,
    min_bars: int | None = None,
) -> SignalReplay:
    """Mirror of strategies/double_ma_strategy.py — SMA golden/death cross."""
    close = np.asarray(close, dtype=float)
    n = close.size
    fast = sma(close, fast_window)
    slow = sma(close, slow_window)
    pos = np.zeros(n, dtype=int)
    actions: list[Action] = []
    cur = 0
    warm = _warmup(min_bars, max(50, slow_window + 5))  # ArrayManager.inited
    for i in range(n):
        if i < 1 or i < warm - 1 or np.isnan(slow[i]) or np.isnan(slow[i - 1]):
            pos[i] = cur
            continue
        cross_over = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        cross_below = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]
        price = float(close[i])
        dt = datetimes[i] if datetimes is not None else None
        if cross_over:
            if cur == 0:
                actions.append(Action(i, dt, "buy", price))
                cur = fixed_size
            elif cur < 0:
                actions.append(Action(i, dt, "cover", price))
                actions.append(Action(i, dt, "buy", price))
                cur = fixed_size
        elif cross_below:
            if cur == 0:
                actions.append(Action(i, dt, "short", price))
                cur = -fixed_size
            elif cur > 0:
                actions.append(Action(i, dt, "sell", price))
                actions.append(Action(i, dt, "short", price))
                cur = -fixed_size
        pos[i] = cur
    return SignalReplay(
        actions=actions,
        position=pos,
        stance=int(np.sign(cur)),
        indicators={"fast_ma": fast, "slow_ma": slow},
    )


def replay_donchian(
    high,
    low,
    close,
    *,
    entry_window: int = 20,
    exit_window: int = 10,
    fixed_size: int = 1,
    datetimes: list | None = None,
    min_bars: int | None = None,
) -> SignalReplay:
    """Mirror of strategies/donchian_strategy.py — channel breakout."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = close.size
    pos = np.zeros(n, dtype=int)
    actions: list[Action] = []
    cur = 0
    warm = _warmup(min_bars, max(50, entry_window + 5))
    for i in range(n):
        if i < entry_window or i < warm - 1:
            pos[i] = cur
            continue
        # Channel levels computed from the entry_window/exit_window bars BEFORE
        # the current bar (am.high_array[-entry-1:-1] in the live strategy).
        entry_up = high[i - entry_window : i].max()
        entry_dn = low[i - entry_window : i].min()
        exit_up = high[i - exit_window : i].max()
        exit_dn = low[i - exit_window : i].min()
        price = float(close[i])
        dt = datetimes[i] if datetimes is not None else None
        if cur == 0:
            if price > entry_up:
                actions.append(Action(i, dt, "buy", price))
                cur = fixed_size
            elif price < entry_dn:
                actions.append(Action(i, dt, "short", price))
                cur = -fixed_size
        elif cur > 0:
            if price < exit_dn:
                actions.append(Action(i, dt, "sell", price))
                cur = 0
                if price < entry_dn:
                    actions.append(Action(i, dt, "short", price))
                    cur = -fixed_size
        elif cur < 0:
            if price > exit_up:
                actions.append(Action(i, dt, "cover", price))
                cur = 0
                if price > entry_up:
                    actions.append(Action(i, dt, "buy", price))
                    cur = fixed_size
        pos[i] = cur
    return SignalReplay(actions=actions, position=pos, stance=int(np.sign(cur)), indicators={})


def replay_boll_reversal(
    close,
    high=None,
    low=None,
    *,
    boll_window: int = 20,
    boll_dev: float = 2.0,
    atr_window: int = 14,
    sl_atr_mult: float = 0.0,
    cooldown_bars: int = 0,
    fixed_size: int = 1,
    datetimes: list | None = None,
    min_bars: int | None = None,
) -> SignalReplay:
    """Mirror of strategies/boll_reversal_strategy.py — fade extremes to the mean."""
    close = np.asarray(close, dtype=float)
    n = close.size
    mid = sma(close, boll_window)
    std = rolling_std_pop(close, boll_window)
    up = mid + boll_dev * std
    down = mid - boll_dev * std
    use_atr = sl_atr_mult > 0 and high is not None and low is not None
    atr = wilder_atr(high, low, close, atr_window) if use_atr else None

    pos = np.zeros(n, dtype=int)
    actions: list[Action] = []
    cur = 0
    entry_price = 0.0
    cooldown = 0
    warm = _warmup(min_bars, max(50, max(boll_window, atr_window) + 5))
    for i in range(n):
        if i < warm - 1 or np.isnan(mid[i]):
            pos[i] = cur
            continue
        if cooldown > 0:
            cooldown -= 1
        price = float(close[i])
        dt = datetimes[i] if datetimes is not None else None
        if cur == 0:
            if cooldown > 0:
                pos[i] = cur
                continue
            if price > up[i]:
                actions.append(Action(i, dt, "short", price))
                cur = -fixed_size
                entry_price = price
            elif price < down[i]:
                actions.append(Action(i, dt, "buy", price))
                cur = fixed_size
                entry_price = price
        elif cur > 0:
            stopped = False
            if use_atr and entry_price > 0 and atr is not None and not np.isnan(atr[i]):
                if price <= entry_price - sl_atr_mult * atr[i]:
                    actions.append(Action(i, dt, "sell", price))
                    cur = 0
                    entry_price = 0.0
                    cooldown = cooldown_bars
                    stopped = True
            if not stopped and price > mid[i]:
                actions.append(Action(i, dt, "sell", price))
                cur = 0
                entry_price = 0.0
        elif cur < 0:
            stopped = False
            if use_atr and entry_price > 0 and atr is not None and not np.isnan(atr[i]):
                if price >= entry_price + sl_atr_mult * atr[i]:
                    actions.append(Action(i, dt, "cover", price))
                    cur = 0
                    entry_price = 0.0
                    cooldown = cooldown_bars
                    stopped = True
            if not stopped and price < mid[i]:
                actions.append(Action(i, dt, "cover", price))
                cur = 0
                entry_price = 0.0
        pos[i] = cur
    return SignalReplay(
        actions=actions,
        position=pos,
        stance=int(np.sign(cur)),
        indicators={"upper": up, "lower": down, "mid": mid},
    )


# ---------------------------------------------------------------------------
# DataFrame convenience dispatcher
# ---------------------------------------------------------------------------
#: name -> (replay fn, columns it needs beyond datetimes)
_REGISTRY: dict[str, str] = {
    "double_ma": "close",
    "donchian": "high,low,close",
    "boll_reversal": "close,high,low",
}


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def replay_dataframe(strategy: str, df, params: dict | None = None) -> SignalReplay:
    """Run a strategy replay against a pandas DataFrame.

    ``df`` must have a ``datetime`` column (or DatetimeIndex) plus the OHLC columns
    the chosen strategy needs (``open/high/low/close`` lowercase).
    """
    params = dict(params or {})
    if strategy not in _REGISTRY:
        raise ValueError(f"unknown strategy {strategy!r}; available: {available_strategies()}")

    if "datetime" in df.columns:
        datetimes = list(df["datetime"])
    else:
        datetimes = list(df.index)

    close = df["close"].to_numpy(dtype=float)
    if strategy == "double_ma":
        return replay_double_ma(close, datetimes=datetimes, **params)
    if strategy == "donchian":
        return replay_donchian(
            df["high"].to_numpy(dtype=float),
            df["low"].to_numpy(dtype=float),
            close,
            datetimes=datetimes,
            **params,
        )
    if strategy == "boll_reversal":
        return replay_boll_reversal(
            close,
            df["high"].to_numpy(dtype=float) if "high" in df.columns else None,
            df["low"].to_numpy(dtype=float) if "low" in df.columns else None,
            datetimes=datetimes,
            **params,
        )
    raise AssertionError("unreachable")  # pragma: no cover
