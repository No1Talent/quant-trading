"""策略层公用工具：

- `safe_callback`：装饰 on_bar/on_tick 等回调，吞异常 + 写日志，避免单 bar 异常拖崩整个策略。
- `safe_buy/safe_sell/safe_short/safe_cover`：发单前调一遍 RiskGuard.check_order_pre()，
  通过才透传到 CtaTemplate.buy/...；被拒则 write_log 并返回空列表（与 vn.py 原生返回形态一致）。
  RiskGuard 未挂载（回测）时无 gate，自动透传。
- 同时旁路 SignalLog.append(...)：每次发单/被拒都落一行 JSONL，作为信号生命周期
  的可观察基线（独立于 vn.py 的字符串日志）。默认实现是 NullSignalLog（零副作用），
  run.py 在 LIVE/SIGNAL_ONLY 模式下显式 set 到 FileSignalLog。
- `BaseCtaStrategy`：吃掉所有策略相同的 lifecycle boilerplate（on_start/on_stop/on_trade
  /on_order/on_stop_order）。子类只需实现 on_init/on_bar/on_tick。发单必须走 safe_*
  —— tests/test_safe_send_migration.py 扫描子类源码，禁止裸调用 self.buy/sell/short/cover。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import time as dt_time
from functools import wraps
from typing import Any

from vnpy.trader.constant import Interval
from vnpy_ctastrategy import BarData, BarGenerator, CtaTemplate

from utils.risk_guard import get_active_risk_guard
from utils.signal_log import get_signal_log

logger = logging.getLogger("strategy")

#: 规范的 interval 字符串→枚举映射，单一事实源。research/backtest_runner 与 run.py(REPLAY)
#: 都从这里取，避免"回测一份、REPLAY 一份、策略默认又一份"这类 interval 定义漂移。
STR_TO_INTERVAL: dict[str, Interval] = {
    "1m": Interval.MINUTE,
    "1h": Interval.HOUR,
    "1d": Interval.DAILY,
}


def safe_callback(func: Callable) -> Callable:
    """装饰策略回调（on_bar/on_tick），异常时 write_log 后吞掉，避免策略整个挂掉。"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            if hasattr(self, "write_log"):
                self.write_log(f"[ERROR] {func.__name__} 异常: {e}\n{tb}")
            else:
                logger.error("%s 异常: %s\n%s", func.__name__, e, tb)

    return wrapper


def _gated_send(
    strategy: Any,
    method_name: str,
    price: float,
    volume: int,
    *args: Any,
    **kwargs: Any,
) -> list:
    """共用包装：gate → SignalLog 落盘 → 透传到 strategy.<method_name>。

    `method_name` ∈ {"buy", "sell", "short", "cover"}，同时作为
    RiskGuard.check_order_pre 的 direction 标签使用（告警里直接读得到 buy/sell
    /short/cover 而不是开仓 / 平仓的一侧名）。其余位置 / 关键字参数（如
    `stop=True`、`lock=True`）原样转发，保持与 CtaTemplate 签名一致。

    SignalLog 旁路：无论 RiskGuard 允许还是拒绝，都落一条记录。这样事后排查时
    "策略本来想做什么 + 风控做了什么决定"是同一份事件流，不需要 join 两份日志。
    """
    vt_symbol = getattr(strategy, "vt_symbol", "")
    strategy_name = getattr(strategy, "strategy_name", strategy.__class__.__name__)
    signal_log = get_signal_log()

    guard = get_active_risk_guard()
    if guard is not None:
        allowed, reason = guard.check_order_pre(vt_symbol, method_name, price, volume)
        if not allowed:
            msg = (
                f"[RISK_GATE] {method_name} 被拒 vt_symbol={vt_symbol} "
                f"price={price} vol={volume} reason={reason}"
            )
            if hasattr(strategy, "write_log"):
                strategy.write_log(msg)
            else:
                logger.warning(msg)
            signal_log.append(
                strategy_name=strategy_name,
                vt_symbol=vt_symbol,
                side=method_name,
                price=price,
                volume=volume,
                allowed=False,
                reject_reason=reason,
            )
            return []

    signal_log.append(
        strategy_name=strategy_name,
        vt_symbol=vt_symbol,
        side=method_name,
        price=price,
        volume=volume,
        allowed=True,
        reject_reason=None,
    )
    fn = getattr(strategy, method_name)
    result = fn(price, volume, *args, **kwargs)
    # SIGNAL_ONLY / REPLAY 把合成 Order/Trade 攒在 gateway buffer 里等 flush，
    # 这里 CtaEngine.send_server_order 已经 plant 完 orderid → strategy 映射，
    # 是最早能安全派发的同步点。LIVE 模式下 CtpGateway 没有这个方法 —— 自然 no-op。
    _flush_signal_gateway_pending(strategy)
    return result


