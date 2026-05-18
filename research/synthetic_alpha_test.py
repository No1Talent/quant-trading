"""Sanity-check the WFA harness with synthetic data that has KNOWN alpha.

Question being asked: if DoubleMa is given a price series with clear, persistent
trends, does the harness correctly identify positive OOS Sharpe? If yes, harness
is fine and the rb 60min "no alpha" conclusion stands. If no, harness has a bug.

Approach: generate 1023 60min bars (matching rb data shape) with regime-switching
drift — strong up trend, flat, strong down trend. DoubleMa should make money on
the directional segments and small losses on flat. Import as syn1.SHFE, run the
same backtest_runner + WFA we used on rb.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research.backtest_runner import run_backtest  # noqa: E402
from research.wfa import run_wfa  # noqa: E402


def generate_trending_ohlc(
    n_bars: int = 1023,
    base_price: float = 3000.0,
    regimes: list[tuple[float, float, int]] | None = None,
    seed: int = 42,
    start: datetime | None = None,
) -> pd.DataFrame:
    """Generate OHLC bars with regime-switching drift.

    regimes: list of (drift_per_bar, sigma_per_bar, duration_in_bars)
    """
    if regimes is None:
        # default: strong up (400) → flat (200) → strong down (400)
        regimes = [
            (0.0015, 0.004, 400),
            (0.0, 0.004, 200),
            (-0.0015, 0.004, 400),
        ]
    if start is None:
        start = datetime(2024, 1, 1, 9, 0, 0)

    rng = np.random.default_rng(seed)
    prices = [base_price]
    for drift, sigma, duration in regimes:
        for _ in range(duration):
            r = drift + rng.normal(0, sigma)
            prices.append(prices[-1] * (1 + r))
    prices = prices[: n_bars + 1]

    rows = []
    dt = start
    for i in range(1, len(prices)):
        o = prices[i - 1]
        c = prices[i]
        # Synthetic high/low: extend beyond OC by a fraction of the bar's range
        spread = abs(c - o) * 0.5 + rng.uniform(0.5, 3.0)
        h = max(o, c) + spread
        lo = max(min(o, c) - spread, 1.0)
        rows.append(
            {
                "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(o, 1),
                "high": round(h, 1),
                "low": round(lo, 1),
                "close": round(c, 1),
                "volume": int(rng.uniform(100, 1000)),
                "open_interest": 0,
            }
        )
        dt += timedelta(hours=1)
    return pd.DataFrame(rows)


def import_synthetic_to_db(symbol: str, df: pd.DataFrame, csv_dir: Path) -> Path:
    from vnpy.trader.constant import Exchange, Interval

    from import_data import import_csv_to_database

    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{symbol}_60min.csv"
    df.to_csv(csv_path, index=False)

    import_csv_to_database(
        csv_path=csv_path,
        symbol=symbol,
        exchange=Exchange.SHFE,
        interval=Interval.HOUR,
        batch_size=5000,
        resume=False,
    )
    return csv_path


def main() -> int:
    from strategies.double_ma_strategy import DoubleMaStrategy

    # ---- Generate + import 4 synthetic "contracts" with different regime mixes
    synthetic_specs = {
        "syn1": [(0.0015, 0.004, 400), (0.0, 0.004, 200), (-0.0015, 0.004, 400)],  # up-flat-down
        "syn2": [(-0.0015, 0.004, 400), (0.0, 0.004, 200), (0.0015, 0.004, 400)],  # down-flat-up
        "syn3": [(0.002, 0.003, 500), (-0.002, 0.003, 500)],  # strong up→down
        "syn4": [(0.001, 0.005, 1000)],  # mild persistent up
    }

    csv_dir = REPO_ROOT / "data" / "bar"
    for sym, regimes in synthetic_specs.items():
        print(f"\n--- Generating {sym} ---")
        df = generate_trending_ohlc(
            n_bars=1023,
            base_price=3000.0,
            regimes=regimes,
            seed=hash(sym) % (2**31),
        )
        print(
            f"  price: start={df['close'].iloc[0]:.0f}  end={df['close'].iloc[-1]:.0f}  "
            f"min={df['close'].min():.0f}  max={df['close'].max():.0f}"
        )
        import_synthetic_to_db(sym, df, csv_dir)

    # ---- Smoke: single full-window DoubleMa backtest on syn1
    print("\n" + "=" * 80)
    print("SMOKE: DoubleMa(10,20) on syn1 (full range)")
    print("=" * 80)
    stats = run_backtest(
        strategy_class=DoubleMaStrategy,
        params={"fast_window": 10, "slow_window": 20, "fixed_size": 1},
        vt_symbol="syn1.SHFE",
        interval="1h",
        start=datetime(2024, 1, 1, 9, 0),
        end=datetime(2024, 5, 1, 9, 0),
        capital=1_000_000,
        rate=1e-4,
        slippage=1,
        size=10,
        pricetick=1,
    )
    print(
        f"  sharpe={stats.get('sharpe_ratio')}  return={stats.get('total_return')}%  "
        f"trades={stats.get('total_trade_count')}  max_dd={stats.get('max_ddpercent')}%"
    )

    # ---- WFA on all 4 synthetic contracts
    print("\n" + "=" * 80)
    print("WFA: DoubleMa on synthetic contracts")
    print("=" * 80)
    import logging

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    all_dfs = []
    syn_start = datetime(2024, 1, 1, 9, 0)
    for sym in synthetic_specs:
        # Each contract is 1023 hours ≈ 42 days, but our windows are calendar days
        # so use generous end date to ensure all bars are loaded.
        end = syn_start + timedelta(hours=1100)
        try:
            df = run_wfa(
                strategy_class=DoubleMaStrategy,
                param_grid={"fast_window": [5, 10, 15], "slow_window": [20, 30, 40]},
                fixed_params={"fixed_size": 1},
                vt_symbol=f"{sym}.SHFE",
                interval="1h",
                start=syn_start,
                end=end,
                train_days=25,  # bars are sequential hours → 25 days ≈ 600 bars
                test_days=12,
                step_days=12,
                metric="sharpe_ratio",
                min_trades=10,
                capital=1_000_000,
                rate=1e-4,
                slippage=1,
                size=10,
                pricetick=1,
            )
            df.insert(0, "contract", sym)
            all_dfs.append(df)
        except Exception as e:
            print(f"  {sym}: SKIPPED  {type(e).__name__}: {e}")

    if not all_dfs:
        print("All WFA runs failed.")
        return 1

    combined = pd.concat(all_dfs, ignore_index=True)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print("\nAll folds:")
    print(combined.to_string(index=False))

    oos = combined["oos_sharpe"].dropna()
    is_ = combined["is_sharpe"].dropna()
    corr = combined[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]

    print("\n" + "=" * 80)
    print("SYNTHETIC vs RB COMPARISON")
    print("=" * 80)
    print(f"  Synthetic folds:           {len(combined)}")
    print(f"  Synthetic OOS Sharpe mean: {oos.mean():+.3f}")
    print(
        f"  Synthetic OOS positive:    {(oos > 0).sum()}/{len(oos)}  ({(oos > 0).mean()*100:.0f}%)"
    )
    print(f"  Synthetic IS→OOS decay:    {is_.mean():+.3f} → {oos.mean():+.3f}")
    print(f"  Synthetic IS-OOS corr:     {corr:+.3f}")
    print()
    print("  RB benchmark (DoubleMa):")
    print("    OOS Sharpe mean:         +0.058")
    print("    OOS positive:            4/8 (50%)")
    print("    IS-OOS corr:             -0.418")
    print()
    print("INTERPRETATION:")
    # Positive OOS Sharpe is the key signal. IS-OOS corr can be negative even
    # when both are firmly positive (both means high, noise around the means).
    if oos.mean() > 1.0 and (oos > 0).all():
        print("  [OK] Harness DETECTS the synthetic alpha. WFA framework is healthy.")
        print("       The rb 60min 'no alpha' finding is real, not a harness artifact.")
    elif oos.mean() > 0:
        print("  [WEAK] Harness shows weak positive signal -- could be alpha or noise.")
        print("         Consider stronger trend regimes or longer datasets.")
    else:
        print("  [FAIL] Harness fails to detect synthetic alpha. WFA framework may have a bug.")
        print("         Investigate before trusting any prior conclusions.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
