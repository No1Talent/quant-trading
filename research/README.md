# research/ — Layer ② index

This directory is the quant research layer: signal discovery, walk-forward
analysis, overfit deflation, and pre-live robustness gates. It has **no runtime
dependency** on the live trading stack (`utils/`, `strategies/`, `vnpy_workspace/`).

- **Conclusions & the live verdict:** [docs/research-findings.md](../docs/research-findings.md)
- **60min-era phase report (superseded):** [docs/research-findings-2026-05.md](../docs/research-findings-2026-05.md)

## Naming convention

| Prefix | Arc | Meaning |
|---|---|---|
| `wfa_*` | 60min phase | per-contract walk-forward batches (first spike) |
| `h*` | hypothesis | daily-continuous time-series-momentum arc (H1 → H7) |
| `m*` | milestone | factor / CPCV / vol-target arc (M0.5 → M3.9) |

Decimal suffixes are follow-ups within a step (`h1_5`, `m0_5`, `m3_5`). Result
artifacts share the script's stem: `<script>` → `<stem>_summary.csv` / `.log`.
Summary CSVs (`wfa_results_*.csv`, the M-series `*_summary.csv` / `*_stats.csv`)
are tracked because they are part of the conclusions; large per-day panels and
run logs are gitignored.

---

## Generic harnesses (reusable)

| File | Purpose |
|---|---|
| `wfa.py` | WFA harness — grid search + `min_trades`, `return_curves`, `skip_empty_folds` |
| `cpcv.py` | Combinatorial Purged CV + Purged Walk-Forward (AFML ch.7), `purge_days=20` |
| `overfit_stats.py` | PSR / DSR / MinTRL / PBO-CSCV, closed-form, scipy-free (24 tests) |
| `vol_target.py` | `causal_vol_target` — size by trailing realized vol, lag-1, capped |
| `backtest_runner.py` | programmatic `BacktestingEngine` wrapper (+ `np.NINF` shim) |
| `panel_loader.py` | MultiIndex(datetime, symbol) parquet-cached panel builder |
| `factors.py` | 15 cross-sectional OHLCV+OI factors + `cs_rank` / `cs_zscore` |
| `factor_eval.py` | `forward_return`, `rank_ic`, `quintile_spread`, `evaluate_all` |
| `synthetic_alpha_test.py` | sanity check — confirms the WFA harness finds known alpha |
| `fetch_minute_data.py` | minute-bar fetcher (akshare / tushare / rqdatac → import CSV) |
| `viz/`, `scripts/render_wfa_report.py` | WFA figure rendering |

## 60min phase (pre-H1, closed — no robust alpha)

| File | What |
|---|---|
| `wfa_rb_batch.py` / `wfa_ag_batch.py` | DoubleMa+Donchian × rb / ag 60min |
| `wfa_boll_batch.py` / `wfa_boll_rb_deepdive.py` / `wfa_boll_ensemble.py` | BollReversal + ensemble + ATR-stop probe |
| `wfa_ag_donchian_deepdive.py` | Donchian/AG 8-contract stress (+0.52→+0.14 collapse) |
| `wfa_daily_batch.py` / `wfa_daily_continuous.py` | daily attempts (warmup-starved, see phase report §4) |

## H-series — daily-continuous trend arc

| File | Step | Result |
|---|---|---|
| `h1_rollover_test.py` | H1 | rollover-gap impact baseline |
| `h1_5_calendar_rollover.py` | H1.5 | OI-based surgical rollover + additive/ratio back-adjust |
| `h2_cross_instrument.py` / `h2_followup_raw_vs_adj.py` | H2 | family confirmed: AG/I/CU replicate DoubleMa daily |
| `h3_drop_fold15.py` | H3 | fold-robustness check |
| `h4_ensemble.py` | H4 | equal-risk ensemble → WFA Sharpe **+0.993** (upper bound) |
| `h4b_sensitivity.py` | H4b | leave-one-out: no single carrier (min LOO +0.730) |
| `h4c_slippage_stress.py` / `h4d_capital_sizing.py` | H4c/d | first-pass Layer ③ probes |
| `h5_ratio_backadjust.py` | H5 | ratio vs additive: I's edge is carry, not momentum |
| `h6_carry_attribution.py` | H6a | 60% of I PnL within ±5 days of rollover |
| `h6b_carry_strategy.py` / `h6c_hybrid_strategy.py` | H6b/c | explicit carry & gated variants both underperform |
| `h7_jm_doublema.py` / `h7b_jm_boll.py` | H7 | JM has no edge — don't promote from sibling inference |

## M-series — factor / CPCV / vol-target arc

| File | Step | Result |
|---|---|---|
| `m05_data_pool_expand.py` | M0.5 | batch fetch + back-adjust 14/22 instruments |
| (`factors.py` + `factor_eval.py`) | M1/M2 | 15-factor IC: max \|IC IR\| 0.108 at N=20 — no signal |
| `m25_factor_deep_eval.py` | M2.5 | IC-by-year + blend: combination HURTS −10% |
| `m3_h4_cpcv.py` | M3 | per-instrument WFA vs PWF — methodology-sensitive |
| `m35_h4_ensemble_cpcv.py` | M3.5 | ensemble PWF: +0.993 → **+0.526** |
| `m36_overfit_deflation.py` | M3.6 | PSR/DSR/MinTRL/PBO: +0.526 not 95%-significant, kurt 63 |
| `m37_ensemble_vol_target.py` | M3.7 | causal vol-target FLIPS it: **+0.782** sig; AG-solo +1.207 |
| `m38_vt_slippage_stress.py` | M3.8 | Layer ③ cost: all PASS at 5× |
| `m39_vt_capital_sizing.py` | M3.9 | Layer ③ capital: AG-solo PASS from ~500k |
| `m4_intraday_vwap_validation.py` | M4 | 剑客 A/B/C on 1h rb: no edge (coarse proxy) |

**Live embodiment of the winning arc:** `strategies/vol_target_ma_strategy.py`
(M3.7 result as a `CtaTemplate`; ships via PR #18).
