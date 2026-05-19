"""H4c: cost-sensitivity stress test on H4 ensemble.

Question: if our cost assumptions (rate, slippage) are too optimistic,
how does the H4 ensemble's OOS Sharpe degrade?

Method: re-run the full WFA + ensemble pipeline at cost multipliers
{1x (baseline), 2x, 5x}. At each multiplier BOTH `rate` and `slippage`
per instrument are scaled — this matters because the WFA grid selector
may pick different (fast, slow) params when costs are higher (slower
crossovers fire fewer signals → lower turnover → less cost drag).

Why full re-run rather than a post-hoc penalty: the saved daily PnL
panel reflects the params chosen under baseline costs. At stressed
costs the optimizer would pick different params; a post-hoc penalty
on baseline params would be a strictly pessimistic bound. Full re-WFA
gives the realistic answer.

Output:
  - research/h4c_slippage_stress_summary.csv (per-mult rows)
  - stdout: per-instrument + portfolio Sharpe at each mult, plus
    a pass/marginal/fail verdict for the Layer-③ gate.

Expected runtime: ~10-30 min (3 mults × 3 instruments × ~15 folds × 10
backtests per fold). Watch logs/h4c.log if redirected.

Pass criterion (Layer-③ gate ①):
  - port Sharpe @ 5x >= +0.5  → [PASS]  cost is not the dominant risk
  - port Sharpe @ 5x  > 0     → [MARGINAL] edge eroded but survives
  - port Sharpe @ 5x <= 0     → [FAIL]  need tighter cost modelling
                                          before any live consideration
"""

from __future__ import annotations

import copy
import logging
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

STRESS_MULTIPLIERS = [1.0, 2.0, 5.0]


def stress_instruments(mult: float) -> list[dict]:
    """Deep-copy INSTRUMENTS, scale rate + slippage by mult."""
    out = []
    for inst in INSTRUMENTS:
        new = copy.deepcopy(inst)
        new["bt"]["rate"] = new["bt"]["rate"] * mult
        new["bt"]["slippage"] = new["bt"]["slippage"] * mult
        out.append(new)
    return out


