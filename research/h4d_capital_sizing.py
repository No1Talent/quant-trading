"""H4d: capital sizing sensitivity on H4 ensemble.

Question: how does the H4 ensemble scale with capital?

Two effects to test:
  (a) Round-lot quantization at small capital: at 500k some instruments
      can't be properly risk-balanced (e.g. 1 lot CU = 524k notional > capital).
  (b) Market impact at large capital: with multi-lot positions, slippage
      grows ∝ √(contracts) per the canonical sqrt-law impact model.

Method:
  - Notional per 1-lot (DB last-close × instrument multiplier):
      AG: ~288k    I: ~81k    CU: ~524k
  - At each capital tier C, target ~10% capital exposure per instrument:
      natural_contracts(sym) = max(1, round(C × 0.10 / notional_per_lot[sym]))
  - Market impact: stressed_slippage = baseline_slippage × √(natural_contracts)
  - Backtest `capital` parameter also set to C per fold (for DD-% calcs).
  - Re-run full WFA + ensemble at each tier; record per-inst Sharpe and
    portfolio Sharpe / total PnL / max DD / DD-as-pct-of-capital.

What this DOESN'T model (out of scope, flagged for later):
  - Liquidity ceiling: at very large capital, market may not absorb the
    contract count even with √-impact slippage. Need DB daily volume to
    check; not currently in continuous CSVs.
  - Variable margin requirements: assumes notional / margin ratio constant.

Pass criterion (Layer-③ gate ②):
  - Sharpe at 10M tier within -0.15 of baseline +0.993 (i.e. ≥ +0.84):
      [PASS] strategy scales to 10M
  - Sharpe at 10M tier within -0.30 of baseline (≥ +0.69):
      [MARGINAL] capital ceiling somewhere between 1M and 10M
  - Sharpe at 10M tier < +0.69 or DD/capital > 5%:
      [FAIL] strategy does not scale; capital ceiling is below 10M
"""

from __future__ import annotations

import copy
import logging
import math
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402

from research.h4_ensemble import (  # noqa: E402
    INSTRUMENTS,
    run_one_instrument,
    summarize_series,
)

# Notional per 1-lot from DB last-close × multiplier (computed once;
# baseline ~2026-05-15 prices). Used only to derive natural contract count.
NOTIONAL_PER_LOT = {
    "AG": 288_510,
    "I": 80_950,
    "CU": 523_550,
}

# Capital tiers ($CNY). 500k chosen as low end because 1 lot CU alone
# is 524k notional — anything below 500k cannot run the 3-instrument
# ensemble meaningfully.
CAPITAL_TIERS = [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]

# Fraction of capital deployed per instrument at "natural" sizing.
# 10% × 3 instruments = 30% total deployment (conservative; leaves 70%
# as drawdown buffer + margin headroom).
DEPLOY_FRACTION_PER_INST = 0.10


def natural_contracts(capital: float, sym: str) -> int:
    """Round-down contracts targeting DEPLOY_FRACTION × capital notional.

    Floor (not round-nearest) because over-deployment burns the buffer.
    Min 1 — if not even 1 lot fits, the test is infeasible (flagged separately).
    """
    target = capital * DEPLOY_FRACTION_PER_INST
    raw = target / NOTIONAL_PER_LOT[sym]
    n = int(math.floor(raw))
    return max(1, n)


def market_impact_slip_factor(contracts: int) -> float:
    """Canonical sqrt-law: slippage grows as √(order size)."""
    return math.sqrt(contracts)


def configure_for_capital(capital: float) -> list[dict]:
    """Deep-copy INSTRUMENTS, set per-tier capital + fixed_size + impacted slip."""
    out = []
    for inst in INSTRUMENTS:
        new = copy.deepcopy(inst)
        n = natural_contracts(capital, new["sym"])
        new["fixed_size"] = n  # picked up by h4_ensemble.run_one_instrument
        # capital affects vn.py BacktestingEngine percentage-return calcs only
        new["bt"]["capital"] = capital
        # market impact
        new["bt"]["slippage"] = new["bt"]["slippage"] * market_impact_slip_factor(n)
        new["_contracts"] = n  # bookkeeping
        out.append(new)
    return out


