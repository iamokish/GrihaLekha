"""Current account balances must be independent of the selected cycle range."""

import csv
from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill
from grihalekha.reports import export_csv, statement_html


@pytest.fixture
def current_account(tmp_path, monkeypatch):
    now = datetime(2026, 11, 7, 10, 15, 30, tzinfo=clock.IST)
    monkeypatch.setattr(clock, "now_ist", lambda: now)
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Current House", "Example Road")
    flat = ledger.add_flat(building, "101", "1", 2)
    tenancy = ledger.add_tenancy(flat, "Tenant", "2026-10-01", 1, 100_000)
    yield ledger, tenancy
    ledger.close()


def _bill(ledger, tenancy, kind, amount, start, end):
    return ledger.add_charge(tenancy, kind, amount, start, end, end, charge_date=start)


def _legacy_future_receipt(ledger, tenancy):
    """Older releases allowed future-dated receipts; reports must exclude them."""
    with ledger._write() as conn:
        ledger._record_payment(conn, tenancy, 25_000, "2026-12-02", "cash", requested={})


def test_future_report_keeps_cycles_but_excludes_future_money(current_account):
    ledger, tenancy = current_account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-11-01", "2026-11-30")
    _bill(ledger, tenancy, "electricity", 10_000, "2026-11-01", "2026-11-30")
    future_rent = _bill(ledger, tenancy, "rent", 100_000, "2026-12-01", "2026-12-31")
    ledger.record_payment(tenancy, 100_000, "2026-11-02", "cash", allocations={rent: 100_000})
    _legacy_future_receipt(ledger, tenancy)
    ledger.record_deposit(tenancy, 50_000, "2026-12-03", "receive")

    snapshot = ledger.statement(tenancy, "2026-11-01", "2027-01-07")
    assert snapshot["balance_as_of"] == "2026-11-07"
    assert snapshot["balance_calculated_at"] == "2026-11-07T10:15:30+05:30"
    assert snapshot["closing_balance_paise"] == 10_000
    assert snapshot["period_closing_balance_paise"] == 10_000
    assert snapshot["balance"]["deposit_paise"] == 0
    assert snapshot["period_charges_paise"] == 110_000
    assert snapshot["period_payments_paise"] == 100_000
    assert len(snapshot["billing_cycles"]) == 3
    assert snapshot["billing_cycles"][0]["payment_status"] == "unpaid"
    assert snapshot["billing_cycles"][0]["paid_paise"] == 100_000
    assert snapshot["billing_cycles"][1]["billed_paise"] == 0
    assert snapshot["billing_cycles"][2]["status"] == "review_pending"
    detail = next(row for row in snapshot["billing_details"] if row["id"] == future_rent)
    assert not detail["billed_at_cutoff"]
    html = statement_html(snapshot)
    assert "Balances as of</strong>: 07 Nov 2026, 10:15:30 IST" in html
    assert "Issued</strong>: 07 Nov 2026, 10:15:30 IST" in html
    assert "Period</strong>: 01 Nov 2026 – 07 Jan 2027" in html
    assert "Amount left to pay</td><td>₹100.00" in html


def test_old_cycles_show_current_paid_status_and_separate_period_totals(current_account, tmp_path):
    ledger, tenancy = current_account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-10-01", "2026-10-31")
    _bill(ledger, tenancy, "electricity", 0, "2026-10-01", "2026-10-31")
    ledger.record_payment(tenancy, 100_000, "2026-11-02", "cash", allocations={rent: 100_000})

    snapshot = ledger.statement(tenancy, "2026-10-01", "2026-10-31")
    assert snapshot["billing_cycles"][0]["status"] == "paid"
    assert snapshot["closing_balance_paise"] == 0
    assert snapshot["period_closing_balance_paise"] == 100_000
    assert snapshot["period_charges_paise"] == 100_000
    assert snapshot["period_payments_paise"] == 0
    assert snapshot["payments"] == []
    html = statement_html(snapshot)
    assert "Net account changes after this period</td><td>−₹1,000.00" in html
    assert "Amount left to pay</td><td>₹0.00" in html
    assert '<span class="payment-paid">Paid</span>' in html
    historical = ledger.statement(tenancy, "2026-10-01", "2026-10-31", as_of="2026-10-31")
    assert historical["closing_balance_paise"] == 100_000
    assert historical["billing_cycles"][0]["payment_status"] == "unpaid"

    paths = export_csv(snapshot, tmp_path / "export")
    with next(path for path in paths if path.name.endswith("-summary.csv")).open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["balance_as_of"] == "2026-11-07"
    assert row["period_closing_balance_inr"] == "1000.00"
    assert row["closing_balance_inr"] == "0.00"


def test_future_only_period_reconciles_without_counting_future_transactions(current_account):
    ledger, tenancy = current_account
    _bill(ledger, tenancy, "rent", 100_000, "2026-11-01", "2026-11-30")
    _bill(ledger, tenancy, "rent", 100_000, "2026-12-01", "2026-12-31")
    _legacy_future_receipt(ledger, tenancy)
    snapshot = ledger.statement(tenancy, "2026-12-01", "2027-01-31")
    assert snapshot["opening_balance_paise"] == snapshot["closing_balance_paise"] == 100_000
    assert snapshot["period_charges_paise"] == snapshot["period_payments_paise"] == 0
    assert len(snapshot["billing_cycles"]) == 2
    assert all(row["billed_paise"] == 0 for row in snapshot["billing_cycles"])
    assert "Earlier bills still pending" in statement_html(snapshot)


def test_current_status_includes_later_corrections_without_rewriting_period_movements(current_account):
    ledger, tenancy = current_account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-10-01", "2026-10-31")
    cancel_historical_bill(ledger, rent, "Agreed waiver", "2026-11-02")
    snapshot = ledger.statement(tenancy, "2026-10-01", "2026-10-31")
    assert snapshot["closing_balance_paise"] == 0
    assert snapshot["period_closing_balance_paise"] == 100_000
    assert snapshot["period_charges_paise"] == 100_000
    assert snapshot["adjustments"] == []
    assert snapshot["charges"][0]["status"] == "void"
    assert "rent" in snapshot["billing_cycles"][0]["review_pending_kinds"]
    html = statement_html(snapshot)
    assert "Net account changes after this period" in html
    assert "Agreed waiver" in html
    assert "Cancelled; correction listed below" not in html


def test_current_statement_requires_cancelled_monthly_bill_without_creating_draft(current_account):
    ledger, tenancy = current_account
    _bill(ledger, tenancy, "rent", 100_000, "2026-11-01", "2026-11-30")
    electricity = _bill(ledger, tenancy, "electricity", 10_000, "2026-11-01", "2026-11-30")
    cancel_historical_bill(ledger, electricity, "Correct meter amount", "2026-11-07")
    before = ledger.get_revision()
    charges_before = ledger.list_charges(tenancy)
    snapshot = ledger.statement(tenancy, "2026-11-01", "2026-11-30")
    cycle, = snapshot["billing_cycles"]
    assert cycle["status"] == "review_pending"
    assert cycle["review_pending_kinds"] == ["electricity"]
    assert cycle["electricity_status"] == "review_pending"
    assert cycle["pending_paise"] == snapshot["closing_balance_paise"] == 100_000
    assert ledger.get_revision() == before
    assert ledger.list_charges(tenancy) == charges_before
    assert all(row["status"] != "draft" for row in charges_before)
    historical = ledger.statement(tenancy, "2026-11-01", "2026-11-30", as_of="2026-11-06")
    assert historical["billing_cycles"][0]["electricity_status"] == "unpaid"
    assert historical["closing_balance_paise"] == 110_000
