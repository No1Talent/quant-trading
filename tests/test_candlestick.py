"""candlestick: figure builder doesn't crash on edge inputs."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go

from research.viz.candlestick import make_candlestick

SH = ZoneInfo("Asia/Shanghai")


def _sample_df(n: int = 5) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [datetime(2024, 1, 1, 9 + i, tzinfo=SH) for i in range(n)],
        name="datetime",
    )
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "volume": [1000.0 + i * 100 for i in range(n)],
        },
        index=idx,
    )


class TestMakeCandlestick:
    def test_returns_plotly_figure(self):
        fig = make_candlestick(_sample_df(), title="test")
        assert isinstance(fig, go.Figure)

    def test_empty_df_returns_placeholder(self):
        # The dashboard polls on a timer; a transient empty window
        # must not raise — it should render a placeholder so the next
        # refresh has a chance to recover.
        fig = make_candlestick(pd.DataFrame(), title="empty")
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 0  # no traces, just an annotation

    def test_candle_and_volume_traces_present(self):
        fig = make_candlestick(_sample_df(), title="test")
        trace_types = {trace.type for trace in fig.data}
        assert "candlestick" in trace_types
        assert "bar" in trace_types

    def test_volume_color_matches_candle_direction(self):
        # Up bar at i=0 (close 101 > open 100) → red
        # Build a mixed-direction frame to lock the contract
        idx = pd.DatetimeIndex(
            [datetime(2024, 1, 1, 9, tzinfo=SH), datetime(2024, 1, 1, 10, tzinfo=SH)],
            name="datetime",
        )
        df = pd.DataFrame(
            {
                "open": [100.0, 105.0],
                "high": [106.0, 106.0],
                "low": [99.0, 100.0],
                "close": [105.0, 100.0],  # up, down
                "volume": [1000.0, 2000.0],
            },
            index=idx,
        )
        fig = make_candlestick(df, title="test")
        vol_trace = next(t for t in fig.data if t.type == "bar")
        assert vol_trace.marker.color[0] == "#d63031"  # up → red
        assert vol_trace.marker.color[1] == "#00b894"  # down → green
