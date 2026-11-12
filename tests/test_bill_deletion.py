"""Miscellaneous bill deletion keeps money and dated history recoverable."""

from datetime import datetime
import json

import pytest
from sqlalchemy import select

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    bill = ledger.add_charge(tenant, "misc", 20_000, "2024-01-01", "2024-01-31",
                             "2024-01-31", "Incorrect cleaning fee", charge_date="2024-01-01")
    yield ledger, tenant, bill
    ledger.close()


def test_paid_bill_deletion_returns_balance_and_keeps_receipts_and_audit(account):
    ledger, tenant, bill = account
    receipt = ledger.deposit_payment(tenant, 30_000, "2024-01-02", "cash", reference="Receipt 1")
    ledger.mark_bill_paid(bill, "2024-01-10")
    before_payment = ledger.list_payments(tenant)[0]
    ledger.delete_bill(bill, "Fee added by mistake")
    assert ledger.balance(tenant)["credit_paise"] == 30_000
    assert ledger.balance(tenant, "2024-01-14")["credit_paise"] == 10_000
    assert ledger.balance(tenant, "2024-01-09")["outstanding_paise"] == 20_000
    assert bill not in {row["id"] for row in ledger.list_charges(tenant)}
    assert bill not in {row["id"] for cycle in ledger.list_bill_cycles(tenant) for row in cycle["charges"]}
    assert bill in {row["id"] for row in ledger.list_charges(tenant, include_deleted=True)}
    assert bill in {row["id"] for cycle in ledger.list_bill_cycles(tenant, include_deleted=True) for row in cycle["charges"]}
    saved = ledger.get_bill(bill)
    assert saved["deleted_on"] == saved["voided_on"] == "2024-01-15"
    assert saved["payments"][0]["cancelled_on"] == "2024-01-15"
    payment = ledger.list_payments(tenant)[0]
    for key in ("id", "amount_paise", "paid_on", "reference", "method", "reversed_on"):
        assert payment[key] == before_payment[key]
    assert payment["id"] == receipt
    with ledger._read() as conn:
        details = json.loads(conn.execute(select(m.audit.c.details).where(
            m.audit.c.action == "delete_bill", m.audit.c.entity_id == bill)).scalar_one())
    assert details["reason"] == "Fee added by mistake"
    assert details["returned_to_balance_paise"] == 20_000
    assert details["previous_bill"]["status"] == "posted"
    assert details["previous_bill"]["amount_paise"] == 20_000
    assert details["previous_bill"]["description"] == "Incorrect cleaning fee"


def test_cancelled_bill_deletion_keeps_original_cancellation(account):
    ledger, tenant, bill = account
    ledger.deposit_payment(tenant, 20_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bill, "2024-01-03")
    ledger.void_charge(bill, "Original cancellation", "2024-01-05")
    ledger.delete_bill(bill, "Remove cancelled fee")
    saved = ledger.get_bill(bill)
    assert saved["voided_on"] == "2024-01-05"
    assert saved["void_reason"] == "Original cancellation"
    assert saved["deleted_on"] == "2024-01-15"
    assert ledger.balance(tenant)["credit_paise"] == 20_000


def test_future_bill_can_be_deleted_before_it_becomes_due(account):
    ledger, tenant, _ = account
    future = ledger.add_charge(tenant, "misc", 7_000, "2024-02-01", "2024-02-29",
                               "2024-02-29", "Future fee", charge_date="2024-02-01")
    ledger.delete_bill(future, "Future fee entered in error")
    saved = ledger.get_bill(future)
    assert saved["deleted_on"] == "2024-01-15"
    assert saved["voided_on"] == "2024-02-01"
    assert ledger.balance(tenant)["outstanding_paise"] == 20_000
    assert ledger.balance(tenant, "2024-02-15")["outstanding_paise"] == 20_000
    assert all(row["id"] != future for row in ledger.list_charges(tenant))


def test_deletion_of_scheduled_cancellation_cannot_hide_current_debt(account):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Scheduled waiver", "2024-02-01")
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="scheduled cancellation"):
        ledger.delete_bill(bill, "Remove sooner")
    assert ledger.get_revision() == before
    assert ledger.balance(tenant)["outstanding_paise"] == 20_000
    assert ledger.get_bill(bill)["deleted_on"] is None
    assert bill in {row["id"] for row in ledger.list_charges(tenant)}


