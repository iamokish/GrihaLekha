"""Electricity deferral carries service forward without inventing debt or units."""

from datetime import datetime

import pytest
from sqlalchemy import select, update

from grihalekha import clock, models as m
from grihalekha.ledger import Ledger
from grihalekha.tenancy_deletion import tenancy_deletion_html


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 4, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Home", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Household", "2024-01-01", 1, 0,
                                initial_meter_reading_hundredths=12_345)
    ledger.prepare_monthly_drafts("2024-03-01")
    yield ledger, tenant
    ledger.close()


def powers(ledger, tenant):
    return {row["electricity_cycle_start"]: row for row in ledger.list_charges(tenant)
            if row["kind"] == "electricity" and row["status"] != "void"}


def defer_two(ledger, tenant):
    bills = powers(ledger, tenant)
    for cycle in ("2024-01-01", "2024-02-01"):
        ledger.set_electricity_deferred(bills[cycle]["id"], True)
    return bills


def post_rents(ledger, tenant):
    for row in ledger.list_charges(tenant):
        if row["kind"] == "rent" and row["status"] == "draft":
            ledger.post_charge(row["id"], 0)


def test_three_month_bill_keeps_one_amount_and_explicit_source_cycles(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    preview = ledger.get_bill(target)
    assert preview["period_start"] == "2024-03-01"
    assert preview["coverage_period_start"] == "2024-01-01"
    assert preview["coverage_period_end"] == "2024-03-31"
    assert preview["electricity_deferred_cycle_starts"] == ["2024-01-01", "2024-02-01"]
    assert ledger.balance(tenant)["outstanding_paise"] == 0
    ledger.post_charge(target, 37_500, meter_reading_hundredths=48_765)
    assert ledger.balance(tenant)["outstanding_paise"] == 37_500
    for cycle in ("2024-01-01", "2024-02-01"):
        source = ledger.get_bill(bills[cycle]["id"])
        assert source["electricity_deferred"] and source["status"] == "draft"
        assert source["amount_paise"] is None and source["meter_reading_hundredths"] is None
        assert source["electricity_covered_by_charge_id"] == target
        assert source["electricity_covered_by_cycle_start"] == "2024-03-01"
        assert not source["needs_amount"]
    assert ledger.prepare_monthly_drafts("2024-03-31") == 0
    ledger.deposit_payment(tenant, 37_500, "2024-04-01", "cash")
    ledger.mark_bill_paid(target)
    assert ledger.balance(tenant)["outstanding_paise"] == ledger.balance(tenant)["credit_paise"] == 0
    assert ledger.get_bill(target)["paid_paise"] == 37_500


def test_deferring_clears_amount_reading_and_undo_reopens_unknown_draft(account):
    ledger, tenant = account
    bill = powers(ledger, tenant)["2024-01-01"]["id"]
    ledger.set_meter_reading(bill, 123_456)
    with ledger._write() as conn:
        conn.execute(update(m.charges).where(m.charges.c.id == bill).values(amount_paise=123))
    ledger.set_electricity_deferred(bill, True)
    deferred = ledger.get_bill(bill)
    assert deferred["amount_paise"] is deferred["meter_reading_hundredths"] is None
    revision = ledger.get_revision()
    ledger.set_electricity_deferred(bill, True)
    assert ledger.get_revision() == revision
    with pytest.raises(ValueError, match="deferral"):
        ledger.post_charge(bill, 0)
    with pytest.raises(ValueError, match="Deferred"):
        ledger.set_meter_reading(bill, 0)
    ledger.set_electricity_deferred(bill, False)
    assert ledger.get_bill(bill)["needs_amount"]
    ledger.post_charge(bill, 0, meter_reading_hundredths=0)
    assert ledger.get_bill(bill)["meter_reading_hundredths"] == 0


def test_deferral_only_accepts_monthly_unposted_electricity(account):
    ledger, tenant = account
    rent = next(row for row in ledger.list_charges(tenant) if row["kind"] == "rent")
    electricity = powers(ledger, tenant)["2024-01-01"]["id"]
    for candidate in (rent["id"],):
        with pytest.raises(ValueError, match="monthly electricity"):
            ledger.set_electricity_deferred(candidate, True)
    with pytest.raises(ValueError, match="Choose"):
        ledger.set_electricity_deferred(electricity, 1)
    ledger.post_charge(electricity, 0)
    with pytest.raises(ValueError, match="monthly electricity"):
        ledger.set_electricity_deferred(electricity, True)


def test_covered_cycle_cannot_be_undone_or_posted_twice(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    ledger.post_charge(bills["2024-03-01"]["id"], 12_345)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="already included"):
        ledger.set_electricity_deferred(bills["2024-01-01"]["id"], False)
    with pytest.raises(ValueError, match="deferral"):
        ledger.post_charge(bills["2024-01-01"]["id"], 12_345)
    assert ledger.get_revision() == revision
    assert ledger.balance(tenant)["billed_paise"] == 12_345


def test_unreviewed_cycle_stops_automatic_coverage(account):
    ledger, tenant = account
    bills = powers(ledger, tenant)
    ledger.set_electricity_deferred(bills["2024-01-01"]["id"], True)
    assert ledger.get_bill(bills["2024-02-01"]["id"])["coverage_period_start"] == "2024-01-01"
    assert ledger.get_bill(bills["2024-03-01"]["id"])["coverage_period_start"] == "2024-03-01"
    ledger.post_charge(bills["2024-03-01"]["id"], 0)
    assert ledger.get_bill(bills["2024-01-01"]["id"])["electricity_covered_by_charge_id"] is None
    ledger.post_charge(bills["2024-02-01"]["id"], 234)
    assert ledger.get_bill(bills["2024-01-01"]["id"])["electricity_covered_by_charge_id"] == bills["2024-02-01"]["id"]


def test_all_covered_cycle_locks_protect_post_payment_and_correction(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="locked"):
        ledger.post_charge(target, 1_000, meter_reading_hundredths=999)
    assert ledger.get_revision() == revision
    assert ledger.get_bill(target)["meter_reading_hundredths"] is None
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", False)
    ledger.post_charge(target, 1_000)
    ledger.deposit_payment(tenant, 1_000, "2024-04-01", "cash")
    ledger.set_cycle_payment_lock(tenant, "2024-02-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.mark_bill_paid(target)
    with pytest.raises(ValueError, match="locked"):
        ledger.revise_bill_amount(target, 900, "Correction")
    ledger.set_meter_reading(target, 123)  # Reference metadata remains editable.
    preview = ledger.preview_move_out(tenant, "2024-02-20", 0)
    assert "2024-02-01" in preview["locked_cycle_starts"]


def test_amount_correction_transfers_coverage_and_reading_without_repaying(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 2_000, meter_reading_hundredths=23_456)
    ledger.deposit_payment(tenant, 2_000, "2024-04-01", "cash")
    ledger.mark_bill_paid(target)
    revised = ledger.revise_bill_amount(target, 1_500, "Correct total")
    detail = ledger.get_bill(revised)
    assert detail["meter_reading_hundredths"] == 23_456
    assert detail["coverage_period_start"] == "2024-01-01"
    assert detail["electricity_cycle_start"] == "2024-03-01"
    assert detail["paid_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 2_000
    assert ledger.get_bill(bills["2024-01-01"]["id"])["electricity_covered_by_charge_id"] == revised
    assert ledger.prepare_monthly_drafts("2024-03-31") == 0


def test_departure_cancellation_reopens_deferred_service_and_preserves_date_boundary(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 3_000, meter_reading_hundredths=40_000)
    ledger.deposit_payment(tenant, 3_000, "2024-04-01", "cash")
    ledger.mark_bill_paid(target)
    summary = ledger.move_out(tenant, "2024-02-20", 0)
    assert target in summary["stopped_bill_ids"]
    assert ledger.balance(tenant)["credit_paise"] == 3_000
    assert ledger.get_bill(bills["2024-01-01"]["id"])["electricity_covered_by_charge_id"] is None
    final_bill = powers(ledger, tenant)["2024-02-01"]["id"]
    ledger.set_electricity_deferred(final_bill, False)
    ledger.post_charge(final_bill, 1_500)
    final = ledger.get_bill(final_bill)
    assert final["coverage_period_start"] == "2024-01-01"
    assert final["coverage_period_end"] == "2024-02-20"
    assert final["charge_date"] == "2024-04-15"
    assert ledger.balance(tenant, "2024-04-14")["billed_paise"] == 3_000
    assert ledger.balance(tenant)["billed_paise"] == 1_500


def test_pending_deferrals_block_archive_but_covered_records_allow_archive_and_delete(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    ledger.move_out(tenant, "2024-03-31", 0)
    post_rents(ledger, tenant)
    with pytest.raises(ValueError, match="pending"):
        ledger.archive_tenancy(tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 0, meter_reading_hundredths=12_345)
    ledger.archive_tenancy(tenant)
    with pytest.raises(ValueError, match="archived"):
        ledger.set_electricity_deferred(bills["2024-01-01"]["id"], False)
    preview = ledger.prepare_tenancy_deletion(tenant)
    html = tenancy_deletion_html(preview)
    assert "Deferred" in html and "123.45" in html
    ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    assert ledger.list_tenancies(include_archived=True) == []
    with ledger._read() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_statement_keeps_deferred_months_distinct_and_counts_combined_total_once(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    post_rents(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 1_234)
    statement = ledger.statement(tenant, "2024-01-01", "2024-03-31")
    cycles = statement["billing_cycles"]
    assert sum(row["electricity_paise"] or 0 for row in cycles) == 1_234
    assert [row["electricity_status"] for row in cycles] == ["deferred", "deferred", "unpaid"]
    assert not any(row["review_pending"] for row in cycles)
    january = ledger.statement(tenant, "2024-01-01", "2024-01-31")
    assert len(january["billing_cycles"]) == 1
    assert january["billing_cycles"][0]["electricity_covered_by_charge_ids"] == [target]
    assert january["billing_cycles"][0]["billed_paise"] == 0


def test_future_combined_bill_is_not_reported_as_covered_before_charge_date(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    with ledger._write() as conn:
        conn.execute(update(m.charges).where(m.charges.c.id == target).values(charge_date="2024-05-01"))
    ledger.post_charge(target, 12_300)
    assert ledger.balance(tenant)["billed_paise"] == 0
    assert ledger.get_bill(bills["2024-01-01"]["id"])["electricity_covered_by_charge_id"] is None
    assert ledger.get_bill(bills["2024-01-01"]["id"], "2024-05-01")["electricity_covered_by_charge_id"] == target
    with pytest.raises(ValueError, match="already included"):
        ledger.set_electricity_deferred(bills["2024-01-01"]["id"], False)


def test_deferrals_survive_reopen_without_extra_drafts(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 456)
    directory = ledger.data_dir
    ledger.close()
    reopened = Ledger(directory)
    try:
        assert reopened.prepare_monthly_drafts("2024-03-01") == 0
        assert reopened.get_bill(target)["coverage_period_start"] == "2024-01-01"
        assert reopened.balance(tenant)["outstanding_paise"] == 456
    finally:
        reopened.close()


def test_manual_charge_cannot_double_bill_deferred_service(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    for posted in (False, True):
        if posted:
            ledger.post_charge(bills["2024-03-01"]["id"], 1_000)
        with pytest.raises(ValueError, match="deferred"):
            ledger.add_charge(tenant, "electricity", 1_000, "2024-01-01", "2024-03-31", "2024-03-31")
    assert ledger.balance(tenant)["billed_paise"] == 1_000


def test_redeferring_cannot_erase_combined_cancellation_chronology(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    target = bills["2024-03-01"]["id"]
    ledger.post_charge(target, 3_000)
    ledger.move_out(tenant, "2024-02-20", 0)
    for cycle in ("2024-01-01", "2024-02-01"):
        source = bills[cycle]["id"]
        ledger.set_electricity_deferred(source, False)
        ledger.set_electricity_deferred(source, True)
    final = bills["2024-02-01"]["id"]
    ledger.set_electricity_deferred(final, False)
    ledger.post_charge(final, 1_000)
    assert ledger.get_bill(final)["charge_date"] == "2024-04-15"
    assert ledger.balance(tenant, "2024-04-14")["billed_paise"] == 3_000


def test_historical_statement_retains_original_coverage_after_correction(account):
    ledger, tenant = account
    bills = defer_two(ledger, tenant)
    original = bills["2024-03-01"]["id"]
    ledger.post_charge(original, 3_000)
    revised = ledger.revise_bill_amount(original, 2_000, "Correct total")
    assert ledger.get_bill(original, "2024-04-14")["coverage_period_start"] == "2024-01-01"
    january = bills["2024-01-01"]["id"]
    assert ledger.get_bill(january, "2024-04-14")["electricity_covered_by_charge_id"] == original
    assert ledger.get_bill(january)["electricity_covered_by_charge_id"] == revised
    assert ledger.get_bill(revised)["coverage_period_start"] == "2024-01-01"


def test_partial_first_cycle_and_day_31_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 5, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path)
    try:
        building = ledger.add_building("Home", "Address")
        flat = ledger.add_flat(building, "1", "Ground", 1)
        tenant = ledger.add_tenancy(flat, "Household", "2024-02-10", 31, 0)
        ledger.prepare_monthly_drafts("2024-03-31")
        bills = powers(ledger, tenant)
        ledger.set_electricity_deferred(bills["2024-01-31"]["id"], True)
        ledger.set_electricity_deferred(bills["2024-02-29"]["id"], True)
        target = bills["2024-03-31"]["id"]
        ledger.post_charge(target, 100)
        assert ledger.get_bill(target)["coverage_period_start"] == "2024-02-10"
        assert ledger.get_bill(target)["coverage_period_end"] == "2024-04-29"
    finally:
        ledger.close()
