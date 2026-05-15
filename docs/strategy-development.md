# 策略开发指南

如何在本仓库里写自己的策略——同时保证回测/实盘行为一致、异常隔离、自动告警。

---

## 最小策略骨架

```python
from vnpy_ctastrategy import (
    CtaTemplate, BarData, BarGenerator, ArrayManager,
    TickData, OrderData, TradeData, StopOrder,
)
from utils.strategy_base import safe_callback


class MyStrategy(CtaTemplate):
    author = "Your Name"

    # 暴露给 GUI 的参数
    fast_window: int = 10
    fixed_size: int = 1
    parameters = ["fast_window", "fixed_size"]

    # 暴露给 GUI 的运行时变量
    fast_ma: float = 0.0
    variables = ["fast_ma"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager()

    def on_init(self):
        self.write_log("策略初始化")
        self.load_bar(self.fast_window + 1)

    def on_start(self):
        self.write_log(f"策略启动 fast_window={self.fast_window}")

    def on_stop(self):
        self.write_log(f"策略停止 持仓={self.pos}")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    @safe_callback
    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        self.fast_ma = self.am.sma(self.fast_window)
        # 你的信号逻辑
        if some_signal and self.pos == 0:
            self.write_log(f"信号: 做多 价格{bar.close_price}")
            self.buy(bar.close_price, self.fixed_size)

        self.put_event()

    def on_order(self, order: OrderData): pass
    def on_trade(self, trade: TradeData):
        self.write_log(f"成交 {trade.direction.value} @{trade.price} x{trade.volume}")
    def on_stop_order(self, stop_order: StopOrder): pass
```

完整范例：[double_ma_strategy.py](../strategies/double_ma_strategy.py)、[intraday_tick_strategy.py](../strategies/intraday_tick_strategy.py)。

---

## 三条必须遵守的规矩

### 1. 不要 import 通知模块

```python
# ❌ 永远不要这么写
from utils.notifier import get_notifier
notifier = get_notifier()
notifier.send_trade(...)

# ✅ 只用 write_log
self.write_log("信号: 金叉")
```

监听器会处理推送。理由见 [architecture.md](architecture.md)。

### 2. 高频回调用 `@safe_callback`

`on_bar`、`on_tick` 这类高频回调里出异常会让策略整个挂掉。装饰器 [`safe_callback`](../utils/strategy_base.py) 会：
- 捕获异常防止策略崩溃。
- 通过 `self.write_log("[ERROR] ...")` 输出。
- `NotifyListener` 监听到 LOG 事件里的 "Error" 关键词，自动推送告警。

```python
@safe_callback
def on_bar(self, bar):
    risky_computation()    # 抛异常也不会让策略死
```

`on_order` / `on_trade` 这类低频回调通常不需要装饰器（vn.py 引擎本身会捕获）。

### 3. 写有意义的 `write_log` 内容

监听器靠**关键词**判断告警级别：

| 关键词 | 触发级别 |
|--------|---------|
| 断线/连接失败/登录失败/CTP前置不活跃 | CRITICAL（force=True） |
| 错误/异常/失败/拒绝/Error/Exception/Failed | WARNING |

完整列表见 [`notify_listener.py`](../utils/notify_listener.py#L58)。

- ✅ `self.write_log("信号: 金叉做多 ...")` — 普通信号，不触发告警。
- ✅ `self.write_log("[ERROR] 持仓异常 expected 0 got 2")` — 含"ERROR"会告警。
- ❌ `self.write_log("oops")` — 没人会看到。

---

## 测试自己的策略

### 1. 单元测试（推荐）

把策略的纯计算逻辑抽到独立函数，用 pytest 测：

```python
# strategies/my_strategy.py
def calc_signal(fast_ma, slow_ma, prev_fast, prev_slow) -> str | None:
    if fast_ma > slow_ma and prev_fast <= prev_slow:
        return "long"
    if fast_ma < slow_ma and prev_fast >= prev_slow:
        return "short"
    return None

class MyStrategy(CtaTemplate):
    @safe_callback
    def on_bar(self, bar):
        ...
        signal = calc_signal(...)
        if signal == "long": self.buy(...)
```

```python
# tests/test_my_strategy.py
from strategies.my_strategy import calc_signal

def test_golden_cross():
    assert calc_signal(11, 10, 9, 10) == "long"
```

### 2. CtaBacktester 回测

`run.py` 已经加载了 `CtaBacktesterApp`。GUI 里选合约、参数、时间区间运行即可。

回测时**不需要**手动注入 `NullNotifier`——因为 `CtaBacktesterApp` 不挂 `NotifyListener`。

### 3. SimNow 实盘演练

跑通回测后，在 SimNow 上跑模拟盘 1–2 周，看：
- 通知告警是否如期触发。
- 拒单、断线等异常路径会不会爆出问题。
- 资金曲线是不是和回测一致（差距太大说明回测/实盘行为不一致，这是一个独立调试问题）。

---

## 多策略并行

vn.py 的 CtaStrategyApp 原生支持多策略实例。本仓库的 `NotifyListener` 已经按 `trade.reference`（策略名）区分消息，每条成交会显示来源策略。

如果你需要按策略关闭/启动某些告警，目前**没有这个能力**——所有策略共享同一个 listener。改造方案见 [roadmap.md](roadmap.md) P1 第 8 条。

---

## 常见陷阱

| 现象 | 原因 | 处理 |
|------|------|------|
| 回测把测试群刷屏 | 不小心挂了 `NotifyListener` | 回测脚本不要调 `attach_notify_listener` |
| 策略偶发崩溃后没有告警 | `on_bar` 没有 `@safe_callback` | 加上装饰器 |
| 信号触发但 GUI 看不到 | 没调 `self.put_event()` | 在变量更新后必调 |
| 告警没收到 | `notify_config.json` 渠道 `"enabled": false` 或环境变量未设置 | 看 [troubleshooting.md](troubleshooting.md) |
| 同样的告警一直发 | 触发的不是同一字符串（带时间戳），去重失效 | 让告警文案稳定，时间戳作为附加而非主体 |
