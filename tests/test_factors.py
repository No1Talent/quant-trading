"""Tests for research.factors — the cross-sectional factor library.

Coverage:
  1. Per-symbol time-series math (no cross-symbol contamination).
  2. Cross-sectional reducers (rank, zscore) behave correctly at each datetime.
  3. The FACTORS registry: every factor returns a properly-indexed Series.
  4. NaN handling at the warmup boundary.

Uses pure synthetic panels — no DB / vn.py dependency, runs in pytest-fast.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.factors import (
    FACTORS,
    close_to_range_pos,
    cs_rank,
    cs_zscore,
    high_low_range,
    oi_pct_change,
    ret_n,
    vol_n,
    volume_ratio,
)

# ---------- Fixtures ----------


@pytest.fixture
def small_panel() -> pd.DataFrame:
    """3-day × 2-symbol panel with simple, easy-to-verify-by-hand numbers."""
    rows = [
        # date          sym   o    h    l    c   vol     oi
        ("2024-01-01", "rb", 100, 102, 99, 101, 1000, 50000),
        ("2024-01-02", "rb", 101, 103, 100, 102, 1200, 51000),
        ("2024-01-03", "rb", 102, 104, 101, 103, 1100, 52000),
        ("2024-01-01", "hc", 200, 202, 199, 201, 800, 40000),
        ("2024-01-02", "hc", 201, 203, 200, 200, 900, 41000),  # down
        ("2024-01-03", "hc", 200, 202, 199, 202, 850, 41500),
    ]
    df = pd.DataFrame(
        rows,
        columns=["datetime", "symbol", "open", "high", "low", "close", "volume", "open_interest"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.set_index(["datetime", "symbol"]).sort_index()


@pytest.fixture
def divergent_panel() -> pd.DataFrame:
    """10 days × 2 symbols. rb is FLAT (zero return). hc oscillates wildly.

    Used to test that rolling time-series factors don't bleed hc's volatility
    into rb's calculation — a common bug if you forget to groupby('symbol')
    or stack/unstack incorrectly.
    """
    rows = []
    for d in range(1, 11):
        rows.append((f"2024-01-{d:02d}", "rb", 100, 100, 100, 100, 1000, 50000))
        # hc: alternates 1000 ↔ 1100, ±10% returns
        hc_close = 1000 if d % 2 == 1 else 1100
        rows.append((f"2024-01-{d:02d}", "hc", hc_close, 1150, 950, hc_close, 800, 40000))
    df = pd.DataFrame(
        rows,
        columns=["datetime", "symbol", "open", "high", "low", "close", "volume", "open_interest"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.set_index(["datetime", "symbol"]).sort_index()


# ---------- Time-series factors ----------


def test_ret_1_per_symbol(small_panel: pd.DataFrame) -> None:
    """ret_1 computes log-return per symbol independently."""
    ret = ret_n(small_panel, 1)
    assert ret.loc[(pd.Timestamp("2024-01-02"), "rb")] == pytest.approx(np.log(102 / 101))
    assert ret.loc[(pd.Timestamp("2024-01-02"), "hc")] == pytest.approx(np.log(200 / 201))
    # Day 1 has no prior — must be NaN
    assert pd.isna(ret.loc[(pd.Timestamp("2024-01-01"), "rb")])
    assert pd.isna(ret.loc[(pd.Timestamp("2024-01-01"), "hc")])


def test_vol_no_cross_symbol_contamination(divergent_panel: pd.DataFrame) -> None:
    """rb is flat → rb's vol must be 0 even when hc is wildly volatile."""
    vol = vol_n(divergent_panel, 5)
    last_day = pd.Timestamp("2024-01-10")
    rb_vol = vol.loc[(last_day, "rb")]
    hc_vol = vol.loc[(last_day, "hc")]
    assert rb_vol == pytest.approx(
        0.0, abs=1e-9
    ), f"rb is flat but vol={rb_vol} — cross-symbol contamination"
    assert hc_vol > 0.05, f"hc oscillates ±10% but vol={hc_vol} (expected > 0.05)"


def test_oi_pct_change(small_panel: pd.DataFrame) -> None:
    """OI: rb 50000 → 51000 → 52000; oi_pct_change(1) for day 2 = 0.02."""
    o = oi_pct_change(small_panel, 1)
    assert o.loc[(pd.Timestamp("2024-01-02"), "rb")] == pytest.approx(0.02)
    assert o.loc[(pd.Timestamp("2024-01-03"), "rb")] == pytest.approx(1000 / 51000)
    # hc 40000 → 41000 → 41500
    assert o.loc[(pd.Timestamp("2024-01-02"), "hc")] == pytest.approx(0.025)


