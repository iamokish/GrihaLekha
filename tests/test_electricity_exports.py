"""Meter information and deferred electricity remain separate from money."""

import copy
import csv

import pytest

from grihalekha.reports import PdfExportJob, export_csv, receipt_html, statement_html


@pytest.fixture
def meter_snapshot():
    electricity = {
        "id": 3, "kind": "electricity", "amount_paise": 12345,
        "paid_paise": 0, "outstanding_paise": 12345,
        "period_start": "2026-03-01", "period_end": "2026-03-31",
        "electricity_cycle_start": "2026-03-01", "charge_date": "2026-03-31",
        "due_date": "2026-03-31", "status": "posted", "description": "Monthly electricity",
        "meter_reading_hundredths": 130045, "electricity_deferred": False,
        "coverage_period_start": "2026-03-01", "coverage_period_end": "2026-03-31",
    }
    rent = {
        "id": 4, "kind": "rent", "amount_paise": 100000,
        "paid_paise": 0, "outstanding_paise": 100000,
        "period_start": "2026-03-01", "period_end": "2026-03-31",
        "cycle_start": "2026-03-01", "charge_date": "2026-03-31",
        "due_date": "2026-03-31", "status": "posted", "description": "Monthly rent",
    }
    cycle = {
        "cycle_start": "2026-03-01", "period_start": "2026-03-01", "period_end": "2026-03-31",
        "rent_paise": 100000, "electricity_paise": 12345, "misc_paise": 0,
        "billed_paise": 112345, "paid_paise": 0, "pending_paise": 112345,
        "payment_status": "unpaid", "review_pending": False, "review_pending_kinds": [],
        "electricity_status": "unpaid", "electricity_deferred": False,
    }
    return {
        "tenancy": {
            "id": 1, "tenant_name": "Sample Tenant", "building_name": "Sample House",
            "address": "Sample Road", "number": "101", "floor": "1", "bhk": 2,
            "move_in": "2025-12-17", "initial_meter_reading_hundredths": 100025,
        },
        "start": "2026-03-01", "end": "2026-03-31", "generated_at": "2026-04-01T12:00:00+05:30",
        "opening_balance_paise": 0, "closing_balance_paise": 112345,
        "balance": {"outstanding_paise": 112345, "overdue_paise": 0, "credit_paise": 0, "deposit_paise": 0},
        "charges": [rent, electricity], "billing_details": [rent, electricity], "billing_cycles": [cycle],
        "payments": [], "refunds": [], "adjustments": [], "deposits": [],
    }


def _csv_rows(paths, suffix):
    path = next(path for path in paths if path.name.endswith(suffix))
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("language,date_text,move_label,initial_label,reading_label", [
    ("en", "17 Dec 2025", "Move-in date", "Initial electricity meter reading", "Meter reading"),
    ("hi", "17 दिसंबर 2025", "रहने की शुरुआत की तारीख", "बिजली मीटर की प्रारंभिक रीडिंग", "मीटर रीडिंग"),
    ("bilingual", "17 Dec 2025", "Move-in date", "Initial electricity meter reading", "Meter reading"),
])
def test_statement_labels_move_in_and_exact_readings(meter_snapshot, language, date_text, move_label, initial_label, reading_label):
    before = copy.deepcopy(meter_snapshot)
    html = statement_html(meter_snapshot, language)
    assert move_label in html and date_text in html
    assert initial_label in html and "1000.25 kWh" in html
    assert reading_label in html and "1300.45 kWh" in html
    assert "₹1,123.45" in html
    assert meter_snapshot == before


@pytest.mark.parametrize("reading,expected", [(None, None), (0, "0.00 kWh"), (1, "0.01 kWh")])
def test_optional_initial_reading_keeps_zero_distinct_from_blank(meter_snapshot, reading, expected):
    meter_snapshot["tenancy"]["initial_meter_reading_hundredths"] = reading
    html = statement_html(meter_snapshot)
    assert "Move-in date" in html
    if expected is None:
        assert "Initial electricity meter reading" not in html
    else:
        assert "Initial electricity meter reading" in html and expected in html