def test_deletion_cannot_skip_future_allocation_history(account, monkeypatch):
    ledger, tenant, bill = account
    ledger.deposit_payment(tenant, 20_000, "2024-01-02", "cash")
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 2, 2, 12, tzinfo=clock.IST))
    ledger.mark_bill_paid(bill, "2024-02-01")
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="last allocation"):
        ledger.delete_bill(bill, "Remove fee")
    assert ledger.get_revision() == before
    assert ledger.get_bill(bill)["deleted_on"] is None
    assert ledger.balance(tenant)["credit_paise"] == 20_000
    assert ledger.get_bill(bill, "2024-02-01")["paid_paise"] == 20_000


def test_deleted_bills_cannot_be_changed_or_deleted_twice(account):
    ledger, _, bill = account
    ledger.delete_bill(bill, "Entered by mistake")
    before = ledger.get_revision()
    for action in (
        lambda: ledger.delete_bill(bill, "Second deletion"),
        lambda: ledger.void_charge(bill, "Second cancellation"),
        lambda: ledger.post_charge(bill),
        lambda: ledger.mark_bill_paid(bill),
        lambda: ledger.mark_bill_unpaid(bill, "Change payment"),
    ):
        with pytest.raises(ValueError, match="deleted"):
            action()
    assert ledger.get_revision() == before


def test_required_bills_locked_cycles_and_archived_accounts_are_protected(account):
    ledger, tenant, bill = account
    for row in ledger.list_charges(tenant):
        if row["kind"] in ("rent", "electricity"):
            with pytest.raises(ValueError, match="cannot be deleted"):
                ledger.delete_bill(row["id"], "Remove required bill")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.delete_bill(bill, "Remove locked bill")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", False)
    ledger.void_charge(bill, "Cancelled fee", "2024-01-10")
    ledger.move_out(tenant, "2024-01-14", 0)
    for row in ledger.list_charges(tenant):
        if row["status"] == "draft":
            ledger.post_charge(row["id"], 0)
    ledger.archive_tenancy(tenant)
    with pytest.raises(ValueError, match="Reopen"):
        ledger.delete_bill(bill, "Remove archived bill")
    assert ledger.get_bill(bill)["deleted_on"] is None


def test_deletion_rolls_back_financial_changes_if_audit_fails(account, monkeypatch):
    ledger, tenant, bill = account
    ledger.deposit_payment(tenant, 20_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bill, "2024-01-03")
    before = ledger.get_revision()
    def fail(*args, **kwargs):
        raise RuntimeError("audit write failed")
    monkeypatch.setattr(ledger, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit write failed"):
        ledger.delete_bill(bill, "Remove fee")
    assert ledger.get_revision() == before
    assert ledger.get_bill(bill)["deleted_on"] is None
    assert ledger.get_bill(bill)["paid_paise"] == 20_000
    assert ledger.balance(tenant)["credit_paise"] == 0


def test_opening_setup_cannot_erase_deleted_draft_history(tmp_path):
    ledger = Ledger(tmp_path / "draft-only")
    try:
        building = ledger.add_building("House", "Address")
        flat = ledger.add_flat(building, "1", "Ground", 1)
        tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
        bill = ledger.add_charge(tenant, "misc", 10_000, "2024-01-01", "2024-01-31",
                                 "2024-01-31", "Draft fee", draft=True)
        ledger.delete_bill(bill, "Unneeded draft")
        before = ledger.get_revision()
        with pytest.raises(ValueError, match="before posting, deleting or restoring"):
            ledger.set_opening_balances(tenant, "2024-01-01")
        assert ledger.get_revision() == before
        assert ledger.get_bill(bill)["deleted_on"]
        assert bill in {row["id"] for row in ledger.list_charges(tenant, include_deleted=True)}
    finally:
        ledger.close()


@pytest.mark.parametrize("reason,effective", [("", None), (None, None), ("Reason", "2024-01-16")])
def test_invalid_deletion_request_keeps_bill(account, reason, effective):
    ledger, _, bill = account
    before = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.delete_bill(bill, reason, effective)
    assert ledger.get_revision() == before
    assert ledger.get_bill(bill)["deleted_on"] is None


@pytest.mark.parametrize("bill", [0, -1, True, "1", 999999])
def test_missing_or_invalid_bill_cannot_be_deleted(account, bill):
    ledger, _, _ = account
    with pytest.raises(ValueError):
        ledger.delete_bill(bill, "Remove fee")
