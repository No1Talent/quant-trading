"""CSV 历史数据 → vn.py 数据库：分批写入 + 进度追踪 + 断点续传。

支持两类数据：
- **K线（bar）**：``import_csv_to_database`` —— 需要 OHLCV 列。
- **Tick（分时级）**：``import_tick_csv_to_database`` —— 需要 last_price + volume，
  并尽量带 turnover / open_interest / 买卖一档，以支撑分时图方法（均价线 VWAP、
  现手、买卖盘压力）的回测。详见 ``docs/intraday_fenshi_method.md`` 的数据缺口一节。

两条路径共用同一套「分批写入 + 进度文件 + 失败隔离 + 断点续传」机制（``_write_in_batches``）。
"""

import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path

import pandas as pd

PARENT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(PARENT_DIR))

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData, TickData

logger = logging.getLogger("import_data")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


REQUIRED_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]
OPTIONAL_COLUMNS = ["turnover", "open_interest"]

# Tick：last_price + volume 必需；其余尽量提供以还原分时图四要素
REQUIRED_TICK_COLUMNS = ["datetime", "last_price", "volume"]
OPTIONAL_TICK_COLUMNS = [
    "last_volume",  # 现手
    "turnover",  # 当日累计成交额 → 均价线 VWAP
    "open_interest",  # 持仓量
    "bid_price_1",
    "ask_price_1",
    "bid_volume_1",
    "ask_volume_1",
    "limit_up",
    "limit_down",
]


def _validate_columns(df: pd.DataFrame, required: list[str], numeric: list[str]) -> None:
    missing = set(required) - set(df.columns)
    if missing:
        raise ValueError(f"CSV缺少必需列: {missing}")

    if df.empty:
        raise ValueError("CSV为空")

    for col in numeric:
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise ValueError(f"列 {col} 不是数值类型")


def _validate_csv(df: pd.DataFrame) -> None:
    """K线 CSV 校验（向后兼容的公开名）。"""
    _validate_columns(df, REQUIRED_COLUMNS, ["open", "high", "low", "close", "volume"])


def _validate_tick_csv(df: pd.DataFrame) -> None:
    """Tick CSV 校验。"""
    _validate_columns(df, REQUIRED_TICK_COLUMNS, ["last_price", "volume"])


def _parse_dt(dt_val, datetime_format: str) -> datetime:
    if isinstance(dt_val, str):
        return datetime.strptime(dt_val, datetime_format)
    return pd.Timestamp(dt_val).to_pydatetime()


def _build_bars(
    df: pd.DataFrame, symbol: str, exchange: Exchange, interval: Interval, datetime_format: str
) -> list[BarData]:
    bars = []
    for idx, row in df.iterrows():
        try:
            bar = BarData(
                symbol=symbol,
                exchange=exchange,
                datetime=_parse_dt(row["datetime"], datetime_format),
                interval=interval,
                open_price=float(row["open"]),
                high_price=float(row["high"]),
                low_price=float(row["low"]),
                close_price=float(row["close"]),
                volume=float(row["volume"]),
                turnover=float(row.get("turnover") or 0),
                open_interest=float(row.get("open_interest") or 0),
                gateway_name="DB",
            )
            bars.append(bar)
        except Exception as e:
            logger.error("第 %d 行解析失败，跳过: %s", idx, e)
    return bars


def _build_ticks(
    df: pd.DataFrame, symbol: str, exchange: Exchange, datetime_format: str
) -> list[TickData]:
    """Tick CSV → list[TickData]。缺失的可选列按 0 填充（与 _build_bars 同风格）。"""
    ticks = []
    for idx, row in df.iterrows():
        try:
            tick = TickData(
                symbol=symbol,
                exchange=exchange,
                datetime=_parse_dt(row["datetime"], datetime_format),
                name=symbol,
                last_price=float(row["last_price"]),
                last_volume=float(row.get("last_volume") or 0),
                volume=float(row["volume"]),
                turnover=float(row.get("turnover") or 0),
                open_interest=float(row.get("open_interest") or 0),
                bid_price_1=float(row.get("bid_price_1") or 0),
                ask_price_1=float(row.get("ask_price_1") or 0),
                bid_volume_1=float(row.get("bid_volume_1") or 0),
                ask_volume_1=float(row.get("ask_volume_1") or 0),
                limit_up=float(row.get("limit_up") or 0),
                limit_down=float(row.get("limit_down") or 0),
                gateway_name="DB",
            )
            ticks.append(tick)
        except Exception as e:
            logger.error("第 %d 行解析失败，跳过: %s", idx, e)
    return ticks


