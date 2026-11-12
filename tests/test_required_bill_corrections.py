"""Required monthly bills stay present and corrections preserve dated money."""

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger
from grihalekha.money import MAX_PAISE


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


@pytest.mark.parametrize("kind", ["rent", "electricity"])
@pytest.mark.parametrize("posted", [False, True])
@pytest.mark.parametrize("replacement", [False, True])
def test_required_bills_cannot_be_cancelled_or_deleted(account, kind, posted, replacement):
    ledger, tenancy, bills = account
    bill = bills[kind]
    if posted:
        ledger.post_charge(bill, 0)
    before = ledger.list_charges(tenancy)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="cannot be cancelled"):
        ledger.void_charge(bill, "Waive it", prepare_replacement=replacement)
    with pytest.raises(ValueError, match="cannot be deleted"):
        ledger.delete_bill(bill, "Waive it")
    assert ledger.list_charges(tenancy) == before
    assert ledger.get_revision() == revision


@pytest.mark.parametrize("kind", ["rent", "electricity"])
@pytest.mark.parametrize("revised_amount", [0, 60_000, 120_000])
def test_paid_amount_correction_returns_full_balance_and_preserves_history(account, kind, revised_amount):
    ledger, tenancy, bills = account
    bill = bills[kind]
    ledger.post_charge(bill, 100_000)
    ledger.deposit_payment(tenancy, 40_000, "2024-01-03", "cash")
    ledger.deposit_payment(tenancy, 60_000, "2024-01-05", "upi")
    ledger.mark_bill_paid(bill, "2024-01-05")
    original = ledger.get_bill(bill)
    history = {on: ledger.balance(tenancy, on) for on in ("2024-01-02", "2024-01-04", "2024-01-09")}
    revised_id = ledger.revise_bill_amount(bill, revised_amount, "Correct meter/agreed amount", "2024-01-10")
    revised = ledger.get_bill(revised_id)
    assert revised_id != bill
    assert revised["amount_paise"] == revised["outstanding_paise"] == revised_amount
    assert revised["paid_paise"] == 0
    assert revised["status"] == "posted"
    assert revised["charge_date"] == "2024-01-10"
    assert revised["replaces_charge_id"] == bill
    for field in ("period_start", "period_end", "due_date", "description", "partial", "opening"):
        assert revised[field] == original[field]
    assert ledger.get_bill(bill)["status"] == "void"
    assert ledger.get_bill(bill)["amount_paise"] == 100_000
    assert ledger.balance(tenancy)["credit_paise"] == 100_000
    assert ledger.balance(tenancy)["billed_paise"] == revised_amount
    assert len(ledger.list_payments(tenancy)) == 2
    assert all(not row["reversed_on"] for row in ledger.list_payments(tenancy))
    assert {on: ledger.balance(tenancy, on) for on in history} == history
    assert ledger.prepare_monthly_drafts("2024-01-20") == 0
    assert len([row for row in ledger.list_charges(tenancy) if row["kind"] == kind and row["status"] != "void"]) == 1
    if revised_amount <= 100_000:
        assert ledger.mark_bill_paid(revised_id) == revised_amount
        assert ledger.balance(tenancy)["credit_paise"] == 100_000 - revised_amount
    else:
        with pytest.raises(ValueError, match="full bill"):
            ledger.mark_bill_paid(revised_id)
        assert ledger.get_bill(revised_id)["paid_paise"] == 0
    with ledger._read() as conn:
        record = conn.execute(select(m.audit).where(m.audit.c.action == "revise_bill_amount")).mappings().one()
    details = json.loads(record["details"])
    assert details["revised_charge_id"] == revised_id
    assert details["returned_to_balance_paise"] == 100_000
    assert details["original"]["amount_paise"] == 100_000


