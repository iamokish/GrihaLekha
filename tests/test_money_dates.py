from datetime import date
from decimal import Decimal

import pytest

from grihalekha.dates import cycle_bounds, iter_periods
from grihalekha.money import decimal_rupees, format_money, parse_money, validate_paise


@pytest.mark.parametrize(
    "entered,paise",
    [
        ("0", 0),
        ("1.2", 120),
        ("₹1,23,456.78", 12345678),
        ("INR 1,234.50", 123450),
        (Decimal("0.01"), 1),
    ],
)
def test_exact_money(entered, paise):
    assert parse_money(entered) == paise
    assert parse_money(decimal_rupees(paise)) == paise


@pytest.mark.parametrize(
    "entered", ["1.001", "-1", "NaN", "1e4", "1,23.50", "", "2.", 0.1, True]
)
def test_invalid_money_never_silently_rounds(entered):
    with pytest.raises(ValueError):
        parse_money(entered)


def test_money_display_two_decimal_indian_grouping():
    assert format_money(123456789) == "₹12,34,567.89"
    assert format_money(-100) == "-₹1.00"
    with pytest.raises(ValueError):
        validate_paise(0, allow_zero=False)
    with pytest.raises(ValueError):
        validate_paise(10.5)


def test_anniversary_periods_and_calendar_month():
    feb = list(iter_periods("2026-02-05", 5, "2026-03-05"))
    assert [(row["period_start"], row["period_end"]) for row in feb] == [
        ("2026-02-05", "2026-03-04"),
        ("2026-03-05", "2026-04-04"),
    ]
    december = list(iter_periods("2026-12-01", 1, "2026-12-10"))
    assert december[0]["period_end"] == "2026-12-31"


def test_short_month_anchor_does_not_drift():
    assert cycle_bounds("2026-02-28", 31) == (date(2026, 2, 28), date(2026, 3, 31))
    assert cycle_bounds("2024-02-29", 31) == (date(2024, 2, 29), date(2024, 3, 31))
    periods = list(iter_periods("2026-01-31", 31, "2026-03-31"))
    assert [row["cycle_start"] for row in periods] == [
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
    ]


def test_partial_first_and_final_periods():
    periods = list(iter_periods("2026-02-12", 5, "2026-05-01", "2026-03-20"))
    assert [
        (row["period_start"], row["period_end"], row["partial"]) for row in periods
    ] == [("2026-02-12", "2026-03-04", True), ("2026-03-05", "2026-03-20", True)]