def test_receipt_shares_move_in_and_initial_reading(meter_snapshot):
    meter_snapshot["payments"] = [{"id": 10, "paid_on": "2026-03-31", "amount_paise": 1000, "method": "cash"}]
    html = receipt_html(meter_snapshot, 10)
    assert "Move-in date" in html and "17 Dec 2025" in html
    assert "1000.25 kWh" in html


def test_csv_metadata_and_posted_reading_keep_financial_columns(meter_snapshot, tmp_path):
    paths = export_csv(meter_snapshot, tmp_path)
    summary, = _csv_rows(paths, "-summary.csv")
    assert summary["move_in"] == "2025-12-17"
    assert summary["initial_meter_reading_kwh"] == "1000.25"
    assert summary["closing_balance_inr"] == "1123.45"
    charges = _csv_rows(paths, "-charges.csv")
    assert charges[0]["meter_reading_kwh"] == ""
    assert charges[1]["meter_reading_kwh"] == "1300.45"
    assert charges[1]["amount_inr"] == "123.45"
    detail, = _csv_rows(paths, "-electricity-details.csv")
    assert detail["meter_reading_kwh"] == "1300.45"
    assert detail["bill_pending_inr"] == "123.45"


def test_draft_readings_export_without_changing_posted_charge_totals(meter_snapshot, tmp_path):
    draft = {
        "id": 5, "kind": "electricity", "status": "draft", "amount_paise": None,
        "period_start": "2026-04-01", "period_end": "2026-04-30", "due_date": "2026-04-30",
        "description": "=DANGEROUS()", "meter_reading_hundredths": 140000,
    }
    meter_snapshot["billing_details"].append(draft)
    html = statement_html(meter_snapshot)
    assert "1400.00 kWh" in html and "Review pending" in html
    paths = export_csv(meter_snapshot, tmp_path)
    assert len(_csv_rows(paths, "-charges.csv")) == 2
    details = _csv_rows(paths, "-electricity-details.csv")
    assert details[-1]["meter_reading_kwh"] == "1400.00"
    assert details[-1]["amount_inr"] == details[-1]["bill_pending_inr"] == ""
    assert details[-1]["payment_status"] == "Review pending"
    assert details[-1]["description"] == "'=DANGEROUS()"
    summary, = _csv_rows(paths, "-summary.csv")
    assert summary["closing_balance_inr"] == "1123.45"


def _add_deferrals(snapshot):
    electricity = snapshot["charges"][1]
    electricity.update(coverage_period_start="2026-01-01", electricity_deferred_charge_ids=[1, 2])
    for identifier, start, end in ((1, "2026-01-01", "2026-01-31"), (2, "2026-02-01", "2026-02-28")):
        snapshot["billing_details"].insert(identifier - 1, {
            "id": identifier, "kind": "electricity", "status": "draft", "amount_paise": None,
            "period_start": start, "period_end": end, "due_date": end,
            "electricity_cycle_start": start, "description": "Monthly electricity",
            "electricity_deferred": True, "electricity_covered_by_charge_id": 3,
            "electricity_covered_by_cycle_start": "2026-03-01", "meter_reading_hundredths": None,
        })
        snapshot["billing_cycles"].insert(identifier - 1, {
            "cycle_start": start, "period_start": start, "period_end": end,
            "rent_paise": 0, "electricity_paise": None, "misc_paise": 0,
            "billed_paise": 0, "paid_paise": 0, "pending_paise": 0,
            "payment_status": "paid", "review_pending": False, "review_pending_kinds": [],
            "electricity_status": "deferred", "electricity_deferred": True,
            "electricity_covered_by_charge_ids": [3],
        })
    snapshot["start"] = "2026-01-01"


