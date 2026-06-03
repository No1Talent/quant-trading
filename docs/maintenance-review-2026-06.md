# Maintenance & dependency review — 2026-06

Reconstructed from an overnight code-review session whose commit was never
pushed (it ran in a read-only sandbox — see "Provenance" below). Every finding
here was **independently re-verified against `main`** before being recorded;
the status tags reflect that re-verification, not the original review's claims.

## Provenance

The overnight reviewer ran in a remote sandbox (`/home/user/quant-trading`) with
read-only GitHub access. Its push attempts returned 403 on both the git proxy
and the GitHub MCP integration, so its commits (`1f1883e`, amended `d70c003` on
branch `claude/wonderful-faraday-YSpQi`) exist **only in that ephemeral
environment** — they are not on the remote and not recoverable here. This
document and the accompanying `pyproject.toml` change re-land the verified,
non-conflicting subset through the normal PR flow.

## Overall health

Good shape. `ruff check` clean, full fast suite green, zero TODO/FIXME/HACK
markers, CI runs ruff + ruff-format + mypy + gitleaks + pytest, Dependabot wired.
Findings below are hardening, not firefighting.

## Applied in this PR

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| 1 | P1 | **Undeclared `scipy`.** `research/factor_eval.rank_ic` calls `Series.corr(method="spearman")`, which in pandas 2.x imports `scipy.stats.spearmanr` at runtime. scipy was declared nowhere, so on a clean install every factor raises inside `evaluate_all`'s broad `except` → the IC table comes back **empty**. Verified empirically (hiding scipy → `ModuleNotFoundError` on the exact call). | Added a `[research]` optional extra (`pip install -e .[research]`). Kept out of core deps — packaged `utils` never imports scipy. |
| 2 | P1 | **CVE-carrying dependency floors.** `requests>=2.28` (CVE-2024-35195, CVE-2023-32681) and `urllib3>=1.26` (redirect/cert advisories), both transitively reachable via the notifier. | Raised to `requests>=2.32.0`, `urllib3>=2.5.0`. Installed env (`requests 2.32.5`, `urllib3 2.6.2`) already satisfies both — no forced upgrade. |

## Deferred — collides with in-flight work

These were part of the overnight review but touch `research/factor_eval.py`,
which has **substantial uncommitted WIP on `feat/factor-cross-section-research`**
(+32 lines in `factor_eval.py`, +467 in `factors.py`). Applying them here would
create a merge conflict the moment that WIP resumes. Apply them **on the factor
branch** instead:

- **P2 — Redundant `forward_return` recomputation.** `evaluate_factor` recomputes
  `forward_return(panel, h)` for every (factor × horizon) — 16 × 3 = **48** of the
  expensive `unstack → log → stack` passes when only **3** distinct results exist
  (one per horizon). Fix: precompute `{h: forward_return(panel, h)}` once in
  `evaluate_all`, pass the cached series into `evaluate_factor`. Keep the public
  `evaluate_factor(factor, panel, horizons)` signature working (an internal
  `_fwd_by_horizon` kwarg, defaulted, preserves back-compat for
  `m25_factor_deep_eval.py` which imports `evaluate_factor`/`forward_return`/`rank_ic`).
- **P3 — `factor_eval` is untested and unimportable without vnpy.** `load_panel`
  is imported at module top (line 41) purely for `main()`, dragging vnpy into the
  pure-computation logic. Make that import lazy (inside `main()`), then add
  `tests/test_factor_eval.py` covering `rank_ic`, `quintile_spread`,
  `forward_return`, plus an equivalence guard (optimized == naive) and a
  call-count assertion (forward returns computed 3×, not 48×). Note: the
  call-count test only passes *after* the P2 optimization, so land them together.

## Recommendations — not changed

- **WFA reloads identical data per grid combo.** `research/backtest_runner.run_backtest`
  calls `engine.load_data()` fresh for every parameter combo over the same fixed
  training window. Biggest remaining perf win for sweeps/WFA, but it needs a vnpy
  runtime to verify a caching change safely (vnpy isn't installable on CI).
- `import_data._build_bars` uses `df.iterrows()` — `itertuples()` is materially
  faster, though this is a one-time ingestion path so the impact is bounded.
- **Pre-commit hook pins are stale** (e.g. ruff 0.4.4) and are **not** covered by
  Dependabot, which only watches `pip` + `github-actions`. Add a periodic
  `pre-commit autoupdate` (manual cadence or a scheduled job).
- **CI tests only Python 3.10** despite `requires-python = ">=3.10"`. Consider a
  3.10/3.11/3.12 matrix to catch version-specific breakage.
- **No dependency lockfile.** `>=` specs aren't reproducible across installs;
  a lock (pip-tools / uv) would pin the resolved set for CI and live boxes.

## Note on the notifier mypy finding

The overnight review also flagged `utils/notifier.py:325`
(`msg["Subject"] = Header(...)`) as a mypy error and wrapped it in `str(...)`.
**Not applied:** the repo's pinned pre-commit mypy passes on this line (confirmed
green in PR #11 CI) — the error only surfaces under a stricter/newer typeshed than
the repo pins. Recorded here so it isn't re-discovered as novel; revisit if/when
the mypy pin is bumped.
