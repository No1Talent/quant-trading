"""启动期对账接线测试：sync_data_loader → reconciler 端到端。

回答的问题：如果用户在 .vntrader/cta_strategy_data.json 写了一个仓位，
启动期对账是否真的会和 CTP 端比对、检测出不一致、并 fail-fast?

这是 P0-3 的端到端验证，比 test_reconciler_flow.py 更高一层：
那个测的是 reconciler 内部时序；这个测的是 run.py 的真实数据路径。
所以它跑 FakeClock + 真实文件 I/O + 真实 loader 调用。

场景：
    - happy_path: 本地有 1 手 AG long，CTP 也有 1 手 AG long → 一致，无 flag
    - mismatch_local_extra: 本地有 1 手 AG，CTP 空仓 → 不一致，flag 写入
    - mismatch_ctp_extra: 本地空仓，CTP 有 1 手 → 不一致，flag 写入
    - empty_files: 两边都空仓（首次启动） → 一致，无 flag
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._fakes import FakeClock, make_position, make_position_event
from utils.notifier import NullNotifier
from utils.reconciler import CtpReconciler, ReconcileError, check_reconcile_flag
from utils.sync_data_loader import load_local_positions_for_reconcile


def _write_strategy_files(
    vntrader_dir: Path,
    strategies: dict[str, tuple[str, int]],
) -> None:
    """工具：写 vn.py 风格的 setting + data JSON，单策略一个 vt_symbol。"""
    setting = {}
    data = {}
    for name, (vt_symbol, pos) in strategies.items():
        setting[name] = {
            "class_name": "DoubleMaStrategy",
            "vt_symbol": vt_symbol,
            "setting": {"fixed_size": abs(pos) if pos != 0 else 1},
        }
        data[name] = {"pos": pos}
    (vntrader_dir / "cta_strategy_setting.json").write_text(
        json.dumps(setting, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (vntrader_dir / "cta_strategy_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_reconciler(
    clock: FakeClock,
    main_engine: MagicMock,
    flag_path: Path,
) -> CtpReconciler:
    """快速窗口配置：让 FakeClock 可以在 ~200ms 虚拟时间内走完全流程。"""
    return CtpReconciler(
        main_engine=main_engine,
        event_engine=MagicMock(),
        notifier=NullNotifier(),
        init_quiet_ms=50,
        init_safety_margin_s=0.05,
        settle_quiet_ms=50,
        hard_timeout_s=3.0,
        poll_interval_s=0.01,
        breach_flag_path=flag_path,
        clock=clock.now,
        sleeper=clock.sleep,
    )


def _arm_position_arrival(r: CtpReconciler, clock: FakeClock) -> None:
    """run.py 的真实场景：CTP query_position 后某个时刻收到 EVENT_POSITION。
    用 FakeClock 把 query_position 调用 30ms 后推一条 position 事件。
    """

    def fire():
        r._on_position(make_position_event("ag2506.SHFE", "多", 1))

    def side_effect(*_args, **_kwargs):
        clock.schedule_after(0.03, fire)

    r.main_engine.query_position.side_effect = side_effect

    def fire_acct():
        # account 不需要内容关心；只需要触发回调推进时间戳
        r._on_account(MagicMock())

    def side_effect_acct(*_args, **_kwargs):
        clock.schedule_after(0.03, fire_acct)

    r.main_engine.query_account.side_effect = side_effect_acct


class TestHappyPath:
    """两边都对得上 — 通过且不写 flag。"""

    def test_match_returns_empty_diff_no_flag(self, tmp_path: Path):
        # 1. 本地 sync_data：1 手 AG long
        _write_strategy_files(tmp_path, {"ag_dm": ("ag2506.SHFE", 1)})

        # 2. loader 读出本地仓位
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {"ag2506.SHFE": ("LONG", 1)}

        # 3. mock CTP 也报 1 手 AG long
        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = [make_position("ag2506.SHFE", "多", 1)]
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        # 4. 对账：不应抛
        diff = r.reconcile_against(local)
        assert diff == []

        # 5. 不该写 flag
        assert not flag_path.exists()
        assert check_reconcile_flag(flag_path) is None


class TestMismatchDetected:
    """不一致 → fail-fast + flag 写入。"""

    def test_local_has_extra_position(self, tmp_path: Path):
        # 本地说有 1 手 AG，CTP 说空仓 — 也就是上次重启前的仓位
        # 在系统离线期间被柜台或人为平掉了。最经典的"仓位幻觉"场景。
        _write_strategy_files(tmp_path, {"ag_dm": ("ag2506.SHFE", 1)})
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {"ag2506.SHFE": ("LONG", 1)}

        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = []  # CTP 空仓
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        with pytest.raises(ReconcileError, match="position_mismatch"):
            r.reconcile_against(local)

        assert flag_path.exists()
        breach = check_reconcile_flag(flag_path)
        assert breach is not None
        assert breach["code"] == "position_mismatch"
        # diff 行应当在 reason 中可被人工识别
        assert "ag2506.SHFE" in breach["reason"]

    def test_ctp_has_extra_position(self, tmp_path: Path):
        # 本地空仓（无策略数据 / 全平仓），CTP 却报有持仓 — 比如手工下单 / 老仓位
        # 没清理。必须 halt 以防策略不知情下双开。
        # 文件不存在 = 等同首次启动
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {}

        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = [make_position("rb2510.SHFE", "空", 2)]
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        with pytest.raises(ReconcileError, match="position_mismatch"):
            r.reconcile_against(local)

        assert flag_path.exists()
        breach = check_reconcile_flag(flag_path)
        assert breach is not None
        assert "rb2510.SHFE" in breach["reason"]


class TestFirstBootClean:
    """首次启动 — 没 sync 文件 + CTP 空仓 → 干净通过。"""

    def test_no_files_empty_ctp_clean_pass(self, tmp_path: Path):
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {}

        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = []
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        diff = r.reconcile_against(local)
        assert diff == []
        assert not flag_path.exists()


class TestMultiStrategySameSymbol:
    """多策略同标的场景：两个策略各持 1 手 AG long，CTP 应当看到 2 手。"""

    def test_two_long_strategies_one_ctp_position_of_size_2(self, tmp_path: Path):
        _write_strategy_files(
            tmp_path,
            {
                "ag_dm_fast": ("ag2506.SHFE", 1),
                "ag_dm_slow": ("ag2506.SHFE", 1),
            },
        )
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {"ag2506.SHFE": ("LONG", 2)}

        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = [make_position("ag2506.SHFE", "多", 2)]
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        diff = r.reconcile_against(local)
        assert diff == []

    def test_long_short_cancel_local_zero_ctp_must_also_be_zero(self, tmp_path: Path):
        # 一个策略 long 1 一个 short 1 → 本地净 0（不出现在 local map）
        # 如果 CTP 也是 0 应通过；CTP 显示 long 1 → 不一致
        _write_strategy_files(
            tmp_path,
            {
                "ag_long": ("ag2506.SHFE", 1),
                "ag_short": ("ag2506.SHFE", -1),
            },
        )
        local = load_local_positions_for_reconcile(tmp_path)
        assert local == {}

        clock = FakeClock()
        me = MagicMock()
        me.get_all_positions.return_value = [make_position("ag2506.SHFE", "多", 1)]
        flag_path = tmp_path / "reconcile_breach.flag"
        r = _make_reconciler(clock, me, flag_path)
        _arm_position_arrival(r, clock)

        with pytest.raises(ReconcileError):
            r.reconcile_against(local)
        assert flag_path.exists()
