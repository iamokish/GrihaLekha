"""Reservation cancellation must not erase receipts or recreate occupancy."""

from datetime import datetime
import json

import pytest
from sqlalchemy import select

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 5, 20, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "data")
    building = ledger.add_building("House", "Example address")
    first = ledger.add_flat(building, "1", "Ground", 1)
    second = ledger.add_flat(building, "2", "First", 1)
    tenant = ledger.add_tenancy(first, "Planned tenant", "2024-06-01", 1, 100_000)
    yield ledger, tenant, first, second
    ledger.close()


def _raw_bills(ledger, tenant):
    with ledger._read() as conn:
        return [dict(row) for row in conn.execute(
            select(m.charges).where(m.charges.c.tenancy_id == tenant).order_by(m.charges.c.id)
        ).mappings()]


def _post_first_month(ledger, tenant):
    ledger.prepare_monthly_drafts("2024-06-01")
    bills = {row["kind"]: row["id"] for row in _raw_bills(ledger, tenant)}
    for kind, charge in bills.items():
        ledger.post_charge(charge, 100_000 if kind == "rent" else 10_000)
    return bills


def _legacy_payment(ledger, tenant, amount, paid_on, allocations=None):
    """Seed future-dated receipts retained from prior app versions."""
    with ledger._write() as conn:
        payment_id = ledger._record_payment(conn, tenant, amount, paid_on, "bank",
                                             reference="Original receipt", requested={})
        payment = ledger._get(conn, m.payments, payment_id)
        for charge_id, allocated in (allocations or {}).items():
            charge = ledger._get(conn, m.charges, charge_id)
            ledger._allocate(conn, payment, charge, allocated, paid_on)
        return payment_id


def test_cancellation_releases_flat_and_keeps_visible_history(account):
    ledger, tenant, first, _ = account
    original = ledger.get_tenancy(tenant)
    revision = ledger.get_revision()
    preview = ledger.preview_cancel_upcoming_tenancy(tenant)
    assert ledger.get_revision() == revision
    assert preview["released_flat_ids"] == [first]
    assert ledger.cancel_upcoming_tenancy(tenant, "  Booking withdrawn  ") == preview
    current = ledger.get_tenancy(tenant)
    assert current["cancelled_on"] == "2024-05-20"
    assert current["cancellation_reason"] == "Booking withdrawn"
    assert current["move_out"] is None
    assert current["occupancy_history"] == original["occupancy_history"]
    assert current["rent_rates"] == original["rent_rates"]
    assert tenant in [row["id"] for row in ledger.list_tenancies()]
    replacement = ledger.add_tenancy(first, "New booking", "2024-06-01", 1, 90_000)
    assert replacement != tenant
    assert ledger.dashboard("2024-06-01")["occupied_flats"] == 1
    with ledger._read() as conn:
        details = conn.execute(select(m.audit.c.details).where(
            m.audit.c.action == "cancel_upcoming_tenancy", m.audit.c.entity_id == tenant,
        )).scalar_one()
    assert json.loads(details)["cancelled_reservations"] == original["occupancy_history"]


def test_cancellation_stops_drafts_permanently_and_keeps_other_tenants(account, monkeypatch):
    ledger, tenant, _, second = account
    ledger.prepare_monthly_drafts("2024-08-01")
    original_ids = [row["id"] for row in _raw_bills(ledger, tenant)]
    assert len(original_ids) == 6
    ledger.cancel_upcoming_tenancy(tenant, "No longer moving")
    other = ledger.add_tenancy(second, "Other booking", "2024-06-01", 1, 70_000)
    assert ledger.prepare_monthly_drafts("2024-08-01") == 6
    assert ledger.prepare_rent_drafts("2025-12-01") == 16
    assert [row["id"] for row in _raw_bills(ledger, tenant)] == original_ids
    assert all(row["status"] == "void" for row in _raw_bills(ledger, tenant))
    assert ledger.list_bill_cycles(tenant) == []
    assert len(ledger.list_bill_cycles(tenant, include_stopped=True)) == 3
    assert ledger.statement(tenant, "2024-06-01", "2025-12-31")["billing_cycles"] == []
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 7, 1, 12, tzinfo=clock.IST))
    assert {row["id"]: row["tenancy_id"] for row in ledger.list_flats()} == {
        account[2]: None, second: other,
    }
    assert ledger.dashboard()["occupied_flats"] == 1


