"""Deletion invariants for corrupted archives, transfers and legacy records."""

from datetime import datetime
import json
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from grihalekha import clock, models as m
from grihalekha.backups import BackupManager
from grihalekha.ledger import Ledger
from grihalekha.tenancy_deletion import tenancy_deletion_html


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 4, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "accounts")
    building = ledger.add_building("House", "Address")
    first = ledger.add_flat(building, "1", "Ground", 1)
    second = ledger.add_flat(building, "2", "Ground", 1)
    tenant = ledger.add_tenancy(first, "Erased household", "2024-01-01", 1, 0)
    other = ledger.add_tenancy(second, "Preserved household", "2024-01-01", 1, 0)
    yield ledger, tenant, other
    ledger.close()


def archive(ledger, tenant):
    ledger.move_out(tenant, "2024-01-31", 0)
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.archive_tenancy(tenant)


def paid_history(ledger, tenant, tmp_path):
    bill = ledger.add_charge(tenant, "misc", 20_000, "2024-01-01", "2024-01-31",
                             "2024-01-31", "Late paid fee", charge_date="2024-01-01")
    proof = tmp_path / "original.pdf"
    proof.write_bytes(b"%PDF-1.4\npayment proof fixture\n%%EOF")
    ledger.deposit_payment(tenant, 20_000, "2024-03-01", "cash", proof_path=proof)
    ledger.mark_bill_paid(bill, "2024-03-02")


def rows(ledger, table):
    with ledger._read() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


@pytest.mark.parametrize("corruption", ["draft", "unpaid", "credit", "deposit", "future_credit"])
def test_deletion_rechecks_settlement_and_drafts(accounts, corruption):
    ledger, tenant, _ = accounts
    archive(ledger, tenant)
    with ledger._write() as conn:
        if corruption in {"draft", "unpaid"}:
            charge = conn.execute(select(m.charges.c.id).where(m.charges.c.tenancy_id == tenant)).scalar()
            conn.execute(update(m.charges).where(m.charges.c.id == charge).values(
                status="draft" if corruption == "draft" else "posted", amount_paise=100))
        elif corruption == "deposit":
            conn.execute(insert(m.deposits).values(tenancy_id=tenant, kind="receipt", amount_paise=100,
                                                  paid_on="2024-03-01", method="cash", notes="", created_at="2024-04-15"))
        else:
            conn.execute(insert(m.payments).values(tenancy_id=tenant, amount_paise=100, kind="payment",
                                                  paid_on="2025-01-01" if corruption == "future_credit" else "2024-03-01",
                                                  method="cash", reference="", notes="", created_at="2024-04-15"))
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="Review|Settle"):
        ledger.delete_archived_tenancy(tenant, expected_revision=revision)
    assert ledger.get_tenancy(tenant)["archived_on"]
    assert ledger.get_revision() == revision


def test_exchange_reference_is_cleared_without_losing_other_occupancy(accounts):
    ledger, tenant, other = accounts
    ledger.exchange_tenancies(tenant, other, "2024-01-10")
    archive(ledger, tenant)
    preview = ledger.prepare_tenancy_deletion(tenant)
    before = ledger.get_tenancy(other)["occupancy_history"]
    ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    after = ledger.get_tenancy(other)["occupancy_history"]
    assert after == [dict(row, paired_tenancy_id=None) for row in before]
    entry = next(row for row in rows(ledger, m.audit) if row["action"] == "exchange")
    assert entry["entity_id"] == other and json.loads(entry["details"])["other_tenancy_id"] is None


def test_change_from_other_ledger_instance_invalidates_preview(accounts):
    ledger, tenant, other = accounts
    archive(ledger, tenant)
    preview = ledger.prepare_tenancy_deletion(tenant)
    external = Ledger(ledger.data_dir)
    try:
        external.update_tenancy(other, "Updated household")
    finally:
        external.close()
    with pytest.raises(ValueError, match="changed"):
        ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    assert ledger.get_tenancy(tenant)["archived_on"]


