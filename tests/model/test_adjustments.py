from datetime import date

import pandas as pd
import pytest

from findata.model.adjustments import OPEN_END, OPEN_START, build_segments, event_factors


def _ca(rows: list[dict[str, object]]) -> pd.DataFrame:
    base = {"ratio": None, "amount": None}
    return pd.DataFrame([{**base, **r} for r in rows])


PRICES = pd.DataFrame(
    {
        "symbol": ["A", "A", "A"],
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "close": [200.0, 100.0, 50.0],
    }
)


def test_split_factor_is_ratio() -> None:
    ca = _ca([{"symbol": "A", "ex_date": "2024-01-04", "action_type": "split", "ratio": 0.1}])
    out = event_factors(ca, PRICES)
    assert out["factor"].tolist() == [0.1]


def test_dividend_factor_uses_close_before_ex_date() -> None:
    # Rs 5 dividend, close before ex-date (2024-01-03) = 100 -> factor 0.95
    ca = _ca([{"symbol": "A", "ex_date": "2024-01-04", "action_type": "dividend", "amount": 5.0}])
    assert event_factors(ca, PRICES)["factor"].iloc[0] == pytest.approx(0.95)


def test_same_day_dividends_multiply() -> None:
    ca = _ca(
        [
            {"symbol": "A", "ex_date": "2024-01-04", "action_type": "dividend", "amount": 5.0},
            {"symbol": "A", "ex_date": "2024-01-04", "action_type": "dividend", "amount": 10.0},
        ]
    )
    assert event_factors(ca, PRICES)["factor"].iloc[0] == pytest.approx(0.95 * 0.90)


@pytest.mark.parametrize(
    "row",
    [
        {"symbol": "A", "ex_date": "2024-01-04", "action_type": "split", "ratio": None},
        {"symbol": "A", "ex_date": "2024-01-04", "action_type": "bonus", "ratio": 0.0},
        {"symbol": "A", "ex_date": "2024-01-04", "action_type": "dividend", "amount": None},
        {"symbol": "A", "ex_date": "2024-01-02", "action_type": "dividend", "amount": 1.0},
        {"symbol": "A", "ex_date": "2024-01-04", "action_type": "dividend", "amount": 500.0},
        {"symbol": "A", "ex_date": "2024-01-04", "action_type": "rights", "ratio": 0.5},
    ],
)
def test_unusable_events_are_skipped(row: dict[str, object]) -> None:
    assert event_factors(_ca([row]), PRICES).empty


def test_empty_inputs() -> None:
    assert event_factors(pd.DataFrame(), PRICES).empty
    assert build_segments(pd.DataFrame()).empty


def test_segments_cumulative_product() -> None:
    events = pd.DataFrame(
        {
            "symbol": ["A", "A", "B"],
            "ex_date": pd.to_datetime(["2024-01-10", "2024-03-01", "2024-02-01"]),
            "factor": [0.5, 0.9, 0.1],
        }
    )
    seg = build_segments(events)
    a = seg[seg["symbol"] == "A"].reset_index(drop=True)
    assert a["valid_from"].tolist() == [OPEN_START, date(2024, 1, 10), date(2024, 3, 1)]
    assert a["valid_to"].tolist() == [date(2024, 1, 9), date(2024, 2, 29), OPEN_END]
    assert a["cumulative_factor"].tolist() == pytest.approx([0.45, 0.9, 1.0])
    b = seg[seg["symbol"] == "B"]
    assert b["cumulative_factor"].tolist() == pytest.approx([0.1, 1.0])
