from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 5, 20, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "data")
    building = ledger.add_building("Home", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Departed tenant", "2024-01-01", 1, 100_000)
    yield ledger, tenant, flat
    ledger.close()


def _prepare(ledger, tenant, *, post=False):
    ledger.prepare_monthly_drafts("2024-05-01")
    bills = ledger.list_charges(tenant)
    if post:
        for bill in bills:
            ledger.post_charge(bill["id"], 100_000 if bill["kind"] == "rent" else 10_000)
    return {(row["period_start"], row["kind"]): row["id"] for row in bills}


def test_backdated_departure_stops_all_later_cycles_and_keeps_final_dues(account):
    ledger, tenant, _ = account
    bills = _prepare(ledger, tenant, post=True)
    fee = ledger.add_charge(tenant, "misc", 5_000, "2024-04-02", "2024-04-02", "2024-04-02", "Later fee")
    revision = ledger.get_revision()
    preview = ledger.preview_move_out(tenant, "2024-02-15", 50_000)
    assert ledger.get_revision() == revision
    assert len(preview["stopped_bill_ids"]) == 7
    assert preview["moved_out_cycle_start"] == "2024-02-01"
    assert preview["stopped_cycle_starts"] == ["2024-03-01", "2024-04-01", "2024-05-01"]
    assert preview["returned_paise"] == 0
    assert ledger.move_out(tenant, "2024-02-15", 50_000) == preview
    cycles = ledger.list_bill_cycles(tenant)
    assert [row["cycle_start"] for row in cycles] == ["2024-01-01", "2024-02-01"]
    assert not cycles[0]["moved_out_cycle"]
    assert cycles[1]["moved_out_cycle"] and cycles[1]["last_occupied_day"] == "2024-02-15"
    assert ledger.get_bill(fee)["status"] == "void"
    assert ledger.get_bill(bills[("2024-01-01", "rent")])["status"] == "posted"
    # The old meter bill remains in history; the shortened period needs review.
    old_power = ledger.get_bill(bills[("2024-02-01", "electricity")])
    assert old_power["period_end"] == "2024-02-29" and old_power["status"] == "void"
    final_power = next(row for row in cycles[-1]["charges"] if row["kind"] == "electricity" and row["status"] == "draft")
    assert final_power["period_end"] == "2024-02-15" and final_power["amount_paise"] is None
    assert ledger.balance(tenant)["outstanding_paise"] == 160_000
    assert len(ledger.list_bill_cycles(tenant, include_stopped=True)) == 5
    assert ledger.prepare_monthly_drafts("2025-01-01") == 0
    statement = ledger.statement(tenant, "2024-01-01", "2025-01-01")
    assert [row["cycle_start"] for row in statement["billing_cycles"]] == ["2024-01-01", "2024-02-01"]
    assert statement["billing_cycles"][-1]["moved_out_cycle"]
    assert statement["billing_cycles"][-1]["last_occupied_day"] == "2024-02-15"


def test_paid_later_cycles_return_money_without_rewriting_receipts_or_history(account):
    ledger, tenant, _ = account
    bills = _prepare(ledger, tenant, post=True)
    receipt = ledger.deposit_payment(tenant, 110_000, "2024-03-05", "bank", "Original receipt")
    for kind in ("rent", "electricity"):
        ledger.mark_bill_paid(bills[("2024-03-01", kind)], "2024-03-05")
    before = ledger.balance(tenant, "2024-04-01")
    preview = ledger.preview_move_out(tenant, "2024-02-15", 50_000)
    assert preview["returned_paise"] == 110_000
    ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.balance(tenant)["credit_paise"] == 110_000
    assert ledger.balance(tenant, "2024-04-01") == before
    assert ledger.get_bill(bills[("2024-03-01", "rent")], "2024-04-01")["paid_paise"] == 100_000
    payments = ledger.list_payments(tenant)
    assert len(payments) == 1 and payments[0]["id"] == receipt
    assert payments[0]["reversed_on"] is None
    assert any(row["cycle_start"] == "2024-03-01" for row in ledger.list_bill_cycles(tenant, "2024-04-01"))


@pytest.mark.parametrize("locked_cycle", ["2024-02-01", "2024-04-01"])
def test_move_out_lock_preflight_is_atomic_for_final_and_later_drafts(account, locked_cycle):
    ledger, tenant, _ = account
    _prepare(ledger, tenant)
    ledger.set_cycle_payment_lock(tenant, locked_cycle, True)
    before_bills = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    preview = ledger.preview_move_out(tenant, "2024-02-15", 50_000)
    assert preview["locked_cycle_starts"] == [locked_cycle]
    with pytest.raises(ValueError, match="Unlock"):
        ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["move_out"] is None
    assert ledger.list_charges(tenant) == before_bills
    ledger.set_cycle_payment_lock(tenant, locked_cycle, False)
    ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-02-15"


def test_departed_account_accepts_payment_months_later_then_can_archive(account, monkeypatch):
    ledger, tenant, _ = account
    _prepare(ledger, tenant, post=True)
    ledger.move_out(tenant, "2024-02-15", 50_000)
    final_power = next(row for row in ledger.list_charges(tenant) if row["status"] == "draft")
    ledger.post_charge(final_power["id"], 10_000)
    assert ledger.list_tenancies()[0]["id"] == tenant
    with pytest.raises(ValueError, match="unpaid bills"):
        ledger.archive_tenancy(tenant)
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 9, 20, 12, tzinfo=clock.IST))
    ledger.deposit_payment(tenant, 170_000, "2024-09-20", "upi")
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "posted":
            ledger.mark_bill_paid(bill["id"])
    assert ledger.balance(tenant)["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 0
    ledger.archive_tenancy(tenant)
    assert ledger.get_tenancy(tenant)["archived_on"] == "2024-09-20"


def test_departure_before_opening_preserves_older_dues_without_new_monthly_bills(account):
    ledger, tenant, _ = account
    ledger.set_opening_balances(tenant, "2024-05-01", rent_paise=100_000, electricity_paise=10_000)
    _prepare(ledger, tenant)
    result = ledger.move_out(tenant, "2024-02-15", 0)
    assert not result["final_rent_recorded"]
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-02-15"
    assert ledger.balance(tenant)["outstanding_paise"] == 110_000
    assert ledger.prepare_monthly_drafts("2025-01-01") == 0
    assert all(row["opening"] for cycle in ledger.list_bill_cycles(tenant) for row in cycle["charges"])
    statement = ledger.statement(tenant, "2024-01-01", "2025-01-01")
    assert all(row["cycle_start"] is None for row in statement["billing_cycles"])
    ledger.deposit_payment(tenant, 110_000, "2024-05-20", "cash")
    for row in ledger.list_charges(tenant):
        if row["opening"]:
            ledger.mark_bill_paid(row["id"])
    ledger.archive_tenancy(tenant)


def test_preopening_departure_does_not_silently_discard_nonzero_final_rent(account):
    ledger, tenant, _ = account
    ledger.set_opening_balances(tenant, "2024-05-01", rent_paise=100_000)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="opening balance"):
        ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["move_out"] is None


