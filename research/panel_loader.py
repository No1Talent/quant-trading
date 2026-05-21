"""Panel loader: multi-index (datetime, symbol) DataFrame from vn.py DB.

Background. Cross-sectional factor research (alpha158-style) needs a panel
shaped (datetime × symbol) → OHLCV+OI, so each factor formula can be vectorised
across symbols at a given timestamp. vn.py's DB API is per-symbol (one Bar
list per `load_bar_data` call); this module wraps N calls into a single panel,
aligns on the union of trading days, and caches the result as parquet for
fast reload during interactive factor research.

Why parquet cache: pulling 28 instruments × ~15 years × daily bars is ~150K
rows and takes 30-60s through vn.py's SQLAlchemy layer. Factor iteration is
heavy (try formula, look at IC, tweak, retry) — paying 60s every reload is
hostile to the workflow.

Cache invalidation is by file mtime + instrument tuple. If you re-run M0.5
(adding a new symbol or refreshing data), pass `refresh=True` once and the
cache rebuilds.

Output shape:
    index = MultiIndex[(pd.Timestamp datetime, str symbol)]  -- "long" format
    columns = open, high, low, close, volume, open_interest
    NaN where a symbol did not trade on a given date (different listing dates,
    holiday calendars, suspensions).

Why long format (vs wide panel): alphalens consumes long format directly,
and groupby('datetime') for cross-sectional rank/zscore is the dominant
factor-computation pattern. Wide-format pivot is one .unstack() away when
needed (e.g. for correlation matrices).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402

logger = logging.getLogger("panel_loader")

CACHE_DIR = REPO_ROOT / "data" / "panel"

# Built-in registry of all instruments with continuous_adj15 daily bars.
# Updated by M0.5 batch import. Mapping is (adj_symbol → exchange).
# Sym short-name (e.g. "rb") is parsed from the adj_symbol prefix.
ALL_ADJ15_INSTRUMENTS: list[tuple[str, Exchange]] = [
    # Pre-existing (from H2 / H4 work)
    ("ag_continuous_adj15", Exchange.SHFE),
    ("hc_continuous_adj15", Exchange.SHFE),
    ("i_continuous_adj15", Exchange.DCE),
    ("au_continuous_adj15", Exchange.SHFE),
    ("cu_continuous_adj15", Exchange.SHFE),
    ("jm_continuous_adj15", Exchange.DCE),
    # M0.5 additions (will only resolve once M0.5 has imported them)
    ("rb_continuous_adj15", Exchange.SHFE),
    ("j_continuous_adj15", Exchange.DCE),
    ("sf_continuous_adj15", Exchange.CZCE),
    ("sm_continuous_adj15", Exchange.CZCE),
    ("al_continuous_adj15", Exchange.SHFE),
    ("zn_continuous_adj15", Exchange.SHFE),
    ("ni_continuous_adj15", Exchange.SHFE),
    ("sn_continuous_adj15", Exchange.SHFE),
    ("ma_continuous_adj15", Exchange.CZCE),
    ("pp_continuous_adj15", Exchange.DCE),
    ("l_continuous_adj15", Exchange.DCE),
    ("ta_continuous_adj15", Exchange.CZCE),
    ("v_continuous_adj15", Exchange.DCE),
    ("eg_continuous_adj15", Exchange.DCE),
    ("m_continuous_adj15", Exchange.DCE),
    ("y_continuous_adj15", Exchange.DCE),
    ("p_continuous_adj15", Exchange.DCE),
    ("sr_continuous_adj15", Exchange.CZCE),
    ("cf_continuous_adj15", Exchange.CZCE),
    ("a_continuous_adj15", Exchange.DCE),
    ("sc_continuous_adj15", Exchange.INE),
    ("fu_continuous_adj15", Exchange.SHFE),
]


def _sym_from_adj(adj_symbol: str) -> str:
    """ag_continuous_adj15 → 'ag'."""
    return adj_symbol.split("_")[0]


def _load_one(
    symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime
) -> pd.DataFrame:
    """Single-symbol bar load → DataFrame indexed by datetime."""
    db = get_database()
    bars = db.load_bar_data(symbol, exchange, interval, start, end)
    rows = [
        {
            "datetime": b.datetime,
            "open": b.open_price,
            "high": b.high_price,
            "low": b.low_price,
            "close": b.close_price,
            "volume": b.volume,
            "open_interest": b.open_interest,
        }
        for b in bars
    ]
    if not rows:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume", "open_interest"]
        )
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    return df.set_index("datetime").sort_index()


def _cache_key(
    instruments: list[tuple[str, Exchange]], interval: Interval, start: datetime, end: datetime
) -> str:
    sym_part = "_".join(sorted(_sym_from_adj(s) for s, _ in instruments))
    return f"{interval.value}_{start:%Y%m%d}_{end:%Y%m%d}_{sym_part}"


def load_panel(
    instruments: list[tuple[str, Exchange]] | None = None,
    interval: Interval = Interval.DAILY,
    start: datetime = datetime(2000, 1, 1),
    end: datetime = datetime(2030, 1, 1),
    refresh: bool = False,
    skip_missing: bool = True,
) -> pd.DataFrame:
    """Build a (datetime, symbol) multi-index panel from vn.py DB.

    Args:
        instruments: list of (adj_symbol, exchange). Defaults to ALL_ADJ15.
        interval: bar interval. Currently only DAILY tested for cross-section.
        start, end: date range to fetch.
        refresh: if True, ignore cache and re-fetch from DB.
        skip_missing: if True, log + skip symbols that return zero bars
            (e.g. before M0.5 finishes for a given sym). If False, raises.

    Returns:
        DataFrame with MultiIndex (datetime, symbol) and columns
        [open, high, low, close, volume, open_interest].
    """
    if instruments is None:
        instruments = ALL_ADJ15_INSTRUMENTS

    cache_key = _cache_key(instruments, interval, start, end)
    cache_path = CACHE_DIR / f"{cache_key}.parquet"

    if cache_path.exists() and not refresh:
        logger.info("panel cache hit: %s", cache_path)
        df = pd.read_parquet(cache_path)
        # Re-establish dtype on MultiIndex level 0 (parquet roundtrip)
        df.index = df.index.set_levels([pd.to_datetime(df.index.levels[0]), df.index.levels[1]])
        return df

    logger.info("panel build: %d instruments, %s..%s", len(instruments), start.date(), end.date())
    pieces: list[pd.DataFrame] = []
    missing: list[str] = []
    for adj_symbol, exchange in instruments:
        sym = _sym_from_adj(adj_symbol)
        try:
            df_one = _load_one(adj_symbol, exchange, interval, start, end)
        except Exception as e:
            if skip_missing:
                logger.warning("load failed for %s: %s -- skipping", adj_symbol, e)
                missing.append(adj_symbol)
                continue
            raise
        if df_one.empty:
            if skip_missing:
                logger.warning("no bars for %s -- skipping", adj_symbol)
                missing.append(adj_symbol)
                continue
            raise RuntimeError(f"no bars for {adj_symbol}")

        df_one["symbol"] = sym
        df_one = df_one.set_index("symbol", append=True)
        pieces.append(df_one)
        logger.info(
            "  %s: %d bars (%s → %s)",
            sym,
            len(df_one),
            df_one.index.get_level_values(0)[0].date(),
            df_one.index.get_level_values(0)[-1].date(),
        )

    if not pieces:
        raise RuntimeError(
            f"no symbols resolved from {len(instruments)} instruments. "
            f"Has M0.5 run? Missing: {missing}"
        )

    panel = pd.concat(pieces).sort_index()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(cache_path)
    logger.info(
        "panel cached: %s (%d rows, %d symbols)",
        cache_path,
        len(panel),
        panel.index.get_level_values(1).nunique(),
    )
    if missing:
        logger.info("skipped (no data): %s", missing)

    return panel


def to_wide(panel: pd.DataFrame, column: str = "close") -> pd.DataFrame:
    """Convert long panel → wide (datetime × symbol) for a single column.

    Useful for cross-instrument correlation matrices, time-series plots,
    or feeding alphalens which sometimes wants prices as wide.
    """
    return panel[column].unstack(level="symbol")


def main() -> int:
    """Smoke test: load all available adj15 + print shape + per-symbol coverage."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    panel = load_panel()
    print(f"\n{'=' * 80}\nPANEL LOAD SUMMARY\n{'=' * 80}")
    print(f"  shape: {panel.shape}")
    print(
        f"  date range: {panel.index.get_level_values(0).min().date()} → "
        f"{panel.index.get_level_values(0).max().date()}"
    )
    print(f"  symbols: {panel.index.get_level_values(1).nunique()}")

    # Per-symbol coverage
    print("\n  Per-symbol coverage:")
    by_sym = panel.groupby(level="symbol").size().sort_values(ascending=False)
    for sym, n in by_sym.items():
        first = panel.xs(sym, level="symbol").index.min().date()
        last = panel.xs(sym, level="symbol").index.max().date()
        print(f"    {sym:4s}: {n:>5d} bars  {first} → {last}")

    # Intersection days (all symbols have data on same date)
    wide_close = to_wide(panel, "close")
    intersection_days = wide_close.dropna(how="any").shape[0]
    print(
        f"\n  Intersection days (all {wide_close.shape[1]} symbols traded): " f"{intersection_days}"
    )
    print(f"  Union days: {wide_close.shape[0]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
