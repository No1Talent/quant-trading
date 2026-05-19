"""Interactive WFA dashboard — Streamlit shim over research/viz/.

Run:
    streamlit run streamlit_app.py

This file owns ONLY UI orchestration:
  - file discovery via research.viz.registry
  - sidebar filters (hypothesis, group, fold range, date range)
  - st.pyplot() of charts produced by research.viz.charts

All data loading and plotting live in research/viz/ so the same code
powers the static CLI renderer (scripts/render_wfa_report.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.viz import themes  # noqa: E402
from research.viz.registry import REGISTRY, Spec, dispatch  # noqa: E402

themes.apply_theme()


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

RESEARCH_DIR = REPO_ROOT / "research"


@st.cache_data(show_spinner=False)
def list_csvs() -> list[Path]:
    return sorted(p for p in RESEARCH_DIR.glob("*.csv") if dispatch(p) is not None)


@st.cache_data(show_spinner=False)
def load_csv(path_str: str, loader_name: str) -> pd.DataFrame:
    """Cached loader call. We key by (path, loader_name) so cache invalidates
    if the registry remaps a file to a different family.
    """
    path = Path(path_str)
    spec = dispatch(path)
    if spec is None or spec.loader.__name__ != loader_name:
        raise RuntimeError(
            f"Registry mismatch for {path.name}: expected loader {loader_name}, "
            f"got {None if spec is None else spec.loader.__name__}"
        )
    return spec.loader(path)


# --------------------------------------------------------------------------- #
# Filter widgets (only meaningful for fold-level frames)
# --------------------------------------------------------------------------- #


def fold_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Sidebar widgets for per-fold WFA result frames. Returns filtered copy."""
    out = df

    group_col = df.attrs.get("group_col")
    if group_col and df[group_col].nunique() > 1:
        groups = sorted(df[group_col].dropna().unique().tolist())
        picked = st.sidebar.multiselect(
            f"Filter by {group_col}",
            groups,
            default=groups,
            help="Show only these symbols / strategies / sources",
        )
        if picked:
            out = out[out[group_col].isin(picked)]

    if "fold" in df.columns and len(df) > 1:
        fmin, fmax = int(df["fold"].min()), int(df["fold"].max())
        if fmin != fmax:
            lo, hi = st.sidebar.slider("Fold range", fmin, fmax, (fmin, fmax))
            out = out[(out["fold"] >= lo) & (out["fold"] <= hi)]

    if "test_start" in df.columns:
        ts_min, ts_max = df["test_start"].min(), df["test_start"].max()
        if pd.notna(ts_min) and pd.notna(ts_max) and ts_min != ts_max:
            sel = st.sidebar.date_input(
                "OOS test-start window",
                value=(ts_min.date(), ts_max.date()),
                min_value=ts_min.date(),
                max_value=ts_max.date(),
            )
            # Streamlit returns a length-1 tuple between the first and second
            # click of a range picker; treat that as "no filter this rerun".
            if isinstance(sel, tuple) and len(sel) == 2:
                d_lo, d_hi = sel
            else:
                d_lo, d_hi = ts_min.date(), ts_max.date()
            mask = (out["test_start"] >= pd.Timestamp(d_lo)) & (
                out["test_start"] <= pd.Timestamp(d_hi)
            )
            out = out[mask]

    # Preserve attrs across filter (pandas drops them on boolean indexing).
    out.attrs.update(df.attrs)
    return out


