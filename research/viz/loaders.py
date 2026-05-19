"""CSV -> DataFrame, one loader per WFA result family.

Loaders normalise column names and parse dates / params so chart functions
can stay schema-agnostic. They never plot.

CSV families recognised:
  - fold-level WFA results  (wfa_results_*.csv)               -> load_fold_results
  - daily PnL panel         (h4_ensemble_daily_panel*.csv)    -> load_daily_panel
  - summary aggregate       (*_summary.csv, wfa_summary_*)    -> load_summary
  - carry attribution daily (h6_carry_attribution.csv)        -> load_carry_attribution
  - ensemble fold results   (wfa_results_*_ensemble.csv)      -> load_ensemble_results
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd

# Per-fold WFA file: columns at minimum include (fold, oos_sharpe, is_sharpe, ...).
_FOLD_REQUIRED = {"fold", "oos_sharpe", "is_sharpe"}


def load_fold_results(path: Path) -> pd.DataFrame:
    """Load a per-fold WFA results CSV.

    Auto-detects optional prefix columns (`sym`, `strategy`, `contract`,
    `source`) and turns date columns into pd.Timestamp.
    """
    df = pd.read_csv(path)
    missing = _FOLD_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing required columns {sorted(missing)}")

    for col in ("train_start", "train_end", "test_start", "test_end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    if "best_params" in df.columns:
        df["best_params_parsed"] = df["best_params"].apply(_safe_parse_params)

    # Choose a group key for plots that need to split by symbol/strategy/source.
    for candidate in ("sym", "source", "strategy", "contract"):
        if candidate in df.columns and df[candidate].nunique() > 1:
            df.attrs["group_col"] = candidate
            break
    else:
        df.attrs["group_col"] = None

    df.attrs["source_path"] = str(path)
    return df


def load_daily_panel(path: Path) -> pd.DataFrame:
    """Load a daily PnL panel (date index, one column per instrument).

    Values are daily net_pnl (not cumulative). The chart layer takes cumsum.
    NaN cells (union-style panels with non-overlapping ranges) are kept as
    NaN so they don't pollute cumulative sums.
    """
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise ValueError(f"{path.name}: expected a 'date' column")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df.attrs["source_path"] = str(path)
    return df


def load_summary(path: Path) -> pd.DataFrame:
    """Load a heterogeneous summary CSV (one row per strategy / variant / bucket).

    Schemas vary by hypothesis — the chart layer renders whatever columns it
    finds, so the frame is returned as-is. Only columns explicitly known to
    hold dates are parsed; the prior `endswith("_min"/"_max")` heuristic
    swallowed `ValueError` on numeric columns like `oos_sharpe_min`.
    """
    df = pd.read_csv(path)
    for col in ("date_min", "date_max", "start", "end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df.attrs["source_path"] = str(path)
    return df


def load_carry_attribution(path: Path) -> pd.DataFrame:
    """Load h6_carry_attribution.csv (date, net_pnl, days_since_rollover, bucket)."""
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df.attrs["source_path"] = str(path)
    return df


def load_ensemble_results(path: Path) -> pd.DataFrame:
    """Load wfa_results_*_ensemble.csv.

    The IS-Sharpe / OOS-Sharpe columns are stringified numpy lists — parse
    them into Python floats for downstream plotting.
    """
    df = pd.read_csv(path)
    for col in ("is_sharpes", "oos_sharpes_indiv", "oos_returns_indiv_pct"):
        if col in df.columns:
            df[col + "_list"] = df[col].apply(_parse_np_list)
    for col in ("train_end", "test_end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df.attrs["source_path"] = str(path)
    return df


def _safe_parse_params(s: object) -> dict | None:
    if not isinstance(s, str):
        return None
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return None


_NP_FLOAT_RE = re.compile(r"np\.float64\(([-\d.eE+]+)\)")


def _parse_np_list(s: object) -> list[float] | None:
    """Parse strings like '[np.float64(3.05), np.float64(2.59)]' -> [3.05, 2.59]."""
    if not isinstance(s, str):
        return None
    nums = _NP_FLOAT_RE.findall(s)
    if nums:
        return [float(x) for x in nums]
    try:
        parsed = ast.literal_eval(s)
        if isinstance(parsed, list):
            return [float(x) for x in parsed]
    except (ValueError, SyntaxError):
        return None
    return None
