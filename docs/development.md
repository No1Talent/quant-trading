# 开发与测试规范

日常拉新分支、跑测试、做回测、提交代码的操作手册。

新手入门请先看 [getting-started.md](getting-started.md)。
要写策略请配合 [strategy-development.md](strategy-development.md)。

---

## 1. 环境准备

### 1.1 Python 与 vn.py

| 项 | 要求 |
|----|------|
| Python | 3.10+（`pyproject.toml` 锁定）|
| 平台   | Windows 10/11（vn.py + CTP gateway 强 Windows 依赖）|
| vn.py  | 推荐用 [VeighNa Studio](https://www.vnpy.com/) 一键安装；CTP/CtaStrategy/CtaBacktester 等都附带 |

确认环境：
```powershell
python --version           # 3.10+
python -c "import vnpy; print(vnpy.__version__)"
```

### 1.2 安装开发工具

仓库根目录下：

```powershell
# 1. 装项目本身（editable，方便改完直接生效）
pip install -e .

# 2. 装开发链工具
pip install pre-commit pytest

# 3. 装 pre-commit 钩子（首次必须）
pre-commit install
```

之后每次 `git commit` 都会自动跑 ruff + ruff-format + mypy + gitleaks + 快速 pytest。

### 1.3 配置文件（每个 clone 都要做一次）

```powershell
copy vnpy_workspace\connect_ctp.json.template    vnpy_workspace\connect_ctp.json
copy vnpy_workspace\notify_config.json.template  vnpy_workspace\notify_config.json
```

凭据细节看 [security.md](security.md)。开发期通知可以全部 `enabled: false`，或者用 SimNow + 测试群机器人。

---

## 2. 日常开发流程

### 2.1 分支策略

- `main`：始终保持可启动、CI 绿。
- 任何改动开新分支：`feat/<topic>`、`fix/<topic>`、`docs/<topic>`、`refactor/<topic>`。
- PR 进 `main` 必须过 CI（见 `.github/workflows/ci.yml`）。

### 2.2 提交信息规范

[Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 风格——`git log` 看一眼就知道历史在做什么：

```
feat(risk_guard): 接入日内回撤熔断
fix(notifier): atexit 阶段静默 StreamHandler 异常
docs(roadmap): 标记 P0-3 完成
chore: 升级 ruff 至 0.4.4
ci: 给 dependabot 加 weekly schedule
build: 把 pyproject 的 requires-python 收紧到 3.10
refactor(listener): 把 last_balance 改成 dict
test(notifier): 并发 dedup 加压力测试
```

scope 用模块名（`notifier`、`risk_guard`、`listener`、`run`…）。

### 2.3 提交前自查清单

| 步骤 | 命令 |
|------|------|
| 1. 静态检查 + 单测 | `pre-commit run --all-files` |
| 2. 全量测试（包括 slow） | `pytest -v` |
| 3. 跑一遍主入口（手动） | `python vnpy_workspace\run.py` 启 GUI 看启动消息是否到 |
| 4. 写 / 更新 CHANGELOG | 至少在 `[Unreleased]` 节点下加一行 |

---

## 3. 测试规范

### 3.1 目录结构

```
tests/
├── __init__.py
├── test_notifier.py       # 通知器（单例 / 限流 / 去重 / flush / 渠道）
├── test_risk_guard.py     # 风控（回撤 / 持仓 / 频次 / 标志位）
└── ...                    # 新模块加新文件，一对一
```

### 3.2 跑测试

```powershell
# 全部测试
pytest -v

# 只跑快速测试（pre-commit 默认）
pytest -m "not slow" -v

# 只跑慢测试（集成 / e2e）
pytest -m slow -v

# 单个文件 / 单个用例
pytest tests/test_risk_guard.py -v
pytest tests/test_risk_guard.py::TestDailyLoss::test_loss_above_threshold_trips -v

# 覆盖率（可选）
pip install pytest-cov
pytest --cov=utils --cov-report=term-missing
```

### 3.3 写测试的三条规矩

1. **mock 外部依赖，别 mock 自己代码**
   - 网络、文件 IO、`main_engine.cancel_all_active_orders` → mock。
   - 自己写的 `RiskGuard._trip` 等内部逻辑 → 不 mock，让它真跑。

2. **每个测试自洁**
   - 用 fixture `tmp_path`、`tmp_flag` 隔离落盘文件。
   - 全局单例（`get_notifier()`）一定要在 fixture 里 `reset_notifier()`——见 [`tests/test_notifier.py`](../tests/test_notifier.py) 的 `cleanup` fixture。

3. **`slow` 标签留给真集成**
   - 任何 `sleep > 0.5s`、网络请求、跨进程的，加 `@pytest.mark.slow`。
   - 默认 `pre-commit` 不跑 slow，避免开发卡顿。

### 3.4 测试在 CI 怎么跑

`.github/workflows/ci.yml` 装 `pre-commit + pytest + pip install -e .`，然后 `pre-commit run --all-files`。这一行会同时跑 ruff、format、mypy、gitleaks、快速 pytest。

CI 环境**没有 vn.py**——CI 跑 `pip install -e .` 只装 `requests + urllib3`。意味着任何 `import vnpy.*` 的模块在 CI 是 import 不动的，但测试通过把 vnpy 的对象 mock 掉（`MagicMock` 替代 `EventEngine` / `main_engine`）规避了这点。

写新测试时——**不要在测试模块顶层 `from vnpy... import X`**，要么用 fixture 注入 mock，要么放 try/except，要么标 `pytest.mark.slow` 然后只在本地有 vn.py 的环境跑。

---

## 4. 回测规范

### 4.1 两种回测路径

| 路径 | 适用场景 | 是否需要 vn.py GUI |
|------|---------|------------------|
| **A. GUI（CtaBacktesterApp）** | 调参、看资金曲线、做参数扫描 | 是 |
| **B. 脚本（BacktestingEngine）** | CI 回归、参数固化后跑批量 | 不需要 GUI，但需要 vnpy_ctabacktester |

`run.py` 启动后通过 GUI 走路径 A；写自动化测试用路径 B。

### 4.2 GUI 回测步骤

1. `python vnpy_workspace\run.py` → 弹主窗口。
2. 顶栏 → `功能` → `CTA回测`。
3. 选合约、参数、起止日期、回测引擎类型（Bar / Tick）、点击 `开始回测`。
4. 等结果出，看资金曲线 / 成交记录 / 参数敏感度。

**回测不会触发推送**——因为 `CtaBacktesterApp` 不挂事件总线（不调 `attach_notify_listener`），自然没有 NotifyListener 监听器。

### 4.3 脚本回测骨架

```python
# scripts/backtest_double_ma.py
from datetime import datetime
from vnpy_ctabacktester.engine import BacktestingEngine

from strategies.double_ma_strategy import DoubleMaStrategy

# 关键：注入空通知器，万一策略里有 import 通知模块也不会真发
from utils.notifier import NullNotifier, set_notifier
set_notifier(NullNotifier())

engine = BacktestingEngine()
engine.set_parameters(
    vt_symbol="rb2510.SHFE",
    interval="1m",
    start=datetime(2024, 1, 1),
    end=datetime(2024, 6, 30),
    rate=1e-4,             # 手续费率
    slippage=1,            # 滑点（最小变动价位）
    size=10,               # 合约乘数
    pricetick=1,           # 最小变动价位
    capital=1_000_000,
)
engine.add_strategy(DoubleMaStrategy, {"fast_window": 10, "slow_window": 30})
engine.load_data()
engine.run_backtesting()
df = engine.calculate_result()
print(engine.calculate_statistics())
engine.show_chart()
```

### 4.4 回测时的"必须遵守"

| 约定 | 原因 |
|------|------|
| 不调 `attach_notify_listener` | 测试群刷屏，可能误报警 |
| 不调 `attach_risk_guard` | 回测的 `EVENT_ACCOUNT` 没有真实账户语义，会乱触发 |
| 调 `set_notifier(NullNotifier())` 兜底 | 防止策略文件违规 import 了通知模块 |
| 数据用 `import_data.py` 入库再跑 | 避免回测和实盘走两套数据路径，参考 [data-import.md](data-import.md) |
| 参数固化后写一个 `test_strategy_*.py` 锁结果 | 防止改一行代码默默改变回测结果（roadmap P0-4）|

### 4.5 回测 → 实盘的注意

回测过的策略**不等于**实盘能跑：
- Tick 数据精度可能不一致（vnpy 回测默认按 Bar 撮合）。
- 滑点和手续费模型是假设值，实盘要按真实成交核对。
- 风控（`attach_risk_guard`）只在实盘挂载；回测的资金曲线不代表会触发熔断。

模拟盘演练 1–2 周再上小资金，参见 [strategy-development.md](strategy-development.md) 的"SimNow 实盘演练"。

---

## 5. 添加新模块时的清单

每加一个 `utils/xxx.py`：

- [ ] 模块开头写一句话功能注释（不写多行 docstring）。
- [ ] 类型注解齐全（mypy 在 pre-commit 里管着）。
- [ ] 注册到 `utils/__init__.py` 的 `__all__`。
- [ ] 在 `tests/test_xxx.py` 加单测，至少覆盖主流程 + 一个错误路径。
- [ ] 在 `docs/` 找合适的章节加一行说明（或 [architecture.md](architecture.md) 加一个 box）。
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 节点下加一行。
- [ ] 在 [roadmap.md](roadmap.md) 找对应条目，标记完成或更新进度。

---

## 6. 调试与排查

- 日志：`logs/trader.log`、`logs/notifier.log`（已按日切割，保留 30 天 — 见 [operations.md](operations.md)）。
- 风控熔断会落盘 `logs/risk_breach.flag`，下次启动 `run.py` 会日志告警；确认账户状态后**手动删除**才能视作复位。
- 通知调试：把 `notify_config.json` 里目标渠道 `enabled` 改为 `true`，启动 `run.py` 后观察 `logs/notifier.log`。
- 慢/不稳定测试：见 [troubleshooting.md](troubleshooting.md) 的"测试相关"章节。

---

## 7. 速查表

| 我想…… | 命令 / 文件 |
|--------|------------|
| 跑所有测试 | `pytest -v` |
| 跑快速测试 | `pytest -m "not slow"` |
| 跑某个文件 | `pytest tests/test_risk_guard.py -v` |
| 自查代码 | `pre-commit run --all-files` |
| GUI 回测 | `python vnpy_workspace\run.py` → 功能 → CTA回测 |
| 脚本回测 | 参见 §4.3 |
| 启实盘 | `python vnpy_workspace\run.py` |
| 风控熔断后恢复 | 删 `logs/risk_breach.flag` 后重启 |
| 看通知日志 | `Get-Content logs\notifier.log -Wait -Tail 50` |
| 加新依赖 | 改 `pyproject.toml`，更新 PR 描述说理由 |
