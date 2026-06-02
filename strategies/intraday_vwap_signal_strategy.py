"""分时图主信号策略：均价线门控 + 放量破前高 + 抬高低点，离场交给 ExitPolicy。

本策略落地 ``docs/intraday_fenshi_method.md`` 第 7/10 节「去重后的 A/B/C 三主信号」：

- **方向门控（均价线分水岭 + 日K趋势同向）**：``close > vwap`` 且趋势向上才允许做多（空头镜像）。
- **A 突破/回踩均价线**：``close`` 上穿当日 VWAP。
- **B 放量破前高**：``close`` 创近 ``breakout_window`` 新高，且本根成交量 > 近 N 均量 × ``vol_mult``。
- **C 抬高低点**：近 ``pivot_window`` 的最低点高于其前一段，多头结构（空头镜像为「降低高点」）。

进场 = 门控成立 且 (A 或 B 或 C)。离场全交给 :class:`utils.exit_policy.ExitPolicy`，
默认启用 **技术位止损（跌破均价线）+ 跟随 ATR 止盈** —— 正是 tick 策略拿不到 VWAP/ATR
而本策略（bar 级）能用的两个离场法。

均价线（VWAP）实现说明
----------------------
讲义的均价线 = 当日成交量加权均价。本策略用**多空乘数无关**的 bar 级近似::

    vwap = Σ(close_i × volume_i) / Σ(volume_i)      # 当日累计，按 session 重置

只用 OHLCV，不需要合约乘数（真正的结算 VWAP = turnover/(volume×mult) 需要乘数，
此处用 close×volume 近似，作为方向门控足够）。**session 重置按日历日变化**，夜盘跨日的
精确 trading-day 切分留作后续（见文末 caveat）。

数据粒度
--------
A/B/C 全部 bar 级可算（不依赖 tick 的买卖盘/现手），因此 1min~60min bar 均可跑；
真正的分时级精度需要 1min/tick 历史（见 import_data.py 的 tick interval 支持）。
"""

from __future__ import annotations

from vnpy.trader.constant import Direction, Offset
from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    TickData,
)

from utils.exit_policy import ExitConfig, ExitPolicy
from utils.strategy_base import (
    BaseCtaStrategy,
    safe_buy,
    safe_callback,
    safe_cover,
    safe_sell,
    safe_short,
)


