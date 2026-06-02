# M4 — IntradayVwapSignalStrategy 验证结论（2026-06-02）

> Gate（`docs/intraday_fenshi_method.md` §11）:能写 ≠ 有 edge。本文如实记录证伪。
> 复现:`python research/m4_intraday_vwap_validation.py`

## 结论:**当前数据上无可证明的 edge,不予推进上线。**

### A) 跨 8 个 rb 1h 合约 · 默认参数 · 纯 OOS（无拟合）

| 合约 | 交易 | 收益% | Sharpe | maxDD% |
|---|---|---|---|---|
| rb2210 | 66 | +0.31 | **+0.59** | -0.76 |
| rb2301 | 24 | +0.08 | **+0.71** | -0.08 |
| rb2305 | 17 | +0.24 | **+0.45** | -1.03 |
| rb2310 | 60 | -0.73 | **-1.62** | -0.99 |
| rb2401 | 50 | +0.25 | **+0.66** | -0.30 |
| rb2405 | 11 | -0.41 | **-1.08** | -0.64 |
| rb2410 | 7 | -0.37 | **-0.80** | -0.91 |
| rb2501 | 148 | +0.43 | **+1.05** | -0.35 |

- **Sharpe 均值 ≈ -0.004（约等于 0）**,中位 +0.52,正 Sharpe 5/8。
- **8 合约合计净盈亏 ≈ -2009**(负)。收益幅度都是 ±零点几个百分点,**信号噪声级**。
- 交易数极不均(7~148),门控稳定性差。

→ **均值 Sharpe ≈ 0 + 合计净亏 = 没有 inherent edge。** 5/8 正 Sharpe 是接近抛硬币的结果,
不是稳定优势。

### B) 单合约 PWF（rb2305,过拟合检查）

`run_pwf` n_folds=6 / purge=10d,grid 扫 trend_window × vol_mult × breakout_window:

- 4 个 split,**OOS Sharpe 均值 -1.99**,中位 -1.44,**std 3.34**(范围 [-6.05, +0.97])。
- 正 OOS 50%,IS-OOS corr +0.59 —— **IS 选出的参数在 OOS 深度为负且方差巨大**:
  典型的「参数选择不泛化、靠个别幸运 fold」。

→ **PWF 确认:没有可泛化的参数。** 与项目既往发现一致(横截面 OHLCV 因子为负、
H4 在 PWF 下 sign-flip,见 `project_research_layer2_status` / `project_cross_section_factor_research`)。

## 重要 caveat（这不是对「分时图方法」本身的终判）

1. **粒度太粗**:分时图法本质是 **tick / 1分钟** 级;这里用的是 **1h** bar,
   当日 VWAP 只摊在 ~6 根/日上,是很差的均价线代理。**native 粒度未被检验**。
2. **数据缺口**:DB 里 1h 只有 rb(8 合约、各 9 个月),无连续 1h、无其它品种、无 tick。
   `import_tick_csv_to_database` 已就绪但**盘上还没有 tick 历史**。
3. 因此本结论严格说是:**「A/B/C 的 1h bar 近似版在 rb 上无 edge」**,不是「分时图法必无效」。

## 建议

- **不要在 1h 上继续调参** —— 默认已约等于 0,再优化就是过拟合这 8 段。
- 真要继续:**先采集 1min/tick 历史**(带 turnover/oi),在 native 粒度重测;否则结论不算数。
- 真正落地的产出仍是 **ExitPolicy**(已 merge #10,对现有 5 策略通用),与 A/B/C 的方向 edge 无关。

---

## 追加(2026-06-02):native 1min 链路已打通,缺的只是「深数据」

为走「在 native 粒度重测」这一步,做了两件事:

1. **`research/fetch_minute_data.py`** —— 取数 → import-ready CSV,三源归一
   (akshare 免费/已实现、tushare pro/需 token、rqdatac/需账号)。
2. **端到端验证链路通**:`fetch_to_csv('RB0') → import_csv_to_database → run_backtest(interval='1m')`
   全程无报错,1023 根 1min 跑出 178 笔交易。

但 **数据深度是硬墙**:
- akshare Sina 1min 只给**最近 ~1023 根**(≈ 3 个交易日)——够验链路,**不够回测**。
- tushare pro `ft_mins` 能拉数年,但**需 token + 期货分钟积分**(环境里没配)。
- rqdatac 需 ricequant 账号(环境里没配)。

**一个值得记下的观察**:同一策略在 1min 上 3 天就触发 **178 笔**(1h 上 9 个月才 7~11 笔)。
粒度一变,**交易成本(滑点+手续费)会成为主要对手**——1min 上 per-trade edge 必须先盖过成本。

**真正推进 option 1 的前置(需人工)**:
- 拿到 tushare token(有分钟积分)→ 设 `TUSHARE_TOKEN` 环境变量 →
  `python research/fetch_minute_data.py RB2510.SHF --source tushare --start 20240101 --end 20241015`
- 或从券商终端(通达信/文华)导出 1min/tick CSV(列名映射成 `datetime/open/high/low/close/volume[/open_interest]`)。
- 数据到位后:`import_csv_to_database(...)` → 改 `m4_intraday_vwap_validation.py` 的合约清单为 1min → 重跑 gate。
