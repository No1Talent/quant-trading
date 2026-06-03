"""M3.8: Layer-③ cost-sensitivity stress on the M3.7 vol-targeted ensemble.

Mirrors h4c_slippage_stress.py (full re-run at cost multipliers {1,2,5}× so the
PWF grid selector adapts to higher costs — a post-hoc penalty would be a strictly
pessimistic bound) but on the M3.7 pipeline: PWF + causal daily vol-target.

Adds two things h4c lacked:
  1. Re-deflation (PSR(0)/MinTRL) at each cost level — does the +0.782 stay
     95%-significant when costs are 5×?
  2. The AG+CU vs AG+I+CU split — M3.7 showed I is a drag (-0.122 even
     vol-targeted), so we report both portfolios and AG-solo.

Layer-③ gate (cost sensitivity): portfolio Sharpe @5×  ≥ +0.5 PASS / > 0
MARGINAL / ≤ 0 FAIL.

Output: research/m38_vt_slippage_summary.csv  (gitignored)
Runtime: ~10-30 min (3 mults × 3 instruments × PWF folds × 9-combo grid).
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
import pandas as pd  # noqa: E402

from research.m35_h4_ensemble_cpcv import INSTRUMENTS, summarize_series  # noqa: E402
from research.m37_ensemble_vol_target import (  # noqa: E402
    VT_MAX_LEVERAGE,
    VT_WINDOW,
    run_one_instrument_vt,
)
from research.overfit_stats import (  # noqa: E402
    min_track_record_length,
    probabilistic_sharpe_ratio,
    sharpe_skew_kurt,
)

STRESS_MULTIPLIERS = [1.0, 2.0, 5.0]
TRADING_DAYS = 252.0


def stress_instruments(mult: float) -> list[dict]:
    out = []
    for inst in INSTRUMENTS:
        new = copy.deepcopy(inst)
        new["bt"]["rate"] *= mult
        new["bt"]["slippage"] *= mult
        out.append(new)
    return out


def _deflated_sharpe(series: pd.Series) -> dict:
    sr, skew, kurt, n = sharpe_skew_kurt(series.values)
    trl = min_track_record_length(sr, skew, kurt, 0.0, 0.95)
    return {
        "sharpe": sr * np.sqrt(TRADING_DAYS),
        "kurt": kurt,
        "psr0": probabilistic_sharpe_ratio(sr, n, skew, kurt, 0.0),
        "min_trl": trl,
        "n": n,
        "significant": bool(np.isfinite(trl) and trl <= n),
    }


def run_at_mult(mult: float) -> dict:
    print(f"\n{'#' * 88}\n# STRESS {mult}×  (rate ×{mult}, slippage ×{mult})\n{'#' * 88}")
    results = [run_one_instrument_vt(inst) for inst in stress_instruments(mult)]
    panel = pd.DataFrame({r["sym"]: r["vt_oos"] for r in results}).dropna(how="any")
    if len(panel) == 0:
        return {"mult": mult, "error": "no_intersection"}

    per = {c: summarize_series(panel[c], c)["sharpe_ann"] for c in panel.columns}
    full = _deflated_sharpe(panel.sum(axis=1))  # AG+I+CU
    agcu = (
        _deflated_sharpe(panel[["AG", "CU"]].sum(axis=1))
        if {"AG", "CU"} <= set(panel.columns)
        else {}
    )
    ag_solo = _deflated_sharpe(panel["AG"]) if "AG" in panel.columns else {}

    for c in panel.columns:
        print(f"    {c:3s} Sharpe={per[c]:+.3f}")
    print(
        f"    PORT(AG+I+CU) Sharpe={full['sharpe']:+.3f} kurt={full['kurt']:.1f} "
        f"PSR0={full['psr0']:.3f} MinTRL={full['min_trl']:,.0f}/{full['n']} "
        f"{'SIG' if full['significant'] else 'short'}"
    )
    if agcu:
        print(
            f"    PORT(AG+CU)   Sharpe={agcu['sharpe']:+.3f} kurt={agcu['kurt']:.1f} "
            f"PSR0={agcu['psr0']:.3f} MinTRL={agcu['min_trl']:,.0f}/{agcu['n']} "
            f"{'SIG' if agcu['significant'] else 'short'}"
        )
    if ag_solo:
        print(
            f"    AG-solo       Sharpe={ag_solo['sharpe']:+.3f} PSR0={ag_solo['psr0']:.3f} "
            f"MinTRL={ag_solo['min_trl']:,.0f}/{ag_solo['n']} "
            f"{'SIG' if ag_solo['significant'] else 'short'}"
        )

    return {
        "mult": mult,
        "ag_sharpe": per.get("AG", np.nan),
        "i_sharpe": per.get("I", np.nan),
        "cu_sharpe": per.get("CU", np.nan),
        "port_full_sharpe": full["sharpe"],
        "port_full_psr0": full["psr0"],
        "port_full_mintrl": full["min_trl"],
        "port_full_sig": full["significant"],
        "port_agcu_sharpe": agcu.get("sharpe", np.nan),
        "port_agcu_psr0": agcu.get("psr0", np.nan),
        "port_agcu_sig": agcu.get("significant", False),
        "ag_solo_sharpe": ag_solo.get("sharpe", np.nan),
        "ag_solo_psr0": ag_solo.get("psr0", np.nan),
        "ag_solo_sig": ag_solo.get("significant", False),
        "days": full["n"],
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("cpcv").setLevel(logging.WARNING)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'=' * 88}")
    print(
        f"M3.8 VT-ensemble cost stress @ {STRESS_MULTIPLIERS} (vol-target {VT_WINDOW}d/{VT_MAX_LEVERAGE}×)"
    )
    print(f"{'=' * 88}")

    rows = [r for m in STRESS_MULTIPLIERS if "error" not in (r := run_at_mult(m))]
    if not rows:
        print("No valid stress runs.")
        return 1

    pd.DataFrame(rows).to_csv(REPO_ROOT / "research" / "m38_vt_slippage_summary.csv", index=False)

    print(f"\n{'=' * 88}\nSTRESS SUMMARY (Sharpe; SIG = 95%-significant for >0)\n{'=' * 88}")
    print(f"  {'mult':>5} {'AG':>7} {'I':>7} {'CU':>7} {'AG+I+CU':>9} {'AG+CU':>8} {'AG-solo':>8}")
    for r in rows:
        print(
            f"  {r['mult']:>4.0f}× {r['ag_sharpe']:>+7.3f} {r['i_sharpe']:>+7.3f} "
            f"{r['cu_sharpe']:>+7.3f} {r['port_full_sharpe']:>+9.3f}"
            f"{'*' if r['port_full_sig'] else ' '} {r['port_agcu_sharpe']:>+7.3f}"
            f"{'*' if r['port_agcu_sig'] else ' '} {r['ag_solo_sharpe']:>+7.3f}"
            f"{'*' if r['ag_solo_sig'] else ' '}"
        )

    base = next((r for r in rows if r["mult"] == 1.0), None)
    five = next((r for r in rows if r["mult"] == 5.0), None)
    print(f"\n{'=' * 88}\nVERDICT (Layer-③ cost gate)\n{'=' * 88}")
    for label, key in [
        ("AG+I+CU", "port_full_sharpe"),
        ("AG+CU", "port_agcu_sharpe"),
        ("AG-solo", "ag_solo_sharpe"),
    ]:
        b = base[key] if base else np.nan
        f = five[key] if five else np.nan
        tag = "[PASS]" if f >= 0.5 else "[MARGINAL]" if f > 0 else "[FAIL]"
        print(f"  {label:9s}: 1× {b:+.3f} → 5× {f:+.3f}  (decay {f - b:+.3f})  {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
