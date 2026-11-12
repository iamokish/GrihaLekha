"""Archived account exports and permanent deletion preserve other households."""

import csv
import io
import json
from pathlib import Path
import zipfile

import pytest
from sqlalchemy import insert, select, update

from grihalekha import models as m
from grihalekha.clock import today_ist
from grihalekha.ledger import Ledger
from grihalekha.tenancy_deletion import tenancy_deletion_html


@pytest.fixture
def archived(tmp_path):
    ledger = Ledger(tmp_path / "accounts")
    building = ledger.add_building("Shared building", "Shared address")
    first = ledger.add_flat(building, "1", "Ground", 1)
    second = ledger.add_flat(building, "2", "Ground", 1)
    tenant = ledger.add_tenancy(first, "=Archived household", "2024-01-01", 1, 10_000)
    other = ledger.add_tenancy(second, "Other household", "2024-01-01", 1, 20_000)
    ledger.set_opening_balances(tenant, "2024-01-01")
    ledger.move_out(tenant, "2024-01-31", 10_000)
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    proof = tmp_path / "bank-proof.pdf"
    proof.write_bytes(b"%PDF-1.4\npayment evidence\n%%EOF\n")
    paid = ledger.record_payment(tenant, 12_000, today_ist(), "bank", proof_path=proof)
    rent = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "rent")
    ledger.mark_bill_paid(rent["id"])
    ledger.refund_credit(tenant, 2_000, today_ist(), "cash", "Unused balance returned")
    ledger.record_deposit(tenant, 5_000, "2024-01-01", "receive")
    ledger.record_deposit(tenant, 5_000, "2024-01-31", "return", notes="Returned on departure")
    misc = ledger.add_charge(tenant, "misc", 100, "2024-01-01", "2024-01-31",
                             "2024-01-31", "<Removed charge>")
    ledger.delete_bill(misc, "Fee waived")
    ledger.archive_tenancy(tenant)
    yield ledger, tenant, other, paid, misc
    ledger.close()


def rows(ledger, table):
    with ledger._read() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def exported(ledger, preview, tmp_path):
    csv_backup = ledger.export_tenancy_deletion_csv(preview, tmp_path / "account-csv.zip")
    pdf_path = tmp_path / "account.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\ncompleted test document\n%%EOF\n")
    pdf_backup = ledger.tenancy_deletion_backup_file(preview, pdf_path)
    return [csv_backup, pdf_backup]


def test_preview_and_csv_export_include_late_payments_removed_bills_and_proofs(archived, tmp_path):
    ledger, tenant, _, paid, misc = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    assert preview["statement"]["start"] == "2024-01-01"
    assert preview["statement"]["end"] >= today_ist().isoformat()
    assert preview["statement"]["payments"][0]["id"] == paid
    assert any(row["id"] == misc and row["deleted_on"] for row in preview["tables"]["charges"])
    revision = ledger.get_revision()
    artifact = ledger.export_tenancy_deletion_csv(preview, tmp_path / "csv.zip")
    with zipfile.ZipFile(artifact["path"]) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["revision"] == revision
        assert manifest["counts"] == preview["counts"]
        assert "integer paise" in manifest["money_unit"]
        household = list(csv.DictReader(io.StringIO(bundle.read("history/tenancies.csv").decode("utf-8-sig"))))
        assert household[0]["tenant_name"] == "'=Archived household"
        history = list(csv.DictReader(io.StringIO(bundle.read("history/charges.csv").decode("utf-8-sig"))))
        assert any(row["id"] == str(misc) and row["deleted_on"] for row in history)
        assert any(row["amount_paise"] == "10000" for row in history)
        proof = preview["tables"]["payments"][0]["proof_path"]
        assert bundle.read(proof) == (ledger.data_dir / proof).read_bytes()
        assert any(name.startswith("statement/") and name.endswith("payments.csv") for name in bundle.namelist())
    assert ledger.get_revision() == revision


