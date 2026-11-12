from decimal import Decimal

import pytest

from grihalekha.currencies import CURRENCIES, get_currency
from grihalekha.money import MAX_PAISE, decimal_rupees, format_money, parse_money


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda currency: currency.code)
@pytest.mark.parametrize("amount", [0, 1, 105, 123456789, MAX_PAISE])
def test_currency_changes_only_prefix_and_keeps_exact_amount(currency, amount):
    original = format_money(amount)
    displayed = format_money(amount, currency_code=currency.code)
    assert displayed == currency.symbol + original.removeprefix("₹")
    assert format_money(-amount, currency_code=currency.code) == (
        ("-" if amount else "") + displayed
    )
    assert format_money(amount, symbol=False, currency_code=currency.code) == original[1:]
    assert parse_money(displayed, currency_code=currency.code) == amount
    assert parse_money(f"{currency.code} {decimal_rupees(amount)}", currency_code=currency.code) == amount
    assert parse_money(Decimal(decimal_rupees(amount)), currency_code=currency.code) == amount


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda currency: currency.code)
@pytest.mark.parametrize("entered", ["1.001", "1e2", "NaN", "-1", True, 0.01, "1,23.45"])
def test_every_currency_rejects_rounding_and_invalid_input(currency, entered):
    with pytest.raises(ValueError):
        parse_money(entered, currency_code=currency.code)


@pytest.mark.parametrize("code", ["", "usd", "CHF", None, [], {}, 123, True])
def test_unknown_currency_is_rejected_without_guessing(code):
    for operation in (
        lambda: get_currency(code),
        lambda: format_money(100, currency_code=code),
        lambda: parse_money("1", currency_code=code),
    ):
        with pytest.raises(ValueError, match="supported currency"):
            operation()


@pytest.mark.parametrize("code,symbol", [("CAD", "$"), ("AUD", "C$"), ("USD", "₹"), ("INR", "€")])
def test_input_prefix_must_match_chosen_currency(code, symbol):
    with pytest.raises(ValueError):
        parse_money(f"{symbol}1.25", currency_code=code)


@pytest.mark.parametrize("amount", [True, 1.5, Decimal("1"), "100"])
def test_currency_formatting_rejects_non_integer_money(amount):
    with pytest.raises(ValueError, match="integer"):
        format_money(amount, currency_code="JPY")
