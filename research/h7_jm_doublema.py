"""H7: Does DoubleMa daily produce a tradeable edge on JM (焦煤, coking coal)?

Why JM. User-flagged instrument of interest. JM is in the DCE black-series
family (same as I / iron ore, which is the confirmed-in-H4 member with raw
continuous → OOS Sharpe +0.445). If JM behaves like I, we get a 4th candidate
for the H4-style ensemble. If it doesn't, we learn something about how local
the black-series momentum signal really is.

Reuses the H2 pipeline end-to-end:
  1. AkShare JM0 → CSV → DB symbol `jm_continuous` (raw)
  2. H1.5 OI-based detector → back-adjust → DB symbol `jm_continuous_adj15`
  3. DoubleMa WFA on BOTH variants using the H4-comparable spec
     (train=700d / test=250d / step=250d, grid {fast: 10/20/30, slow: 40/60/100},
     min_trades=5)
  4. Side-by-side raw vs adj15 — per [[project_backadjust_universality]] the
     additive H1.5 adjustment is unsafe for contango instruments. JM is a known
     contango carrier (storage costs + supply seasonality), so we EXPECT raw
     to win or tie; if adj15 clearly wins it would be a surprise worth tracing.

Reference points (from H4 ensemble research):
  - I (raw):  OOS Sharpe +0.445, the contango sibling — closest analogue
  - AG (adj15): OOS Sharpe +0.424 — different family (precious metal)
  - CU (adj15): OOS Sharpe +0.568 — different family (base metal)

Decision rule:
  Sharpe > +0.30 AND pos% > 60  → [PROMOTE]   add to SIGNAL_ONLY watch list
  Sharpe > +0.15 AND pos% > 55  → [PARTIAL]   keep researching, no live yet
  otherwise                      → [NO_EDGE]   JM not tradeable with DoubleMa daily

Prerequisites:
  - AkShare reachable (script will fetch on first run; subsequent runs reuse DB).
  - vn.py database initialised (any prior import_data.py run satisfies this).

Run:
  python research/h7_jm_doublema.py

The first run is ~2-3 minutes for the fetch + back-adjust + 2× WFA.
Subsequent runs skip the fetch (set FORCE_REFETCH=True to override).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402

from research.h2_cross_instrument import (  # noqa: E402
    adjust_and_import,
    fetch_and_import_continuous,
)

# JM (DCE coking coal): 60 t/lot, tick 0.5 元/t, fees ~6e-5 (same family as I).
# Slippage 0.5 = 1 tick per side, consistent with I in H2/H4.
JM: dict[str, Any] = {
    "sym": "jm",
    "ak_symbol": "JM0",
    "exchange": Exchange.DCE,
    "expected_rolls_per_year": 6,  # DCE black series, 1/5/9 main contracts
    "start": datetime(2013, 4, 1),  # JM listed 2013-03-22
    "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=60, pricetick=0.5),
}

END_DATE = datetime(2026, 5, 15)

TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

FORCE_REFETCH = False


def _db_has_symbol(symbol: str, exchange: Exchange, interval: Interval) -> bool:
    db = get_database()
    overviews = db.get_bar_overview()
    for ov in overviews:
        if ov.symbol == symbol and ov.exchange == exchange and ov.interval == interval:
            return ov.count > 0
    return False


def ensure_data() -> tuple[str, str]:
    """Make sure both raw and adj15 are in the DB. Returns (raw_symbol, adj_symbol)."""
    raw_symbol = f"{JM['sym']}_continuous"
    adj_symbol = f"{JM['sym']}_continuous_adj15"

    raw_ready = _db_has_symbol(raw_symbol, JM["exchange"], Interval.DAILY)
    adj_ready = _db_has_symbol(adj_symbol, JM["exchange"], Interval.DAILY)

    if FORCE_REFETCH or not raw_ready:
        print(f"\n--- Fetching {JM['ak_symbol']} → {raw_symbol} ---")
        fetch_and_import_continuous(JM)
    else:
        print(f"\n--- {raw_symbol} already in DB, skipping fetch ---")

    if FORCE_REFETCH or not adj_ready:
        print(f"\n--- Building {adj_symbol} via H1.5 OI back-adjust ---")
        adjust_and_import(JM, raw_symbol)
    else:
        print(f"\n--- {adj_symbol} already in DB, skipping back-adjust ---")

    return raw_symbol, adj_symbol


def run_wfa_variant(db_symbol: str, variant_label: str) -> dict[str, Any]:
    """DoubleMa WFA on one continuous variant (raw or adj15)."""
    from research.wfa_rb_batch import run_batch
    from strategies.double_ma_strategy import DoubleMaStrategy

    vt_symbol = f"{db_symbol}.{JM['exchange'].value}"
    df = run_batch(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        label=f"DoubleMa/JM ({variant_label})",
        contracts=[(vt_symbol, JM["start"], END_DATE)],
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        bt_kwargs=JM["bt"],
        interval="1d",
        min_trades=MIN_TRADES,
    )

    if df.empty:
        return {"variant": variant_label, "df": df, "folds": 0}

    oos = df["oos_sharpe"].dropna()
    return {
        "variant": variant_label,
        "df": df,
        "folds": len(df),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_sharpe_min": float(oos.min()),
        "oos_sharpe_max": float(oos.max()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "total_oos_return_pct": float(df["oos_return_pct"].sum()),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]),
        "is_sharpe_mean": float(df["is_sharpe"].dropna().mean()),
    }


def classify(res: dict[str, Any]) -> str:
    if res["folds"] == 0:
        return "[FAIL_RUN]"
    s = res["oos_sharpe_mean"]
    pos = res["oos_positive_pct"]
    if s > 0.30 and pos > 60:
        return "[PROMOTE]"
    if s > 0.15 and pos > 55:
        return "[PARTIAL]"
    if s > 0:
        return "[WEAK]"
    return "[NO_EDGE]"


def _print_variant(res: dict[str, Any]) -> None:
    v = res["variant"]
    if res["folds"] == 0:
        print(f"  [{v}] FAIL — no folds produced")
        return
    print(
        f"  [{v}] {classify(res)}  folds={res['folds']}  "
        f"OOS Sharpe mean={res['oos_sharpe_mean']:+.3f}  "
        f"median={res['oos_sharpe_median']:+.3f}  "
        f"pos%={res['oos_positive_pct']:.1f}  "
        f"IS→OOS decay={res['oos_sharpe_mean'] - res['is_sharpe_mean']:+.3f}  "
        f"corr={res['is_oos_corr']:+.3f}  "
        f"total={res['total_oos_return_pct']:+.2f}%"
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)
    logging.getLogger("data_fetcher").setLevel(logging.WARNING)

    print(f"\n{'#' * 84}\n# H7: DoubleMa daily on JM (焦煤) — raw vs adj15\n{'#' * 84}")
    print(f"  Grid: {DM_GRID}")
    print(f"  WFA: train={TRAIN_DAYS}d / test={TEST_DAYS}d / step={STEP_DAYS}d")
    print("  Reference (H4): I raw +0.445, AG adj15 +0.424, CU adj15 +0.568")

    raw_symbol, adj_symbol = ensure_data()

    print(f"\n{'=' * 84}\n=== Phase A: WFA on {raw_symbol} (raw continuous) ===\n{'=' * 84}")
    raw_res = run_wfa_variant(raw_symbol, "raw")

    print(f"\n{'=' * 84}\n=== Phase B: WFA on {adj_symbol} (H1.5 back-adjusted) ===\n{'=' * 84}")
    adj_res = run_wfa_variant(adj_symbol, "adj15")

    print(f"\n\n{'=' * 84}\nH7 RESULTS\n{'=' * 84}")
    _print_variant(raw_res)
    _print_variant(adj_res)

    out_dir = REPO_ROOT / "research"
    if raw_res["folds"] > 0:
        raw_res["df"].to_csv(out_dir / "wfa_results_h7_jm_raw.csv", index=False)
    if adj_res["folds"] > 0:
        adj_res["df"].to_csv(out_dir / "wfa_results_h7_jm_adj15.csv", index=False)

    # Verdict — pick the better-performing variant for the SIGNAL_ONLY recommendation.
    print(f"\n{'=' * 84}\nVERDICT\n{'=' * 84}")
    candidates = [r for r in (raw_res, adj_res) if r["folds"] > 0]
    if not candidates:
        print("  Both variants failed to produce folds. Check data + windows.")
        return 1

    best = max(candidates, key=lambda r: r["oos_sharpe_mean"])
    tag = classify(best)
    print(f"  Best variant: {best['variant']}  {tag}")

    if tag == "[PROMOTE]":
        print(f"  → JM ({best['variant']}) is tradeable with DoubleMa daily.")
        print("     Next step: pick the most-frequent IS-winning params from")
        print(f"     wfa_results_h7_jm_{best['variant']}.csv and add a DoubleMaStrategy")
        print(
            f"     instance for jm_continuous{'_adj15' if best['variant'] == 'adj15' else ''}.DCE"
        )
        print("     to cta_strategy_setting.json. Launch with QUANT_MODE=SIGNAL_ONLY.")
        print("     Observe ≥10 trading days before considering LIVE.")
    elif tag == "[PARTIAL]":
        print("  → JM shows a weak signal. Not yet ready for SIGNAL_ONLY.")
        print("     Consider: alt parameter grid (longer slow window for contango drift),")
        print("     or try Boll/Donchian on JM (different signal families).")
    else:
        print("  → JM does not produce a tradeable DoubleMa daily edge.")
        print("     Don't add to live stack. Consider mean-reversion (Boll) or skip JM.")

    # Universality cross-check
    if raw_res["folds"] > 0 and adj_res["folds"] > 0:
        delta = adj_res["oos_sharpe_mean"] - raw_res["oos_sharpe_mean"]
        print(f"\n  raw vs adj15 OOS Sharpe delta: {delta:+.3f}")
        if delta < -0.20:
            print("  → confirms [[project_backadjust_universality]]: JM is contango-affected,")
            print("     adj15 hurts. Use raw continuous in production.")
        elif delta > 0.20:
            print("  → SURPRISE: adj15 materially beats raw on JM. Worth tracing why")
            print("     (run h1_5_calendar_rollover diagnostic on jm_continuous).")
        else:
            print("  → raw ≈ adj15 on JM. Either variant viable; prefer raw for simplicity.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
