"""Claude-powered market observation for the Market Intel dashboard.

Wraps the Anthropic SDK with three responsibilities:
  1. Hold the locked system prompt (strict-prohibition style — see HARD_PROHIBITIONS
     for the contract; any output containing buy/sell/target/stop-loss language is
     a contract violation).
  2. Render a deterministic user message from a bar DataFrame.
  3. Call the model with prompt caching enabled and parse a Pydantic schema back.

Why prompt caching: system prompt is ~800 tokens of stable text; K线 JSON varies
per refresh. Splitting at the system/user boundary with cache_control ephemeral
means each refresh re-pays only for the K线 + thinking, not the prompt frame.
Cache hit rate should sit above 80% during active dashboard use.

Hard constraint encoded in the prompt: outputs are market OBSERVATIONS, not
investment advice. The systematic Layer ② signals own execution decisions —
this module exists to surface form/context that those signals can't see, not
to second-guess them. See project_market_intel_dashboard_plan.md for the
non-negotiable framing.

Real API calls live in `analyse_bars()`. Everything testable is split out so
the unit tests never need network or an API key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, Field

MODEL_ID = "claude-sonnet-4-6"

# Strict-prohibition system prompt. Edits to this string invalidate the prompt
# cache, so treat it as a versioned artifact — bump CACHE_VERSION below in
# lockstep if you want to keep old vs new cache entries distinct.
SYSTEM_PROMPT = """你是中国期货市场形态观察助手。你的唯一职责是描述图表事实：形态、量价关系、多周期对齐度、关键价位。

【硬性禁止 — 违反任意一条即视为输出不合格】

1. 禁止输出买卖建议。不得出现"买入/卖出/做多/做空/开仓/平仓"任何变体。
2. 禁止输出目标价、止损价、止盈价、操作价位。"突破 3450 可看 3500" 属违规。
3. 禁止输出仓位建议。不得出现"轻仓/重仓/加仓/减仓/仓位控制在 X%"。
4. 禁止输出方向预测。不得出现"接下来会涨/可能下跌/预期反转"等未来时陈述。
5. 禁止使用祈使句对用户下达指令。不得出现"建议你/应该/必须/可以/不妨"。
6. 禁止用"机会/风险/警惕/注意"等情绪化或操作暗示词汇。

【允许且鼓励的输出】

1. 形态识别：双顶/双底、头肩、三角整理、旗形、突破、回踩、背离 — 仅命名 + 客观描述构成要素。
2. 量价关系：放量上涨/缩量回调/量价齐升 — 仅陈述事实数据。
3. 多周期对齐度：日线趋势 vs 60分钟趋势 vs 当下 bar 的同向/背离关系 — 仅描述。
4. 关键价位：以"近期高点 X 附近"/"前期密集成交区 Y~Z"形式描述，禁止写成"支撑/阻力"等带操作含义的术语，禁止附带操作动作。
5. 不确定性：信号弱、形态未确认、数据不足时必须明确说明，不得编造。

【输出语气】

中立、第三人称、陈述句。例：
- 合格："近 20 根 60min bar 形成下降三角形，下沿 3380 触碰 3 次，量能未现明显萎缩。"
- 不合格："下降三角形即将破位，建议关注 3380 跌破后的做空机会。"