def _flush_signal_gateway_pending(strategy: Any) -> None:
    """触发 SIGNAL_ONLY / REPLAY gateway 的 ``flush_pending_dispatches``。

    通过 ``strategy.cta_engine.main_engine.gateways`` 遍历，找到所有支持
    ``flush_pending_dispatches`` 协议的 gateway 调用。LIVE 的 CtpGateway 没有这个
    方法 → 跳过；签名鸭子类型让未来再多一种 signal-mode gateway 时不用改这里。

    单 gateway 异常吞掉日志 —— flush 失败不应阻断后续策略逻辑（事件还在 buffer 里，
    下次 flush 还有机会）。
    """
    try:
        main_engine = strategy.cta_engine.main_engine
    except AttributeError:
        return
    gateway_names = getattr(main_engine, "gateways", None)
    if not gateway_names:
        return
    for name in list(gateway_names):
        try:
            gateway = main_engine.get_gateway(name)
        except Exception:
            continue
        flush = getattr(gateway, "flush_pending_dispatches", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                logger.exception("flush_pending_dispatches on %s failed", name)


def safe_buy(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.buy()。开多。"""
    return _gated_send(strategy, "buy", price, volume, *args, **kwargs)


def safe_sell(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.sell()。平多。"""
    return _gated_send(strategy, "sell", price, volume, *args, **kwargs)


def safe_short(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.short()。开空。"""
    return _gated_send(strategy, "short", price, volume, *args, **kwargs)


def safe_cover(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.cover()。平空。"""
    return _gated_send(strategy, "cover", price, volume, *args, **kwargs)


class BaseCtaStrategy(CtaTemplate):
    """CTA 策略默认基类：把所有策略复用的 lifecycle boilerplate 收编到一处。

    子类约束：
    - 必须实现 `on_init`（载入历史 bar 数取决于具体指标窗口）
    - 必须实现 `on_bar`（信号逻辑）；bar 驱动型策略的 `on_tick` 通常仅做 `bg.update_tick`
    - 发单**只能**走 `safe_buy/sell/short/cover` —— 直接调 `self.buy(...)` 会绕过
      RiskGuard pre-gate 与 SignalLog 旁路，下游分析跨模式 diff 会失真。
      `tests/test_safe_send_migration.py` 会扫源码强制。

    默认实现可覆盖的场景：
    - `on_start`：默认日志"策略启动 参数: …"，需要额外初始化时叠加
    - `on_stop`：默认日志 + sync_data；通常不必动
    - `on_trade`：默认日志 + put_event + sync_data；策略需要在 on_trade 维护额外
      状态（极少见，目前没有）才覆盖
    - `on_order` / `on_stop_order`：默认 no-op；vn.py 已用 EVENT_ORDER 推 NotifyListener

    时间框架契约（单一事实源）：
    - `bar_interval` / `bar_window`：策略在哪个粒度交易。基类据此统一构建
      `BarGenerator`，使 **LIVE 的聚合粒度 == 声明的 bar_interval**，并让
      `on_init` 里的 `load_bar(N)` 自动按该粒度预热。回测 / REPLAY 同样应读这两个值。
      改一处即全链路同步，杜绝"研究在 1h、实盘 BarGenerator 默认却吐 1min"的沉默漂移。
    - `live_eligible`：是否允许进入实盘订单路径（LIVE / SIGNAL_ONLY）。日线换月类
      研究策略设 False，由 `install_live_eligibility_guard` 拦截。
    - `uses_bar_generator`：tick 原生策略（直接吃 on_tick）设 False，基类不建 bar。
    """

    bar_interval: str = "1h"  # "1m" / "1h" / "1d"
    bar_window: int = 1  # 多少根源 bar 聚合成一根（当前仅验证 window=1）
    daily_end: dt_time = dt_time(15, 0)  # 仅 DAILY 聚合用到（日盘收盘时刻）
    live_eligible: bool = True
    uses_bar_generator: bool = True

    def __init__(self, cta_engine: Any, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # bar_interval 等是类属性，super().__init__ 后即可读到。bar 型策略统一在此
        # 构建 BarGenerator —— 把"实盘聚合粒度 == 声明粒度"做成基类不变量，而不是
        # 留给每个子类各写一句 BarGenerator(self.on_bar)（那等于默认 1 分钟）。
        self.bg: BarGenerator | None = None
        if self.uses_bar_generator:
            self.bg = self._build_bar_generator()

    @property
    def resolved_bar_interval(self) -> Interval:
        """`bar_interval` 字符串 → vn.py `Interval` 枚举（非法值 fail-fast）。"""
        try:
            return STR_TO_INTERVAL[self.bar_interval]
        except KeyError:
            raise ValueError(
                f"{type(self).__name__}.bar_interval={self.bar_interval!r} 非法，"
                f"应为 {sorted(STR_TO_INTERVAL)} 之一"
            ) from None

    def _build_bar_generator(self) -> BarGenerator:
        """按声明粒度构建 BarGenerator，使 on_bar 永远收到"声明粒度"的 bar。

        - 1m + window=1：on_bar 每分钟直接触发（与回测喂 1m 一致），无聚合层。
        - 其余（1h / 1d / N 分钟）：分钟源 bar 转发进聚合器，窗口满才回调 on_bar，
          使 LIVE（tick→1min→聚合）与回测（DB 直接喂该粒度 bar）口径一致。
        """
        interval = self.resolved_bar_interval
        if interval == Interval.MINUTE and self.bar_window == 1:
            return BarGenerator(self.on_bar)
        if interval == Interval.DAILY:
            return BarGenerator(
                self._on_source_bar,
                self.bar_window,
                self.on_bar,
                interval=interval,
                daily_end=self.daily_end,
            )
        return BarGenerator(self._on_source_bar, self.bar_window, self.on_bar, interval=interval)

    def _on_source_bar(self, bar: BarData) -> None:
        """1 分钟源 bar → 聚合器；窗口完成才回调 self.on_bar。"""
        if self.bg is not None:
            self.bg.update_bar(bar)

    def load_bar(
        self,
        days: int,
        interval: Interval | None = None,
        callback: Callable | None = None,
        use_database: bool = False,
    ) -> None:
        """覆写 CtaTemplate.load_bar：interval 缺省取声明的 bar_interval。

        子类 on_init 里的 `self.load_bar(N)` 因此自动按正确粒度预热，而不是
        vn.py 默认的 1 分钟 —— 否则 1h 策略实盘会用 1min 历史暖机后突然切到 1h。
        """
        if interval is None:
            interval = self.resolved_bar_interval
        super().load_bar(days, interval, callback, use_database)

    @safe_callback
    def on_tick(self, tick: Any) -> None:
        """bar 型策略默认把 tick 喂给 BarGenerator；tick 原生策略覆写本方法。"""
        if self.uses_bar_generator and self.bg is not None:
            self.bg.update_tick(tick)

    def on_start(self) -> None:
        params = ", ".join(f"{p}={getattr(self, p)}" for p in self.parameters)
        self.write_log(f"策略启动 参数: {params}")

    def on_stop(self) -> None:
        self.write_log(f"策略停止 当前持仓: {self.pos}")
        self.sync_data()

    def on_order(self, order) -> None:
        pass

    def on_trade(self, trade) -> None:
        self.write_log(
            f"成交 {trade.direction.value} {trade.offset.value} "
            f"价格={trade.price} 数量={trade.volume}"
        )
        self.put_event()
        self.sync_data()

    def on_stop_order(self, stop_order) -> None:
        pass


def is_live_eligible(strategy_class: type) -> bool:
    """研究型策略（`live_eligible=False`）不应进入实盘订单路径。

    缺省 True：未声明该属性的第三方/旧策略保持原有可上线行为，只有显式标注
    研究型的才被拦截。
    """
    return bool(getattr(strategy_class, "live_eligible", True))


def install_live_eligibility_guard(cta_engine: Any, log: logging.Logger | None = None) -> None:
    """包裹 `cta_engine.add_strategy`，拒绝 `live_eligible=False` 的策略进实盘。

    仅在 LIVE / SIGNAL_ONLY（真实行情 + 真/合成订单）安装；REPLAY / 回测不装。
    GUI 运行时新增策略与启动期 `cta_strategy_setting.json` 加载都经由
    `add_strategy`，包裹这一处即同时拦住两条路径。拦截 = 写 CRITICAL 日志并跳过
    （不抛异常，保持引擎可继续加载其余合规策略）。

    幂等：重复调用不会二次包裹（用 `_live_guard_installed` 标记）。
    """
    if getattr(cta_engine, "_live_guard_installed", False):
        return
    logger_ = log or logger
    original = cta_engine.add_strategy
    classes = cta_engine.classes  # class_name -> strategy class

    @wraps(original)
    def guarded(class_name: str, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        cls = classes.get(class_name)
        if cls is not None and not is_live_eligible(cls):
            logger_.critical(
                "⛔ 拒绝加载研究型策略 %s（live_eligible=False，研究粒度=%s）到实盘路径："
                "其 alpha 不为实盘 bar 流设计，跳过。仅可用于回测 / REPLAY。",
                class_name,
                getattr(cls, "bar_interval", "?"),
            )
            return None
        return original(class_name, strategy_name, vt_symbol, setting)

    cta_engine.add_strategy = guarded
    cta_engine._live_guard_installed = True
