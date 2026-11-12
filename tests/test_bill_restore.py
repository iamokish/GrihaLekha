"""Undo cancellation retains dated balances and cannot duplicate active bills."""

from datetime import datetime

import pytest
from sqlalchemy import select

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 20, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    bills = {row["kind"]: row["id"] for row in ledger.list_charges(tenancy)}
    yield ledger, tenancy, bills
    ledger.close()


@pytest.mark.parametrize("kind,amount", [("rent", 100_000), ("electricity", 12_345)])
def test_restore_reuses_required_draft_without_spending_balance_or_rewriting_history(account, kind, amount):
    ledger, tenancy, bills = account
    original = bills[kind]
    ledger.post_charge(original, amount)
    ledger.deposit_payment(tenancy, amount, "2024-01-02", "cash")
    ledger.mark_bill_paid(original, "2024-01-02")
    cancel_historical_bill(ledger, original, "Incorrect cancellation", "2024-01-05", prepare_replacement=True)
    replacement = next(row for row in ledger.list_charges(tenancy) if row["replaces_charge_id"] == original)
    receipts = ledger.list_payments(tenancy)
    history = {on: ledger.balance(tenancy, on) for on in ("2024-01-04", "2024-01-06")}
    restored_id = ledger.restore_cancelled_bill(original, "Bill was valid", "2024-01-07")
    assert restored_id == replacement["id"]
    restored = ledger.get_bill(restored_id)
    assert restored["status"] == "posted"
    assert restored["amount_paise"] == restored["outstanding_paise"] == amount
    assert restored["paid_paise"] == 0
    assert restored["charge_date"] == "2024-01-07"
    assert restored["replaces_charge_id"] == original
    assert ledger.get_bill(original)["status"] == "void"
    assert ledger.get_bill(original)["voided_on"] == "2024-01-05"
    assert ledger.list_payments(tenancy) == receipts
    assert ledger.balance(tenancy)["credit_paise"] == amount
    assert {on: ledger.balance(tenancy, on) for on in history} == history
    assert ledger.prepare_monthly_drafts("2024-01-20") == 0
    assert len([row for row in ledger.list_charges(tenancy) if row["kind"] == kind and row["status"] != "void"]) == 1
    for on in ("2024-01-04", "2024-01-06", "2024-01-07"):
        snapshot = ledger.statement(tenancy, "2024-01-01", "2024-01-31", as_of=on)
        assert snapshot["closing_balance_paise"] == ledger.balance(tenancy, on)["net_paise"]


def test_restore_unreviewed_electricity_stays_unknown_and_follows_move_out(account):
    ledger, tenancy, bills = account
    original = bills["electricity"]
    cancel_historical_bill(ledger, original, "Accidental click", "2024-01-05")
    restored_id = ledger.restore_cancelled_bill(original, "Still needs review", "2024-01-06")
    restored = ledger.get_bill(restored_id)
    assert restored_id != original
    assert restored["status"] == "draft" and restored["amount_paise"] is None
    assert restored["electricity_cycle_start"] == "2024-01-01"
    assert ledger.balance(tenancy)["billed_paise"] == 0
    ledger.move_out(tenancy, "2024-01-25", 50_000)
    assert ledger.get_bill(restored_id)["period_end"] == "2024-01-25"
    assert ledger.prepare_monthly_drafts("2024-01-31") == 0


def test_restore_zero_electricity_creates_no_payment(account):
    ledger, tenancy, bills = account
    ledger.post_charge(bills["electricity"], 0)
    cancel_historical_bill(ledger, bills["electricity"], "Accidental click", "2024-01-05", prepare_replacement=True)
    restored_id = ledger.restore_cancelled_bill(bills["electricity"], "Zero confirmed")
    bill = ledger.get_bill(restored_id)
    assert bill["status"] == "posted"
    assert bill["amount_paise"] == bill["outstanding_paise"] == 0
    assert ledger.list_payments(tenancy) == []


