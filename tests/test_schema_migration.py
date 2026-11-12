from contextlib import closing
from datetime import datetime
import sqlite3

import pytest

from grihalekha import clock, models
from grihalekha.backups import BackupManager
from grihalekha.ledger import Ledger


def _remove_schema_six(connection):
    # SQLite cannot DROP a column referenced by a table-level foreign key.
    # Rebuild this synthetic fixture with the actual old columns/constraints.
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (0,)
    connection.execute("DROP INDEX ix_audit_entity_action")
    removed = {"electricity_covered_by_charge_id", "electricity_deferred", "meter_reading_hundredths"}
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='charges'").fetchone()[0]
    old_schema = "\n".join(line for line in schema.splitlines()
                           if not any(column in line for column in removed))
    columns = [row[1] for row in connection.execute("PRAGMA table_info(charges)") if row[1] not in removed]
    column_sql = ", ".join('"' + column + '"' for column in columns)
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='charges' "
        "AND sql IS NOT NULL AND name != 'ix_electricity_coverage'")]
    connection.execute(old_schema.replace("CREATE TABLE charges (", "CREATE TABLE charges_v5 (", 1))
    connection.execute(f"INSERT INTO charges_v5 ({column_sql}) SELECT {column_sql} FROM charges")
    connection.execute("DROP TABLE charges")
    connection.execute("ALTER TABLE charges_v5 RENAME TO charges")
    for index in indexes:
        connection.execute(index)
    connection.execute("ALTER TABLE tenancies DROP COLUMN initial_meter_reading_hundredths")


def _remove_schema_five(connection):
    """Reconstruct an actual older schema, not a current DB with an old label."""
    _remove_schema_six(connection)
    connection.execute("DROP TABLE deposit_sources")
    connection.execute("ALTER TABLE tenancies DROP COLUMN cancelled_on")
    connection.execute("ALTER TABLE tenancies DROP COLUMN cancellation_reason")


def _version_one_account(path):
    ledger = Ledger(path)
    building = ledger.add_building("Old building", "Old address")
    flat = ledger.add_flat(building, "12", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Existing tenant", "2024-01-01", 1, 10_000)
    ledger.record_payment(tenant, 1_234, "2024-01-02", "cash", allocations={})
    ledger.close()
    database = path / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_five(connection)
        connection.execute("ALTER TABLE charges DROP COLUMN deleted_on")
        connection.execute("DROP TABLE cycle_payment_locks")
        connection.execute("DROP TABLE tenancy_occupancies")
        connection.execute("ALTER TABLE tenancies DROP COLUMN archived_on")
        connection.execute("DROP INDEX uq_electricity_cycle")
        connection.execute("ALTER TABLE charges DROP COLUMN electricity_cycle_start")
        connection.execute("PRAGMA user_version=1")
    return database, tenant, flat


def test_v1_upgrade_preserves_accounts_and_creates_verified_snapshot(tmp_path):
    data = tmp_path / "old"
    database, tenant, flat = _version_one_account(data)
    ledger = Ledger(data)
    try:
        assert ledger.balance(tenant)["credit_paise"] == 1_234
        tenancy = ledger.get_tenancy(tenant)
        assert tenancy["occupancy_history"][0]["flat_id"] == flat
        assert tenancy["archived_on"] is None
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == models.SCHEMA_VERSION
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        snapshots = list((data / "migration-backups").glob("*.sqlite3"))
        assert len(snapshots) == 1
        with closing(sqlite3.connect(snapshots[0])) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)
            assert connection.execute("SELECT amount_paise FROM payments").fetchone() == (1_234,)
    finally:
        ledger.close()
    Ledger(data).close()
    assert len(list((data / "migration-backups").glob("*.sqlite3"))) == 1


