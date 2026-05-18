"""Programmatic wrapper around vn.py's BacktestingEngine.

Single function `run_backtest(...)` returns a stats dict — suitable for being
called in loops (WFA, parameter sweeps, slippage sensitivity).

Notification side effects are silenced via NullNotifier per docs/development.md §4.4.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# vn.py's bundled empyrical (used by calculate_statistics) references np.NINF,
# which was removed in NumPy 2.0. Shim it before importing vn.py.
import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

from vnpy.trader.constant import Interval  # noqa: E402
from vnpy_ctabacktester.engine import BacktestingEngine  # noqa: E402

from utils.notifier import NullNotifier, set_notifier  # noqa: E402

logger = logging.getLogger("backtest_runner")


_INTERVAL_MAP = {
    "1m": Interval.MINUTE,
    "1h": Interval.HOUR,
    "1d": Interval.DAILY,
}


def run_backtest(
    strategy_class: type,
    params: dict,
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    capital: float = 1_000_000,
    rate: float = 1e-4,
    slippage: float = 1,
    size: float = 10,
    pricetick: float = 1,
    return_daily_df: bool = False,
):
    """Run a single backtest and return the statistics dict.

    Returns vn.py's standard stats: sharpe_ratio, max_ddpercent, total_return,
    annual_return, total_trade_count, etc. Keys with NaN are normalized to None.

    If `return_daily_df=True`, returns (stats, daily_df) where daily_df is
    vn.py's per-day DataFrame (columns include net_pnl, balance, return). Used
    by ensemble research to combine instrument curves at the daily level.
    """
    set_notifier(NullNotifier())  # belt-and-suspenders, in case strategy imports notifier

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=vt_symbol,
        interval=_INTERVAL_MAP[interval],
        start=start,
        end=end,
        rate=rate,
        slippage=slippage,
        size=size,
        pricetick=pricetick,
        capital=capital,
    )
    engine.add_strategy(strategy_class, params)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    stats = engine.calculate_statistics(output=False)

    # Normalize NaN → None for clean JSON / table output downstream
    import math

    stats = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in stats.items()}

    if return_daily_df:
        daily_df = getattr(engine, "daily_df", None)
        return stats, daily_df
    return stats


def main() -> int:
    """Smoke test: run DoubleMa on rb2410 60min with defaults."""
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    stats = run_backtest(
        strategy_class=DoubleMaStrategy,
        params={"fast_window": 10, "slow_window": 20, "fixed_size": 1},
        vt_symbol="rb2410.SHFE",
        interval="1h",
        start=datetime(2024, 1, 22),
        end=datetime(2024, 10, 15),
        capital=1_000_000,
        rate=1e-4,
        slippage=1,
        size=10,  # rb contract multiplier
        pricetick=1,
    )

    print("\n" + "=" * 60)
    print("BACKTEST STATS: DoubleMa(10,20) on rb2410.SHFE 1h")
    print("=" * 60)
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"  {k:30s} {v:>15.4f}")
        else:
            print(f"  {k:30s} {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
