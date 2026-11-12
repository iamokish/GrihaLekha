"""Small, transactional schema upgrades with a verified pre-upgrade snapshot."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from uuid import uuid4

from sqlalchemy import insert, select

from . import models as m


def _snapshot(database: Path, version: int) -> Path:
    directory = database.parent / "migration-backups"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"before-schema-{m.SCHEMA_VERSION}-{stamp}-{uuid4().hex[:8]}.sqlite3"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("The migration safety copy failed its integrity check.")
                if backup.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("The migration safety copy has broken record references.")
                if backup.execute("PRAGMA user_version").fetchone()[0] != version:
                    raise ValueError("The migration safety copy has an unexpected schema version.")
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def upgrade(conn, database: Path, version: int) -> None:
    """Called under BEGIN IMMEDIATE, before any schema-changing statements."""
    if version not in (1, 2, 3, 4, 5):
        raise ValueError("There is no supported migration for this database version.")
    _snapshot(database, version)
    if version == 1:
        conn.exec_driver_sql("ALTER TABLE tenancies ADD COLUMN archived_on VARCHAR(10)")
        conn.exec_driver_sql("ALTER TABLE charges ADD COLUMN electricity_cycle_start VARCHAR(10)")
        m.tenancy_occupancies.create(conn)
        conn.execute(
            insert(m.tenancy_occupancies).from_select(
                ["tenancy_id", "flat_id", "starts_on", "ends_on"],
                select(m.tenancies.c.id, m.tenancies.c.flat_id, m.tenancies.c.move_in, m.tenancies.c.move_out),
            )
        )
        conn.exec_driver_sql("CREATE UNIQUE INDEX uq_electricity_cycle ON charges (tenancy_id, electricity_cycle_start)")
    if version < 3:
        m.cycle_payment_locks.create(conn)
    if version < 4:
        conn.exec_driver_sql("ALTER TABLE charges ADD COLUMN deleted_on VARCHAR(10)")
    if version < 5:
        conn.exec_driver_sql("ALTER TABLE tenancies ADD COLUMN cancelled_on VARCHAR(10)")
        conn.exec_driver_sql("ALTER TABLE tenancies ADD COLUMN cancellation_reason TEXT")
        m.deposit_sources.create(conn)
    conn.exec_driver_sql(
        "ALTER TABLE tenancies ADD COLUMN initial_meter_reading_hundredths INTEGER "
        "CONSTRAINT ck_initial_meter_reading CHECK "
        "(initial_meter_reading_hundredths BETWEEN 0 AND 99999999999999)"
    )
    conn.exec_driver_sql(
        "ALTER TABLE charges ADD COLUMN meter_reading_hundredths INTEGER "
        "CONSTRAINT ck_meter_reading CHECK (meter_reading_hundredths BETWEEN 0 AND 99999999999999)"
    )
    conn.exec_driver_sql("ALTER TABLE charges ADD COLUMN electricity_deferred BOOLEAN NOT NULL DEFAULT '0'")
    conn.exec_driver_sql(
        "ALTER TABLE charges ADD COLUMN electricity_covered_by_charge_id INTEGER REFERENCES charges(id) ON DELETE RESTRICT"
    )
    conn.exec_driver_sql("CREATE INDEX ix_electricity_coverage ON charges (electricity_covered_by_charge_id)")
    conn.exec_driver_sql("CREATE INDEX ix_audit_entity_action ON audit (entity, action, entity_id)")
    conn.exec_driver_sql(f"PRAGMA user_version={m.SCHEMA_VERSION}")
