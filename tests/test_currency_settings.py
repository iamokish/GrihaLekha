"""A display preference must not mutate accounts or lose backup isolation."""

import copy
from datetime import datetime
import json

import pytest
from sqlalchemy import insert, select

from grihalekha import clock, models as m
from grihalekha.backups import BackupManager
from grihalekha.currencies import CURRENCIES
from grihalekha.ledger import Ledger
from grihalekha.reports import statement_html


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 31, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "accounts")
    building = ledger.add_building("Example house", "Example address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(
        flat, "Example tenant", "2024-01-01", 1, 100_025,
        initial_meter_reading_hundredths=12_345,
    )
    bill = ledger.add_charge(
        tenant, "misc", 20_075, "2024-01-01", "2024-01-31", "2024-01-31",
        description="Agreed fee", charge_date="2024-01-05",
    )
    ledger.add_charge(
        tenant, "electricity", 0, "2024-01-01", "2024-01-31", "2024-01-31",
        charge_date="2024-01-05", meter_reading_hundredths=54_321,
    )
    ledger.record_payment(tenant, 100_123, "2024-01-02", "cash")
    ledger.mark_bill_paid(bill, "2024-01-06")
    ledger.transfer_balance_to_deposit(tenant, 30_017, "2024-01-10", "Agreed deposit")
    ledger.refund_credit(tenant, 5_009, "2024-01-15", "cash", "Returned excess")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    yield ledger, tenant
    ledger.close()


def _tables(ledger, *, financial_only=False):
    with ledger._read() as conn:
        return {
            name: [dict(row) for row in conn.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings()]
            for name, table in m.metadata.tables.items()
            if not financial_only or name not in {"audit", "settings"}
        }


def test_existing_account_defaults_to_inr_without_writes_or_migration(account):
    ledger, tenant = account
    before = _tables(ledger)
    assert ledger.get_currency_code() == "INR"
    assert ledger.statement(tenant, "2024-01-01", "2024-01-31")["currency_code"] == "INR"
    ledger.set_currency_code("INR")
    assert _tables(ledger) == before
    assert not (ledger.data_dir / "migration-backups").exists()
    with ledger._read() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == m.SCHEMA_VERSION


def test_switching_all_currencies_preserves_every_financial_row_and_snapshot(account):
    ledger, tenant = account
    tables = _tables(ledger, financial_only=True)
    balance = ledger.balance(tenant)
    old_snapshot = ledger.statement(tenant, "2024-01-01", "2024-01-31")
    old_copy = copy.deepcopy(old_snapshot)
    revision = ledger.get_revision()
    for index, currency in enumerate(CURRENCIES):
        ledger.set_currency_code(currency.code)
        assert ledger.get_currency_code() == currency.code
        assert ledger.get_revision() == revision + index
        assert _tables(ledger, financial_only=True) == tables
        assert ledger.balance(tenant) == balance
        current = ledger.statement(tenant, "2024-01-01", "2024-01-31")
        assert current == dict(old_snapshot, currency_code=currency.code)
        assert old_snapshot == old_copy
        ledger.set_currency_code(currency.code)
        assert ledger.get_revision() == revision + index
    assert "₹" in statement_html(old_snapshot, document_id="OLD-SNAPSHOT")
    assert "NZ$" in statement_html(current, document_id="NEW-SNAPSHOT")
    audits = [row for row in _tables(ledger)["audit"] if row["action"] == "set_currency"]
    assert len(audits) == len(CURRENCIES) - 1
    assert all(row["entity"] == "settings" and row["entity_id"] is None for row in audits)
    assert json.loads(audits[0]["details"]) == {
        "previous_currency_code": "INR", "currency_code": "USD",
    }


@pytest.mark.parametrize("invalid", [None, "", "usd", " CHF ", "CHF", 1, True, [], {}])
def test_invalid_selection_never_changes_settings_accounts_or_revision(account, invalid):
    ledger, _ = account
    ledger.set_currency_code("EUR")
    before = _tables(ledger)
    with pytest.raises(ValueError, match="supported currency"):
        ledger.set_currency_code(invalid)
    assert _tables(ledger) == before