def test_cancellation_voids_required_bills_but_preserves_fees_receipts_and_deposit(account):
    ledger, tenant, _, _ = account
    bills = _post_first_month(ledger, tenant)
    fee = ledger.add_charge(tenant, "misc", 2_000, "2024-06-01", "2024-06-01",
                            "2024-06-01", "Agreed booking fee")
    receipt = _legacy_payment(ledger, tenant, 120_000, "2024-06-01", {bills["rent"]: 100_000})
    deposit = ledger.record_deposit(tenant, 50_000, "2024-06-01", "receive")
    current = ledger.balance(tenant)
    preview = ledger.preview_cancel_upcoming_tenancy(tenant)
    assert preview["returned_paise"] == 100_000
    assert preview["remaining_bill_ids"] == [fee]
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled booking")
    assert ledger.balance(tenant) == current
    future = ledger.balance(tenant, "2024-06-01")
    assert future["outstanding_paise"] == 2_000
    assert future["credit_paise"] == 120_000
    assert future["deposit_paise"] == 50_000
    with ledger._read() as conn:
        assert ledger._get(conn, m.payments, receipt)["reversed_on"] is None
        assert ledger._get(conn, m.deposits, deposit)["reversed_on"] is None
    assert ledger.get_bill(fee, "2024-06-01")["status"] == "posted"
    ledger.refund_credit(tenant, 120_000, "2024-06-01", "bank", "Booking refund")
    ledger.record_deposit(tenant, 50_000, "2024-06-01", "return", notes="Deposit returned")
    assert ledger.balance(tenant, "2024-06-01")["credit_paise"] == 0
    assert ledger.balance(tenant, "2024-06-01")["deposit_paise"] == 0


def test_cancellation_keeps_opening_dues_and_existing_issued_document(account):
    ledger, tenant, _, _ = account
    ledger.set_opening_balances(tenant, "2024-06-01", rent_paise=4_500)
    original = _raw_bills(ledger, tenant)
    document = ledger.data_dir / "documents" / "issued.pdf"
    document.parent.mkdir()
    document.write_bytes(b"An already-issued document")
    ledger.cancel_upcoming_tenancy(tenant, "Do not move in")
    assert _raw_bills(ledger, tenant) == original
    assert ledger.balance(tenant, "2024-06-01")["outstanding_paise"] == 4_500
    assert document.read_bytes() == b"An already-issued document"


def test_locked_cycle_prevents_partial_cancellation(account):
    ledger, tenant, _, _ = account
    ledger.prepare_monthly_drafts("2024-07-01")
    ledger.set_cycle_payment_lock(tenant, "2024-07-01", True)
    previous = ledger.get_tenancy(tenant), _raw_bills(ledger, tenant), ledger.get_revision()
    assert ledger.preview_cancel_upcoming_tenancy(tenant)["locked_cycle_starts"] == ["2024-07-01"]
    with pytest.raises(ValueError, match="Unlock"):
        ledger.cancel_upcoming_tenancy(tenant, "Cancelled booking")
    assert (ledger.get_tenancy(tenant), _raw_bills(ledger, tenant), ledger.get_revision()) == previous
    ledger.set_cycle_payment_lock(tenant, "2024-07-01", False)
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled booking")
    assert all(row["status"] == "void" for row in _raw_bills(ledger, tenant))


@pytest.mark.parametrize("reason", [None, "", " \t ", 3, "x" * 10_001])
def test_cancellation_requires_reason_without_changes(account, reason):
    ledger, tenant, _, _ = account
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="reason"):
        ledger.cancel_upcoming_tenancy(tenant, reason)
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["cancelled_on"] is None


@pytest.mark.parametrize("day", [1, 2])
def test_started_tenancy_cannot_be_cancelled(account, monkeypatch, day):
    ledger, tenant, _, _ = account
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 6, day, 12, tzinfo=clock.IST))
    with pytest.raises(ValueError, match="after today"):
        ledger.cancel_upcoming_tenancy(tenant, "Has already started")
    assert ledger.get_tenancy(tenant)["cancelled_on"] is None


def test_repeated_cancellation_does_not_change_original_reason(account):
    ledger, tenant, _, _ = account
    ledger.cancel_upcoming_tenancy(tenant, "Original reason")
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="cancelled"):
        ledger.cancel_upcoming_tenancy(tenant, "Different reason")
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["cancellation_reason"] == "Original reason"


