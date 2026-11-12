"""Optional cumulative electricity readings, stored as exact hundredths of kWh."""

import re
from decimal import Decimal


MAX_METER_READING = 99_999_999_999_999
UNSET = object()


def validate_meter_reading(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Meter readings must be integer hundredths of kWh or blank.")
    if not 0 <= value <= MAX_METER_READING:
        raise ValueError("Enter a non-negative meter reading within the supported range.")
    return value


def parse_meter_reading(value: str | Decimal | int | None) -> int | None:
    """Parse kWh without rounding; blank is distinct from a recorded zero."""
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise ValueError("Enter the meter reading as text with at most two decimal places.")
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 100 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        raise ValueError("Enter a non-negative meter reading in kWh with at most two decimal places.")
    whole, _, fraction = text.partition(".")
    return validate_meter_reading(int(whole) * 100 + int(fraction.ljust(2, "0")))


def format_meter_reading(value: int | None) -> str:
    value = validate_meter_reading(value)
    if value is None:
        return ""
    whole, fraction = divmod(value, 100)
    return f"{whole}.{fraction:02d}"
