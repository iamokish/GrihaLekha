"""Account dates follow IST at midnight, month, and year boundaries."""

from datetime import date, datetime

import pytest

from grihalekha import clock


@pytest.mark.parametrize("utc,expected", [
    ("2026-11-17T18:29:59+00:00", "2026-11-17T23:59:59+05:30"),
    ("2026-11-17T18:30:00+00:00", "2026-11-18T00:00:00+05:30"),
    ("2026-01-31T18:30:00+00:00", "2026-02-01T00:00:00+05:30"),
    ("2026-12-31T18:30:00+00:00", "2027-01-01T00:00:00+05:30"),
    ("2024-02-28T18:30:00+00:00", "2024-02-29T00:00:00+05:30"),
    ("2024-02-29T18:30:00+00:00", "2024-03-01T00:00:00+05:30"),
])
def test_account_clock_converts_utc_to_ist(monkeypatch, utc, expected):
    instant = datetime.fromisoformat(utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(instant.timestamp(), tz)

    monkeypatch.setattr(clock, "datetime", FrozenDatetime)
    assert clock.now_ist().isoformat() == expected
    assert clock.today_ist() == date.fromisoformat(expected[:10])