def test_deferred_and_combined_service_dates_print_without_zero_electricity(meter_snapshot, tmp_path):
    _add_deferrals(meter_snapshot)
    html = statement_html(meter_snapshot)
    assert "Electricity billing deferred" in html
    assert "Included in bill #3" in html
    assert "Billed in cycle starting 01 Mar 2026" in html
    assert "Electricity service period: 01 Jan 2026 – 31 Mar 2026" in html
    assert '<td class="num"><span class="status">Deferred</span></td>' in html
    assert "Review pending" not in html
    paths = export_csv(meter_snapshot, tmp_path)
    details = _csv_rows(paths, "-electricity-details.csv")
    assert len(details) == 3
    assert all(row["meter_reading_kwh"] == row["amount_inr"] == row["bill_pending_inr"] == "" for row in details[:2])
    assert all(row["payment_status"] == "Deferred" and row["electricity_covered_by_charge_id"] == "3" for row in details[:2])
    assert details[2]["coverage_period_start"] == "2026-01-01"
    assert details[2]["included_deferred_charge_ids"] == "1, 2"
    assert details[2]["amount_inr"] == "123.45"
    assert len(_csv_rows(paths, "-charges.csv")) == 2
    cycles = _csv_rows(paths, "-billing-cycles.csv")
    assert all(row["electricity_inr"] == "" and row["electricity_status"] == "Deferred" for row in cycles[:2])
    assert all(row["payment_status"] == "Deferred" for row in cycles[:2])


@pytest.mark.parametrize("status", ["paid", "unpaid", "not_billed"])
def test_deferred_cycle_never_claims_complete_payment(status):
    from grihalekha.reports import _cycle_csv_status, _cycle_status

    cycle = {
        "payment_status": status, "electricity_status": "deferred", "electricity_deferred": True,
        "electricity_covered_by_charge_ids": [3], "review_pending": False,
    }
    html = _cycle_status(cycle, "en")
    assert "Deferred" in html and "Included in bill #3" in html
    assert "Paid" not in html
    if status == "unpaid":
        assert "Unpaid" in html and "Unpaid" in _cycle_csv_status(cycle)
    else:
        assert _cycle_csv_status(cycle) == "Deferred"


def test_legacy_data_does_not_gain_an_extra_electricity_file(meter_snapshot, tmp_path):
    meter_snapshot["tenancy"].pop("initial_meter_reading_hundredths")
    meter_snapshot["charges"][1].pop("meter_reading_hundredths")
    paths = export_csv(meter_snapshot, tmp_path)
    assert len(paths) == 6
    assert not any(path.name.endswith("-electricity-details.csv") for path in paths)


def test_reexport_after_clearing_readings_removes_stale_supplement(meter_snapshot, tmp_path):
    original = export_csv(meter_snapshot, tmp_path)
    supplement = next(path for path in original if path.name.endswith("-electricity-details.csv"))
    meter_snapshot["tenancy"]["initial_meter_reading_hundredths"] = None
    meter_snapshot["charges"][1]["meter_reading_hundredths"] = None
    paths = export_csv(meter_snapshot, tmp_path)
    assert len(paths) == 6
    assert not supplement.exists()
    summary, = _csv_rows(paths, "-summary.csv")
    assert summary["initial_meter_reading_kwh"] == ""


@pytest.fixture
def combined_account(tmp_path):
    from grihalekha.ledger import Ledger

    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Sample House", "Sample Road")
    flat = ledger.add_flat(building, "101", "1", 2)
    tenant = ledger.add_tenancy(flat, "Sample Tenant", "2026-01-01", 1, 0,
                                initial_meter_reading_hundredths=0)
    ledger.prepare_monthly_drafts("2026-03-01")
    electricity = []
    for bill in ledger.list_charges(tenant):
        if bill["kind"] == "rent":
            ledger.post_charge(bill["id"], amount_paise=0)
        else:
            electricity.append(bill["id"])
    ledger.set_electricity_deferred(electricity[0], True)
    ledger.set_electricity_deferred(electricity[1], True)
    ledger.post_charge(electricity[2], amount_paise=50000, meter_reading_hundredths=123456)
    yield ledger, tenant, electricity
    ledger.close()


