# 新手上手指南

零基础 5 步把这套补丁跑起来。

---

## 前置条件

- Windows 10/11，已安装 [VeighNa Studio](https://www.vnpy.com/) 或在 venv 里装好 `vnpy`、`vnpy_ctp`、`vnpy_ctastrategy` 等。
- Python 3.10+。
- 一个 [SimNow](http://www.simnow.com.cn/) 测试账号（先跑模拟盘，**永远不要直接上实盘**）。

---

## Step 1 — 把补丁覆盖到 vn.py 工作目录

```powershell
xcopy /E /I Quant_patch\* C:\Quant\
```

或者直接把整个 `Quant_patch/` 目录拷贝过去。

> 如果 `C:\Quant\` 里已经有同名文件（旧版本），覆盖前**先备份你的 `connect_ctp.json` 和 `notify_config.json`**——这两个含真实凭据，模板覆盖会覆盖空。

---

## Step 2 — 复制配置模板

```powershell
cd C:\Quant
copy vnpy_workspace\connect_ctp.json.template    vnpy_workspace\connect_ctp.json
copy vnpy_workspace\notify_config.json.template  vnpy_workspace\notify_config.json
```

---

## Step 3 — 填配置

### `connect_ctp.json`（CTP 凭据）

```json
{
    "用户名": "你的SimNow账号",
    "密码": "你的SimNow密码",
    "经纪商代码": "9999",
    "交易服务器": "tcp://180.168.146.187:10130",
    "行情服务器": "tcp://180.168.146.187:10131",
    "产品名称": "simnow_client_test",
    "授权编码": "0000000000000000"
}
```

### `notify_config.json`（通知渠道）

按需把 `"enabled": true`，填上 webhook 或邮箱凭据。**完整字段说明见 [security.md](security.md)**。最简配置——只用企业微信群机器人：

```json
{
    "dedup_window_seconds": 60,
    "rate_limit_per_minute": 30,
    "level_routing": {
        "INFO": ["wechat_work"],
        "WARNING": ["all"],
        "ERROR": ["all"],
        "CRITICAL": ["all"]
    },
    "wechat_work": {
        "enabled": true,
        "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    }
}
```

### 生产环境用环境变量替代文件凭据（推荐）

```powershell
$env:WECHAT_WORK_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
```

环境变量优先级**高于**配置文件。详见 [security.md](security.md)。

---

## Step 4 — 跑测试验证

```powershell
.venv\Scripts\activate.bat
pip install pytest
pytest tests/ -v
python test_notify.py
```

- 单元测试全绿 → 核心模块 OK。
- `test_notify.py` 推送成功 → 渠道配置 OK，应该收到一条测试消息。

如果失败，看 [troubleshooting.md](troubleshooting.md)。

---

## Step 5 — 启动交易系统

```powershell
python vnpy_workspace\run.py
```

会发生：
1. 加载 CTP Gateway + 各 App。
2. 挂载 `NotifyListener` 到事件总线。
3. 弹出 vn.py 主窗口。
4. 你的通知渠道会收到"系统启动"消息。

接下来用 GUI 加载策略、连接 CTP、订阅合约。具体操作参考 vn.py 官方文档。

---

## 下一步

- **想写自己的策略** → [strategy-development.md](strategy-development.md)
- **想导入历史数据** → [data-import.md](data-import.md)
- **理解为什么策略不用 import 通知模块** → [architecture.md](architecture.md)
- **关心日志/回测/告警细节** → [operations.md](operations.md)
