"""M0.5: Expand data pool to 28 instruments for cross-sectional factor research.

Background. Layer ② cross-sectional factor work (alpha158-style IC evaluation)
needs a panel of >= 20 instruments to make quintile-grouped IC meaningful — 5
instruments degenerate the rank-IC into pure noise. As of 2026-05-21 we have 6
instruments with `_continuous_adj15` daily bars (AG, HC, I, AU, CU, JM, from
the H2/H4 work). This script adds 22 more across 5 sectors.

Out of scope: stock-index (IF/IC/IH) and bond (T/TF) futures. Their alpha is
macro-driven (rates, equity factors), not supply/demand. Mixing them into a
single cross-section would muddy momentum/value rank signals from commodities.

Per-instrument pipeline (identical to H2):
  1. AKShare daily continuous (XX0 symbol) → CSV under data/bar/
  2. Import to vn.py SQLite as `{sym}_continuous`
  3. H1.5 OI-based rollover detector (|ΔOI|>20% AND |gap|>0.3%) → back-adjust
  4. Re-import as `{sym}_continuous_adj15`

Failures (AKShare empty, contract too new, OI all-zero) are logged and
skipped — not fail-fast — so a single dead symbol doesn't kill the run.
Idempotent via fetch_and_save / import_csv_to_database overwrite semantics.

Output: `research/m05_data_pool_summary.csv` with per-instrument status,
bar count, year span, expected vs detected rollovers, fit ratio.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
from vnpy.trader.constant import Exchange  # noqa: E402

from research.h2_cross_instrument import (  # noqa: E402
    adjust_and_import,
    fetch_and_import_continuous,
)

# expected_rolls_per_year is only used by the H1.5 diagnose() banner. SHFE
# non-ferrous (AL/ZN/CU) have monthly deliveries → 12/yr; everything else
# defaults to ~6/yr (quarterly/bi-monthly cadence).
INSTRUMENTS: list[dict[str, Any]] = [
    # Ferrous (4 new — HC and JM already have adj15)
    {
        "sym": "rb",
        "ak_symbol": "RB0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "sector": "ferrous",
    },
    {
        "sym": "j",
        "ak_symbol": "J0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "ferrous",
    },
    {
        "sym": "sf",
        "ak_symbol": "SF0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "ferrous",
    },
    {
        "sym": "sm",
        "ak_symbol": "SM0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "ferrous",
    },
    # Non-ferrous (4 new — CU already has adj15)
    {
        "sym": "al",
        "ak_symbol": "AL0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 12,
        "sector": "nonferrous",
    },
    {
        "sym": "zn",
        "ak_symbol": "ZN0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 12,
        "sector": "nonferrous",
    },
    {
        "sym": "ni",
        "ak_symbol": "NI0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "sector": "nonferrous",
    },
    {
        "sym": "sn",
        "ak_symbol": "SN0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "sector": "nonferrous",
    },
    # Chemicals (6 new)
    {
        "sym": "ma",
        "ak_symbol": "MA0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    {
        "sym": "pp",
        "ak_symbol": "PP0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    {
        "sym": "l",
        "ak_symbol": "L0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    {
        "sym": "ta",
        "ak_symbol": "TA0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    {
        "sym": "v",
        "ak_symbol": "V0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    {
        "sym": "eg",
        "ak_symbol": "EG0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "chemicals",
    },
    # Agriculturals (6 new)
    {
        "sym": "m",
        "ak_symbol": "M0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    {
        "sym": "y",
        "ak_symbol": "Y0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    {
        "sym": "p",
        "ak_symbol": "P0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    {
        "sym": "sr",
        "ak_symbol": "SR0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    {
        "sym": "cf",
        "ak_symbol": "CF0",
        "exchange": Exchange.CZCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    {
        "sym": "a",
        "ak_symbol": "A0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "sector": "agri",
    },
    # Energy (2 new)
    {
        "sym": "sc",
        "ak_symbol": "SC0",
        "exchange": Exchange.INE,
        "expected_rolls_per_year": 6,
        "sector": "energy",
    },
    {
        "sym": "fu",
        "ak_symbol": "FU0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "sector": "energy",
    },
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("data_fetcher").setLevel(logging.WARNING)

    print(
        f"\n{'#' * 84}\n# M0.5: Data pool expansion "
        f"({len(INSTRUMENTS)} new instruments)\n{'#' * 84}"
    )
    by_sector: dict[str, int] = {}
    for inst in INSTRUMENTS:
        by_sector[inst["sector"]] = by_sector.get(inst["sector"], 0) + 1
    print("  Sector distribution: " + ", ".join(f"{k}={v}" for k, v in by_sector.items()))

    summary: list[dict[str, Any]] = []
    for i, inst in enumerate(INSTRUMENTS, 1):
        sym = inst["sym"]
        print(
            f"\n{'=' * 84}\n=== [{i}/{len(INSTRUMENTS)}] {sym.upper()} "
            f"({inst['ak_symbol']}, {inst['exchange'].value}, {inst['sector']}) "
            f"===\n{'=' * 84}"
        )

        # Phase 1: fetch + import raw continuous
        try:
            src_symbol = fetch_and_import_continuous(inst)
        except Exception as e:
            print(f"  FETCH FAILED: {type(e).__name__}: {e}")
            summary.append(
                {
                    "sym": sym,
                    "sector": inst["sector"],
                    "stage": "fetch",
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            continue

        # Phase 2: detect rollovers + back-adjust + import adj15
        try:
            adj_symbol, diag = adjust_and_import(inst, src_symbol)
        except Exception as e:
            print(f"  ADJUST FAILED: {type(e).__name__}: {e}")
            summary.append(
                {
                    "sym": sym,
                    "sector": inst["sector"],
                    "stage": "adjust",
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            continue

        summary.append(
            {
                "sym": sym,
                "sector": inst["sector"],
                "stage": "ok",
                "ok": True,
                "adj_symbol": adj_symbol,
                **diag,
            }
        )

    # Summary table
    print(f"\n\n{'=' * 100}\nM0.5 SUMMARY\n{'=' * 100}")
    df = pd.DataFrame(summary)
    ok_count = int(df["ok"].sum()) if not df.empty else 0
    fail_count = len(df) - ok_count
    print(
        f"  {ok_count}/{len(df)} OK,  {fail_count} failed.  "
        f"6 pre-existing (AG/HC/I/AU/CU/JM) → total adj15 pool = "
        f"{6 + ok_count}"
    )

    if not df.empty:
        ok_df = df[df["ok"]].copy()
        if not ok_df.empty:
            print("\n  Successful instruments:")
            cols = [
                "sym",
                "sector",
                "n_bars",
                "years",
                "n_flagged",
                "expected_rolls",
                "fit_ratio",
                "has_oi",
            ]
            cols = [c for c in cols if c in ok_df.columns]
            print(ok_df[cols].to_string(index=False))

        fail_df = df[~df["ok"]]
        if not fail_df.empty:
            print("\n  Failed instruments:")
            print(fail_df[["sym", "sector", "stage", "error"]].to_string(index=False))

    out_path = REPO_ROOT / "research" / "m05_data_pool_summary.csv"
    df.to_csv(out_path, index=False)
    print(f"\nSummary → {out_path}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
