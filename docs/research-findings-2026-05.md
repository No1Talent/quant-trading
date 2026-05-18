# Layer ② 研究阶段性报告 — 2026-05

**期间：** 2026-05-17 ~ 2026-05-18（一个工作日内的研究 spike）
**作者：** Quant Team
**状态：** 阶段性 ✅，未结案。下一阶段方向见末尾。

---

## TL;DR

- 建成 **per-contract WFA 框架**（数据获取 → 回测 → 滚动窗口 → 网格优化 → 跨合约聚合），共评估 **40 个 fold** 覆盖 2 策略族 × 4 策略 × 2 品种 × 4-8 合约。
- 经合成数据验证，WFA harness 能正确识别 alpha（Sharpe ~10 on 已知趋势）。**所有"无 alpha"结论可信，不是工具 bug。**
- **6 个 strategy×instrument 组合的诊断结果：** 2 个值得继续（Donchian/AG, BollRev/RB），1 个边缘（BollRev/AG），3 个明确放弃。
- 核心方法学发现：**IS-OOS Sharpe 相关性比 OOS Sharpe 均值更稳定地区分 edge vs noise**。
- 限制：单合约日线 ~240 bars 无法支持标准 WFA；要做日线 TS 动量需解决主力连续/复权问题。

---

## 1. 方法学

### 1.1 数据策略：单合约独立 WFA
拒绝主力连续合约（rollover gap 污染 fold 边界），采用 4-8 个延寿延续的单合约，每个独立做 WFA。优点：物理上没拼接、没复权、没跳空。缺点：每合约 ~240 bars 日线（~1023 bars 60min），需要小心窗口配置。

### 1.2 WFA 配置
- **60min**: train=120 calendar / test=60 / step=60 → ~2 fold/合约
- **日线**: train=150 / test=60 / step=60 → ~3 fold/合约（但 OOS 0 trades，见 §4）
- **网格搜索** + `min_trades` 过滤（剔除运气信号），按 IS Sharpe 选最优
- **测度：** IS Sharpe, OOS Sharpe, IS-OOS Sharpe correlation（核心诊断）

### 1.3 工具栈
- 数据：AkShare（免费 60min/daily），存 vn.py SQLite（`utils/data_fetcher.py`）
- 回测：vn.py `BacktestingEngine` 程序化包装（`research/backtest_runner.py`）
- WFA：自研 `research/wfa.py` + 批量驱动 `research/wfa_*_batch.py`
- 兼容补丁：vn.py 自带 empyrical 引用了 `np.NINF`（NumPy 2.0 已删），在 backtest_runner 加 monkey-patch

---

## 2. 测试矩阵

测试了 4 个策略 × 2 个品种（rb 螺纹钢、ag 白银）× 2 个 timeframe（60min、日线），构成以下评估单元（仅 60min 完整可比，日线见 §4）：

| Strategy | Instrument | Folds | OOS Sharpe mean | OOS Sharpe std | OOS positive % | IS-OOS corr |
|----------|------------|-------|----:|----:|----:|----:|
| BollRev | RB | 8 | **+0.116** | 1.23 | **62%** | -0.725 |
| DoubleMa | RB | 8 | +0.058 | 2.55 | 50% | -0.418 |
| Donchian | AG | 8 | -0.147 | 2.79 | 50% | **+0.516** |
| Donchian | RB | 8 | -1.064 | 1.45 | 25% | -0.791 |
| BollRev | AG | 8 | -1.104 | 3.83 | 25% | +0.231 |
| DoubleMa | AG | 8 | -1.427 | 2.62 | 25% | -0.006 |

合成强趋势验证：DoubleMa OOS Sharpe **+9.51**（3/3 folds positive）→ harness 健康。

---

## 3. 关键发现

### 3.1 没有"全策略适用"的 timeframe 结论
"60min 没机会"是错的，"60min 全是 mean-reverting"也是错的。**每个 strategy × instrument 组合需要单独评估。**