def test_planned_exchange_must_be_cancelled_first(account):
    ledger, tenant, first, second = account
    other = ledger.add_tenancy(second, "Other booking", "2024-06-01", 1, 80_000)
    ledger.exchange_tenancies(tenant, other, "2024-07-01")
    before = ledger.get_tenancy(tenant), ledger.get_tenancy(other)
    with pytest.raises(ValueError, match="exchange"):
        ledger.cancel_upcoming_tenancy(tenant, "Booking cancelled")
    assert (ledger.get_tenancy(tenant), ledger.get_tenancy(other)) == before
    ledger.cancel_scheduled_transfer(tenant)
    ledger.cancel_upcoming_tenancy(tenant, "Booking cancelled")
    ledger.add_tenancy(first, "Replacement", "2024-06-01", 1, 75_000)
    assert ledger.get_tenancy(other)["cancelled_on"] is None


def test_unpaired_future_moves_release_all_reserved_flats(account):
    ledger, tenant, first, second = account
    ledger.transfer_tenancy(tenant, second, "2024-07-01", rent_paise=120_000)
    before = ledger.get_tenancy(tenant)
    result = ledger.cancel_upcoming_tenancy(tenant, "All plans withdrawn")
    assert result["released_flat_ids"] == [first, second]
    assert ledger.get_tenancy(tenant)["occupancy_history"] == before["occupancy_history"]
    ledger.add_tenancy(first, "New first tenant", "2024-06-01", 1, 0)
    ledger.add_tenancy(second, "New second tenant", "2024-06-01", 1, 0)


def test_future_allocation_conflict_is_rejected_atomically(account):
    ledger, tenant, _, _ = account
    bills = _post_first_month(ledger, tenant)
    _legacy_payment(ledger, tenant, 100_000, "2024-06-15", {bills["rent"]: 100_000})
    previous = _raw_bills(ledger, tenant), ledger.get_revision()
    with pytest.raises(ValueError, match="scheduled payment allocation"):
        ledger.cancel_upcoming_tenancy(tenant, "Cancel booking")
    assert (_raw_bills(ledger, tenant), ledger.get_revision()) == previous
    assert ledger.get_tenancy(tenant)["cancelled_on"] is None


def test_legacy_scheduled_void_is_advanced_without_future_debt(account):
    ledger, tenant, _, _ = account
    bills = _post_first_month(ledger, tenant)
    _legacy_payment(ledger, tenant, 100_000, "2024-06-01", {bills["rent"]: 100_000})
    with ledger._write() as conn:
        ledger._void_charge(conn, bills["rent"], "Legacy future correction", "2024-06-20")
    assert ledger.balance(tenant, "2024-06-10")["credit_paise"] == 0
    assert ledger.preview_cancel_upcoming_tenancy(tenant)["future_credit_paise"] == 100_000
    ledger.cancel_upcoming_tenancy(tenant, "Booking withdrawn")
    assert ledger.balance(tenant, "2024-06-01")["credit_paise"] == 100_000
    assert ledger.balance(tenant, "2024-06-01")["outstanding_paise"] == 0
    with ledger._read() as conn:
        assert ledger._get(conn, m.charges, bills["rent"])["voided_on"] == "2024-06-01"


