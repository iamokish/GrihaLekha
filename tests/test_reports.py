from __future__ import annotations

import copy
import csv
import os
import pytest

from grihalekha.reports import export_csv, receipt_html, statement_html


@pytest.fixture
def snapshot():
    return {
        "tenancy": {"id": 7, "tenant_name": "अमित कुमार", "building_name": "कृष्णा निवास", "address": "12 उदाहरण मार्ग\nनई दिल्ली", "number": "201", "floor": "2", "bhk": 2},
        "start": "2026-02-05", "end": "2026-03-04", "generated_at": "2026-03-04T18:00:00+05:30",
        "opening_balance_paise": 0, "closing_balance_paise": 100000,
        "balance": {"outstanding_paise": 100000, "overdue_paise": 0, "credit_paise": 0, "deposit_paise": 0},
        "charges": [
            {"id": 1, "kind": "rent", "amount_paise": 1000000, "paid_paise": 1000000, "outstanding_paise": 0, "period_start": "2026-02-05", "period_end": "2026-03-04", "charge_date": "2026-03-04", "due_date": "2026-03-04", "status": "posted", "description": "Monthly rent"},
            {"id": 2, "kind": "electricity", "amount_paise": 80000, "paid_paise": 0, "outstanding_paise": 80000, "period_start": "2026-02-05", "period_end": "2026-03-04", "charge_date": "2026-03-04", "due_date": "2026-03-04", "status": "posted", "description": "बिजली बिल"},
            {"id": 3, "kind": "misc", "amount_paise": 20000, "paid_paise": 0, "outstanding_paise": 20000, "period_start": "2026-02-05", "period_end": "2026-03-04", "charge_date": "2026-03-04", "due_date": "2026-03-04", "status": "posted", "description": "दरवाज़े के ताले की मरम्मत"},
        ],
        "payments": [{"id": 4, "paid_on": "2026-03-04", "method": "upi", "reference": "UPI-123", "notes": "किराये का भुगतान", "amount_paise": 1000000, "available_paise": 0, "proof_path": "attachments/proof.png", "allocations": [{"charge_id": 1, "amount_paise": 1000000, "effective_on": "2026-03-04"}]}],
        "refunds": [], "adjustments": [], "deposits": [],
    }


@pytest.mark.parametrize("language,expected", [("en", "Account statement"), ("hi", "खाता विवरण"), ("bilingual", "Account statement<span")])
def test_language_templates_embed_hindi_fonts_and_exact_rupees(snapshot, language, expected):
    html = statement_html(snapshot, language, document_id="GL-TEST-001")
    assert expected in html
    assert "अमित कुमार" in html
    assert "₹10,000.00" in html
    assert "₹1,000.00" in html
    assert "दरवाज़े के ताले की मरम्मत" in html
    assert "data:font/ttf;base64," in html
    assert "GL Devanagari" in html
    assert "GL-TEST-001" in html
    assert "https://" not in html


def test_renderer_escapes_untrusted_names_and_reasons(snapshot):
    snapshot["tenancy"]["tenant_name"] = '<script>alert("x")</script>'
    snapshot["charges"][2]["description"] = '<img src="https://example.com/tracker"> & repair'
    html = statement_html(snapshot)
    assert "<script>alert" not in html
    assert "<img src=" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; repair" in html
    assert "default-src 'none'" in html


@pytest.mark.parametrize("language,month", [("en", "Nov"), ("hi", "नवंबर"), ("bilingual", "Nov")])
def test_issued_and_balance_timestamps_use_ist_with_date_rollover(snapshot, language, month):
    snapshot["generated_at"] = "2026-11-06T20:45:12+00:00"
    snapshot["balance_calculated_at"] = snapshot["generated_at"]
    snapshot["end"] = "2027-01-07"
    html = statement_html(snapshot, language)
    assert html.count(f"07 {month} 2026, 02:15:12 IST") == 2
    assert f"07 {month} 2026, 02:15:12 IST" in receipt_html(snapshot, 4, language)