def test_repeated_undo_cannot_reuse_an_already_restored_automatic_draft(account):
    ledger, tenancy, bills = account
    cancel_historical_bill(ledger, bills["rent"], "Mistake", "2024-01-02", prepare_replacement=True)
    original = next(row for row in ledger.list_charges(tenancy) if row["replaces_charge_id"] == bills["rent"])
    cancel_historical_bill(ledger, original["id"], "Second mistake", "2024-01-03", prepare_replacement=True)
    restored_id = ledger.restore_cancelled_bill(original["id"], "Keep it for review", "2024-01-04")
    assert ledger.get_bill(restored_id)["status"] == "draft"
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="already been restored"):
        ledger.restore_cancelled_bill(original["id"], "Duplicate click", "2024-01-05")
    assert ledger.get_revision() == before
    assert ledger.get_bill(restored_id)["charge_date"] == "2024-01-04"


def test_restore_miscellaneous_creates_linked_bill_and_rejects_duplicate_restore(account):
    ledger, tenancy, _ = account
    bill = ledger.add_charge(tenancy, "misc", 5000, "2024-01-01", "2024-01-03", "2024-01-03", "Lock repair")
    ledger.void_charge(bill, "Accidental click", "2024-01-05")
    restored = ledger.restore_cancelled_bill(bill, "Repair was requested", "2024-01-06")
    assert ledger.get_bill(restored)["description"] == "Lock repair"
    assert ledger.get_bill(restored)["replaces_charge_id"] == bill
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(bill, "Duplicate click")
    assert ledger.get_revision() == before
    ledger.void_charge(restored, "Second cancellation", "2024-01-10")
    with pytest.raises(ValueError, match="later linked corrections"):
        ledger.restore_cancelled_bill(bill, "Too early", "2024-01-08")
    latest = ledger.restore_cancelled_bill(bill, "Restore after both corrections", "2024-01-11")
    assert ledger.get_bill(latest)["charge_date"] == "2024-01-11"
    assert ledger.balance(tenancy)["billed_paise"] == 5000


def test_restore_rejects_posted_replacement_and_keeps_every_record(account):
    ledger, tenancy, bills = account
    original = bills["rent"]
    ledger.post_charge(original)
    cancel_historical_bill(ledger, original, "Wrong amount", "2024-01-05", prepare_replacement=True)
    replacement = next(row for row in ledger.list_charges(tenancy) if row["replaces_charge_id"] == original)
    ledger.post_charge(replacement["id"], 80_000)
    before = ledger.list_charges(tenancy)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(original, "Restore old amount")
    assert ledger.list_charges(tenancy) == before
    assert ledger.get_revision() == revision


