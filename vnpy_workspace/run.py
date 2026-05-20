"""vn.py 交易系统启动入口。挂载通知监听器和风控前置，自动连接 CTP。"""

import logging
import logging.handlers
import os
import shutil
import sys
from pathlib import Path

WORKSPACE_DIR = Path(__file__).parent.absolute()
PARENT_DIR = WORKSPACE_DIR.parent
sys.path.insert(0, str(PARENT_DIR))

# QUANT_MODE 控制订单路径与上游行情，并决定 cwd / .vntrader 隔离：
#   LIVE         — 默认。cwd=vnpy_workspace/，用 .vntrader/，下真单
#   SIGNAL_ONLY  — 拦截 send_order 合成成交；cwd 切到 .signal_only_runtime/，
#                  让 cta_strategy_data.json 物理独立 —— 否则假成交累积出的 self.pos
#                  会被 vn.py save_strategy_data 写盘，下次 LIVE 启动直接拿到虚假持仓。
#   REPLAY       — 端到端 SIT：CTP 整层换成 ReplayGateway，DB bar → 合成 tick →
#                  策略 → 同步派发 SIGNAL_ONLY 式合成成交。无 GUI、无 CTP。
#                  cwd 切到 .replay_runtime/ 同样隔离 sync_data。
# 必须在任何 vnpy.* import 之前完成 cwd 切换：vnpy.trader.utility 在导入时即把
# TEMP_DIR 钉死成 cwd/.vntrader（若存在）或 ~/.vntrader。
QUANT_MODE = os.environ.get("QUANT_MODE", "LIVE").upper()
if QUANT_MODE not in ("LIVE", "SIGNAL_ONLY", "REPLAY"):
    print(f"[run.py] 未知 QUANT_MODE={QUANT_MODE!r}，回退到 LIVE", file=sys.stderr)
    QUANT_MODE = "LIVE"

_LIVE_VNTRADER = WORKSPACE_DIR / ".vntrader"
_LIVE_VNTRADER.mkdir(exist_ok=True)

# 统一 DB 路径：研究脚本（cwd=repo root）默认走 ~/.vntrader/database.db；
# 这里把 LIVE/SIGNAL_ONLY 模式（cwd=workspace 或 sandbox）也固定到同一份物理 DB，
# 否则 vnpy_sqlite 会在各自 cwd 下找 ./database.db —— LIVE 拿到 workspace/.vntrader/
# 那份空 DB（36KB header-only），策略 load_bar(N) 返回 0 条直接 init 失败。
# 注：.vntrader/ 整目录 gitignore，所以靠 run.py 启动时幂等注入而不是 commit 配置。
# get_file_path 对绝对路径透传，所以这条配置同时被 LIVE/SIGNAL_ONLY 两边使用。
import json as _json

_vt_setting_path = _LIVE_VNTRADER / "vt_setting.json"
_vt_setting: dict = {}
if _vt_setting_path.exists():
    try:
        _vt_setting = _json.loads(_vt_setting_path.read_text(encoding="utf-8") or "{}")
    except _json.JSONDecodeError:
        _vt_setting = {}
if not _vt_setting.get("database.database"):
    _vt_setting["database.database"] = str(Path.home() / ".vntrader" / "database.db")
    _vt_setting_path.write_text(
        _json.dumps(_vt_setting, indent=4, ensure_ascii=False), encoding="utf-8"
    )


def _mirror_live_configs(target_vntrader: Path) -> None:
    """把 LIVE .vntrader/ 的配置文件镜像到沙箱目录，但保留沙箱自己的 cta_strategy_data.json。

    LIVE 端的 strategy 设置 / 风控规则 / vt_setting 变更需要无缝传给沙箱；但
    cta_strategy_data.json 是 self.pos 持久层，沙箱必须保留自己的副本，否则
    模拟成交累积出的虚假持仓会污染 LIVE 启动。
    """
    if not _LIVE_VNTRADER.exists():
        return
    for _f in _LIVE_VNTRADER.iterdir():
        if _f.is_file() and _f.name != "cta_strategy_data.json":
            shutil.copy2(_f, target_vntrader / _f.name)


