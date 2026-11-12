"""Supported display currencies; no exchange rates or amount conversion."""

from dataclasses import dataclass

DEFAULT_CURRENCY = "INR"


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    name: str
    symbol: str


CURRENCIES = (
    Currency("INR", "Indian Rupee", "₹"),
    Currency("USD", "US Dollar", "$"),
    Currency("EUR", "Euro", "€"),
    Currency("JPY", "Japanese Yen", "¥"),
    Currency("GBP", "British Pound", "£"),
    Currency("CAD", "Canadian Dollar", "C$"),
    Currency("AUD", "Australian Dollar", "A$"),
    Currency("SGD", "Singapore Dollar", "S$"),
    Currency("NZD", "New Zealand Dollar", "NZ$"),
)
_BY_CODE = {currency.code: currency for currency in CURRENCIES}


def get_currency(code: str = DEFAULT_CURRENCY) -> Currency:
    """Resolve an exact supported code, rejecting invalid input consistently."""
    if not isinstance(code, str) or code not in _BY_CODE:
        raise ValueError("Choose a supported currency: " + ", ".join(_BY_CODE) + ".")
    return _BY_CODE[code]
