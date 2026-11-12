"""Transactional tenancy accounts, with exact money and effective-date history."""

# Public service validation consistently raises ValueError for the desktop UI.
# ruff: noqa: TRY004

import json
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from uuid import uuid4

from sqlalchemy import create_engine, delete, event, func, insert, or_, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from . import models as m
from .billing import BillingMixin
from .bill_corrections import BillCorrectionMixin
from .clock import today_ist
from .currencies import DEFAULT_CURRENCY, get_currency
from .lifecycle import LifecycleMixin
from .tenancy_deletion import TenancyDeletionMixin
from .tenancy_cancellation import TenancyCancellationMixin
from .deposit_transfers import DepositTransferMixin
from .dates import as_date, cycle_bounds, iso_date, iter_account_periods
from .money import validate_paise
from .meter import UNSET, validate_meter_reading

FAR_FUTURE = "9999-12-31"


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _text(value, label, required=False, maximum=10_000):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > maximum:
        raise ValueError(f"{label} is too long (maximum {maximum} characters).")
    return value


def _active(column, cutoff):
    return or_(column.is_(None), column > cutoff)


def _method(value):
    if value not in ("cash", "bank", "upi"):
        raise ValueError("Choose cash, bank transfer or UPI.")
    return value


class Ledger(BillCorrectionMixin, BillingMixin, LifecycleMixin, TenancyDeletionMixin,
             TenancyCancellationMixin, DepositTransferMixin):
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "grihalekha.sqlite3"
        self._lock = RLock()
        self.engine = create_engine(
            URL.create("sqlite", database=str(self.db_path)),
            connect_args={"timeout": 15},
        )

        @event.listens_for(self.engine, "connect")
        def configure(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

        try:
            self._initialize_schema()
        except BaseException:
            self.engine.dispose()
            raise

    def _initialize_schema(self):
        with self.engine.connect() as conn:
            version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
            if version > m.SCHEMA_VERSION:
                self.engine.dispose()
                raise ValueError(
                    "This database was created by a newer GrihaLekha version."
                )
            existing = (
                conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                .scalars()
                .all()
            )
            if version == 0 and existing:
                raise ValueError(
                    "The database has no recognized schema version; restore a valid GrihaLekha backup."
                )
            if version:
                required = m.required_schema(version)
                if set(required) - set(existing):
                    raise ValueError(
                        "The database is missing required tables. Restore a valid backup."
                    )
                for name, expected_columns in required.items():
                    columns = {
                        row[1]
                        for row in conn.exec_driver_sql(
                            f"PRAGMA table_info('{name}')"
                        )
                    }
                    if expected_columns - columns:
                        raise ValueError(
                            "The database is missing required columns. Restore a valid backup."
                        )
                if (
                    conn.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok"
                    or conn.exec_driver_sql("PRAGMA foreign_key_check").first()
                    is not None
                ):
                    raise ValueError(
                        "The database integrity check failed. Restore a valid backup."
                    )
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.commit()
        if version == 0:
            with self.engine.begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                m.metadata.create_all(conn)
                conn.execute(insert(m.settings).values(key="revision", value="0"))
                conn.exec_driver_sql(f"PRAGMA user_version={m.SCHEMA_VERSION}")
        elif version < m.SCHEMA_VERSION:
            from .migrations import upgrade

            with self.engine.begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                current = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
                if current < m.SCHEMA_VERSION:
                    upgrade(conn, self.db_path, current)

    def close(self):
        self.engine.dispose()

    @contextmanager
    def _read(self):
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN")
            yield conn

    @contextmanager
    def _write(self):
        with self._lock, self.engine.connect() as conn:
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except IntegrityError as exc:
                conn.rollback()
                raise ValueError(
                    "This entry conflicts with an existing record or violates an account constraint."
                ) from exc
            except BaseException:
                conn.rollback()
                raise

    def _audit(self, conn, action, entity, entity_id=None, **details):
        conn.execute(
            insert(m.audit).values(
                action=action,
                entity=entity,
                entity_id=entity_id,
                details=json.dumps(details, ensure_ascii=False),
                created_at=_now(),
            )
        )
        revision = int(
            conn.execute(
                select(m.settings.c.value).where(m.settings.c.key == "revision")
            ).scalar_one()
        )
        conn.execute(
            update(m.settings)
            .where(m.settings.c.key == "revision")
            .values(value=str(revision + 1))
        )

    def get_revision(self):
        with self._read() as conn:
            return int(
                conn.execute(
                    select(m.settings.c.value).where(m.settings.c.key == "revision")
                ).scalar_one()
            )

    @staticmethod
    def _currency_code(conn):
        saved = conn.execute(
            select(m.settings.c.value).where(m.settings.c.key == "currency_code")
        ).scalar_one_or_none()
        if saved is None:
            return DEFAULT_CURRENCY
        try:
            return get_currency(saved).code
        except ValueError:
            # An unsupported preference must not prevent access to accounts.
            return DEFAULT_CURRENCY

    def get_currency_code(self):
        """Return this account folder's display currency, defaulting to INR."""
        with self._read() as conn:
            return self._currency_code(conn)

    def set_currency_code(self, code):
        """Change currency labels without converting or rewriting any amounts."""
        code = get_currency(code).code
        with self._write() as conn:
            saved = conn.execute(
                select(m.settings.c.value).where(m.settings.c.key == "currency_code")
            ).scalar_one_or_none()
            if saved == code or (saved is None and code == DEFAULT_CURRENCY):
                return
            if saved is None:
                conn.execute(insert(m.settings).values(key="currency_code", value=code))
            else:
                conn.execute(
                    update(m.settings)
                    .where(m.settings.c.key == "currency_code")
                    .values(value=code)
                )
            self._audit(
                conn, "set_currency", "settings",
                previous_currency_code=saved or DEFAULT_CURRENCY,
                currency_code=code,
            )

    def _get(self, conn, table, record_id, label="Record"):
        if (
            isinstance(record_id, bool)
            or not isinstance(record_id, int)
            or record_id <= 0
        ):
            raise ValueError(f"Choose a valid {label.lower()}.")
        row = (
            conn.execute(select(table).where(table.c.id == record_id))
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError(f"{label} was not found.")
        return dict(row)

    def list_buildings(self):
        with self._read() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(m.buildings).order_by(m.buildings.c.name, m.buildings.c.id)
                ).mappings()
            ]

    def add_building(self, name, address):
        values = {
            "name": _text(name, "Building name", True, 200),
            "address": _text(address, "Address", True, 4000),
        }
        with self._write() as conn:
            record_id = conn.execute(
                insert(m.buildings).values(**values)
            ).inserted_primary_key[0]
            self._audit(conn, "create", "building", record_id)
            return record_id

    def update_building(self, building_id, name, address):
        with self._write() as conn:
            self._get(conn, m.buildings, building_id, "Building")
            conn.execute(
                update(m.buildings)
                .where(m.buildings.c.id == building_id)
                .values(
                    name=_text(name, "Building name", True, 200),
                    address=_text(address, "Address", True, 4000),
                )
            )
            self._audit(conn, "update", "building", building_id)

    def add_flat(self, building_id, number, floor, bhk):
        if isinstance(bhk, bool) or not isinstance(bhk, int) or not 1 <= bhk <= 4:
            raise ValueError("Choose 1, 2, 3 or 4 BHK.")
        with self._write() as conn:
            self._get(conn, m.buildings, building_id, "Building")
            record_id = conn.execute(
                insert(m.flats).values(
                    building_id=building_id,
                    number=_text(number, "Flat number", True, 100),
                    floor=_text(str(floor), "Floor", True, 100),
                    bhk=bhk,
                )
            ).inserted_primary_key[0]
            self._audit(conn, "create", "flat", record_id)
            return record_id

    def remove_building(self, building_id):
        """Delete an empty property without cascading into tenancy history."""
        with self._write() as conn:
            building = self._get(conn, m.buildings, building_id, "Building")
            if conn.execute(select(m.flats.c.id).where(m.flats.c.building_id == building_id).limit(1)).first():
                raise ValueError("Remove or move this building's flats first. Buildings with flats cannot be removed.")
            conn.execute(delete(m.buildings).where(m.buildings.c.id == building_id))
            self._audit(conn, "delete", "building", building_id, **building)

    def remove_flat(self, flat_id):
        """Only unused flats can be deleted; former and future tenants count."""
        with self._write() as conn:
            flat = self._get(conn, m.flats, flat_id, "Flat")
            if conn.execute(select(m.tenancy_occupancies.c.id).where(m.tenancy_occupancies.c.flat_id == flat_id).limit(1)).first():
                raise ValueError("This flat has tenant history and cannot be removed. Its accounts and payments must remain available.")
            conn.execute(delete(m.flats).where(m.flats.c.id == flat_id))
            self._audit(conn, "delete", "flat", flat_id, **flat)

    def list_flats(self):
        today = today_ist().isoformat()
        with self._read() as conn:
            rows = (
                conn.execute(
                    select(
                        m.flats,
                        m.buildings.c.name.label("building_name"),
                        m.buildings.c.address,
                    )
                    .join(m.buildings)
                    .order_by(m.buildings.c.name, m.flats.c.number)
                )
                .mappings()
                .all()
            )
            active = (
                conn.execute(
                    select(
                        m.tenancies.c.id,
                        m.tenancy_occupancies.c.flat_id,
                        m.tenancies.c.tenant_name,
                    ).join(m.tenancy_occupancies, m.tenancy_occupancies.c.tenancy_id == m.tenancies.c.id).where(
                        m.tenancies.c.cancelled_on.is_(None),
                        m.tenancy_occupancies.c.starts_on <= today,
                        or_(
                            m.tenancy_occupancies.c.ends_on.is_(None),
                            m.tenancy_occupancies.c.ends_on >= today,
                        ),
                    )
                )
                .mappings()
                .all()
            )
            by_flat = {row["flat_id"]: row for row in active}
            return [
                dict(
                    row,
                    tenant_name=by_flat.get(row["id"], {}).get("tenant_name", ""),
                    tenancy_id=by_flat.get(row["id"], {}).get("id"),
                )
                for row in rows
            ]

    def update_flat(self, flat_id, building_id, number, floor, bhk):
        if isinstance(bhk, bool) or not isinstance(bhk, int) or not 1 <= bhk <= 4:
            raise ValueError("Choose 1, 2, 3 or 4 BHK.")
        with self._write() as conn:
            self._get(conn, m.flats, flat_id, "Flat")
            self._get(conn, m.buildings, building_id, "Building")
            conn.execute(
                update(m.flats)
                .where(m.flats.c.id == flat_id)
                .values(
                    building_id=building_id,
                    number=_text(number, "Flat number", True, 100),
                    floor=_text(str(floor), "Floor", True, 100),
                    bhk=bhk,
                )
            )
            self._audit(conn, "update", "flat", flat_id)

    def _members(self, members):
        if members is None:
            return "[]"
        if not isinstance(members, list) or len(members) > 100:
            raise ValueError("Family members must be a list of at most 100 people.")
        result = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError(
                    "Enter a name and relationship for each family member."
                )
            result.append(
                {
                    "name": _text(
                        member.get("name", ""), "Family member name", True, 200
                    ),
                    "relationship": _text(
                        member.get("relationship", ""), "Relationship", False, 100
                    ),
                }
            )
        return json.dumps(result, ensure_ascii=False)

    def _grace(self, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 365
        ):
            raise ValueError("Grace days must be between 0 and 365.")
        return value

    def add_tenancy(
        self,
        flat_id,
        tenant_name,
        move_in,
        billing_day,
        rent_paise,
        grace_days=0,
        phone="",
        members=None,
        opening_date=None,
        initial_meter_reading_hundredths=None,
    ):
        start = iso_date(move_in)
        opening = iso_date(opening_date or start)
        if opening < start:
            raise ValueError("The account opening date cannot precede move-in.")
        cycle_bounds(start, billing_day)
        validate_paise(rent_paise)
        validate_meter_reading(initial_meter_reading_hundredths)
        with self._write() as conn:
            self._get(conn, m.flats, flat_id, "Flat")
            self._assert_flat_available(conn, flat_id, start, None)
            record_id = conn.execute(
                insert(m.tenancies).values(
                    flat_id=flat_id,
                    tenant_name=_text(tenant_name, "Tenant name", True, 200),
                    phone=_text(phone, "Phone", False, 100),
                    members=self._members(members),
                    move_in=start,
                    initial_meter_reading_hundredths=initial_meter_reading_hundredths,
                    opening_date=opening,
                    billing_day=billing_day,
                    grace_days=self._grace(grace_days),
                    created_at=_now(),
                )
            ).inserted_primary_key[0]
            conn.execute(insert(m.tenancy_occupancies).values(
                tenancy_id=record_id, flat_id=flat_id, starts_on=start,
            ))
            conn.execute(
                insert(m.rent_rates).values(
                    tenancy_id=record_id,
                    effective_from=start,
                    amount_paise=rent_paise,
                    created_at=_now(),
                )
            )
            self._audit(conn, "create", "tenancy", record_id)
            return record_id

    def _tenancy_dict(self, conn, row):
        result = dict(row)
        result["occupancy_history"] = self._occupancy_history(conn, result["id"])
        if result["occupancy_history"]:
            today = today_ist().isoformat()
            occurred = [item for item in result["occupancy_history"] if item["starts_on"] <= today]
            result["flat_id"] = (occurred[-1] if occurred else result["occupancy_history"][0])["flat_id"]
        flat = self._get(conn, m.flats, result["flat_id"], "Flat")
        building = self._get(conn, m.buildings, flat["building_id"], "Building")
        result.update(
            building_id=building["id"],
            building_name=building["name"],
            address=building["address"],
            number=flat["number"],
            floor=flat["floor"],
            bhk=flat["bhk"],
        )
        result["members"] = json.loads(result["members"])
        result["rent_paise"] = self._rent_rate(
            conn, result["id"], max(today_ist().isoformat(), result["move_in"])
        )
        result["rent_rates"] = [
            dict(rate)
            for rate in conn.execute(
                select(m.rent_rates)
                .where(m.rent_rates.c.tenancy_id == result["id"])
                .order_by(m.rent_rates.c.effective_from)
            ).mappings()
        ]
        return result

    def list_tenancies(self, flat_id=None, include_archived=False):
        with self._read() as conn:
            query = select(m.tenancies).order_by(
                m.tenancies.c.move_in.desc(), m.tenancies.c.id.desc()
            )
            if not include_archived:
                query = query.where(m.tenancies.c.archived_on.is_(None))
            if flat_id is not None:
                query = query.where(m.tenancies.c.id.in_(select(m.tenancy_occupancies.c.tenancy_id).where(m.tenancy_occupancies.c.flat_id == flat_id)))
            return [
                self._tenancy_dict(conn, row) for row in conn.execute(query).mappings()
            ]

    def get_tenancy(self, tenancy_id):
        with self._read() as conn:
            return self._tenancy_dict(
                conn, self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            )

    def update_tenancy(
        self, tenancy_id, tenant_name, phone="", members=None, grace_days=0,
        *, initial_meter_reading_hundredths=UNSET,
    ):
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            reading = (tenancy["initial_meter_reading_hundredths"]
                       if initial_meter_reading_hundredths is UNSET
                       else validate_meter_reading(initial_meter_reading_hundredths))
            conn.execute(
                update(m.tenancies)
                .where(m.tenancies.c.id == tenancy_id)
                .values(
                    tenant_name=_text(tenant_name, "Tenant name", True, 200),
                    phone=_text(phone, "Phone", False, 100),
                    members=self._members(members),
                    grace_days=self._grace(grace_days),
                    initial_meter_reading_hundredths=reading,
                )
            )
            self._audit(conn, "update", "tenancy", tenancy_id,
                        previous_initial_meter_reading_hundredths=tenancy["initial_meter_reading_hundredths"],
                        initial_meter_reading_hundredths=reading)

    def _rent_rate(self, conn, tenancy_id, on):
        rate = conn.execute(
            select(m.rent_rates.c.amount_paise)
            .where(
                m.rent_rates.c.tenancy_id == tenancy_id,
                m.rent_rates.c.effective_from <= on,
            )
            .order_by(m.rent_rates.c.effective_from.desc())
            .limit(1)
        ).scalar_one_or_none()
        if rate is None:
            raise ValueError("No rent rate is defined for this period.")
        return rate

    def change_rent(self, tenancy_id, effective_from, rent_paise):
        effective = iso_date(effective_from)
        validate_paise(rent_paise)
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            self._ensure_tenancy_not_cancelled(tenancy)
            if (
                effective <= today_ist().isoformat()
                or cycle_bounds(effective, tenancy["billing_day"])[0].isoformat()
                != effective
            ):
                raise ValueError(
                    "Choose a future billing-cycle start date for the new rent."
                )
            if effective < max(tenancy["move_in"], tenancy["opening_date"]):
                raise ValueError(
                    "The new rate cannot start before this tenancy's account begins."
                )
            if tenancy["move_out"] and effective > tenancy["move_out"]:
                raise ValueError("The new rent starts after this tenancy ended.")
            if conn.execute(
                select(m.charges.c.id).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.kind == "rent",
                    m.charges.c.period_start >= effective,
                    m.charges.c.posted_at.is_not(None),
                )
            ).first():
                raise ValueError(
                    "Rent is already posted for an affected period. Correct those charges explicitly before changing this rate."
                )
            conn.execute(
                insert(m.rent_rates).values(
                    tenancy_id=tenancy_id,
                    effective_from=effective,
                    amount_paise=rent_paise,
                    created_at=_now(),
                )
            )
            # Existing drafts must use the rate actually effective in their period.
            for charge in conn.execute(
                select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.kind == "rent",
                    m.charges.c.status == "draft",
                    m.charges.c.period_start >= effective,
                    m.charges.c.partial.is_(False),
                )
            ).mappings():
                conn.execute(
                    update(m.charges)
                    .where(m.charges.c.id == charge["id"])
                    .values(
                        amount_paise=self._rent_rate(
                            conn, tenancy_id, charge["period_start"]
                        )
                    )
                )
            self._audit(
                conn,
                "change_rent",
                "tenancy",
                tenancy_id,
                effective_from=effective,
                rent_paise=rent_paise,
            )

    def cancel_rent_change(self, rate_id):
        with self._write() as conn:
            rate = self._get(conn, m.rent_rates, rate_id, "Rent rate")
            tenancy_id = rate["tenancy_id"]
            self._ensure_tenancy_not_cancelled(self._ensure_account_open(conn, tenancy_id))
            first_id = conn.execute(
                select(m.rent_rates.c.id)
                .where(m.rent_rates.c.tenancy_id == tenancy_id)
                .order_by(m.rent_rates.c.effective_from)
                .limit(1)
            ).scalar_one()
            if (
                rate_id == first_id
                or rate["effective_from"] <= today_ist().isoformat()
            ):
                raise ValueError(
                    "Only a scheduled future rent change can be cancelled; the initial and historical rates are retained."
                )
            if conn.execute(
                select(m.charges.c.id).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.kind == "rent",
                    m.charges.c.period_start >= rate["effective_from"],
                    m.charges.c.posted_at.is_not(None),
                )
            ).first():
                raise ValueError(
                    "An affected rent bill was already posted. Correct the bill explicitly before changing its schedule."
                )
            conn.execute(update(m.tenancy_occupancies).where(m.tenancy_occupancies.c.rent_rate_id == rate_id).values(rent_rate_id=None))
            conn.execute(delete(m.rent_rates).where(m.rent_rates.c.id == rate_id))
            for charge in conn.execute(
                select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.kind == "rent",
                    m.charges.c.status == "draft",
                    m.charges.c.partial.is_(False),
                    m.charges.c.period_start >= rate["effective_from"],
                )
            ).mappings():
                conn.execute(
                    update(m.charges)
                    .where(m.charges.c.id == charge["id"])
                    .values(
                        amount_paise=self._rent_rate(
                            conn, tenancy_id, charge["period_start"]
                        )
                    )
                )
            self._audit(conn, "cancel_scheduled_rate", "rent_rate", rate_id, rate=rate)

    def _prepare_drafts(self, conn, tenancy, through):
        if tenancy.get("archived_on") or tenancy.get("cancelled_on"):
            return 0
        count = 0
        existing = {
            row["cycle_start"]: dict(row) for row in conn.execute(
                select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy["id"],
                    m.charges.c.cycle_start.is_not(None),
                )
            ).mappings()
        }
        for period in iter_account_periods(tenancy, through):
            if period["cycle_start"] in existing:
                original = existing[period["cycle_start"]]
                if original["status"] == "void":
                    next_cycle = cycle_bounds(period["cycle_start"], tenancy["billing_day"])[1].isoformat()
                    replacement = conn.execute(select(m.charges.c.id).where(
                        m.charges.c.tenancy_id == tenancy["id"],
                        m.charges.c.kind == "rent", m.charges.c.opening.is_(False),
                        m.charges.c.status != "void",
                        m.charges.c.period_start >= period["cycle_start"],
                        m.charges.c.period_start < next_cycle,
                    )).first()
                    if not replacement:
                        cancelled_on = conn.execute(select(func.max(m.charges.c.voided_on)).where(
                            m.charges.c.tenancy_id == tenancy["id"],
                            m.charges.c.kind == "rent", m.charges.c.opening.is_(False),
                            m.charges.c.period_start >= period["cycle_start"],
                            m.charges.c.period_start < next_cycle,
                        )).scalar_one()
                        self._insert_charge(
                            conn, tenancy, "rent", None if period["partial"] else self._rent_rate(
                                conn, tenancy["id"], period["period_start"]),
                            period["period_start"], period["period_end"], period["period_end"],
                            "Monthly rent — review replacement bill",
                            max(period["period_start"], cancelled_on), draft=True,
                        )
                        count += 1
                continue
            conn.execute(
                insert(m.charges).values(
                    tenancy_id=tenancy["id"],
                    kind="rent",
                    amount_paise=None
                    if period["partial"]
                    else self._rent_rate(conn, tenancy["id"], period["period_start"]),
                    description="Monthly rent"
                    if not period["partial"]
                    else "Partial period — enter the agreed rent",
                    period_start=period["period_start"],
                    period_end=period["period_end"],
                    due_date=period["period_end"],
                    charge_date=period["period_start"],
                    cycle_start=period["cycle_start"],
                    status="draft",
                    partial=period["partial"],
                    opening=False,
                    created_at=_now(),
                )
            )
            count += 1
        return count

    def prepare_rent_drafts(self, as_of=None):
        through = iso_date(as_of or today_ist())
        with self._write() as conn:
            total = sum(
                self._prepare_drafts(conn, dict(tenancy), through)
                for tenancy in conn.execute(select(m.tenancies)).mappings()
            )
            if total:
                self._audit(
                    conn, "prepare_rent", "charge", count=total, through=through
                )
            return total

    def _allocation_total(self, conn, column, record_id, cutoff):
        return conn.execute(
            select(func.coalesce(func.sum(m.allocations.c.amount_paise), 0)).where(
                column == record_id,
                m.allocations.c.effective_on <= cutoff,
                _active(m.allocations.c.cancelled_on, cutoff),
            )
        ).scalar_one()

    def _charge_dict(self, conn, row, cutoff, allocated=None):
        result = dict(row)
        active = (
            result["posted_at"] is not None
            and result["charge_date"] <= cutoff
            and (result["voided_on"] is None or result["voided_on"] > cutoff)
        )
        if active:
            result["status"] = "posted"
        result["paid_paise"] = (
            self._allocation_total(
                conn, m.allocations.c.charge_id, result["id"], cutoff
            ) if allocated is None else allocated
        ) if active else 0
        result["outstanding_paise"] = (
            (result["amount_paise"] or 0) - result["paid_paise"] if active else 0
        )
        result["needs_amount"] = (
            result["status"] == "draft" and result["amount_paise"] is None
            and not result["electricity_deferred"]
        )
        return result

    def list_charges(self, tenancy_id, as_of=None, *, include_deleted=False):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            query = (
                select(m.charges)
                .where(m.charges.c.tenancy_id == tenancy_id)
                .order_by(m.charges.c.period_start, m.charges.c.id)
            )
            if not include_deleted:
                query = query.where(m.charges.c.deleted_on.is_(None))
            if as_of is not None:
                query = query.where(m.charges.c.charge_date <= cutoff)
            result = [
                self._charge_dict(conn, row, cutoff)
                for row in conn.execute(query).mappings()
            ]
            self._annotate_electricity_coverage(conn, tenancy, result, cutoff=cutoff)
            return result

    def add_charge(
        self,
        tenancy_id,
        kind,
        amount_paise,
        period_start,
        period_end,
        due_date,
        description="",
        charge_date=None,
        draft=False,
        opening=False,
        apply_credit=False,
        meter_reading_hundredths=None,
    ):
        return self.add_charges(
            tenancy_id,
            [
                {
                    "kind": kind,
                    "amount_paise": amount_paise,
                    "period_start": period_start,
                    "period_end": period_end,
                    "due_date": due_date,
                    "description": description,
                    "charge_date": charge_date,
                    "draft": draft,
                    "opening": opening,
                    "meter_reading_hundredths": meter_reading_hundredths,
                }
            ],
            apply_credit=apply_credit,
        )[0]

    def _insert_charge(
        self,
        conn,
        tenancy,
        kind,
        amount_paise,
        period_start,
        period_end,
        due_date,
        description="",
        charge_date=None,
        draft=False,
        opening=False,
        meter_reading_hundredths=None,
    ):
        self._ensure_account_open(conn, tenancy["id"])
        if kind in {"rent", "electricity"} and not opening:
            self._ensure_tenancy_not_cancelled(tenancy)
        if kind not in ("rent", "electricity", "misc"):
            raise ValueError("Choose rent, electricity or miscellaneous fees.")
        validate_meter_reading(meter_reading_hundredths)
        if kind != "electricity" and meter_reading_hundredths is not None:
            raise ValueError("Meter readings are only available for electricity bills.")
        if amount_paise is not None:
            validate_paise(amount_paise)
        elif not draft:
            raise ValueError("Enter the amount before posting the charge.")
        start, end, due = (
            iso_date(period_start),
            iso_date(period_end),
            iso_date(due_date),
        )
        charged = iso_date(charge_date or start)
        if not draft:
            self._ensure_cycle_unlocked(conn, {
                "tenancy_id": tenancy["id"], "period_start": start,
                "cycle_start": None, "electricity_cycle_start": None,
            })
        if end < start:
            raise ValueError("The period end cannot precede its start.")
        description = _text(
            description, "Reason" if kind == "misc" else "Description", kind == "misc"
        )
        if charged < tenancy["opening_date"]:
            raise ValueError(
                "The charge date cannot precede the account opening date. Use an opening entry for older dues."
            )
        cycle_start = None
        replaces = None
        partial = amount_paise is None
        if (kind == "electricity" and not opening and tenancy["move_out"]
                and start > tenancy["move_out"]):
            raise ValueError("Electricity must cover service dates on or before move-out; the bill can be recorded later.")
        if kind == "electricity" and not opening and conn.execute(select(m.charges.c.id).where(
            m.charges.c.tenancy_id == tenancy["id"],
            m.charges.c.kind == "electricity", m.charges.c.status != "void",
            m.charges.c.electricity_deferred.is_(True),
            m.charges.c.period_start <= end, m.charges.c.period_end >= start,
        ).limit(1)).first():
            raise ValueError("These service dates contain deferred electricity. Review the monthly electricity bill or undo its deferral first.")
        if kind == "rent" and not opening:
            cycle_begin, cycle_next = cycle_bounds(start, tenancy["billing_day"])
            cycle_start = cycle_begin.isoformat()
            if start < max(tenancy["move_in"], tenancy["opening_date"]) or (
                tenancy["move_out"] and end > tenancy["move_out"]
            ):
                raise ValueError(
                    "Rent must cover occupied dates within this tenancy's account."
                )
            if end >= cycle_next.isoformat():
                raise ValueError(
                    "Enter one rent charge per billing cycle; a payment can settle several cycles together."
                )
            partial = (
                partial
                or start != cycle_start
                or end != (cycle_next - timedelta(days=1)).isoformat()
            )
            existing = (
                conn.execute(
                    select(m.charges).where(
                        m.charges.c.tenancy_id == tenancy["id"],
                        m.charges.c.cycle_start == cycle_start,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                replacements = conn.execute(
                    select(m.charges.c.id).where(
                        m.charges.c.tenancy_id == tenancy["id"],
                        m.charges.c.kind == "rent",
                        m.charges.c.period_start >= cycle_start,
                        m.charges.c.period_start
                        < cycle_bounds(start, tenancy["billing_day"])[1].isoformat(),
                        m.charges.c.status != "void",
                        m.charges.c.opening.is_(False),
                    )
                ).first()
                if existing["status"] != "void" or replacements:
                    raise ValueError(
                        "Rent already exists for this billing cycle. Review or post its existing draft."
                    )
                latest_cancelled = conn.execute(select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy["id"],
                    m.charges.c.kind == "rent", m.charges.c.opening.is_(False),
                    m.charges.c.period_start >= cycle_start,
                    m.charges.c.period_start < cycle_next.isoformat(),
                    m.charges.c.status == "void",
                ).order_by(m.charges.c.voided_on.desc(), m.charges.c.id.desc()).limit(1)).mappings().one()
                if latest_cancelled["voided_on"] and charged < latest_cancelled["voided_on"]:
                    raise ValueError(
                        "The replacement charge date cannot precede the original bill's cancellation date."
                    )
                cycle_start, replaces = None, latest_cancelled["id"]
        charge_id = conn.execute(
            insert(m.charges).values(
                tenancy_id=tenancy["id"],
                kind=kind,
                amount_paise=amount_paise,
                meter_reading_hundredths=meter_reading_hundredths,
                description=description,
                period_start=start,
                period_end=end,
                due_date=due,
                charge_date=charged,
                cycle_start=cycle_start,
                status="draft" if draft else "posted",
                partial=partial,
                opening=bool(opening),
                created_at=_now(),
                posted_at=None if draft else _now(),
                replaces_charge_id=replaces,
            )
        ).inserted_primary_key[0]
        self._audit(conn, "create_draft" if draft else "post", "charge", charge_id)
        return charge_id

    def add_charges(self, tenancy_id, items, apply_credit=False):
        if not isinstance(items, list) or not items or len(items) > 100:
            raise ValueError("Provide between 1 and 100 charge lines.")
        with self._write() as conn:
            tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            result = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(
                        "Each charge must contain an amount, dates and charge type."
                    )
                try:
                    result.append(self._insert_charge(conn, tenancy, **item))
                except TypeError as exc:
                    raise ValueError(
                        "The charge fields are incomplete or invalid."
                    ) from exc
            if apply_credit:
                earliest = min(
                    self._get(conn, m.charges, charge_id)["charge_date"]
                    for charge_id in result
                )
                self._apply_credit(conn, tenancy_id, None, earliest)
            return result

    def _post_charge(self, conn, charge_id, amount_paise=None, apply_credit=False):
        charge = self._get(conn, m.charges, charge_id, "Charge")
        tenancy = self._ensure_account_open(conn, charge["tenancy_id"])
        if charge["kind"] in {"rent", "electricity"} and not charge["opening"]:
            self._ensure_tenancy_not_cancelled(tenancy)
        self._ensure_cycle_unlocked(conn, charge)
        if charge["status"] != "draft":
            raise ValueError("Only a draft can be posted.")
        if charge["electricity_deferred"]:
            raise ValueError("Undo this electricity deferral before billing this cycle separately.")
        amount = charge["amount_paise"] if amount_paise is None else amount_paise
        if amount is None:
            raise ValueError("Enter the bill amount before posting this draft.")
        validate_paise(amount)
        if charge["kind"] == "electricity" and not charge["opening"]:
            self._cover_deferred_electricity(conn, tenancy, charge)
        description = charge["description"]
        if description == "Monthly electricity — enter the bill amount":
            description = "Monthly electricity"
        elif description == "Partial period — enter the agreed rent":
            description = "Rent for partial period"
        conn.execute(
            update(m.charges)
            .where(m.charges.c.id == charge_id)
            .values(amount_paise=amount, description=description, status="posted", posted_at=_now(),
                    electricity_covered_by_charge_id=None)
        )
        if apply_credit:
            self._apply_credit(conn, charge["tenancy_id"], None, charge["charge_date"])

    def _set_meter_reading(self, conn, charge_id, meter_reading_hundredths):
        reading = validate_meter_reading(meter_reading_hundredths)
        charge = self._get(conn, m.charges, charge_id, "Bill")
        self._ensure_account_open(conn, charge["tenancy_id"])
        if charge["kind"] != "electricity":
            raise ValueError("Meter readings are only available for electricity bills.")
        if charge["status"] == "void" or charge.get("deleted_on"):
            raise ValueError("A cancelled bill's saved history cannot be changed. Open its replacement.")
        if charge["electricity_deferred"] and reading is not None:
            raise ValueError("Deferred electricity has no meter reading. Undo its deferral first.")
        if charge["meter_reading_hundredths"] == reading:
            return
        conn.execute(update(m.charges).where(m.charges.c.id == charge_id).values(
            meter_reading_hundredths=reading))
        self._audit(conn, "update_meter_reading", "charge", charge_id,
                    previous_meter_reading_hundredths=charge["meter_reading_hundredths"],
                    meter_reading_hundredths=reading)

    def set_meter_reading(self, charge_id, meter_reading_hundredths):
        """Edit optional reference data without changing money or cycle locks."""
        with self._write() as conn:
            self._set_meter_reading(conn, charge_id, meter_reading_hundredths)

    def post_charge(self, charge_id, amount_paise=None, apply_credit=False, *,
                    meter_reading_hundredths=UNSET):
        with self._write() as conn:
            if meter_reading_hundredths is not UNSET:
                self._set_meter_reading(conn, charge_id, meter_reading_hundredths)
            self._post_charge(conn, charge_id, amount_paise, apply_credit)
            self._audit(conn, "post", "charge", charge_id)

    def _void_charge(self, conn, charge_id, reason, effective):
        charge = self._get(conn, m.charges, charge_id, "Charge")
        self._ensure_account_open(conn, charge["tenancy_id"])
        self._ensure_cycle_unlocked(conn, charge)
        if charge["status"] == "void":
            raise ValueError("This charge has already been cancelled.")
        if effective < charge["charge_date"]:
            raise ValueError("A correction cannot take effect before the charge date.")
        dates = (
            conn.execute(
                select(
                    m.allocations.c.effective_on, m.allocations.c.cancelled_on
                ).where(m.allocations.c.charge_id == charge_id)
            )
            .mappings()
            .all()
        )
        if any(value and value > effective for row in dates for value in row.values()):
            raise ValueError(
                "The correction date must be on or after the last allocation or allocation correction."
            )
        conn.execute(
            update(m.charges)
            .where(m.charges.c.id == charge_id)
            .values(status="void", voided_on=effective, void_reason=reason)
        )
        conn.execute(
            update(m.allocations)
            .where(
                m.allocations.c.charge_id == charge_id,
                m.allocations.c.cancelled_on.is_(None),
            )
            .values(cancelled_on=effective, cancel_reason=reason)
        )

    def void_charge(self, charge_id, reason, effective_on=None, *, prepare_replacement=False):
        reason = _text(reason, "Correction reason", True)
        effective = iso_date(effective_on or today_ist())
        if not isinstance(prepare_replacement, bool):
            raise ValueError("Choose whether to prepare a replacement bill.")
        with self._write() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            self._ensure_account_open(conn, charge["tenancy_id"])
            self._ensure_cycle_unlocked(conn, charge)
            if charge["kind"] in {"rent", "electricity"}:
                raise ValueError(
                    "Rent and electricity bills cannot be cancelled. Correct the amount instead; it can be zero."
                )
            self._void_charge(conn, charge_id, reason, effective)
            self._audit(
                conn, "void", "charge", charge_id, effective_on=effective, reason=reason
            )

    def delete_bill(self, charge_id, reason, effective_on=None):
        """Remove a miscellaneous bill from view while retaining dated history."""
        reason = _text(reason, "Deletion reason", True)
        effective = iso_date(effective_on or today_ist())
        if effective > today_ist().isoformat():
            raise ValueError("A bill cannot be deleted on a future date.")
        with self._write() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            self._ensure_account_open(conn, charge["tenancy_id"])
            if charge["kind"] != "misc":
                raise ValueError("Rent and electricity bills cannot be deleted. Cancel or correct them instead.")
            self._ensure_cycle_unlocked(conn, charge)
            # Future bills can be removed now without ever becoming a debt.
            # Their financial cancellation starts on their original bill date.
            cancellation = max(effective, charge["charge_date"])
            returned = 0
            if charge["status"] == "void":
                if charge["voided_on"] > cancellation:
                    raise ValueError(
                        "This bill has a later scheduled cancellation. Delete it on or after "
                        f"{charge['voided_on']}; it has not been removed or changed."
                    )
            else:
                returned = self._allocation_total(
                    conn, m.allocations.c.charge_id, charge_id, cancellation)
                self._void_charge(conn, charge_id, reason, cancellation)
            conn.execute(update(m.charges).where(m.charges.c.id == charge_id).values(
                deleted_on=effective))
            self._audit(conn, "delete_bill", "charge", charge_id, reason=reason,
                        effective_on=effective, returned_to_balance_paise=returned,
                        previous_bill=dict(charge))

    def _payment_available(self, conn, payment, cutoff):
        if payment["paid_on"] > cutoff or (
            payment["reversed_on"] and payment["reversed_on"] <= cutoff
        ):
            return 0
        allocated = self._allocation_total(
            conn, m.allocations.c.payment_id, payment["id"], cutoff
        )
        returned = conn.execute(
            select(func.coalesce(func.sum(m.refund_sources.c.amount_paise), 0))
            .select_from(m.refund_sources.join(m.refunds))
            .where(
                m.refund_sources.c.payment_id == payment["id"],
                m.refunds.c.paid_on <= cutoff,
                _active(m.refunds.c.reversed_on, cutoff),
            )
        ).scalar_one()
        return payment["amount_paise"] - allocated - returned - self._payment_deposit_total(conn, payment["id"], cutoff)

    def _available_since(self, conn, payment, effective):
        """Do not make a backdated allocation/refund overspend money at any cutoff."""
        dates = {effective, FAR_FUTURE}
        dates.update(self._payment_deposit_dates(conn, payment["id"], effective))
        for row in conn.execute(
            select(m.allocations.c.effective_on, m.allocations.c.cancelled_on).where(
                m.allocations.c.payment_id == payment["id"]
            )
        ).mappings():
            dates.update(
                value for value in row.values() if value and value >= effective
            )
        for row in conn.execute(
            select(m.refunds.c.paid_on, m.refunds.c.reversed_on)
            .select_from(m.refunds.join(m.refund_sources))
            .where(m.refund_sources.c.payment_id == payment["id"])
        ).mappings():
            dates.update(
                value for value in row.values() if value and value >= effective
            )
        return min(self._payment_available(conn, payment, cutoff) for cutoff in dates)

    def _charge_available_since(self, conn, charge, effective):
        dates = {effective, FAR_FUTURE}
        for row in conn.execute(
            select(m.allocations.c.effective_on, m.allocations.c.cancelled_on).where(
                m.allocations.c.charge_id == charge["id"]
            )
        ).mappings():
            dates.update(
                value for value in row.values() if value and value >= effective
            )
        return min(
            charge["amount_paise"]
            - self._allocation_total(
                conn, m.allocations.c.charge_id, charge["id"], cutoff
            )
            for cutoff in dates
        )

    def _allocate(self, conn, payment, charge, amount, effective):
        validate_paise(amount, allow_zero=False)
        self._ensure_cycle_unlocked(conn, charge)
        if payment["tenancy_id"] != charge["tenancy_id"]:
            raise ValueError("A payment can only settle charges from the same tenancy.")
        if charge["status"] != "posted" or payment["reversed_on"]:
            raise ValueError(
                "Only a valid payment and a posted charge can be allocated."
            )
        effective = max(effective, payment["paid_on"], charge["charge_date"])
        if amount > self._available_since(conn, payment, effective):
            raise ValueError(
                "The allocation exceeds available credit for this payment."
            )
        if amount > self._charge_available_since(conn, charge, effective):
            raise ValueError("The allocation exceeds the unpaid amount of this charge.")
        conn.execute(
            insert(m.allocations).values(
                payment_id=payment["id"],
                charge_id=charge["id"],
                amount_paise=amount,
                effective_on=effective,
                created_at=_now(),
            )
        )

    def _validate_full_bill_payment(self, conn, charge, amount, effective):
        validate_paise(amount, allow_zero=False)
        self._ensure_cycle_unlocked(conn, charge)
        if effective > today_ist().isoformat():
            raise ValueError("A bill payment cannot have a future date.")
        if charge["status"] != "posted" or charge["charge_date"] > effective:
            raise ValueError("Choose a posted bill dated on or before the payment date.")
        remaining = charge["amount_paise"] - self._allocation_total(
            conn, m.allocations.c.charge_id, charge["id"], effective)
        if amount != remaining:
            raise ValueError("Pay the full remaining bill amount. Partial payments are not allowed.")
        if amount > self._charge_available_since(conn, charge, effective):
            raise ValueError("Later payment history prevents paying this bill on that date.")

    def _allocate_payment(self, conn, payment, requested, effective):
        if requested is not None and not isinstance(requested, dict):
            raise ValueError(
                "Payment allocations must map charge IDs to amounts in paise."
            )
        if requested is None:
            # Receiving money never changes a bill's Paid/Unpaid state.
            return
        if (
            sum(validate_paise(amount) for amount in requested.values())
            > payment["amount_paise"]
        ):
            raise ValueError("Allocations cannot exceed the payment amount.")
        for charge_id, amount in requested.items():
            if amount:
                charge = self._get(conn, m.charges, charge_id, "Charge")
                self._validate_full_bill_payment(conn, charge, amount, effective)
                self._allocate(conn, payment, charge, amount, effective)

    def _open_charges(self, conn, tenancy_id):
        used = (
            select(
                m.allocations.c.charge_id,
                func.sum(m.allocations.c.amount_paise).label("amount"),
            )
            .where(m.allocations.c.cancelled_on.is_(None))
            .group_by(m.allocations.c.charge_id)
            .subquery()
        )
        query = (
            select(m.charges)
            .outerjoin(used, used.c.charge_id == m.charges.c.id)
            .where(
                m.charges.c.tenancy_id == tenancy_id,
                m.charges.c.status == "posted",
                m.charges.c.amount_paise > func.coalesce(used.c.amount, 0),
            )
            .order_by(m.charges.c.due_date, m.charges.c.period_start, m.charges.c.id)
        )
        return [dict(row) for row in conn.execute(query).mappings()]

    def payment_candidates(self, tenancy_id, effective_on):
        effective = iso_date(effective_on)
        with self._read() as conn:
            self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            candidates = []
            for charge in self._open_charges(conn, tenancy_id):
                if charge["charge_date"] > effective:
                    continue
                amount = self._charge_available_since(conn, charge, effective)
                if amount > 0:
                    result = self._charge_dict(conn, charge, effective)
                    result["outstanding_paise"] = amount
                    candidates.append(result)
            return candidates

    def _open_payments(self, conn, tenancy_id):
        used = (
            select(
                m.allocations.c.payment_id,
                func.sum(m.allocations.c.amount_paise).label("amount"),
            )
            .where(m.allocations.c.cancelled_on.is_(None))
            .group_by(m.allocations.c.payment_id)
            .subquery()
        )
        returned = (
            select(
                m.refund_sources.c.payment_id,
                func.sum(m.refund_sources.c.amount_paise).label("amount"),
            )
            .join(m.refunds)
            .where(m.refunds.c.reversed_on.is_(None))
            .group_by(m.refund_sources.c.payment_id)
            .subquery()
        )
        held = (
            select(m.deposit_sources.c.payment_id,
                   func.sum(m.deposit_sources.c.amount_paise).label("amount"))
            .join(m.deposits)
            .where(m.deposits.c.reversed_on.is_(None))
            .group_by(m.deposit_sources.c.payment_id).subquery()
        )
        query = (
            select(m.payments)
            .outerjoin(used, used.c.payment_id == m.payments.c.id)
            .outerjoin(returned, returned.c.payment_id == m.payments.c.id)
            .outerjoin(held, held.c.payment_id == m.payments.c.id)
            .where(
                m.payments.c.tenancy_id == tenancy_id,
                m.payments.c.reversed_on.is_(None),
                m.payments.c.amount_paise
                > func.coalesce(used.c.amount, 0) + func.coalesce(returned.c.amount, 0) + func.coalesce(held.c.amount, 0),
            )
            .order_by(m.payments.c.paid_on, m.payments.c.id)
        )
        return [dict(row) for row in conn.execute(query).mappings()]

    def _allocation_opportunity(self, conn, payment, charge, effective):
        start = max(effective, payment["paid_on"], charge["charge_date"])
        dates = {start}
        dates.update(self._payment_deposit_dates(conn, payment["id"], start))
        release_dates = conn.execute(
            select(m.allocations.c.cancelled_on).where(
                or_(
                    m.allocations.c.payment_id == payment["id"],
                    m.allocations.c.charge_id == charge["id"],
                ),
                m.allocations.c.cancelled_on >= start,
            )
        ).scalars()
        dates.update(release_dates)
        dates.update(
            conn.execute(
                select(m.refunds.c.reversed_on)
                .join(m.refund_sources)
                .where(
                    m.refund_sources.c.payment_id == payment["id"],
                    m.refunds.c.reversed_on >= start,
                )
            ).scalars()
        )
        for on in sorted(dates):
            amount = min(
                self._available_since(conn, payment, on),
                self._charge_available_since(conn, charge, on),
            )
            if amount > 0:
                return on, amount
        return start, 0

    def _apply_credit(self, conn, tenancy_id, requested, effective):
        candidates = [payment for payment in self._open_payments(conn, tenancy_id)
                      if payment["paid_on"] <= effective]
        if requested is None:
            # Compatibility for callers that explicitly ask to apply balance:
            # each bill is either fully paid or left completely unchanged.
            for charge in self._open_charges(conn, tenancy_id):
                if charge["charge_date"] > effective:
                    continue
                remaining = charge["amount_paise"] - self._allocation_total(
                    conn, m.allocations.c.charge_id, charge["id"], effective)
                available = sum(self._available_since(conn, payment, effective) for payment in candidates)
                if remaining > 0 and remaining <= available:
                    self._apply_credit(conn, tenancy_id, {charge["id"]: remaining}, effective)
            return
        if not isinstance(requested, dict):
            raise ValueError(
                "Credit allocations must map charge IDs to amounts in paise."
            )
        for charge_id, amount in requested.items():
            validate_paise(amount)
            charge = self._get(conn, m.charges, charge_id, "Charge")
            if charge["tenancy_id"] != tenancy_id or charge["status"] != "posted":
                raise ValueError("Choose posted charges from this tenancy.")
            if amount == 0:
                continue
            self._validate_full_bill_payment(conn, charge, amount, effective)
            remaining = amount
            for payment in candidates:
                applied = min(remaining, self._available_since(conn, payment, effective))
                if applied:
                    self._allocate(conn, payment, charge, applied, effective)
                    remaining -= applied
                if not remaining:
                    break
            if remaining:
                raise ValueError(
                    "There is not enough available balance to pay the full bill amount."
                )

    def apply_credit(self, tenancy_id, allocations=None, effective_on=None):
        effective = iso_date(effective_on or today_ist())
        if effective > today_ist().isoformat():
            raise ValueError("A bill payment cannot have a future date.")
        with self._write() as conn:
            self._ensure_account_open(conn, tenancy_id)
            before = conn.execute(
                select(func.count()).select_from(m.allocations)
            ).scalar_one()
            self._apply_credit(conn, tenancy_id, allocations, effective)
            after = conn.execute(
                select(func.count()).select_from(m.allocations)
            ).scalar_one()
            if after != before:
                self._audit(
                    conn, "apply_credit", "tenancy", tenancy_id, effective_on=effective
                )

    def _copy_proof(self, source):
        if not source:
            return None, None
        path = Path(source)
        if not path.is_file():
            raise ValueError("The selected proof file does not exist.")
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError(
                "Payment proof must be a nonempty file no larger than 20 MB."
            )
        extension = path.suffix.lower()
        with path.open("rb") as handle:
            signature = handle.read(8)
        valid = (
            (extension == ".pdf" and signature.startswith(b"%PDF-"))
            or (
                extension in (".jpg", ".jpeg") and signature.startswith(b"\xff\xd8\xff")
            )
            or (extension == ".png" and signature == b"\x89PNG\r\n\x1a\n")
        )
        if not valid:
            raise ValueError("Choose a valid PDF, JPG or PNG payment proof.")
        folder = self.data_dir / "attachments"
        folder.mkdir(exist_ok=True)
        relative = "attachments/" + uuid4().hex + extension
        shutil.copyfile(path, self.data_dir / relative)
        return relative, path.name

    def _record_payment(
        self,
        conn,
        tenancy_id,
        amount_paise,
        paid_on,
        method,
        reference="",
        notes="",
        requested=None,
        proof=None,
        proof_name=None,
        kind="payment",
    ):
        validate_paise(amount_paise, allow_zero=False)
        tenancy = self._ensure_account_open(conn, tenancy_id)
        if paid_on < tenancy["opening_date"]:
            raise ValueError(
                "The payment date cannot precede the account opening date. Enter an opening advance instead."
            )
        payment_id = conn.execute(
            insert(m.payments).values(
                tenancy_id=tenancy_id,
                amount_paise=amount_paise,
                paid_on=paid_on,
                method=method,
                kind=kind,
                reference=_text(reference, "Reference", False, 1000),
                notes=_text(notes, "Notes"),
                proof_path=proof,
                proof_name=proof_name,
                created_at=_now(),
            )
        ).inserted_primary_key[0]
        payment = self._get(conn, m.payments, payment_id)
        self._allocate_payment(conn, payment, requested, paid_on)
        return payment_id

    def record_payment(
        self,
        tenancy_id,
        amount_paise,
        paid_on,
        method,
        reference="",
        notes="",
        allocations=None,
        proof_path=None,
        opening=False,
    ):
        paid = iso_date(paid_on)
        if not opening and paid > today_ist().isoformat():
            raise ValueError("A received payment cannot have a future date.")
        method = "opening" if opening else _method(method)
        copied = None
        try:
            with self._write() as conn:
                copied, original_name = self._copy_proof(proof_path)
                payment_id = self._record_payment(
                    conn,
                    tenancy_id,
                    amount_paise,
                    paid,
                    method,
                    reference,
                    notes,
                    allocations,
                    copied,
                    original_name,
                    "opening" if opening else "payment",
                )
                self._audit(
                    conn,
                    "opening_payment" if opening else "receive",
                    "payment",
                    payment_id,
                )
                return payment_id
        except BaseException:
            if copied:
                (self.data_dir / copied).unlink(missing_ok=True)
            raise

    def _payment_dict(self, conn, row, cutoff):
        result = dict(row)
        result["opening"] = result["kind"] == "opening"
        if result["method"] == "internal":
            result["method"] = "deposit"
        if result["reversed_on"] and result["reversed_on"] > cutoff:
            result["reversed_on"] = None
        result["available_paise"] = self._payment_available(conn, row, cutoff)
        result["allocations"] = [
            dict(allocation)
            for allocation in conn.execute(
                select(
                    m.allocations.c.charge_id,
                    m.allocations.c.amount_paise,
                    m.allocations.c.effective_on,
                ).where(
                    m.allocations.c.payment_id == result["id"],
                    m.allocations.c.effective_on <= cutoff,
                    _active(m.allocations.c.cancelled_on, cutoff),
                )
            ).mappings()
        ]
        return result

    def list_payments(self, tenancy_id, as_of=None):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            return [
                self._payment_dict(conn, row, cutoff)
                for row in conn.execute(
                    select(m.payments)
                    .where(
                        m.payments.c.tenancy_id == tenancy_id,
                        m.payments.c.paid_on <= cutoff,
                    )
                    .order_by(m.payments.c.paid_on, m.payments.c.id)
                ).mappings()
            ]

    def reverse_payment(self, payment_id, reason, effective_on=None):
        reason = _text(reason, "Correction reason", True)
        effective = iso_date(effective_on or today_ist())
        with self._write() as conn:
            self._reverse_payment(conn, payment_id, reason, effective)

    def _reverse_payment(self, conn, payment_id, reason, effective):
        payment = self._get(conn, m.payments, payment_id, "Payment")
        self._ensure_account_open(conn, payment["tenancy_id"])
        if payment["reversed_on"]:
            raise ValueError("This payment was already reversed.")
        if effective < payment["paid_on"]:
            raise ValueError("The reversal cannot precede the payment date.")
        self._ensure_payment_deposit_reversal(conn, payment_id, effective)
        # Releasing only this receipt could leave a bill partly paid. Require
        # the bill to be marked Unpaid first when it used several receipts.
        affected = conn.execute(select(m.charges).join(m.allocations).where(
            m.allocations.c.payment_id == payment_id,
            m.allocations.c.cancelled_on.is_(None),
        ).distinct()).mappings().all()
        for charge in affected:
            self._ensure_cycle_unlocked(conn, charge)
            if conn.execute(select(m.allocations.c.id).where(
                m.allocations.c.charge_id == charge["id"],
                m.allocations.c.payment_id != payment_id,
                m.allocations.c.cancelled_on.is_(None),
            )).first():
                raise ValueError(
                    "This receipt shares a bill with other payments. Mark that bill Unpaid "
                    "before correcting the receipt, so no partial payment is left."
                )
        if conn.execute(
            select(m.refund_sources.c.id)
            .join(m.refunds)
            .where(
                m.refund_sources.c.payment_id == payment_id,
                m.refunds.c.reversed_on.is_(None),
            )
        ).first():
            raise ValueError(
                "Some of this payment was refunded. Correct that refund before reversing the payment."
            )
        dates = (
            conn.execute(
                select(
                    m.allocations.c.effective_on, m.allocations.c.cancelled_on
                ).where(m.allocations.c.payment_id == payment_id)
            )
            .mappings()
            .all()
        )
        refund_dates = (
            conn.execute(
                select(m.refunds.c.reversed_on)
                .join(m.refund_sources)
                .where(m.refund_sources.c.payment_id == payment_id)
            )
            .scalars()
            .all()
        )
        if any(
            value and value > effective for row in dates for value in row.values()
        ) or any(value and value > effective for value in refund_dates):
            raise ValueError(
                "The reversal date must be on or after related allocations and refund corrections."
            )
        conn.execute(
            update(m.payments)
            .where(m.payments.c.id == payment_id)
            .values(reversed_on=effective, reverse_reason=reason)
        )
        conn.execute(
            update(m.allocations)
            .where(
                m.allocations.c.payment_id == payment_id,
                m.allocations.c.cancelled_on.is_(None),
            )
            .values(cancelled_on=effective, cancel_reason=reason)
        )
        # Reversing an internal deposit transfer restores the separate deposit.
        conn.execute(
            update(m.deposits)
            .where(m.deposits.c.payment_id == payment_id, m.deposits.c.kind == "apply")
            .values(reversed_on=effective, reverse_reason=reason)
        )
        self._audit(
            conn,
            "reverse",
            "payment",
            payment_id,
            effective_on=effective,
            reason=reason,
        )

    def refund_credit(self, tenancy_id, amount_paise, paid_on, method, reason):
        validate_paise(amount_paise, allow_zero=False)
        paid = iso_date(paid_on)
        method = _method(method)
        reason = _text(reason, "Refund reason", True)
        with self._write() as conn:
            self._ensure_account_open(conn, tenancy_id)
            candidates = [
                dict(row)
                for row in conn.execute(
                    select(m.payments)
                    .where(
                        m.payments.c.tenancy_id == tenancy_id,
                        m.payments.c.reversed_on.is_(None),
                        m.payments.c.paid_on <= paid,
                    )
                    .order_by(m.payments.c.paid_on, m.payments.c.id)
                ).mappings()
            ]
            available = [
                (payment, self._available_since(conn, payment, paid))
                for payment in candidates
            ]
            if sum(amount for _, amount in available) < amount_paise:
                raise ValueError(
                    "The refund exceeds available advance credit on that date."
                )
            refund_id = conn.execute(
                insert(m.refunds).values(
                    tenancy_id=tenancy_id,
                    amount_paise=amount_paise,
                    paid_on=paid,
                    method=method,
                    notes=reason,
                    created_at=_now(),
                )
            ).inserted_primary_key[0]
            remaining = amount_paise
            for payment, amount in available:
                amount = min(amount, remaining)
                if amount:
                    conn.execute(
                        insert(m.refund_sources).values(
                            refund_id=refund_id,
                            payment_id=payment["id"],
                            amount_paise=amount,
                        )
                    )
                    remaining -= amount
                if not remaining:
                    break
            self._audit(conn, "refund", "refund", refund_id)
            return refund_id

    def reverse_refund(self, refund_id, reason, effective_on=None):
        reason = _text(reason, "Correction reason", True)
        effective = iso_date(effective_on or today_ist())
        with self._write() as conn:
            refund = self._get(conn, m.refunds, refund_id, "Refund")
            self._ensure_account_open(conn, refund["tenancy_id"])
            if refund["reversed_on"] or effective < refund["paid_on"]:
                raise ValueError(
                    "Choose an unreversed refund and a correction date on or after its payment date."
                )
            conn.execute(
                update(m.refunds)
                .where(m.refunds.c.id == refund_id)
                .values(reversed_on=effective, reverse_reason=reason)
            )
            self._audit(
                conn,
                "reverse",
                "refund",
                refund_id,
                effective_on=effective,
                reason=reason,
            )

    def _deposit_balance(self, conn, tenancy_id, cutoff):
        movements = conn.execute(
            select(m.deposits.c.kind, m.deposits.c.amount_paise).where(
                m.deposits.c.tenancy_id == tenancy_id,
                m.deposits.c.paid_on <= cutoff,
                _active(m.deposits.c.reversed_on, cutoff),
            )
        ).mappings()
        return sum(
            row["amount_paise"] * (1 if row["kind"] in ("receipt", "opening") else -1)
            for row in movements
        )

    def _deposit_available_since(self, conn, tenancy_id, effective):
        dates = {effective, FAR_FUTURE}
        for row in conn.execute(
            select(m.deposits.c.paid_on, m.deposits.c.reversed_on).where(
                m.deposits.c.tenancy_id == tenancy_id
            )
        ).mappings():
            dates.update(
                value for value in row.values() if value and value >= effective
            )
        return min(self._deposit_balance(conn, tenancy_id, cutoff) for cutoff in dates)

    def record_deposit(
        self,
        tenancy_id,
        amount_paise,
        paid_on,
        action,
        method="cash",
        notes="",
        opening=False,
    ):
        validate_paise(amount_paise)
        paid = iso_date(paid_on)
        if action not in ("receive", "return", "apply"):
            raise ValueError("Choose receive, return or apply deposit.")
        if opening and action != "receive":
            raise ValueError("An opening deposit records money already held.")
        notes = _text(notes, "Deposit note/reason", action != "receive")
        method = (
            "opening"
            if opening
            else ("internal" if action == "apply" else _method(method))
        )
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            if action == "receive":
                self._ensure_tenancy_not_cancelled(tenancy)
            if paid < tenancy["opening_date"]:
                raise ValueError(
                    "The deposit date cannot precede the account opening date."
                )
            if not amount_paise:
                return 0
            if (
                action != "receive"
                and self._deposit_available_since(conn, tenancy_id, paid) < amount_paise
            ):
                raise ValueError("The amount exceeds the deposit held on that date.")
            payment_id = None
            if action == "apply":
                payment_id = self._record_payment(
                    conn,
                    tenancy_id,
                    amount_paise,
                    paid,
                    "internal",
                    notes=notes,
                    kind="deposit_transfer",
                )
            kind = (
                "opening"
                if opening
                else {"receive": "receipt", "return": "refund", "apply": "apply"}[
                    action
                ]
            )
            deposit_id = conn.execute(
                insert(m.deposits).values(
                    tenancy_id=tenancy_id,
                    kind=kind,
                    amount_paise=amount_paise,
                    paid_on=paid,
                    method=method,
                    notes=notes,
                    payment_id=payment_id,
                    created_at=_now(),
                )
            ).inserted_primary_key[0]
            self._audit(conn, action, "deposit", deposit_id)
            return deposit_id

    def reverse_deposit(self, deposit_id, reason, effective_on=None):
        reason = _text(reason, "Correction reason", True)
        effective = iso_date(effective_on or today_ist())
        with self._write() as conn:
            deposit = self._get(conn, m.deposits, deposit_id, "Deposit movement")
            self._ensure_account_open(conn, deposit["tenancy_id"])
            if deposit["reversed_on"]:
                raise ValueError("This deposit entry was already reversed.")
            if effective < deposit["paid_on"]:
                raise ValueError(
                    "The correction cannot precede the deposit movement's date."
                )
            if deposit["kind"] == "apply":
                self._reverse_payment(conn, deposit["payment_id"], reason, effective)
            else:
                if (
                    deposit["kind"] in ("receipt", "opening")
                    and self._deposit_available_since(
                        conn, deposit["tenancy_id"], effective
                    )
                    < deposit["amount_paise"]
                ):
                    raise ValueError(
                        "Some of this deposit has been used or returned. Correct those movements before reversing the receipt."
                    )
                conn.execute(
                    update(m.deposits)
                    .where(m.deposits.c.id == deposit_id)
                    .values(reversed_on=effective, reverse_reason=reason)
                )
            self._audit(
                conn,
                "reverse",
                "deposit",
                deposit_id,
                effective_on=effective,
                reason=reason,
            )

    def _balance(self, conn, tenancy_id, cutoff):
        tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
        charge_rows = (
            conn.execute(
                select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.posted_at.is_not(None),
                    m.charges.c.charge_date <= cutoff,
                    _active(m.charges.c.voided_on, cutoff),
                )
            )
            .mappings()
            .all()
        )
        allocated = dict(
            conn.execute(
                select(
                    m.allocations.c.charge_id, func.sum(m.allocations.c.amount_paise)
                )
                .join(m.charges)
                .where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.allocations.c.effective_on <= cutoff,
                    _active(m.allocations.c.cancelled_on, cutoff),
                )
                .group_by(m.allocations.c.charge_id)
            ).all()
        )
        billed = outstanding = overdue = 0
        for row in charge_rows:
            billed += row["amount_paise"]
            unpaid = row["amount_paise"] - allocated.get(row["id"], 0)
            outstanding += unpaid
            if as_date(row["due_date"]) + timedelta(
                days=tenancy["grace_days"]
            ) < as_date(cutoff):
                overdue += unpaid
        paid = conn.execute(
            select(func.coalesce(func.sum(m.payments.c.amount_paise), 0)).where(
                m.payments.c.tenancy_id == tenancy_id,
                m.payments.c.paid_on <= cutoff,
                _active(m.payments.c.reversed_on, cutoff),
            )
        ).scalar_one()
        refunded = conn.execute(
            select(func.coalesce(func.sum(m.refunds.c.amount_paise), 0)).where(
                m.refunds.c.tenancy_id == tenancy_id,
                m.refunds.c.paid_on <= cutoff,
                _active(m.refunds.c.reversed_on, cutoff),
            )
        ).scalar_one()
        transferred = self._balance_deposit_total(conn, tenancy_id, cutoff)
        credit = paid - sum(allocated.values()) - refunded - transferred
        return {
            "billed_paise": billed,
            "paid_paise": paid,
            "refunded_paise": refunded,
            "outstanding_paise": outstanding,
            "overdue_paise": overdue,
            "credit_paise": credit,
            "net_paise": billed - paid + refunded + transferred,
            "transferred_to_deposit_paise": transferred,
            "deposit_paise": self._deposit_balance(conn, tenancy_id, cutoff),
        }

    def balance(self, tenancy_id, as_of=None):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            return self._balance(conn, tenancy_id, cutoff)

    def dashboard(self, as_of=None):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            result = {
                "outstanding_paise": 0,
                "overdue_paise": 0,
                "credit_paise": 0,
                "deposit_paise": 0,
            }
            for tenancy_id in conn.execute(select(m.tenancies.c.id)).scalars():
                balance = self._balance(conn, tenancy_id, cutoff)
                for key in result:
                    result[key] += balance[key]
            result["total_flats"] = conn.execute(
                select(func.count()).select_from(m.flats)
            ).scalar_one()
            result["occupied_flats"] = conn.execute(
                select(func.count(func.distinct(m.tenancy_occupancies.c.flat_id)))
                .select_from(m.tenancy_occupancies.join(m.tenancies, m.tenancies.c.id == m.tenancy_occupancies.c.tenancy_id)).where(
                    m.tenancies.c.cancelled_on.is_(None),
                    m.tenancy_occupancies.c.starts_on <= cutoff,
                    or_(
                        m.tenancy_occupancies.c.ends_on.is_(None),
                        m.tenancy_occupancies.c.ends_on >= cutoff,
                    ),
                )
            ).scalar_one()
            result["draft_count"] = conn.execute(
                select(func.count())
                .select_from(m.charges)
                .where(
                    m.charges.c.status == "draft", m.charges.c.period_start <= cutoff,
                    m.charges.c.electricity_deferred.is_(False),
                )
            ).scalar_one()
            return result

    def set_opening_balances(
        self,
        tenancy_id,
        opening_date,
        rent_paise=0,
        electricity_paise=0,
        misc_paise=0,
        misc_reason="",
        advance_paise=0,
        deposit_paise=0,
    ):
        opening = iso_date(opening_date)
        for amount in (
            rent_paise,
            electricity_paise,
            misc_paise,
            advance_paise,
            deposit_paise,
        ):
            validate_paise(amount)
        if misc_paise:
            misc_reason = _text(misc_reason, "Opening miscellaneous reason", True)
        key = f"opening_initialized_{tenancy_id}"
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            self._ensure_tenancy_not_cancelled(tenancy)
            if opening < tenancy["move_in"]:
                raise ValueError("The opening date cannot precede move-in.")
            if conn.execute(
                select(m.settings.c.key).where(m.settings.c.key == key)
            ).first():
                raise ValueError(
                    "Opening balances were already recorded. Use explicit corrections for any changes."
                )
            for table in (m.charges, m.payments, m.deposits):
                query = select(table.c.id).where(table.c.tenancy_id == tenancy_id)
                if table is m.charges:
                    query = query.where(or_(
                        table.c.posted_at.is_not(None), table.c.deleted_on.is_not(None),
                        table.c.replaces_charge_id.is_not(None),
                    ))
                if conn.execute(query).first():
                    raise ValueError(
                        "Record opening balances before posting, deleting or restoring bills, or receiving money for this account."
                    )
            conn.execute(
                update(m.tenancies)
                .where(m.tenancies.c.id == tenancy_id)
                .values(opening_date=opening)
            )
            # Unposted drafts are provisional and can be rebuilt from the new start.
            conn.execute(
                delete(m.charges).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.posted_at.is_(None),
                )
            )
            tenancy["opening_date"] = opening
            for kind, amount, description in (
                ("rent", rent_paise, "Opening unpaid rent"),
                ("electricity", electricity_paise, "Opening unpaid electricity"),
                ("misc", misc_paise, misc_reason),
            ):
                if amount:
                    self._insert_charge(
                        conn,
                        tenancy,
                        kind,
                        amount,
                        opening,
                        opening,
                        opening,
                        description,
                        opening,
                        opening=True,
                    )
            if advance_paise:
                self._record_payment(
                    conn,
                    tenancy_id,
                    advance_paise,
                    opening,
                    "opening",
                    notes="Opening advance already received",
                    kind="opening",
                )
            if deposit_paise:
                conn.execute(
                    insert(m.deposits).values(
                        tenancy_id=tenancy_id,
                        kind="opening",
                        amount_paise=deposit_paise,
                        paid_on=opening,
                        method="opening",
                        notes="Opening security deposit already held",
                        created_at=_now(),
                    )
                )
            conn.execute(insert(m.settings).values(key=key, value=opening))
            self._prepare_drafts(conn, tenancy, today_ist().isoformat())
            self._audit(
                conn, "set_opening", "tenancy", tenancy_id, opening_date=opening
            )

    def statement(self, tenancy_id, start, end, *, as_of=None):
        """Selected billing periods with balances/status as of the current IST date.

        ``as_of`` supports explicitly dated accounting snapshots. The selected
        period's movement totals reconcile separately from the current balance.
        """
        from .clock import now_ist
        from .statement_cycles import billing_cycles

        start, end = iso_date(start), iso_date(end)
        if end < start:
            raise ValueError("The statement end cannot precede its start.")
        with self._read() as conn:
            generated = now_ist()
            cutoff = generated.date().isoformat() if as_of is None else iso_date(as_of)
            period_end = min(end, cutoff)
            tenancy = self._tenancy_dict(
                conn, self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            )
            locations = tenancy.get("occupancy_history", [])
            at_cutoff = [
                location for location in locations
                if location["starts_on"] <= cutoff
                and (location.get("ends_on") is None or location["ends_on"] >= cutoff)
            ]
            if not at_cutoff:
                at_cutoff = [location for location in locations if location["starts_on"] <= cutoff]
            if at_cutoff:
                location = at_cutoff[-1]
                tenancy.update({key: location[key] for key in (
                    "flat_id", "building_id", "building_name", "address", "number", "floor", "bhk"
                ) if key in location})
            previous = (
                (as_date(start) - timedelta(days=1)).isoformat()
                if start > "0001-01-01"
                else start
            )
            opening = (
                self._balance(conn, tenancy_id, min(previous, cutoff))["net_paise"]
                if start > "0001-01-01"
                else 0
            )
            balance = self._balance(conn, tenancy_id, cutoff)
            period_balance = (
                balance if period_end == cutoff else self._balance(conn, tenancy_id, period_end)
            )
            all_charges = [
                dict(row)
                for row in conn.execute(
                    select(m.charges)
                    .where(
                        m.charges.c.tenancy_id == tenancy_id,
                        m.charges.c.posted_at.is_not(None),
                        m.charges.c.charge_date <= cutoff,
                    )
                    .order_by(m.charges.c.charge_date, m.charges.c.id)
                ).mappings()
            ]
            as_of_charges = {
                row["id"]: self._charge_dict(conn, row, cutoff) for row in all_charges
            }
            # Removed bills stay in the accounting ledger so dated balances
            # remain exact, but newly generated tenant documents omit them.
            deleted_ids = {row["id"] for row in all_charges if row.get("deleted_on")}
            charge_rows = [as_of_charges[row["id"]] for row in all_charges if start <= row["charge_date"] <= period_end]
            outstanding = [
                as_of_charges[row["id"]]
                for row in all_charges
                if (row["voided_on"] is None or row["voided_on"] > cutoff)
            ]
            outstanding = [row for row in outstanding if row["outstanding_paise"] > 0 and row["id"] not in deleted_ids]
            cycle_charges = []
            for raw in conn.execute(
                select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy_id,
                    m.charges.c.period_start <= end,
                    m.charges.c.period_end >= start,
                ).order_by(m.charges.c.period_start, m.charges.c.id)
            ).mappings():
                if raw.get("deleted_on"):
                    continue
                if tenancy.get("cancelled_on") and not raw["opening"] and raw["kind"] in {"rent", "electricity"}:
                    continue
                if (as_of is None and tenancy.get("move_out") and not raw["opening"]
                        and raw["period_start"] > tenancy["move_out"] and raw["status"] == "void"):
                    # A stopped future draft must not reappear as a live cycle
                    # just because its dated correction is still in the future.
                    continue
                row = as_of_charges.get(raw["id"])
                if row is None:
                    row = dict(raw, paid_paise=0, outstanding_paise=0)
                    if row["status"] == "void" and row["posted_at"] is None and row["voided_on"] and row["voided_on"] > cutoff:
                        row["status"] = "draft"
                cycle_charges.append(dict(
                    row,
                    billed_at_cutoff=row["id"] in as_of_charges and row["status"] == "posted",
                ))
            self._annotate_electricity_coverage(conn, tenancy, cycle_charges, cutoff=cutoff)
            self._annotate_electricity_coverage(conn, tenancy, list(as_of_charges.values()), cutoff=cutoff)
            period_summaries = billing_cycles(tenancy, start, end, cycle_charges, require_monthly=as_of is None)
            cycle_ids = {charge_id for period in period_summaries for charge_id in period["charge_ids"]}
            all_payments = [
                dict(row)
                for row in conn.execute(
                    select(m.payments)
                    .where(
                        m.payments.c.tenancy_id == tenancy_id,
                        m.payments.c.paid_on <= cutoff,
                    )
                    .order_by(m.payments.c.paid_on, m.payments.c.id)
                ).mappings()
            ]
            payment_rows = [
                self._payment_dict(conn, row, cutoff)
                for row in all_payments
                if start <= row["paid_on"] <= period_end
            ]
            for payment in payment_rows:
                payment["allocations"] = [
                    allocation for allocation in payment["allocations"]
                    if allocation["charge_id"] not in deleted_ids
                ]
            all_refunds = [
                dict(row)
                for row in conn.execute(
                    select(m.refunds)
                    .where(
                        m.refunds.c.tenancy_id == tenancy_id, m.refunds.c.paid_on <= cutoff
                    )
                    .order_by(m.refunds.c.paid_on, m.refunds.c.id)
                ).mappings()
            ]
            refund_rows = [
                dict(row, reason=row["notes"])
                for row in all_refunds
                if start <= row["paid_on"] <= period_end
            ]
            all_deposits = [
                dict(row)
                for row in conn.execute(
                    select(m.deposits)
                    .where(
                        m.deposits.c.tenancy_id == tenancy_id,
                        m.deposits.c.paid_on <= cutoff,
                    )
                    .order_by(m.deposits.c.paid_on, m.deposits.c.id)
                ).mappings()
            ]
            deposit_rows = [
                dict(
                    row,
                    action="from_balance" if row["kind"] == "receipt" and row["method"] == "internal" else {
                        "receipt": "receive",
                        "opening": "receive",
                        "refund": "return",
                        "apply": "apply",
                    }[row["kind"]],
                    opening=row["kind"] == "opening",
                    reversed_on=row["reversed_on"]
                    if row["reversed_on"] and row["reversed_on"] <= cutoff
                    else None,
                )
                for row in all_deposits
                if start <= row["paid_on"] <= period_end
            ]
            for row in all_deposits:
                if row["reversed_on"] and start <= row["reversed_on"] <= period_end:
                    deposit_rows.append(
                        {
                            "id": row["id"],
                            "kind": "reversal",
                            "action": "reverse",
                            "original_kind": row["kind"],
                            "amount_paise": row["amount_paise"]
                            * (-1 if row["kind"] in ("receipt", "opening") else 1),
                            "paid_on": row["reversed_on"],
                            "method": row["method"],
                            "notes": row["reverse_reason"],
                            "reason": row["reverse_reason"],
                            "reversed_on": row["reversed_on"],
                            "opening": False,
                        }
                    )
            deposit_rows.sort(key=lambda row: (row["paid_on"], row["id"], row["kind"]))
            adjustments = self._balance_deposit_adjustments(all_deposits, start, period_end)
            for rows, date_key, reason_key, kind, sign, source_key in (
                (
                    all_charges,
                    "voided_on",
                    "void_reason",
                    "charge_cancelled",
                    -1,
                    "charge_id",
                ),
                (
                    all_payments,
                    "reversed_on",
                    "reverse_reason",
                    "payment_reversed",
                    1,
                    "payment_id",
                ),
                (
                    all_refunds,
                    "reversed_on",
                    "reverse_reason",
                    "refund_reversed",
                    -1,
                    "refund_id",
                ),
            ):
                for row in rows:
                    if row[date_key] and start <= row[date_key] <= period_end:
                        adjustments.append(
                            {
                                "kind": kind,
                                "effective_on": row[date_key],
                                "date": row[date_key],
                                "amount_paise": sign * row["amount_paise"],
                                "reason": row[reason_key],
                                source_key: row["id"],
                            }
                        )
            removed_net = sum(row["amount_paise"] for row in charge_rows if row["id"] in deleted_ids)
            charge_rows = [row for row in charge_rows if row["id"] not in deleted_ids]
            visible_adjustments = []
            for adjustment in adjustments:
                if adjustment.get("charge_id") in deleted_ids:
                    removed_net += adjustment["amount_paise"]
                else:
                    visible_adjustments.append(adjustment)
            if removed_net:
                # Consolidate the selected-period financial effect, without
                # printing a removed bill's ID, description or deletion reason.
                visible_adjustments.append({
                    "kind": "account_adjustment", "effective_on": period_end,
                    "date": period_end, "amount_paise": removed_net, "reason": "",
                })
            adjustments = sorted(visible_adjustments, key=lambda row: (row["effective_on"], row["kind"]))
            totals = {
                "period_charges_paise": sum(row["amount_paise"] for row in charge_rows),
                "period_payments_paise": sum(
                    row["amount_paise"] for row in payment_rows
                ),
                "period_refunds_paise": sum(row["amount_paise"] for row in refund_rows),
                "adjustments_paise": sum(row["amount_paise"] for row in adjustments),
            }
            result = {
                "tenancy": tenancy,
                "currency_code": self._currency_code(conn),
                "start": start,
                "end": end,
                "generated_at": generated.isoformat(),
                "balance_as_of": cutoff,
                "balance_calculated_at": generated.isoformat() if as_of is None else cutoff,
                "period_balance_as_of": period_end,
                "opening_balance_paise": opening,
                "closing_balance_paise": balance["net_paise"],
                "period_closing_balance_paise": period_balance["net_paise"],
                "balance": balance,
                "charges": charge_rows,
                "outstanding_charges": outstanding,
                "billing_cycles": period_summaries,
                "billing_details": sorted(
                    {row["id"]: row for row in [*charge_rows, *(item for item in cycle_charges if item["id"] in cycle_ids)]}.values(),
                    key=lambda row: (row["period_start"], row["id"]),
                ),
                "payments": payment_rows,
                "refunds": refund_rows,
                "adjustments": adjustments,
                "deposits": deposit_rows,
                **totals,
            }
            result["allocations"] = [
                dict(row)
                for row in conn.execute(
                    select(m.allocations)
                    .join(m.payments)
                    .where(
                        m.payments.c.tenancy_id == tenancy_id,
                        m.allocations.c.effective_on <= cutoff,
                        _active(m.allocations.c.cancelled_on, cutoff),
                    )
                ).mappings()
                if row["charge_id"] not in deleted_ids
            ]
            reconciled = (
                opening
                + totals["period_charges_paise"]
                - totals["period_payments_paise"]
                + totals["period_refunds_paise"]
                + totals["adjustments_paise"]
            )
            if reconciled != period_balance["net_paise"]:
                raise RuntimeError(
                    "The statement does not reconcile. Do not issue it; restore or inspect the account history."
                )
            return result