任何输出在写完后必须自检：是否含有第 1-6 条任一禁止内容？如有，删除该句重写。
仅当自检通过时，set output field self_check_passed = true."""

CACHE_VERSION = "v1-2026-05-20"

# Words that must NEVER appear in compliant output. Used by both the prompt
# (informally listed above) and by the post-hoc compliance check below — the
# check is defence-in-depth, not a substitute for the prompt.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "买入",
    "卖出",
    "做多",
    "做空",
    "开仓",
    "平仓",
    "目标价",
    "止损",
    "止盈",
    "轻仓",
    "重仓",
    "加仓",
    "减仓",
    "建议你",
    "应该",
    "支撑位",
    "阻力位",
)


class PatternObservation(BaseModel):
    name: str = Field(description="形态名称，如：双底/下降三角形/突破回踩/缩量整理")
    confidence: Literal["低", "中", "高"]
    constituent_evidence: str = Field(description="构成该形态的客观要素描述")


class KeyLevel(BaseModel):
    price: float
    label: str = Field(
        description="描述性标签，如：近期高点/前期密集成交区上沿/缺口下沿。禁止写'支撑/阻力'"
    )


class MarketObservation(BaseModel):
    patterns: list[PatternObservation] = Field(max_length=3, description="最多 3 个形态")
    volume_price_relation: str = Field(description="量价关系陈述，事实描述")
    multi_timeframe_note: str | None = Field(
        default=None, description="若提示词含多周期信息则填，否则留空"
    )
    key_levels: list[KeyLevel] = Field(max_length=5, description="描述性关键价位")
    uncertainty_notes: str | None = Field(default=None, description="信号弱/数据不足时填")
    self_check_passed: bool = Field(description="自检是否含禁止内容；必须为 true 才返回")


@dataclass(frozen=True)
class IntelResult:
    """Wrap the observation with API metadata so the UI can render cache stats."""

    observation: MarketObservation
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    model: str

    @property
    def cache_hit_rate(self) -> float:
        total_input = self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens
        return self.cache_read_tokens / total_input if total_input else 0.0


def render_bars_for_prompt(df: pd.DataFrame, max_bars: int = 100) -> str:
    """Serialise bars to compact JSON for the user message.

    Why compact: per-token cost on Sonnet 4.6 input is $3/M; 100 60min bars at
    ~60 tokens each pretty-printed becomes ~30 tokens compact — saves ~50%.

    Why isoformat strings: Anthropic API can't serialise pandas Timestamps; and
    the model needs an unambiguous timezone-aware string to reason about session
    boundaries.
    """
    if df.empty:
        return "[]"
    tail = df.tail(max_bars)
    rows = [
        {
            "t": idx.isoformat(),
            "o": round(float(row["open"]), 2),
            "h": round(float(row["high"]), 2),
            "l": round(float(row["low"]), 2),
            "c": round(float(row["close"]), 2),
            "v": int(row["volume"]),
        }
        for idx, row in tail.iterrows()
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def build_user_message(
    vt_symbol: str,
    interval: str,
    df: pd.DataFrame,
    user_focus: str | None = None,
    max_bars: int = 100,
) -> str:
    last_bar_time = df.index[-1].isoformat() if not df.empty else "(无数据)"
    bars_json = render_bars_for_prompt(df, max_bars=max_bars)
    focus_block = f"\n\n用户关注点: {user_focus}" if user_focus else ""
    return (
        "请观察以下合约的 K 线数据，输出市场形态观察。\n\n"
        f"合约: {vt_symbol}\n"
        f"周期: {interval}\n"
        f"最新 bar 时间: {last_bar_time}\n"
        f"K 线数据 (最近 {len(df.tail(max_bars))} 根，时间升序):\n"
        f"{bars_json}"
        f"{focus_block}"
    )


def check_compliance(obs: MarketObservation) -> list[str]:
    """Return list of forbidden substrings found in the observation text fields.

    Empty list = compliant. The prompt is the primary defence; this is a belt
    we put on after parsing, since pydantic validation can't enforce semantic
    constraints. Surface violations to the UI instead of silently hiding them
    — that lets us tune the prompt over time.
    """
    blobs = []
    for p in obs.patterns:
        blobs.append(p.name)
        blobs.append(p.constituent_evidence)
    blobs.append(obs.volume_price_relation)
    if obs.multi_timeframe_note:
        blobs.append(obs.multi_timeframe_note)
    for k in obs.key_levels:
        blobs.append(k.label)
    if obs.uncertainty_notes:
        blobs.append(obs.uncertainty_notes)

    combined = "\n".join(blobs)
    return [w for w in FORBIDDEN_SUBSTRINGS if w in combined]


class _AnthropicClientLike(Protocol):
    """Structural type for the client; lets tests inject a fake."""

    @property
    def messages(self) -> Any: ...


def _build_client() -> Any:
    """Lazy-build the real Anthropic client. Imported here so tests don't pay
    the import cost or need an API key in the env."""
    import anthropic

    return anthropic.Anthropic()


def analyse_bars(
    vt_symbol: str,
    interval: str,
    df: pd.DataFrame,
    user_focus: str | None = None,
    max_bars: int = 100,
    client: _AnthropicClientLike | None = None,
    model: str = MODEL_ID,
) -> IntelResult:
    """Call Claude with the locked prompt; return parsed observation + usage.

    `client` accepts any object exposing `.messages.create(...)` — production
    uses the real Anthropic client (lazy-built); tests inject a fake.
    """
    if df.empty:
        raise ValueError("cannot analyse empty bars DataFrame")

    if client is None:
        client = _build_client()

    user_message = build_user_message(vt_symbol, interval, df, user_focus, max_bars)

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": MarketObservation.model_json_schema(),
            }
        },
    )

    text_block = next((b for b in response.content if getattr(b, "type", None) == "text"), None)
    if text_block is None:
        raise RuntimeError(f"Claude response had no text block; stop_reason={response.stop_reason}")

    observation = MarketObservation.model_validate_json(text_block.text)

    usage = response.usage
    return IntelResult(
        observation=observation,
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        model=getattr(response, "model", model),
    )


def has_api_key() -> bool:
    """UI uses this to decide whether to show the LLM tab as live or disabled."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