def run_at_mult(mult: float) -> dict:
    """Full H4 ensemble at one cost multiplier; returns summary dict."""
    print(f"\n{'#' * 90}")
    print(f"# STRESS MULT = {mult}x  (rate ×{mult}, slippage ×{mult})")
    print(f"{'#' * 90}")

    stressed = stress_instruments(mult)
    results = []
    for inst in stressed:
        print(
            f"\n--- {inst['sym']} ({inst['source']}) "
            f"rate={inst['bt']['rate']:.2e}  slip={inst['bt']['slippage']} ---"
        )
        r = run_one_instrument(inst)
        results.append(r)
        df = r["wfa_df"]
        oos = df["oos_sharpe"].dropna()
        print(
            f"  WFA: {len(df)} folds, "
            f"OOS Sharpe mean={oos.mean():+.3f}, pos%={(oos > 0).mean() * 100:.1f}"
        )

    panel = pd.DataFrame({r["sym"]: r["scaled_oos"] for r in results})
    intersection = panel.dropna(how="any")
    if len(intersection) == 0:
        return {"mult": mult, "error": "no_intersection"}

    per_inst_stats = []
    for col in intersection.columns:
        st = summarize_series(intersection[col], col)
        per_inst_stats.append(st)

    portfolio = intersection.sum(axis=1)
    port_stats = summarize_series(portfolio, "PORTFOLIO")

    print(f"\n  intersection days: {len(intersection)}")
    for st in per_inst_stats:
        print(
            f"    {st['label']:3s}: Sharpe={st['sharpe_ann']:+.3f}  "
            f"total={st['total_pnl']:>+12,.0f}  maxDD={st['max_dd']:>+12,.0f}"
        )
    print(
        f"    PORT: Sharpe={port_stats['sharpe_ann']:+.3f}  "
        f"total={port_stats['total_pnl']:>+12,.0f}  maxDD={port_stats['max_dd']:>+12,.0f}"
    )

    return {
        "mult": mult,
        "days": len(intersection),
        "per_inst": per_inst_stats,
        "port_sharpe": port_stats["sharpe_ann"],
        "port_total": port_stats["total_pnl"],
        "port_dd": port_stats["max_dd"],
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'=' * 90}")
    print(
        f"H4c slippage stress: re-running H4 ensemble at " f"{len(STRESS_MULTIPLIERS)} cost levels"
    )
    print(
        f"Baseline costs per instrument: "
        f"{[(i['sym'], i['bt']['rate'], i['bt']['slippage']) for i in INSTRUMENTS]}"
    )
    print(f"{'=' * 90}")

    all_results = [run_at_mult(m) for m in STRESS_MULTIPLIERS]

    # ---- Summary table ----
    print(f"\n\n{'=' * 100}")
    print("STRESS SUMMARY")
    print(f"{'=' * 100}")
    header = (
        f"{'Mult':>6} | {'AG Sharpe':>10} | {'I Sharpe':>10} | "
        f"{'CU Sharpe':>10} | {'PORT Sharpe':>12} | {'PORT PnL':>14} | {'PORT DD':>14}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for r in all_results:
        if "error" in r:
            print(f"{r['mult']:>5.1f}x | ERROR: {r['error']}")
            continue
        d = {s["label"]: s["sharpe_ann"] for s in r["per_inst"]}
        ag, i_, cu = d.get("AG", np.nan), d.get("I", np.nan), d.get("CU", np.nan)
        print(
            f"{r['mult']:>5.1f}x | {ag:>+10.3f} | {i_:>+10.3f} | {cu:>+10.3f} | "
            f"{r['port_sharpe']:>+12.3f} | {r['port_total']:>+14,.0f} | {r['port_dd']:>+14,.0f}"
        )
        rows.append(
            {
                "mult": r["mult"],
                "ag_sharpe": ag,
                "i_sharpe": i_,
                "cu_sharpe": cu,
                "port_sharpe": r["port_sharpe"],
                "port_total": r["port_total"],
                "port_dd": r["port_dd"],
                "days": r["days"],
            }
        )

    if not rows:
        print("\nNo valid stress runs completed.")
        return 1

    out_path = REPO_ROOT / "research" / "h4c_slippage_stress_summary.csv"
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"\nArtefact: {out_path.name} -> research/")

    # ---- Verdict ----
    five_x = next((r["port_sharpe"] for r in rows if r["mult"] == 5.0), np.nan)
    base = next((r["port_sharpe"] for r in rows if r["mult"] == 1.0), np.nan)

    print(f"\n{'=' * 100}")
    print("VERDICT (Layer-③ gate ①: cost sensitivity)")
    print(f"{'=' * 100}")
    print(f"  Baseline (1x) portfolio Sharpe:  {base:+.3f}")
    print(f"  Stressed (5x) portfolio Sharpe:  {five_x:+.3f}")
    print(f"  Sharpe decay:                    {(five_x - base):+.3f}")

    if not np.isfinite(five_x):
        tag = "[INCONCLUSIVE]"
        note = "5x run did not complete cleanly."
    elif five_x >= 0.5:
        tag = "[PASS]"
        note = (
            "Cost assumptions are NOT the dominant risk. "
            "Proceed to P0-2 (capital sizing sensitivity)."
        )
    elif five_x > 0:
        tag = "[MARGINAL]"
        note = (
            "Edge survives 5x cost stress but is materially eroded. "
            "Investigate per-instrument decay; consider tighter execution modelling "
            "before live consideration."
        )
    else:
        tag = "[FAIL]"
        note = (
            "Cost assumptions ARE the dominant risk. The +0.88 baseline is fragile. "
            "Must improve slippage / fill modelling (e.g. next-bar-open fills, "
            "partial-fill simulation) before any live consideration."
        )
    print(f"  {tag} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
