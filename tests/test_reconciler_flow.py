"""Reconciler 流程测试：FakeClock 单线程驱动，覆盖五个核心场景。

场景：
    - init_quiet_pass：init-settle-quiet 正常通过
    - two_positions：两个持仓全部到齐后正确收敛 + 一致
    - zero_positions：空仓账户不死循环
    - mismatch_fail_fast：持仓不平 → 抛 ReconcileError + 写 flag + 不调 sys.exit
    - init_quiet_timeout：合约事件永不停 → 硬超时
    - position_query_error：query_position 抛异常 → 立即 fail_fast
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests._fakes import (
    FakeClock,
    make_account_event,
    make_contract_event,
    make_position,
    make_position_event,
)
from utils.notifier import NullNotifier
from utils.reconciler import CtpReconciler, ReconcileError, check_reconcile_flag


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def main_engine() -> MagicMock:
    me = MagicMock()
    me.query_position = MagicMock()
    me.query_account = MagicMock()
    me.get_all_positions = MagicMock(return_value=[])
    return me


@pytest.fixture
def event_engine() -> MagicMock:
    return MagicMock()


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "reconcile_breach.flag"


def _build(
    me: MagicMock,
    ee: MagicMock,
    clock: FakeClock,
    flag_path,
    **overrides,
) -> CtpReconciler:
    defaults = dict(
        init_quiet_ms=2000,
        init_safety_margin_s=1.0,
        settle_quiet_ms=800,
        hard_timeout_s=15.0,
        poll_interval_s=0.1,
        breach_flag_path=flag_path,
        clock=clock.now,
        sleeper=clock.sleep,
    )
    defaults.update(overrides)
    return CtpReconciler(main_engine=me, event_engine=ee, notifier=NullNotifier(), **defaults)


# =========================================================================
# 成功路径
# =========================================================================


class TestSuccessPath:
    def test_init_quiet_passes_with_steady_contract_burst_then_silence(
        self, main_engine, event_engine, clock, flag_path
    ):
        r = _build(main_engine, event_engine, clock, flag_path)

        # 合约下发模拟：0.5s / 1.0s / 1.5s 各推一次，之后静默
        for t in [0.5, 1.0, 1.5]:
            clock.schedule(t, lambda: r._on_contract(make_contract_event()))

        # 持仓查询触发时安排两条 position event
        def on_query_pos():
            clock.schedule_after(
                0.1, lambda: r._on_position(make_position_event("rb2510.SHFE", "多", 3))
            )
            clock.schedule_after(
                0.3, lambda: r._on_position(make_position_event("hc2510.SHFE", "空", 2))
            )

        main_engine.query_position.side_effect = on_query_pos

        def on_query_acct():
            clock.schedule_after(0.1, lambda: r._on_account(make_account_event()))

        main_engine.query_account.side_effect = on_query_acct

        main_engine.get_all_positions.return_value = [
            make_position("rb2510.SHFE", "多", 3),
            make_position("hc2510.SHFE", "空", 2),
        ]

        diff = r.reconcile_against(
            {
                "rb2510.SHFE": ("LONG", 3),
                "hc2510.SHFE": ("SHORT", 2),
            }
        )

        assert diff == []
        assert not flag_path.exists()
        main_engine.query_position.assert_called_once()
        main_engine.query_account.assert_called_once()
        # init quiet 在 t=1.5 + 2.0 = 3.5 后通过；+ 1.0 safety + 2 次 settle (≈ 800ms each)
        # 整个对账完成的虚拟时间应在 5s 以上
        assert clock.now() >= 5.0

    def test_zero_positions_does_not_hang(self, main_engine, event_engine, clock, flag_path):
        r = _build(main_engine, event_engine, clock, flag_path)

        # 不安排任何 contract event：init prime 后 2000ms 自然静默
        # 不安排任何 position event：position prime 后 800ms 自然静默
        def on_query_acct():
            clock.schedule_after(0.1, lambda: r._on_account(make_account_event()))

        main_engine.query_account.side_effect = on_query_acct
        main_engine.get_all_positions.return_value = []

        diff = r.reconcile_against({})

        assert diff == []
        assert not flag_path.exists()
        # 至少跨过：init quiet(2s) + safety(1s) + pos settle(0.8s) + acct settle(0.8s) ≈ 4.6s
        assert clock.now() >= 4.0

    def test_listeners_unregistered_on_success(self, main_engine, event_engine, clock, flag_path):
        r = _build(main_engine, event_engine, clock, flag_path)
        # 让流程尽快结束
        main_engine.query_account.side_effect = lambda: clock.schedule_after(
            0.05, lambda: r._on_account(make_account_event())
        )
        r.reconcile_against({})
        # register / unregister 各 3 次（CONTRACT / POSITION / ACCOUNT）
        assert event_engine.register.call_count == 3
        assert event_engine.unregister.call_count == 3


# =========================================================================
# 失败路径
# =========================================================================


class TestFailFastPath:
    def test_mismatch_volume_raises_and_writes_flag(
        self, main_engine, event_engine, clock, flag_path
    ):
        r = _build(main_engine, event_engine, clock, flag_path)

        main_engine.query_position.side_effect = lambda: clock.schedule_after(
            0.1, lambda: r._on_position(make_position_event("rb2510.SHFE", "多", 5))
        )
        main_engine.query_account.side_effect = lambda: clock.schedule_after(
            0.1, lambda: r._on_account(make_account_event())
        )
        main_engine.get_all_positions.return_value = [
            make_position("rb2510.SHFE", "多", 5),
        ]

        with pytest.raises(ReconcileError, match="position_mismatch"):
            r.reconcile_against({"rb2510.SHFE": ("LONG", 3)})  # 本地说 3 手，CTP 是 5

        assert flag_path.exists()
        loaded = check_reconcile_flag(flag_path)
        assert loaded is not None
        assert loaded["code"] == "position_mismatch"
        assert "rb2510.SHFE" in loaded["reason"]

    def test_local_extra_position_raises(self, main_engine, event_engine, clock, flag_path):
        """本地说有 rb，CTP 实际为空 — 重启后最危险的情况之一。"""
        r = _build(main_engine, event_engine, clock, flag_path)
        main_engine.query_account.side_effect = lambda: clock.schedule_after(
            0.1, lambda: r._on_account(make_account_event())
        )
        main_engine.get_all_positions.return_value = []

        with pytest.raises(ReconcileError, match="position_mismatch"):
            r.reconcile_against({"rb2510.SHFE": ("LONG", 3)})

        assert flag_path.exists()

    def test_init_quiet_timeout_when_contracts_never_stop(
        self, main_engine, event_engine, clock, flag_path
    ):
        """合约事件永不静默 → init-quiet 永远不通过 → 硬超时。"""
        r = _build(main_engine, event_engine, clock, flag_path, hard_timeout_s=5.0)

        # 每 300ms 一条 contract，远小于 init_quiet_ms (2000ms)
        for t in [0.3 * i for i in range(1, 30)]:
            clock.schedule(t, lambda: r._on_contract(make_contract_event()))

        with pytest.raises(ReconcileError, match="init_quiet_timeout"):
            r.reconcile_against({})

        assert flag_path.exists()
        loaded = check_reconcile_flag(flag_path)
        assert loaded is not None
        assert loaded["code"] == "init_quiet_timeout"
        # position 查询从未发起
        main_engine.query_position.assert_not_called()

    def test_position_query_raises_short_circuits(
        self, main_engine, event_engine, clock, flag_path
    ):
        r = _build(main_engine, event_engine, clock, flag_path)
        main_engine.query_position.side_effect = RuntimeError("CTP 网关挂了")

        with pytest.raises(ReconcileError, match="query_position_error"):
            r.reconcile_against({})

        assert flag_path.exists()
        loaded = check_reconcile_flag(flag_path)
        assert loaded is not None
        assert loaded["code"] == "query_position_error"
        assert "CTP 网关挂了" in loaded["reason"]
        # account 查询从未发起
        main_engine.query_account.assert_not_called()

    def test_account_query_raises_short_circuits(self, main_engine, event_engine, clock, flag_path):
        r = _build(main_engine, event_engine, clock, flag_path)
        main_engine.query_account.side_effect = RuntimeError("session lost")

        with pytest.raises(ReconcileError, match="query_account_error"):
            r.reconcile_against({})

        assert flag_path.exists()

    def test_listeners_unregistered_on_failure(self, main_engine, event_engine, clock, flag_path):
        r = _build(main_engine, event_engine, clock, flag_path)
        main_engine.query_position.side_effect = RuntimeError("boom")

        with pytest.raises(ReconcileError):
            r.reconcile_against({})

        # 即使失败，事件监听必须反注册，避免泄漏
        assert event_engine.unregister.call_count == 3


# =========================================================================
# 副作用 / 落盘
# =========================================================================


class TestBreachFlag:
    def test_flag_payload_shape(self, main_engine, event_engine, clock, flag_path):
        r = _build(main_engine, event_engine, clock, flag_path)
        main_engine.query_position.side_effect = RuntimeError("x")

        with pytest.raises(ReconcileError):
            r.reconcile_against({})

        data = json.loads(flag_path.read_text(encoding="utf-8"))
        assert set(data.keys()) >= {"tripped_at", "code", "reason"}

    def test_check_returns_none_when_no_flag(self, tmp_path):
        assert check_reconcile_flag(tmp_path / "missing.flag") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