@pytest.mark.parametrize("kind", ["rent", "electricity"])
def test_same_amount_is_noop_and_repeated_revisions_link_to_latest_bill(account, kind):
    ledger, tenancy, bills = account
    bill = bills[kind]
    ledger.post_charge(bill, 100_000)
    ledger.deposit_payment(tenancy, 100_000, "2024-01-03", "cash")
    ledger.mark_bill_paid(bill, "2024-01-03")
    before = ledger.get_bill(bill)
    revision = ledger.get_revision()
    assert ledger.revise_bill_amount(bill, 100_000, "Amount already correct") == bill
    assert ledger.get_bill(bill) == before
    assert ledger.get_revision() == revision
    first = ledger.revise_bill_amount(bill, 90_000, "First correction", "2024-01-10")
    second = ledger.revise_bill_amount(first, 80_000, "Second correction", "2024-01-11")
    assert ledger.get_bill(second)["replaces_charge_id"] == first
    assert ledger.get_bill(first)["replaces_charge_id"] == bill
    assert ledger.balance(tenancy)["credit_paise"] == 100_000
    assert ledger.balance(tenancy)["outstanding_paise"] == 80_000
    assert ledger.prepare_monthly_drafts("2024-01-20") == 0
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(bill, "Cannot duplicate a corrected bill")
    if kind == "electricity":
        assert ledger.get_bill(second)["electricity_cycle_start"] == "2024-01-01"
        assert ledger.get_bill(first)["electricity_cycle_start"] is None
        assert ledger.get_bill(bill)["electricity_cycle_start"] is None


def test_zero_rent_and_electricity_are_complete_reviewed_paid_cycle(account):
    ledger, tenancy, bills = account
    for bill in bills.values():
        ledger.post_charge(bill, 10_000)
        ledger.revise_bill_amount(bill, 0, "No charge by agreement", "2024-01-05")
    snapshot = ledger.statement(tenancy, "2024-01-01", "2024-01-31")
    cycle, = snapshot["billing_cycles"]
    assert cycle["status"] == "paid"
    assert cycle["rent_paise"] == cycle["electricity_paise"] == 0
    assert not cycle["review_pending"]
    assert ledger.list_payments(tenancy) == []


@pytest.mark.parametrize("amount", [-1, True, 1.25, None, "100", MAX_PAISE + 1])
def test_invalid_amount_rejected_without_changes(account, amount):
    ledger, tenancy, bills = account
    ledger.post_charge(bills["rent"])
    before = ledger.list_charges(tenancy)
    revision = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.revise_bill_amount(bills["rent"], amount, "Invalid amount")
    assert ledger.list_charges(tenancy) == before
    assert ledger.get_revision() == revision


@pytest.mark.parametrize("reason", ["", "  ", None, 4, "x" * 10_001])
def test_amount_correction_requires_reason(account, reason):
    ledger, _, bills = account
    ledger.post_charge(bills["rent"])
    with pytest.raises(ValueError, match="reason"):
        ledger.revise_bill_amount(bills["rent"], 0, reason)