if QUANT_MODE == "SIGNAL_ONLY":
    SIGNAL_RUNTIME_DIR = WORKSPACE_DIR / ".signal_only_runtime"
    _SIGNAL_VNTRADER = SIGNAL_RUNTIME_DIR / ".vntrader"
    SIGNAL_RUNTIME_DIR.mkdir(exist_ok=True)
    _SIGNAL_VNTRADER.mkdir(exist_ok=True)
    _mirror_live_configs(_SIGNAL_VNTRADER)
    os.chdir(SIGNAL_RUNTIME_DIR)
elif QUANT_MODE == "REPLAY":
    REPLAY_RUNTIME_DIR = WORKSPACE_DIR / ".replay_runtime"
    _REPLAY_VNTRADER = REPLAY_RUNTIME_DIR / ".vntrader"
    REPLAY_RUNTIME_DIR.mkdir(exist_ok=True)
    _REPLAY_VNTRADER.mkdir(exist_ok=True)
    _mirror_live_configs(_REPLAY_VNTRADER)
    os.chdir(REPLAY_RUNTIME_DIR)
else:
    os.chdir(WORKSPACE_DIR)

LOG_DIR = PARENT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_trader_file_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "trader.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8",
    delay=True,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        _trader_file_handler,
    ],
)
logger = logging.getLogger("trader")

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp
from vnpy.trader.utility import get_file_path, load_json
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctp import CtpGateway
from vnpy_datamanager import DataManagerApp
from vnpy_datarecorder import DataRecorderApp
from vnpy_portfoliostrategy import PortfolioStrategyApp
from vnpy_riskmanager import RiskManagerApp
from vnpy_spreadtrading import SpreadTradingApp

from utils import (
    FileSignalLog,
    NotifyLevel,
    ReplayGateway,
    attach_notify_listener,
    attach_risk_guard,
    check_breach_flag,
    check_reconcile_flag,
    get_notifier,
    load_local_positions_for_reconcile,
    make_signal_only_class,
    run_reconcile,
    set_signal_log,
)


