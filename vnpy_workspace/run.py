"""
================================================================
vn.py 交易系统启动入口 (v2 - 修复版)
================================================================
本版本修复：
    - SEVERE-2: 关闭时主动flush通知，避免消息丢失
    - SEVERE-6: 用监听器模式替代Mixin，策略无需感知通知模块
    - OPT-4:   用logging替代print
================================================================
"""

import logging
import os
import sys
from pathlib import Path

# ---------- 路径设置 ----------
WORKSPACE_DIR = Path(__file__).parent.absolute()
os.chdir(WORKSPACE_DIR)
PARENT_DIR = WORKSPACE_DIR.parent
sys.path.insert(0, str(PARENT_DIR))

# ---------- 配置日志 ----------
LOG_DIR = PARENT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "trader.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("trader")

# ---------- vn.py核心组件 ----------
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp
from vnpy_ctabacktester import CtaBacktesterApp

# ---------- Apps ----------
from vnpy_ctastrategy import CtaStrategyApp

# ---------- Gateway ----------
from vnpy_ctp import CtpGateway
from vnpy_datamanager import DataManagerApp
from vnpy_datarecorder import DataRecorderApp
from vnpy_portfoliostrategy import PortfolioStrategyApp
from vnpy_riskmanager import RiskManagerApp
from vnpy_spreadtrading import SpreadTradingApp

# ---------- 通知模块 ----------
from utils import NotifyLevel, attach_notify_listener, get_notifier


def main():
    logger.info("=" * 60)
    logger.info("vn.py 交易系统启动")
    logger.info("=" * 60)

    # 1. 初始化通知器
    notifier = get_notifier()
    notifier.send("vn.py交易系统启动中...", title="系统启动", level=NotifyLevel.INFO, force=True)

    # 2. 初始化Qt和引擎
    qapp = create_qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 3. 添加Gateway和Apps
    main_engine.add_gateway(CtpGateway)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(SpreadTradingApp)
    main_engine.add_app(PortfolioStrategyApp)
    main_engine.add_app(DataManagerApp)
    main_engine.add_app(DataRecorderApp)
    main_engine.add_app(RiskManagerApp)

    # 4. 挂载通知监听器（关键！策略不用改一行代码就自动有通知）
    attach_notify_listener(main_engine, event_engine, notifier)

    # 5. 启动GUI
    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    notifier.send("✅ 交易系统已就绪", title="系统就绪", level=NotifyLevel.INFO, force=True)

    # 6. 主循环
    try:
        qapp.exec()
    finally:
        # SEVERE-2: 关闭时主动flush，确保通知发出去
        logger.info("交易系统正在关闭...")
        notifier.send("交易系统已关闭", title="系统关闭", level=NotifyLevel.WARNING, force=True)
        # 主动等待所有通知发送完成（最多10秒）
        notifier.flush(timeout=10)

        # vn.py引擎关闭
        main_engine.close()
        logger.info("已干净退出")


if __name__ == "__main__":
    main()
