# Quant — vn.py 量化交易增强

基于 **vn.py + CTP** 的国内期货量化交易工作区，在原生 vn.py 之外补齐：

- 线程安全、防风暴、异步发送的多渠道告警（邮件 / 企业微信 / 钉钉 / Server酱 / 飞书）
- 事件总线驱动的通知监听器 — 策略代码零依赖通知模块
- 引擎层风控前置（日内回撤 / 持仓 / 成交频次熔断）+ 启动后 CTP 持仓对账
- 可断点续传的 CSV 历史数据导入
- 8 个 CTA 策略：1 个 live 候选（vol-target AG-solo）+ 7 个带完整证伪记录的研究形态
- Layer ② 研究管线：WFA / CPCV-PWF / PSR·DSR·MinTRL·PBO 去拟合 + causal vol-target（结论真源 [docs/research-findings.md](docs/research-findings.md)）
- `QUANT_MODE` 三态：LIVE / SIGNAL_ONLY（推信号不下单）/ REPLAY（历史回放 SIT）
- 三个 Streamlit 面板：研究浏览（app）/ 运维观察（live）/ 行情 intel（market）

仓库目录即 vn.py 工作目录，约定为 `C:\Quant\`。

---

## 快速开始

```powershell
# 1. clone 到 vn.py 工作目录
git clone <repo-url> C:\Quant
cd C:\Quant

# 2. 复制配置模板并填凭据
copy vnpy_workspace\connect_ctp.json.template    vnpy_workspace\connect_ctp.json
copy vnpy_workspace\notify_config.json.template  vnpy_workspace\notify_config.json

# 3. 启动
python vnpy_workspace\run.py
```

完整步骤、配置项说明、回测禁用通知等 → [docs/getting-started.md](docs/getting-started.md)。

---

## 依赖

`pyproject.toml` 只声明纯 Python 工具链依赖（requests / akshare / pandas 等）。vn.py
本体及 CTP 网关**不通过 PyPI 安装**——CTP 依赖编译好的 Windows DLL，PyPI wheel
经常版本不对位。

**推荐**：装 [VeighNa Studio](https://www.vnpy.com/)（一键安装器，自带 CTP DLL）。

**手动 venv 也可**，需要以下 vn.py 组件（按本仓库实际 import 顺序）：

| 组件 | 用途 | 仓库 import 位置 |
|------|------|-----------------|
| `vnpy` | 事件总线、主引擎 | `utils/notify_listener.py`、`utils/risk_guard.py` |
| `vnpy_ctp` | CTP 网关（行情+交易） | `vnpy_workspace/run.py` |
| `vnpy_ctastrategy` | CTA 策略框架 | 所有 `strategies/*.py` |
| `vnpy_ctabacktester` | 图形化回测 | （可选，仅 GUI 时需要） |

`pip install vnpy vnpy_ctp vnpy_ctastrategy` 在大多数 Windows + Python 3.10 环境
能拉到 wheel，但仍以官方 VeighNa Studio 为准。Linux/macOS 上 `vnpy_ctp` 无 wheel。

---

## 文档导航

按推荐阅读顺序排列。

| 文档 | 内容 |
|------|------|
| [getting-started](docs/getting-started.md) | 安装、配置、5 步启动 |
| [architecture](docs/architecture.md) | 事件驱动解耦原则与模块关系 |
| [strategy-development](docs/strategy-development.md) | 编写策略、单元测试、回测、SimNow 演练 |
| [development](docs/development.md) | 分支规范、提交规范、CI、日常速查 |
| [operations](docs/operations.md) | 日志管理、告警分级、风控熔断、运维操作 |
| [security](docs/security.md) | 凭据管理、环境变量优先级、泄露处理 |
| [data-import](docs/data-import.md) | CSV 历史数据导入、断点续传 |
| [troubleshooting](docs/troubleshooting.md) | 常见报错 FAQ |
| [roadmap](docs/roadmap.md) | 待办优化（P0–P3）与认领指南 |
| [CHANGELOG](CHANGELOG.md) | 版本变更记录 |

---

## 核心约定（必读）

**策略文件不允许 `import utils.notifier`**。只用 `self.write_log(...)`，由挂在事件总线上的 `NotifyListener` 独立完成推送。

为什么？详见 [architecture.md](docs/architecture.md)。

---

## 项目结构

```
Quant/
├── README.md / CHANGELOG.md
├── docs/                  # 所有文档（研究结论真源 research-findings.md 在此）
├── config/                # products.yaml 产品注册表 + watchlist + signal_service
├── utils/                 # 通知/风控/对账/换月/ExitPolicy/SIGNAL_ONLY·REPLAY 沙箱
├── strategies/            # CTA 策略（live 候选 + 研究记录）
├── research/              # Layer ② 研究层：WFA/CPCV/去拟合/vol-target + 结果表
├── scripts/               # ctp_smoke、WFA 报告渲染、服务启动脚本
├── vnpy_workspace/        # 入口 run.py + 配置模板
├── tests/                 # pytest 单元测试
├── signal_service.py      # standalone 信号服务入口
├── streamlit_app.py / streamlit_live.py / streamlit_market.py
├── import_data.py         # CSV → 数据库
├── data/                  # 历史数据 CSV（.gitignore，本地资产不入库）
└── logs/                  # 运行时日志（.gitignore）
```

---

## License & 联系

内部使用，未对外发布。问题与建议请走 issue。