### 3.2 IS-OOS Sharpe 相关性是核心诊断
比 OOS Sharpe 均值更稳定。原因：
- 均值会被单个 fold 主导（如 ag2406 撑起 Donchian/AG 整张表）
- 相关性测的是"我们的选择过程是否系统性有效"，不依赖单次幸运
- **正相关 = IS 信号转化为 OOS edge；零或负相关 = 优化器在挑噪音**

### 3.3 "有 alpha 但选不到"是一种独立的失败模式
**BollRev/RB 是教科书例子：** OOS 均值正 + 62% 折正收益，但 IS-OOS corr -0.73。
含义：策略本身在 RB 60min 有微弱正期望，但用 IS Sharpe 选参一定亏。
对策：(a) 不优化，固定一组合理参数跑；(b) 换优化目标（如 Sortino 或参数稳健性）；(c) 用 ensemble 跑多组参数取平均。

### 3.4 高 IS Sharpe 是红旗，不是绿灯
AG 上的 IS Sharpe 均 >1.5，OOS decay 平均 -1.7 ~ -3.5。**IS Sharpe 越高，越可能是过拟合，不是 edge 的证据。** 真 edge 的特征是**中等 IS Sharpe + 正 IS-OOS 相关性**。

### 3.5 vn.py BacktestingEngine 已确认 quirk
`CtaTemplate.load_bar(N)` 在 backtest 模式下**不从 DB 取 [start - N days, start) 的 warmup 数据**，只能从 engine.load_data() 已加载的 [start, end] 范围里取。后果：每个 fold 的 OOS 都要在 test 窗口内自己 warmup ArrayManager。

**实务影响：** WFA 的 `test_days` 必须 >> `slow_window × bars_per_day`，否则 OOS 0 trades。日线单合约（~240 bars）和默认 slow=20 + 60-day test 窗口正好踩雷，这是为什么日线测试在当前数据集下无法完成。

### 3.6 ArrayManager 默认 size=100 必须按需调整
默认 `ArrayManager()` 要 100 bars 才 `inited`。日线测试窗口 ~60 bars 直接饿死。改为 `ArrayManager(size=max(50, window+5))` 让短窗口策略也能跑。已应用到所有 3 个策略文件。

---

## 4. 日线测试的失败本身是一个发现

D2 阶段尝试在 rb+ag 日线上跑同样的 WFA。失败模式：
- IS：跑通，每个 fold 5-13 trades，IS Sharpe -0.7 ~ +2.5（正常分布）
- **OOS：全部 0 trades**，因为 §3.5 的 warmup 问题

**结构性结论：** 单合约日线（~240 bars/contract）+ 标准 WFA 窗口 = 无法测试任何 slow_window ≥ 15 的策略。

**要测日线 TS 动量必须先解决以下之一：**
- 用主力连续合约（rb0/ag0 有 4000+ bars）+ 容忍换月跳空
- 用复权后连续合约（AkShare 没有官方支持，需要自己用展期收益率拼接）
- 改换不依赖长 warmup 的策略（如截面动量、配对交易）
- 改 vn.py backtest engine 让 load_bar 真的从 DB 取历史 warmup

---

## 5. 已落地交付物

### 代码
- `utils/data_fetcher.py` — AkShare 60min/daily → CSV → vn.py DB CLI
- `research/backtest_runner.py` — 程序化 BacktestingEngine 包装 + np.NINF 兼容
- `research/wfa.py` — WFA harness：`make_windows`, `grid_search`, `run_wfa`
- `research/wfa_rb_batch.py` — RB 60min 批量 + 复用函数 `run_batch` / `summarize`
- `research/wfa_ag_batch.py` — AG 60min 批量
- `research/wfa_daily_batch.py` — 日线批量（产出 0 trades，见 §4）
- `research/wfa_boll_batch.py` — BollReversal 批量
- `research/synthetic_alpha_test.py` — 合成数据验证 harness 健康
- `strategies/double_ma_strategy.py` — 已修：ArrayManager(size=slow+5)
- `strategies/donchian_strategy.py` — 新增（time-series momentum 对照）
- `strategies/boll_reversal_strategy.py` — 新增（mean reversion 对照）

