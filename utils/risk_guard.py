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
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TICK, EVENT_TRADE

from .notifier import INotifier, get_notifier
from .product_registry import ProductRegistry, get_default_registry

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
        max_price_deviation: float = 0.05,
        max_tick_age_seconds: float = 60.0,
        breach_flag_path: Path | str | None = None,
        startup_sync_timeout_s: float | None = 10.0,
        max_position_per_underlying: int | None = None,
        product_registry: ProductRegistry | None = None,
    ) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.notifier = notifier or get_notifier()

        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_per_symbol = max_position_per_symbol
        self.max_trades_per_minute = max_trades_per_minute
        self.max_price_deviation = max_price_deviation
        self.max_tick_age_seconds = max_tick_age_seconds

        # max_position_per_underlying 是 P1 新增的标的级风控维度 —— 防止策略
        # 在同一标的多个合约月份各自占满 max_position_per_symbol 而绕过总仓限制。
        # None = 禁用（保持向后兼容）。开启时如未显式传入 product_registry，按需
        # 懒加载默认 YAML 实例。
        self.max_position_per_underlying = max_position_per_underlying
        if max_position_per_underlying is not None and product_registry is None:
            product_registry = get_default_registry()
        self.product_registry = product_registry

        self.breach_flag_path = Path(breach_flag_path) if breach_flag_path else DEFAULT_BREACH_FLAG

        self._lock = threading.Lock()
        self.tripped: bool = False
        self.trip_reason: str = ""

        # 日内基线在每天首笔 EVENT_ACCOUNT 时锁定，跨日重置
        self.daily_start_balance: float | None = None
        self.daily_start_date: date | None = None

        # 独立记一份净持仓用于规则判断（不复用 OmsEngine 的持仓，避免依赖）
        self.position: dict[str, int] = defaultdict(int)
        # 标的级聚合净持仓（vt_symbol → underlying 通过 ProductRegistry 解析）。
        # 未注册的 vt_symbol 不进入这张表，规则也自然跳过该合约（不当作违例）。
        self.underlying_position: dict[str, int] = defaultdict(int)

        self.trade_window: deque[float] = deque()

        # 最近行情缓存：发单 pre-gate 用。仅记 last_price + ts，不存整个 TickData。
        self.latest_tick_price: dict[str, float] = {}
        self.latest_tick_at: dict[str, datetime] = {}

        # pre-gate 拒发计数，便于排障 / 测试断言
        self.pre_gate_rejects: int = 0

        # 同 (合约, 原因) 的告警节流：30s 内重复仅日志/计数，不再 push notifier。
        # 防止策略 bug 导致告警风暴把真实告警淹没（DingTalk 等会静默限流）。
        self._reject_alert_cooldown_s: float = 30.0
        self._reject_alert_last: dict[tuple[str, str], datetime] = {}

        # 启动 fallback：若 EVENT_ACCOUNT 在 startup_sync_timeout_s 内未到，主动拉取
        # 一次持仓——否则 self.position 起始为空，全部持仓校验形同虚设。
        self._initial_sync_done: bool = False
        self._startup_sync_timeout_s = startup_sync_timeout_s
        self._startup_timer: threading.Timer | None = None

        self._register()
        self._schedule_startup_sync_fallback()

        underlying_line = (
            f"\n标的汇总持仓上限: {max_position_per_underlying}"
            if max_position_per_underlying is not None
            else ""
        )
        self.notifier.send(
            f"风控已启动\n日内回撤阈值: {max_daily_loss_pct * 100:.1f}%\n"
            f"单合约持仓上限: {max_position_per_symbol}{underlying_line}\n"
            f"单分钟成交上限: {max_trades_per_minute}\n"
            f"发单价格偏离上限: ±{max_price_deviation * 100:.1f}%",
            title="风控启动",
            force=True,
        )
        logger.info(
            "RiskGuard 启动: daily=%.2f%% pos=%d underlying=%s trades/min=%d price_dev=±%.2f%%",
            max_daily_loss_pct * 100,
            max_position_per_symbol,
            max_position_per_underlying if max_position_per_underlying is not None else "off",
            max_trades_per_minute,
            max_price_deviation * 100,
        )

    def _resolve_underlying(self, vt_symbol: str) -> str | None:
        """通过 ProductRegistry 把 vt_symbol → underlying。未配置/未注册 → None。

        以 None 表达"该合约不在标的池"是显式契约：on_trade 看到 None 就跳过标的
        汇总检查；on_sync 看到 None 就不把它计入 underlying_position。从不抛异常 ——
        风控引擎的事件回调里抛任何异常都会被 vn.py 吞掉，连日志都难追。
        """
        if self.product_registry is None:
            return None
        try:
            return self.product_registry.underlying_of_or_none(vt_symbol)
        except Exception as e:  # 防御性：registry 内部 bug 不应崩 RiskGuard
            logger.warning("ProductRegistry 解析 %s 失败: %s", vt_symbol, e)
            return None

    def _sync_positions_from_engine(self) -> None:
        """从 OmsEngine 读取净持仓，在每日首条账户事件或启动 fallback 时调用。

        成功（包括"无持仓"这种合法空返回）即标记 `_initial_sync_done = True`，
        让启动 timer 跳过 fallback；只有调用本身抛异常才视为未完成。
        """
        try:
            positions = self.main_engine.get_all_positions() or []
            # 标的汇总从零重算 —— 同一标的可能有多个合约月份，必须每次同步都全量重算，
            # 否则 sync_data 之间会出现"幽灵存量"。
            new_underlying: dict[str, int] = defaultdict(int)
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
                underlying = self._resolve_underlying(pos.vt_symbol)
                if underlying is not None:
                    new_underlying[underlying] += net
            self.underlying_position = new_underlying
            self._initial_sync_done = True
            logger.info(
                "已从引擎恢复 %d 个合约持仓: %s（按标的汇总: %s）",
                len(self.position),
                dict(self.position),
                dict(self.underlying_position),
            )
        except Exception as e:
            logger.warning("从引擎读取持仓失败，以 0 起始: %s", e)

    def _schedule_startup_sync_fallback(self) -> None:
        """注册一次性 timer：若 timeout 内 EVENT_ACCOUNT 未到达就主动同步持仓。

        startup_sync_timeout_s 设为 None 或 <= 0 时跳过（测试场景）。
        """
        if self._startup_sync_timeout_s is None or self._startup_sync_timeout_s <= 0:
            return
        self._startup_timer = threading.Timer(
            self._startup_sync_timeout_s, self._run_startup_sync_fallback
        )
        self._startup_timer.daemon = True
        self._startup_timer.start()

    def _run_startup_sync_fallback(self) -> None:
        """启动 timer 到期：若初始同步未完成，主动拉取持仓 + 发告警。"""
        with self._lock:
            if self._initial_sync_done:
                return
        timeout_s = self._startup_sync_timeout_s or 0.0
        logger.warning("启动 %.1fs 内未收到 EVENT_ACCOUNT，主动拉取持仓基线...", timeout_s)
        self._sync_positions_from_engine()
        msg = (
            f"启动 {timeout_s:.0f}s 内未收到账户事件\n已主动从引擎同步持仓基线\n"
            f"请确认 CTP 账户网关是否正常（持仓数：{len(self.position)}）"
        )
        try:
            self.notifier.send(msg, title="风控启动 fallback", force=True)
        except Exception as e:
            logger.error("启动 fallback 告警推送失败: %s", e)

    def _register(self) -> None:
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_ACCOUNT, self.on_account)
        self.event_engine.register(EVENT_TICK, self.on_tick)

    def unregister(self) -> None:
        self.event_engine.unregister(EVENT_TRADE, self.on_trade)
        self.event_engine.unregister(EVENT_ACCOUNT, self.on_account)
        self.event_engine.unregister(EVENT_TICK, self.on_tick)
        if self._startup_timer is not None:
            self._startup_timer.cancel()
            self._startup_timer = None

    def on_tick(self, event: Event) -> None:
        """缓存最近 tick 价格，给 check_order_pre() 做参考价。"""
        try:
            tick = event.data
            price = float(getattr(tick, "last_price", 0) or 0)
            if price <= 0:
                # 脏 tick 直接不缓存，让 pre-gate 因"无参考价"拒发
                return
            ts = getattr(tick, "datetime", None) or datetime.now()
            with self._lock:
                self.latest_tick_price[tick.vt_symbol] = price
                self.latest_tick_at[tick.vt_symbol] = ts
        except Exception as e:
            logger.error("on_tick 异常: %s", e)

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
                underlying = self._resolve_underlying(vt_symbol)
                if underlying is not None:
                    self.underlying_position[underlying] += signed

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

                if self.max_position_per_underlying is not None and underlying is not None:
                    upos = self.underlying_position[underlying]
                    if abs(upos) > self.max_position_per_underlying:
                        self._trip(
                            f"标的 {underlying} 汇总净持仓 {upos} 超过阈值 "
                            f"±{self.max_position_per_underlying}（来自 {vt_symbol}）"
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

    def check_order_pre(
        self,
        vt_symbol: str,
        direction: str,
        price: float,
        volume: int = 1,
        reference_price: float | None = None,
    ) -> tuple[bool, str]:
        """发单前 gate：价格 / 行情 / 熔断状态校验。返回 (allowed, reason)。

        rule 1: 已熔断 → 拒。重启或人工 reset 前都拒。
        rule 2: 限价 <= 0 → 拒。捕获策略代码 bug / 脏 tick 传染。
        rule 3: 无参考价（外部未传 + 缓存空 / 缓存陈旧）→ 拒。
                未订阅或市场闭市后再发单都会落到这一条。
        rule 4: |price - ref| / ref > max_price_deviation → 拒。
                这一条直接堵死脏 tick → 异常市价单的核心场景。

        本函数**只判断不下单也不熔断**——单笔被拒不应升级到全账户熔断。
        触发的 reject 通过 notifier.send（非 critical）告警。
        """
        if self.tripped:
            self._record_reject(vt_symbol, direction, price, "已熔断，禁止开新仓")
            return False, "tripped"

        if price <= 0:
            self._record_reject(vt_symbol, direction, price, f"非法限价 {price}")
            return False, "non_positive_price"

        ref = reference_price
        ref_source = "caller"
        if ref is None:
            with self._lock:
                ref = self.latest_tick_price.get(vt_symbol)
                tick_at = self.latest_tick_at.get(vt_symbol)
            ref_source = "cache"
            if ref is None or ref <= 0:
                self._record_reject(
                    vt_symbol,
                    direction,
                    price,
                    f"无参考价（未订阅 / 未收到有效 tick）vt_symbol={vt_symbol}",
                )
                return False, "no_reference_price"

            # 陈旧 tick 视为无参考价：休市后任何下单都会卡在这里
            if tick_at is not None:
                age = (datetime.now() - tick_at).total_seconds()
                if age > self.max_tick_age_seconds:
                    self._record_reject(
                        vt_symbol,
                        direction,
                        price,
                        f"最近 tick 已陈旧 {age:.0f}s > {self.max_tick_age_seconds:.0f}s",
                    )
                    return False, "stale_reference_price"

        deviation = abs(price - ref) / ref
        if deviation > self.max_price_deviation:
            self._record_reject(
                vt_symbol,
                direction,
                price,
                f"价格偏离 {deviation * 100:.2f}% > 阈值 "
                f"{self.max_price_deviation * 100:.2f}% (ref={ref} src={ref_source})",
            )
            return False, "price_deviation_exceeded"

        return True, "ok"

    def _record_reject(
        self,
        vt_symbol: str,
        direction: str,
        price: float,
        reason: str,
    ) -> None:
        """统一记录 pre-gate 拒发：日志 + 计数 + 节流告警。

        同一 (vt_symbol, reason) 在 cooldown 窗口内重复触发只记日志，
        不再 push notifier；窗口外才再次告警。防止异常循环刷爆告警通道。
        """
        now = datetime.now()
        key = (vt_symbol, reason)
        with self._lock:
            self.pre_gate_rejects += 1
            count = self.pre_gate_rejects
            last = self._reject_alert_last.get(key)
            alert = last is None or (now - last).total_seconds() >= self._reject_alert_cooldown_s
            if alert:
                self._reject_alert_last[key] = now

        logger.warning(
            "pre-gate 拒发: %s %s @%s — %s (累计 %d)",
            vt_symbol,
            direction,
            price,
            reason,
            count,
        )
        if not alert:
            return
        try:
            self.notifier.send(
                f"pre-gate 拒发\n合约: {vt_symbol}\n方向: {direction}\n"
                f"限价: {price}\n原因: {reason}",
                title="风控发单拦截",
            )
        except Exception as e:
            logger.error("pre-gate 告警推送失败: %s", e)

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


def get_active_risk_guard() -> RiskGuard | None:
    """返回当前挂载的 RiskGuard（最后一个 attach）。

    回测环境下没有 attach，返回 None；safe_* 发单包装因此自然降级为透传。
    """
    return _guards[-1] if _guards else None


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
