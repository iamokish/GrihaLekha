"""Tenant-facing cycle coverage without changing financial cutoff accounting."""

import csv

import pytest

from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill
from grihalekha import models as m
from grihalekha.reports import export_csv, statement_html
from grihalekha.statement_cycles import billing_cycles


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Example House", "Example Road")
    flat = ledger.add_flat(building, "101", "1", 2)
    tenancy = ledger.add_tenancy(flat, "Tenant", "2026-01-01", 1, 100_000)
    yield ledger, tenancy
    ledger.close()


def _bill(ledger, tenancy, kind, amount, start, end):
    existing = next((row for row in ledger.list_charges(tenancy) if row["kind"] == kind and row["period_start"] == start and row["status"] == "draft"), None)
    if existing:
        ledger.post_charge(existing["id"], amount_paise=amount, apply_credit=False)
        return existing["id"]
    return ledger.add_charge(tenancy, kind, amount, start, end, end, charge_date=start, apply_credit=False)


def _legacy_partial_payment(ledger, tenancy, charge, amount, paid_on):
    """Seed a pre-upgrade partial allocation; current APIs require full bills."""
    payment = ledger.record_payment(tenancy, amount, paid_on, "cash", allocations={})
    with ledger._write() as conn:
        ledger._allocate(conn, ledger._get(conn, m.payments, payment),
                         ledger._get(conn, m.charges, charge), amount, paid_on)


def test_missing_cycles_are_review_pending_without_writing(account):
    ledger, tenancy = account
    revision = ledger.get_revision()
    snapshot = ledger.statement(tenancy, "2026-01-15", "2026-04-10", as_of="2026-04-10")
    assert len(snapshot["billing_cycles"]) == 4
    assert [row["period_start"] for row in snapshot["billing_cycles"]] == ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    assert all(row["status"] == "review_pending" for row in snapshot["billing_cycles"])
    assert all(row["rent_paise"] is None and row["electricity_paise"] is None for row in snapshot["billing_cycles"])
    assert all(row["billed_paise"] == row["pending_paise"] == 0 for row in snapshot["billing_cycles"])
    assert snapshot["closing_balance_paise"] == 0
    assert ledger.get_revision() == revision
    assert ledger.list_charges(tenancy) == []
    html = statement_html(snapshot)
    assert "Review pending" in html
    assert "Amount left to pay" in html
    assert "Closing account balance" not in html
    assert "Outstanding bills" not in html and "Overdue" not in html and "Settled" not in html


def test_paid_partial_and_review_states_use_cutoff_and_exclude_drafts(account):
    ledger, tenancy = account
    ledger.prepare_rent_drafts("2026-03-01")
    jan_rent = _bill(ledger, tenancy, "rent", 100_000, "2026-01-01", "2026-01-31")
    jan_electricity = _bill(ledger, tenancy, "electricity", 10_000, "2026-01-01", "2026-01-31")
    feb_rent = _bill(ledger, tenancy, "rent", 100_000, "2026-02-01", "2026-02-28")
    ledger.record_payment(tenancy, 110_000, "2026-01-10", "cash", allocations={jan_rent: 100_000, jan_electricity: 10_000})
    _legacy_partial_payment(ledger, tenancy, feb_rent, 40_000, "2026-02-10")
    ledger.record_payment(tenancy, 60_000, "2026-03-01", "cash", allocations={feb_rent: 60_000})
    snapshot = ledger.statement(tenancy, "2026-01-01", "2026-02-28", as_of="2026-02-28")
    january, february = snapshot["billing_cycles"]
    assert january["status"] == "paid"
    assert january["billed_paise"] == january["paid_paise"] == 110_000
    assert january["pending_paise"] == 0
    assert february["payment_status"] == "unpaid"
    assert february["review_pending_kinds"] == ["electricity"]
    assert february["pending_paise"] == 60_000
    assert february["electricity_paise"] is None
    assert snapshot["closing_balance_paise"] == 60_000
    html = statement_html(snapshot)
    assert "Partially paid" in html and "Review pending" in html and "Amount Paid" in html
    assert "₹600.00" in html


