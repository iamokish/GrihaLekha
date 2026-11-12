"""India-local account dates without a system timezone database dependency."""

from datetime import datetime, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist():
    return now_ist().date()
