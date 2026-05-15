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
    get_notifier,
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

    notifier = get_notifier()
    notifier.send("vn.py交易系统启动中...", title="系统启动", level=NotifyLevel.INFO, force=True)

    qapp = create_qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtpGateway)
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
