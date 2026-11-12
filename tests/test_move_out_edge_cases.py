"""Historical departures, late settlements and atomic lifecycle corrections."""

from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2026, 11, 7, 12, tzinfo=clock.IST))
    account = Ledger(tmp_path / "account")
    building = account.add_building("House", "Address")
    flat = account.add_flat(building, "1", "Ground", 1)
    yield account, flat
    account.close()


def test_pre_opening_departure_keeps_opening_dues_and_accepts_late_payment(ledger):
    account, flat = ledger
    tenant = account.add_tenancy(flat, "Former resident", "2026-01-05", 5, 100_000,
                                 opening_date="2026-11-07")
    account.set_opening_balances(tenant, "2026-11-07", rent_paise=200_000,
                                 electricity_paise=15_000)
    account.prepare_monthly_drafts()
    account.move_out(tenant, "2026-07-10", 0)
    assert account.prepare_monthly_drafts("2027-01-01") == 0
    assert account.get_tenancy(tenant)["move_out"] == "2026-07-10"
    assert account.balance(tenant)["outstanding_paise"] == 215_000
    assert not any(bill["status"] != "void" and not bill["opening"]
                   for bill in account.list_charges(tenant))
    account.deposit_payment(tenant, 215_000, "2026-11-07", "cash")
    for bill in account.list_charges(tenant):
        if bill["opening"]:
            account.mark_bill_paid(bill["id"])
    assert account.balance(tenant)["outstanding_paise"] == 0
    account.archive_tenancy(tenant)
    assert account.get_tenancy(tenant)["archived_on"] == "2026-11-07"


def test_pre_opening_final_amount_validation_keeps_occupancy_and_bills(ledger):
    account, flat = ledger
    tenant = account.add_tenancy(flat, "Former resident", "2026-01-01", 1, 100_000,
                                 opening_date="2026-11-07")
    account.prepare_monthly_drafts()
    before = account.list_charges(tenant)
    revision = account.get_revision()
    with pytest.raises(ValueError, match="opening"):
        account.move_out(tenant, "2026-07-31", 50_000)
    assert account.get_tenancy(tenant)["move_out"] is None
    assert account.list_charges(tenant) == before
    assert account.get_revision() == revision


def test_backdated_move_out_keeps_delayed_payment_on_earlier_bill(ledger):
    account, flat = ledger
    tenant = account.add_tenancy(flat, "Tenant", "2026-01-01", 1, 100_000)
    account.prepare_monthly_drafts("2026-11-01")
    january = next(row for row in account.list_charges(tenant)
                   if row["kind"] == "rent" and row["period_start"] == "2026-01-01")
    account.post_charge(january["id"])
    payment = account.deposit_payment(tenant, 100_000, "2026-10-15", "upi")
    account.mark_bill_paid(january["id"], "2026-10-15")
    account.move_out(tenant, "2026-03-31", 100_000)
    assert account.get_bill(january["id"])["paid_paise"] == 100_000
    assert account.list_payments(tenant)[0]["id"] == payment
    assert account.balance(tenant)["credit_paise"] == 0
    assert account.prepare_monthly_drafts("2027-12-31") == 0
    assert all(row["cycle_start"] <= "2026-03-01" for row in account.list_bill_cycles(tenant))


def test_locked_later_electricity_rolls_back_entire_move_out(ledger):
    account, flat = ledger
    tenant = account.add_tenancy(flat, "Tenant", "2026-01-01", 1, 100_000)
    account.prepare_monthly_drafts("2026-04-01")
    electricity = next(row for row in account.list_charges(tenant)
                       if row["kind"] == "electricity" and row["period_start"] == "2026-04-01")
    account.post_charge(electricity["id"], 5_000)
    account.set_cycle_payment_lock(tenant, "2026-04-01", True)
    before = account.list_charges(tenant)
    revision = account.get_revision()
    with pytest.raises(ValueError, match="locked"):
        account.move_out(tenant, "2026-02-28", 100_000)
    assert account.get_tenancy(tenant)["move_out"] is None
    assert account.list_charges(tenant) == before
    assert account.get_revision() == revision
