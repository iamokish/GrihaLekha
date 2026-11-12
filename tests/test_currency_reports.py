"""Currency selection changes report labels while preserving exact values."""

import copy
import csv
import io
import json
import zipfile

import pytest

from grihalekha.currencies import CURRENCIES
from grihalekha.ledger import Ledger
from grihalekha.reports import PdfExportJob, export_csv, receipt_html, statement_html
from grihalekha.tenancy_deletion import tenancy_deletion_html


@pytest.fixture
def currency_snapshot():
    def charge(identifier, kind, amount, paid, start="2026-02-01", end="2026-02-28"):
        return {
            "id": identifier, "kind": kind, "amount_paise": amount,
            "paid_paise": paid, "outstanding_paise": amount - paid,
            "period_start": start, "period_end": end, "charge_date": end,
            "due_date": end, "status": "posted", "description": "Sample bill",
        }

    rent = charge(1, "rent", 123_456_789, 123_456_789)
    electricity = dict(charge(2, "electricity", 12_345, 0), meter_reading_hundredths=130_045)
    return {
        "tenancy": {
            "id": 7, "tenant_name": "Sample Tenant", "building_name": "Sample House",
            "number": "101", "move_in": "2026-01-01", "initial_meter_reading_hundredths": 100_025,
        },
        "start": "2026-02-01", "end": "2026-02-28", "balance_as_of": "2026-03-20",
        "generated_at": "2026-03-20T12:00:00+05:30", "document_id": "GL-CURRENCY-TEST",
        "opening_balance_paise": 2_000, "period_closing_balance_paise": 14_395,
        "closing_balance_paise": 14_895,
        "balance": {"outstanding_paise": 14_895, "overdue_paise": 0, "credit_paise": 0, "deposit_paise": 98_765},
        "charges": [rent, electricity], "billing_details": [rent, electricity],
        "outstanding_charges": [
            charge(3, "rent", 2_000, 0, "2026-01-01", "2026-01-31"),
            charge(4, "misc", 500, 0, "2026-03-01", "2026-03-15"),
        ],
        "billing_cycles": [{
            "cycle_start": "2026-02-01", "period_start": "2026-02-01", "period_end": "2026-02-28",
            "rent_paise": 123_456_789, "electricity_paise": 12_345, "misc_paise": 0,
            "billed_paise": 123_469_134, "paid_paise": 123_456_789, "pending_paise": 12_345,
            "payment_status": "unpaid", "review_pending": False, "review_pending_kinds": [],
        }],
        "payments": [{
            "id": 5, "paid_on": "2026-02-28", "amount_paise": 123_456_789, "method": "cash",
            "available_paise": 0, "reference": "Payment reference", "notes": "Sample payment",
            "allocations": [{"charge_id": 1, "amount_paise": 123_456_789, "effective_on": "2026-02-28"}],
        }],
        "refunds": [{"id": 6, "paid_on": "2026-02-28", "amount_paise": 100, "method": "cash"}],
        "adjustments": [{"effective_on": "2026-02-28", "amount_paise": -50, "kind": "void_charge"}],
        "deposits": [{"id": 8, "paid_on": "2026-02-28", "amount_paise": 98_765, "action": "receive"}],
    }


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda item: item.code)
@pytest.mark.parametrize("language", ["en", "hi", "bilingual"])
def test_every_document_amount_changes_only_its_symbol(currency_snapshot, currency, language):
    default_statement = statement_html(currency_snapshot, language)
    default_receipt = receipt_html(currency_snapshot, 5, language)
    currency_snapshot["currency_code"] = currency.code
    before = copy.deepcopy(currency_snapshot)
    for default, result in (
        (default_statement, statement_html(currency_snapshot, language)),
        (default_receipt, receipt_html(currency_snapshot, 5, language)),
    ):
        default_body, _ = default.split('<div class="footer">')
        body, footer = result.split('<div class="footer">')
        assert body == default_body.replace("₹", currency.symbol)
        assert currency.code in footer
        assert f"{currency.symbol}12,34,567.89" in body
        if currency.code != "INR":
            assert "INR" not in footer and "Indian rupees" not in footer
    assert currency_snapshot == before


def _csv_tables(paths):
    tables = {}
    for path in paths:
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            tables[path.name] = (reader.fieldnames, rows)
    return tables


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda item: item.code)
def test_all_csv_values_and_meter_readings_survive_currency_changes(currency_snapshot, currency, tmp_path):
    currency_snapshot["charges"][0]["description"] = "  =1+2"
    currency_snapshot["closing_balance_paise"] = -12_345
    default = _csv_tables(export_csv(currency_snapshot, tmp_path / "default"))
    currency_snapshot["currency_code"] = currency.code
    before = copy.deepcopy(currency_snapshot)
    actual = _csv_tables(export_csv(currency_snapshot, tmp_path / currency.code))
    assert len(actual) == 7 and actual.keys() == default.keys()
    for name, (fields, rows) in actual.items():
        def original_key(key):
            suffix = "_" + currency.code.lower()
            return key.removesuffix(suffix) + "_inr" if key.endswith(suffix) else key

        assert [original_key(key) for key in fields] == default[name][0]
        assert all(row["currency_code"] == currency.code for row in rows)
        normalized = [
            {original_key(key): "INR" if key == "currency_code" else value for key, value in row.items()}
            for row in rows
        ]
        assert normalized == default[name][1]
    summary = next(rows[0] for name, (_, rows) in actual.items() if name.endswith("-summary.csv"))
    assert summary[f"closing_balance_{currency.code.lower()}"] == "-123.45"
    assert summary["initial_meter_reading_kwh"] == "1000.25"
    assert currency_snapshot == before