def test_failed_migration_rolls_back_schema_and_preserves_v1_data(tmp_path, monkeypatch):
    data = tmp_path / "old"
    database, _, _ = _version_one_account(data)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(models.tenancy_occupancies, "create", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        Ledger(data)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert "archived_on" not in {row[1] for row in connection.execute("PRAGMA table_info(tenancies)")}
        assert connection.execute("SELECT amount_paise FROM payments").fetchone() == (1_234,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_migration_snapshot_includes_committed_wal_data(tmp_path):
    data = tmp_path / "old"
    database, tenant, _ = _version_one_account(data)
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE payments SET amount_paise=4_567")
        writer.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        ledger = Ledger(data)
        try:
            assert ledger.balance(tenant)["credit_paise"] == 4_567
            snapshot = next((data / "migration-backups").glob("*.sqlite3"))
            with closing(sqlite3.connect(snapshot)) as saved:
                assert saved.execute("SELECT amount_paise FROM payments").fetchone() == (4_567,)
        finally:
            ledger.close()


def test_old_backups_can_restore_then_upgrade_and_new_backups_use_current_schema(tmp_path):
    original = tmp_path / "old"
    database, tenant, _ = _version_one_account(original)
    manager = BackupManager(original, database)
    backup = manager.create(tmp_path / "version-one.zip")
    assert manager.validate(backup)["schema"] == 1
    target = tmp_path / "restored"
    target.mkdir()
    restored = BackupManager(target, target / "grihalekha.sqlite3")
    restored.restore(backup)
    ledger = Ledger(target)
    try:
        assert ledger.balance(tenant)["credit_paise"] == 1_234
        assert restored.validate(restored.create())["schema"] == models.SCHEMA_VERSION
    finally:
        ledger.close()


def test_v2_upgrade_preserves_history_and_takes_v2_snapshot(tmp_path):
    data = tmp_path / "version-two"
    ledger = Ledger(data)
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "1", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    ledger.deposit_payment(tenant, 123_456, "2024-01-02", "cash")
    ledger.close()
    database = data / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_five(connection)
        connection.execute("ALTER TABLE charges DROP COLUMN deleted_on")
        connection.execute("DROP TABLE cycle_payment_locks")
        connection.execute("PRAGMA user_version=2")
    manager = BackupManager(data, database)
    old_backup = manager.create(tmp_path / "v2.zip")
    assert manager.validate(old_backup)["schema"] == 2
    upgraded = Ledger(data)
    try:
        assert upgraded.balance(tenant)["credit_paise"] == 123_456
        upgraded.set_cycle_payment_lock(tenant, "2024-01-01", True)
        assert upgraded.list_bill_cycles(tenant)[0]["locked"]
        saved_path = next((data / "migration-backups").glob("*.sqlite3"))
        with closing(sqlite3.connect(saved_path)) as saved:
            assert saved.execute("PRAGMA user_version").fetchone() == (2,)
            assert saved.execute("SELECT amount_paise FROM payments").fetchone() == (123_456,)
    finally:
        upgraded.close()


def test_failed_v2_migration_keeps_original_schema(tmp_path, monkeypatch):
    data = tmp_path / "version-two"
    Ledger(data).close()
    database = data / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_five(connection)
        connection.execute("ALTER TABLE charges DROP COLUMN deleted_on")
        connection.execute("DROP TABLE cycle_payment_locks")
        connection.execute("PRAGMA user_version=2")
    def fail(*args, **kwargs):
        raise RuntimeError("simulated lock-table failure")
    monkeypatch.setattr(models.cycle_payment_locks, "create", fail)
    with pytest.raises(RuntimeError, match="lock-table failure"):
        Ledger(data)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def _version_three_account(path):
    ledger = Ledger(path)
    building = ledger.add_building("Schema 3 house", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    bill = ledger.add_charge(tenant, "misc", 20_000, "2024-01-01", "2024-01-31",
                             "2024-01-31", "Existing fee", charge_date="2024-01-01")
    ledger.deposit_payment(tenant, 30_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bill, "2024-01-03")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    ledger.close()
    database = path / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_five(connection)
        connection.execute("ALTER TABLE charges DROP COLUMN deleted_on")
        connection.execute("PRAGMA user_version=3")
    return database, tenant, bill


def test_v3_backup_upgrade_and_deleted_history_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    data = tmp_path / "version-three"
    database, tenant, bill = _version_three_account(data)
    manager = BackupManager(data, database)
    old_backup = manager.create(tmp_path / "v3.zip")
    assert manager.validate(old_backup)["schema"] == 3
    restored_data = tmp_path / "upgraded"
    restored_data.mkdir()
    restored_manager = BackupManager(restored_data, restored_data / "grihalekha.sqlite3")
    restored_manager.restore(old_backup)
    upgraded = Ledger(restored_data)
    try:
        assert upgraded.balance(tenant)["credit_paise"] == 10_000
        assert upgraded.get_bill(bill)["paid_paise"] == 20_000
        assert upgraded.get_bill(bill)["deleted_on"] is None
        assert upgraded.list_bill_cycles(tenant)[0]["locked"]
        snapshot = next((restored_data / "migration-backups").glob("*.sqlite3"))
        with closing(sqlite3.connect(snapshot)) as saved:
            assert saved.execute("PRAGMA user_version").fetchone() == (3,)
            assert "deleted_on" not in {row[1] for row in saved.execute("PRAGMA table_info(charges)")}
            assert saved.execute("SELECT amount_paise FROM allocations WHERE charge_id=?", (bill,)).fetchone() == (20_000,)
        upgraded.set_cycle_payment_lock(tenant, "2024-01-01", False)
        upgraded.delete_bill(bill, "Fee should be removed")
        current_backup = restored_manager.create(tmp_path / "v4-deleted.zip")
        assert restored_manager.validate(current_backup)["schema"] == models.SCHEMA_VERSION
    finally:
        upgraded.close()
    final_data = tmp_path / "restored-deletion"
    final_data.mkdir()
    BackupManager(final_data, final_data / "grihalekha.sqlite3").restore(current_backup)
    final = Ledger(final_data)
    try:
        assert final.get_bill(bill)["deleted_on"] == "2024-01-15"
        assert bill not in {row["id"] for row in final.list_charges(tenant)}
        assert final.balance(tenant)["credit_paise"] == 30_000
        assert final.get_bill(bill)["payments"][0]["cancelled_on"] == "2024-01-15"
    finally:
        final.close()


def test_failed_v3_upgrade_rolls_back_added_column_and_preserves_history(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    data = tmp_path / "version-three"
    database, tenant, bill = _version_three_account(data)
    original = Connection.exec_driver_sql
    def fail_after_alter(connection, statement, *args, **kwargs):
        result = original(connection, statement, *args, **kwargs)
        if statement == "ALTER TABLE charges ADD COLUMN deleted_on VARCHAR(10)":
            raise RuntimeError("simulated post-alter failure")
        return result
    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_alter)
    with pytest.raises(RuntimeError, match="post-alter failure"):
        Ledger(data)
    with closing(sqlite3.connect(database)) as saved:
        assert saved.execute("PRAGMA user_version").fetchone() == (3,)
        assert "deleted_on" not in {row[1] for row in saved.execute("PRAGMA table_info(charges)")}
        assert saved.execute("SELECT amount_paise FROM allocations WHERE charge_id=?", (bill,)).fetchone() == (20_000,)
        assert saved.execute("SELECT locked FROM cycle_payment_locks WHERE tenancy_id=?", (tenant,)).fetchone() == (1,)
        assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)


def _version_four_account(path):
    ledger = Ledger(path)
    building = ledger.add_building("Existing house", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Existing tenant", "2024-01-01", 1, 100_000)
    ledger.deposit_payment(tenant, 50_000, "2024-01-02", "bank")
    ledger.record_deposit(tenant, 20_000, "2024-01-03", "receive")
    ledger.close()
    database = path / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_five(connection)
        connection.execute("PRAGMA user_version=4")
    return database, tenant


def test_v4_backup_upgrade_preserves_money_and_new_transfers_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    source = tmp_path / "v4"
    database, tenant = _version_four_account(source)
    manager = BackupManager(source, database)
    old = manager.create(tmp_path / "v4.zip")
    assert manager.validate(old)["schema"] == 4
    target = tmp_path / "upgraded"
    target.mkdir()
    restored = BackupManager(target, target / "grihalekha.sqlite3")
    restored.restore(old)
    ledger = Ledger(target)
    try:
        assert ledger.balance(tenant)["credit_paise"] == 50_000
        assert ledger.balance(tenant)["deposit_paise"] == 20_000
        assert ledger.get_tenancy(tenant)["cancelled_on"] is None
        snapshot = next((target / "migration-backups").glob("*.sqlite3"))
        with closing(sqlite3.connect(snapshot)) as saved:
            assert saved.execute("PRAGMA user_version").fetchone() == (4,)
            assert "cancelled_on" not in {row[1] for row in saved.execute("PRAGMA table_info(tenancies)")}
        ledger.transfer_balance_to_deposit(tenant, 12_345, "2024-01-10", "Deposit funded from balance")
        current = restored.create(tmp_path / "v5.zip")
        assert restored.validate(current)["schema"] == models.SCHEMA_VERSION
    finally:
        ledger.close()
    final = tmp_path / "final"
    final.mkdir()
    BackupManager(final, final / "grihalekha.sqlite3").restore(current)
    ledger = Ledger(final)
    try:
        assert ledger.balance(tenant)["credit_paise"] == 37_655
        assert ledger.balance(tenant)["deposit_paise"] == 32_345
        assert ledger.balance(tenant, "2024-01-09")["credit_paise"] == 50_000
        with ledger._read() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        ledger.close()


def test_failed_v4_upgrade_rolls_back_columns_and_preserves_snapshot(tmp_path, monkeypatch):
    data = tmp_path / "v4"
    database, _ = _version_four_account(data)
    def fail(*args, **kwargs):
        raise RuntimeError("simulated transfer-table failure")
    monkeypatch.setattr(models.deposit_sources, "create", fail)
    with pytest.raises(RuntimeError, match="transfer-table failure"):
        Ledger(data)
    with closing(sqlite3.connect(database)) as saved:
        assert saved.execute("PRAGMA user_version").fetchone() == (4,)
        columns = {row[1] for row in saved.execute("PRAGMA table_info(tenancies)")}
        assert not {"cancelled_on", "cancellation_reason"} & columns
        assert saved.execute("SELECT amount_paise FROM payments").fetchone() == (50_000,)
        assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert len(list((data / "migration-backups").glob("*.sqlite3"))) == 1


def _version_five_account(path):
    ledger = Ledger(path)
    building = ledger.add_building("Schema 5 house", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Existing tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    power = next(row["id"] for row in ledger.list_charges(tenant) if row["kind"] == "electricity")
    ledger.post_charge(power, 10000)
    ledger.deposit_payment(tenant, 20000, "2024-01-10", "cash")
    ledger.mark_bill_paid(power, "2024-01-10")
    ledger.transfer_balance_to_deposit(tenant, 1000, "2024-01-10", "Deposit")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    ledger.close()
    database = path / "grihalekha.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        _remove_schema_six(connection)
        connection.execute("PRAGMA user_version=5")
    return database, tenant, power


def test_v5_backup_upgrade_and_readings_restore_preserve_payments_and_locks(tmp_path):
    source = tmp_path / "schema-five"
    database, tenant, power = _version_five_account(source)
    old_manager = BackupManager(source, database)
    old_backup = old_manager.create(tmp_path / "schema-five.zip")
    assert old_manager.validate(old_backup)["schema"] == 5
    target = tmp_path / "upgraded-six"
    target.mkdir()
    manager = BackupManager(target, target / "grihalekha.sqlite3")
    manager.restore(old_backup)
    ledger = Ledger(target)
    try:
        assert ledger.get_tenancy(tenant)["initial_meter_reading_hundredths"] is None
        bill = ledger.get_bill(power)
        assert bill["meter_reading_hundredths"] is None
        assert not bill["electricity_deferred"]
        assert bill["electricity_covered_by_charge_id"] is None
        assert bill["paid_paise"] == 10000 and bill["payment_locked"]
        assert ledger.balance(tenant)["credit_paise"] == 9000
        assert ledger.balance(tenant)["deposit_paise"] == 1000
        snapshot = next((target / "migration-backups").glob("*.sqlite3"))
        with closing(sqlite3.connect(snapshot)) as saved:
            assert saved.execute("PRAGMA user_version").fetchone() == (5,)
            assert "meter_reading_hundredths" not in {
                row[1] for row in saved.execute("PRAGMA table_info(charges)")
            }
            assert saved.execute("SELECT amount_paise FROM allocations").fetchone() == (10000,)
        ledger.set_meter_reading(power, 12345)
        ledger.update_tenancy(tenant, "Existing tenant", initial_meter_reading_hundredths=0)
        current_backup = manager.create(tmp_path / "schema-six.zip")
        assert manager.validate(current_backup)["schema"] == models.SCHEMA_VERSION
    finally:
        ledger.close()
    final = tmp_path / "restored-six"
    final.mkdir()
    BackupManager(final, final / "grihalekha.sqlite3").restore(current_backup)
    ledger = Ledger(final)
    try:
        assert ledger.get_tenancy(tenant)["initial_meter_reading_hundredths"] == 0
        assert ledger.get_bill(power)["meter_reading_hundredths"] == 12345
        assert ledger.get_bill(power)["paid_paise"] == 10000
        assert ledger.get_bill(power)["payment_locked"]
    finally:
        ledger.close()


def test_failed_v5_upgrade_rolls_back_new_columns_and_retains_snapshot(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    database, _, power = _version_five_account(tmp_path / "schema-five")
    original = Connection.exec_driver_sql

    def fail_after_alter(connection, statement, *args, **kwargs):
        result = original(connection, statement, *args, **kwargs)
        if statement.startswith("ALTER TABLE charges ADD COLUMN meter_reading_hundredths"):
            raise RuntimeError("simulated meter migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_alter)
    with pytest.raises(RuntimeError, match="meter migration failure"):
        Ledger(database.parent)
    with closing(sqlite3.connect(database)) as saved:
        assert saved.execute("PRAGMA user_version").fetchone() == (5,)
        assert "initial_meter_reading_hundredths" not in {
            row[1] for row in saved.execute("PRAGMA table_info(tenancies)")
        }
        assert "meter_reading_hundredths" not in {
            row[1] for row in saved.execute("PRAGMA table_info(charges)")
        }
        assert saved.execute("SELECT amount_paise FROM allocations WHERE charge_id=?", (power,)).fetchone() == (10000,)
        assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert len(list((database.parent / "migration-backups").glob("*.sqlite3"))) == 1
