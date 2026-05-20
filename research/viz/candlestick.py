"""Plotly candlestick + volume chart for the Market Intel dashboard.

Lives under research/viz/ so the same builder can be reused by future
multi-contract views and any static export (e.g. snapshot PNGs).

CN convention: red = up, green = down (opposite of US/EU).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def make_candlestick(df: pd.DataFrame, title: str = "") -> go.Figure:
    """Build OHLC candlestick + volume subplot.

    `df` must be indexed by datetime with columns at minimum
    [open, high, low, close, volume]. Empty input returns a placeholder
    figure with an annotation — the dashboard relies on this so chart
    rendering never raises during a transient empty-data window.
    """
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(
            text="No data",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(size=18, color="gray"),
        )
        fig.update_layout(title=title, height=500)
        return fig

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.75, 0.25],
    )

    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="OHLC",
            increasing_line_color="#d63031",
            decreasing_line_color="#00b894",
            increasing_fillcolor="#d63031",
            decreasing_fillcolor="#00b894",
        ),
        row=1,
        col=1,
    )

    bar_colors = [
        "#d63031" if c >= o else "#00b894" for o, c in zip(df["open"], df["close"], strict=True)
    ]
    fig.add_trace(
        go.Bar(
            x=df.index,
            y=df["volume"],
            marker_color=bar_colors,
            name="Volume",
            showlegend=False,
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        title=title,
        height=700,
        xaxis_rangeslider_visible=False,
        showlegend=False,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=40, b=10),
        template="plotly_white",
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Vol", row=2, col=1)

    return fig
