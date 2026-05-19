"""Dispatch table: CSV filename -> (loader, [chart factories], label).

Adding a new hypothesis = one entry here. No renderer code changes.

The renderer matches each CSV against `REGISTRY` in order and uses the
first matching spec. Filenames not in the registry are skipped (and
logged) — explicit > implicit.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from matplotlib.figure import Figure

from research.viz import charts, loaders

LoaderFn = Callable[[Path], pd.DataFrame]
ChartFn = Callable[..., Figure | None]


@dataclass(frozen=True)
class Spec:
    name: str  # short label, used in output filenames
    pattern: str  # fnmatch pattern against filename
    loader: LoaderFn
    chart_fns: tuple[ChartFn, ...]
    title_prefix: str = ""
    extra: dict = field(default_factory=dict)


# Order matters: more-specific patterns first.
REGISTRY: tuple[Spec, ...] = (
    # ---------- Family D: summaries (must match before fold patterns) ----------
    Spec(
        name="h4_ensemble_summary",
        pattern="h4_ensemble_summary.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H4 ensemble summary",
    ),
    Spec(
        name="h4b_sensitivity_summary",
        pattern="h4b_sensitivity_summary.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H4b sensitivity",
    ),
    Spec(
        name="wfa_summary_h2",
        pattern="wfa_summary_h2.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H2 cross-instrument summary",
    ),
    Spec(
        name="wfa_summary_h5",
        pattern="wfa_summary_h5.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H5 ratio-adjust summary",
    ),
    Spec(
        name="h6b_summary",
        pattern="h6b_summary.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H6b carry strategy verdict",
    ),
    Spec(
        name="h6c_summary",
        pattern="h6c_summary.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H6c hybrid strategy verdict",
    ),
    Spec(
        name="h6_segment_summary",
        pattern="h6_segment_summary.csv",
        loader=loaders.load_summary,
        chart_fns=(charts.fig_summary_table,),
        title_prefix="H6 segment summary",
    ),
    # ---------- Family C: daily PnL panels ----------
    Spec(
        name="h4_ensemble_daily_panel",
        pattern="h4_ensemble_daily_panel.csv",
        loader=loaders.load_daily_panel,
        chart_fns=(charts.fig_cumulative_pnl, charts.fig_drawdown),
        title_prefix="H4 ensemble (intersection)",
    ),
    Spec(
        name="h4_ensemble_daily_panel_union",
        pattern="h4_ensemble_daily_panel_union.csv",
        loader=loaders.load_daily_panel,
        chart_fns=(charts.fig_cumulative_pnl, charts.fig_drawdown),
        title_prefix="H4 ensemble (union, zero-fill)",
    ),
    # ---------- Family E: carry attribution ----------
    Spec(
        name="h6_carry_attribution",
        pattern="h6_carry_attribution.csv",
        loader=loaders.load_carry_attribution,
        chart_fns=(charts.fig_bucket_pnl_bars,),
        title_prefix="H6 carry attribution",
    ),
    # ---------- Family B: ensemble fold results ----------
    Spec(
        name="rb_boll_ensemble",
        pattern="wfa_results_rb_boll_ensemble.csv",
        loader=loaders.load_ensemble_results,
        chart_fns=(charts.fig_ensemble_vs_indiv,),
        title_prefix="RB Bollinger ensemble",
    ),
    # ---------- Family A: per-fold WFA (catch-all, last) ----------
    Spec(
        name="wfa_fold_results",
        pattern="wfa_results_*.csv",
        loader=loaders.load_fold_results,
        chart_fns=(
            charts.fig_fold_sharpe_bars,
            charts.fig_is_vs_oos_scatter,
            charts.fig_oos_metrics_panel,
            charts.fig_param_winners,
        ),
    ),
)


def dispatch(path: Path) -> Spec | None:
    """Return the first matching spec for `path.name`, or None."""
    for spec in REGISTRY:
        if fnmatch.fnmatch(path.name, spec.pattern):
            return spec
    return None
