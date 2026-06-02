"""M4: IntradayVwapSignalStrategy 验证 —— 分时图 A/B/C 主信号到底有没有 edge。

按 docs/intraday_fenshi_method.md 第 11 节的 gate:能写 ≠ 有 edge。

数据现状:DB 里 1h 只有 8 个独立 rb 合约（各 ~9 个月、~1023 根），没有连续 1h。
因此本验证分两层:

A) **跨合约固定参数**(headline):8 个合约各跑一遍**默认参数**(不做任何拟合)。
   这天然就是 8 段独立 OOS —— 若默认参数在多数合约亏钱,基本可判没有 inherent edge,
   再怎么调参也大概率是过拟合这 8 段。

B) **单合约 PWF**(过拟合检查):在数据最长的合约上跑 grid + 净化游走,
   看 IS→OOS 衰减。9 个月对 PWF 偏短,仅作参考。

结论无论正负都如实记录 —— 证伪也是结论。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.backtest_runner import run_backtest  # noqa: E402
from research.cpcv import print_summary, run_pwf, summarize  # noqa: E402
from strategies.intraday_vwap_signal_strategy import IntradayVwapSignalStrategy  # noqa: E402

logger = logging.getLogger("m4_vwap_validation")

# (vt_symbol, start, end) —— 取自 DB overview，各合约略向内收以避开首尾不全
RB_1H_CONTRACTS = [
    ("rb2210.SHFE", datetime(2022, 1, 26), datetime(2022, 10, 17)),
    ("rb2301.SHFE", datetime(2022, 5, 10), datetime(2023, 1, 16)),
    ("rb2305.SHFE", datetime(2022, 8, 25), datetime(2023, 5, 15)),
    ("rb2310.SHFE", datetime(2023, 2, 1), datetime(2023, 10, 16)),
    ("rb2401.SHFE", datetime(2023, 5, 6), datetime(2024, 1, 15)),
    ("rb2405.SHFE", datetime(2023, 8, 24), datetime(2024, 5, 15)),
    ("rb2410.SHFE", datetime(2024, 1, 23), datetime(2024, 10, 15)),
    ("rb2501.SHFE", datetime(2024, 5, 7), datetime(2025, 1, 15)),
]

# rb 合约参数（dict[str, Any] 以便 **splat 进带 bool 参数的回测函数，过 mypy）
BT_KWARGS: dict[str, Any] = dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1)
DEFAULT_PARAMS = {"fixed_size": 1}


def cross_contract_fixed_params(params: dict) -> pd.DataFrame:
    """A) 8 合约固定参数各跑一遍，返回每合约一行。"""
    rows = []
    for vt_symbol, start, end in RB_1H_CONTRACTS:
        try:
            stats = run_backtest(
                strategy_class=IntradayVwapSignalStrategy,
                params=params,
                vt_symbol=vt_symbol,
                interval="1h",
                start=start,
                end=end,
                **BT_KWARGS,
            )
        except Exception as e:
            logger.warning("backtest failed %s: %s", vt_symbol, e)
            continue
        rows.append(
            {
                "contract": vt_symbol,
                "trades": stats.get("total_trade_count"),
                "return_pct": stats.get("total_return"),
                "sharpe": stats.get("sharpe_ratio"),
                "max_dd_pct": stats.get("max_ddpercent"),
                "net_pnl": stats.get("total_net_pnl"),
            }
        )
    return pd.DataFrame(rows)


def single_contract_pwf(vt_symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """B) 单合约净化游走。grid 扫方向门控 + 放量阈值这两个最关键的旋钮。"""
    return run_pwf(
        strategy_class=IntradayVwapSignalStrategy,
        param_grid={
            "trend_window": [30, 60],
            "vol_mult": [1.2, 1.5, 2.0],
            "breakout_window": [10, 20],
        },
        fixed_params={"fixed_size": 1, "use_vwap_stop": True, "trailing_atr_mult": 2.0},
        vt_symbol=vt_symbol,
        interval="1h",
        start=start,
        end=end,
        n_folds=6,
        purge_days=10,  # 特征最长 trend_window≈60h≈10 交易日
        metric="sharpe_ratio",
        min_trades=3,
        **BT_KWARGS,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.WARNING)
    logging.getLogger("vnpy_ctabacktester.engine").setLevel(logging.WARNING)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    print("\n" + "=" * 90)
    print("A) 跨 8 个 rb 1h 合约 · 默认参数 · 纯 OOS（无拟合）")
    print("=" * 90)
    df = cross_contract_fixed_params(DEFAULT_PARAMS)
    print(df.to_string(index=False))

    sh = df["sharpe"].dropna()
    print("\n--- A 汇总 ---")
    print(f"  合约数:           {len(df)}")
    print(f"  总交易数:         {int(df['trades'].sum())}  (均 {df['trades'].mean():.1f}/合约)")
    if not sh.empty:
        print(f"  Sharpe 均值/中位: {sh.mean():+.3f} / {sh.median():+.3f}")
        print(f"  Sharpe 区间:      [{sh.min():+.3f}, {sh.max():+.3f}]")
        print(f"  正 Sharpe 合约:   {(sh > 0).sum()} / {len(sh)}")
        print(f"  正收益合约:       {(df['return_pct'] > 0).sum()} / {len(df)}")

    df.to_csv("research/m4_vwap_cross_contract.csv", index=False)
    print("  saved → research/m4_vwap_cross_contract.csv")

    print("\n" + "=" * 90)
    print("B) 单合约 PWF（最长合约 rb2305，过拟合检查，9 个月偏短仅供参考）")
    print("=" * 90)
    try:
        pwf_df = single_contract_pwf("rb2305.SHFE", datetime(2022, 8, 25), datetime(2023, 5, 15))
        if pwf_df.empty:
            print("  PWF 无有效 split（合约太短 / 信号太稀疏）")
        else:
            print(pwf_df.to_string(index=False))
            print_summary(summarize(pwf_df, label="VWAP/rb2305 PWF"))
            pwf_df.to_csv("research/m4_vwap_pwf_rb2305.csv", index=False)
            print("  saved → research/m4_vwap_pwf_rb2305.csv")
    except Exception as e:
        print(f"  PWF 跳过: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
