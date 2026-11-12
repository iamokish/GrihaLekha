"""Departure corrections stop bills while preserving dated receipts and debt."""

from datetime import date

import pytest
from sqlalchemy import select

from grihalekha import models as m
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(tmp_path, monkeypatch):
    for module in ("ledger", "lifecycle", "billing"):
        monkeypatch.setattr(f"grihalekha.{module}.today_ist", lambda: date(2024, 7, 10))
    ledger = Ledger(tmp_path / "moveout")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    yield ledger, tenant, flat
    ledger.close()


def bill(ledger, tenant, cycle, kind):
    return next(row for row in ledger.list_charges(tenant)
                if row["period_start"] == cycle and row["kind"] == kind and row["status"] != "void")


def test_backdated_departure_stops_later_bills_and_keeps_late_payments(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-06-01")
    for row in ledger.list_charges(tenant):
        ledger.post_charge(row["id"], 100_000 if row["kind"] == "rent" else 1_000)
    march = bill(ledger, tenant, "2024-03-01", "rent")
    receipt = ledger.deposit_payment(tenant, 100_000, "2024-03-10", "cash")
    ledger.mark_bill_paid(march["id"], "2024-03-10")
    original_balance = ledger.balance(tenant, "2024-03-31")
    revision = ledger.get_revision()
    preview = ledger.preview_move_out(tenant, "2024-02-15", 50_000)
    assert preview["returned_paise"] == 100_000
    assert len(preview["stopped_bill_ids"]) == 8
    assert len(preview["electricity_review_bill_ids"]) == 1
    assert ledger.get_revision() == revision
    assert ledger.move_out(tenant, "2024-02-15", 50_000) == preview
    cycles = ledger.list_bill_cycles(tenant)
    assert [row["cycle_start"] for row in cycles] == ["2024-01-01", "2024-02-01"]
    assert cycles[-1]["moved_out_cycle"]
    assert cycles[-1]["last_occupied_day"] == "2024-02-15"
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert ledger.balance(tenant)["outstanding_paise"] == 151_000
    assert ledger.balance(tenant, "2024-03-31") == original_balance
    assert ledger.list_payments(tenant)[0]["id"] == receipt
    electricity = bill(ledger, tenant, "2024-02-01", "electricity")
    assert (electricity["period_end"], electricity["status"], electricity["amount_paise"]) == ("2024-02-15", "draft", None)
    ledger.post_charge(electricity["id"], 0)
    late_receipt = ledger.deposit_payment(tenant, 51_000, "2024-07-10", "upi")
    for row in ledger.list_charges(tenant):
        if row["status"] == "posted":
            ledger.mark_bill_paid(row["id"], "2024-07-10")
    assert ledger.balance(tenant)["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 0
    assert late_receipt in {row["id"] for row in ledger.list_payments(tenant)}
    ledger.prepare_monthly_drafts("2025-01-01")
    assert len(ledger.list_bill_cycles(tenant)) == 2


def test_moveout_lock_rolls_back_all_occupancy_and_financial_changes(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-05-01")
    ledger.set_cycle_payment_lock(tenant, "2024-04-01", True)
    before = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    assert ledger.preview_move_out(tenant, "2024-02-15", 0)["locked_cycle_starts"] == ["2024-04-01"]
    with pytest.raises(ValueError, match="locked"):
        ledger.move_out(tenant, "2024-02-15", 0)
    assert ledger.get_tenancy(tenant)["move_out"] is None
    assert ledger.list_charges(tenant) == before
    assert ledger.get_revision() == revision


def test_moveout_before_account_opening_keeps_only_opening_debt(account):
    ledger, original, flat = account
    ledger.move_out(original, "2024-01-01", 0)
    tenant = ledger.add_tenancy(flat, "Imported former tenant", "2024-02-01", 1, 100_000,
                                opening_date="2024-06-01")
    ledger.set_opening_balances(tenant, "2024-06-01", rent_paise=10_000)
    ledger.prepare_monthly_drafts("2024-07-01")
    with pytest.raises(ValueError, match="opening balance"):
        ledger.move_out(tenant, "2024-04-30", 1)
    ledger.move_out(tenant, "2024-04-30", 0)
    assert ledger.balance(tenant)["outstanding_paise"] == 10_000
    assert all(row["opening"] for cycle in ledger.list_bill_cycles(tenant) for row in cycle["charges"])
    assert ledger.prepare_monthly_drafts("2024-12-01") == 0


def test_cancel_planned_moveout_resumes_drafts_and_returns_applied_money(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-08-01")
    ledger.move_out(tenant, "2024-07-20", 50_000)
    rent = bill(ledger, tenant, "2024-07-01", "rent")
    electricity = bill(ledger, tenant, "2024-07-01", "electricity")
    ledger.post_charge(electricity["id"], 2_000)
    payment = ledger.deposit_payment(tenant, 52_000, "2024-07-10", "cash")
    ledger.mark_bill_paid(rent["id"], "2024-07-10")
    ledger.mark_bill_paid(electricity["id"], "2024-07-10")
    before = ledger.get_revision()
    preview = ledger.preview_cancel_move_out(tenant)
    assert preview["returned_paise"] == 52_000
    assert preview["resumed_through"] == "2024-08-01"
    assert ledger.get_revision() == before
    assert ledger.cancel_scheduled_move_out(tenant, "Staying longer") == preview
    tenancy = ledger.get_tenancy(tenant)
    assert tenancy["move_out"] is None
    assert tenancy["occupancy_history"][-1]["ends_on"] is None
    assert ledger.balance(tenant)["credit_paise"] == 52_000
    assert ledger.list_payments(tenant)[0]["id"] == payment
    rent = bill(ledger, tenant, "2024-07-01", "rent")
    assert (rent["status"], rent["period_end"], rent["amount_paise"]) == ("draft", "2024-07-31", 100_000)
    electricity = bill(ledger, tenant, "2024-07-01", "electricity")
    assert (electricity["status"], electricity["period_end"], electricity["amount_paise"]) == ("draft", "2024-07-31", None)
    assert len(ledger.list_bill_cycles(tenant)) == 8
    assert not any(row["moved_out_cycle"] for row in ledger.list_bill_cycles(tenant))
    assert ledger.prepare_monthly_drafts("2024-08-01") == 0


def test_cancel_moveout_rejects_overlap_without_changes(account):
    ledger, tenant, flat = account
    ledger.move_out(tenant, "2024-07-20", 50_000)
    ledger.add_tenancy(flat, "Incoming household", "2024-07-21", 1, 100_000)
    before = ledger.list_charges(tenant)
    with pytest.raises(ValueError, match="overlapping"):
        ledger.cancel_scheduled_move_out(tenant)
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-07-20"
    assert ledger.list_charges(tenant) == before


def test_cancel_moveout_respects_locks_and_rejects_completed_departure(account):
    ledger, tenant, _ = account
    with pytest.raises(ValueError, match="future planned"):
        ledger.cancel_scheduled_move_out(tenant)
    ledger.move_out(tenant, "2024-07-20", 50_000)
    ledger.set_cycle_payment_lock(tenant, "2024-07-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.cancel_scheduled_move_out(tenant)
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-07-20"
    ledger.set_cycle_payment_lock(tenant, "2024-07-01", False)
    ledger.move_out(tenant, "2024-07-10", 50_000)
    with pytest.raises(ValueError, match="future planned"):
        ledger.cancel_scheduled_move_out(tenant)


def test_manual_electricity_service_dates_are_preserved_at_departure(account):
    ledger, tenant, _ = account
    electricity = ledger.add_charge(tenant, "electricity", 8_000, "2024-01-20", "2024-02-19", "2024-02-25")
    ledger.move_out(tenant, "2024-02-15", 50_000)
    result = ledger.get_bill(electricity)
    assert result["status"] == "posted"
    assert (result["period_start"], result["period_end"], result["amount_paise"]) == ("2024-01-20", "2024-02-19", 8_000)


def test_cancel_full_cycle_departure_keeps_unchanged_paid_bills_and_locks(account):
    ledger, tenant, _ = account
    ledger.move_out(tenant, "2024-07-31", 100_000)
    rent = bill(ledger, tenant, "2024-07-01", "rent")
    ledger.deposit_payment(tenant, 100_000, "2024-07-10", "cash")
    ledger.mark_bill_paid(rent["id"], "2024-07-10")
    ledger.set_cycle_payment_lock(tenant, "2024-07-01", True)
    before = ledger.list_charges(tenant)
    summary = ledger.cancel_scheduled_move_out(tenant)
    assert not summary["corrected_bill_ids"]
    assert not summary["locked_cycle_starts"]
    assert summary["returned_paise"] == 0
    assert ledger.list_charges(tenant) == before
    assert ledger.get_bill(rent["id"])["paid_paise"] == 100_000


def test_backdated_short_month_anchor_keeps_exact_final_cycle(account):
    ledger, _, _ = account
    building = ledger.add_building("Other house", "Other address")
    flat = ledger.add_flat(building, "31", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Month end tenant", "2024-01-31", 31, 100_000)
    ledger.prepare_monthly_drafts("2024-05-31")
    ledger.move_out(tenant, "2024-02-29", 0)
    cycles = ledger.list_bill_cycles(tenant)
    assert [row["cycle_start"] for row in cycles] == ["2024-01-31", "2024-02-29"]
    assert cycles[-1]["moved_out_cycle"]
    final = bill(ledger, tenant, "2024-02-29", "rent")
    assert (final["period_end"], final["amount_paise"], final["status"]) == ("2024-02-29", 0, "posted")


def test_paid_final_electricity_replacement_preserves_historical_money(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-02-01")
    electricity = bill(ledger, tenant, "2024-02-01", "electricity")
    ledger.post_charge(electricity["id"], 2_000)
    ledger.deposit_payment(tenant, 2_000, "2024-02-05", "upi")
    ledger.mark_bill_paid(electricity["id"], "2024-02-05")
    before = ledger.balance(tenant, "2024-03-01")
    preview = ledger.preview_move_out(tenant, "2024-02-15", 0)
    assert preview["returned_paise"] == 2_000
    ledger.move_out(tenant, "2024-02-15", 0)
    assert ledger.balance(tenant, "2024-03-01") == before
    assert ledger.balance(tenant)["credit_paise"] == 2_000
    replacement = bill(ledger, tenant, "2024-02-01", "electricity")
    assert replacement["replaces_charge_id"] == electricity["id"]
    assert replacement["amount_paise"] is None
    assert replacement["electricity_cycle_start"] == "2024-02-01"


@pytest.mark.parametrize("kind", ["rent", "electricity"])
@pytest.mark.parametrize("paid", [False, True])
def test_moveout_advances_legacy_scheduled_cancellation_and_payment_release(account, kind, paid):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-03-01")
    later = bill(ledger, tenant, "2024-03-01", kind)
    ledger.post_charge(later["id"], 100_000)
    if paid:
        ledger.deposit_payment(tenant, 100_000, "2024-03-10", "cash")
        ledger.mark_bill_paid(later["id"], "2024-03-10")
    cancel_historical_bill(ledger, later["id"], "Old scheduled correction", "2024-07-20")
    history = {day: ledger.balance(tenant, day) for day in ("2024-03-05", "2024-03-15", "2024-07-09")}
    preview = ledger.preview_move_out(tenant, "2024-02-15", 0)
    assert later["id"] in preview["stopped_bill_ids"]
    assert preview["returned_paise"] == (100_000 if paid else 0)
    assert preview["advanced_cancellations"] == [{
        "charge_id": later["id"], "previous_voided_on": "2024-07-20",
        "effective_on": "2024-07-10", "returned_paise": 100_000 if paid else 0,
    }]
    assert ledger.move_out(tenant, "2024-02-15", 0) == preview
    assert ledger.balance(tenant)["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == (100_000 if paid else 0)
    assert ledger.get_bill(later["id"])["voided_on"] == "2024-07-10"
    assert [row["cycle_start"] for row in ledger.list_bill_cycles(tenant)] == ["2024-01-01", "2024-02-01"]
    assert {day: ledger.balance(tenant, day) for day in history} == history
    with ledger._read() as conn:
        assert conn.execute(select(m.audit.c.id).where(
            m.audit.c.action == "advance_move_out_cancellation", m.audit.c.entity_id == later["id"],
        )).first()
        if paid:
            assert conn.execute(select(m.allocations.c.cancelled_on).where(
                m.allocations.c.charge_id == later["id"],
            )).scalar_one() == "2024-07-10"


@pytest.mark.parametrize("scheduled_cancellation", [False, True])
def test_moveout_rejects_advancing_cancellation_past_future_allocation(account, monkeypatch, scheduled_cancellation):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-03-01")
    later = bill(ledger, tenant, "2024-03-01", "rent")
    ledger.post_charge(later["id"], 100_000)
    # Seed the future payment history permitted by older application versions.
    with monkeypatch.context() as historical_clock:
        historical_clock.setattr("grihalekha.ledger.today_ist", lambda: date(2024, 7, 15))
        ledger.record_payment(tenant, 100_000, "2024-07-15", "cash", allocations={later["id"]: 100_000})
    if scheduled_cancellation:
        cancel_historical_bill(ledger, later["id"], "Old scheduled correction", "2024-07-20")
    before = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    for action in (ledger.preview_move_out, ledger.move_out):
        with pytest.raises(ValueError, match="scheduled payment allocation on 2024-07-15"):
            action(tenant, "2024-02-15", 0)
    assert ledger.get_tenancy(tenant)["move_out"] is None
    assert ledger.list_charges(tenant) == before
    assert ledger.get_revision() == revision


def test_final_rent_scheduled_cancellation_is_advanced_before_replacement(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-02-01")
    final = bill(ledger, tenant, "2024-02-01", "rent")
    ledger.post_charge(final["id"], 100_000)
    cancel_historical_bill(ledger, final["id"], "Old scheduled correction", "2024-07-20")
    ledger.move_out(tenant, "2024-02-15", 50_000)
    replacement = bill(ledger, tenant, "2024-02-01", "rent")
    assert replacement["charge_date"] == "2024-07-10"
    assert replacement["period_end"] == "2024-02-15"
    assert replacement["replaces_charge_id"] == final["id"]
    assert ledger.balance(tenant)["outstanding_paise"] == 50_000
    assert ledger.balance(tenant, "2024-07-09")["outstanding_paise"] == 100_000


def test_cancel_moveout_corrects_pending_legacy_final_bill_cancellation(account):
    ledger, tenant, _ = account
    ledger.move_out(tenant, "2024-07-20", 50_000)
    final = bill(ledger, tenant, "2024-07-01", "rent")
    ledger.deposit_payment(tenant, 50_000, "2024-07-05", "cash")
    ledger.mark_bill_paid(final["id"], "2024-07-05")
    cancel_historical_bill(ledger, final["id"], "Old scheduled correction", "2024-07-25")
    previous = ledger.balance(tenant, "2024-07-09")
    preview = ledger.preview_cancel_move_out(tenant)
    assert preview["returned_paise"] == 50_000
    assert ledger.cancel_scheduled_move_out(tenant) == preview
    assert ledger.balance(tenant)["credit_paise"] == 50_000
    assert ledger.balance(tenant, "2024-07-09") == previous
    assert ledger.get_bill(final["id"])["voided_on"] == "2024-07-10"
    replacement = bill(ledger, tenant, "2024-07-01", "rent")
    assert replacement["charge_date"] == "2024-07-10"
    assert replacement["status"] == "draft"
    assert replacement["amount_paise"] == 100_000


def test_cancel_moveout_respects_lock_on_stopped_future_cycle(account):
    ledger, tenant, _ = account
    ledger.prepare_monthly_drafts("2024-08-01")
    ledger.move_out(tenant, "2024-07-20", 50_000)
    ledger.set_cycle_payment_lock(tenant, "2024-08-01", True)
    assert ledger.preview_cancel_move_out(tenant)["locked_cycle_starts"] == ["2024-08-01"]
    with pytest.raises(ValueError, match="locked"):
        ledger.cancel_scheduled_move_out(tenant)
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-07-20"