def test_paid_and_unpaid_colors_survive_native_html_conversion(snapshot):
    from grihalekha.reports import _native_html

    html = statement_html(snapshot, "bilingual")
    assert '<span class="payment-paid">Paid<span class="translation">' in html
    assert '<span class="payment-unpaid">Unpaid<span class="translation">' in html
    native, _ = _native_html(html)
    assert '<span style="color:#16723c;font-weight:bold">Paid / <span style="color:#16723c' in native
    assert '<span style="color:#ba2525;font-weight:bold">Unpaid / <span style="color:#ba2525' in native


def test_cycle_review_takes_priority_over_paid_status():
    from grihalekha.reports import _cycle_status

    row = {"payment_status": "paid", "review_pending": True, "review_pending_kinds": ["electricity"]}
    html = _cycle_status(row, "en")
    assert "Review pending" in html
    assert "Paid" not in html
    row["payment_status"] = "unpaid"
    html = _cycle_status(row, "en")
    assert html.index("Review pending") < html.index("Unpaid")


@pytest.mark.parametrize("legacy,clean", [
    ("Monthly electricity — enter the bill amount", "Monthly electricity"),
    ("Partial period — enter the agreed rent", "Rent for partial period"),
])
def test_legacy_posted_bill_instructions_are_not_printed(snapshot, legacy, clean):
    snapshot["charges"][0]["description"] = legacy
    html = statement_html(snapshot)
    assert legacy not in html
    assert clean in html
    assert snapshot["charges"][0]["description"] == legacy


def test_renderer_rejects_non_reconciling_snapshot(snapshot):
    snapshot["closing_balance_paise"] += 1
    with pytest.raises(ValueError, match="reconcile"):
        statement_html(snapshot)


def test_original_cancelled_bill_and_signed_correction_are_both_shown(snapshot):
    snapshot["charges"][2].update(status="void", outstanding_paise=0)
    snapshot["adjustments"] = [{"amount_paise": -20000, "effective_on": "2026-03-04", "kind": "void_charge", "reason": "Duplicate repair charge"}]
    snapshot["closing_balance_paise"] -= 20000
    snapshot["balance"]["outstanding_paise"] -= 20000
    html = statement_html(snapshot)
    assert "Cancelled" in html
    assert "Duplicate repair charge" in html
    assert "−₹200.00" in html
    assert "₹11,000.00" in html  # original posted total is preserved


def test_deposit_is_separate_from_statement_reconciliation(snapshot):
    snapshot["deposits"] = [{"amount_paise": 100000, "paid_on": "2026-03-04", "action": "receive", "notes": "Refundable deposit"}]
    snapshot["balance"]["deposit_paise"] = 100000
    html = statement_html(snapshot)
    assert "Deposit movements" in html
    assert "Refundable deposit" in html
    assert "Security deposits are separate" in html


def test_missing_electricity_is_not_reported_as_confirmed_zero(snapshot):
    snapshot["charges"] = [snapshot["charges"][0]]
    snapshot["closing_balance_paise"] = 0
    snapshot["balance"]["outstanding_paise"] = 0
    html = statement_html(snapshot)
    assert '<td class="num">—</td>' in html


def test_receipt_contains_actual_date_method_and_selected_allocation(snapshot):
    html = receipt_html(snapshot, 4, language="bilingual", document_id="GL-RECEIPT")
    assert "Payment receipt" in html
    assert "भुगतान रसीद" in html
    assert "04 Mar 2026" in html
    assert "UPI-123" in html
    assert "Bill<span" in html and "#1" in html
    assert "GL-RECEIPT" in html
    assert "attachments/proof.png" not in html
    with pytest.raises(ValueError, match="not in"):
        receipt_html(snapshot, 999)


def test_reports_reject_binary_float_money_and_invalid_date_ranges(snapshot):
    bad = copy.deepcopy(snapshot)
    bad["charges"][0]["amount_paise"] = 1.1
    with pytest.raises(ValueError, match="integer paise"):
        statement_html(bad)
    bad = copy.deepcopy(snapshot)
    bad["end"] = "2025-01-01"
    with pytest.raises(ValueError, match="start date"):
        statement_html(bad)
    with pytest.raises(ValueError, match="language"):
        statement_html(snapshot, "fr")