def main():
    logger.info("=" * 60)
    logger.info("vn.py 交易系统启动")
    logger.info("=" * 60)

    breach = check_breach_flag()
    if breach:
        logger.critical("⛔ 检测到上次运行触发风控熔断: %s", breach)
        logger.critical("请确认账户状态后手动删除 logs/risk_breach.flag 再启动")
        # 不强制退出 - 由人工决定。

    # 对账 breach 与风控 breach 区分对待：对账失败意味着仓位幻觉风险，
    # 不允许带病启动 — 直接退出，必须人工核对账户并删除 flag 后才能再启。
    reconcile_breach = check_reconcile_flag()
    if reconcile_breach:
        logger.critical("⛔ 检测到上次启动期对账失败: %s", reconcile_breach)
        logger.critical(
            "请核对 CTP 真实持仓与本地 sync_data，处理后删除 logs/reconcile_breach.flag"
        )
        sys.exit(1)

    notifier = get_notifier()
    startup_msg = f"vn.py交易系统启动中...（模式: {QUANT_MODE}）"
    notifier.send(startup_msg, title="系统启动", level=NotifyLevel.INFO, force=True)

    # 结构化信号日志（JSONL）— P2 旁路：策略的每次 safe_buy/sell/short/cover
    # 都落一行到 logs/signals.jsonl，无论 RiskGuard allow/reject。
    # LIVE 与 SIGNAL_ONLY 共享同一份 ground truth，跨模式 diff 即可验证拦截
    # 路径没有偏离策略意图。
    set_signal_log(FileSignalLog(PARENT_DIR / "logs" / "signals.jsonl"))
    if QUANT_MODE == "SIGNAL_ONLY":
        logger.warning("=" * 60)
        logger.warning("⚠️  SIGNAL_ONLY 模式：策略信号会触发合成成交事件，但不下真单")
        logger.warning("    Runtime sandbox: %s", SIGNAL_RUNTIME_DIR)
        logger.warning("    LIVE 的 .vntrader/cta_strategy_data.json 本次不会被修改")
        logger.warning("    若需切回实盘请清除环境变量 QUANT_MODE 或设为 LIVE 后重启")
        logger.warning("=" * 60)

    qapp = create_qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    if QUANT_MODE == "SIGNAL_ONLY":
        gateway_cls = make_signal_only_class(CtpGateway)
    else:
        gateway_cls = CtpGateway
    main_engine.add_gateway(gateway_cls)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(SpreadTradingApp)
    main_engine.add_app(PortfolioStrategyApp)
    main_engine.add_app(DataManagerApp)
    main_engine.add_app(DataRecorderApp)
    main_engine.add_app(RiskManagerApp)

    attach_notify_listener(main_engine, event_engine, notifier)

    attach_risk_guard(
        main_engine,
        event_engine,
        notifier,
        max_daily_loss_pct=0.05,
        max_position_per_symbol=10,
        # P1 新增：标的汇总上限 —— 同一标的（如 RB）多个合约月份累积不得超过 15 手。
        # 比单合约阈值略松（允许少量换月双跨），但堵死"两个月份各跑满 10 手 = 20 手"
        # 这个之前会沉默通过的漏洞。未注册合约自动跳过该维度。
        max_position_per_underlying=15,
        max_trades_per_minute=20,
    )

    # vn.py 的 load_json 从 <cwd>/.vntrader/ 读取，把模板复制到该路径后即可自动连接
    ctp_path = get_file_path("connect_ctp.json")
    if breach:
        logger.warning("风控熔断标志存在，跳过自动连接 — 请人工确认账户状态后通过 GUI 连接")
    elif not ctp_path.exists():
        logger.warning(
            "未找到 %s — 跳过自动连接。"
            "请把 vnpy_workspace/connect_ctp.json.template 填好后复制到该路径",
            ctp_path,
        )
    else:
        ctp_setting = load_json("connect_ctp.json")
        if ctp_setting:
            logger.info("自动连接 CTP: %s", ctp_path)
            main_engine.connect(ctp_setting, "CTP")

            # 启动期对账 — 在 CTP 握手完成后、GUI 出现前阻塞执行。
            # reconciler 内部用 Init-Settle-Quiet 等待 vn.py 的启动流水线完成
            # （合约下发 ~2-3s + 安全余量 1s），无需在此 sleep。
            #
            # 数据来源：vn.py 把所有 CTA 策略的 sync_data 写到
            # <cwd>/.vntrader/cta_strategy_data.json，vt_symbol 在
            # cta_strategy_setting.json。loader 合并两者按 vt_symbol 聚合。
            #
            # 失败语义：diff 不一致 / 超时 → run_reconcile 内部 sys.exit(1)，
            # GUI 不会出现，logs/reconcile_breach.flag 留作下次启动门禁。
            #
            # SIGNAL_ONLY 模式跳过 — 不下真单，CTP 上的真实持仓和本地 sync_data
            # 必然不一致，跑 reconcile 会假性失败。
            if QUANT_MODE == "SIGNAL_ONLY":
                logger.info("SIGNAL_ONLY 模式：跳过启动期 CTP 对账（无实盘持仓需校对）")
            else:
                try:
                    local_positions = load_local_positions_for_reconcile(
                        WORKSPACE_DIR / ".vntrader"
                    )
                    logger.info("启动期对账：本地非零仓位 %d 个", len(local_positions))
                    run_reconcile(main_engine, event_engine, local_positions, notifier)
                    logger.info("✅ 启动期对账通过")
                except ValueError as e:
                    # sync_data 文件损坏 — 不能假装没事。
                    logger.critical("⛔ sync_data 解析失败: %s", e)
                    sys.exit(1)
        else:
            logger.warning("%s 内容为空 — 跳过自动连接", ctp_path)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    notifier.send("✅ 交易系统已就绪", title="系统就绪", level=NotifyLevel.INFO, force=True)

    try:
        qapp.exec()
    finally:
        logger.info("交易系统正在关闭...")
        # 先 close：触发各策略 on_stop → write_log → 通知入队
        main_engine.close()
        # 再 flush：排干所有在途消息（含 on_stop 产生的通知）
        notifier.send("交易系统已关闭", title="系统关闭", level=NotifyLevel.WARNING, force=True)
        notifier.flush(timeout=10)
        logger.info("已干净退出")


