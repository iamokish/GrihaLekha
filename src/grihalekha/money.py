"""Exact fixed-point money with selectable display currency.

Public money values retain integer paise (hundredths) for compatibility.
"""

# Public input validation consistently raises ValueError for the desktop UI.
# ruff: noqa: TRY004

import re
from decimal import Decimal, InvalidOperation

from .currencies import DEFAULT_CURRENCY, get_currency

MAX_PAISE = 99_999_999_999_999


def validate_paise(value: int, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Money must be an integer number of paise.")
    if value < 0 or (not allow_zero and value == 0) or value > MAX_PAISE:
        raise ValueError(
            "Enter a valid non-negative amount within the supported range."
            if allow_zero
            else "Enter an amount greater than zero within the supported range."
        )
    return value


def parse_money(value: str | Decimal | int, *, currency_code: str = DEFAULT_CURRENCY) -> int:
    """Parse an amount without rounding or accepting a third decimal place.

    Both western and Indian digit grouping are accepted. Floats are rejected to
    avoid making their binary approximation part of a financial entry. An
    optional prefix must match the selected currency; the scale is always 100.
    """
    currency = get_currency(currency_code)
    if isinstance(value, (bool, float)):
        raise ValueError("Enter the amount as text with at most two decimal places.")
    text = str(value).strip()
    if text.startswith(currency.symbol):
        text = text[len(currency.symbol):].strip()
    elif text.upper().startswith(currency.code + " "):
        text = text[len(currency.code) + 1:].strip()
    if not text:
        raise ValueError("Enter an amount.")
    if "," in text:
        whole, _, fraction = text.partition(".")
        if not (
            re.fullmatch(r"\d{1,3}(,\d{3})+", whole)
            or re.fullmatch(r"\d{1,2}(,\d{2})*,\d{3}", whole)
        ):
            raise ValueError("The amount has invalid digit grouping.")
        text = whole.replace(",", "") + ("." + fraction if "." in text else "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise ValueError(
            f"Enter a non-negative {currency.code} amount with at most two decimal places."
        )
    try:
        result = int(Decimal(text) * 100)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("Invalid amount.") from exc
    return validate_paise(result)


def format_money(paise: int, *, symbol: bool = True, currency_code: str = DEFAULT_CURRENCY) -> str:
    """Format integer paise with Indian grouping and exactly two decimal places."""
    currency = get_currency(currency_code)
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise ValueError("Money must be an integer number of paise.")
    negative = paise < 0
    whole, fraction = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while head:
            groups.append(head[-2:])
            head = head[:-2]
        digits = ",".join(reversed(groups)) + "," + tail
    return (
        ("-" if negative else "") + (currency.symbol if symbol else "") + f"{digits}.{fraction:02d}"
    )


def decimal_rupees(paise: int) -> str:
    """Unlocalized two-decimal value for CSV and editable input fields."""
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise ValueError("Money must be an integer number of paise.")
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}{whole}.{fraction:02d}"


to_paise = parse_money
format_inr = format_money
