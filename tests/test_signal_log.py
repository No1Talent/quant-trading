"""SignalLog (JSONL) — null/file 实现 + 并发追加 + _gated_send 旁路集成。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils.signal_log import (
    FileSignalLog,
    NullSignalLog,
    get_signal_log,
    set_signal_log,
)
from utils.strategy_base import safe_buy, safe_sell


@pytest.fixture(autouse=True)
def _isolate_signal_log():
    set_signal_log(None)
    yield
    set_signal_log(None)


class TestNullSignalLog:
    def test_append_is_noop(self):
        log = NullSignalLog()
        # 不应抛、不应写、不应改 state — 调用通过即视为通过
        log.append(
            strategy_name="X",
            vt_symbol="rb2410.SHFE",
            side="buy",
            price=3000.0,
            volume=1,
            allowed=True,
            reject_reason=None,
        )

    def test_default_singleton_is_null(self):
        # 进程启动默认是 NullSignalLog；测试 fixture 也确保隔离
        assert isinstance(get_signal_log(), NullSignalLog)


class TestFileSignalLog:
    def test_append_writes_one_jsonl_line(self, tmp_path: Path):
        p = tmp_path / "signals.jsonl"
        log = FileSignalLog(p)
        log.append(
            strategy_name="DoubleMa-rb",
            vt_symbol="rb2410.SHFE",
            side="buy",
            price=3210.5,
            volume=2,
            allowed=True,
            reject_reason=None,
            metadata={"fast_ma": 3211.2, "slow_ma": 3208.7},
        )
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["strategy"] == "DoubleMa-rb"
        assert record["vt_symbol"] == "rb2410.SHFE"
        assert record["side"] == "buy"
        assert record["price"] == 3210.5
        assert record["volume"] == 2
        assert record["allowed"] is True
        assert record["reject_reason"] is None
        assert record["metadata"] == {"fast_ma": 3211.2, "slow_ma": 3208.7}
        assert "ts" in record  # ISO 时间戳格式由 datetime.isoformat 保证

    def test_appends_are_additive(self, tmp_path: Path):
        p = tmp_path / "signals.jsonl"
        log = FileSignalLog(p)
        for i in range(5):
            log.append(
                strategy_name="X",
                vt_symbol="rb2410.SHFE",
                side="buy",
                price=3000.0 + i,
                volume=1,
                allowed=True,
                reject_reason=None,
            )
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5
        prices = [json.loads(line)["price"] for line in lines]
        assert prices == [3000.0, 3001.0, 3002.0, 3003.0, 3004.0]

    def test_rejected_signal_records_reason(self, tmp_path: Path):
        p = tmp_path / "signals.jsonl"
        log = FileSignalLog(p)
        log.append(
            strategy_name="X",
            vt_symbol="rb2410.SHFE",
            side="buy",
            price=9999.0,
            volume=1,
            allowed=False,
            reject_reason="price_deviation_exceeded",
        )
        record = json.loads(p.read_text(encoding="utf-8").strip())
        assert record["allowed"] is False
        assert record["reject_reason"] == "price_deviation_exceeded"

    def test_creates_parent_dir(self, tmp_path: Path):
        p = tmp_path / "nested" / "subdir" / "signals.jsonl"
        FileSignalLog(p)
        assert p.parent.exists()

    def test_write_failure_does_not_raise(self, tmp_path: Path, caplog):
        # 信号日志写入失败必须不能传播到上层 — 否则一次 IO 错误就会冒到
        # 策略的 send_order 链路里 crash 掉真实交易
        p = tmp_path / "signals.jsonl"
        log = FileSignalLog(p)

        with patch.object(Path, "open", side_effect=OSError("disk full")):
            log.append(
                strategy_name="X",
                vt_symbol="rb2410.SHFE",
                side="buy",
                price=3000.0,
                volume=1,
                allowed=True,
                reject_reason=None,
            )
        assert any("SignalLog 写入失败" in rec.message for rec in caplog.records)

    def test_concurrent_appends_are_atomic(self, tmp_path: Path):
        # 多线程同时 append 时不能交叉断行 — JSONL 的关键不变量是每行可独立解析
        p = tmp_path / "signals.jsonl"
        log = FileSignalLog(p)

        def worker(tid: int) -> None:
            for i in range(50):
                log.append(
                    strategy_name=f"worker-{tid}",
                    vt_symbol="rb2410.SHFE",
                    side="buy",
                    price=float(tid * 1000 + i),
                    volume=1,
                    allowed=True,
                    reject_reason=None,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 8 * 50
        # 每一行都得是合法 JSON — 行间没有交叉
        for line in lines:
            json.loads(line)


class _FakeStrategy:
    def __init__(self, vt_symbol: str = "rb2410.SHFE", strategy_name: str = "DoubleMa-X"):
        self.vt_symbol = vt_symbol
        self.strategy_name = strategy_name
        self.write_log = MagicMock()
        self.buy = MagicMock(return_value=["oid1"])
        self.sell = MagicMock(return_value=["oid2"])


class TestGatedSendIntegration:
    """验证 _gated_send 旁路 SignalLog 的契约：allow / reject 都记录。"""

    def test_allowed_send_writes_signal_log(self, tmp_path: Path):
        p = tmp_path / "signals.jsonl"
        set_signal_log(FileSignalLog(p))

        guard = MagicMock()
        guard.check_order_pre.return_value = (True, "ok")
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=guard):
            safe_buy(strategy, 3210.0, 2)

        record = json.loads(p.read_text(encoding="utf-8").strip())
        assert record["strategy"] == "DoubleMa-X"
        assert record["side"] == "buy"
        assert record["allowed"] is True
        assert record["reject_reason"] is None
        strategy.buy.assert_called_once_with(3210.0, 2)

    def test_rejected_send_writes_signal_log_with_reason(self, tmp_path: Path):
        p = tmp_path / "signals.jsonl"
        set_signal_log(FileSignalLog(p))

        guard = MagicMock()
        guard.check_order_pre.return_value = (False, "tripped")
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=guard):
            safe_sell(strategy, 9000.0, 1)

        record = json.loads(p.read_text(encoding="utf-8").strip())
        assert record["side"] == "sell"
        assert record["allowed"] is False
        assert record["reject_reason"] == "tripped"
        strategy.sell.assert_not_called()  # 被风控挡掉，未透传

    def test_no_guard_path_still_logs(self, tmp_path: Path):
        # 回测场景：guard 为 None，仍要落信号日志（这正是 LIVE/回测对账的入口）
        p = tmp_path / "signals.jsonl"
        set_signal_log(FileSignalLog(p))

        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=None):
            safe_buy(strategy, 3210.0, 1)

        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["allowed"] is True
        strategy.buy.assert_called_once()

    def test_default_null_log_does_not_create_file(self, tmp_path: Path, monkeypatch):
        # 不调 set_signal_log → 仍是 NullSignalLog → 测试 / 回测零文件副作用
        from utils import signal_log

        monkeypatch.setattr(signal_log, "DEFAULT_SIGNAL_LOG_PATH", tmp_path / "signals.jsonl")
        strategy = _FakeStrategy()
        with patch("utils.strategy_base.get_active_risk_guard", return_value=None):
            safe_buy(strategy, 3210.0, 1)
        assert not (tmp_path / "signals.jsonl").exists()