def test_restore_rejects_locked_and_archived_accounts(account):
    ledger, tenancy, bills = account
    original = bills["electricity"]
    cancel_historical_bill(ledger, original, "Mistake", "2024-01-05")
    ledger.set_cycle_payment_lock(tenancy, "2024-01-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.restore_cancelled_bill(original, "Restore")
    ledger.set_cycle_payment_lock(tenancy, "2024-01-01", False)
    ledger.move_out(tenancy, "2024-01-19", 0)
    for bill in ledger.list_charges(tenancy):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.archive_tenancy(tenancy)
    with pytest.raises(ValueError, match="archived"):
        ledger.restore_cancelled_bill(original, "Restore")


def test_restore_rejects_original_period_outside_revised_stay(account):
    ledger, tenancy, bills = account
    original = bills["rent"]
    ledger.post_charge(original)
    cancel_historical_bill(ledger, original, "Mistake", "2024-01-05")
    ledger.move_out(tenancy, "2024-01-19", 50_000)
    before = ledger.balance(tenancy)
    with pytest.raises(ValueError, match="recorded stay"):
        ledger.restore_cancelled_bill(original, "Restore the full month")
    assert ledger.balance(tenancy) == before


def test_future_cancellation_restore_preserves_schedule_and_rejects_earlier_explicit_date(account):
    ledger, tenancy, _ = account
    future = ledger.add_charge(tenancy, "misc", 5000, "2024-02-01", "2024-02-02", "2024-02-02", "Scheduled repair")
    ledger.void_charge(future, "Cancelled booking", "2024-02-03")
    with pytest.raises(ValueError, match="restoration date"):
        ledger.restore_cancelled_bill(future, "Too early", "2024-02-02")
    restored = ledger.restore_cancelled_bill(future, "Booking reinstated")
    assert ledger.get_bill(restored)["charge_date"] == "2024-02-03"
    assert ledger.balance(tenancy)["billed_paise"] == 0
    assert ledger.balance(tenancy, "2024-02-03")["billed_paise"] == 5000


def test_restore_rolls_back_draft_reuse_if_audit_fails(account, monkeypatch):
    ledger, tenancy, bills = account
    ledger.post_charge(bills["rent"])
    cancel_historical_bill(ledger, bills["rent"], "Mistake", "2024-01-05", prepare_replacement=True)
    before = ledger.list_charges(tenancy)
    revision = ledger.get_revision()
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(ledger, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        ledger.restore_cancelled_bill(bills["rent"], "Restore")
    assert ledger.list_charges(tenancy) == before
    assert ledger.get_revision() == revision


def test_restore_rejects_deleted_original_or_descendant(account):
    ledger, tenancy, _ = account
    bill = ledger.add_charge(tenancy, "misc", 5000, "2024-01-01", "2024-01-03", "2024-01-03", "Repair")
    ledger.void_charge(bill, "Mistake", "2024-01-05")
    restored = ledger.restore_cancelled_bill(bill, "Restore", "2024-01-06")
    ledger.delete_bill(restored, "No longer needed", "2024-01-07")
    with pytest.raises(ValueError, match="deleted"):
        ledger.restore_cancelled_bill(restored, "Restore")
    with pytest.raises(ValueError, match="deleted"):
        ledger.restore_cancelled_bill(bill, "Restore original")


def test_restore_records_reason_and_original_history_in_audit(account):
    ledger, _, bills = account
    original = bills["rent"]
    cancel_historical_bill(ledger, original, "Mistake", "2024-01-05")
    restored = ledger.restore_cancelled_bill(original, "Wrong bill selected", "2024-01-06")
    with ledger._read() as conn:
        record = conn.execute(select(m.audit).where(m.audit.c.action == "undo_cancellation")).mappings().one()
    assert record["entity_id"] == original
    assert '"restored_charge_id": ' + str(restored) in record["details"]
    assert "Wrong bill selected" in record["details"]
    assert "Mistake" in record["details"]


def test_legacy_electricity_restore_rejects_overlapping_monthly_bill(account):
    ledger, tenancy, bills = account
    cancel_historical_bill(ledger, bills["electricity"], "Replacing with meter bill", "2024-01-02")
    original = ledger.add_charge(tenancy, "electricity", 6000, "2024-01-01", "2024-02-15", "2024-02-15")
    cancel_historical_bill(ledger, original, "Mistake", "2024-01-05")
    ledger.add_charge(tenancy, "electricity", 3000, "2024-02-01", "2024-02-15", "2024-02-15")
    before = ledger.balance(tenancy)
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(original, "Restore meter bill")
    assert ledger.balance(tenancy) == before


@pytest.mark.parametrize("other_charge_date", ["2024-01-06", "2024-01-25"])
def test_restore_waits_for_unlinked_electricity_scheduled_cancellation(account, other_charge_date):
    ledger, tenancy, bills = account
    original = bills["electricity"]
    ledger.post_charge(original, 12_000)
    cancel_historical_bill(ledger, original, "Mistake", "2024-01-05")
    other = ledger.add_charge(
        tenancy, "electricity", 8000, "2024-01-01", "2024-01-31", "2024-01-31",
        charge_date=other_charge_date,
    )
    cancel_historical_bill(ledger, other, "Scheduled cancellation", "2024-01-30")
    with pytest.raises(ValueError, match="overlapping bill cancellations"):
        ledger.restore_cancelled_bill(original, "Do not overlap", "2024-01-20")
    before = {on: ledger.balance(tenancy, on) for on in ("2024-01-20", "2024-01-29")}
    restored = ledger.restore_cancelled_bill(original, "Restore after the other bill ends")
    assert ledger.get_bill(restored)["charge_date"] == "2024-01-30"
    assert {on: ledger.balance(tenancy, on) for on in before} == before
    assert ledger.balance(tenancy, "2024-01-30")["billed_paise"] == 12_000