def test_cycle_overlapping_start_includes_bills_issued_before_start(account):
    ledger, tenancy = account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-02-01", "2026-02-28")
    _bill(ledger, tenancy, "electricity", 10_000, "2026-02-01", "2026-02-28")
    _legacy_partial_payment(ledger, tenancy, rent, 40_000, "2026-02-10")
    snapshot = ledger.statement(tenancy, "2026-02-15", "2026-02-28", as_of="2026-02-28")
    assert snapshot["charges"] == []
    assert snapshot["period_charges_paise"] == 0
    cycle, = snapshot["billing_cycles"]
    assert cycle["billed_paise"] == 110_000
    assert cycle["paid_paise"] == 40_000
    assert cycle["pending_paise"] == 70_000
    assert snapshot["opening_balance_paise"] == snapshot["closing_balance_paise"] == 70_000
    html = statement_html(snapshot)
    assert "Earlier bills still pending" not in html  # these same bills are already detailed
    assert "₹700.00" in html


def test_later_cancellation_does_not_rewrite_historical_cycle(account):
    ledger, tenancy = account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-01-01", "2026-01-31")
    cancel_historical_bill(ledger, rent, "Waived later", "2026-02-01")
    january = ledger.statement(tenancy, "2026-01-01", "2026-01-31", as_of="2026-01-31")
    assert january["billing_cycles"][0]["rent_paise"] == 100_000
    assert january["billing_cycles"][0]["payment_status"] == "unpaid"
    february = ledger.statement(tenancy, "2026-01-01", "2026-02-28", as_of="2026-02-28")
    assert february["billing_cycles"][0]["rent_paise"] is None
    assert february["closing_balance_paise"] == 0
    assert "Waived later" in statement_html(february)


def test_cancelled_monthly_bills_are_reviewed_not_missing(account):
    ledger, tenancy = account
    ledger.prepare_rent_drafts("2026-01-31")
    charges = ledger.list_charges(tenancy)
    electricity = next((row["id"] for row in charges if row["kind"] == "electricity"), None)
    if electricity is None:
        electricity = ledger.add_charge(tenancy, "electricity", None, "2026-01-01", "2026-01-31", "2026-01-31", draft=True)
    rent = next(row["id"] for row in charges if row["kind"] == "rent")
    cancel_historical_bill(ledger, rent, "No rent due by agreement", "2026-01-31")
    cancel_historical_bill(ledger, electricity, "No electricity charge by agreement", "2026-01-31")
    snapshot = ledger.statement(tenancy, "2026-01-01", "2026-01-31", as_of="2026-01-31")
    cycle, = snapshot["billing_cycles"]
    assert cycle["status"] == "cancelled"
    assert cycle["cancelled_kinds"] == ["rent", "electricity"]
    assert not cycle["review_pending"]
    assert cycle["billed_paise"] == cycle["pending_paise"] == 0
    assert snapshot["closing_balance_paise"] == 0
    html = statement_html(snapshot)
    assert "Cancelled" in html
    assert "Review pending" not in html


def test_draft_cancelled_after_cutoff_was_still_pending_at_cutoff(account):
    ledger, tenancy = account
    electricity = ledger.add_charge(tenancy, "electricity", None, "2026-01-01", "2026-01-31", "2026-01-31", draft=True)
    cancel_historical_bill(ledger, electricity, "No electricity charge by agreement", "2026-02-01")
    january = ledger.statement(tenancy, "2026-01-01", "2026-01-31", as_of="2026-01-31")
    assert january["billing_cycles"][0]["electricity_status"] == "review_pending"
    assert january["billing_details"][0]["status"] == "draft"
    february = ledger.statement(tenancy, "2026-01-01", "2026-02-28", as_of="2026-02-28")
    assert february["billing_cycles"][0]["electricity_status"] == "cancelled"


def test_cancelled_drafts_outside_occupancy_do_not_invent_months():
    tenancy = {"move_in": "2026-01-01", "opening_date": "2026-01-01", "move_out": "2026-01-31", "billing_day": 1}
    cancelled = {"id": 1, "kind": "rent", "status": "void", "amount_paise": 100_000, "paid_paise": 0, "outstanding_paise": 0, "period_start": "2026-02-01", "period_end": "2026-02-28", "cycle_start": "2026-02-01"}
    cycles = billing_cycles(tenancy, "2026-01-01", "2026-03-31", [cancelled])
    assert len(cycles) == 1
    assert cycles[0]["period_start"] == "2026-01-01"
    assert cycles[0]["charge_ids"] == []


