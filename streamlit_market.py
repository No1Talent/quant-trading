"""Market Intel dashboard — real-time K线 + LLM analysis (WIP).

Run:
    streamlit run streamlit_market.py

Third dashboard, alongside:
  - streamlit_app.py  = offline WFA research browser
  - streamlit_live.py = ops observation (sync_data / flags / logs)

Tabs:
  - 单合约   = one contract, full-size chart + metric strip
  - Watchlist = N contracts from config/market_watchlist.yaml as smaller cards
  - LLM 分析 = placeholder (M4-M5)

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
from utils.market_watchlist import (  # noqa: E402
    DEFAULT_WATCHLIST_PATH,
    WatchItem,
    load_watchlist,
)


@st.cache_data(show_spinner=False, ttl=25)
def _cached_bars(vt_symbol: str, interval: str, n_bars: int, use_realtime: bool):
    """Cache result for slightly less than the default 30s refresh so a
    refresh always pulls fresh AkShare data, but rapid re-renders inside
    the same window (sidebar twiddling) don't re-hit the network."""
    return get_recent_bars(BarRequest(vt_symbol, interval, n_bars, use_realtime))


@st.cache_data(show_spinner=False, ttl=60)
def _cached_watchlist(path_str: str) -> list[WatchItem]:
    # path_str instead of Path so the cache key is hashable + stable.
    return load_watchlist(Path(path_str))


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


def _render_single(vt_symbol: str, interval: str, n_bars: int, use_realtime: bool) -> None:
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


def _render_watchlist_card(item: WatchItem, use_realtime: bool) -> None:
    """One contract card on the Watchlist tab. Compact chart + 2-tile metric."""
    try:
        df = _cached_bars(item.vt_symbol, item.interval, item.n_bars, use_realtime)
    except Exception as e:  # noqa: BLE001
        st.error(f"{item.name} 加载失败: {e}")
        return

    if df.empty:
        st.warning(f"{item.name} ({item.vt_symbol}) 无数据")
        return

    last = df.iloc[-1]
    first = df.iloc[0]
    change_pct = (last["close"] - first["close"]) / first["close"] * 100 if first["close"] else 0

    header_cols = st.columns([3, 1, 1])
    header_cols[0].markdown(f"**{item.name}** · `{item.vt_symbol}` · {item.interval}")
    header_cols[1].metric("最新", f"{last['close']:.2f}")
    header_cols[2].metric("窗口涨跌", f"{change_pct:+.2f}%")

    fig = make_candlestick(df, title="")
    fig.update_layout(height=350, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


def _render_watchlist(use_realtime: bool) -> None:
    wl_path = st.session_state.get("wl_path", str(DEFAULT_WATCHLIST_PATH))
    st.caption(f"配置文件: `{wl_path}` — 编辑后下次刷新生效（缓存 60s）")

    try:
        items = _cached_watchlist(wl_path)
    except FileNotFoundError as e:
        st.error(str(e))
        return
    except ValueError as e:
        st.error(f"YAML 校验失败: {e}")
        return

    if not items:
        st.info("Watchlist 为空，编辑 YAML 添加合约即可")
        return

    for i, item in enumerate(items):
        if i > 0:
            st.divider()
        _render_watchlist_card(item, use_realtime)


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

    st.sidebar.header("单合约设置")
    vt_symbol = st.sidebar.text_input(
        "vt_symbol",
        value="rb2501.SHFE",
        help="格式: <symbol>.<exchange>，如 rb2501.SHFE / ag2506.SHFE / jm2501.DCE",
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
    st.sidebar.header("Watchlist 配置")
    wl_path_input = st.sidebar.text_input(
        "YAML 路径",
        value=str(DEFAULT_WATCHLIST_PATH),
        help="编辑 YAML 后切换到 Watchlist tab 即可看到更新",
    )
    st.session_state["wl_path"] = wl_path_input

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

    tab_single, tab_watch, tab_llm = st.tabs(["单合约", "Watchlist", "LLM 分析"])

    with tab_single:
        _render_single(vt_symbol, interval, n_bars, use_realtime)

    with tab_watch:
        _render_watchlist(use_realtime)

    with tab_llm:
        st.info("M4-M5 待接入：Claude API 形态/上下文分析。")
        st.caption(
            "约束：输出严格定位为**市场观察**（形态识别、量价关系、多周期对齐度），"
            "**禁止**输出买卖建议/价位预测/仓位建议。"
        )

    # Same auto-refresh idiom as streamlit_live.py
    st.markdown(
        f'<meta http-equiv="refresh" content="{refresh_s}">',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
