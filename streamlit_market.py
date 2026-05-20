"""Market Intel dashboard — real-time K线 + LLM analysis (WIP).

Run:
    streamlit run streamlit_market.py

Third dashboard, alongside:
  - streamlit_app.py  = offline WFA research browser
  - streamlit_live.py = ops observation (sync_data / flags / logs)

This one is the trader's market view: single-contract K线 in M2;
multi-contract watchlist (M3) and LLM commentary (M4-M5) come next.

Data source: vn.py SQLite DB (history) + AkShare polling (latest tail),
composed in utils/market_data.py. See the project memory file
project_market_intel_dashboard_plan.md for milestones and constraints.

Hard constraint: LLM commentary, when added, is "market observation"
not "investment advice". The systematic Layer ② signals own execution
decisions; this dashboard is a research aid.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.viz.candlestick import make_candlestick  # noqa: E402
from utils.market_data import BarRequest, get_recent_bars  # noqa: E402


@st.cache_data(show_spinner=False, ttl=25)
def _cached_bars(vt_symbol: str, interval: str, n_bars: int, use_realtime: bool):
    """Cache result for slightly less than the default 30s refresh so a
    refresh always pulls fresh AkShare data, but rapid re-renders inside
    the same window (sidebar twiddling) don't re-hit the network."""
    return get_recent_bars(BarRequest(vt_symbol, interval, n_bars, use_realtime))


def _quick_metrics(df) -> None:
    if df.empty:
        st.warning("无数据返回 — 检查 vt_symbol 是否存在于 DB 中，或 AkShare 是否可达。")
        return

    last = df.iloc[-1]
    first = df.iloc[0]
    change = last["close"] - first["close"]
    change_pct = change / first["close"] * 100 if first["close"] else 0

    cols = st.columns(4)
    cols[0].metric("最新", f"{last['close']:.2f}")
    cols[1].metric("窗口涨跌", f"{change:+.2f}", f"{change_pct:+.2f}%")
    cols[2].metric("窗口最高", f"{df['high'].max():.2f}")
    cols[3].metric("窗口最低", f"{df['low'].min():.2f}")


def main() -> None:
    st.set_page_config(
        page_title="Market Intel — K线",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Market Intel")
    st.caption(
        "vn.py DB 历史 + AkShare 实时合流。**观察工具，非投资建议。** "
        "系统化信号由 Layer ② 策略负责，本面板仅做形态/上下文观察。"
    )

    st.sidebar.header("合约")
    vt_symbol = st.sidebar.text_input(
        "vt_symbol",
        value="rb2410.SHFE",
        help="格式: <symbol>.<exchange>，如 rb2410.SHFE / ag2412.SHFE / jm2501.DCE",
    )
    interval = st.sidebar.radio(
        "周期",
        options=["1m", "60m", "1d"],
        index=1,
        horizontal=True,
    )
    n_bars = st.sidebar.slider("显示根数", min_value=50, max_value=500, value=200, step=50)
    use_realtime = st.sidebar.toggle(
        "AkShare 实时合流",
        value=True,
        help="关闭则只读 DB（更快，但停留在最后一次 import_data 的时间点）",
    )

    st.sidebar.divider()
    st.sidebar.header("刷新")
    refresh_s = st.sidebar.slider(
        "自动刷新 (秒)",
        min_value=10,
        max_value=120,
        value=30,
        step=10,
        help="默认 30s — AkShare 实际延迟约 15s，更高频意义不大",
    )

    try:
        df = _cached_bars(vt_symbol, interval, n_bars, use_realtime)
    except ValueError as e:
        st.error(f"参数错误: {e}")
        return
    except Exception as e:  # noqa: BLE001  — surface AkShare/DB transients to the UI
        st.error(f"加载失败: {e}")
        return

    _quick_metrics(df)

    fig = make_candlestick(
        df,
        title=f"{vt_symbol} · {interval} · last {len(df)} bars",
    )
    st.plotly_chart(fig, use_container_width=True)

    if not df.empty:
        st.caption(
            f"数据源: **{df.attrs.get('source', '?')}** · "
            f"行数: {len(df)} · "
            f"最后 bar: `{df.index[-1]}` · "
            f"渲染于: {datetime.now().strftime('%H:%M:%S')}"
        )

    # Same auto-refresh idiom as streamlit_live.py
    st.markdown(
        f'<meta http-equiv="refresh" content="{refresh_s}">',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