def test_statement_advance_is_separate_and_amount_left_to_pay_is_nonnegative(account):
    ledger, tenancy = account
    ledger.record_payment(tenancy, 50_000, "2026-01-10", "cash", allocations={})
    snapshot = ledger.statement(tenancy, "2026-01-01", "2026-01-31", as_of="2026-01-31")
    html = statement_html(snapshot)
    assert snapshot["closing_balance_paise"] == -50_000
    assert "Advance remaining after bills" in html
    assert "Amount left to pay</td><td>₹0.00" in html


def test_different_service_dates_count_once_and_final_cycle_is_clipped():
    tenancy = {"move_in": "2026-01-05", "opening_date": "2026-01-05", "move_out": "2026-03-20", "billing_day": 5}
    electricity = {"id": 1, "kind": "electricity", "status": "posted", "amount_paise": 15_000, "paid_paise": 0, "outstanding_paise": 15_000, "period_start": "2026-01-20", "period_end": "2026-02-19"}
    cycles = billing_cycles(tenancy, "2026-01-05", "2026-04-30", [electricity])
    assert len(cycles) == 3
    assert cycles[-1]["period_end"] == "2026-03-20"
    assert cycles[-1]["partial"]
    assert sum(row["billed_paise"] for row in cycles) == 15_000
    assert cycles[0]["different_service_dates"]


def test_csv_includes_all_cycles_and_friendly_status_columns(account, tmp_path):
    ledger, tenancy = account
    rent = _bill(ledger, tenancy, "rent", 100_000, "2026-01-01", "2026-01-31")
    ledger.record_payment(tenancy, 100_000, "2026-01-10", "cash", allocations={rent: 100_000})
    snapshot = ledger.statement(tenancy, "2026-01-01", "2026-03-31", as_of="2026-03-31")
    paths = export_csv(snapshot, tmp_path / "csv")
    with next(path for path in paths if path.name.endswith("-billing-cycles.csv")).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["amount_paid_inr"] == "1000.00"
    assert rows[0]["payment_status"] == "Paid"
    assert rows[0]["review_pending"] == "True"
    assert rows[1]["electricity_inr"] == ""
    with next(path for path in paths if path.name.endswith("-charges.csv")).open(encoding="utf-8-sig", newline="") as handle:
        charge = next(csv.DictReader(handle))
    assert charge["payment_status"] == "Paid"
    assert charge["amount_paid_inr"] == "1000.00"


def test_bilingual_cycle_pdf_keeps_statuses_and_real_money(account, tmp_path, qapp):
    import pymupdf
    from PySide6.QtCore import QEventLoop, QTimer
    from grihalekha.reports import PdfExportJob

    ledger, tenancy = account
    _bill(ledger, tenancy, "rent", 100_000, "2026-01-01", "2026-01-31")
    snapshot = ledger.statement(tenancy, "2026-01-01", "2026-06-30", as_of="2026-06-30")
    destination = tmp_path / "cycles.pdf"
    job = PdfExportJob(statement_html(snapshot, "bilingual", "GL-CYCLE-TEST"), destination, renderer="native")
    loop = QEventLoop()
    result = {}
    job.finished.connect(lambda path: (result.update(path=path), loop.quit()))
    job.failed.connect(lambda error: (result.update(error=error), loop.quit()))
    QTimer.singleShot(0, job.start)
    QTimer.singleShot(10_000, loop.quit)
    loop.exec()
    assert result.get("path") == str(destination.resolve()), result
    with pymupdf.open(destination) as document:
        text = "\n".join(page.get_text() for page in document)
        assert "GL-CYCLE-TEST" in text
        assert "Review pending" in text and "Unpaid" in text
        assert "₹1,000.00" in text
        assert "Closing account balance" not in text
        for page in document:
            bottom = page.rect.height - 15 * 72 / 25.4
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        assert span["bbox"][3] <= bottom + 1, "Glyphs bleed into the bottom margin from the next page"
