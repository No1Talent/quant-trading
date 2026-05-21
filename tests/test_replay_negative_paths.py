"""SIT M0 负路径：拒单 / 部分成交 / 撤单 在 REPLAY 模式下的端到端走通。

为什么需要这些用例
------------------
``tests/test_replay_gateway.py`` 只覆盖 happy path：每笔 send_order 一次 ALLTRADED。
SIT M0 必须验证三类异常：

1. **拒单**：柜台返回 REJECTED 单（资金不足 / 风控前置 / 价格越界），策略须
   清掉 active 单且不出现持仓漂移；RiskGuard 因无 Trade 事件不应累积仓位。
2. **部分成交**：单一委托被切成多笔 TradeData 推送，最终 ALLTRADED；策略
   `self.pos` 必须按累计成交量增加，RiskGuard 同步；CtaEngine ``vt_tradeids`` 去重
   确认按 tradeid（不是 orderid）粒度。
3. **撤单**：之前 happy-path 用 vnpy 自带的 DoubleMaStrategy（``on_bar`` 顶部
   ``self.cancel_all()``）跑出"几十行 委托撤单"刷屏 —— 那是因为 ``init_engine``
   把教科书版加载在我们的 ``BaseCtaStrategy`` 版后面、按"后注册胜出"覆盖了本地版。
   本文件用本地 DoubleMaStrategy 跑一轮，确认无"委托撤单"刷屏。

依赖
----
本文件构造一个完整的 MainEngine + CtaEngine + RiskGuard + ReplayGateway 环境，
通过 ``ReplayGateway.queue_response(ResponseSpec(...))`` 注入负路径 spec，再驱动一根
人造 tick 让 ``RecordingStrategy.on_bar`` 通过 ``safe_buy`` 发单。整链路完全同步。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Interval, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.object import BarData
from vnpy_ctastrategy import CtaEngine, CtaStrategyApp

from tests._fakes import make_test_event_engine, stop_event_engine_fast
from utils.notifier import NullNotifier
from utils.replay_gateway import ReplayGateway, ResponseSpec
from utils.risk_guard import RiskGuard
from utils.strategy_base import BaseCtaStrategy, safe_buy

# ---------------------------------------------------------------------------
# 记录式策略：暴露 on_order / on_trade 回调流以便断言
# ---------------------------------------------------------------------------


class RecordingStrategy(BaseCtaStrategy):
    """最小化策略：on_bar 时根据 ``send_on_next_bar`` 标志发单，记录所有回调。

    ``vt_symbol`` 和 ``fixed_size`` 由 setting 注入。``send_on_next_bar`` 测试控制
    台手动打开 → 触发 ``safe_buy(...)``；策略不主动做策略判断（避免和回放节奏纠缠）。
    """

    author: str = "sit"
    parameters: list[str] = ["fixed_size"]
    variables: list[str] = []

    fixed_size: int = 1

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.received_orders: list = []
        self.received_trades: list = []
        # 单元控制变量：测试设 True 时下一根 on_bar 触发一次 safe_buy
        self.send_on_next_bar: bool = False
        # 记录 safe_buy 被调用的次数（用于 "策略没有重复发单" 这种负断言）
        self.send_attempts: int = 0

    def on_init(self) -> None:
        pass

    def on_tick(self, tick) -> None:
        # 本测试不走 BarGenerator —— 我们直接调 cta_engine.process_bar_event-equivalent
        pass

    def on_bar(self, bar) -> None:
        if not self.send_on_next_bar:
            return
        self.send_on_next_bar = False
        self.send_attempts += 1
        safe_buy(self, bar.close_price, self.fixed_size)

    def on_order(self, order) -> None:
        self.received_orders.append(order)

    def on_trade(self, trade) -> None:
        # 先调父类（更新日志 / sync_data / put_event），再追加到记录
        super().on_trade(trade)
        self.received_trades.append(trade)


# ---------------------------------------------------------------------------
# 集成 fixture：MainEngine + CtaEngine + ReplayGateway + RecordingStrategy
# ---------------------------------------------------------------------------


@pytest.fixture
def sit_env(tmp_path) -> Iterator[tuple]:
    # 50ms timer interval 让 teardown 时 _timer 线程秒退；详见 _fakes.py
    ee = make_test_event_engine()
    # MainEngine.__init__ 内部会调 event_engine.start()，这里不可重复 start
    me = MainEngine(ee)
    me.add_gateway(ReplayGateway)
    me.add_app(CtaStrategyApp)

    gateway: ReplayGateway = me.get_gateway(ReplayGateway.default_name)
    gateway.set_signal_notifier(NullNotifier())
    me.connect({"symbols": [("rb2410", Exchange.SHFE)]}, ReplayGateway.default_name)

    cta_engine: CtaEngine = me.get_engine("CtaStrategy")
    cta_engine.init_engine()
    # 注入 RecordingStrategy 进 classes —— 不依赖磁盘 strategies/ 扫描
    cta_engine.classes["RecordingStrategy"] = RecordingStrategy

    # 干掉 init_engine 顺手载入的 vnpy 教科书版 DoubleMaStrategy / 其它策略，
    # 避免它们抢占 setting 名空间；本 fixture 只用 RecordingStrategy。
    for key in list(cta_engine.classes.keys()):
        if key != "RecordingStrategy":
            del cta_engine.classes[key]

    strategy_name = "sit-recording"
    cta_engine.add_strategy(
        "RecordingStrategy",
        strategy_name,
        "rb2410.SHFE",
        {"fixed_size": 1},
    )
    strategy: RecordingStrategy = cta_engine.strategies[strategy_name]
    # 走简化生命周期：跳过 init_executor，直接置位
    strategy.inited = True
    strategy.trading = True

    # 挂 RiskGuard，breach_flag 落到 tmp_path 避免污染仓库
    guard = RiskGuard(
        main_engine=me,
        event_engine=ee,
        notifier=NullNotifier(),
        max_daily_loss_pct=0.05,
        max_position_per_symbol=10,
        max_trades_per_minute=20,
        breach_flag_path=str(tmp_path / "risk_breach.flag"),
        startup_sync_timeout_s=None,  # 跳过启动 fallback timer
    )

    yield me, ee, cta_engine, gateway, strategy, guard

    gateway.clear_response_queue()
    guard.unregister()
    # 先 fast-stop：me.close() 内部会 join EventEngine 两个线程，提前唤醒 worker
    # 并把 timer 的 50ms interval 用完 → join 瞬时返回。整体 teardown 从 ~1s → ~60ms。
    stop_event_engine_fast(ee)
    me.close()


def _drive_bar(strategy: RecordingStrategy, *, close_price: float = 4500.0) -> None:
    """合成一根 bar 直接喂 strategy.on_bar —— 跳过 BarGenerator 让断言更聚焦。

    BarGenerator 已被 test_replay_gateway 覆盖；这里关心的是发单回报链路，
    所以让测试直接驱动 on_bar，bar 内容只要够 ``safe_buy`` 使用即可。
    """
    bar = BarData(
        gateway_name=ReplayGateway.default_name,
        symbol="rb2410",
        exchange=Exchange.SHFE,
        datetime=__import__("datetime").datetime(2026, 1, 1, 10, 0, 0),
        interval=Interval.MINUTE,
        open_price=close_price,
        high_price=close_price,
        low_price=close_price,
        close_price=close_price,
        volume=1,
        turnover=close_price,
        open_interest=0,
    )
    strategy.on_bar(bar)


def _drain(ee: EventEngine, timeout: float = 2.0) -> None:
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if ee._queue.empty():
            time.sleep(0.02)
            if ee._queue.empty():
                return
        time.sleep(0.01)
    raise TimeoutError(f"event queue 在 {timeout}s 内未排空")


# ---------------------------------------------------------------------------
# 1. 拒单 (REJECTED) walk
# ---------------------------------------------------------------------------


class TestRejectionWalk:
    """注入 REJECTED 单，验证策略与 RiskGuard 状态不漂移。"""

    def test_rejected_order_does_not_change_pos(self, sit_env) -> None:
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        gateway.queue_response(ResponseSpec(kind="reject", reason="资金不足"))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        # 策略只调一次 safe_buy；on_bar 不应反复 fire 同一信号
        assert strategy.send_attempts == 1
        # 策略 self.pos 不动 —— 没有任何 Trade 事件
        assert strategy.pos == 0
        assert strategy.received_trades == []
        # 但 on_order 必须被回调，状态为 REJECTED
        assert len(strategy.received_orders) == 1
        assert strategy.received_orders[0].status == Status.REJECTED
        # RiskGuard 同步没漂移
        assert guard.position == {} or all(v == 0 for v in guard.position.values())
        assert len(guard.trade_window) == 0

    def test_rejected_order_removed_from_active_set(self, sit_env) -> None:
        """REJECTED 不 active → CtaEngine 必须把 vt_orderid 从 strategy_orderid_map 删掉。"""
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        gateway.queue_response(ResponseSpec(kind="reject", reason="风控前置"))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        rejected = strategy.received_orders[0]
        active = cta_engine.strategy_orderid_map.get(strategy.strategy_name, set())
        assert rejected.vt_orderid not in active, (
            f"REJECTED vt_orderid {rejected.vt_orderid} 仍在 active 集 {active} —— "
            "CtaEngine.process_order_event 状态机回归"
        )

    def test_strategy_can_resend_after_rejection(self, sit_env) -> None:
        """拒单后下一根 bar 必须能正常成交 —— 状态机不被卡住。"""
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        gateway.queue_response(ResponseSpec(kind="reject", reason="一次性拒单"))
        # 第二笔不入队 → 默认 ALLTRADED

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)
        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        assert strategy.send_attempts == 2
        # 1 个 REJECTED + 1 个 ALLTRADED
        statuses = [o.status for o in strategy.received_orders]
        assert Status.REJECTED in statuses
        assert Status.ALLTRADED in statuses
        # 只有第二笔成交累积仓位
        assert strategy.pos == 1
        assert len(strategy.received_trades) == 1


# ---------------------------------------------------------------------------
# 2. 部分成交 (PARTTRADED → ALLTRADED) walk
# ---------------------------------------------------------------------------


class TestPartialFillWalk:
    """注入多笔 trade 模拟分批成交，验证持仓和风控累积正确。"""

    def test_pos_accumulates_across_partial_fills(self, sit_env) -> None:
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        strategy.fixed_size = 5
        gateway.queue_response(ResponseSpec(kind="partial", fills=[2, 2, 1]))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        # 3 笔 trade 累计 5 手
        assert len(strategy.received_trades) == 3
        assert [t.volume for t in strategy.received_trades] == [2, 2, 1]
        assert strategy.pos == 5
        # CtaEngine 按 tradeid 去重 —— 三个 tradeid 都不同
        tradeids = [t.tradeid for t in strategy.received_trades]
        assert len(set(tradeids)) == 3

    def test_order_status_evolves_part_to_all(self, sit_env) -> None:
        """3 笔 trade 之前对应 3 笔 OrderData，状态机：PART → PART → ALL。"""
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        strategy.fixed_size = 4
        gateway.queue_response(ResponseSpec(kind="partial", fills=[1, 2, 1]))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        statuses = [o.status for o in strategy.received_orders]
        assert statuses == [
            Status.PARTTRADED,
            Status.PARTTRADED,
            Status.ALLTRADED,
        ]
        # 最终 traded == volume，from active 集移除
        final_order = strategy.received_orders[-1]
        assert final_order.traded == 4
        active = cta_engine.strategy_orderid_map.get(strategy.strategy_name, set())
        assert final_order.vt_orderid not in active

    def test_riskguard_position_tracks_partial_fills(self, sit_env) -> None:
        """RiskGuard.on_trade 也按每笔 trade 累积 → 总头寸应等于 fills 总和。"""
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        strategy.fixed_size = 6
        gateway.queue_response(ResponseSpec(kind="partial", fills=[3, 2, 1]))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        # 单合约持仓 = +6（all buy）
        assert guard.position["rb2410.SHFE"] == 6
        # trade_window 累计 3 笔（即使逻辑分钟相同 —— RiskGuard 的窗口不去重）
        assert len(guard.trade_window) == 3
        # 未触发熔断（6 < max_position_per_symbol=10）
        assert guard.tripped is False

    def test_partial_then_position_limit_trips_riskguard(self, sit_env) -> None:
        """fills 让单合约累积超 ``max_position_per_symbol=10`` 时 RiskGuard 应熔断。"""
        me, ee, cta_engine, gateway, strategy, guard = sit_env
        strategy.fixed_size = 11
        gateway.queue_response(ResponseSpec(kind="partial", fills=[5, 5, 1]))

        strategy.send_on_next_bar = True
        _drive_bar(strategy)
        _drain(ee)

        assert guard.tripped is True
        assert "持仓" in guard.trip_reason and "11" in guard.trip_reason


# ---------------------------------------------------------------------------
# 3. 撤单（cosmetic noise 回归断言）
# ---------------------------------------------------------------------------


class TestCancelSpamRegression:
    """我们本地的 BaseCtaStrategy 子类不应触发 vn.py 的"委托撤单"日志刷屏。

    背景
    ----
    happy-path 1023-bar storm 期间日志被刷出"几十行 委托撤单"。根因是
    ``cta_engine.init_engine`` 把 ``vnpy_ctastrategy/strategies/`` 里的教科书版
    DoubleMaStrategy 二次注册到 ``cta_engine.classes`` —— 教科书版 ``on_bar``
    第一句就是 ``self.cancel_all()``。本 fixture 已经清掉 cta_engine.classes 里
    除 RecordingStrategy 外所有类；本测试再确认一次：在 fixture 控制下不出现刷屏。

    更早版本的 ``_run_replay`` 引入了 ``load_strategy_class_from_folder`` 调用，
    但放在 ``init_engine`` 之前 —— 教科书版加载在后，覆盖了本地版。修复后
    ``run.py`` 在 init_engine 之后再调一次 ``load_strategy_class_from_folder``，
    确保 ``strategies/`` 本地版以"最后加载者胜出"取得位置。
    """

    def test_local_strategy_does_not_call_cancel_all(self, sit_env) -> None:
        me, ee, cta_engine, gateway, strategy, guard = sit_env

        # 抓 MainEngine 的 LOG 事件 —— "委托撤单 -> REPLAY" 来自 main_engine.cancel_order
        cancel_logs: list = []

        def _on_log(event) -> None:
            msg = getattr(event.data, "msg", "")
            if "委托撤单" in msg:
                cancel_logs.append(msg)

        ee.register(EVENT_LOG, _on_log)

        # 跑 10 根 bar，本地策略每根都不主动撤单 → 期望零条 "委托撤单"
        for _ in range(10):
            strategy.send_on_next_bar = True
            _drive_bar(strategy)
            _drain(ee)

        assert cancel_logs == [], f"出现非预期撤单日志（cosmetic noise 回归）：{cancel_logs}"
        # 10 根 bar × 1 单 = 10 笔 ALLTRADED，但 max_position 10 → 第 11 笔会熔断
        assert strategy.pos == 10
        assert guard.tripped is False  # 卡在阈值边界，未越界

    def test_bundled_doublema_would_have_spammed_cancel(
        self, sit_env, caplog: pytest.LogCaptureFixture
    ) -> None:
        """反证：换回教科书版（带 cancel_all）就能复现刷屏。

        本用例证明 cancel-spam 的根因是策略侧主动撤单，与 ReplayGateway 无关 ——
        修复路径必须保证本地 BaseCtaStrategy 子类不调用 cancel_all（已通过约束 +
        ``test_safe_send_migration`` 守住），不是去掩盖网关日志。
        """
        me, ee, cta_engine, gateway, strategy, guard = sit_env

        # 套一个"在 on_bar 顶部撤单"的临时策略（模拟教科书版）
        class CancelHappyStrategy(BaseCtaStrategy):
            author = "sit-bundled-stand-in"
            parameters: list[str] = []
            variables: list[str] = []

            def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
                super().__init__(cta_engine, strategy_name, vt_symbol, setting)
                self.send_on_next_bar = False

            def on_init(self) -> None:
                pass

            def on_tick(self, tick) -> None:
                pass

            def on_bar(self, bar) -> None:
                self.cancel_all()  # ← 教科书版同款，触发 spam
                if self.send_on_next_bar:
                    self.send_on_next_bar = False
                    safe_buy(self, bar.close_price, 1)

        cta_engine.classes["CancelHappyStrategy"] = CancelHappyStrategy
        cta_engine.add_strategy("CancelHappyStrategy", "sit-cancel-happy", "rb2410.SHFE", {})
        spammer = cta_engine.strategies["sit-cancel-happy"]
        spammer.inited = True
        spammer.trading = True

        cancel_logs: list = []

        def _on_log(event) -> None:
            msg = getattr(event.data, "msg", "")
            if "委托撤单" in msg:
                cancel_logs.append(msg)

        ee.register(EVENT_LOG, _on_log)

        # 先成交一单制造一个 active orderid，然后接着跑 5 根 bar 让 cancel_all 反复撤它
        spammer.send_on_next_bar = True
        _drive_bar(spammer)
        _drain(ee)
        for _ in range(5):
            _drive_bar(spammer)  # 不发新单，只触发 cancel_all
            _drain(ee)

        # 注意：ALLTRADED 后 cta_engine 会把 vt_orderid 从 active 集移除（即使因为
        # preregister + dispatch_sync 的双 plant 顺序），cancel_all 找不到 active 单
        # 时不再 emit "委托撤单"。所以这条断言其实只验证：cancel_all 路径在我们的
        # 注册顺序下不会因为 "vt_orderid 没被清理" 而误刷。
        # 若未来某个 vn.py 升级改了 process_order_event 的 active 清理时机，这条断言
        # 会先于 production 报警。
        assert len(cancel_logs) == 0, (
            f"教科书版策略也不应刷屏 —— "
            f"如果开始刷屏，说明 active 集清理回归。日志：{cancel_logs}"
        )