def test_archive_pdf_history_contains_deleted_bills_and_escapes_user_text(archived):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    for language in ("en", "hi", "bilingual"):
        html = tenancy_deletion_html(preview, language)
        assert "&lt;Removed charge&gt;" in html
        assert "<Removed charge>" not in html
        assert "Unused balance returned" in html
        assert "Returned on departure" in html
        assert "Shared building" in html


def test_delete_with_both_backups_removes_owned_rows_and_preserves_unrelated_data(archived, tmp_path):
    ledger, tenant, other, _, _ = archived
    ledger.record_payment(other, 321, "2024-01-01", "cash")
    other_before = ledger.get_tenancy(other)
    other_payments = ledger.list_payments(other)
    properties = (rows(ledger, m.buildings), rows(ledger, m.flats))
    issued = ledger.data_dir / "documents" / "issued.pdf"
    issued.parent.mkdir()
    issued.write_bytes(b"Issued statements are not ownership-tracked")
    managed = ledger.data_dir / "backups" / "previous.zip"
    managed.parent.mkdir()
    managed.write_bytes(b"Existing backup")
    preview = ledger.prepare_tenancy_deletion(tenant)
    proof = ledger.data_dir / preview["tables"]["payments"][0]["proof_path"]
    artifacts = exported(ledger, preview, tmp_path)
    result = ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"], backups=artifacts)
    assert result == {"deleted_tenancy_id": tenant, "cleanup_warnings": []}
    with pytest.raises(ValueError, match="not found"):
        ledger.get_tenancy(tenant)
    assert ledger.get_tenancy(other) == other_before
    assert ledger.list_payments(other) == other_payments
    assert (rows(ledger, m.buildings), rows(ledger, m.flats)) == properties
    for table in (m.charges, m.deposits, m.refunds, m.tenancy_occupancies, m.rent_rates, m.cycle_payment_locks):
        assert not any(row["tenancy_id"] == tenant for row in rows(ledger, table))
    assert not rows(ledger, m.allocations)
    assert not rows(ledger, m.refund_sources)
    assert not any(row["key"] == f"opening_initialized_{tenant}" for row in rows(ledger, m.settings))
    assert all("Archived household" not in row["details"] for row in rows(ledger, m.audit))
    assert not proof.exists()
    assert issued.exists() and managed.exists()
    assert all(Path(item["path"]).is_file() for item in artifacts)
    with ledger._read() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").first() is None


def test_open_accounts_cannot_be_previewed_or_deleted(archived):
    ledger, _, other, _, _ = archived
    with pytest.raises(ValueError, match="Only an archived"):
        ledger.prepare_tenancy_deletion(other)
    with pytest.raises(ValueError, match="Only an archived"):
        ledger.delete_archived_tenancy(other, expected_revision=ledger.get_revision())


def test_account_changes_invalidate_export_and_deletion_review(archived, tmp_path):
    ledger, tenant, other, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    ledger.record_payment(other, 100, "2024-01-01", "cash")
    with pytest.raises(ValueError, match="changed"):
        ledger.export_tenancy_deletion_csv(preview, tmp_path / "stale.zip")
    with pytest.raises(ValueError, match="changed"):
        ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    assert not (tmp_path / "stale.zip").exists()
    assert ledger.get_tenancy(tenant)["archived_on"]


def test_deletion_requires_both_exports_and_rejects_changed_artifacts(archived, tmp_path):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    artifacts = exported(ledger, preview, tmp_path)
    with pytest.raises(ValueError, match="both"):
        ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"], backups=artifacts[:1])
    Path(artifacts[1]["path"]).write_bytes(b"tampered PDF")
    with pytest.raises(ValueError, match="missing or changed"):
        ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"], backups=artifacts)
    assert ledger.get_tenancy(tenant)["archived_on"]
    assert ledger.get_revision() == preview["revision"]


