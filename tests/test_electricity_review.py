"""Independent lifecycle and historical-cutoff regressions for combined bills."""

from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger


@pytest.fixture
def combined(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 4, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path)
    building = ledger.add_building("Review house", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 0)
    ledger.prepare_monthly_drafts("2024-03-01")
    bills = sorted((bill for bill in ledger.list_charges(tenant) if bill["kind"] == "electricity"),
                   key=lambda bill: bill["period_start"])
    for source in bills[:2]:
        ledger.set_electricity_deferred(source["id"], True)
    ledger.post_charge(bills[2]["id"], 3_000, meter_reading_hundredths=40_001)
    yield ledger, tenant, bills
    ledger.close()


def test_historical_combined_coverage_survives_amount_correction(combined):
    ledger, tenant, bills = combined
    original = bills[2]["id"]
    revised = ledger.revise_bill_amount(original, 2_500, "Correct combined amount")
    historical = ledger.get_bill(original, "2024-03-31")
    assert historical["status"] == "posted"
    assert historical["coverage_period_start"] == "2024-01-01"
    for source in bills[:2]:
        assert ledger.get_bill(source["id"], "2024-03-31")["electricity_covered_by_charge_id"] == original
        assert ledger.get_bill(source["id"])["electricity_covered_by_charge_id"] == revised
    snapshot = ledger.statement(tenant, "2024-01-01", "2024-03-31", as_of="2024-03-31")
    assert snapshot["billing_cycles"][0]["electricity_covered_by_charge_ids"] == [original]


def test_partial_departure_combined_rebill_preserves_money_once(combined):
    ledger, tenant, bills = combined
    original = bills[2]["id"]
    ledger.deposit_payment(tenant, 3_000, "2024-04-01", "cash")
    ledger.mark_bill_paid(original)
    summary = ledger.move_out(tenant, "2024-03-20", 0)
    assert original in summary["electricity_review_bill_ids"]
    replacement = next(bill for bill in ledger.list_charges(tenant)
                       if bill["kind"] == "electricity" and not bill["electricity_deferred"]
                       and bill["status"] == "draft")
    assert replacement["coverage_period_start"] == "2024-01-01"
    assert replacement["coverage_period_end"] == "2024-03-20"
    ledger.post_charge(replacement["id"], 2_500, meter_reading_hundredths=39_901)
    assert ledger.balance(tenant)["credit_paise"] == 3_000
    assert ledger.balance(tenant)["outstanding_paise"] == 2_500
    ledger.mark_bill_paid(replacement["id"])
    assert ledger.balance(tenant)["credit_paise"] == 500
    assert len(ledger.list_payments(tenant)) == 1


def test_future_correction_keeps_current_combined_bill_and_prevents_double_billing(combined):
    ledger, tenant, bills = combined
    original = bills[2]["id"]
    ledger.revise_bill_amount(original, 2_500, "Correction from next month", effective_on="2024-05-01")
    assert ledger.balance(tenant)["outstanding_paise"] == 3_000
    assert ledger.balance(tenant, "2024-05-01")["outstanding_paise"] == 2_500
    assert ledger.get_bill(bills[0]["id"])["electricity_covered_by_charge_id"] == original
    with pytest.raises(ValueError, match="already included"):
        ledger.set_electricity_deferred(bills[0]["id"], False)