@pytest.mark.parametrize("initial", [None, "USD"])
def test_failed_audit_rolls_back_preference_insert_or_update(account, monkeypatch, initial):
    ledger, _ = account
    if initial is not None:
        ledger.set_currency_code(initial)
    before = _tables(ledger)
    audit = ledger._audit

    def fail_after_audit(*args, **kwargs):
        audit(*args, **kwargs)
        raise RuntimeError("Simulated failure after audit")

    monkeypatch.setattr(ledger, "_audit", fail_after_audit)
    with pytest.raises(RuntimeError, match="Simulated failure"):
        ledger.set_currency_code("JPY")
    assert ledger.get_currency_code() == (initial or "INR")
    assert _tables(ledger) == before


@pytest.mark.parametrize("stored", ["CHF", "", "usd"])
def test_unsupported_saved_preference_falls_back_and_can_be_repaired(account, stored):
    ledger, tenant = account
    with ledger._write() as conn:
        conn.execute(insert(m.settings).values(key="currency_code", value=stored))
    before = _tables(ledger)
    assert ledger.get_currency_code() == "INR"
    assert ledger.statement(tenant, "2024-01-01", "2024-01-31")["currency_code"] == "INR"
    assert _tables(ledger) == before
    revision = ledger.get_revision()
    ledger.set_currency_code("INR")
    assert ledger.get_revision() == revision + 1
    with ledger._read() as conn:
        assert conn.execute(select(m.settings.c.value).where(
            m.settings.c.key == "currency_code"
        )).scalar_one() == "INR"


def test_statement_currency_uses_same_read_transaction_as_financial_data(account, monkeypatch):
    ledger, tenant = account
    ledger.set_currency_code("EUR")
    tenancy_dict = ledger._tenancy_dict

    def change_preference_after_read_begins(conn, tenancy):
        result = tenancy_dict(conn, tenancy)
        ledger.set_currency_code("USD")
        return result

    monkeypatch.setattr(ledger, "_tenancy_dict", change_preference_after_read_begins)
    snapshot = ledger.statement(tenant, "2024-01-01", "2024-01-31")
    assert ledger.get_currency_code() == "USD"
    assert snapshot["currency_code"] == "EUR"


def test_currency_is_isolated_by_account_folder_and_survives_reopening(account, tmp_path):
    ledger, tenant = account
    ledger.set_currency_code("CAD")
    other_path = tmp_path / "other"
    other = Ledger(other_path)
    try:
        assert other.get_currency_code() == "INR"
        other.set_currency_code("AUD")
        assert ledger.get_currency_code() == "CAD"
        assert ledger.statement(tenant, "2024-01-01", "2024-01-31")["currency_code"] == "CAD"
    finally:
        other.close()
    reopened = Ledger(other_path)
    try:
        assert reopened.get_currency_code() == "AUD"
    finally:
        reopened.close()


@pytest.mark.parametrize("currency_code", [None, "JPY", "NZD"])
def test_backup_restore_carries_preference_and_defaults_old_backups_to_inr(account, tmp_path, currency_code):
    source, tenant = account
    if currency_code is not None:
        source.set_currency_code(currency_code)
    manager = BackupManager(source.data_dir, source.db_path)
    expected_tables = _tables(source)
    original_document = b"%PDF-1.4 previously issued INR document fixture"
    documents = source.data_dir / "documents"
    documents.mkdir(exist_ok=True)
    (documents / "issued.pdf").write_bytes(original_document)
    archive = manager.create(tmp_path / "transfer.zip")
    destination = tmp_path / "restored"
    old = Ledger(destination)
    old.set_currency_code("GBP")
    old.close()
    BackupManager(destination, destination / "grihalekha.sqlite3").restore(archive)
    restored = Ledger(destination)
    try:
        assert restored.get_currency_code() == (currency_code or "INR")
        assert _tables(restored) == expected_tables
        assert restored.balance(tenant) == source.balance(tenant)
        assert (destination / "documents" / "issued.pdf").read_bytes() == original_document
    finally:
        restored.close()