def test_csv_preserves_unicode_exact_decimal_and_independent_tables(snapshot, tmp_path):
    paths = export_csv(snapshot, tmp_path)
    assert len(paths) == 5
    for path in paths:
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    charges_path = next(path for path in paths if path.name.endswith("-charges.csv"))
    with charges_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["amount_inr"] == "10000.00"
    assert rows[0]["tenant"] == "अमित कुमार"
    assert rows[2]["description"] == "दरवाज़े के ताले की मरम्मत"
    allocations_path = next(path for path in paths if path.name.endswith("-allocations.csv"))
    with allocations_path.open(encoding="utf-8-sig", newline="") as handle:
        allocations = list(csv.DictReader(handle))
    assert allocations[0]["payment_id"] == "4"
    assert allocations[0]["amount_inr"] == "10000.00"


@pytest.mark.parametrize("dangerous", ["=HYPERLINK(\"https://example.com\")", "  +SUM(1,2)", "\t@SUM(1)", "-1+2", "\r=1+1", "\x00=1+1"])
def test_csv_prevents_formula_injection_in_user_text(snapshot, tmp_path, dangerous):
    snapshot["charges"][2]["description"] = dangerous
    paths = export_csv(snapshot, tmp_path)
    with next(path for path in paths if path.name.endswith("-charges.csv")).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[2]["description"] == "'" + dangerous


def test_negative_summary_numbers_remain_numeric_csv(snapshot, tmp_path):
    snapshot["closing_balance_paise"] = -12345
    paths = export_csv(snapshot, tmp_path)
    with next(path for path in paths if path.name.endswith("-summary.csv")).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["closing_balance_inr"] == "-123.45"


def test_csv_cannot_use_an_external_path_from_tenancy_identifier(snapshot, tmp_path):
    snapshot["tenancy"]["id"] = "../../escape"
    with pytest.raises(ValueError, match="identifier"):
        export_csv(snapshot, tmp_path)


def test_real_ledger_statement_preserves_cutoff_and_reconciles_corrections(tmp_path):
    from grihalekha.ledger import Ledger

    ledger = Ledger(tmp_path / "accounts")
    try:
        building = ledger.add_building("कृष्णा निवास", "नई दिल्ली")
        flat = ledger.add_flat(building, "201", "2", 2)
        tenancy = ledger.add_tenancy(flat, "अमित कुमार", "2026-02-05", 5, 1000000)
        rent = ledger.add_charge(tenancy, "rent", 1000000, "2026-02-05", "2026-03-04", "2026-03-04", charge_date="2026-03-04")
        ledger.add_charge(tenancy, "electricity", 80000, "2026-02-05", "2026-03-04", "2026-03-04", charge_date="2026-03-04")
        repair = ledger.add_charge(tenancy, "misc", 20000, "2026-02-05", "2026-03-04", "2026-03-04", "ताले की मरम्मत", charge_date="2026-03-04")
        paid = ledger.record_payment(tenancy, 1000000, "2026-03-04", "upi", allocations={rent: 1000000})
        ledger.record_payment(tenancy, 50000, "2026-04-07", "cash")
        ledger.void_charge(repair, "Repair charge waived", "2026-04-08")
        ledger.record_deposit(tenancy, 500000, "2026-04-10", "receive")

        march = ledger.statement(tenancy, "2026-02-05", "2026-03-31", as_of="2026-03-31")
        april = ledger.statement(tenancy, "2026-04-01", "2026-04-30", as_of="2026-04-30")
        assert march["closing_balance_paise"] == 100000
        assert april["opening_balance_paise"] == 100000
        assert april["closing_balance_paise"] == 30000
        assert april["balance"]["deposit_paise"] == 500000
        assert "₹1,000.00" in statement_html(march)
        html = statement_html(april, "bilingual")
        assert "Earlier bills still pending" in html
        assert "Repair charge waived" in html
        assert "−₹200.00" in html
        assert "₹300.00" in html
        assert "Payment receipt" in receipt_html(march, paid)
        assert len(export_csv(april, tmp_path / "exports")) == 6
    finally:
        ledger.close()


