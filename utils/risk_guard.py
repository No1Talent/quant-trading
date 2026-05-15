"""引擎层风控熔断：日内回撤 / 单合约持仓 / 单分钟成交频次。

触发后撤单 + CRITICAL + 落盘 flag；**不**自动平仓（合规/道德考量）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any

from vnpy.event import Event, EventEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TRADE

from .notifier import INotifier, get_notifier

logger = logging.getLogger("risk_guard")


DEFAULT_BREACH_FLAG = Path(__file__).parent.parent / "logs" / "risk_breach.flag"


class RiskGuard:
    """订阅 EVENT_TRADE / EVENT_ACCOUNT 做规则校验。只检查不平仓。"""

    def __init__(
        self,
        main_engine: Any,
        event_engine: EventEngine,
        notifier: INotifier | None = None,
        max_daily_loss_pct: float = 0.05,
        max_position_per_symbol: int = 10,
        max_trades_per_minute: int = 20,
        breach_flag_path: Path | str | None = None,
    ) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.notifier = notifier or get_notifier()

        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_per_symbol = max_position_per_symbol
        self.max_trades_per_minute = max_trades_per_minute

        self.breach_flag_path = Path(breach_flag_path) if breach_flag_path else DEFAULT_BREACH_FLAG

        self._lock = threading.Lock()
        self.tripped: bool = False
        self.trip_reason: str = ""

        # 日内基线在每天首笔 EVENT_ACCOUNT 时锁定，跨日重置
        self.daily_start_balance: float | None = None
        self.daily_start_date: date | None = None

        # 独立记一份净持仓用于规则判断（不复用 OmsEngine 的持仓，避免依赖）
        self.position: dict[str, int] = defaultdict(int)

        self.trade_window: deque[float] = deque()

        self._register()

        self.notifier.send(
            f"风控已启动\n日内回撤阈值: {max_daily_loss_pct * 100:.1f}%\n"
            f"单合约持仓上限: {max_position_per_symbol}\n"
            f"单分钟成交上限: {max_trades_per_minute}",
            title="风控启动",
            force=True,
        )
        logger.info(
            "RiskGuard 启动: daily=%.2f%% pos=%d trades/min=%d",
            max_daily_loss_pct * 100,
            max_position_per_symbol,
            max_trades_per_minute,
        )

    def _sync_positions_from_engine(self) -> None:
        """从 OmsEngine 读取净持仓，在每日首条账户事件时调用，恢复重启前持仓状态。"""
        try:
            positions = self.main_engine.get_all_positions()
            if not positions:
                return
            for pos in positions:
                direction = (
                    pos.direction.value if hasattr(pos.direction, "value") else str(pos.direction)
                )
                net = (
                    int(pos.volume)
                    if ("多" in direction or "long" in direction.lower())
                    else -int(pos.volume)
                )
                self.position[pos.vt_symbol] = net
            logger.info(
                "已从引擎恢复 %d 个合约持仓: %s",
                len(self.position),
                dict(self.position),
            )
        except Exception as e:
            logger.warning("从引擎读取持仓失败，以 0 起始: %s", e)

    def _register(self) -> None:
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_ACCOUNT, self.on_account)

    def unregister(self) -> None:
        self.event_engine.unregister(EVENT_TRADE, self.on_trade)
        self.event_engine.unregister(EVENT_ACCOUNT, self.on_account)

    def on_account(self, event: Event) -> None:
        try:
            account = event.data
            today = datetime.now().date()

            with self._lock:
                if self.daily_start_date != today:
                    self.daily_start_date = today
                    self.daily_start_balance = account.balance
                    self._sync_positions_from_engine()
                    logger.info(
                        "日内基线已重置 date=%s balance=%.2f",
                        today,
                        account.balance,
                    )
                    return

                if self.daily_start_balance is None or self.daily_start_balance <= 0:
                    return

                loss_pct = (self.daily_start_balance - account.balance) / self.daily_start_balance
                if loss_pct >= self.max_daily_loss_pct and not self.tripped:
                    self._trip(
                        f"日内亏损 {loss_pct * 100:.2f}% 超过阈值 "
                        f"{self.max_daily_loss_pct * 100:.2f}%\n"
                        f"日初: {self.daily_start_balance:.2f} 当前: {account.balance:.2f}"
                    )
        except Exception as e:
            logger.error("on_account 异常: %s", e)

    def on_trade(self, event: Event) -> None:
        try:
            trade = event.data
            vt_symbol = trade.vt_symbol
            volume = int(trade.volume)
            direction = (
                trade.direction.value if hasattr(trade.direction, "value") else str(trade.direction)
            )
            ts = (
                trade.datetime.timestamp()
                if hasattr(trade.datetime, "timestamp")
                else (datetime.now().timestamp())
            )

            with self._lock:
                # 在 CTP 模型里方向决定净持仓符号 (buy=+ / sell=-)；offset (开/平/平今/平昨)
                # 只是头寸归属，不影响净持仓变化的方向。
                signed = (
                    volume
                    if ("多" in direction or "long" in direction.lower() or direction == "Long")
                    else -volume
                )
                self.position[vt_symbol] += signed

                self.trade_window.append(ts)
                cutoff = ts - 60.0
                while self.trade_window and self.trade_window[0] < cutoff:
                    self.trade_window.popleft()

                if self.tripped:
                    return

                pos = self.position[vt_symbol]
                if abs(pos) > self.max_position_per_symbol:
                    self._trip(
                        f"合约 {vt_symbol} 净持仓 {pos} 超过阈值 ±{self.max_position_per_symbol}"
                    )
                    return

                if len(self.trade_window) > self.max_trades_per_minute:
                    self._trip(
                        f"过去 60 秒成交 {len(self.trade_window)} 次 "
                        f"超过阈值 {self.max_trades_per_minute}"
                    )
                    return
        except Exception as e:
            logger.error("on_trade 异常: %s", e)

    def _trip(self, reason: str) -> None:
        # 调用方必须持有 self._lock
        self.tripped = True
        self.trip_reason = reason
        logger.critical("⛔ 风控熔断: %s", reason)

        cancel_err: str | None = None
        try:
            cancel_fn = getattr(self.main_engine, "cancel_all_active_orders", None)
            if callable(cancel_fn):
                cancel_fn()
            else:
                # 兼容旧版 vn.py
                fallback = getattr(self.main_engine, "cancel_all_orders", None)
                if callable(fallback):
                    fallback()
                else:
                    cancel_err = "main_engine 无 cancel_all_active_orders / cancel_all_orders 方法"
        except Exception as e:
            cancel_err = str(e)

        flag_err: str | None = None
        try:
            self.breach_flag_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "tripped_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "cancel_error": cancel_err,
            }
            self.breach_flag_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            flag_err = str(e)

        msg = f"风控熔断已触发\n原因：{reason}\n动作：已撤所有挂单，停止开新仓"
        if cancel_err:
            msg += f"\n⚠️ 撤单失败：{cancel_err}"
        if flag_err:
            msg += f"\n⚠️ 标志文件写入失败：{flag_err}"
        msg += "\n\n平仓决策请人工介入，重启前请先删除 logs/risk_breach.flag"

        try:
            self.notifier.send_critical(msg)
        except Exception as e:
            logger.error("熔断告警推送失败: %s", e)

    def reset(self) -> None:
        """人工复位（测试/排障用）。"""
        with self._lock:
            self.tripped = False
            self.trip_reason = ""
        if self.breach_flag_path.exists():
            try:
                self.breach_flag_path.unlink()
            except OSError as e:
                logger.error("删除熔断标志失败: %s", e)
        logger.info("RiskGuard 已复位")


# 持有引用防止 GC
_guards: list[RiskGuard] = []


def attach_risk_guard(
    main_engine: Any,
    event_engine: EventEngine,
    notifier: INotifier | None = None,
    **kwargs: Any,
) -> RiskGuard:
    """挂载风控，在 run.py 调用一次即可。阈值通过 kwargs 透传。"""
    guard = RiskGuard(main_engine, event_engine, notifier, **kwargs)
    _guards.append(guard)
    return guard


def check_breach_flag(flag_path: Path | str | None = None) -> dict | None:
    """启动前检查上次是否触发过熔断；未触发返回 None。"""
    p = Path(flag_path) if flag_path else DEFAULT_BREACH_FLAG
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("读取熔断标志失败: %s", e)
        return {"error": str(e), "raw_path": str(p)}