def test_attachment_cleanup_failure_reports_committed_deletion(accounts, tmp_path, monkeypatch):
    ledger, tenant, _ = accounts
    paid_history(ledger, tenant, tmp_path)
    archive(ledger, tenant)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.parent.name == "attachments":
            raise PermissionError("File open in another program")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    result = ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    assert len(result["cleanup_warnings"]) == 1
    with pytest.raises(ValueError, match="not found"):
        ledger.get_tenancy(tenant)


def test_database_backup_restores_deleted_account_and_proof(accounts, tmp_path):
    ledger, tenant, other = accounts
    paid_history(ledger, tenant, tmp_path)
    archive(ledger, tenant)
    manager = BackupManager(ledger.data_dir, ledger.db_path)
    backup = manager.create(tmp_path / "restorable-backup.zip")
    before = ledger.get_tenancy(tenant)
    relative = ledger.list_payments(tenant)[0]["proof_path"]
    ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    ledger.close()
    manager.restore(backup)
    restored = Ledger(ledger.data_dir)
    try:
        assert restored.get_tenancy(tenant) == before
        assert restored.get_tenancy(other)["tenant_name"] == "Preserved household"
        assert (restored.data_dir / relative).exists()
    finally:
        restored.close()


def test_linked_bill_replacements_can_be_deleted_atomically(accounts):
    ledger, tenant, _ = accounts
    old = ledger.add_charge(tenant, "misc", 100, "2024-01-01", "2024-01-31",
                            "2024-01-31", "Old fee", charge_date="2024-01-01")
    ledger.void_charge(old, "Replaced", "2024-01-02")
    new = ledger.add_charge(tenant, "misc", 0, "2024-01-01", "2024-01-31",
                            "2024-01-31", "Replacement fee", charge_date="2024-01-02")
    with ledger._write() as conn:
        conn.execute(update(m.charges).where(m.charges.c.id == new).values(replaces_charge_id=old))
    archive(ledger, tenant)
    ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    assert not any(row["id"] in {old, new} for row in rows(ledger, m.charges))


def test_future_legacy_entries_exported_with_current_balance(accounts):
    ledger, tenant, _ = accounts
    archive(ledger, tenant)
    with ledger._write() as conn:
        conn.execute(insert(m.payments).values(tenancy_id=tenant, amount_paise=100, kind="payment",
                                              paid_on="2025-01-01", reversed_on="2025-01-01",
                                              reverse_reason="Future receipt corrected", method="cash",
                                              reference="Legacy future receipt", notes="Future note", created_at="2024-04-15"))
    preview = ledger.prepare_tenancy_deletion(tenant)
    assert preview["statement"]["end"] == "2025-01-01"
    assert preview["statement"]["balance_as_of"] == "2024-04-15"
    assert preview["statement"]["payments"] == []
    html = tenancy_deletion_html(preview)
    assert "Future-dated transaction history" in html and "Legacy future receipt" in html
    assert preview["statement"]["closing_balance_paise"] == 0


def test_deleted_rate_audit_does_not_erase_another_tenants_reused_id(accounts):
    ledger, tenant, other = accounts
    archive(ledger, tenant)
    with ledger._write() as conn:
        rate_id = conn.execute(select(m.rent_rates.c.id).where(m.rent_rates.c.tenancy_id == other)).scalar_one()
        ledger._audit(conn, "cancel_scheduled_rate", "rent_rate", rate_id,
                      rate={"id": rate_id, "tenancy_id": tenant, "amount_paise": 10})
        ledger._audit(conn, "update_rate", "rent_rate", rate_id)
    ledger.delete_archived_tenancy(tenant, expected_revision=ledger.get_revision())
    audit = rows(ledger, m.audit)
    assert not any(row["action"] == "cancel_scheduled_rate" for row in audit)
    assert any(row["action"] == "update_rate" and row["entity_id"] == rate_id for row in audit)