def test_cancellation_rolls_back_all_bills_when_audit_fails(account, monkeypatch):
    ledger, tenant, _, _ = account
    _post_first_month(ledger, tenant)
    before = ledger.get_tenancy(tenant), _raw_bills(ledger, tenant), ledger.get_revision()
    original = ledger._audit

    def fail(conn, action, *args, **kwargs):
        if action == "cancel_upcoming_tenancy":
            raise RuntimeError("Simulated storage failure")
        return original(conn, action, *args, **kwargs)

    monkeypatch.setattr(ledger, "_audit", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        ledger.cancel_upcoming_tenancy(tenant, "Cancel booking")
    assert (ledger.get_tenancy(tenant), _raw_bills(ledger, tenant), ledger.get_revision()) == before


def test_cancelled_account_can_archive_then_reopen_without_reserving_flat(account):
    ledger, tenant, first, _ = account
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled")
    ledger.archive_tenancy(tenant)
    assert ledger.get_tenancy(tenant)["archived_on"] == "2024-05-20"
    replacement = ledger.add_tenancy(first, "Replacement", "2024-06-01", 1, 0)
    ledger.unarchive_tenancy(tenant)
    assert ledger.get_tenancy(tenant)["cancelled_on"] == "2024-05-20"
    assert ledger.get_tenancy(tenant)["archived_on"] is None
    assert ledger.dashboard("2024-06-01")["occupied_flats"] == 1
    assert ledger.get_tenancy(replacement)["cancelled_on"] is None


def test_cancelled_account_with_future_money_cannot_archive(account):
    ledger, tenant, _, _ = account
    _legacy_payment(ledger, tenant, 100, "2024-06-01")
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled")
    with pytest.raises(ValueError, match="Settle"):
        ledger.archive_tenancy(tenant)
    ledger.refund_credit(tenant, 100, "2024-06-01", "cash", "Full refund")
    ledger.archive_tenancy(tenant)


def test_cancelled_tenancy_cannot_regain_occupancy_or_required_bills(account):
    ledger, tenant, _, second = account
    bills = _post_first_month(ledger, tenant)
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled")
    operations = [
        lambda: ledger.transfer_tenancy(tenant, second, "2024-07-01"),
        lambda: ledger.move_out(tenant, "2024-06-30", 0),
        lambda: ledger.change_rent(tenant, "2024-07-01", 200_000),
        lambda: ledger.restore_cancelled_bill(bills["rent"], "Restore", "2024-06-01"),
        lambda: ledger.add_charge(tenant, "electricity", 500, "2024-06-01", "2024-06-30", "2024-06-30"),
    ]
    revision = ledger.get_revision()
    for operation in operations:
        with pytest.raises(ValueError, match="cancelled"):
            operation()
    assert ledger.get_revision() == revision


def test_cancelled_tenancy_never_reports_fake_rent_review_for_retained_fee(account):
    ledger, tenant, _, _ = account
    ledger.prepare_monthly_drafts("2024-06-01")
    fee = ledger.add_charge(tenant, "misc", 2_000, "2024-06-01", "2024-06-01",
                            "2024-06-01", "Agreed booking fee")
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled")
    snapshot = ledger.statement(tenant, "2024-06-01", "2024-06-30", as_of="2024-06-30")
    assert len(snapshot["billing_cycles"]) == 1
    cycle = snapshot["billing_cycles"][0]
    assert cycle["charge_ids"] == [fee]
    assert cycle["review_pending_kinds"] == []
    assert cycle["locations"] == []
    assert cycle["billed_paise"] == 2_000


def test_stale_cancellation_preview_rejects_new_bill_and_requires_review(account):
    ledger, tenant, _, _ = account
    preview = ledger.preview_cancel_upcoming_tenancy(tenant)
    ledger.add_charge(tenant, "misc", 2_000, "2024-06-01", "2024-06-01",
                      "2024-06-01", "Agreed booking fee")
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="changed after"):
        ledger.cancel_upcoming_tenancy(tenant, "Cancel booking", expected_revision=preview["revision"])
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["cancelled_on"] is None
    fresh = ledger.preview_cancel_upcoming_tenancy(tenant)
    assert len(fresh["remaining_bill_ids"]) == 1
    assert ledger.cancel_upcoming_tenancy(tenant, "Cancel booking", expected_revision=fresh["revision"]) == fresh


def test_stale_cancellation_preview_rejects_concurrent_receipt(account):
    ledger, tenant, _, second = account
    current = ledger.add_tenancy(second, "Current tenant", "2024-05-01", 1, 10_000)
    preview = ledger.preview_cancel_upcoming_tenancy(tenant)
    other_connection = Ledger(ledger.data_dir)
    try:
        other_connection.deposit_payment(current, 500, "2024-05-20", "cash")
    finally:
        other_connection.close()
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="changed after"):
        ledger.cancel_upcoming_tenancy(tenant, "Cancel booking", expected_revision=preview["revision"])
    assert ledger.get_revision() == revision
    assert ledger.get_tenancy(tenant)["cancelled_on"] is None
    fresh = ledger.preview_cancel_upcoming_tenancy(tenant)
    ledger.cancel_upcoming_tenancy(tenant, "Cancel booking", expected_revision=fresh["revision"])
    assert ledger.balance(current)["credit_paise"] == 500


def test_cancelled_account_rejects_new_security_deposit_receipt(account):
    ledger, tenant, _, _ = account
    ledger.cancel_upcoming_tenancy(tenant, "Cancelled booking")
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="cancelled"):
        ledger.record_deposit(tenant, 100, "2024-06-01", "receive")
    assert ledger.get_revision() == revision
