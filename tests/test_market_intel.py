"""market_intel: prompt invariants, schema parsing, cache wiring, compliance check.

We never hit the real Anthropic API — every test injects a fake client. The
goal here isn't to test Claude; it's to lock the contract our code makes with
the SDK so a future SDK upgrade or refactor surfaces breakage immediately.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from utils.market_intel import (
    FORBIDDEN_SUBSTRINGS,
    MODEL_ID,
    SYSTEM_PROMPT,
    KeyLevel,
    MarketObservation,
    PatternObservation,
    analyse_bars,
    build_user_message,
    check_compliance,
    render_bars_for_prompt,
)

SH = ZoneInfo("Asia/Shanghai")


def _sample_df(n: int = 5) -> pd.DataFrame:
    base = datetime(2024, 1, 1, 9, tzinfo=SH)
    idx = pd.DatetimeIndex([base + pd.Timedelta(hours=i) for i in range(n)], name="datetime")
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "volume": [1000 + i * 100 for i in range(n)],
        },
        index=idx,
    )


def _compliant_observation() -> MarketObservation:
    return MarketObservation(
        patterns=[
            PatternObservation(
                name="对称三角形整理",
                confidence="中",
                constituent_evidence="近 20 根 60min bar 高低点逐次收敛",
            )
        ],
        volume_price_relation="整理期间成交量持续萎缩约 35%",
        multi_timeframe_note=None,
        key_levels=[KeyLevel(price=3425.0, label="三角形上沿，最近一次触碰未破")],
        uncertainty_notes="三角形尚未完成方向选择",
        self_check_passed=True,
    )


def _fake_response(
    obs: MarketObservation,
    cache_read: int = 800,
    cache_creation: int = 0,
    input_tokens: int = 200,
    output_tokens: int = 300,
):
    text_block = SimpleNamespace(type="text", text=obs.model_dump_json())
    return SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        model=MODEL_ID,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        ),
    )


class TestSystemPrompt:
    def test_strict_prohibition_keywords_present(self):
        # If someone softens the prompt by accident, this catches it before
        # the model starts emitting buy/sell language.
        for keyword in ["禁止", "买入", "卖出", "目标价", "止损", "自检"]:
            assert keyword in SYSTEM_PROMPT, f"prompt lost prohibition marker: {keyword}"

    def test_allowed_outputs_listed(self):
        # Strict prohibition without examples of allowed output silently
        # collapses to "refuse everything" — the model needs to know what
        # IS valid, not just what isn't.
        for keyword in ["形态识别", "量价关系", "多周期"]:
            assert keyword in SYSTEM_PROMPT


class TestRenderBars:
    def test_compact_json_uses_short_keys(self):
        out = render_bars_for_prompt(_sample_df(2))
        parsed = json.loads(out)
        assert len(parsed) == 2
        # Single-char keys are the cost optimisation — guard against a future
        # "let's make this readable" refactor that doubles input tokens.
        assert set(parsed[0].keys()) == {"t", "o", "h", "l", "c", "v"}

    def test_max_bars_truncates_from_head(self):
        out = render_bars_for_prompt(_sample_df(20), max_bars=5)
        parsed = json.loads(out)
        assert len(parsed) == 5
        # Tail, not head — most recent bars are what matter for observation
        assert parsed[-1]["c"] == 120.0

    def test_empty_df_returns_empty_array(self):
        assert render_bars_for_prompt(pd.DataFrame()) == "[]"


class TestBuildUserMessage:
    def test_includes_symbol_interval_and_bars(self):
        msg = build_user_message("rb2501.SHFE", "60m", _sample_df(3))
        assert "rb2501.SHFE" in msg
        assert "60m" in msg
        assert '"c":101.0' in msg

    def test_user_focus_appended_when_provided(self):
        msg = build_user_message("rb2501.SHFE", "60m", _sample_df(3), user_focus="今天放量怎么看")
        assert "今天放量怎么看" in msg

    def test_user_focus_omitted_when_none(self):
        msg = build_user_message("rb2501.SHFE", "60m", _sample_df(3))
        assert "用户关注点" not in msg


class TestSchema:
    def test_compliant_observation_round_trips(self):
        obs = _compliant_observation()
        re_parsed = MarketObservation.model_validate_json(obs.model_dump_json())
        assert re_parsed == obs

    def test_patterns_capped_at_three(self):
        with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
            MarketObservation(
                patterns=[
                    PatternObservation(name=f"p{i}", confidence="中", constituent_evidence="x")
                    for i in range(4)
                ],
                volume_price_relation="x",
                key_levels=[],
                self_check_passed=True,
            )


class TestCompliance:
    def test_compliant_observation_passes(self):
        assert check_compliance(_compliant_observation()) == []

    def test_forbidden_substrings_detected_in_evidence(self):
        bad = MarketObservation(
            patterns=[
                PatternObservation(
                    name="突破",
                    confidence="高",
                    constituent_evidence="价格突破，建议你做多",
                )
            ],
            volume_price_relation="放量",
            key_levels=[],
            self_check_passed=True,
        )
        violations = check_compliance(bad)
        assert "建议你" in violations
        assert "做多" in violations

    def test_forbidden_substrings_detected_in_key_level_label(self):
        bad = MarketObservation(
            patterns=[],
            volume_price_relation="x",
            key_levels=[KeyLevel(price=3400.0, label="支撑位 3400")],
            self_check_passed=True,
        )
        # 'support level' wording would let prompts drift into advice territory
        assert "支撑位" in check_compliance(bad)

    def test_forbidden_substrings_constant_is_nonempty(self):
        # Cheap sanity — if someone empties the tuple thinking it's unused,
        # the compliance check silently passes everything.
        assert len(FORBIDDEN_SUBSTRINGS) > 10


class TestAnalyseBars:
    def test_passes_cache_control_on_system_block(self):
        # Lock the cache wiring — silent removal would multiply costs ~5x
        # without any test failure on its own.
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(_compliant_observation())

        analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)

        kwargs = fake_client.messages.create.call_args.kwargs
        system = kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == SYSTEM_PROMPT

    def test_uses_locked_model(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(_compliant_observation())
        analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
        assert fake_client.messages.create.call_args.kwargs["model"] == MODEL_ID

    def test_uses_adaptive_thinking(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(_compliant_observation())
        analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
        # 4.6+ deprecates budget_tokens; adaptive is the supported on-mode
        assert fake_client.messages.create.call_args.kwargs["thinking"] == {"type": "adaptive"}

    def test_passes_json_schema_output_config(self):
        # Prefills are 400 on 4.6 — output_config.format is the replacement
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(_compliant_observation())
        analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
        cfg = fake_client.messages.create.call_args.kwargs["output_config"]
        assert cfg["format"]["type"] == "json_schema"
        assert "properties" in cfg["format"]["schema"]

    def test_parses_response_into_observation(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(_compliant_observation())
        result = analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
        assert result.observation.patterns[0].name == "对称三角形整理"
        assert result.observation.self_check_passed is True

    def test_surfaces_cache_token_counts(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_response(
            _compliant_observation(), cache_read=800, cache_creation=0, input_tokens=200
        )
        result = analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
        assert result.cache_read_tokens == 800
        # 800 / (200 + 800 + 0) = 0.8
        assert result.cache_hit_rate == pytest.approx(0.8)

    def test_empty_df_raises(self):
        with pytest.raises(ValueError, match="empty"):
            analyse_bars("rb2501.SHFE", "60m", pd.DataFrame(), client=MagicMock())

    def test_no_text_block_raises_with_stop_reason(self):
        # If thinking-only response or refusal slips through, the user needs
        # to see *why* the analysis failed — silent empty would be confusing.
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="...")],
            stop_reason="refusal",
            model=MODEL_ID,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response
        with pytest.raises(RuntimeError, match="refusal"):
            analyse_bars("rb2501.SHFE", "60m", _sample_df(10), client=fake_client)