def test_repeated_departure_and_reextension_never_duplicate_required_bills(account):
    ledger, tenant, _ = account
    _prepare(ledger, tenant, post=True)
    ledger.move_out(tenant, "2024-02-15", 50_000)
    before = ledger.list_charges(tenant)
    ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.list_charges(tenant) == before
    ledger.move_out(tenant, "2024-04-20", 70_000)
    for cycle in ledger.list_bill_cycles(tenant):
        active = [row for row in cycle["charges"] if row["status"] != "void"]
        assert sorted(row["kind"] for row in active) == ["electricity", "rent"]
    active_power = [row for row in ledger.list_charges(tenant)
                    if row["kind"] == "electricity" and row["status"] != "void" and row["period_start"] >= "2024-03-01"]
    assert all(row["status"] == "draft" and row["amount_paise"] is None for row in active_power)
    assert ledger.prepare_monthly_drafts("2025-01-01") == 0


def test_move_out_before_a_recorded_transfer_is_rejected_without_erasing_history(account):
    ledger, tenant, flat = account
    building = ledger.list_buildings()[0]["id"]
    other = ledger.add_flat(building, "2", "First", 1)
    ledger.transfer_tenancy(tenant, other, "2024-03-01")
    before = ledger.get_tenancy(tenant)
    with pytest.raises(ValueError, match="recorded or scheduled"):
        ledger.move_out(tenant, "2024-02-15", 50_000)
    assert ledger.get_tenancy(tenant) == before
    assert before["occupancy_history"][0]["flat_id"] == flat


def test_cancel_planned_transfer_corrects_posted_rent_and_preserves_dated_payments(account, monkeypatch):
    ledger, tenant, flat = account
    building = ledger.list_buildings()[0]["id"]
    other = ledger.add_flat(building, "2", "First", 1)
    ledger.transfer_tenancy(tenant, other, "2024-06-01", rent_paise=120_000)
    ledger.prepare_rent_drafts("2024-06-01")
    bill = next(row for row in ledger.list_charges(tenant) if row["period_start"] == "2024-06-01")
    ledger.post_charge(bill["id"])
    # Older data can contain future-dated allocations; corrections must stay
    # after them even though the planned transfer is cancelled today.
    with monkeypatch.context() as historical_clock:
        historical_clock.setattr(clock, "now_ist", lambda: datetime(2024, 6, 2, 12, tzinfo=clock.IST))
        receipt = ledger.record_payment(tenant, 120_000, "2024-06-02", "bank", allocations={bill["id"]: 120_000})
    ledger.cancel_scheduled_transfer(tenant)
    tenancy = ledger.get_tenancy(tenant)
    assert tenancy["flat_id"] == flat and len(tenancy["rent_rates"]) == 1
    old = ledger.get_bill(bill["id"])
    assert old["status"] == "void" and old["voided_on"] == "2024-06-02"
    replacement = next(row for row in ledger.list_charges(tenant) if row["replaces_charge_id"] == bill["id"])
    assert replacement["status"] == "draft" and replacement["amount_paise"] == 100_000
    assert replacement["charge_date"] == "2024-06-02"
    assert ledger.balance(tenant, "2024-06-01")["outstanding_paise"] == 120_000
    assert ledger.balance(tenant, "2024-06-02")["credit_paise"] == 120_000
    assert ledger.list_payments(tenant, "2024-06-02")[0]["id"] == receipt
