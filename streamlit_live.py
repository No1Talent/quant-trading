"""SimNow / 实盘观察 dashboard。

与 [streamlit_app.py](streamlit_app.py) 区分:
    streamlit_app.py = 离线研究 dashboard(WFA CSV 浏览)
    streamlit_live.py = 在线运行观察(本地 sync_data / 日志 / flag 状态)

被动观察设计 — 只读 filesystem,不连 vn.py、不连 CTP、不下单。
适用场景:vn.py 在主进程跑,这个 dashboard 在另一个终端 `streamlit run streamlit_live.py`
开着,用于实时核对仓位 / flag / 日志,无需切换到 vn.py GUI 窗口。

自动刷新:默认 5s,可在 UI 调整。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.sync_data_loader import (  # noqa: E402
    load_local_positions_for_reconcile,
)

# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = REPO_ROOT / "vnpy_workspace"
VNTRADER_DIR = WORKSPACE_DIR / ".vntrader"
LOG_DIR = REPO_ROOT / "logs"
RECONCILE_FLAG = LOG_DIR / "reconcile_breach.flag"
RISK_FLAG = LOG_DIR / "risk_breach.flag"
TRADER_LOG = LOG_DIR / "trader.log"

SETTING_FILE = VNTRADER_DIR / "cta_strategy_setting.json"
DATA_FILE = VNTRADER_DIR / "cta_strategy_data.json"


# --------------------------------------------------------------------------- #
# Status readers
# --------------------------------------------------------------------------- #


def _read_flag(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"_parse_error": str(e), "path": str(path)}


def _file_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    import time

    return time.time() - path.stat().st_mtime


def _tail_lines(path: Path, n: int = 30) -> list[str]:
    if not path.exists():
        return []
    try:
        # Cheap tail — fine for log files < 100 MB.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError as e:
        return [f"[error reading {path}: {e}]"]


def _read_strategy_data() -> dict:
    """Read both setting + data JSON for strategy variable display."""
    if not SETTING_FILE.exists() and not DATA_FILE.exists():
        return {"setting": {}, "data": {}}
    setting = {}
    data = {}
    if SETTING_FILE.exists():
        try:
            setting = json.loads(SETTING_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            setting = {"_parse_error": "cta_strategy_setting.json malformed"}
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"_parse_error": "cta_strategy_data.json malformed"}
    return {"setting": setting, "data": data}


# --------------------------------------------------------------------------- #
# UI sections
# --------------------------------------------------------------------------- #


def section_system_status() -> None:
    st.subheader("系统状态")

    col1, col2, col3 = st.columns(3)

    # 对账 flag
    rec_flag = _read_flag(RECONCILE_FLAG)
    with col1:
        if rec_flag is None:
            st.success("✅ 对账无 breach")
        else:
            st.error("⛔ 对账 BREACH")
            st.json(rec_flag)

    # 风控 flag
    risk_flag = _read_flag(RISK_FLAG)
    with col2:
        if risk_flag is None:
            st.success("✅ 风控无 breach")
        else:
            st.error("⛔ 风控 BREACH")
            st.json(risk_flag)

    # 日志新鲜度 = 隐式判断 vn.py 是否仍在跑
    log_age = _file_age_seconds(TRADER_LOG)
    with col3:
        if log_age is None:
            st.warning("⚪ 无 trader.log(未启动?)")
        elif log_age < 30:
            st.success(f"🟢 trader.log 活跃({log_age:.0f}s 前)")
        elif log_age < 300:
            st.warning(f"🟡 trader.log {log_age:.0f}s 未更新")
        else:
            st.error(f"🔴 trader.log {log_age / 60:.1f}min 未更新")


def section_local_positions() -> None:
    st.subheader("本地仓位(sync_data 聚合)")

    if not (SETTING_FILE.exists() or DATA_FILE.exists()):
        st.info("尚无 sync_data 文件 — 首次启动或未交易")
        return

    try:
        positions = load_local_positions_for_reconcile(VNTRADER_DIR)
    except ValueError as e:
        st.error(f"sync_data 损坏: {e}")
        return

    if not positions:
        st.info("本地无非零仓位")
        return

    rows = [
        {
            "vt_symbol": vt_symbol,
            "direction": direction,
            "volume": volume,
        }
        for vt_symbol, (direction, volume) in sorted(positions.items())
    ]
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption("⚠ 仅本地视角。CTP 真实仓位需对账或 GUI 查询;启动期 reconciler 已比对。")


def section_strategy_state() -> None:
    st.subheader("策略状态")

    bundle = _read_strategy_data()
    setting = bundle["setting"]
    data = bundle["data"]

    if not setting:
        st.info("无策略配置(cta_strategy_setting.json 缺失或空)")
        return

    rows = []
    for strategy_name, cfg in setting.items():
        if not isinstance(cfg, dict):
            continue
        var = data.get(strategy_name, {})
        if not isinstance(var, dict):
            var = {}
        rows.append(
            {
                "strategy": strategy_name,
                "class": cfg.get("class_name", "—"),
                "vt_symbol": cfg.get("vt_symbol", "—"),
                "pos": var.get("pos", "—"),
                "vars": ", ".join(f"{k}={v}" for k, v in var.items() if k != "pos") or "—",
            }
        )

    if not rows:
        return

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def section_recent_logs(n: int) -> None:
    st.subheader(f"最近 {n} 行日志")
    lines = _tail_lines(TRADER_LOG, n=n)
    if not lines:
        st.info("trader.log 不存在或为空")
        return

    # Color-code by log level via simple substring match
    def colorize(line: str) -> str:
        s = line.rstrip()
        if "[CRITICAL]" in s or "⛔" in s:
            return f":red[{s}]"
        if "[ERROR]" in s:
            return f":red[{s}]"
        if "[WARNING]" in s:
            return f":orange[{s}]"
        if "[INFO]" in s:
            return s
        return s

    body = "\n".join(colorize(ln) for ln in lines)
    st.text(body)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(
        page_title="vn.py Live Observation",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("vn.py Live Observation")
    st.caption("被动观察:只读本地状态(.vntrader/, logs/),不连 vn.py 也不连 CTP。")

    st.sidebar.header("Settings")
    refresh_s = st.sidebar.slider("Auto-refresh (秒)", min_value=1, max_value=30, value=5)
    log_lines = st.sidebar.slider("日志行数", min_value=10, max_value=200, value=30)

    st.sidebar.divider()
    st.sidebar.caption("Paths")
    st.sidebar.code(f"workspace: {WORKSPACE_DIR}\nvntrader:  {VNTRADER_DIR}\nlogs:      {LOG_DIR}")

    # Render
    section_system_status()
    st.divider()
    section_local_positions()
    st.divider()
    section_strategy_state()
    st.divider()
    section_recent_logs(log_lines)

    # Auto-refresh via meta — simpler than st.rerun; works under Streamlit's flow
    st.markdown(
        f'<meta http-equiv="refresh" content="{refresh_s}">',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
