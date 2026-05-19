"""vn.py 交易系统启动入口。挂载通知监听器和风控前置，自动连接 CTP。"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

WORKSPACE_DIR = Path(__file__).parent.absolute()
os.chdir(WORKSPACE_DIR)
PARENT_DIR = WORKSPACE_DIR.parent
sys.path.insert(0, str(PARENT_DIR))

# vn.py 的 TEMP_DIR 在 vnpy.trader.utility 导入时即确定，逻辑是 "若 cwd/.vntrader 存在
# 则用之，否则回退到 ~/.vntrader"。必须在任何 vnpy.* 导入之前预创建本目录，否则配置会
# 写到用户家目录，工作区的 connect_ctp.json 永远不会被读取。
(WORKSPACE_DIR / ".vntrader").mkdir(exist_ok=True)

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
    NotifyLevel,
    attach_notify_listener,
    attach_risk_guard,
    check_breach_flag,
    check_reconcile_flag,
    get_notifier,
    load_local_positions_for_reconcile,
    make_signal_only_class,
    run_reconcile,
)

# QUANT_MODE 控制订单路径：
#   LIVE         — 默认。真实下单到 CTP（保留原有行为）
#   SIGNAL_ONLY  — 拦截 send_order，合成成交事件，只给运营者发"信号触发"通知，不报单
# 选择 env var 而不是 JSON 字段是为了让"切到实盘"这一步显式、需要刻意操作（默认是不报单）。
QUANT_MODE = os.environ.get("QUANT_MODE", "LIVE").upper()
if QUANT_MODE not in ("LIVE", "SIGNAL_ONLY"):
    logger.warning("未知 QUANT_MODE=%r，回退到 LIVE", QUANT_MODE)
    QUANT_MODE = "LIVE"


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
    if QUANT_MODE == "SIGNAL_ONLY":
        logger.warning("=" * 60)
        logger.warning("⚠️  SIGNAL_ONLY 模式：策略信号会触发合成成交事件，但不下真单")
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
            if QUANT_MODE == "SIGNAL_ONLY":
                # SIGNAL_ONLY 不下真单，CTP 上的真实持仓和本地 sync_data 必然不一致，
                # 跑 reconcile 会假性失败。仅在 LIVE 模式做启动期对账。
                logger.info("SIGNAL_ONLY 模式：跳过启动期 CTP 对账（无实盘持仓需校对）")
            else:
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


if __name__ == "__main__":
    main()