class IntradayVwapSignalStrategy(BaseCtaStrategy):
    author: str = "Quant Team"

    # --- 信号参数 ---
    trend_window: int = 60  # 日K趋势代理：close 相对此 SMA 的上下（intraday SMA 代理日趋势）
    breakout_window: int = 20  # B：新高/新低回看窗
    vol_ma_window: int = 20  # B：均量窗 N
    vol_mult: float = 1.5  # B：放量倍数 k
    pivot_window: int = 10  # C：抬高低点/降低高点的两段比较窗
    fixed_size: int = 1

    # --- 离场参数（喂给 ExitPolicy）---
    use_vwap_stop: bool = True  # 技术位止损：跌破(多)/升破(空) 均价线
    trailing_atr_mult: float = 2.0  # 跟随止盈：自极值回撤 mult×ATR
    atr_window: int = 14
    stop_loss: float = 0.0  # 定额止损硬底（0=关，靠 vwap_stop）
    profit_target: float = 0.0  # 固定止盈（0=关，靠 trailing 让利润奔跑）
    breakeven_trigger: float = 0.0  # 保本触发（0=关）
    breakeven_offset: float = 0.0

    parameters = [
        "trend_window",
        "breakout_window",
        "vol_ma_window",
        "vol_mult",
        "pivot_window",
        "fixed_size",
        "use_vwap_stop",
        "trailing_atr_mult",
        "atr_window",
        "stop_loss",
        "profit_target",
        "breakeven_trigger",
        "breakeven_offset",
    ]
    variables = ["vwap", "session_date"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        size = max(self.trend_window, 2 * self.pivot_window, self.breakout_window, self.atr_window)
        self.am = ArrayManager(size=size + 5)

        self.exit_policy = ExitPolicy(self._build_exit_config())

        # 当日累计 VWAP 状态（按 session 重置）
        self.vwap: float = 0.0
        self.session_date: str = ""
        self._cum_pv: float = 0.0  # Σ close×volume
        self._cum_vol: float = 0.0  # Σ volume
        # 上一根的 close/vwap，用于 A 的「上穿」判定
        self._prev_close: float | None = None
        self._prev_vwap: float | None = None

    def _build_exit_config(self) -> ExitConfig:
        trigger = self.breakeven_trigger or None
        return ExitConfig(
            fixed_stop=self.stop_loss or None,
            fixed_target=self.profit_target or None,
            trailing_atr_mult=self.trailing_atr_mult or None,
            use_vwap_stop=self.use_vwap_stop,
            breakeven_trigger=trigger,
            breakeven_offset=self.breakeven_offset if trigger else 0.0,
        )

    def on_init(self) -> None:
        self.write_log(f"分时图主信号策略初始化：{self.strategy_name}")
        self.load_bar(max(self.trend_window, 2 * self.pivot_window) + 1)

    def on_trade(self, trade) -> None:
        """成交确认 → 登记/注销 ExitPolicy 逻辑仓（单一事实源，同 IntradayTickStrategy）。"""
        super().on_trade(trade)
        if trade.offset == Offset.OPEN:
            direction = 1 if trade.direction == Direction.LONG else -1
            self.exit_policy.open(direction, trade.price)
        else:
            self.exit_policy.close()

    @safe_callback
    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def _update_session_vwap(self, bar: BarData) -> None:
        """累计当日 VWAP；按日历日变化重置 session。"""
        bar_date = bar.datetime.strftime("%Y-%m-%d")
        if bar_date != self.session_date:
            self.session_date = bar_date
            self._cum_pv = 0.0
            self._cum_vol = 0.0
        self._cum_pv += bar.close_price * bar.volume
        self._cum_vol += bar.volume
        self.vwap = self._cum_pv / self._cum_vol if self._cum_vol > 0 else bar.close_price

    @safe_callback
    def on_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        self._update_session_vwap(bar)

        if not am.inited:
            self._remember(bar)
            return

        close = bar.close_price
        vwap = self.vwap
        atr = am.atr(self.atr_window)

        # 1) 持仓中：ExitPolicy 决策（bar 驱动 → advance_bar 默认 True）
        if self.exit_policy.active:
            decision = self.exit_policy.update(close, vwap=vwap, atr=atr)
            if decision.should_exit:
                self.write_log(f"离场[{decision.reason.value}] {decision.note}")
                self._close_position(bar)
            self._remember(bar)
            return

        # 2) 空仓：评估 A/B/C
        if self.pos == 0:
            long_signal, short_signal = self._evaluate_signals(close, vwap)
            if long_signal:
                safe_buy(self, close, self.fixed_size)
                self.write_log(f"做多信号 close={close:.2f} vwap={vwap:.2f}")
            elif short_signal:
                safe_short(self, close, self.fixed_size)
                self.write_log(f"做空信号 close={close:.2f} vwap={vwap:.2f}")

        self._remember(bar)
        self.put_event()

    def _evaluate_signals(self, close: float, vwap: float) -> tuple[bool, bool]:
        """返回 (long_signal, short_signal)：方向门控 ∧ (A ∨ B ∨ C)。"""
        am = self.am

        # 方向门控：均价线分水岭 + 日K趋势代理同向
        sma_trend = am.sma(self.trend_window)
        trend_up = close > sma_trend
        trend_dn = close < sma_trend
        long_domain = close > vwap
        short_domain = close < vwap

        # A 上穿/下穿均价线
        cross_up = (
            self._prev_close is not None
            and self._prev_vwap is not None
            and self._prev_close <= self._prev_vwap
            and close > vwap
        )
        cross_dn = (
            self._prev_close is not None
            and self._prev_vwap is not None
            and self._prev_close >= self._prev_vwap
            and close < vwap
        )

        # B 放量破前高/前低（前高用「不含当根」的近 W 根）
        # 注意：am.sma 是 close 的均线；成交量均值要直接对 volume 数组取，不能用 sma
        vol_ma = am.volume[-self.vol_ma_window :].mean()
        vol_up = vol_ma > 0 and am.volume[-1] > vol_ma * self.vol_mult
        prior_high = am.high[-self.breakout_window - 1 : -1].max()
        prior_low = am.low[-self.breakout_window - 1 : -1].min()
        b_long = vol_up and close >= prior_high
        b_short = vol_up and close <= prior_low

        # C 抬高低点 / 降低高点（两段 pivot_window 比较）
        pw = self.pivot_window
        recent_low = am.low[-pw:].min()
        seg_prior_low = am.low[-2 * pw : -pw].min()
        higher_lows = recent_low > seg_prior_low
        recent_high = am.high[-pw:].max()
        seg_prior_high = am.high[-2 * pw : -pw].max()
        lower_highs = recent_high < seg_prior_high

        long_signal = trend_up and long_domain and (cross_up or b_long or higher_lows)
        short_signal = trend_dn and short_domain and (cross_dn or b_short or lower_highs)
        return long_signal, short_signal

    def _remember(self, bar: BarData) -> None:
        self._prev_close = bar.close_price
        self._prev_vwap = self.vwap

    def _close_position(self, bar: BarData) -> None:
        volume = abs(self.pos)
        if not volume:
            return
        if self.exit_policy.direction > 0:
            safe_sell(self, bar.close_price, volume)
        elif self.exit_policy.direction < 0:
            safe_cover(self, bar.close_price, volume)
