# 故障排查 FAQ

按"症状"组织，找到症状直接看处理方法。

---

## 启动相关

### `python vnpy_workspace\run.py` 报 `ModuleNotFoundError: No module named 'vnpy'`

vn.py 没装或不在当前 Python 路径里。

```powershell
# 如果用 VeighNa Studio，要用它自带的 Python
C:\veighna_studio\python.exe vnpy_workspace\run.py
```

### 报 `ModuleNotFoundError: No module named 'utils'`

`run.py` 顶部把 `PARENT_DIR` 插入 `sys.path` 才能 import `utils`。如果你从别处复制了启动脚本但漏了那段路径设置，会报这个错。

### Qt 窗口闪一下就关闭

通常是 CTP 凭据错误或权限问题。看 `logs/trader.log` 里的 traceback。

### `配置文件不存在 notify_config.json`

你没复制模板：
```powershell
copy vnpy_workspace\notify_config.json.template vnpy_workspace\notify_config.json
```

补丁会用空配置启动（所有渠道禁用），不报错但收不到通知。

---

## 通知收不到

### 1. 检查渠道 `enabled`

打开 `notify_config.json`，对应渠道的 `"enabled"` 必须是 `true`（不是字符串 `"true"`）。

### 2. 检查环境变量是否生效

```powershell
echo $env:WECHAT_WORK_WEBHOOK
```

如果空，环境变量没设。注意 PowerShell 持久化要用：
```powershell
[System.Environment]::SetEnvironmentVariable("WECHAT_WORK_WEBHOOK", "...", "User")
```
然后**新开**一个窗口。

### 3. 跑通知自测

```powershell
python test_notify.py
```

成功 → 渠道配置 OK，是事件没触发的问题。
失败 → 看错误信息：
- `401` / `403` → webhook URL / 授权码无效。
- `Connection refused` → 网络问题或被防火墙拦。
- `企业微信API错误: {'errcode': 93000, ...}` → key 错。

### 4. 看 `logs/notifier.log`

```powershell
Get-Content C:\Quant\logs\notifier.log -Tail 50
```

如果有 `通知器已关闭，丢弃消息: ...` → 说明 `_shutdown_flag=True`，可能是 `flush()` 被重复调或 `atexit` 触发了。

### 5. 限流 / 去重把消息吞了

短时间发送大量相同内容会被去重（默认 60 秒窗口）。看日志有没有 `频率超限，丢弃消息`。

调大参数：
```json
"dedup_window_seconds": 10,
"rate_limit_per_minute": 60
```

---

## 策略相关

### 策略加载到 GUI 但启动不了

GUI 的 Log 窗口里通常有 traceback。常见：
- `parameters` 或 `variables` 列表里的字段在类里没定义。
- `__init__` 报错（典型：忘记 `super().__init__`）。

### `on_bar` 异常但没收到告警

检查策略是否用了 `@safe_callback`：

```python
@safe_callback           # ← 必须有
def on_bar(self, bar):
    ...
```

未装饰时异常会被 vn.py 引擎接住，但不会通过 `write_log` 触发告警监听器。

### 回测时也在推送通知

检查回测脚本里：
- 不应该调用 `attach_notify_listener(...)`。
- 如果用的 `CtaBacktesterApp`（GUI 回测），不会挂监听器，正常情况不会推送。
- 如果用 `BacktestingEngine` 自己写脚本，看看是不是 import 了 `run.py`（会执行 `attach_notify_listener`）。

兜底方案：
```python
from utils.notifier import NullNotifier, set_notifier
set_notifier(NullNotifier())
```

### 拒单告警刷屏

拒单关键词路径会推 WARNING。如果某个策略持续拒单，会被限流（默认 30/分钟）但 force=True 路径不会限流。

排查根本原因：
- 资金不足？
- 合约代码写错（如夜盘合约代码变化）？
- 触发交易所风控（频繁报撤、自成交）？

修策略而非屏蔽告警。

---

## 数据导入

### `CSV缺少必需列: {'open'}`

CSV 列名拼写错、大小写不对、或者用了中文列名。本工具要求小写英文：`datetime, open, high, low, close, volume`。

### 断点续传从奇怪的位置继续

检查 `{csv_name}.progress.json` 里的 `completed_rows`。如果想从头：
```powershell
del data\bar\rb2510_1min.progress.json
```

或者参数 `resume=False`。

### `第 N 行解析失败` 大量出现

通常是 `datetime` 格式不匹配。检查 CSV 实际格式，传 `datetime_format` 参数：
```python
import_csv_to_database(..., datetime_format="%Y/%m/%d %H:%M")
```

---

## 测试相关

### `pytest tests/ -v` 报 `No module named 'pytest'`

```powershell
.venv\Scripts\activate.bat
pip install pytest
```

如果用 VeighNa Studio 的 Python：
```powershell
C:\veighna_studio\python.exe -m pip install pytest
```

### `test_flush_waits_for_pending_tasks` 偶尔失败

时间相关的测试在慢机器上会 flaky。重跑通常就过。如果稳定失败，可能是线程池真的有 bug——开 issue。

---

## Git 相关

### 不小心把 `notify_config.json` 提交了

立刻：
1. 撤销/重置所有相关凭据（去对应平台的管理后台）。
2. 从历史移除：见 [security.md](security.md) "已泄露怎么办"。

### `git status` 显示一大堆 `__pycache__` / `*.pyc`

确认 `.gitignore` 里有：
```
__pycache__/
*.py[cod]
```

正常情况下我们的 `.gitignore` 已经排除。如果还显示，可能是这些文件之前已经 commit 过了：
```powershell
git rm -r --cached __pycache__
git commit -m "chore: ignore __pycache__"
```

---

## 还是搞不定？

1. 收集：完整的 traceback + `logs/notifier.log` 最后 100 行 + `logs/trader.log` 最后 100 行。
2. 截掉所有凭据 / webhook URL 的具体值（用 `xxx` 代替）。
3. 开 issue 描述：做了什么操作 → 期望什么 → 实际什么。