### 数据
- DB 内 16 个合约：rb2310/2401/2405/2410 和 ag2306/2312/2406/2410 各有 60min + daily 两套
- CSV in `data/bar/`：上述 16 套 + 4 套合成（syn1-4）

### 结果 CSV
- `research/wfa_results_rb_compare.csv` — DoubleMa+Donchian × RB 60min
- `research/wfa_results_ag.csv` — DoubleMa+Donchian × AG 60min
- `research/wfa_results_daily.csv` — 日线（0 trades，留作回归参考）
- `research/wfa_results_boll.csv` — BollReversal × RB+AG 60min

### Memory
- `project_research_layer2_status.md` 已记录方法论 + 32 fold 主结果

---

## 6. 下一阶段候选

按"信息回报 / 工作量"排序：

### 6.1 深挖 BollRev/RB 的"有 alpha 选不对"
- 试 OOS Sharpe + 参数稳健性的联合优化（不只看 IS Sharpe）
- 跑参数 ensemble：同时持有 3-5 组参数的等权组合
- 看是不是某些固定参数（如 boll_window=20）一直稳定，而 IS Sharpe 选了其他
- **工作量：半天。** 信息回报：高（可能从"没 edge"变"小 edge"）

### 6.2 深挖 Donchian/AG 的 +0.52 corr
- ag2406 单合约扛了正收益，需要更多 AG 合约验证（ag2206, ag2212, ag2502, ag2506...）
- 看是不是普遍现象还是 2024H1 特定行情
- **工作量：1 小时。** 信息回报：高（可能确认第一个真 edge）

### 6.3 解决日线数据长度问题
- 拉 RB0 daily（4158 bars），跑 WFA，看 daily TS 动量的真实表现
- 跳空污染需要量化评估（在 fold 边界附近的 trade 单独标记）
- **工作量：2 小时。** 信息回报：高（验证日线 vs 60min 真假设）

### 6.4 引入新数据维度
- 跨品种动量（同时看 rb, hc, i 三个黑色品种）
- 跨期套利（rb 不同月份合约价差）
- 加宏观/基本面因子（库存、利润率）
- **工作量：高（需要拉新数据 + 写新框架）。** 信息回报：高，但前期投入大

### 6.5 工程侧补强
- 给 `utils/data_fetcher.py`, `research/*` 补单测
- 改 `import_data.py` 接受 Interval 参数（现在 hardcode HOUR / 用 fetcher 间接传）
- 把 backtest 引擎的 mojibake 输出问题修掉（PYTHONIOENCODING=utf-8）
- **工作量：低，但量化研究优先级低**

**建议下一步：** 6.2 优先（最便宜的潜在突破），失败后做 6.1，6.3 留到有时间再做。

---

## 7. 教训沉淀

1. **不要在第一次得到"漂亮 Sharpe"时就停下** —— 1.71 IS Sharpe 在 WFA 下变成 0 OOS Sharpe，再不做 WFA 就盲目上实盘等于送钱。
2. **多策略族对比是必需的** —— 单看 DoubleMa 一类会高估"市场没机会"。BollRev 的 62% positive folds 是 DoubleMa 一类策略测试根本看不到的。
3. **库默认值不能假定合理** —— ArrayManager 默认 size=100 是 vn.py 的"安全起点"，但对短数据集是致命的。需要按场景手工配置。
4. **结构性失败本身有价值** —— 日线测不通的发现避免了我们花更多时间在错误的数据维度上。

---

*报告生成于 Layer ② 第一阶段总结。下一份报告将在 6.2 或 6.3 跑完后产出。*