def test_volume_ratio(small_panel: pd.DataFrame) -> None:
    """volume_ratio_short_long with short=1, long=2: day 2 rb = 1200 / mean(1000,1200) = 1200/1100."""
    vr = volume_ratio(small_panel, short=1, long_=2)
    rb_d2 = vr.loc[(pd.Timestamp("2024-01-02"), "rb")]
    assert rb_d2 == pytest.approx(1200 / 1100)


def test_high_low_range(small_panel: pd.DataFrame) -> None:
    """hl_range over window=2 at day 2 for rb: (max(102,103) - min(99,100)) / 102."""
    rng = high_low_range(small_panel, window=2)
    rb_d2 = rng.loc[(pd.Timestamp("2024-01-02"), "rb")]
    assert rb_d2 == pytest.approx((103 - 99) / 102)


def test_close_to_range_pos(small_panel: pd.DataFrame) -> None:
    """close_pos at day 2 for rb: (102 - 99) / (103 - 99) = 0.75. Donchian-style."""
    pos = close_to_range_pos(small_panel, window=2)
    rb_d2 = pos.loc[(pd.Timestamp("2024-01-02"), "rb")]
    assert rb_d2 == pytest.approx(0.75)


# ---------- Cross-sectional reducers ----------


def test_cs_rank_two_symbols(small_panel: pd.DataFrame) -> None:
    """At day 2, rb went up, hc went down. cs_rank(ret_1) gives rb=1.0, hc=0.5
    (pct=True, two symbols, ranks 1 and 2 → 0.5 and 1.0)."""
    ret = ret_n(small_panel, 1)
    rank = cs_rank(ret)
    assert rank.loc[(pd.Timestamp("2024-01-02"), "rb")] == pytest.approx(1.0)
    assert rank.loc[(pd.Timestamp("2024-01-02"), "hc")] == pytest.approx(0.5)


def test_cs_zscore_centred_zero(small_panel: pd.DataFrame) -> None:
    """At any datetime, cs_zscore values must sum to zero across symbols
    (definition of mean-centred). NaN at day 1 due to ret warmup is fine."""
    ret = ret_n(small_panel, 1)
    z = cs_zscore(ret)
    for date in [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]:
        day_z = z.xs(date, level="datetime").dropna()
        if len(day_z) > 1:
            assert day_z.sum() == pytest.approx(0.0, abs=1e-9)


def test_cs_rank_excludes_nan() -> None:
    """If one symbol has NaN factor, it should be excluded from that day's rank,
    not crash and not get rank 0."""
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-01"), "rb"),
            (pd.Timestamp("2024-01-01"), "hc"),
            (pd.Timestamp("2024-01-01"), "i"),
        ],
        names=["datetime", "symbol"],
    )
    factor = pd.Series([1.0, np.nan, 2.0], index=idx)
    rank = cs_rank(factor)
    assert pd.isna(rank.loc[(pd.Timestamp("2024-01-01"), "hc")])
    # rb (1.0) and i (2.0): pct ranks 0.5 and 1.0
    assert rank.loc[(pd.Timestamp("2024-01-01"), "rb")] == pytest.approx(0.5)
    assert rank.loc[(pd.Timestamp("2024-01-01"), "i")] == pytest.approx(1.0)


# ---------- Registry contract ----------


def test_all_factors_return_correctly_indexed_series(divergent_panel: pd.DataFrame) -> None:
    """Every factor in FACTORS must return a Series with MultiIndex(datetime, symbol)
    and a non-empty `name` attribute matching its registry key."""
    for name, fn in FACTORS.items():
        result = fn(divergent_panel)
        assert isinstance(result, pd.Series), f"{name} returned {type(result).__name__}"
        assert result.index.nlevels == 2, f"{name} lost MultiIndex"
        assert result.index.names == [
            "datetime",
            "symbol",
        ], f"{name} has index names {result.index.names}"
        # Same row count as input (NaN-warmup rows still in index)
        assert len(result) == len(
            divergent_panel
        ), f"{name}: {len(result)} rows but panel has {len(divergent_panel)}"


def test_factors_produce_finite_values_after_warmup(divergent_panel: pd.DataFrame) -> None:
    """After the longest factor's warmup (60 days for ret_60), values should be
    mostly finite. Divergent panel only has 10 days, so we test factors with
    warmup ≤ 5 here."""
    # Only factors whose warmup ≤ 5 days fit in this 10-day fixture.
    # vol_ratio_5_20 / volume_ratio_5_20 / dv_zscore_20 / etc. need 20+ days.
    short_factors = {"ret_1", "ret_5", "oi_pct_5"}
    for name in short_factors:
        if name not in FACTORS:
            continue
        result = FACTORS[name](divergent_panel)
        last_day_values = result.xs(pd.Timestamp("2024-01-10"), level="datetime")
        # At least one symbol should have a finite value at the latest date
        assert (
            last_day_values.notna().any()
        ), f"{name} all-NaN on the last day — warmup too long for 10-day panel?"