def daily_panel_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Date-range slider for daily PnL panels."""
    if df.empty:
        return df
    d_min, d_max = df.index.min().date(), df.index.max().date()
    if d_min == d_max:
        return df
    sel = st.sidebar.date_input(
        "Date window",
        value=(d_min, d_max),
        min_value=d_min,
        max_value=d_max,
    )
    # Streamlit returns a length-1 tuple between the first and second click
    # of a range picker; treat that as "no filter this rerun".
    if isinstance(sel, tuple) and len(sel) == 2:
        d_lo, d_hi = sel
    else:
        d_lo, d_hi = d_min, d_max
    out = df.loc[pd.Timestamp(d_lo) : pd.Timestamp(d_hi)]
    out.attrs.update(df.attrs)
    return out


# Map loader function name → filter widget. Loaders not listed get no filter.
FILTERS = {
    "load_fold_results": fold_filters,
    "load_daily_panel": daily_panel_filters,
}


# --------------------------------------------------------------------------- #
# Per-CSV rendering
# --------------------------------------------------------------------------- #


def _quick_stats(df: pd.DataFrame, spec: Spec) -> None:
    """Show 3-4 headline metrics at the top of the page."""
    cols = st.columns(4)
    if spec.loader.__name__ == "load_fold_results":
        n = len(df)
        oos = df["oos_sharpe"].dropna() if "oos_sharpe" in df.columns else pd.Series(dtype=float)
        cols[0].metric("Folds (after filter)", n)
        cols[1].metric("OOS Sharpe mean", f"{oos.mean():+.3f}" if not oos.empty else "—")
        cols[2].metric(
            "OOS positive %",
            f"{(oos > 0).mean() * 100:.1f}%" if not oos.empty else "—",
        )
        if {"is_sharpe", "oos_sharpe"}.issubset(df.columns):
            paired = df[["is_sharpe", "oos_sharpe"]].dropna()
            corr_label = (
                f"{paired['is_sharpe'].corr(paired['oos_sharpe']):+.2f}" if len(paired) > 1 else "—"
            )
        else:
            corr_label = "—"
        cols[3].metric("IS→OOS corr", corr_label)
    elif spec.loader.__name__ == "load_daily_panel":
        instruments = [c for c in df.columns if c.lower() != "date"]
        if instruments:
            # For union panels, rows where every instrument is NaN mean nothing
            # was trading that day — keep them out of the Sharpe denominator so
            # non-overlapping series don't dilute the portfolio metric.
            inst_df = df[instruments].dropna(how="all")
            portfolio_daily = inst_df.fillna(0).sum(axis=1)
            total = portfolio_daily.sum()
            equity = portfolio_daily.cumsum()
            dd = (equity - equity.cummax()).min()
            sharpe_ann = (
                portfolio_daily.mean() / portfolio_daily.std() * (252**0.5)
                if portfolio_daily.std() > 0
                else 0
            )
            cols[0].metric("Days", len(inst_df))
            cols[1].metric("Total PnL", f"{total:,.0f}")
            cols[2].metric("Max DD", f"{dd:,.0f}")
            cols[3].metric("Portfolio Sharpe (ann)", f"{sharpe_ann:+.3f}")


def render_page(csv_path: Path) -> None:
    spec = dispatch(csv_path)
    if spec is None:
        st.error(f"No spec for {csv_path.name}")
        return

    st.markdown(f"### `{csv_path.name}` &nbsp;·&nbsp; *{spec.title_prefix or spec.name}*")

    df = load_csv(str(csv_path), spec.loader.__name__)

    filter_fn = FILTERS.get(spec.loader.__name__)
    if filter_fn is not None:
        st.sidebar.subheader("Filters")
        df_view = filter_fn(df)
    else:
        df_view = df

    _quick_stats(df_view, spec)

    if df_view.empty:
        st.warning("No rows after filtering.")
        return

    title_base = spec.title_prefix or csv_path.stem
    for chart_fn in spec.chart_fns:
        chart_label = chart_fn.__name__.replace("fig_", "").replace("_", " ")
        title = f"{title_base} — {chart_label}"
        try:
            fig = chart_fn(df_view, title=title)
        except Exception as e:  # noqa: BLE001
            st.error(f"{chart_fn.__name__} failed: {e}")
            continue
        if fig is None:
            continue
        st.pyplot(fig, clear_figure=True)

    with st.expander("Raw data"):
        st.dataframe(df_view, use_container_width=True)
        st.caption(f"Source: `{csv_path.relative_to(REPO_ROOT).as_posix()}`")


# --------------------------------------------------------------------------- #
# App entry
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(
        page_title="WFA Research Dashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("WFA Research Dashboard")
    st.caption(
        "Interactive view over `research/wfa_results_*.csv`. "
        "Same loaders + charts as the static renderer "
        "(`scripts/render_wfa_report.py`)."
    )

    csvs = list_csvs()
    if not csvs:
        st.error(f"No registry-matched CSVs found in {RESEARCH_DIR}")
        return

    # Group CSVs by registry spec for a tidier sidebar.
    by_spec: dict[str, list[Path]] = {}
    for csv in csvs:
        spec = dispatch(csv)
        assert spec is not None
        by_spec.setdefault(spec.name, []).append(csv)

    st.sidebar.header("Select CSV")
    spec_names = sorted(by_spec.keys())
    default_idx = spec_names.index("wfa_fold_results") if "wfa_fold_results" in by_spec else 0
    spec_name = st.sidebar.selectbox("Family", options=spec_names, index=default_idx)
    csv_path = st.sidebar.selectbox(
        "File",
        options=by_spec[spec_name],
        format_func=lambda p: p.name,
    )

    render_page(csv_path)

    st.sidebar.divider()
    st.sidebar.caption(f"Registry: {len(REGISTRY)} specs · {len(csvs)} CSVs matched")


if __name__ == "__main__":
    main()