@pytest.mark.parametrize("invalid", [None, "usd", "CHF", "USD<script>", 42])
def test_invalid_snapshot_currency_fails_before_export(currency_snapshot, invalid, tmp_path):
    currency_snapshot["currency_code"] = invalid
    for render in (statement_html, lambda snapshot: receipt_html(snapshot, 5)):
        with pytest.raises(ValueError, match="currency"):
            render(currency_snapshot)
    with pytest.raises(ValueError, match="currency"):
        export_csv(currency_snapshot, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_currency_selection_preserves_currency_mentions_in_user_text(currency_snapshot, tmp_path):
    currency_snapshot["currency_code"] = "USD"
    note = "Original agreement: ₹123.45 INR; current label is a display preference."
    currency_snapshot["charges"][0]["description"] = note
    currency_snapshot["payments"][0]["notes"] = note
    for document in (statement_html(currency_snapshot), receipt_html(currency_snapshot, 5)):
        assert note in document
        assert "$12,34,567.89" in document
    tables = _csv_tables(export_csv(currency_snapshot, tmp_path))
    charges = next(rows for name, (_, rows) in tables.items() if name.endswith("-charges.csv"))
    payments = next(rows for name, (_, rows) in tables.items() if name.endswith("-payments.csv"))
    assert charges[0]["description"] == payments[0]["notes"] == note


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda item: item.code)
def test_native_pdf_embeds_each_currency_symbol_without_rounding(currency_snapshot, currency, tmp_path, qapp, qtbot):
    import pymupdf

    currency_snapshot["currency_code"] = currency.code
    destination = tmp_path / f"receipt-{currency.code}.pdf"
    job = PdfExportJob(receipt_html(currency_snapshot, 5), destination, renderer="native")
    errors = []
    job.failed.connect(errors.append)
    with qtbot.waitSignal(job.finished, timeout=10_000):
        job.start()
    assert not errors
    with pymupdf.open(destination) as document:
        text = "\n".join(page.get_text() for page in document)
        assert f"{currency.symbol}12,34,567.89" in text
        assert currency.code in text
        fonts = {font[0] for page in document for font in page.get_fonts()}
        assert fonts and all(document.extract_font(xref)[3] for xref in fonts)


def test_archived_export_uses_snapshot_currency_for_history_and_manifest(tmp_path):
    ledger = Ledger(tmp_path / "accounts")
    try:
        building = ledger.add_building("Sample House", "Sample address")
        flat = ledger.add_flat(building, "1", "Ground", 1)
        tenant = ledger.add_tenancy(flat, "Sample Tenant", "2024-01-01", 1, 12_345)
        ledger.set_opening_balances(tenant, "2024-01-01")
        ledger.move_out(tenant, "2024-01-31", 12_345)
        for bill in ledger.list_charges(tenant):
            if bill["status"] == "draft":
                ledger.post_charge(bill["id"], 0)
        rent = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "rent")
        ledger.record_payment(tenant, 12_345, "2024-01-31", "cash")
        ledger.mark_bill_paid(rent["id"])
        ledger.archive_tenancy(tenant)
        preview = ledger.prepare_tenancy_deletion(tenant)
        preview["statement"]["currency_code"] = "NZD"
        before = copy.deepcopy(preview)
        for language in ("en", "hi", "bilingual"):
            html = tenancy_deletion_html(preview, language)
            assert "₹" not in html
            assert html.count("NZ$123.45") >= 3
        result = ledger.export_tenancy_deletion_csv(preview, tmp_path / "archive.zip")
        with zipfile.ZipFile(result["path"]) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            assert manifest["currency_code"] == "NZD"
            assert "NZD in statement CSVs" in manifest["money_unit"]
            history = list(csv.DictReader(io.StringIO(bundle.read("history/charges.csv").decode("utf-8-sig"))))
            assert any(row["amount_paise"] == "12345" for row in history)
            payment_path = next(name for name in bundle.namelist() if name.startswith("statement/") and name.endswith("-payments.csv"))
            payments = list(csv.DictReader(io.StringIO(bundle.read(payment_path).decode("utf-8-sig"))))
            assert payments[0]["currency_code"] == "NZD"
            assert payments[0]["amount_nzd"] == "123.45"
        assert preview == before

        # The separate future-history appendix must use the same snapshot
        # currency even though these entries are outside the current balance.
        future_preview = copy.deepcopy(preview)
        future_preview["tables"]["payments"].append({
            "id": 999, "amount_paise": 105, "paid_on": "2099-01-01",
            "reversed_on": "2099-01-01", "reference": "Future legacy receipt", "notes": "",
        })
        html = tenancy_deletion_html(future_preview)
        assert "Future-dated transaction history" in html
        assert "NZ$1.05" in html and "₹" not in html
    finally:
        ledger.close()
