"""Removed bills stay private while tenant documents still reconcile."""

import csv
from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger
from grihalekha.reports import PdfExportJob, export_csv, receipt_html, statement_html


@pytest.fixture
def deleted_account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2026, 11, 7, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "101", "1", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2026-10-01", 1, 100_000)
    rent = ledger.add_charge(tenant, "rent", 100_000, "2026-10-01", "2026-10-31", "2026-10-31")
    ledger.add_charge(tenant, "electricity", 0, "2026-10-01", "2026-10-31", "2026-10-31")
    fee = ledger.add_charge(tenant, "misc", 12_500, "2026-10-10", "2026-10-10", "2026-10-31", "PRIVATE REPAIR LINE")
    payment = ledger.deposit_payment(tenant, 12_500, "2026-10-11", "cash")
    ledger.mark_bill_paid(fee, "2026-10-11")
    yield ledger, tenant, rent, fee, payment
    ledger.close()


def _assert_private(snapshot, fee):
    for collection in ("charges", "billing_details", "outstanding_charges"):
        assert all(row["id"] != fee for row in snapshot[collection])
    assert all(fee not in cycle["charge_ids"] for cycle in snapshot["billing_cycles"])
    assert all(row["charge_id"] != fee for row in snapshot["allocations"])
    assert all(allocation["charge_id"] != fee
               for payment in snapshot["payments"] for allocation in payment["allocations"])
    html = statement_html(snapshot)
    assert "PRIVATE REPAIR LINE" not in html
    assert "PRIVATE DELETION REASON" not in html
    assert (snapshot["opening_balance_paise"] + snapshot["period_charges_paise"]
            - snapshot["period_payments_paise"] + snapshot["period_refunds_paise"]
            + snapshot["adjustments_paise"] == snapshot["period_closing_balance_paise"])
    return html


def test_paid_deleted_bill_keeps_received_money_without_any_bill_rows(deleted_account, tmp_path):
    ledger, tenant, rent, fee, payment = deleted_account
    ledger.delete_bill(fee, "PRIVATE DELETION REASON")
    revision = ledger.get_revision()
    snapshot = ledger.statement(tenant, "2026-10-01", "2026-11-30")
    _assert_private(snapshot, fee)
    assert snapshot["period_charges_paise"] == 100_000
    assert snapshot["adjustments"] == []  # removed charge and cancellation net to zero
    assert snapshot["closing_balance_paise"] == 87_500
    assert snapshot["balance"]["credit_paise"] == 12_500
    assert snapshot["payments"][0]["id"] == payment
    assert snapshot["billing_cycles"][0]["misc_paise"] == 0
    assert not ledger.list_payments(tenant)[0]["reversed_on"]
    for path in export_csv(snapshot, tmp_path / "csv"):
        text = path.read_text(encoding="utf-8-sig")
        assert "PRIVATE REPAIR LINE" not in text and "PRIVATE DELETION REASON" not in text
        if path.name.endswith("-charges.csv"):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                assert str(fee) not in [row["id"] for row in csv.DictReader(handle)]
    assert ledger.get_revision() == revision


@pytest.mark.parametrize("start,end,opening,correction,period_balance", [
    ("2026-10-01", "2026-10-31", 0, 12_500, 100_000),
    ("2026-11-01", "2026-11-30", 100_000, -12_500, 87_500),
    ("2026-10-15", "2026-11-30", 100_000, -12_500, 87_500),
    ("2026-12-01", "2026-12-31", 87_500, 0, 87_500),
])
def test_removed_bill_across_period_boundaries_reconciles_without_details(
    deleted_account, start, end, opening, correction, period_balance
):
    ledger, tenant, _, fee, _ = deleted_account
    ledger.delete_bill(fee, "PRIVATE DELETION REASON")
    snapshot = ledger.statement(tenant, start, end)
    html = _assert_private(snapshot, fee)
    assert snapshot["opening_balance_paise"] == opening
    assert snapshot["adjustments_paise"] == correction
    assert snapshot["period_closing_balance_paise"] == period_balance
    assert snapshot["closing_balance_paise"] == 87_500
    if correction:
        correction_row, = snapshot["adjustments"]
        assert correction_row["kind"] == "account_adjustment"
        assert not correction_row.get("charge_id") and not correction_row["reason"]
        assert "Account adjustment" in html


def test_historical_accounting_keeps_amounts_but_new_exports_hide_deleted_details(deleted_account, tmp_path):
    ledger, tenant, _, fee, payment = deleted_account
    ledger.delete_bill(fee, "PRIVATE DELETION REASON")
    snapshot = ledger.statement(tenant, "2026-10-01", "2026-10-31", as_of="2026-10-31")
    _assert_private(snapshot, fee)
    assert snapshot["closing_balance_paise"] == ledger.balance(tenant, "2026-10-31")["net_paise"] == 100_000
    assert snapshot["period_charges_paise"] == 100_000
    assert snapshot["adjustments_paise"] == 12_500
    html = receipt_html(snapshot, payment)
    assert f"#{fee}" not in html and "PRIVATE REPAIR LINE" not in html
    paths = export_csv(snapshot, tmp_path / "historical-csv")
    with next(path for path in paths if path.name.endswith("-allocations.csv")).open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == []


def test_new_pdf_hides_deleted_bill_and_preserves_issued_pdf(deleted_account, qtbot, tmp_path):
    import pymupdf

    ledger, tenant, _, fee, _ = deleted_account

    def export(snapshot, destination):
        job = PdfExportJob(statement_html(snapshot), destination, renderer="native")
        result = {}
        job.finished.connect(lambda path: result.update(path=path))
        job.failed.connect(lambda error: result.update(error=error))
        job.start()
        qtbot.waitUntil(lambda: bool(result), timeout=10_000)
        assert "error" not in result
        return destination.read_bytes()

    original = tmp_path / "issued.pdf"
    original_bytes = export(ledger.statement(tenant, "2026-10-01", "2026-11-30"), original)
    with pymupdf.open(original) as document:
        assert "PRIVATE REPAIR LINE" in "\n".join(page.get_text() for page in document)
    ledger.delete_bill(fee, "PRIVATE DELETION REASON")
    refreshed = tmp_path / "refreshed.pdf"
    export(ledger.statement(tenant, "2026-10-01", "2026-11-30"), refreshed)
    with pymupdf.open(refreshed) as document:
        text = "\n".join(page.get_text() for page in document)
        assert "PRIVATE REPAIR LINE" not in text and "PRIVATE DELETION REASON" not in text
    assert original.read_bytes() == original_bytes
