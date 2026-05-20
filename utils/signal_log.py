"""结构化信号日志（JSONL）— Layer ② 解耦的第一块基石。

为什么先有 SignalLog 再有 Signal 抽象
-------------------------------------
现在的策略 ``write_log("信号: 金叉做多 ...")`` 是非结构化的字符串日志，
信号→订单是同步阻塞调用，没有任何可以 grep 之外的二次消费形态。

P2 的目标不是把策略改成"只 yield Signal 不下单"（那是 P3 的破坏性重构），
而是先在 *已有* 的下单链路上**旁路 tap** 一条结构化记录：
- 每次 ``safe_buy / safe_sell / safe_short / safe_cover`` 走过 ``_gated_send``，
  就把"意图（intent）+ 风控结论 + 时间戳"原子写一行 JSONL
- 拒发也要写 —— allowed=False + reject_reason，让事后排查 RiskGuard 行为有
  ground truth
- 文件按日轮转（与 ``notifier.py`` 的 TimedRotatingFileHandler 对齐），
  90 天保留期

立即带来的能力
- LIVE vs SIGNAL_ONLY 对账：两份 signals.jsonl 跨模式 diff，验证"假成交"
  方案没有改变策略行为
- WFA 样本外 → 实盘漂移监控：研究脚本预测的 cross 时间点 vs 实盘真实触发
  时间点的 lag 分布
- Postmortem：熔断/异常事件后回看那一时刻策略的"完整意图序列"

设计约束
- 默认 ``NullSignalLog`` —— 测试与 backtest 必须零文件副作用
- 线程安全：``_gated_send`` 在 EventEngine 工作线程上被调
- 不阻塞：写文件用追加 + ``flush()``；如果写失败只记日志不抛（信号日志失败
  绝不应该影响交易主链路）
- 不需要 ``ThreadPoolExecutor``：每条 JSONL 100~300 字节，Windows NTFS 单次
  ``write`` + ``flush`` 微秒级；引入异步会顺便引入"crash 时丢失最近数据"的
  风险，得不偿失
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("signal_log")

DEFAULT_SIGNAL_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "signals.jsonl"


class ISignalLog(Protocol):
    """信号日志接口。允许 NullSignalLog / FileSignalLog / 测试桩等多实现。"""

    def append(
        self,
        *,
        strategy_name: str,
        vt_symbol: str,
        side: str,
        price: float,
        volume: int,
        allowed: bool,
        reject_reason: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class NullSignalLog:
    """no-op 实现。回测 / 单元测试默认走它，文件系统零副作用。"""

    def append(
        self,
        *,
        strategy_name: str,
        vt_symbol: str,
        side: str,
        price: float,
        volume: int,
        allowed: bool,
        reject_reason: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class FileSignalLog:
    """JSONL 追加写入实现。

    每行一条 JSON，字段固定为：
    ``ts / strategy / vt_symbol / side / price / volume / allowed / reject_reason / metadata``。
    新增字段时只能 append，**禁止** rename / 改类型，保持下游 grep / pandas 解析
    向后兼容。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_SIGNAL_LOG_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 写锁：多个策略实例 / EventEngine worker 可能在不同线程上调
        # （SIGNAL_ONLY/REPLAY 的 dispatch_sync 把 send_order 拉到回放线程）
        self._lock = threading.Lock()

    def append(
        self,
        *,
        strategy_name: str,
        vt_symbol: str,
        side: str,
        price: float,
        volume: int,
        allowed: bool,
        reject_reason: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "strategy": strategy_name,
            "vt_symbol": vt_symbol,
            "side": side,
            "price": price,
            "volume": volume,
            "allowed": allowed,
            "reject_reason": reject_reason,
            "metadata": metadata or {},
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except OSError as e:
            # 信号日志写入失败绝不能影响主交易链路 — 落日志后继续
            logger.error("SignalLog 写入失败 path=%s err=%s", self.path, e)


# 进程级单例：默认 NullSignalLog，运行入口（run.py）显式 set 到 FileSignalLog
_signal_log: ISignalLog = NullSignalLog()


def get_signal_log() -> ISignalLog:
    return _signal_log


def set_signal_log(log: ISignalLog | None) -> None:
    """显式注入。``None`` 退回 NullSignalLog（关掉旁路）。"""
    global _signal_log
    _signal_log = log if log is not None else NullSignalLog()
