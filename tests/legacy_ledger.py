"""Explicit fixtures for histories that current public APIs no longer create."""

from grihalekha import models as m
from grihalekha.clock import today_ist
from grihalekha.dates import iso_date


def cancel_historical_bill(ledger, charge_id, reason, effective_on=None, *, prepare_replacement=False):
    """Seed a prior-version cancellation while keeping its dated ledger rules.

    Rent/electricity cancellation is now prohibited by the public API. Existing
    databases still contain these records and must retain restoration, report
    and balance behavior. Never monkeypatch the public guard for these tests.
    """
    effective = iso_date(effective_on or today_ist())
    with ledger._write() as conn:
        ledger._void_charge(conn, charge_id, reason, effective)
        if prepare_replacement:
            bill = ledger._get(conn, m.charges, charge_id)
            tenancy = ledger._get(conn, m.tenancies, bill["tenancy_id"])
            through = max(today_ist().isoformat(), bill["period_start"])
            ledger._prepare_drafts(conn, tenancy, through)
            ledger._prepare_electricity_drafts(conn, tenancy, through)
        ledger._audit(conn, "void", "charge", charge_id, effective_on=effective, reason=reason)
