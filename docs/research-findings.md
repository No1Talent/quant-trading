# Layer ② Research — Master Findings

**Status:** Layer ③ pre-live gates COMPLETE (cost + capital). The lead live
candidate is **vol-targeted AG-solo trend**. Next step is a human go/no-go
decision (trading-safety red line), **not** more research.

This is the living master document for the research layer. For a per-script
navigation map see [research/README.md](../research/README.md); for the
60min-era phase report (superseded) see
[research-findings-2026-05.md](research-findings-2026-05.md).

---

## TL;DR — the live candidate

**Vol-targeted DoubleMa on AG (silver), single instrument.**

- Sharpe ~1.2, DSR 0.999, PSR ≈ 1.0, MinTRL ~450 (< sample) → statistically significant
- Max drawdown ~7% of capital, ~1× leverage
- Survives 5× slippage stress (+1.165); deployable from ~500k capital
- Live embodiment: `strategies/vol_target_ma_strategy.py` (ships via PR #18)
- **AG+CU** is a viable richer-risk variant (~0.9–1.0 Sharpe) but DD 18–24%/cap
  at the 5k vol target — halve the target for ~10–12% DD.
- **Drop I (iron ore)** — negative at every cost level; its earlier edge was a
  training-window artifact.

---

## 1. Strategies tested

| Family | Strategy file | Idea | Verdict |
|---|---|---|---|
| Trend (dual-MA) | `strategies/double_ma_strategy.py` | fast/slow MA crossover | ✅ survives — replicates on AG/I/CU daily |
| Trend (breakout) | `strategies/donchian_strategy.py` | N-day channel breakout | ❌ regime-dependent; +0.52→+0.14 IS-OOS corr at 8 contracts |
| Mean reversion | `strategies/boll_reversal_strategy.py` | Bollinger fade | ❌ "62% win-rate, loses money"; stops ⊥ mean-reversion |
| Carry / rollover | `strategies/carry_roll_strategy.py` | enter sign(roll-gap), hold N | ❌ worked 2013–18, broke after 2018 (H6b) |
| Trend × roll gate | `strategies/ma_cross_rollover_gated_strategy.py` | cross only near rollovers | ❌ 0 OOS trades on 14/15 folds (H6c) |
| Cross-sectional factors | `research/factors.py` | 15 OHLCV+OI rank factors | ❌ max \|IC IR\|=0.108 at N=20; blend hurts −10% |
| Intraday VWAP (剑客) | `strategies/intraday_vwap_signal_strategy.py` | A/B/C 分时图 signals | ❌ no edge on 1h rb (1h is a coarse proxy; native-tick untested) |
| **Vol-targeted trend** | `strategies/vol_target_ma_strategy.py` | DoubleMa sized by causal vol | ✅ **the live candidate** |

Reusable byproduct: `utils/exit_policy.py` (ExitPolicy — fixed/技术位穿均价线/
time stop + fixed/trailing-ATR/breakeven take-profit) applies to all live
strategies, even though the 剑客 A/B/C signal it came from had no directional edge.

Instruments studied: **AG** silver, **I** iron-ore, **CU** copper, HC hot-rolled
coil, AU gold, JM coking-coal, RB rebar.

---

## 2. The promotion arc — one number, four honesty filters

The headline Sharpe shrank as each over-fit filter was applied, then recovered
once the sizing bug was fixed:

```
WFA +0.993        →  PWF +0.526         →  vol-target +0.782 (AG-solo +1.2)
(upper bound)        (fat-tailed,           (significant, 5×-cost-robust,
                      not significant)        capital-robust from ~500k)
```

1. **WFA +0.993 was an upper bound, not an estimate.** The H4 equal-risk
   ensemble (AG.adj15 + I.raw + CU.adj15) scored +0.993 under walk-forward
   analysis. Re-running the *same panel* under Combinatorial Purged CV /
   Purged Walk-Forward (AFML ch.7, `research/cpcv.py`) cut it to **+0.526**
   (M3.5). Rule: never quote a WFA Sharpe without its PWF number beside it.

2. **+0.526 was not 95%-significant — and was secretly fat-tailed.**
   `research/overfit_stats.py` (PSR / DSR / MinTRL / PBO, native, scipy-free):
   PSR(0)=0.943, MinTRL@95%=2643 > the 2441 days available, DSR 0.42–0.59,
   kurtosis **63** (M3.6). Per-instrument, only **AG passes all** (DSR 0.90,
   PBO 0.011); **CU is fragile** (PBO 0.57); **I has no edge** (IS Sharpe ≈ 0,
   PBO 0.59 — its WFA +0.445 was pure windowing).

3. **The fat tail had one cause: vol-scaling, not a real risk.** The kurt=63
   traced to per-split inverse-*train*-vol weighting that over-levered AG into
   the 2026 silver vol explosion (M3.6b — AG's 2026 OOS daily std was ~16× the
   target).

4. **A causal daily vol-target flipped the verdict.** `research/vol_target.py`
   (size by trailing realized vol, lag-1, cap 4×, window 63d) inside the PWF
   loop (M3.7) → portfolio **+0.782, kurt 7.2, MinTRL 1095 < 2421 → significant**,
   DSR 0.719, PSR 0.993. AG-solo nearly doubled to **+1.207** (DSR 0.999) by
   down-sizing the silver spike. CU +0.392; **I stayed negative (−0.122)** even
   vol-targeted.

**Layer ③ gates (both PASS):**
- **Cost (M3.8):** all configs survive 5× slippage — AG-solo +1.165, AG+CU
  +0.910, AG+I+CU +0.764. Daily trend = low turnover → cost is not the dominant risk.
- **Capital (M3.9):** AG-solo clean PASS across 300k–10M (DD 6–9%/cap, ~1× lev);
  integer-lot quantization only bites below ~500k. AG+CU Sharpe-passes but at
  18–24% DD/cap unless the vol target is halved.

---

## 3. Methodology lessons (the transferable assets)

These outlive any single strategy and should gate all future research:

1. **IS-OOS Sharpe correlation > OOS Sharpe mean** as the edge/noise
   discriminator. Positive ≈ real edge; ≈0 = noise; negative = noise extraction.
2. **High IS Sharpe is a red flag, not green** — true edge looks like *moderate*
   IS Sharpe + positive IS-OOS correlation.
3. **Pair WFA with CPCV-PWF for every Sharpe claim**; quote the lower number.
4. **Before live, deflate with PSR(0) + MinTRL + DSR + PBO.** A Sharpe with
   PBO > 0.5 or DSR < 0.5 is not promotable regardless of its point value.
5. **Stops ⊥ mean-reversion** is structural, not empirical luck — proven on rb
   (any ATR stop cuts the profit source; G1).
6. **Reverse-DoubleMa is a statistical trap** — sub-random win-rate is not an
   invertible alpha; costs make a reversed loser lose worse (H7).
7. **Don't promote from inference on a sibling instrument** — JM looked like I
   and had no edge; DSR trial-count is the quantitative form of this rule.
8. **Sizing is part of the strategy.** The same signal went from
   not-significant to Sharpe ~1.2 purely by switching to causal vol-targeting.

---

## 4. Negative results — do NOT retry these recipes

- **Cross-sectional OHLCV+OI factor zoo at N=20** — exhausted (max \|IC IR\|
  0.108; blending hurts). To revive: N≥40 instruments, OR non-OHLCV features
  (basis, term-structure slope, COT, inventory), OR sector-relative ranking.
- **60min momentum/mean-reversion on rb/ag** — 88 fold-evaluations, no robust
  alpha. Coffin nailed (see the 2026-05 phase report).
- **Explicit carry / roll-gated variants on I** — both underperform continuous
  trend-follow (H6b/H6c).
- **Intraday A/B/C on 1h bars** — no edge (but 1h is a coarse proxy; native-tick
  is the only honest test and is blocked on data depth — AkShare free-tier caps
  ~3 days of 1min, ~1023 bars of 60min).

---

## 5. Infrastructure built (research/)

Generic harnesses (reusable):
- `wfa.py` — WFA harness: grid search + `min_trades` filter, `return_curves`,
  `skip_empty_folds` for event-gated strategies.
- `cpcv.py` — Combinatorial Purged CV + Purged Walk-Forward (AFML ch.7);
  train-before-test invariant, `purge_days=20`.
- `overfit_stats.py` — PSR / DSR / MinTRL / PBO-CSCV, closed-form, scipy-free
  (24 unit tests, CI-safe).
- `vol_target.py` — `causal_vol_target` (trailing realized vol, lag-1, cap).
- `backtest_runner.py` — programmatic `BacktestingEngine` wrapper (+ `np.NINF`
  shim for empyrical/NumPy 2.0).
- `panel_loader.py` / `factors.py` / `factor_eval.py` — cross-sectional panel +
  factor zoo + IC evaluation.
- `h1_5_calendar_rollover.py` — OI-based surgical rollover detection +
  additive/ratio back-adjust.

The full per-script index (h1→h7, m0.5→m3.9) is in
[research/README.md](../research/README.md).

---

## 6. Open items / next decision

- **Live go/no-go on AG-solo vol-target** — Elenka's call (trading-safety red
  line). Pre-flight blockers in `strategies/vol_target_ma_strategy.py` docstring:
  (1) confirm live daily-bar boundary == research exchange-daily bar (AG/CU have
  night sessions), (2) `contract_size` matches live contract (AG=15, CU=5),
  (3) pick capital → `size_scale` + `target_vol` (DD budget),
  (4) paper / SIGNAL_ONLY parity run.
- **2018 iron-ore regime break** — why pure gap-direction stopped predicting
  forward returns (event-calendar research, queued, not blocking).
- **More instruments** — test whether the trend-near-rollover pattern scales
  beyond the current universe (rebar, soybean, palm oil).

---

*Maintained as the single in-repo source of truth for Layer ② conclusions.
Update on every research milestone that changes a promote/drop/threshold verdict.*