def _run_replay() -> None:
    """REPLAY 模式：headless 端到端 SIT。

    需要在函数内 import threading/time（模块顶层未引入），保持 LIVE 路径冷启动开销不变。

    流程
    ----
    1. 装配 ReplayGateway + CtaStrategyApp（不开 GUI）
    2. 接好 NotifyListener / RiskGuard，与 LIVE 同样规则
    3. ``main_engine.connect`` 推 ContractData，让 CtaEngine 能 subscribe
    4. ``init_all_strategies`` → 等待初始化完成 → ``start_all_strategies``
    5. 从 DB 加载 ``REPLAY_VT_SYMBOL`` 的 bar，调 ``gw.start_replay(bars, delay_ms, block=True)``
    6. 干净关闭，flush 通知队列

    环境变量
    --------
    - REPLAY_VT_SYMBOL：必填，形如 ``rb2410.SHFE``
    - REPLAY_BAR_DELAY_MS：每 bar 间隔，默认 100；置 0 触发风暴模式
    - REPLAY_INTERVAL：HOUR / MINUTE / DAILY，默认 HOUR（rb2410 DB 里就是 60min）
    """
    import threading
    import time
    from datetime import datetime as _dt

    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.database import get_database
    from vnpy_ctastrategy import CtaEngine

    logger.info("=" * 60)
    logger.info("REPLAY 模式启动（headless SIT，无 GUI、无 CTP）")
    logger.info("Runtime sandbox: %s", REPLAY_RUNTIME_DIR)
    logger.info("=" * 60)

    vt_symbol = os.environ.get("REPLAY_VT_SYMBOL", "rb2410.SHFE")
    delay_ms = int(os.environ.get("REPLAY_BAR_DELAY_MS", "100"))
    interval_name = os.environ.get("REPLAY_INTERVAL", "HOUR").upper()
    # 默认走 NullNotifier。``notify_signal`` 调 ``send(force=True)`` 会绕过
    # WebhookNotifier 的 rate_limit_per_minute / dedup —— REPLAY 一次跑 50+
    # 信号瞬间灌出去会被企业微信/钉钉/邮件 API 当成滥用，触发 429 / 拉黑。
    # 真的要测 webhook 路径，显式 REPLAY_ENABLE_NOTIFIER=1（自担风险）。
    enable_notifier = os.environ.get("REPLAY_ENABLE_NOTIFIER", "0") == "1"
    try:
        interval = Interval[interval_name]
    except KeyError:
        logger.error("REPLAY_INTERVAL=%s 不是合法 Interval，回退 HOUR", interval_name)
        interval = Interval.HOUR

    symbol, exchange_name = vt_symbol.split(".")
    exchange = Exchange(exchange_name)
    logger.info("REPLAY 目标：%s %s 节拍=%dms", vt_symbol, interval.value, delay_ms)

    if enable_notifier:
        notifier = get_notifier()
        logger.warning("REPLAY_ENABLE_NOTIFIER=1 — 真实推送通道被打开，可能触发 webhook 429。")
    else:
        from utils.notifier import NullNotifier, set_notifier

        notifier = NullNotifier()
        set_notifier(notifier)  # 让 NotifyListener / RiskGuard 通过 get_notifier() 拿到的也是它
        logger.info("REPLAY: 默认 NullNotifier（不外推）。要灌真实通道请 REPLAY_ENABLE_NOTIFIER=1")
    notifier.send(
        f"REPLAY-SIT 启动（{vt_symbol} {interval.value} {delay_ms}ms/bar）",
        title="系统启动",
        level=NotifyLevel.INFO,
        force=True,
    )

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(ReplayGateway)
    main_engine.add_app(CtaStrategyApp)

    attach_notify_listener(main_engine, event_engine, notifier)
    attach_risk_guard(
        main_engine,
        event_engine,
        notifier,
        max_daily_loss_pct=0.05,
        max_position_per_symbol=10,
        # P1 新增：标的汇总上限 —— 同一标的（如 RB）多个合约月份累积不得超过 15 手。
        # 比单合约阈值略松（允许少量换月双跨），但堵死"两个月份各跑满 10 手 = 20 手"
        # 这个之前会沉默通过的漏洞。未注册合约自动跳过该维度。
        max_position_per_underlying=15,
        max_trades_per_minute=20,
    )

    # 推合约 — 必须先 connect 让 ContractData 进 main_engine.contracts，
    # 否则 CtaEngine.init_strategy 的 subscribe_data 拿不到合约会跳过订阅。
    main_engine.connect({"symbols": [(symbol, exchange)]}, ReplayGateway.default_name)

    cta_engine: CtaEngine = main_engine.get_engine("CtaStrategy")

    # 预扫 repo 的 strategies/。vnpy CtaEngine.load_strategy_class 只扫
    # cwd/strategies/，REPLAY 的 cwd 是 .replay_runtime/ → 默认扫不到 DoubleMa 等
    # 自研策略。这里显式把 PARENT_DIR/strategies 注入 importer，类名才能被
    # add_strategy 找到。
    cta_engine.load_strategy_class_from_folder(PARENT_DIR / "strategies", "strategies")

    # 缺策略配置则 bootstrap：DoubleMa-rb2410，短窗口让回放期内多触几次金叉
    setting_path = Path.cwd() / ".vntrader" / "cta_strategy_setting.json"
    if not setting_path.exists() or setting_path.stat().st_size == 0:
        bootstrap = {
            "DoubleMa-rb2410-REPLAY": {
                "class_name": "DoubleMaStrategy",
                "vt_symbol": vt_symbol,
                "setting": {"fast_window": 5, "slow_window": 15, "fixed_size": 1},
            }
        }
        setting_path.write_text(
            _json.dumps(bootstrap, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("REPLAY: bootstrap cta_strategy_setting.json → %s", setting_path)

    cta_engine.init_engine()
    # init_all_strategies 在工作线程跑，需 join 等待全部 inited 后再 start
    init_evt = threading.Event()

    def _watch_init():
        # 简单 busy-poll：每 200ms 检查所有策略 .inited
        deadline = time.time() + 60
        while time.time() < deadline:
            if cta_engine.strategies and all(
                getattr(s, "inited", False) for s in cta_engine.strategies.values()
            ):
                init_evt.set()
                return
            time.sleep(0.2)
        logger.error("REPLAY: 60 秒内策略未完成 init，仍尝试启动")
        init_evt.set()

    threading.Thread(target=_watch_init, daemon=True).start()
    cta_engine.init_all_strategies()
    init_evt.wait()
    cta_engine.start_all_strategies()

    # 从 DB 拉 bar
    db = get_database()
    bars = db.load_bar_data(
        symbol=symbol,
        exchange=exchange,
        interval=interval,
        start=_dt(2000, 1, 1),
        end=_dt(2099, 1, 1),
    )
    if not bars:
        logger.critical("REPLAY: DB 中无 %s %s bar，回放无意义，退出", vt_symbol, interval.value)
        main_engine.close()
        notifier.flush(timeout=5)
        sys.exit(1)
    logger.info("REPLAY: 从 DB 加载 %d 根 %s bar", len(bars), interval.value)

    gateway: ReplayGateway = main_engine.get_gateway(ReplayGateway.default_name)
    try:
        gateway.start_replay(bars, delay_ms=delay_ms, block=True)
    finally:
        logger.info("REPLAY: 回放结束，关闭引擎...")
        main_engine.close()
        notifier.send("REPLAY-SIT 已结束", title="系统关闭", level=NotifyLevel.WARNING, force=True)
        notifier.flush(timeout=10)
        logger.info("REPLAY: 干净退出")


if __name__ == "__main__":
    if QUANT_MODE == "REPLAY":
        _run_replay()
    else:
        main()
