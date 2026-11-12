"""Calendar periods with a stable billing anchor, including short months."""

# Public input validation consistently raises ValueError for the desktop UI.
# ruff: noqa: TRY004

from calendar import monthrange
from datetime import date, datetime, timedelta


def as_date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("Use a calendar date, not a timestamp.")
    if isinstance(value, date):
        return value
    try:
        result = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Enter a valid date in YYYY-MM-DD format.") from exc
    if result.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD for dates.")
    return result


def iso_date(value: str | date) -> str:
    return as_date(value).isoformat()


def anchor_date(year: int, month: int, billing_day: int) -> date:
    if (
        isinstance(billing_day, bool)
        or not isinstance(billing_day, int)
        or not 1 <= billing_day <= 31
    ):
        raise ValueError("Billing day must be between 1 and 31.")
    return date(year, month, min(billing_day, monthrange(year, month)[1]))


def shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + month - 1 + offset
    return total // 12, total % 12 + 1


def cycle_bounds(on: str | date, billing_day: int) -> tuple[date, date]:
    """Return [start, next_start), retaining the original billing day."""
    current = as_date(on)
    start = anchor_date(current.year, current.month, billing_day)
    if start > current:
        year, month = shift_month(current.year, current.month, -1)
        start = anchor_date(year, month, billing_day)
    year, month = shift_month(start.year, start.month, 1)
    return start, anchor_date(year, month, billing_day)


def iter_periods(
    move_in: str | date,
    billing_day: int,
    through: str | date,
    move_out: str | date | None = None,
):
    """Yield occupied inclusive periods and whether manual rent is required."""
    first = as_date(move_in)
    limit = as_date(through)
    last = as_date(move_out) if move_out else None
    if last and last < first:
        raise ValueError("Move-out cannot be earlier than move-in.")
    if limit < first:
        return
    start, next_start = cycle_bounds(first, billing_day)
    while start <= limit and (last is None or start <= last):
        occupied_start = max(first, start)
        natural_end = next_start - timedelta(days=1)
        occupied_end = min(natural_end, last) if last else natural_end
        if occupied_start <= occupied_end:
            yield {
                "cycle_start": start.isoformat(),
                "period_start": occupied_start.isoformat(),
                "period_end": occupied_end.isoformat(),
                "partial": occupied_start != start or occupied_end != natural_end,
            }
        start = next_start
        year, month = shift_month(start.year, start.month, 1)
        next_start = anchor_date(year, month, billing_day)


def iter_account_periods(tenancy: dict, through: str | date):
    """Yield service periods covered by this account's opening date.

    An imported account can open after the tenant already left. Its older
    dues belong to opening entries, so no new monthly bills are generated.
    """
    if tenancy.get("cancelled_on"):
        return
    first = max(tenancy["move_in"], tenancy["opening_date"])
    last = tenancy.get("move_out")
    if last and last < first:
        return
    yield from iter_periods(first, tenancy["billing_day"], through, last)


parse_date = as_date
