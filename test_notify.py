"""
================================================================
通知功能测试脚本 (v2)
================================================================
运行：
    cd C:\\Quant
    .venv\\Scripts\\activate.bat
    python test_notify.py
================================================================
"""

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(ROOT))

from utils.notifier import NotifyLevel, get_notifier


def run_tests():
    print("=" * 60)
    print("vn.py 通知模块测试 v2")
    print("=" * 60)

    n = get_notifier()
    channels = n._get_enabled_channels() if hasattr(n, "_get_enabled_channels") else []
    if not channels:
        print("\n⚠️  没有启用任何推送渠道！")
        print("请编辑 vnpy_workspace/notify_config.json 启用至少一个渠道")
        print("或复制 notify_config.json.template -> notify_config.json 并填入凭据")
        return

    print(f"\n启用渠道: {channels}")
    print("将发送约8条测试消息...\n")

    # 1. 基础消息
    print("[1/7] 基础消息...")
    n.send(
        "测试消息 - 通知模块工作正常 ✅", title="vn.py通知测试", level=NotifyLevel.INFO, force=True
    )
    time.sleep(2)

    # 2. 各级别
    print("[2/7] 级别测试...")
    n.send("WARNING级别：撤单失败但策略继续运行", level=NotifyLevel.WARNING, force=True)
    time.sleep(1)
    n.send("ERROR级别：策略代码异常", level=NotifyLevel.ERROR, force=True)
    time.sleep(2)

    # 3. 严重告警
    print("[3/7] 严重告警...")
    n.send_critical("CRITICAL级别测试：CTP连接断开")
    time.sleep(2)

    # 4. 成交推送
    print("[4/7] 成交推送...")
    n.send_trade(
        "螺纹双均线",
        {
            "symbol": "rb2510.SHFE",
            "direction": "多",
            "offset": "开仓",
            "price": 3850.0,
            "volume": 1,
            "datetime": datetime.now(),
        },
    )
    time.sleep(2)

    # 5. 错误推送（含堆栈）
    print("[5/7] 错误推送...")
    try:
        1 / 0
    except Exception as e:
        n.send_error("测试策略", "除零错误演示", exception=e)
    time.sleep(2)

    # 6. 信号推送
    print("[6/7] 信号推送...")
    n.send_signal("ATR突破策略", "上轨突破做多", "ATR=15.3\n上轨=3880\n当前价=3885")
    time.sleep(2)

    # 7. 去重验证
    print("[7/7] 去重测试（应只收到1条）...")
    for _ in range(3):
        n.send("去重测试 - 这条消息只应出现1次", level=NotifyLevel.INFO)
        time.sleep(0.3)
    time.sleep(2)

    # flush等待全部发完
    print("\n等待所有消息发送完成...")
    n.flush(timeout=15)

    print("\n" + "=" * 60)
    print("✅ 测试完毕，请检查对应渠道")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
