"""Transfers reserve real receipt credit and preserve dated financial history."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
import zipfile

import pytest
from sqlalchemy import select, update

from grihalekha import clock, models as m
from grihalekha.backups import BackupManager
from grihalekha.ledger import Ledger
from grihalekha.money import MAX_PAISE
from grihalekha.reports import statement_html


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 31, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Example house", "Example address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Example tenant", "2024-01-01", 1, 0)
    yield ledger, tenant, building
    ledger.close()


def _rows(ledger, table):
    with ledger._read() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id)).mappings()]


def _fee(ledger, tenant, amount):
    return ledger.add_charge(
        tenant, "misc", amount, "2024-01-01", "2024-01-31", "2024-01-31",
        description="Agreed fee", charge_date="2024-01-05",
    )


def test_transfer_splits_receipts_without_adding_cash_and_preserves_historical_balances(account):
    ledger, tenant, _ = account
    first = ledger.record_payment(tenant, 60_050, "2024-01-02", "cash")
    second = ledger.record_payment(tenant, 40_025, "2024-01-03", "upi", reference="Original proof")
    originals = _rows(ledger, m.payments)
    deposit = ledger.transfer_balance_to_deposit(tenant, 75_065, "2024-01-10", "Reserve agreed deposit")
    assert _rows(ledger, m.payments) == originals
    assert not _rows(ledger, m.refunds)
    assert [(r["deposit_id"], r["payment_id"], r["amount_paise"]) for r in _rows(ledger, m.deposit_sources)] == [
        (deposit, first, 60_050), (deposit, second, 15_015),
    ]
    before = ledger.balance(tenant, "2024-01-09")
    after = ledger.balance(tenant, "2024-01-10")
    assert (before["credit_paise"], before["deposit_paise"]) == (100_075, 0)
    assert (after["credit_paise"], after["deposit_paise"], after["net_paise"]) == (25_010, 75_065, -25_010)
    assert before["paid_paise"] == after["paid_paise"] == 100_075
    assert after["refunded_paise"] == 0
    assert [r["available_paise"] for r in ledger.list_payments(tenant)] == [0, 25_010]
    assert ledger.dashboard()["deposit_paise"] == 75_065


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "100", MAX_PAISE + 1])
def test_transfer_rejects_invalid_amount_atomically(account, amount):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    revision = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.transfer_balance_to_deposit(tenant, amount, "2024-01-10", "Deposit")
    assert ledger.get_revision() == revision
    assert not _rows(ledger, m.deposits)
    assert not _rows(ledger, m.deposit_sources)


@pytest.mark.parametrize("paid_on,notes", [
    ("2023-12-31", "Earlier than opening"),
    ("2024-02-01", "Future transfer"),
    ("not-a-date", "Bad date"),
    ("2024-01-10", "   "),
    ("2024-01-10", None),
    ("2024-01-10", "x" * 10_001),
])
def test_transfer_rejects_invalid_date_or_reason(account, paid_on, notes):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    revision = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.transfer_balance_to_deposit(tenant, 50_000, paid_on, notes)
    assert ledger.get_revision() == revision
    assert ledger.balance(tenant)["credit_paise"] == 100_000


def test_transfer_cannot_use_money_received_after_its_date_or_another_tenant(account):
    ledger, tenant, building = account
    flat = ledger.add_flat(building, "2", "Ground", 1)
    other = ledger.add_tenancy(flat, "Other tenant", "2024-01-01", 1, 0)
    ledger.record_payment(other, 1_000_000, "2024-01-02", "cash")
    ledger.record_payment(tenant, 100_000, "2024-01-20", "cash")
    with pytest.raises(ValueError, match="available balance"):
        ledger.transfer_balance_to_deposit(tenant, 1, "2024-01-10", "Deposit")
    assert ledger.balance(other)["credit_paise"] == 1_000_000


@pytest.mark.parametrize("later_use", ["bill", "refund", "transfer"])
def test_backdated_transfer_respects_later_uses(account, later_use):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    if later_use == "bill":
        ledger.mark_bill_paid(_fee(ledger, tenant, 80_000), "2024-01-20")
    elif later_use == "refund":
        ledger.refund_credit(tenant, 80_000, "2024-01-20", "cash", "Returned advance")
    else:
        ledger.transfer_balance_to_deposit(tenant, 80_000, "2024-01-20", "First transfer")
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="available balance"):
        ledger.transfer_balance_to_deposit(tenant, 20_001, "2024-01-10", "Too much")
    assert ledger.get_revision() == revision
    ledger.transfer_balance_to_deposit(tenant, 20_000, "2024-01-10", "Remaining funds")
    assert ledger.balance(tenant)["credit_paise"] == 0


@pytest.mark.parametrize("later_use", ["bill", "refund"])
def test_transfer_reserves_source_credit_against_later_and_backdated_spending(account, later_use):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    ledger.transfer_balance_to_deposit(tenant, 80_000, "2024-01-20", "Security deposit")
    revision = ledger.get_revision()
    if later_use == "bill":
        bill = _fee(ledger, tenant, 30_000)
        revision = ledger.get_revision()
        with pytest.raises(ValueError, match="balance|available"):
            ledger.mark_bill_paid(bill, "2024-01-10")
        assert ledger.get_bill(bill)["paid_paise"] == 0
    else:
        with pytest.raises(ValueError, match="advance|credit|available"):
            ledger.refund_credit(tenant, 30_000, "2024-01-10", "cash", "Too much")
    assert ledger.get_revision() == revision
    assert ledger.balance(tenant)["credit_paise"] == 20_000


def test_reversal_releases_original_sources_on_its_date_and_protects_receipt_chronology(account):
    ledger, tenant, _ = account
    payment = ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    deposit = ledger.transfer_balance_to_deposit(tenant, 70_000, "2024-01-10", "Deposit")
    with pytest.raises(ValueError, match="security deposit"):
        ledger.reverse_payment(payment, "Wrong receipt", "2024-01-20")
    ledger.reverse_deposit(deposit, "Undo transfer", "2024-01-20")
    assert ledger.balance(tenant, "2024-01-19")["credit_paise"] == 30_000
    assert ledger.balance(tenant, "2024-01-20")["credit_paise"] == 100_000
    assert ledger.balance(tenant, "2024-01-20")["deposit_paise"] == 0
    with pytest.raises(ValueError, match="transfer corrections"):
        ledger.reverse_payment(payment, "Would erase earlier funding", "2024-01-19")
    ledger.reverse_payment(payment, "Correct original receipt", "2024-01-20")
    assert ledger.balance(tenant)["credit_paise"] == 0
    assert ledger.balance(tenant, "2024-01-19")["credit_paise"] == 30_000
    assert len(_rows(ledger, m.deposit_sources)) == 1


def test_transfer_reversal_cannot_erase_a_deposit_already_returned(account):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    deposit = ledger.transfer_balance_to_deposit(tenant, 100_000, "2024-01-10", "Deposit")
    returned = ledger.record_deposit(tenant, 60_000, "2024-01-20", "return", notes="Returned deposit")
    with pytest.raises(ValueError, match="used or returned"):
        ledger.reverse_deposit(deposit, "Undo transfer", "2024-01-15")
    ledger.reverse_deposit(returned, "Return was entered twice", "2024-01-25")
    with pytest.raises(ValueError, match="used or returned"):
        ledger.reverse_deposit(deposit, "Too early", "2024-01-24")
    ledger.reverse_deposit(deposit, "Undo transfer", "2024-01-25")
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert ledger.balance(tenant)["deposit_paise"] == 0


def test_round_trip_deposit_funding_cannot_duplicate_money(account):
    ledger, tenant, _ = account
    ledger.record_deposit(tenant, 100_000, "2024-01-02", "receive")
    applied = ledger.record_deposit(tenant, 100_000, "2024-01-05", "apply", notes="Move to balance")
    moved_back = ledger.transfer_balance_to_deposit(tenant, 100_000, "2024-01-10", "Keep as deposit")
    with pytest.raises(ValueError, match="security deposit"):
        ledger.reverse_deposit(applied, "Undo original application", "2024-01-20")
    ledger.reverse_deposit(moved_back, "Undo move back", "2024-01-20")
    ledger.reverse_deposit(applied, "Undo original application", "2024-01-20")
    balance = ledger.balance(tenant)
    assert balance["credit_paise"] == 0
    assert balance["deposit_paise"] == 100_000


@pytest.mark.parametrize("start,end,adjustment", [
    ("2024-01-01", "2024-01-09", 0),
    ("2024-01-10", "2024-01-19", 70_025),
    ("2024-01-20", "2024-01-31", -70_025),
    ("2024-01-01", "2024-01-31", 0),
])
def test_statement_transfer_and_reversal_reconcile_without_fake_refunds(account, start, end, adjustment):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_075, "2024-01-02", "cash")
    deposit = ledger.transfer_balance_to_deposit(tenant, 70_025, "2024-01-10", "Deposit agreed")
    ledger.reverse_deposit(deposit, "Entry correction", "2024-01-20")
    snapshot = ledger.statement(tenant, start, end, as_of=end)
    assert snapshot["adjustments_paise"] == adjustment
    assert snapshot["period_refunds_paise"] == 0
    assert snapshot["refunds"] == []
    assert snapshot["period_payments_paise"] == (100_075 if start <= "2024-01-02" <= end else 0)
    assert snapshot["period_closing_balance_paise"] == (
        snapshot["opening_balance_paise"] + snapshot["period_charges_paise"]
        - snapshot["period_payments_paise"] + snapshot["adjustments_paise"]
    )
    for language in ("en", "hi", "bilingual"):
        assert "<html" in statement_html(snapshot, language)
    received = [row for row in snapshot["deposits"] if row["kind"] == "receipt"]
    if received:
        assert received[0]["action"] == "from_balance"


def test_audit_failure_rolls_back_deposit_and_sources_together(account, monkeypatch):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    revision = ledger.get_revision()
    def fail(*args, **kwargs):
        raise RuntimeError("Simulated audit failure")
    monkeypatch.setattr(ledger, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        ledger.transfer_balance_to_deposit(tenant, 50_000, "2024-01-10", "Deposit")
    assert not _rows(ledger, m.deposits)
    assert not _rows(ledger, m.deposit_sources)
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert ledger.get_revision() == revision


@pytest.mark.parametrize("field", ["archived_on", "cancelled_on"])
def test_closed_or_cancelled_account_rejects_new_transfer(account, field):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    with ledger._write() as conn:
        conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenant).values({field: "2024-01-20"}))
    with pytest.raises(ValueError, match="archiv|closed|cancelled"):
        ledger.transfer_balance_to_deposit(tenant, 50_000, "2024-01-10", "Deposit")
    assert ledger.balance(tenant)["credit_paise"] == 100_000


def test_concurrent_transfers_cannot_spend_the_same_receipt_twice(account):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    other = Ledger(ledger.data_dir)
    barrier = Barrier(2)
    def transfer(service):
        barrier.wait(timeout=5)
        try:
            service.transfer_balance_to_deposit(tenant, 60_000, "2024-01-10", "Deposit")
            return True
        except ValueError as error:
            assert "available balance" in str(error)
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(transfer, (ledger, other)))
        assert sorted(results) == [False, True]
        assert ledger.balance(tenant)["credit_paise"] == 40_000
        assert ledger.balance(tenant)["deposit_paise"] == 60_000
    finally:
        other.close()


def test_complete_backup_restores_transfer_and_funding_links(account, tmp_path):
    ledger, tenant, _ = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    deposit = ledger.transfer_balance_to_deposit(tenant, 70_000, "2024-01-10", "Deposit")
    manager = BackupManager(ledger.data_dir, ledger.db_path)
    backup = manager.create(tmp_path / "transfer-backup.zip")
    target = tmp_path / "restored"
    target.mkdir()
    BackupManager(target, target / "grihalekha.sqlite3").restore(backup)
    restored = Ledger(target)
    try:
        assert restored.balance(tenant)["credit_paise"] == 30_000
        assert restored.balance(tenant)["deposit_paise"] == 70_000
        assert _rows(restored, m.deposit_sources) == _rows(ledger, m.deposit_sources)
        restored.reverse_deposit(deposit, "Correct after recovery", "2024-01-20")
        assert restored.balance(tenant)["credit_paise"] == 100_000
    finally:
        restored.close()


def test_archival_export_and_deletion_include_transfer_sources(account, tmp_path):
    ledger, tenant, building = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    ledger.transfer_balance_to_deposit(tenant, 70_000, "2024-01-10", "Agreed deposit")
    other_flat = ledger.add_flat(building, "2", "Ground", 1)
    other = ledger.add_tenancy(other_flat, "Other household", "2024-01-01", 1, 0)
    ledger.record_payment(other, 10_000, "2024-01-02", "cash")
    other_transfer = ledger.transfer_balance_to_deposit(other, 5_000, "2024-01-10", "Other deposit")

    ledger.move_out(tenant, "2024-01-15", 0, "Tenant departed")
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.refund_credit(tenant, 30_000, "2024-01-20", "cash", "Remaining advance returned")
    ledger.record_deposit(tenant, 70_000, "2024-01-20", "return", notes="Deposit returned")
    ledger.archive_tenancy(tenant)
    preview = ledger.prepare_tenancy_deletion(tenant)
    assert len(preview["tables"]["deposit_sources"]) == 1
    artifact = ledger.export_tenancy_deletion_csv(preview, tmp_path / "archived-transfer.zip")
    with zipfile.ZipFile(artifact["path"]) as archive:
        assert "70000" in archive.read("history/deposit_sources.csv").decode("utf-8-sig")
    ledger.delete_archived_tenancy(tenant, expected_revision=preview["revision"])
    with pytest.raises(ValueError, match="not found"):
        ledger.get_tenancy(tenant)
    assert [row["deposit_id"] for row in _rows(ledger, m.deposit_sources)] == [other_transfer]
    assert ledger.balance(other)["credit_paise"] == 5_000
    assert ledger.balance(other)["deposit_paise"] == 5_000
    with ledger._read() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_deletion_guard_rejects_cross_account_deposit_source(account):
    ledger, tenant, building = account
    ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    ledger.transfer_balance_to_deposit(tenant, 70_000, "2024-01-10", "Deposit")
    other_flat = ledger.add_flat(building, "2", "Ground", 1)
    other = ledger.add_tenancy(other_flat, "Other household", "2024-01-01", 1, 0)
    other_payment = ledger.record_payment(other, 100_000, "2024-01-02", "cash")
    with ledger._write() as conn:
        conn.execute(update(m.deposit_sources).values(payment_id=other_payment))
    with ledger._read() as conn:
        for account_id in (tenant, other):
            with pytest.raises(ValueError, match="cross-account"):
                ledger._check_deletion_references(conn, account_id)


def test_cancelling_upcoming_tenant_preserves_transferred_deposit_for_settlement(account):
    ledger, _, building = account
    flat = ledger.add_flat(building, "Upcoming", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Upcoming tenant", "2024-02-01", 1, 60_000)
    # Exercise legacy/imported booking-credit records. The current Add tenant
    # workflow itself does not allow an account opening before move-in.
    with ledger._write() as conn:
        conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenant).values(opening_date="2024-01-01"))
    payment = ledger.record_payment(tenant, 100_000, "2024-01-02", "cash")
    rent = ledger.add_charge(
        tenant, "rent", 60_000, "2024-02-01", "2024-02-29", "2024-02-29",
        charge_date="2024-01-10",
    )
    ledger.mark_bill_paid(rent, "2024-01-10")
    ledger.transfer_balance_to_deposit(tenant, 40_000, "2024-01-12", "Agreed deposit")
    source_rows = _rows(ledger, m.deposit_sources)
    ledger.cancel_upcoming_tenancy(tenant, "Booking cancelled")
    balance = ledger.balance(tenant)
    assert (balance["credit_paise"], balance["deposit_paise"], balance["outstanding_paise"]) == (60_000, 40_000, 0)
    assert ledger.balance(tenant, "2024-01-30")["credit_paise"] == 0
    assert _rows(ledger, m.deposit_sources) == source_rows
    assert ledger.list_payments(tenant)[0]["id"] == payment
    ledger.refund_credit(tenant, 60_000, "2024-01-31", "cash", "Advance returned")
    ledger.record_deposit(tenant, 40_000, "2024-01-31", "return", notes="Deposit returned")
    ledger.archive_tenancy(tenant)
    assert ledger.get_tenancy(tenant)["cancelled_on"] == "2024-01-31"
    assert ledger.get_tenancy(tenant)["archived_on"] == "2024-01-31"
    assert ledger.balance(tenant)["credit_paise"] == ledger.balance(tenant)["deposit_paise"] == 0
    assert ledger.add_tenancy(flat, "Replacement tenant", "2024-02-01", 1, 60_000)
    assert "<html" in statement_html(ledger.statement(tenant, "2024-01-01", "2024-02-29"), "bilingual")
