"""REPLAY 模式：从 DB 加载历史 bar，按节拍合成 tick 喂给策略，再走 SIGNAL_ONLY 同款合成成交。

为什么需要 REPLAY 模式
----------------------
SIGNAL_ONLY（[[project_signal_only_mode]]）只截断 ``send_order``，行情仍依赖 CTP 连接
+ 真实可交易合约。H4 alpha 的 WFA 是在 ``i_continuous.DCE`` 这种"伪连续合约"上证完的，
但 CTP 不认这个 symbol —— 所以 SIGNAL_ONLY 当前没法用 H4 既定参数端到端验证
策略 → notifier 全链路。

REPLAY 把上游的实盘行情换成"DB bar 重放"：
- 无需 CTP，无需交易时段限制
- ``QUANT_MODE=REPLAY`` 一开即跑，~2 分钟搞完 1023 根 60min bar 的全链路
- 下游（合成 Order/Trade → 同步派发 → handler → 通知）和 SIGNAL_ONLY **完全一样**，
  共用 ``signal_only_gateway`` 顶层 free functions。

保真度边界 (Fidelity boundary)
------------------------------
**REPLAY 只测管线连通性与状态机跃迁，不可用于验证微观执行**。具体：
- 每根 bar 只合成 1 个 tick（+末尾 flush tick），没有真盘口、没有真买卖盘价差
- ``send_order`` 立刻 ALLTRADED，没有滑点、没有限价撮合、没有挂单/撤单时序
- 时间被人为压缩（默认 100ms/bar），不能用来评估限频/撤单超时等以 wall-clock
  为边界的逻辑

所以 REPLAY 的有效断言只能是：
- 信号 → send_order → 合成成交 → handler → notifier 全链路联通
- RiskGuard / NotifyListener 收到事件、状态机正确跃迁、无重复推送
- 不阻塞 I/O 违反 Handler 合约（dispatch_sync 内 watchdog WARN）

REPLAY **不**适用：滑点研究、限价单成交率验证、订单簿压力建模、
任何依赖 wall-clock 的限频/限价 logic 的端到端验证。

时钟分离 (Wall-clock vs Logical-clock)
--------------------------------------
``RiskGuard.on_trade`` 用 ``trade.datetime.timestamp()`` 维护 60s 滑动窗口
（max_trades_per_minute）。如果合成 trade 用 ``datetime.now()``，所有 trade 都会
落在 1.7 分钟物理时间内，第 21 笔起 RiskGuard 立刻假性熔断。

修正：ReplayGateway 在 ``_replay_loop`` 里维护 ``_current_synthetic_dt``，每发
一个 tick 都更新；``send_order`` 把这个值传给 ``synthesize_order_trade`` 写到
``order.datetime`` / ``trade.datetime``。这样 RiskGuard 看到的就是"逻辑时间"
—— 1023 根 bar 跨 17 小时，仓位限额仍能正确触发，但限频阈值不会被物理压缩误杀。

设计要点（2026-05-19 与 user/Gemini 对齐）
------------------------------------------
1. 独立类，不嵌套到 SIGNAL_ONLY 工厂：``QUANT_MODE in {LIVE, SIGNAL_ONLY, REPLAY}``。
2. 节拍由 ``REPLAY_BAR_DELAY_MS`` 控制，默认 100ms / bar；置 0 触发风暴测试。
3. 复用 ``signal_only_gateway`` 模块顶层 helpers（``synthesize_order_trade`` /
   ``dispatch_sync`` / ``notify_signal`` / ``OrderIdSequencer``）。
4. 通知模板里"模式"字段显示 ``REPLAY (历史回放，非实盘)``。

BarGenerator 行为
-----------------
``vnpy_ctastrategy.BarGenerator.update_tick`` 仅在 ``tick.datetime.minute`` 或 ``.hour``
变化时 flush 上一根 bar。所以每根历史 bar 我们只发 1 个 tick（datetime 错开 1 分钟），
下一个 tick 抵达时上一根就被 on_bar 推给策略；最后再补 1 个 "flush tick" 让最后一根
也能落地。

历史预热
--------
``DoubleMaStrategy.on_init`` 调 ``load_bar(slow_window+1)``，但 DB 里 rb2410 只有
HOUR bar、CtaTemplate.load_bar 默认 MINUTE，所以载入 0 条；am.inited 改由"前
``slow_window+1`` 个回放 tick 累积"驱动 —— 等价于以"合成分钟"为单位运行策略。
对 SIT 而言 OK，因为我们只需"信号被产生"，不在乎信号点位精度。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timedelta

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Product
from vnpy.trader.event import EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    BarData,
    CancelRequest,
    ContractData,
    OrderRequest,
    SubscribeRequest,
    TickData,
)

from .notifier import INotifier, get_notifier
from .signal_only_gateway import (
    OrderIdSequencer,
    dispatch_sync,
    notify_signal,
    synthesize_order_trade,
)

logger = logging.getLogger("replay_gateway")

GATEWAY_NAME = "REPLAY"

# 节拍：bar 之间的 wall-clock 间隔。默认 100ms → 1023 根 ≈ 1.7 分钟。
# 置 0 触发风暴测试，用于验证 RiskGuard.max_trades_per_minute=20 拦截。
DEFAULT_BAR_DELAY_MS = 100


class ReplayGateway(BaseGateway):
    """读 DB bar → 合成 tick → 触发策略 → 走 SIGNAL_ONLY 同款合成成交。

    使用流程
    --------
        gw_cls = ReplayGateway
        main_engine.add_gateway(gw_cls)
        main_engine.connect(setting={"symbols": [("rb2410", Exchange.SHFE)]}, "REPLAY")
        # 策略 init/start 完成后：
        gw = main_engine.get_gateway("REPLAY")
        gw.start_replay(bars, delay_ms=100)

    线程模型
    --------
    - ``connect`` 同步推 ContractData，让 CtaEngine 能 resolve vt_symbol
    - ``subscribe`` no-op（合约一开始就在 main_engine.contracts 里）
    - ``start_replay`` 启动 daemon 线程，逐 bar **同步**派发 tick：tick →
      strategy.on_tick → BarGenerator → on_bar → send_order → 合成回报 →
      handler。整条链在 replay 线程的同一栈帧上完成，下一根 bar 才被推。
      这与 SIGNAL_ONLY 的同步合约一致，并消除一个会污染 ``_current_synthetic_dt``
      的竞态：若 tick 走 ``event_engine.put`` 异步队列，回放线程可能在 worker
      处理第 0 根的 send_order 前已经把 dt 推到第 N 根，所有合成 trade 拿到
      同一个最新 dt → RiskGuard 假性熔断回归（Gemini 2026-05-19 指出）。
    """

    default_name: str = GATEWAY_NAME
    default_setting: dict = {}
    exchanges: list[Exchange] = list(Exchange)

    def __init__(self, event_engine: EventEngine, gateway_name: str = GATEWAY_NAME) -> None:
        super().__init__(event_engine, gateway_name)
        self._id_seq = OrderIdSequencer()
        self._signal_notifier: INotifier | None = None
        self._replay_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._registered_contracts: list[ContractData] = []
        # 当前回放进度的"逻辑时间"。send_order 用它给合成 trade 打时间戳，避免
        # RiskGuard 用 wall-clock 60s 窗口误判（详见模块顶部"时钟分离"段落）。
        # 回放线程写、send_order 调用栈读 —— 单读单写，加 lock 既无必要也无价值。
        self._current_synthetic_dt: datetime | None = None
        logger.warning(
            "REPLAY 模式启用 — gateway=%s 将从 DB 重放历史 bar，所有 send_order 合成假成交",
            gateway_name,
        )

    # ------------------------------------------------------------------
    # 显式配置
    # ------------------------------------------------------------------

    def set_signal_notifier(self, notifier: INotifier) -> None:
        """与 SIGNAL_ONLY 对齐 — 测试态注入 NullNotifier，运行态走 get_notifier()。"""
        self._signal_notifier = notifier

    # ------------------------------------------------------------------
    # BaseGateway 必需接口
    # ------------------------------------------------------------------

    def connect(self, setting: dict) -> None:
        """注册合约让 CtaEngine 能 subscribe。

        setting 形如 ``{"symbols": [("rb2410", Exchange.SHFE), ...]}``。
        每个合约都推一条 ContractData 到 main_engine，确保 strategy_engine
        能在 subscribe_data 阶段拿到 contract 完成回调订阅。
        """
        symbols: Sequence[tuple[str, Exchange]] = setting.get("symbols", [])
        for symbol, exchange in symbols:
            contract = ContractData(
                gateway_name=self.gateway_name,
                symbol=symbol,
                exchange=exchange,
                name=symbol,
                product=Product.FUTURES,
                size=1,
                pricetick=1.0,
                min_volume=1,
                history_data=False,
            )
            self._registered_contracts.append(contract)
            self.on_contract(contract)
            logger.info("REPLAY: 注册合约 %s.%s", symbol, exchange.value)
        self.write_log("REPLAY gateway 连接完成，等待 start_replay()")

    def close(self) -> None:
        self._stop_flag.set()
        if self._replay_thread and self._replay_thread.is_alive():
            self._replay_thread.join(timeout=2.0)
            if self._replay_thread.is_alive():
                logger.warning("REPLAY 回放线程 join 超时（2s）")

    def subscribe(self, req: SubscribeRequest) -> None:
        # 合约已在 connect 里推过，此处无需向"行情服务器"再次注册
        logger.debug("REPLAY: subscribe %s.%s acknowledged", req.symbol, req.exchange.value)

    def send_order(self, req: OrderRequest) -> str:
        """与 SIGNAL_ONLY 同款：合成 ALLTRADED + Trade 同步派发，再发信号通知。

        ``trade.datetime`` 锚到 ``_current_synthetic_dt``（最近一个回放 tick 的
        逻辑时间）而非 ``datetime.now()``，这样 RiskGuard 的限频窗口看到的是
        "策略逻辑时间序列"而不是被压缩成 1.7 分钟的物理时间。
        """
        orderid = self._id_seq.next_orderid()
        vt_orderid = f"{self.gateway_name}.{orderid}"
        order, trade = synthesize_order_trade(
            req,
            gateway_name=self.gateway_name,
            orderid=orderid,
            tradeid=self._id_seq.next_tradeid(),
            now=self._current_synthetic_dt,  # None → helper 回退 datetime.now()
        )

        dispatch_sync(self.event_engine, EVENT_ORDER, order)
        dispatch_sync(self.event_engine, EVENT_TRADE, trade)

        notify_signal(
            self._signal_notifier or get_notifier(),
            req,
            mode_label="REPLAY（历史回放，非实盘）",
        )

        return vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        logger.debug("REPLAY: cancel_order ignored for %s", req.orderid)

    def query_account(self) -> None:
        pass

    def query_position(self) -> None:
        pass

    # ------------------------------------------------------------------
    # 回放驱动
    # ------------------------------------------------------------------

    def start_replay(
        self,
        bars: Sequence[BarData],
        delay_ms: int = DEFAULT_BAR_DELAY_MS,
        block: bool = False,
    ) -> threading.Thread:
        """启动回放线程，按 ``delay_ms`` 节拍推 tick。

        Parameters
        ----------
        bars
            按时间升序的 BarData 序列。
        delay_ms
            每两个 bar 之间的 wall-clock sleep 毫秒。0 = 风暴模式。
        block
            True 时主线程在此 join，便于无 GUI 的单元/集成测试同步等待结束。
        """
        if self._replay_thread and self._replay_thread.is_alive():
            raise RuntimeError("REPLAY: 已有回放线程在跑")

        self._stop_flag.clear()
        thread = threading.Thread(
            target=self._replay_loop,
            args=(list(bars), delay_ms),
            name="ReplayGateway-loop",
            daemon=True,
        )
        self._replay_thread = thread
        thread.start()
        if block:
            thread.join()
        return thread

    def stop_replay(self) -> None:
        """对外暴露的优雅停止入口（GUI shutdown / 测试 cleanup）。"""
        self._stop_flag.set()

    def _replay_loop(self, bars: list[BarData], delay_ms: int) -> None:
        """逐 bar 合成 tick，发送给 EventEngine。

        BarGenerator 的 minute 边界检测要求相邻 tick 的 ``datetime.minute`` 不同。
        所以这里给第 i 根 bar 用一个 ``i`` 分钟偏移的 datetime，让 BarGenerator 把
        上一根 bar flush 出去。最后追加 1 个 "flush tick" 保证最后一根落地。
        """
        if not bars:
            logger.warning("REPLAY: bars 序列为空，回放线程立即退出")
            return

        base_dt = datetime(2026, 1, 1, 9, 0, 0)
        delay_s = delay_ms / 1000.0
        logger.info(
            "REPLAY: 开始回放 %d 根 bar，节拍 %dms (~%.1f 秒预计耗时)",
            len(bars),
            delay_ms,
            len(bars) * delay_s,
        )

        for i, bar in enumerate(bars):
            if self._stop_flag.is_set():
                logger.info("REPLAY: stop_flag 置位，回放在第 %d 根 bar 退出", i)
                return
            synthetic_dt = base_dt + timedelta(minutes=i)
            # 必须先写 _current_synthetic_dt 再 emit tick：tick 同步触发 on_bar →
            # 策略 send_order → 此处读取 self._current_synthetic_dt。顺序反了会
            # 让第一根 bar 的合成 trade 拿到上一根 / None 的时间戳。
            # 同步派发：见类 docstring。tick 不走 event_engine.put，避免回放线程
            # 跑得比 worker 快导致所有合成 trade 抓到同一个 dt。
            self._current_synthetic_dt = synthetic_dt
            tick = TickData(
                gateway_name=self.gateway_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                datetime=synthetic_dt,
                last_price=bar.close_price,
                last_volume=bar.volume,
                volume=bar.volume,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                pre_close=bar.open_price,
                bid_price_1=bar.close_price,
                ask_price_1=bar.close_price,
                bid_volume_1=1,
                ask_volume_1=1,
            )
            self._dispatch_tick_sync(tick)
            if delay_s > 0:
                time.sleep(delay_s)

        # Flush tick：与最后一根 bar 在 minute 上错开，触发 BarGenerator on_bar
        flush_dt = base_dt + timedelta(minutes=len(bars))
        self._current_synthetic_dt = flush_dt
        last = bars[-1]
        flush_tick = TickData(
            gateway_name=self.gateway_name,
            symbol=last.symbol,
            exchange=last.exchange,
            datetime=flush_dt,
            last_price=last.close_price,
            volume=last.volume,
            bid_price_1=last.close_price,
            ask_price_1=last.close_price,
            bid_volume_1=1,
            ask_volume_1=1,
        )
        self._dispatch_tick_sync(flush_tick)
        logger.info("REPLAY: 回放完成 (%d 根 bar + 1 flush tick)", len(bars))

    def _dispatch_tick_sync(self, tick: TickData) -> None:
        """模拟 BaseGateway.on_tick 的双路推送（general + per-vt_symbol），但同步。

        实盘下 ``BaseGateway.on_tick`` 走 event_engine.put，由 worker 异步派发。
        REPLAY 必须同步：避免回放线程跑得比 worker 快，导致 ``_current_synthetic_dt``
        在 worker 真正调 send_order 之前已被覆写到更后面的 bar。
        """
        dispatch_sync(self.event_engine, EVENT_TICK, tick)
        dispatch_sync(self.event_engine, EVENT_TICK + tick.vt_symbol, tick)