def test_live_combined_bill_exports_once_in_its_receiving_cycle(combined_account, tmp_path):
    ledger, tenant, electricity = combined_account
    snapshot = ledger.statement(tenant, "2026-01-01", "2026-03-31", as_of="2026-03-31")
    assert len(snapshot["billing_cycles"]) == 3
    assert [row["electricity_status"] for row in snapshot["billing_cycles"]] == ["deferred", "deferred", "unpaid"]
    assert sum(row["billed_paise"] for row in snapshot["billing_cycles"]) == 50000
    html = statement_html(snapshot)
    assert "Electricity service period: 01 Jan 2026 – 31 Mar 2026" in html
    assert "1234.56 kWh" in html and "0.00 kWh" in html
    paths = export_csv(snapshot, tmp_path / "csv")
    details = _csv_rows(paths, "-electricity-details.csv")
    assert [row["amount_inr"] for row in details] == ["", "", "500.00"]
    assert details[2]["coverage_period_start"] == "2026-01-01"
    assert details[2]["included_deferred_charge_ids"] == f"{electricity[0]}, {electricity[1]}"
    january = ledger.statement(tenant, "2026-01-01", "2026-01-31", as_of="2026-03-31")
    assert len(january["billing_cycles"]) == 1
    assert january["billing_cycles"][0]["electricity_paise"] is None
    assert january["period_charges_paise"] == 0
    assert january["closing_balance_paise"] == 50000
    assert "Electricity service period: 01 Jan 2026 – 31 Mar 2026" in statement_html(january)


def test_updated_reading_changes_new_export_without_rewriting_old_snapshot(combined_account):
    ledger, tenant, electricity = combined_account
    before = ledger.statement(tenant, "2026-01-01", "2026-03-31", as_of="2026-03-31")
    ledger.set_meter_reading(electricity[2], 123457)
    after = ledger.statement(tenant, "2026-01-01", "2026-03-31", as_of="2026-03-31")
    assert "1234.56 kWh" in statement_html(before)
    assert "1234.57 kWh" in statement_html(after)
    assert after["closing_balance_paise"] == before["closing_balance_paise"] == 50000


def test_corrected_combined_bill_retains_historical_service_period(combined_account):
    ledger, tenant, electricity = combined_account
    replacement = ledger.revise_bill_amount(electricity[2], 55000, "Correct utility amount", effective_on="2026-04-01")
    historical = ledger.statement(tenant, "2026-01-01", "2026-03-31", as_of="2026-03-31")
    original = next(row for row in historical["billing_details"] if row["id"] == electricity[2])
    assert original["coverage_period_start"] == "2026-01-01"
    assert historical["closing_balance_paise"] == 50000
    assert "Electricity service period: 01 Jan 2026 – 31 Mar 2026" in statement_html(historical)
    current = ledger.statement(tenant, "2026-01-01", "2026-03-31", as_of="2026-04-01")
    revised = next(row for row in current["billing_details"] if row["id"] == replacement)
    assert revised["coverage_period_start"] == "2026-01-01"
    assert revised["meter_reading_hundredths"] == 123456
    assert current["closing_balance_paise"] == 55000


def test_native_pdf_contains_move_in_meter_and_combined_service_period(meter_snapshot, tmp_path, qapp):
    import pymupdf
    from PySide6.QtCore import QEventLoop, QTimer

    _add_deferrals(meter_snapshot)
    destination = tmp_path / "meter-statement.pdf"
    job = PdfExportJob(statement_html(meter_snapshot, "bilingual", "GL-METER-TEST"), destination, renderer="native")
    loop = QEventLoop()
    result = {}
    job.finished.connect(lambda path: (result.update(path=path), loop.quit()))
    job.failed.connect(lambda error: (result.update(error=error), loop.quit()))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(10000)
    QTimer.singleShot(0, job.start)
    loop.exec()
    timer.stop()
    assert result.get("path") == str(destination.resolve()), result
    with pymupdf.open(destination) as document:
        text = "\n".join(page.get_text() for page in document)
        assert "GL-METER-TEST" in text and "Move-in date" in text and "17 Dec 2025" in text
        assert "1000.25 kWh" in text and "1300.45 kWh" in text
        assert "Deferred" in text and "Included in bill" in text and "#3" in text
        assert "Electricity service period" in text
        for page in document:
            bottom = page.rect.height - 15 * 72 / 25.4
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        assert span["bbox"][3] <= bottom + 1