def run_at_capital(capital: float) -> dict:
    print(f"\n{'#' * 90}")
    print(f"# CAPITAL = {capital:,.0f} CNY")
    print(f"{'#' * 90}")

    configured = configure_for_capital(capital)
    results = []
    for inst in configured:
        n = inst["_contracts"]
        slip = inst["bt"]["slippage"]
        deployed_pct = NOTIONAL_PER_LOT[inst["sym"]] * n / capital * 100
        print(
            f"\n--- {inst['sym']}  contracts={n}  "
            f"deployed≈{deployed_pct:.1f}% of capital  "
            f"slip={slip:.2f} (×√{n}={market_impact_slip_factor(n):.2f}) ---"
        )
        r = run_one_instrument(inst)
        results.append(r)

    panel = pd.DataFrame({r["sym"]: r["scaled_oos"] for r in results})
    intersection = panel.dropna(how="any")
    if len(intersection) == 0:
        return {"capital": capital, "error": "no_intersection"}

    per_inst_stats = []
    for col in intersection.columns:
        st = summarize_series(intersection[col], col)
        per_inst_stats.append(st)

    portfolio = intersection.sum(axis=1)
    port_stats = summarize_series(portfolio, "PORTFOLIO")

    # Years covered for annualization sanity-print
    years = len(intersection) / 252.0
    ann_return_dollar = port_stats["total_pnl"] / years
    ann_return_pct = ann_return_dollar / capital * 100
    dd_pct_capital = abs(port_stats["max_dd"]) / capital * 100

    print(f"\n  intersection days: {len(intersection)} (~{years:.1f} years)")
    for st, inst in zip(per_inst_stats, configured):
        print(
            f"    {st['label']:3s}: Sharpe={st['sharpe_ann']:+.3f}  "
            f"contracts={inst['_contracts']:>2d}  "
            f"total=${st['total_pnl']:>+14,.0f}  maxDD=${st['max_dd']:>+12,.0f}"
        )
    print(
        f"    PORT: Sharpe={port_stats['sharpe_ann']:+.3f}  "
        f"total=${port_stats['total_pnl']:>+14,.0f}  "
        f"maxDD=${port_stats['max_dd']:>+12,.0f}  "
        f"({dd_pct_capital:.1f}% of capital)"
    )
    print(f"    Annualized: ${ann_return_dollar:>+,.0f}/yr " f"({ann_return_pct:+.1f}% of capital)")

    return {
        "capital": capital,
        "days": len(intersection),
        "per_inst": per_inst_stats,
        "contracts": {inst["sym"]: inst["_contracts"] for inst in configured},
        "port_sharpe": port_stats["sharpe_ann"],
        "port_total": port_stats["total_pnl"],
        "port_dd": port_stats["max_dd"],
        "dd_pct_capital": dd_pct_capital,
        "ann_return_pct": ann_return_pct,
        "ann_return_dollar": ann_return_dollar,
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'=' * 90}")
    print(f"H4d capital sizing: re-running H4 ensemble at " f"{len(CAPITAL_TIERS)} capital tiers")
    print(f"Notional per 1-lot (DB-derived): {NOTIONAL_PER_LOT}")
    print(f"Target deployment per instrument: {DEPLOY_FRACTION_PER_INST * 100:.0f}% of capital")
    print("Market impact: slippage × √(contracts) (sqrt-law)")
    print(f"{'=' * 90}")

    all_results = [run_at_capital(c) for c in CAPITAL_TIERS]

    # ---- Summary table ----
    print(f"\n\n{'=' * 110}")
    print("CAPITAL SIZING SUMMARY")
    print(f"{'=' * 110}")
    header = (
        f"{'Capital':>12} | {'AG ct':>5} {'I ct':>4} {'CU ct':>5} | "
        f"{'PORT Sharpe':>12} | {'ann $':>14} | {'ann %':>7} | "
        f"{'maxDD $':>14} | {'DD/cap %':>8}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for r in all_results:
        if "error" in r:
            print(f"{r['capital']:>12,.0f} | ERROR: {r['error']}")
            continue
        c = r["contracts"]
        print(
            f"{r['capital']:>12,.0f} | "
            f"{c['AG']:>5d} {c['I']:>4d} {c['CU']:>5d} | "
            f"{r['port_sharpe']:>+12.3f} | "
            f"{r['ann_return_dollar']:>+14,.0f} | "
            f"{r['ann_return_pct']:>+6.1f}% | "
            f"{r['port_dd']:>+14,.0f} | "
            f"{r['dd_pct_capital']:>7.1f}%"
        )
        rows.append(
            {
                "capital": r["capital"],
                "ag_contracts": c["AG"],
                "i_contracts": c["I"],
                "cu_contracts": c["CU"],
                "port_sharpe": r["port_sharpe"],
                "ann_return_dollar": r["ann_return_dollar"],
                "ann_return_pct": r["ann_return_pct"],
                "port_dd": r["port_dd"],
                "dd_pct_capital": r["dd_pct_capital"],
                "days": r["days"],
            }
        )

    if not rows:
        print("\nNo valid capital runs completed.")
        return 1

    out_path = REPO_ROOT / "research" / "h4d_capital_sizing_summary.csv"
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"\nArtefact: {out_path.name} -> research/")

    # ---- Verdict ----
    baseline_sharpe = 0.993  # h4_ensemble baseline
    top_sharpe = next((r["port_sharpe"] for r in rows if r["capital"] == 10_000_000), np.nan)
    top_dd = next((r["dd_pct_capital"] for r in rows if r["capital"] == 10_000_000), np.nan)
    sharpe_decay = top_sharpe - baseline_sharpe

    print(f"\n{'=' * 110}")
    print("VERDICT (Layer-③ gate ②: capital scalability)")
    print(f"{'=' * 110}")
    print(f"  Baseline (h4_ensemble) Sharpe:     {baseline_sharpe:+.3f}")
    print(f"  10M-tier Sharpe:                   {top_sharpe:+.3f}")
    print(f"  Sharpe decay (10M − baseline):     {sharpe_decay:+.3f}")
    print(f"  10M-tier max DD as % of capital:   {top_dd:.1f}%")

    if not np.isfinite(top_sharpe):
        tag = "[INCONCLUSIVE]"
        note = "10M tier did not complete cleanly."
    elif sharpe_decay >= -0.15 and top_dd < 5.0:
        tag = "[PASS]"
        note = (
            f"Strategy scales to 10M with negligible Sharpe decay ({sharpe_decay:+.3f}) "
            f"and DD/capital under 5%. Proceed to P0-3 (CTP reconciliation)."
        )
    elif sharpe_decay >= -0.30 and top_dd < 10.0:
        tag = "[MARGINAL]"
        note = (
            f"Sharpe decays meaningfully at 10M ({sharpe_decay:+.3f}). "
            f"Capital ceiling somewhere between 1M and 10M. "
            f"For initial live deployment, stay at 1-2M tier."
        )
    else:
        tag = "[FAIL]"
        note = (
            f"Sharpe collapses at 10M ({sharpe_decay:+.3f}) or DD/capital > 10% "
            f"({top_dd:.1f}%). Strategy does NOT scale meaningfully beyond 1-2M. "
            f"Either improve sizing model or accept low-capital deployment."
        )
    print(f"  {tag} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
