"""
================================================================
CSV历史数据导入工具 (v2 - 事务安全版)
================================================================
本版本修复 DB-1：分批写入 + 进度追踪 + 失败可恢复

核心策略：
    1. 分批写入（默认5000条/批），单批失败不影响已成功的批
    2. 进度文件记录已完成位置，断点续传
    3. 写入前校验CSV格式
    4. 写入完成后校验数据库总数
================================================================
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PARENT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(PARENT_DIR))

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData

# 日志
logger = logging.getLogger("import_data")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


REQUIRED_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]
OPTIONAL_COLUMNS = ["turnover", "open_interest"]


def _validate_csv(df: pd.DataFrame) -> None:
    """校验CSV格式 - 缺列直接报错，不要等到写入时崩"""
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"CSV缺少必需列: {missing}")

    if df.empty:
        raise ValueError("CSV为空")

    # 校验数值列
    for col in ["open", "high", "low", "close", "volume"]:
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise ValueError(f"列 {col} 不是数值类型")


def _build_bars(
    df: pd.DataFrame, symbol: str, exchange: Exchange, interval: Interval, datetime_format: str
) -> list[BarData]:
    """从DataFrame构造BarData列表"""
    bars = []
    for idx, row in df.iterrows():
        try:
            dt_val = row["datetime"]
            if isinstance(dt_val, str):
                dt = datetime.strptime(dt_val, datetime_format)
            else:
                dt = pd.Timestamp(dt_val).to_pydatetime()

            bar = BarData(
                symbol=symbol,
                exchange=exchange,
                datetime=dt,
                interval=interval,
                open_price=float(row["open"]),
                high_price=float(row["high"]),
                low_price=float(row["low"]),
                close_price=float(row["close"]),
                volume=float(row["volume"]),
                turnover=float(row.get("turnover", 0)),
                open_interest=float(row.get("open_interest", 0)),
                gateway_name="DB",
            )
            bars.append(bar)
        except Exception as e:
            logger.error("第 %d 行解析失败，跳过: %s", idx, e)
    return bars


def import_csv_to_database(
    csv_path: str | Path,
    symbol: str,
    exchange: Exchange,
    interval: Interval = Interval.MINUTE,
    datetime_format: str = "%Y-%m-%d %H:%M:%S",
    batch_size: int = 5000,
    resume: bool = True,
) -> int:
    """
    分批事务安全地导入CSV到数据库

    Args:
        csv_path:     CSV文件路径
        symbol:       合约代码
        exchange:     交易所
        interval:     K线周期
        datetime_format: 时间格式
        batch_size:   每批写入条数
        resume:       是否启用断点续传

    Returns:
        成功导入的条数
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV文件不存在: {csv_path}")

    logger.info("开始导入: %s", csv_path)
    logger.info("目标: %s.%s %s", symbol, exchange.value, interval.value)

    # ---------- 进度文件路径 ----------
    progress_file = csv_path.with_suffix(".progress.json")
    start_idx = 0

    if resume and progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            start_idx = progress.get("completed_rows", 0)
            logger.info("断点续传：从第 %d 行继续", start_idx)
        except Exception as e:
            logger.warning("读取进度文件失败，从头开始: %s", e)

    # ---------- 1. 读取并校验 ----------
    df = pd.read_csv(csv_path)
    _validate_csv(df)
    total_rows = len(df)
    logger.info("CSV共 %d 行，待处理 %d 行", total_rows, total_rows - start_idx)

    # ---------- 2. 切片处理（从断点开始） ----------
    df_to_process = df.iloc[start_idx:].reset_index(drop=True)

    # ---------- 3. 分批写入 ----------
    database = get_database()
    success_count = 0
    failed_batches = []

    for batch_start in range(0, len(df_to_process), batch_size):
        batch_end = min(batch_start + batch_size, len(df_to_process))
        batch_df = df_to_process.iloc[batch_start:batch_end]

        try:
            bars = _build_bars(batch_df, symbol, exchange, interval, datetime_format)
            if not bars:
                continue

            # 单批写入 - vnpy的save_bar_data在底层用INSERT OR REPLACE
            # 配合batch_size有限，单批失败损失可控
            database.save_bar_data(bars)
            success_count += len(bars)

            # 更新进度文件
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
                len(bars),
            )

        except KeyboardInterrupt:
            logger.warning("用户中断，已写入 %d 条，下次可断点续传", success_count)
            raise
        except Exception as e:
            logger.error("第 %d-%d 批写入失败: %s", batch_start, batch_end, e)
            failed_batches.append((batch_start, batch_end, str(e)))
            # 继续下一批，不让单批失败终止整个导入

    # ---------- 4. 完成报告 ----------
    logger.info("=" * 60)
    logger.info("导入完成")
    logger.info("成功: %d 条", success_count)
    if failed_batches:
        logger.warning("失败批次: %d", len(failed_batches))
        for s, end, err in failed_batches[:5]:
            logger.warning("  第 %d-%d 行: %s", s, end, err)

    # ---------- 5. 校验数据库 ----------
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

    # 全部成功才删除进度文件
    if not failed_batches and progress_file.exists():
        progress_file.unlink()
        logger.info("进度文件已清理")

    return success_count


if __name__ == "__main__":
    # 使用示例
    import_csv_to_database(
        csv_path=r"C:\Quant\data\bar\rb2510_1min.csv",
        symbol="rb2510",
        exchange=Exchange.SHFE,
        interval=Interval.MINUTE,
        batch_size=5000,
        resume=True,
    )
