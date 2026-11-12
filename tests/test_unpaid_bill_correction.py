"""Unpaid miscellaneous corrections preserve receipts and dated bill history."""

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillAmountDialog, BillDialog


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 20, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    bill = ledger.add_charge(tenant, "misc", 5_000, "2024-01-03", "2024-01-05",
                             "2024-01-05", "Repair fee")
    yield ledger, tenant, bill
    ledger.close()


@pytest.mark.parametrize("amount", [0, 3_000, 12_000])
def test_unpaid_misc_correction_preserves_receipts_balance_and_original_bill(account, amount):
    ledger, tenant, bill = account
    ledger.deposit_payment(tenant, 15_000, "2024-01-02", "cash")
    original = ledger.get_bill(bill)
    receipts = ledger.list_payments(tenant)
    history = {day: ledger.balance(tenant, day) for day in ("2024-01-02", "2024-01-05", "2024-01-09")}

    revised = ledger.revise_bill_amount(bill, amount, "Correct repair estimate", "2024-01-10")

    changed = ledger.get_bill(revised)
    assert changed["replaces_charge_id"] == bill
    assert changed["amount_paise"] == changed["outstanding_paise"] == amount
    assert changed["paid_paise"] == 0
    assert changed["charge_date"] == "2024-01-10"
    for field in ("kind", "description", "period_start", "period_end", "due_date", "opening"):
        assert changed[field] == original[field]
    assert ledger.get_bill(bill)["status"] == "void"
    assert ledger.get_bill(bill)["amount_paise"] == 5_000
    assert ledger.list_payments(tenant) == receipts
    assert ledger.balance(tenant)["credit_paise"] == 15_000
    assert ledger.balance(tenant)["outstanding_paise"] == amount
    assert {day: ledger.balance(tenant, day) for day in history} == history
    with ledger._read() as conn:
        audit = conn.execute(select(m.audit.c.details).where(
            m.audit.c.action == "revise_bill_amount", m.audit.c.entity_id == bill,
        )).scalar_one()
    details = json.loads(audit)
    assert details["reason"] == "Correct repair estimate"
    assert details["returned_to_balance_paise"] == 0


