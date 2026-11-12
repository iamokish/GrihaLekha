"""Versioned SQLite schema. Financial history is corrected, never deleted."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

SCHEMA_VERSION = 6
metadata = MetaData()

settings = Table(
    "settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
)
buildings = Table(
    "buildings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("address", Text, nullable=False),
)
flats = Table(
    "flats",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "building_id", ForeignKey("buildings.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("number", String(collation="NOCASE"), nullable=False),
    Column("floor", Text, nullable=False),
    Column("bhk", Integer, nullable=False),
    UniqueConstraint("building_id", "number", name="uq_flat_in_building"),
    CheckConstraint("bhk BETWEEN 1 AND 4", name="ck_flat_bhk"),
)
tenancies = Table(
    "tenancies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("flat_id", ForeignKey("flats.id", ondelete="RESTRICT"), nullable=False),
    Column("tenant_name", Text, nullable=False),
    Column("phone", Text, nullable=False, default=""),
    Column("members", Text, nullable=False, default="[]"),
    Column("move_in", String(10), nullable=False),
    Column("initial_meter_reading_hundredths", Integer, CheckConstraint(
        "initial_meter_reading_hundredths BETWEEN 0 AND 99999999999999",
        name="ck_initial_meter_reading")),
    Column("move_out", String(10)),
    Column("archived_on", String(10)),
    Column("cancelled_on", String(10)),
    Column("cancellation_reason", Text),
    Column("opening_date", String(10), nullable=False),
    Column("billing_day", Integer, nullable=False),
    Column("grace_days", Integer, nullable=False, default=0),
    Column("created_at", String, nullable=False),
    CheckConstraint("billing_day BETWEEN 1 AND 31", name="ck_tenancy_anchor"),
    CheckConstraint("grace_days BETWEEN 0 AND 365", name="ck_tenancy_grace"),
    CheckConstraint(
        "move_out IS NULL OR move_out >= move_in", name="ck_tenancy_occupancy"
    ),
)
Index(
    "ix_tenancies_flat", tenancies.c.flat_id, tenancies.c.move_in, tenancies.c.move_out
)
tenancy_occupancies = Table(
    "tenancy_occupancies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False),
    Column("flat_id", ForeignKey("flats.id", ondelete="RESTRICT"), nullable=False),
    Column("starts_on", String(10), nullable=False),
    Column("ends_on", String(10)),
    Column("rent_rate_id", ForeignKey("rent_rates.id", ondelete="RESTRICT")),
    Column("paired_tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT")),
    CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ck_occupancy_dates"),
    UniqueConstraint("tenancy_id", "starts_on", name="uq_occupancy_start"),
)
Index("ix_occupancies_flat_dates", tenancy_occupancies.c.flat_id, tenancy_occupancies.c.starts_on, tenancy_occupancies.c.ends_on)
rent_rates = Table(
    "rent_rates",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("effective_from", String(10), nullable=False),
    Column("amount_paise", Integer, nullable=False),
    Column("created_at", String, nullable=False),
    CheckConstraint("amount_paise >= 0", name="ck_rent_rate_amount"),
    UniqueConstraint("tenancy_id", "effective_from", name="uq_rent_rate_date"),
)
charges = Table(
    "charges",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("kind", String, nullable=False),
    Column("amount_paise", Integer),
    Column("description", Text, nullable=False, default=""),
    Column("period_start", String(10), nullable=False),
    Column("period_end", String(10), nullable=False),
    Column("due_date", String(10), nullable=False),
    Column("charge_date", String(10), nullable=False),
    Column("cycle_start", String(10)),
    Column("electricity_cycle_start", String(10)),
    Column("meter_reading_hundredths", Integer, CheckConstraint(
        "meter_reading_hundredths BETWEEN 0 AND 99999999999999",
        name="ck_meter_reading")),
    Column("electricity_deferred", Boolean, nullable=False, default=False, server_default="0"),
    Column("electricity_covered_by_charge_id", ForeignKey("charges.id", ondelete="RESTRICT")),
    Column("status", String, nullable=False),
    Column("partial", Boolean, nullable=False, default=False),
    Column("opening", Boolean, nullable=False, default=False),
    Column("voided_on", String(10)),
    Column("void_reason", Text),
    Column("deleted_on", String(10)),
    Column("created_at", String, nullable=False),
    Column("posted_at", String),
    Column("replaces_charge_id", ForeignKey("charges.id", ondelete="RESTRICT")),
    CheckConstraint("kind IN ('rent','electricity','misc')", name="ck_charge_kind"),
    CheckConstraint("status IN ('draft','posted','void')", name="ck_charge_status"),
    CheckConstraint(
        "amount_paise IS NULL OR amount_paise >= 0", name="ck_charge_amount"
    ),
    CheckConstraint(
        "status != 'posted' OR amount_paise IS NOT NULL", name="ck_posted_amount"
    ),
    CheckConstraint("period_end >= period_start", name="ck_charge_period"),
    UniqueConstraint("tenancy_id", "cycle_start", name="uq_rent_cycle"),
)
Index(
    "ix_charges_account_date",
    charges.c.tenancy_id,
    charges.c.charge_date,
    charges.c.status,
)
Index("uq_electricity_cycle", charges.c.tenancy_id, charges.c.electricity_cycle_start, unique=True)
Index("ix_electricity_coverage", charges.c.electricity_covered_by_charge_id)
cycle_payment_locks = Table(
    "cycle_payment_locks",
    metadata,
    Column("tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), primary_key=True),
    Column("cycle_start", String(10), primary_key=True),
    Column("locked", Boolean, nullable=False),
    Column("updated_at", String, nullable=False),
)
payments = Table(
    "payments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("amount_paise", Integer, nullable=False),
    Column("paid_on", String(10), nullable=False),
    Column("method", String, nullable=False),
    Column("kind", String, nullable=False, default="payment"),
    Column("reference", Text, nullable=False, default=""),
    Column("notes", Text, nullable=False, default=""),
    Column("proof_path", Text),
    Column("proof_name", Text),
    Column("reversed_on", String(10)),
    Column("reverse_reason", Text),
    Column("created_at", String, nullable=False),
    CheckConstraint("amount_paise > 0", name="ck_payment_amount"),
    CheckConstraint(
        "method IN ('cash','bank','upi','opening','internal')", name="ck_payment_method"
    ),
    CheckConstraint(
        "kind IN ('payment','opening','deposit_transfer')", name="ck_payment_kind"
    ),
)
Index("ix_payments_account_date", payments.c.tenancy_id, payments.c.paid_on)
allocations = Table(
    "allocations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "payment_id", ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("charge_id", ForeignKey("charges.id", ondelete="RESTRICT"), nullable=False),
    Column("amount_paise", Integer, nullable=False),
    Column("effective_on", String(10), nullable=False),
    Column("cancelled_on", String(10)),
    Column("cancel_reason", Text),
    Column("created_at", String, nullable=False),
    CheckConstraint("amount_paise > 0", name="ck_allocation_amount"),
)
Index("ix_allocations_payment", allocations.c.payment_id, allocations.c.effective_on)
Index("ix_allocations_charge", allocations.c.charge_id, allocations.c.effective_on)
refunds = Table(
    "refunds",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("amount_paise", Integer, nullable=False),
    Column("paid_on", String(10), nullable=False),
    Column("method", String, nullable=False),
    Column("notes", Text, nullable=False),
    Column("reversed_on", String(10)),
    Column("reverse_reason", Text),
    Column("created_at", String, nullable=False),
    CheckConstraint("amount_paise > 0", name="ck_refund_amount"),
    CheckConstraint("method IN ('cash','bank','upi')", name="ck_refund_method"),
)
Index("ix_refunds_account_date", refunds.c.tenancy_id, refunds.c.paid_on)
refund_sources = Table(
    "refund_sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("refund_id", ForeignKey("refunds.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "payment_id", ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("amount_paise", Integer, nullable=False),
    CheckConstraint("amount_paise > 0", name="ck_refund_source_amount"),
)
deposits = Table(
    "deposits",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "tenancy_id", ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("kind", String, nullable=False),
    Column("amount_paise", Integer, nullable=False),
    Column("paid_on", String(10), nullable=False),
    Column("method", String, nullable=False),
    Column("notes", Text, nullable=False),
    Column("payment_id", ForeignKey("payments.id", ondelete="RESTRICT")),
    Column("reversed_on", String(10)),
    Column("reverse_reason", Text),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "kind IN ('receipt','opening','refund','apply')", name="ck_deposit_kind"
    ),
    CheckConstraint("amount_paise > 0", name="ck_deposit_amount"),
)
Index("ix_deposits_account_date", deposits.c.tenancy_id, deposits.c.paid_on)

deposit_sources = Table(
    "deposit_sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("deposit_id", ForeignKey("deposits.id", ondelete="RESTRICT"), nullable=False),
    Column("payment_id", ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False),
    Column("amount_paise", Integer, nullable=False),
    CheckConstraint("amount_paise > 0", name="ck_deposit_source_amount"),
    UniqueConstraint("deposit_id", "payment_id", name="uq_deposit_source_payment"),
)
Index("ix_deposit_sources_payment", deposit_sources.c.payment_id)
audit = Table(
    "audit",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("action", String, nullable=False),
    Column("entity", String, nullable=False),
    Column("entity_id", Integer),
    Column("details", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
Index("ix_audit_entity_action", audit.c.entity, audit.c.action, audit.c.entity_id)


def required_schema(version: int) -> dict[str, set[str]]:
    """Required columns for each readable format, including pre-upgrade backups."""
    if version not in (1, 2, 3, 4, 5, SCHEMA_VERSION):
        raise ValueError("The database has no recognized GrihaLekha schema version.")
    result = {name: set(table.c.keys()) for name, table in metadata.tables.items()}
    if version < 6:
        result["tenancies"].remove("initial_meter_reading_hundredths")
        result["charges"].difference_update({
            "meter_reading_hundredths", "electricity_deferred", "electricity_covered_by_charge_id",
        })
    if version < 5:
        result.pop("deposit_sources")
        result["tenancies"].remove("cancelled_on")
        result["tenancies"].remove("cancellation_reason")
    if version < 4:
        result["charges"].remove("deleted_on")
    if version < 3:
        result.pop("cycle_payment_locks")
    if version == 1:
        result.pop("tenancy_occupancies")
        result["tenancies"].remove("archived_on")
        result["charges"].remove("electricity_cycle_start")
    return result
