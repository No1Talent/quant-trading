"""拉 1 分钟期货 bar → 写成 import_data.py 能直接吃的 CSV。

为什么需要它
------------
分时图 A/B/C 在 1h 上无 edge（见 research/m4_vwap_findings.md），但 1h 是粗代理；
要在 native（1min/tick）粒度重测，先得有 1min 数据。本脚本把「取数 → 落 CSV」这步标准化，
让 ``import_csv_to_database`` 直接导入、M4 直接重跑。

数据源与深度（重要）
--------------------
- **akshare（免费、已实现、默认）**：``futures_zh_minute_sina`` 只给**最近 ~1023 根** 1min
  （≈ 3 个交易日）。够验证「1min 全链路通不通」，**不够回测**（trend_window=60 都快吃光样本）。
- **tushare pro（需 token + 积分）**：``ft_mins`` 可拉数年 1min。设 ``TUSHARE_TOKEN`` 环境变量后启用。
- **rqdatac（需 ricequant 账号）**：``get_price(frequency='1m')``。设账号后启用。

三个源都归一到同一套列：``datetime, open, high, low, close, volume, open_interest``
（VWAP 用 close×volume 算，不依赖 turnover；open_interest 供量仓特征）。

用法
----
    # 默认 akshare（浅，验证链路用）
    python research/fetch_minute_data.py RB0 --exchange SHFE
    # tushare（深，需先 setx TUSHARE_TOKEN ...）
    python research/fetch_minute_data.py RB2510.SHF --source tushare --start 20240101 --end 20241015
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

logger = logging.getLogger("fetch_minute_data")

# 统一输出列（import_data.import_csv_to_database 的 REQUIRED + OPTIONAL）
OUT_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "open_interest"]


def _normalize(df: pd.DataFrame, rename: dict[str, str]) -> pd.DataFrame:
    """改列名 → 补缺列(0) → 只留 OUT_COLUMNS → datetime 转字符串。"""
    df = df.rename(columns=rename)
    for col in OUT_COLUMNS:
        if col not in df.columns:
            df[col] = 0
    df = df[OUT_COLUMNS].copy()
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "open_interest"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)


def fetch_akshare_minute(symbol: str, period: str = "1") -> pd.DataFrame:
    """akshare Sina 1min（免费、浅 ~1023 根）。symbol 如 'RB0'（主连）/ 'rb2510'。"""
    import akshare as ak

    raw = ak.futures_zh_minute_sina(symbol=symbol, period=period)
    # 列：datetime, open, high, low, close, volume, hold
    return _normalize(raw, rename={"hold": "open_interest"})


def fetch_tushare_minute(
    ts_code: str, start: str, end: str, freq: str = "1min", token: str | None = None
) -> pd.DataFrame:
    """tushare pro ``ft_mins``（需 token + 积分，可拉数年）。

    token 取自参数 / ``TUSHARE_TOKEN`` 环境变量 / ``ts.get_token()``。列名按常见 tier 映射，
    若你的 tier 列名不同，改下面 rename 即可。
    """
    import tushare as ts

    token = token or os.environ.get("TUSHARE_TOKEN")
    if token:
        ts.set_token(token)
    if not ts.get_token():
        raise RuntimeError(
            "tushare 无 token：setx TUSHARE_TOKEN <你的token> 或传 --token，"
            "且账户需有期货分钟权限积分。"
        )
    pro = ts.pro_api()
    raw = pro.ft_mins(ts_code=ts_code, freq=freq, start_date=start, end_date=end)
    if raw is None or raw.empty:
        raise RuntimeError(f"tushare ft_mins 返回空：{ts_code} {start}~{end}（检查积分/代码/区间）")
    raw = raw.sort_values("trade_time")
    return _normalize(
        raw,
        rename={"trade_time": "datetime", "vol": "volume", "oi": "open_interest"},
    )


def fetch_rqdatac_minute(order_book_id: str, start: str, end: str) -> pd.DataFrame:
    """rqdatac ``get_price(frequency='1m')``（需 ricequant 账号 init）。"""
    import rqdatac

    rqdatac.init()  # 读环境/配置里的账号；未配置会抛错
    raw = rqdatac.get_price(
        order_book_id,
        start_date=start,
        end_date=end,
        frequency="1m",
        fields=["open", "high", "low", "close", "volume", "open_interest"],
    )
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"rqdatac 返回空：{order_book_id} {start}~{end}")
    raw = raw.reset_index().rename(columns={"datetime": "datetime"})
    return _normalize(raw, rename={})


def fetch_to_csv(
    symbol: str,
    out_dir: str | Path = "data/bar",
    source: str = "akshare",
    start: str | None = None,
    end: str | None = None,
    token: str | None = None,
) -> Path:
    """取数 → 落 CSV，返回路径。文件名 ``{symbol}_1min.csv``（symbol 中的点替成下划线）。"""
    if source == "akshare":
        df = fetch_akshare_minute(symbol)
    elif source == "tushare":
        if not (start and end):
            raise ValueError("tushare 需要 --start / --end（YYYYMMDD）")
        df = fetch_tushare_minute(symbol, start, end, token=token)
    elif source == "rqdatac":
        if not (start and end):
            raise ValueError("rqdatac 需要 --start / --end")
        df = fetch_rqdatac_minute(symbol, start, end)
    else:
        raise ValueError(f"未知 source: {source}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{symbol.replace('.', '_')}_1min.csv"
    df.to_csv(out_path, index=False)
    logger.info(
        "%s: %d 根 1min (%s ~ %s) → %s",
        symbol,
        len(df),
        df["datetime"].iloc[0] if len(df) else "-",
        df["datetime"].iloc[-1] if len(df) else "-",
        out_path,
    )
    return out_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="拉 1min 期货 bar → import-ready CSV")
    ap.add_argument("symbol", help="akshare: RB0/rb2510 | tushare: RB2510.SHF | rqdatac: RB2510")
    ap.add_argument("--source", default="akshare", choices=["akshare", "tushare", "rqdatac"])
    ap.add_argument("--out-dir", default="data/bar")
    ap.add_argument("--start", help="YYYYMMDD（tushare/rqdatac 必填）")
    ap.add_argument("--end", help="YYYYMMDD（tushare/rqdatac 必填）")
    ap.add_argument("--token", help="tushare token（也可用 TUSHARE_TOKEN 环境变量）")
    args = ap.parse_args()

    path = fetch_to_csv(
        symbol=args.symbol,
        out_dir=args.out_dir,
        source=args.source,
        start=args.start,
        end=args.end,
        token=args.token,
    )
    print(f"\n写出: {path}")
    print("下一步导入:")
    print("  from import_data import import_csv_to_database")
    print("  from vnpy.trader.constant import Exchange, Interval")
    print(
        f"  import_csv_to_database(r'{path}', symbol='{args.symbol.split('.')[0]}', "
        "exchange=Exchange.SHFE, interval=Interval.MINUTE)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
