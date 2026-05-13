# 代码审查修复补丁 - 变更说明

## 一、修复的严重问题（6项全部修复）

### SEVERE-1：单例竞态漏洞
**问题**：`Notifier.__new__`的双重检查锁有时序窗口，并发下可能操作半初始化实例。

**修复**：废弃`__new__`单例，改用**模块级单例函数** `get_notifier()` + `threading.Lock`。

**影响文件**：`utils/notifier.py`

### SEVERE-2：线程池永不关闭，退出时消息丢失
**问题**：`ThreadPoolExecutor`无shutdown，进程退出时在途消息全丢。

**修复**：
- 注册`atexit`回调，进程退出前自动`shutdown(wait=True)`
- 新增`flush(timeout)`方法，业务代码可在关键节点主动等待
- `run.py`的`finally`块调用`notifier.flush()`后再退出
- 新增`_shutdown_flag`拒绝已关闭后的新消息

**影响文件**：`utils/notifier.py`, `vnpy_workspace/run.py`

### SEVERE-3：去重/限流容器多线程不安全
**问题**：`recent_messages`字典和`send_timestamps`列表被多线程同时读写。

**修复**：新增`_dedup_lock`和`_rate_lock`两把细粒度锁，所有读写操作都在锁内完成。dict重建用新变量赋值，避免迭代中修改。

**影响文件**：`utils/notifier.py`

### SEVERE-4：敏感信息明文无保护
**问题**：CTP密码、邮箱授权码、Webhook URL明文存储，无.gitignore。

**修复**：
- 新增`.gitignore`，排除所有敏感配置文件
- 原配置文件改为`.template`后缀（可提交到git）
- 通知器支持**环境变量覆盖**敏感字段：
  - `EMAIL_AUTH_CODE`
  - `WECHAT_WORK_WEBHOOK`
  - `SERVER_CHAN_SENDKEY`
  - `DINGTALK_WEBHOOK`

**影响文件**：`.gitignore`, `vnpy_workspace/*.template`, `utils/notifier.py`

### SEVERE-5：内部错误递归告警
**问题**：Notifier发送失败→print错误→被LOG事件广播→on_log匹配"失败"关键词→再次推送→循环。

**修复**：
- Notifier内部错误只走`logging`模块，不走任何vn.py事件
- `NotifyListener.on_log`检测到消息含`[Notifier]`或`[NotifyListener]`时跳过

**影响文件**：`utils/notifier.py`, `utils/notify_listener.py`

### SEVERE-6：Mixin依赖MRO，解耦不彻底
**问题**：`NotifyMixin`要求特定继承顺序，子类重写on_start忘记super()就全失效。

**修复**：
- **废弃NotifyMixin**
- 新增`NotifyListener`（事件订阅模式），挂在事件总线上独立运行
- 策略代码**完全不需要import通知模块**，只用`self.write_log()`输出
- `NotifyListener`自动监听：
  - `EVENT_TRADE` → 成交推送
  - `EVENT_ORDER` → 拒单告警
  - `EVENT_LOG` → 错误/断线关键词告警
  - `EVENT_CTA_STRATEGY` → 策略启停状态变化推送
  - `EVENT_ACCOUNT` → 账户资金异常变化告警

**影响文件**：`utils/notify_listener.py`, `utils/strategy_base.py`, 所有策略文件

---

## 二、一并修复的优化项

| 编号 | 内容 | 修复方式 |
|------|------|---------|
| OPT-1 | 策略与Notifier强耦合 | 引入`INotifier`接口 + `NullNotifier`（回测用） |
| OPT-3 | requests无连接池/重试 | 改用`requests.Session` + `HTTPAdapter(Retry)` |
| OPT-4 | print与write_log混用 | 统一`logging`模块，自动写文件 |
| OPT-7 | 缺类型注解 | 关键接口加完整注解 |
| OPT-8 | Tick策略list.pop(0)是O(n) | 改`deque(maxlen=N)` |
| DB-1 | 数据导入无事务 | 分批写入 + 进度文件断点续传 |

---

## 三、新增文件

```
tests/
  __init__.py
  test_notifier.py          ← 单元测试（pytest）
utils/
  notify_listener.py        ← 事件订阅式通知（替代Mixin）
vnpy_workspace/
  connect_ctp.json.template ← 配置模板（可提交git）
  notify_config.json.template
.gitignore                  ← 敏感文件保护
```

## 四、替换文件

```
utils/notifier.py           ← 重写（单例/线程安全/连接池/日志）
utils/strategy_base.py      ← 简化（只保留safe_callback装饰器）
utils/__init__.py            ← 更新导出
vnpy_workspace/run.py        ← 用监听器，flush退出
strategies/double_ma_strategy.py   ← 解耦，不再import通知
strategies/intraday_tick_strategy.py ← deque修复
import_data.py               ← 分批+断点续传
test_notify.py               ← 适配新API
```

## 五、架构变化对比

### 旧架构（v1）
```
策略 ──import──→ Notifier ──直接调用──→ 邮件/微信/钉钉
  │
  └── on_start: self.notifier.send(...)
      on_trade: self.notifier.send_trade(...)
      on_bar:   self.notifier.send_signal(...)
```
问题：策略和通知紧耦合，回测也会触发推送。

### 新架构（v2）
```
策略 ──write_log──→ vn.py事件总线 ──→ NotifyListener ──→ 邮件/微信/钉钉
  │                     │
  │                     └── EVENT_TRADE ──→ 成交推送
  │                     └── EVENT_ORDER ──→ 拒单告警
  │                     └── EVENT_LOG   ──→ 关键词告警
  │                     └── EVENT_ACCOUNT → 资金告警
  │
  └── 策略不import任何通知代码，纯策略逻辑
```
好处：策略可独立测试，回测不挂NotifyListener就零副作用。

## 六、安装方法

1. 把补丁包解压到 `C:\Quant\` 覆盖同名文件
2. 复制模板为真实配置：
   ```
   copy vnpy_workspace\connect_ctp.json.template vnpy_workspace\connect_ctp.json
   copy vnpy_workspace\notify_config.json.template vnpy_workspace\notify_config.json
   ```
3. 编辑 `connect_ctp.json` 和 `notify_config.json` 填入凭据
4. 安装测试依赖：
   ```
   .venv\Scripts\activate.bat
   pip install pytest
   ```
5. 跑单元测试：
   ```
   pytest tests/ -v
   ```
6. 测试通知：
   ```
   python test_notify.py
   ```

## 七、回测时禁用通知

回测脚本里不挂载NotifyListener即可，或者主动注入NullNotifier：

```python
from utils.notifier import NullNotifier, set_notifier

# 回测前调用
set_notifier(NullNotifier())
```