def _read_progress(progress_file: Path, resume: bool) -> int:
    if resume and progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            start_idx = progress.get("completed_rows", 0)
            logger.info("断点续传：从第 %d 行继续", start_idx)
            return start_idx
        except Exception as e:
            logger.warning("读取进度文件失败，从头开始: %s", e)
    return 0


def _write_in_batches(
    df_to_process: pd.DataFrame,
    build_fn: Callable[[pd.DataFrame], list],
    save_fn: Callable[[list], object],
    csv_path: Path,
    progress_file: Path,
    start_idx: int,
    total_rows: int,
    batch_size: int,
) -> tuple[int, list[tuple[int, int, str]]]:
    """共用的分批写入循环：build → save → 更新进度，单批失败不终止整体。

    K线 / Tick 只是 build_fn / save_fn 不同（``_build_bars``+``save_bar_data`` 或
    ``_build_ticks``+``save_tick_data``），其余分批、进度、失败隔离逻辑完全一致。
    """
    success_count = 0
    failed_batches: list[tuple[int, int, str]] = []

    for batch_start in range(0, len(df_to_process), batch_size):
        batch_end = min(batch_start + batch_size, len(df_to_process))
        batch_df = df_to_process.iloc[batch_start:batch_end]

        try:
            objs = build_fn(batch_df)
            if not objs:
                continue

            # vnpy 的 save_*_data 底层 INSERT OR REPLACE；分批写入让单批失败损失可控
            save_fn(objs)
            success_count += len(objs)

            completed = start_idx + batch_end
            progress_file.write_text(
                json.dumps(
                    {
                        "csv_path": str(csv_path),
                        "completed_rows": completed,
                        "total_rows": total_rows,
                        "last_update": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            logger.info(
                "进度: %d/%d (%.1f%%) 本批 %d 条",
                completed,
                total_rows,
                completed * 100 / total_rows,
                len(objs),
            )

        except KeyboardInterrupt:
            logger.warning("用户中断，已写入 %d 条，下次可断点续传", success_count)
            raise
        except Exception as e:
            logger.error("第 %d-%d 批写入失败: %s", batch_start, batch_end, e)
            failed_batches.append((batch_start, batch_end, str(e)))
            # 继续下一批，不让单批失败终止整个导入

    return success_count, failed_batches


def _finalize(
    success_count: int,
    failed_batches: list[tuple[int, int, str]],
    progress_file: Path,
) -> None:
    logger.info("=" * 60)
    logger.info("导入完成")
    logger.info("成功: %d 条", success_count)
    if failed_batches:
        logger.warning("失败批次: %d", len(failed_batches))
        for s, end, err in failed_batches[:5]:
            logger.warning("  第 %d-%d 行: %s", s, end, err)

    if not failed_batches and progress_file.exists():
        progress_file.unlink()
        logger.info("进度文件已清理")


def import_csv_to_database(
    csv_path: str | Path,
    symbol: str,
    exchange: Exchange,
    interval: Interval = Interval.MINUTE,
    datetime_format: str = "%Y-%m-%d %H:%M:%S",
    batch_size: int = 5000,
    resume: bool = True,
) -> int:
    """分批写入 K线 CSV → 数据库；进度文件记录已完成行数，支持断点续传。"""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV文件不存在: {csv_path}")

    logger.info("开始导入(bar): %s", csv_path)
    logger.info("目标: %s.%s %s", symbol, exchange.value, interval.value)

    progress_file = csv_path.with_suffix(".progress.json")
    start_idx = _read_progress(progress_file, resume)

    df = pd.read_csv(csv_path)
    _validate_csv(df)
    total_rows = len(df)
    logger.info("CSV共 %d 行，待处理 %d 行", total_rows, total_rows - start_idx)

    df_to_process = df.iloc[start_idx:].reset_index(drop=True)

    database = get_database()
    success_count, failed_batches = _write_in_batches(
        df_to_process,
        build_fn=partial(
            _build_bars,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            datetime_format=datetime_format,
        ),
        save_fn=database.save_bar_data,
        csv_path=csv_path,
        progress_file=progress_file,
        start_idx=start_idx,
        total_rows=total_rows,
        batch_size=batch_size,
    )

    _finalize(success_count, failed_batches, progress_file)

    overview = database.get_bar_overview()
    matched = [
        o
        for o in overview
        if o.symbol == symbol and o.exchange == exchange and o.interval == interval
    ]
    if matched:
        o = matched[0]
        logger.info(
            "数据库当前 %s.%s 共 %d 条 (%s ~ %s)", symbol, exchange.value, o.count, o.start, o.end
        )

    return success_count


def import_tick_csv_to_database(
    csv_path: str | Path,
    symbol: str,
    exchange: Exchange,
    datetime_format: str = "%Y-%m-%d %H:%M:%S",
    batch_size: int = 5000,
    resume: bool = True,
) -> int:
    """分批写入 Tick CSV → 数据库；与 bar 导入共用断点续传机制。

    Tick 没有 interval 维度。必需列 ``last_price`` + ``volume``；尽量提供 ``turnover``
    （均价线 VWAP 用）、``open_interest``（持仓量）、买卖一档（买卖盘压力）。
    亚秒时间戳请用 ``datetime_format="%Y-%m-%d %H:%M:%S.%f"``。
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV文件不存在: {csv_path}")

    logger.info("开始导入(tick): %s", csv_path)
    logger.info("目标: %s.%s TICK", symbol, exchange.value)

    progress_file = csv_path.with_suffix(".progress.json")
    start_idx = _read_progress(progress_file, resume)

    df = pd.read_csv(csv_path)
    _validate_tick_csv(df)
    total_rows = len(df)
    logger.info("CSV共 %d 行，待处理 %d 行", total_rows, total_rows - start_idx)

    df_to_process = df.iloc[start_idx:].reset_index(drop=True)

    database = get_database()
    success_count, failed_batches = _write_in_batches(
        df_to_process,
        build_fn=partial(
            _build_ticks,
            symbol=symbol,
            exchange=exchange,
            datetime_format=datetime_format,
        ),
        save_fn=database.save_tick_data,
        csv_path=csv_path,
        progress_file=progress_file,
        start_idx=start_idx,
        total_rows=total_rows,
        batch_size=batch_size,
    )

    _finalize(success_count, failed_batches, progress_file)

    overview = database.get_tick_overview()
    matched = [o for o in overview if o.symbol == symbol and o.exchange == exchange]
    if matched:
        o = matched[0]
        logger.info(
            "数据库当前 %s.%s TICK 共 %d 条 (%s ~ %s)",
            symbol,
            exchange.value,
            o.count,
            o.start,
            o.end,
        )

    return success_count


if __name__ == "__main__":
    # K线示例（改路径后 python import_data.py）
    import_csv_to_database(
        csv_path=r"C:\Quant\data\bar\rb2510_1min.csv",
        symbol="rb2510",
        exchange=Exchange.SHFE,
        interval=Interval.MINUTE,
        batch_size=5000,
        resume=True,
    )

    # Tick 示例（带亚秒时间戳时改 datetime_format）：
    # import_tick_csv_to_database(
    #     csv_path=r"C:\Quant\data\tick\rb2510_tick.csv",
    #     symbol="rb2510",
    #     exchange=Exchange.SHFE,
    #     datetime_format="%Y-%m-%d %H:%M:%S.%f",
    # )