def test_missing_proof_export_fails_without_replacing_existing_backup(archived, tmp_path):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    target = tmp_path / "existing.zip"
    target.write_bytes(b"Existing good backup")
    (ledger.data_dir / preview["tables"]["payments"][0]["proof_path"]).unlink()
    with pytest.raises(ValueError, match="proof is missing"):
        ledger.export_tenancy_deletion_csv(preview, target)
    assert target.read_bytes() == b"Existing good backup"
    assert not list(tmp_path.glob("*.partial"))
    assert ledger.get_tenancy(tenant)["archived_on"]


def test_incomplete_pdf_and_wrong_account_csv_are_rejected(archived, tmp_path):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    pdf = tmp_path / "incomplete.pdf"
    pdf.write_bytes(b"%PDF-1.4\nunfinished")
    with pytest.raises(ValueError, match="incomplete"):
        ledger.tenancy_deletion_backup_file(preview, pdf)
    wrong_csv = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong_csv, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"format": "grihalekha-tenant-csv", "tenancy_id": tenant + 1}))
    with pytest.raises(ValueError, match="does not match"):
        ledger.tenancy_deletion_backup_file(preview, wrong_csv, kind="csv")


def test_shared_payment_proof_is_kept_for_other_household(archived):
    ledger, tenant, other, _, _ = archived
    other_payment = ledger.record_payment(other, 100, "2024-01-01", "cash")
    payment = ledger.list_payments(tenant)[0]
    with ledger._write() as conn:
        conn.execute(update(m.payments).where(m.payments.c.id == other_payment).values(proof_path=payment["proof_path"]))
    preview = ledger.prepare_tenancy_deletion(tenant)
    ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    assert (ledger.data_dir / payment["proof_path"]).is_file()
    assert ledger.list_payments(other)[0]["proof_path"] == payment["proof_path"]


def test_transaction_failure_keeps_all_account_records_and_proofs(archived, monkeypatch):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    original_audit = ledger._audit

    def fail_deletion(conn, action, *args, **kwargs):
        if action == "delete_archived_tenant":
            raise RuntimeError("simulated storage failure")
        return original_audit(conn, action, *args, **kwargs)

    monkeypatch.setattr(ledger, "_audit", fail_deletion)
    with pytest.raises(RuntimeError, match="storage failure"):
        ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    assert ledger.prepare_tenancy_deletion(tenant)["tables"] == preview["tables"]
    assert (ledger.data_dir / preview["tables"]["payments"][0]["proof_path"]).is_file()


def test_cross_account_financial_reference_prevents_deletion(archived):
    ledger, tenant, other, paid, _ = archived
    bill = ledger.add_charge(other, "misc", 100, "2024-01-01", "2024-01-31", "2024-01-31", "Other fee")
    with ledger._write() as conn:
        conn.execute(insert(m.allocations).values(payment_id=paid, charge_id=bill, amount_paise=1,
                     effective_on="2024-01-01", cancelled_on="2024-01-01", created_at="2024-01-01T00:00:00"))
    with pytest.raises(ValueError, match="cross-account"):
        ledger.prepare_tenancy_deletion(tenant)
    with pytest.raises(ValueError, match="cross-account"):
        ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    assert ledger.get_tenancy(tenant)["archived_on"]


@pytest.mark.parametrize("relative", ["../outside.pdf", "attachments/../../outside.pdf", "documents/issued.pdf"])
def test_unsafe_payment_proof_path_blocks_deletion(archived, relative):
    ledger, tenant, _, paid, _ = archived
    with ledger._write() as conn:
        conn.execute(update(m.payments).where(m.payments.c.id == paid).values(proof_path=relative))
    with pytest.raises(ValueError, match="unsafe file path"):
        ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    assert ledger.get_tenancy(tenant)["archived_on"]


