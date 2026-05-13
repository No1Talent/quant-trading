# 历史数据导入

用 [`import_data.py`](../import_data.py) 把 CSV 历史数据导入 vn.py 数据库，支持分批 + 断点续传 + 失败可恢复。

---

## CSV 格式要求

**必需列**（缺一个就报错）：

| 列名 | 类型 | 说明 |
|------|------|------|
| `datetime` | 字符串或时间戳 | K 线开始时间 |
| `open` | 数值 | 开盘价 |
| `high` | 数值 | 最高价 |
| `low` | 数值 | 最低价 |
| `close` | 数值 | 收盘价 |
| `volume` | 数值 | 成交量 |

**可选列**：

| 列名 | 默认 |
|------|------|
| `turnover` | 0 |
| `open_interest` | 0 |

`datetime` 默认格式 `"%Y-%m-%d %H:%M:%S"`，可通过参数 `datetime_format` 覆盖。

---

## 最简用法

```python
from import_data import import_csv_to_database
from vnpy.trader.constant import Exchange, Interval

import_csv_to_database(
    csv_path=r"C:\Quant\data\bar\rb2510_1min.csv",
    symbol="rb2510",
    exchange=Exchange.SHFE,
    interval=Interval.MINUTE,
)
```

或者直接跑文件（用文件底部的 `__main__` 示例，改一下路径）：

```powershell
python import_data.py
```

---

## 完整参数

```python
def import_csv_to_database(
    csv_path: str | Path,
    symbol: str,
    exchange: Exchange,
    interval: Interval = Interval.MINUTE,
    datetime_format: str = "%Y-%m-%d %H:%M:%S",
    batch_size: int = 5000,         # 每批写入多少条
    resume: bool = True,            # 是否启用断点续传
) -> int                            # 返回成功导入条数
```

---

## 断点续传机制

进度文件路径：与 CSV 同目录，`{csv_name}.progress.json`。

格式：
```json
{
    "csv_path": "C:\\Quant\\data\\bar\\rb2510_1min.csv",
    "completed_rows": 250000,
    "total_rows": 500000,
    "last_update": "2026-05-13T10:30:00"
}
```

行为：
- 每批写入成功后更新进度。
- `resume=True` 时（默认）启动从 `completed_rows` 继续。
- **全部成功后自动删除进度文件**。
- 有批次失败时**保留**进度文件，下次再跑会跳过已成功批次。

如果想强制从头开始：删掉 `*.progress.json`，或者 `resume=False`。

---

## 失败处理

| 情况 | 行为 |
|------|------|
| CSV 缺列 / 为空 / 非数值 | 启动时报 `ValueError`，不写入任何数据 |
| 某行 datetime 格式错 | 该行跳过，日志记录，其他行继续 |
| 某批写库失败 | 该批跳过，记入 `failed_batches`，继续下一批 |
| 用户 Ctrl+C | 抛 `KeyboardInterrupt`，但进度文件已保存，下次可续 |
| 全部完成有失败批次 | **不删除**进度文件，方便排查 |

完成报告会打印失败批次的前 5 个，详细错在 `logs/`（如果你把 logger 重定向了）或控制台。

---

## 校验

导入完成后会自动调用 `database.get_bar_overview()` 输出：
```
数据库当前 rb2510.SHFE 共 500000 条 (2024-01-01 ~ 2026-05-13)
```

如果数字对不上 CSV 行数：
- 检查 datetime 是否有重复行（vnpy 用 INSERT OR REPLACE，重复会合并）。
- 检查"第 N 行解析失败"日志，看跳过了多少。

---

## 大数据集建议

| 规模 | 建议 |
|------|------|
| < 10 万行 | 默认 `batch_size=5000` 够用 |
| 10–100 万行 | 调大到 `batch_size=20000` 减少事务数 |
| > 100 万行 | 考虑分文件，按月切 |

数据库吞吐瓶颈通常在 vnpy 用的 SQLite/MySQL 单机性能，不在 Python 端。

---

## 多合约批量导入

```python
import_jobs = [
    ("rb2510_1min.csv", "rb2510", Exchange.SHFE),
    ("ag2510_1min.csv", "ag2510", Exchange.SHFE),
    # ...
]

for csv, sym, ex in import_jobs:
    try:
        import_csv_to_database(
            csv_path=f"C:\\Quant\\data\\bar\\{csv}",
            symbol=sym, exchange=ex,
            interval=Interval.MINUTE,
        )
    except Exception as e:
        logger.error("导入 %s 失败: %s", csv, e)
        # 继续下一个
```

并行导入暂未实现，见 [roadmap.md](roadmap.md) P2 第 12 条。

---

## 数据从哪来

本补丁不提供数据源。常见获取方式：
- 通达信、文华财经导出 CSV。
- tushare / akshare 拉取后转 CSV。
- 期货公司柜台日终数据。

CSV 列名记得映射成本工具要求的 `datetime/open/high/low/close/volume`。
