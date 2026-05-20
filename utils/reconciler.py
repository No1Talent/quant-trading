"""启动期 CTP 持仓 / 资金对账：拒绝信任本地 sync_data，强制与柜台真实状态对齐。

工作流程：
    1. Init-Settle-Quiet：等 vn.py CtpGateway 内部启动流水线（合约下发→查资金→查持仓）静默
       后再发起外部查询，否则会撞 CTP 全局 1 QPS 流控（-2 / -3 错误）。
    2. query_position + Settle-Quiet：流式回调没有 EOF 帧，用"连续 N ms 无新事件"反推完成。
    3. query_account：同样用 Settle-Quiet 收敛，保持单一阻塞通道（便于测试时钟注入）。
    4. Diff：把 CTP 真实净持仓与本地 sync_data 对比，任何不一致都是致命错误。
    5. Fail-Fast：不平 / 超时 → 抛异常 + 落盘 flag + CRITICAL + sys.exit(1)，**绝不**重试或自愈。

设计底线：
    - 单向依赖：本模块只依赖 vn.py 的 MainEngine / EventEngine / notifier，不 import 任何
      Strategy / RiskGuard / 执行模块。
    - 锁粒度：threading.Lock 只包裹变量赋值，绝不持锁 sleep / 调用 CTP，否则会反向阻塞
      vn.py 底层 C++ 回调线程。
    - 时间注入：clock 和 sleeper 作为构造参数注入，测试用 FakeClock 单线程驱动。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy.event import Event, EventEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_POSITION

from .notifier import INotifier, get_notifier

logger = logging.getLogger("reconciler")


DEFAULT_BREACH_FLAG = Path(__file__).parent.parent / "logs" / "reconcile_breach.flag"


class ReconcileError(Exception):
    """对账失败的统一异常类型。捕获方应当 fail-fast 退出。"""


def is_settled(last_event_ts: float | None, now: float, quiet_ms: int) -> bool:
    """纯函数：自上次事件后是否已过 quiet_ms 静默窗口。None 视为永远未静默。"""
    if last_event_ts is None:
        return False
    return (now - last_event_ts) * 1000.0 >= quiet_ms


def diff_positions(
    local: dict[str, tuple[str, int]],
    ctp: dict[str, tuple[str, int]],
) -> list[dict[str, Any]]:
    """对比本地 sync_data 与 CTP 真实持仓。返回不一致条目列表，空列表 = 一致。

    输入格式：{vt_symbol: (direction, volume)}，direction 取规范化大写 LONG / SHORT。
    """
    rows: list[dict[str, Any]] = []
    keys = set(local) | set(ctp)
    for k in sorted(keys):
        lv = local.get(k)
        cv = ctp.get(k)
        if lv != cv:
            rows.append({"vt_symbol": k, "local": lv, "ctp": cv})
    return rows


def _normalize_direction(direction: Any) -> str:
    """vn.py Direction 枚举 / 中文字符串 / 英文字符串 → 'LONG' | 'SHORT' | 'NET'。"""
    raw = direction.value if hasattr(direction, "value") else str(direction)
    if "多" in raw or raw.lower() in ("long", "direction.long"):
        return "LONG"
    if "空" in raw or raw.lower() in ("short", "direction.short"):
        return "SHORT"
    return "NET"


class CtpReconciler:
    """启动期对账器。reconcile_against() 阻塞至对账完成或失败。"""

    def __init__(
        self,
        main_engine: Any,
        event_engine: EventEngine,
        notifier: INotifier | None = None,
        *,
        gateway_name: str = "CTP",
        init_quiet_ms: int = 2000,
        init_safety_margin_s: float = 1.0,
        settle_quiet_ms: int = 800,
        hard_timeout_s: float = 15.0,
        poll_interval_s: float = 0.1,
        breach_flag_path: Path | str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.notifier = notifier or get_notifier()
        self.gateway_name = gateway_name

        self.init_quiet_ms = init_quiet_ms
        self.init_safety_margin_s = init_safety_margin_s
        self.settle_quiet_ms = settle_quiet_ms
        self.hard_timeout_s = hard_timeout_s
        self.poll_interval_s = poll_interval_s

        self.breach_flag_path = Path(breach_flag_path) if breach_flag_path else DEFAULT_BREACH_FLAG

        self._now = clock
        self._sleep = sleeper

        # 锁只保护时间戳赋值，绝不持锁 sleep / 调 CTP
        self._lock = threading.Lock()
        self._last_contract_ts: float | None = None
        self._last_position_ts: float | None = None
        self._last_account_ts: float | None = None

    # =========================================================================
    # 公开接口
    # =========================================================================

    def reconcile_against(
        self, local_positions: dict[str, tuple[str, int]]
    ) -> list[dict[str, Any]]:
        """阻塞执行完整对账流程。返回 diff 列表（空 = 一致），异常则已抛 ReconcileError。

        本地持仓格式：{vt_symbol: (direction, volume)}，direction 用 'LONG' / 'SHORT'。
        触发 fail-fast 的场景：硬超时、CTP 查询接口异常、diff 非空。
        """
        self._register_listeners()
        try:
            self._wait_init_quiet()
            self._sleep(self.init_safety_margin_s)
            self._query_and_settle_position()
            self._query_and_settle_account()

            ctp_positions = self._snapshot_ctp_positions()
            rows = diff_positions(local_positions, ctp_positions)
            if rows:
                self._fail_fast(
                    "position_mismatch",
                    f"持仓对账不一致 {len(rows)} 条：{rows}",
                )
            logger.info("对账通过：local 与 CTP 持仓一致（%d 合约）", len(ctp_positions))
            return rows
        finally:
            self._unregister_listeners()

    # =========================================================================
    # 事件回调（在 EventEngine 线程执行；只更新时间戳）
    # =========================================================================

    def _on_contract(self, event: Event) -> None:
        del event
        ts = self._now()
        with self._lock:
            self._last_contract_ts = ts

    def _on_position(self, event: Event) -> None:
        del event
        ts = self._now()
        with self._lock:
            self._last_position_ts = ts

    def _on_account(self, event: Event) -> None:
        del event
        ts = self._now()
        with self._lock:
            self._last_account_ts = ts

    # =========================================================================
    # Settle-Quiet 主流程
    # =========================================================================

    def _wait_init_quiet(self) -> None:
        """等 vn.py CtpGateway 内部启动流水线（合约下发）静默。"""
        # Prime：从 reconciler 启动那一刻开始计时
        with self._lock:
            if self._last_contract_ts is None:
                self._last_contract_ts = self._now()

        deadline = self._now() + self.hard_timeout_s
        while self._now() < deadline:
            self._sleep(self.poll_interval_s)
            with self._lock:
                last_ts = self._last_contract_ts
            if is_settled(last_ts, self._now(), self.init_quiet_ms):
                logger.info(
                    "Init-Settle-Quiet 通过（%dms 无 EVENT_CONTRACT）",
                    self.init_quiet_ms,
                )
                return
        self._fail_fast(
            "init_quiet_timeout",
            f"等待 CtpGateway 启动流水线静默超过 {self.hard_timeout_s}s",
        )

    def _query_and_settle_position(self) -> None:
        # Prime 当前时间戳：使空仓账户也能正常退出
        with self._lock:
            self._last_position_ts = self._now()
        try:
            # query_position 是 gateway 方法，不是 MainEngine 方法 — 必须走 get_gateway
            gateway = self.main_engine.get_gateway(self.gateway_name)
            if gateway is None:
                raise RuntimeError(f"未找到 gateway '{self.gateway_name}'")
            gateway.query_position()
        except Exception as e:
            self._fail_fast("query_position_error", f"CTP query_position 异常：{e}")

        deadline = self._now() + self.hard_timeout_s
        while self._now() < deadline:
            self._sleep(self.poll_interval_s)
            with self._lock:
                last_ts = self._last_position_ts
            if is_settled(last_ts, self._now(), self.settle_quiet_ms):
                return
        self._fail_fast(
            "position_settle_timeout",
            f"query_position 在 {self.hard_timeout_s}s 内未静默",
        )

    def _query_and_settle_account(self) -> None:
        with self._lock:
            self._last_account_ts = self._now()
        try:
            gateway = self.main_engine.get_gateway(self.gateway_name)
            if gateway is None:
                raise RuntimeError(f"未找到 gateway '{self.gateway_name}'")
            gateway.query_account()
        except Exception as e:
            self._fail_fast("query_account_error", f"CTP query_account 异常：{e}")

        deadline = self._now() + self.hard_timeout_s
        while self._now() < deadline:
            self._sleep(self.poll_interval_s)
            with self._lock:
                last_ts = self._last_account_ts
            if is_settled(last_ts, self._now(), self.settle_quiet_ms):
                return
        self._fail_fast(
            "account_settle_timeout",
            f"query_account 在 {self.hard_timeout_s}s 内未静默",
        )

    # =========================================================================
    # CTP 状态读取
    # =========================================================================

    def _snapshot_ctp_positions(self) -> dict[str, tuple[str, int]]:
        """从 OmsEngine 缓存读取持仓。vn.py 在 EVENT_POSITION 派发后已写入该缓存。"""
        try:
            positions = self.main_engine.get_all_positions() or []
        except Exception as e:
            self._fail_fast("get_all_positions_error", f"读取 OmsEngine 持仓异常：{e}")

        snapshot: dict[str, tuple[str, int]] = {}
        for p in positions:
            direction = _normalize_direction(p.direction)
            volume = int(p.volume)
            if volume == 0:
                continue
            snapshot[p.vt_symbol] = (direction, volume)
        return snapshot

    # =========================================================================
    # 事件注册
    # =========================================================================

    def _register_listeners(self) -> None:
        self.event_engine.register(EVENT_CONTRACT, self._on_contract)
        self.event_engine.register(EVENT_POSITION, self._on_position)
        self.event_engine.register(EVENT_ACCOUNT, self._on_account)

    def _unregister_listeners(self) -> None:
        try:
            self.event_engine.unregister(EVENT_CONTRACT, self._on_contract)
            self.event_engine.unregister(EVENT_POSITION, self._on_position)
            self.event_engine.unregister(EVENT_ACCOUNT, self._on_account)
        except Exception as e:  # 反注册失败不应掩盖正流程异常
            logger.warning("事件反注册异常（忽略）：%s", e)

    # =========================================================================
    # Fail-Fast
    # =========================================================================

    def _fail_fast(self, code: str, reason: str) -> None:
        """落盘 flag + CRITICAL + 抛 ReconcileError。run.py 应捕获后 sys.exit(1)。"""
        logger.critical("⛔ 对账失败 [%s]：%s", code, reason)

        flag_err: str | None = None
        try:
            self.breach_flag_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "tripped_at": datetime.now().isoformat(timespec="seconds"),
                "code": code,
                "reason": reason,
            }
            self.breach_flag_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            flag_err = str(e)

        msg = (
            f"启动期对账失败\n"
            f"错误码：{code}\n"
            f"原因：{reason}\n"
            f"动作：拒绝挂载策略，进程即将退出"
        )
        if flag_err:
            msg += f"\n⚠️ 标志文件写入失败：{flag_err}"
        msg += "\n\n请人工核对 CTP 真实持仓与本地 sync_data，处理后删除 logs/reconcile_breach.flag"

        try:
            self.notifier.send_critical(msg)
        except Exception as e:
            logger.error("对账告警推送失败：%s", e)

        raise ReconcileError(f"[{code}] {reason}")


# 模块级列表持有引用，防止 GC
_reconcilers: list[CtpReconciler] = []


def run_reconcile(
    main_engine: Any,
    event_engine: EventEngine,
    local_positions: dict[str, tuple[str, int]],
    notifier: INotifier | None = None,
    *,
    exit_on_failure: bool = True,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """run.py 入口：跑一次对账。失败默认 sys.exit(1)；测试时可设 exit_on_failure=False。"""
    r = CtpReconciler(main_engine, event_engine, notifier, **kwargs)
    _reconcilers.append(r)
    try:
        return r.reconcile_against(local_positions)
    except ReconcileError:
        if exit_on_failure:
            sys.exit(1)
        raise


def check_reconcile_flag(flag_path: Path | str | None = None) -> dict | None:
    """启动前检查上次对账是否失败；未失败返回 None。run.py 应阻断启动。"""
    p = Path(flag_path) if flag_path else DEFAULT_BREACH_FLAG
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("读取对账标志失败：%s", e)
        return {"error": str(e), "raw_path": str(p)}