def test_draft_cancelled_deleted_locked_and_archived_bills_rejected(account):
    ledger, tenancy, bills = account
    with pytest.raises(ValueError, match="posted bill"):
        ledger.revise_bill_amount(bills["electricity"], 0, "Review draft first")
    misc = ledger.add_charge(tenancy, "misc", 100, "2024-01-01", "2024-01-01", "2024-01-01", "Repair")
    corrected_misc = ledger.revise_bill_amount(misc, 0, "Correct the unpaid miscellaneous bill")
    assert ledger.get_bill(corrected_misc)["amount_paise"] == 0
    ledger.delete_bill(misc, "Not needed")
    with pytest.raises(ValueError, match="deleted"):
        ledger.revise_bill_amount(misc, 0, "Do not restore deleted bill")
    ledger.post_charge(bills["rent"])
    ledger.set_cycle_payment_lock(tenancy, "2024-01-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.revise_bill_amount(bills["rent"], 0, "Unlock first")
    ledger.set_cycle_payment_lock(tenancy, "2024-01-01", False)
    revised = ledger.revise_bill_amount(bills["rent"], 0, "Waive agreed amount")
    with pytest.raises(ValueError, match="posted bill"):
        ledger.revise_bill_amount(bills["rent"], 0, "Already revised")
    ledger.post_charge(bills["electricity"], 0)
    ledger.move_out(tenancy, "2024-01-19", 0)
    for bill in ledger.list_charges(tenancy):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.archive_tenancy(tenancy)
    with pytest.raises(ValueError, match="archived"):
        ledger.revise_bill_amount(revised, 50, "Reopen first")


def test_correction_cannot_precede_payment_or_payment_release_history(account):
    ledger, tenancy, bills = account
    bill = bills["rent"]
    ledger.post_charge(bill)
    ledger.deposit_payment(tenancy, 100_000, "2024-01-05", "cash")
    ledger.mark_bill_paid(bill, "2024-01-05")
    before = ledger.get_revision()
    for effective in ("2023-12-31", "2024-01-04"):
        with pytest.raises(ValueError, match="charge date|allocation"):
            ledger.revise_bill_amount(bill, 0, "Too early", effective)
    assert ledger.get_revision() == before
    ledger.mark_bill_unpaid(bill, "Return to balance", "2024-01-10")
    with pytest.raises(ValueError, match="allocation correction"):
        ledger.revise_bill_amount(bill, 0, "Cannot rewrite release", "2024-01-09")
    revised = ledger.revise_bill_amount(bill, 0, "Valid correction", "2024-01-10")
    assert ledger.get_bill(revised)["amount_paise"] == 0
    assert ledger.balance(tenancy)["credit_paise"] == 100_000


@pytest.mark.parametrize("failure", ["_insert_charge", "_audit"])
def test_correction_failure_rolls_back_bill_balance_anchor_and_audit(account, monkeypatch, failure):
    ledger, tenancy, bills = account
    bill = bills["electricity"]
    ledger.post_charge(bill, 20_000)
    ledger.deposit_payment(tenancy, 20_000, "2024-01-05", "cash")
    ledger.mark_bill_paid(bill, "2024-01-05")
    charges = ledger.list_charges(tenancy)
    balance = ledger.balance(tenancy)
    payments = ledger.list_payments(tenancy)
    revision = ledger.get_revision()
    original_audit = ledger._audit
    def fail(*args, **kwargs):
        if failure == "_audit" and args[1] != "revise_bill_amount":
            return original_audit(*args, **kwargs)
        raise RuntimeError("simulated correction failure")
    monkeypatch.setattr(ledger, failure, fail)
    with pytest.raises(RuntimeError, match="correction failure"):
        ledger.revise_bill_amount(bill, 0, "Must roll back")
    assert ledger.list_charges(tenancy) == charges
    assert ledger.balance(tenancy) == balance
    assert ledger.list_payments(tenancy) == payments
    assert ledger.get_revision() == revision


def test_opening_required_amount_can_be_corrected_without_changing_its_service_period(account):
    ledger, tenancy, _ = account
    bill = ledger.add_charge(tenancy, "rent", 25_000, "2023-12-01", "2023-12-31", "2023-12-31",
                             charge_date="2024-01-01", opening=True)
    revised = ledger.revise_bill_amount(bill, 0, "Opening balance correction")
    assert ledger.get_bill(revised)["opening"]
    assert ledger.get_bill(revised)["period_start"] == "2023-12-01"
    with pytest.raises(ValueError, match="cannot be cancelled"):
        ledger.void_charge(revised, "Still required")


@pytest.mark.parametrize("kind", ["rent", "electricity"])
def test_future_bill_amount_correction_preserves_its_schedule(account, kind):
    ledger, tenancy, _ = account
    ledger.prepare_monthly_drafts("2024-02-01")
    future = next(row for row in ledger.list_charges(tenancy)
                  if row["kind"] == kind and row["period_start"] == "2024-02-01")
    ledger.post_charge(future["id"], 10_000)
    before = ledger.balance(tenancy)
    revised_id = ledger.revise_bill_amount(future["id"], 6_000, "Correct next month's agreed amount")
    revised = ledger.get_bill(revised_id)
    assert revised["charge_date"] == "2024-02-01"
    assert revised["period_start"] == future["period_start"]
    assert revised["period_end"] == future["period_end"]
    assert ledger.balance(tenancy) == before
    assert ledger.balance(tenancy, "2024-02-01")["outstanding_paise"] == 6_000
    assert ledger.prepare_monthly_drafts("2024-02-01") == 0