@pytest.mark.parametrize("allocated", [2_000, 5_000])
def test_paid_or_legacy_partly_paid_misc_requires_mark_unpaid_first(account, allocated):
    ledger, tenant, bill = account
    payment = ledger.deposit_payment(tenant, 5_000, "2024-01-03", "cash")
    # Seed a legacy partial allocation; the public UI now permits only full bills.
    with ledger._write() as conn:
        ledger._allocate(conn, ledger._get(conn, m.payments, payment),
                         ledger._get(conn, m.charges, bill), allocated, "2024-01-04")
    history = ledger.balance(tenant, "2024-01-05")
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="Mark this miscellaneous bill Unpaid"):
        ledger.revise_bill_amount(bill, 3_000, "Mistyped fee", "2024-01-10")
    assert ledger.get_revision() == revision
    assert ledger.get_bill(bill)["paid_paise"] == allocated

    ledger.mark_bill_unpaid(bill, "Review fee before paying again", "2024-01-10")
    with pytest.raises(ValueError):
        ledger.revise_bill_amount(bill, 3_000, "Cannot predate the release", "2024-01-09")
    revised = ledger.revise_bill_amount(bill, 3_000, "Correct the unpaid fee", "2024-01-10")
    assert ledger.get_bill(revised)["paid_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 5_000
    assert ledger.balance(tenant, "2024-01-05") == history


def test_same_amount_is_noop_and_later_correction_keeps_one_active_fee(account):
    ledger, tenant, bill = account
    revision = ledger.get_revision()
    assert ledger.revise_bill_amount(bill, 5_000, "Already correct") == bill
    assert ledger.get_revision() == revision
    first = ledger.revise_bill_amount(bill, 4_000, "First correction", "2024-01-10")
    second = ledger.revise_bill_amount(first, 3_000, "Final correction", "2024-01-11")
    assert ledger.get_bill(second)["replaces_charge_id"] == first
    assert ledger.balance(tenant)["outstanding_paise"] == 3_000
    assert [row["id"] for row in ledger.list_charges(tenant)
            if row["kind"] == "misc" and row["status"] == "posted"] == [second]
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(bill, "Do not duplicate the original fee")


def test_correction_keeps_future_bill_schedule_and_does_not_create_current_debt(account):
    ledger, tenant, _ = account
    future = ledger.add_charge(tenant, "misc", 5_000, "2024-02-01", "2024-02-01",
                               "2024-02-01", "Upcoming repair")
    before = ledger.balance(tenant)
    changed = ledger.revise_bill_amount(future, 3_000, "Correct upcoming fee")
    assert ledger.get_bill(changed)["charge_date"] == "2024-02-01"
    assert ledger.balance(tenant) == before
    assert ledger.balance(tenant, "2024-02-01")["outstanding_paise"] == 8_000


def test_failed_final_audit_rolls_back_both_bill_records(account, monkeypatch):
    ledger, tenant, bill = account
    original = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    audit = ledger._audit

    def fail(conn, action, *args, **kwargs):
        if action == "revise_bill_amount":
            raise RuntimeError("Simulated audit failure")
        return audit(conn, action, *args, **kwargs)

    monkeypatch.setattr(ledger, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        ledger.revise_bill_amount(bill, 3_000, "Must roll back")
    assert ledger.list_charges(tenant) == original
    assert ledger.get_revision() == revision


def test_locked_misc_amount_cannot_be_changed(account):
    ledger, tenant, bill = account
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="locked"):
        ledger.revise_bill_amount(bill, 3_000, "Unlock first")
    assert ledger.get_bill(bill)["amount_paise"] == 5_000
    assert ledger.get_revision() == revision


@pytest.mark.parametrize("archived,locked,paid", [(False, False, 0), (True, False, 0),
                                                (False, True, 0), (False, False, 5_000)])
def test_misc_bill_dialog_only_enables_correction_without_applied_money(account, qtbot, archived, locked, paid):
    ledger, _, bill = account
    charge = dict(ledger.get_bill(bill), payment_locked=locked, paid_paise=paid,
                  outstanding_paise=5_000 - paid)
    calls = []
    dialog = BillDialog(charge, lambda: None, lambda: None, archived=archived,
                        correct_amount=lambda: calls.append("correct"))
    qtbot.addWidget(dialog)
    enabled = not archived and not locked and not paid
    assert dialog.correct_button.isEnabled() == enabled
    dialog.correct_button.click()
    assert calls == (["correct"] if enabled else [])


def test_misc_amount_dialog_prefills_amount_and_validates_zero_and_reason(account, qtbot):
    ledger, _, bill = account
    dialog = BillAmountDialog(ledger.get_bill(bill))
    qtbot.addWidget(dialog)
    assert dialog.amount.paise() == 5_000
    dialog.amount.set_paise(0)
    with pytest.raises(ValueError, match="Reason"):
        dialog.validate()
    dialog.reason.setText("No repair charge due")
    assert dialog.validate() == {"amount_paise": 0, "reason": "No repair charge due"}


@pytest.mark.parametrize("kind", ["rent", "electricity"])
@pytest.mark.parametrize("operation", ["revise", "restore", "replace"])
def test_cancelled_upcoming_account_keeps_opening_dues_correctable(account, kind, operation):
    from legacy_ledger import cancel_historical_bill

    ledger, _, _ = account
    building = ledger.add_building("Other house", "Other address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Upcoming tenant", "2024-02-01", 1, 100_000)
    opening = ledger.add_charge(tenant, kind, 5_000, "2024-01-01", "2024-01-31",
                                "2024-01-31", "Opening dues", charge_date="2024-02-01", opening=True)
    if operation != "revise":
        cancel_historical_bill(ledger, opening, "Earlier correction", "2024-02-02")
    ledger.cancel_upcoming_tenancy(tenant, "Booking cancelled")

    if operation == "revise":
        changed = ledger.revise_bill_amount(opening, 3_000, "Correct retained opening dues")
    elif operation == "restore":
        changed = ledger.restore_cancelled_bill(opening, "Restore retained opening dues")
    else:
        changed, = ledger.replace_cancelled_bill(opening, [dict(
            amount_paise=3_000, period_start="2024-01-01", period_end="2024-01-31",
            due_date="2024-01-31", description="Correct retained opening dues",
        )])
    bill = ledger.get_bill(changed)
    assert bill["opening"] and bill["kind"] == kind
    assert bill["replaces_charge_id"] == opening
    assert ledger.get_tenancy(tenant)["cancelled_on"] == "2024-01-20"
    ledger.prepare_monthly_drafts("2024-03-01")
    assert all(row["opening"] for row in ledger.list_charges(tenant))