def test_real_ledger_advance_refund_and_payment_reversal_report(tmp_path):
    from grihalekha.ledger import Ledger

    ledger = Ledger(tmp_path / "accounts")
    try:
        building = ledger.add_building("A", "Address")
        flat = ledger.add_flat(building, "1", "1", 1)
        tenancy = ledger.add_tenancy(flat, "Tenant", "2026-02-01", 1, 1000000)
        ledger.record_payment(tenancy, 150000, "2026-02-01", "bank", allocations={})
        ledger.refund_credit(tenancy, 50000, "2026-02-02", "cash", "Unused advance returned")
        failed = ledger.record_payment(tenancy, 10000, "2026-02-03", "upi", allocations={})
        ledger.reverse_payment(failed, "Transfer failed", "2026-02-04")
        snapshot = ledger.statement(tenancy, "2026-02-01", "2026-02-28", as_of="2026-02-28")
        html = statement_html(snapshot, "hi")
        assert "Unused advance returned" in html
        assert "Transfer failed" in html
        assert "भुगतान प्रविष्टि वापस ली गई" in html
        assert snapshot["closing_balance_paise"] == -100000
        assert "बिलों के बाद बची अग्रिम राशि" in html
        assert "₹1,000.00" in html
    finally:
        ledger.close()


@pytest.fixture(scope="module")
def pdf_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.mark.parametrize("language", ["en", "hi", "bilingual"])
def test_native_pdf_is_real_with_embedded_fonts_and_exact_currency(snapshot, tmp_path, pdf_app, language):
    import pymupdf
    from PySide6.QtCore import QEventLoop, QTimer
    from grihalekha.reports import PdfExportJob

    destination = tmp_path / f"statement-{language}.pdf"
    job = PdfExportJob(statement_html(snapshot, language, "GL-NATIVE-TEST"), destination, renderer="native")
    loop = QEventLoop()
    result = {}
    job.finished.connect(lambda path: (result.update(path=path), loop.quit()))
    job.failed.connect(lambda error: (result.update(error=error), loop.quit()))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(10_000)
    QTimer.singleShot(0, job.start)
    loop.exec()
    timer.stop()
    assert result.get("path") == str(destination.resolve()), result
    assert job.used_renderer == "native"
    assert not job.used_native_fallback
    with pymupdf.open(destination) as document:
        assert len(document) >= 1
        text = "\n".join(page.get_text() for page in document)
        assert "GL-NATIVE-TEST" in text
        assert "₹1,000.00" in text
        assert "base64" not in text and "<table" not in text
        fonts = {font[0] for page in document for font in page.get_fonts()}
        assert fonts
        assert all(len(document.extract_font(xref)[3]) > 0 for xref in fonts)
        colored_text = {
            span["color"]
            for page in document for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", []) for span in line.get("spans", [])
            if span["text"].strip()
        }
        assert 0x16723C in colored_text, "Paid status must be printed in green"
        assert 0xBA2525 in colored_text, "Unpaid status must be printed in red"


def test_native_pdf_reports_write_failure_without_replacing_destination(snapshot, tmp_path, pdf_app):
    from PySide6.QtCore import QEventLoop, QTimer
    from grihalekha.reports import PdfExportJob

    destination = tmp_path / "cannot-replace.pdf"
    destination.mkdir()
    job = PdfExportJob(statement_html(snapshot), destination, renderer="native")
    loop = QEventLoop()
    result = {}
    job.finished.connect(lambda path: (result.update(path=path), loop.quit()))
    job.failed.connect(lambda error: (result.update(error=error), loop.quit()))
    QTimer.singleShot(0, job.start)
    loop.exec()
    assert "error" in result and "path" not in result
    assert destination.is_dir()