def test_backup_destination_cannot_overwrite_managed_account_files(archived):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    for target in (ledger.db_path, ledger.data_dir / "attachments" / "backup.zip", ledger.data_dir / "documents" / "backup.zip"):
        with pytest.raises(ValueError, match="outside the managed"):
            ledger.export_tenancy_deletion_csv(preview, target)
    assert ledger.get_tenancy(tenant)["archived_on"]


def test_csv_registration_rejects_missing_history_and_malformed_archives(archived, tmp_path):
    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    complete = ledger.export_tenancy_deletion_csv(preview, tmp_path / "complete.zip")
    incomplete = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(complete["path"]) as source, zipfile.ZipFile(incomplete, "w") as output:
        output.writestr("manifest.json", source.read("manifest.json"))
    with pytest.raises(ValueError, match="does not match"):
        ledger.tenancy_deletion_backup_file(preview, incomplete, kind="csv")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"This is not a ZIP")
    with pytest.raises(ValueError, match="incomplete or unreadable"):
        ledger.tenancy_deletion_backup_file(preview, corrupt, kind="csv")
    with zipfile.ZipFile(corrupt, "w") as output:
        output.writestr("manifest.json", "{")
    with pytest.raises(ValueError, match="incomplete or unreadable"):
        ledger.tenancy_deletion_backup_file(preview, corrupt, kind="csv")


def test_exchange_counterpart_keeps_occupancy_and_audit_after_deletion(tmp_path):
    ledger = Ledger(tmp_path / "accounts")
    try:
        building = ledger.add_building("Exchange house", "Address")
        flats = [ledger.add_flat(building, str(index), "Ground", 1) for index in (1, 2)]
        tenants = [ledger.add_tenancy(flat, f"Household {index}", "2024-01-01", 1, 0)
                   for index, flat in enumerate(flats)]
        ledger.exchange_tenancies(*tenants, "2024-02-01")
        ledger.move_out(tenants[0], "2024-02-29", 0)
        for bill in ledger.list_charges(tenants[0]):
            if bill["status"] == "draft":
                ledger.post_charge(bill["id"], 0)
        ledger.archive_tenancy(tenants[0])
        other_before = ledger.get_tenancy(tenants[1])
        preview = ledger.prepare_tenancy_deletion(tenants[0])
        ledger.delete_archived_tenancy(tenants[0], expected_revision=preview["revision"])
        other_after = ledger.get_tenancy(tenants[1])
        assert other_after["flat_id"] == other_before["flat_id"]
        for old, new in zip(other_before["occupancy_history"], other_after["occupancy_history"], strict=True):
            assert {**old, "paired_tenancy_id": None} == new
        exchanges = [row for row in rows(ledger, m.audit) if row["action"] == "exchange"]
        assert len(exchanges) == 1 and exchanges[0]["entity_id"] == tenants[1]
        details = json.loads(exchanges[0]["details"])
        assert details["other_tenancy_id"] is None
        assert details["counterpart_deleted"] is True
        assert details["effective_on"] == "2024-02-01"
    finally:
        ledger.close()


def test_actual_archive_pdf_is_complete_and_can_be_registered(archived, tmp_path, qapp, qtbot):
    import pymupdf
    from grihalekha.reports import PdfExportJob

    ledger, tenant, _, _, _ = archived
    preview = ledger.prepare_tenancy_deletion(tenant)
    destination = tmp_path / "archive-history.pdf"
    job = PdfExportJob(tenancy_deletion_html(preview), destination, renderer="native")
    failures = []
    job.failed.connect(failures.append)
    with qtbot.waitSignal(job.finished, timeout=15_000):
        job.start()
    assert not failures
    artifact = ledger.tenancy_deletion_backup_file(preview, destination)
    assert artifact["kind"] == "pdf" and artifact["size"] > 1000
    with pymupdf.open(destination) as document:
        text = "\n".join(page.get_text() for page in document)
    assert "Archived account history" in text
    assert "<Removed charge>" in text
    assert "Flat history" in text
