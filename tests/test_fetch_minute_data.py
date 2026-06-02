"""fetch_minute_data._normalize: 列归一化纯逻辑（取数的网络部分不在单测范围）。"""

from __future__ import annotations

import pandas as pd

from research.fetch_minute_data import OUT_COLUMNS, _normalize


def _raw(**overrides) -> pd.DataFrame:
    base = {
        "datetime": ["2024-01-02 09:01:00", "2024-01-02 09:02:00"],
        "open": [100.0, 101.0],
        "high": [101.0, 102.0],
        "low": [99.0, 100.0],
        "close": [100.5, 101.5],
        "volume": [10, 20],
        "hold": [1000, 1010],  # akshare 的持仓量列名
    }
    base.update(overrides)
    return pd.DataFrame(base)


class TestNormalize:
    def test_output_columns_exact(self):
        out = _normalize(_raw(), rename={"hold": "open_interest"})
        assert list(out.columns) == OUT_COLUMNS

    def test_hold_renamed_to_open_interest(self):
        out = _normalize(_raw(), rename={"hold": "open_interest"})
        assert out["open_interest"].tolist() == [1000, 1010]

    def test_missing_optional_filled_zero(self):
        raw = _raw().drop(columns=["hold"])  # 无持仓量列
        out = _normalize(raw, rename={})
        assert (out["open_interest"] == 0).all()

    def test_datetime_stringified(self):
        out = _normalize(_raw(), rename={"hold": "open_interest"})
        assert out["datetime"].iloc[0] == "2024-01-02 09:01:00"
        assert isinstance(out["datetime"].iloc[0], str)

    def test_numeric_coercion_and_dropna(self):
        raw = _raw(close=[100.5, "bad"])  # 第二行非数值 → 该行被 drop
        out = _normalize(raw, rename={"hold": "open_interest"})
        assert len(out) == 1
        assert out["close"].iloc[0] == 100.5

    def test_extra_columns_dropped(self):
        raw = _raw(amount=[5000, 6000])  # turnover 类多余列
        out = _normalize(raw, rename={"hold": "open_interest"})
        assert "amount" not in out.columns
