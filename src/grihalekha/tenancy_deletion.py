"""Explicit, settled-account erasure with complete export and revision checks.

CSV/PDF exports are portable records, not a substitute for the application's
restorable database backups. Existing backup bundles and unowned issued PDFs
are deliberately retained. Only explicitly owned payment proofs are removed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
import zipfile

from sqlalchemy import delete, or_, select, update

from . import models as m
from .backups import _safe_name
from .clock import today_ist
from .currencies import DEFAULT_CURRENCY, get_currency


_OWNED_TABLES = (
    m.tenancies, m.tenancy_occupancies, m.rent_rates, m.charges,
    m.cycle_payment_locks, m.payments, m.refunds, m.deposits,
)
_ENTITIES = {
    "tenancy": "tenancies", "rent_rate": "rent_rates", "charge": "charges",
    "payment": "payments", "refund": "refunds", "deposit": "deposits",
    "allocation": "allocations", "occupancy": "tenancy_occupancies",
}


def _revision(conn):
    return int(conn.execute(select(m.settings.c.value).where(
        m.settings.c.key == "revision")).scalar_one())


def _check_revision(conn, expected):
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError("A valid account revision is required before deletion.")
    if _revision(conn) != expected:
        raise ValueError("Account data changed. Review the tenant and create fresh backups before deleting.")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_bytes(table, rows):
    # Use the same spreadsheet protection as ordinary statements; integer
    # paise columns remain exact numbers rather than floating point rupees.
    from .reports import _safe_csv

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(table.c.keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _safe_csv(value) for key, value in row.items()})
    return stream.getvalue().encode("utf-8-sig")


def _explicit_owner(value):
    if isinstance(value, dict):
        if "tenancy_id" in value:
            return value["tenancy_id"]
        for key in ("rate", "previous_bill", "cancelled_occupancy", "cancelled_rate"):
            owner = _explicit_owner(value.get(key))
            if owner is not None:
                return owner
    return None


def _redact_counterpart(value, tenancy_id):
    """Keep another tenant's exchange dates while removing erased references."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"other_tenancy_id", "paired_tenancy_id"} and item == tenancy_id:
                result[key] = None
                result["counterpart_deleted"] = True
            else:
                result[key] = _redact_counterpart(item, tenancy_id)
        return result
    if isinstance(value, list):
        return [_redact_counterpart(item, tenancy_id) for item in value]
    return value


class TenancyDeletionMixin:
    def _deletable_tenancy(self, conn, tenancy_id):
        tenant = self._get(conn, m.tenancies, tenancy_id, "Tenant")
        today = today_ist().isoformat()
        if not tenant["archived_on"]:
            raise ValueError("Only an archived tenant account can be permanently deleted.")
        if not tenant.get("cancelled_on") and (not tenant["move_out"] or tenant["move_out"] >= today):
            raise ValueError("Delete an archived account only after the tenant's last occupied day.")
        if self._has_unreviewed_bills(conn, tenancy_id):
            raise ValueError("Review all bill drafts and deferred electricity before deleting the archived account.")
        for cutoff in (today, "9999-12-31"):
            balance = self._balance(conn, tenancy_id, cutoff)
            if any(balance[key] for key in ("outstanding_paise", "credit_paise", "deposit_paise")):
                raise ValueError("Settle all unpaid bills, advance credit and security deposits before deletion.")
        return tenant

    def _deletion_tables(self, conn, tenancy_id):
        result = {}
        for table in _OWNED_TABLES:
            owner = table.c.id if table is m.tenancies else table.c.tenancy_id
            result[table.name] = [dict(row) for row in conn.execute(
                select(table).where(owner == tenancy_id).order_by(*table.primary_key.columns)
            ).mappings()]
        payment_ids = select(m.payments.c.id).where(m.payments.c.tenancy_id == tenancy_id)
        refund_ids = select(m.refunds.c.id).where(m.refunds.c.tenancy_id == tenancy_id)
        deposit_ids = select(m.deposits.c.id).where(m.deposits.c.tenancy_id == tenancy_id)
        result["deposit_sources"] = [dict(row) for row in conn.execute(select(m.deposit_sources).where(
            m.deposit_sources.c.deposit_id.in_(deposit_ids)).order_by(m.deposit_sources.c.id)).mappings()]
        result["allocations"] = [dict(row) for row in conn.execute(select(m.allocations).where(
            m.allocations.c.payment_id.in_(payment_ids)).order_by(m.allocations.c.id)).mappings()]
        result["refund_sources"] = [dict(row) for row in conn.execute(select(m.refund_sources).where(
            m.refund_sources.c.refund_id.in_(refund_ids)).order_by(m.refund_sources.c.id)).mappings()]
        result["settings"] = [dict(row) for row in conn.execute(select(m.settings).where(
            m.settings.c.key == f"opening_initialized_{tenancy_id}")).mappings()]
        flats = {row["flat_id"] for row in result["tenancy_occupancies"]}
        flats.add(result["tenancies"][0]["flat_id"])
        result["flats"] = [dict(row) for row in conn.execute(select(m.flats).where(
            m.flats.c.id.in_(flats)).order_by(m.flats.c.id)).mappings()]
        buildings = {row["building_id"] for row in result["flats"]}
        result["buildings"] = [dict(row) for row in conn.execute(select(m.buildings).where(
            m.buildings.c.id.in_(buildings)).order_by(m.buildings.c.id)).mappings()]
        owned = {entity: {row["id"]: row for row in result[table]}
                 for entity, table in _ENTITIES.items()}
        audit, redact = [], []
        for raw in conn.execute(select(m.audit).order_by(m.audit.c.id)).mappings():
            row = dict(raw)
            details = json.loads(row["details"])
            owner = _explicit_owner(details)
            record = owned.get(row["entity"], {}).get(row["entity_id"])
            belongs = owner == tenancy_id if owner is not None else bool(
                record and row["created_at"] >= record.get("created_at", ""))
            if belongs:
                audit.append(row)
            else:
                clean = _redact_counterpart(details, tenancy_id)
                if clean != details:
                    redact.append((row["id"], clean))
        result["audit"] = audit
        return result, redact

    def _check_deletion_references(self, conn, tenancy_id):
        # SQLite foreign keys protect existence, but they do not ensure that
        # the two ends of every financial reference belong to one account.
        for left, right, link, left_id, right_id in (
            (m.payments, m.charges, m.allocations, m.allocations.c.payment_id, m.allocations.c.charge_id),
            (m.refunds, m.payments, m.refund_sources, m.refund_sources.c.refund_id, m.refund_sources.c.payment_id),
            (m.deposits, m.payments, m.deposit_sources, m.deposit_sources.c.deposit_id, m.deposit_sources.c.payment_id),
        ):
            query = select(link.c.id).select_from(link.join(left, left_id == left.c.id).join(right, right_id == right.c.id)).where(
                or_(left.c.tenancy_id == tenancy_id, right.c.tenancy_id == tenancy_id),
                left.c.tenancy_id != right.c.tenancy_id,
            )
            if conn.execute(query.limit(1)).first():
                raise ValueError("This account has a cross-account financial reference; repair it before deletion.")
        for left, right, column in (
            (m.deposits, m.payments, m.deposits.c.payment_id),
            (m.tenancy_occupancies, m.rent_rates, m.tenancy_occupancies.c.rent_rate_id),
            (m.charges, m.charges.alias("replaced_bill"), m.charges.c.replaces_charge_id),
            (m.charges, m.charges.alias("covering_bill"), m.charges.c.electricity_covered_by_charge_id),
        ):
            query = select(left.c.id).select_from(left.join(right, column == right.c.id)).where(
                or_(left.c.tenancy_id == tenancy_id, right.c.tenancy_id == tenancy_id),
                left.c.tenancy_id != right.c.tenancy_id,
            )
            if conn.execute(query.limit(1)).first():
                raise ValueError("This account has a cross-account record reference; repair it before deletion.")

    def _deletion_proof_path(self, relative):
        if (not isinstance(relative, str) or not relative.startswith("attachments/")
                or not _safe_name(relative)):
            raise ValueError("A payment proof has an unsafe file path. Repair the attachment before deletion.")
        path = self.data_dir / relative
        root = self.data_dir / "attachments"
        if not path.resolve().is_relative_to(root.resolve()) or root.resolve().parent != self.data_dir:
            raise ValueError("A payment proof points outside the account attachments folder.")
        for item in (path, *path.parents):
            if item == self.data_dir:
                break
            if item.is_symlink() or item.is_junction():
                raise ValueError("A payment proof uses a linked file or folder; repair it before deletion.")
        if path.exists() and not path.is_file():
            raise ValueError("A payment proof is not a regular file.")
        return path

    def _deletion_backup_target(self, path):
        target = Path(path).resolve()
        if (target in {self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")}
                or any(target.is_relative_to((self.data_dir / folder).resolve())
                       for folder in ("attachments", "documents"))):
            raise ValueError("Save the deletion backup outside the managed database, attachments and documents.")
        return target

    def prepare_tenancy_deletion(self, tenancy_id):
        """Return immutable-by-convention account data for review and export.

        The global revision deliberately invalidates this preview after any
        account write, including edits from another running Ledger instance.
        """
        with self._lock:
            with self._read() as conn:
                tenant = self._deletable_tenancy(conn, tenancy_id)
                self._check_deletion_references(conn, tenancy_id)
                tables, _ = self._deletion_tables(conn, tenancy_id)
                revision = _revision(conn)
            dates = [value for value in (tenant["move_in"], tenant["opening_date"],
                                         tenant["move_out"], tenant.get("cancelled_on")) if value]
            date_columns = {"period_start", "period_end", "charge_date", "paid_on", "effective_on",
                            "voided_on", "cancelled_on", "reversed_on", "deleted_on", "starts_on", "ends_on"}
            for rows in tables.values():
                dates.extend(value for row in rows for key, value in row.items()
                             if key in date_columns and value)
            statement = self.statement(tenancy_id, min(dates), max(dates))
            with self._read() as conn:
                _check_revision(conn, revision)
            return {
                "tenancy_id": tenancy_id, "revision": revision,
                "generated_at": statement["generated_at"], "tenancy": statement["tenancy"],
                "statement": statement, "tables": tables,
                "counts": {name: len(rows) for name, rows in tables.items()},
            }

    def tenancy_deletion_backup_file(self, preview, path, *, kind="pdf"):
        """Register a completed export; failures never change account records."""
        if kind not in {"csv", "pdf"}:
            raise ValueError("Choose CSV or PDF for the account backup.")
        target = self._deletion_backup_target(path)
        with self._read() as conn:
            _check_revision(conn, preview["revision"])
            self._deletable_tenancy(conn, preview["tenancy_id"])
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError("The account backup was not completed; no tenant data was deleted.")
        if kind == "pdf":
            with target.open("rb") as stream:
                header = stream.read(5)
                stream.seek(max(0, target.stat().st_size - 1024))
                ending = stream.read()
            if header != b"%PDF-" or b"%%EOF" not in ending:
                raise ValueError("The PDF backup is incomplete; no tenant data was deleted.")
        else:
            try:
                with zipfile.ZipFile(target) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    required = {"manifest.json", *(f"history/{name}.csv" for name in preview["tables"])}
                    required.update(row["proof_path"] for row in preview["tables"]["payments"] if row["proof_path"])
                    names = archive.namelist()
                    if (not isinstance(manifest, dict)
                            or manifest.get("format") != "grihalekha-tenant-csv"
                            or manifest.get("format_version") != 1
                            or manifest.get("schema_version") != m.SCHEMA_VERSION
                            or manifest.get("tenancy_id") != preview["tenancy_id"]
                            or manifest.get("revision") != preview["revision"]
                            or manifest.get("counts") != preview["counts"]
                            or not required.issubset(names)
                            or len(names) != len(set(names)) or archive.testzip()):
                        raise ValueError("The CSV backup does not match the reviewed account.")
            except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("The CSV backup is incomplete or unreadable; no tenant data was deleted.") from exc
        return {"kind": kind, "path": str(target), "sha256": _sha256(target),
                "size": target.stat().st_size, "tenancy_id": preview["tenancy_id"],
                "revision": preview["revision"]}

    def export_tenancy_deletion_csv(self, preview, destination):
        """Atomically publish a ZIP of complete, spreadsheet-safe CSV tables.

        The normal readable statement CSVs accompany exact raw history,
        including removed bills, reversals, audit records and payment proofs.
        """
        from .reports import export_csv

        target = self._deletion_backup_target(destination)
        if target.suffix.lower() != ".zip":
            raise ValueError("Save the CSV account backup as a .zip file.")
        with self._read() as conn:
            _check_revision(conn, preview["revision"])
            self._deletable_tenancy(conn, preview["tenancy_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + f".{uuid4().hex}.partial")
        manifest = {key: preview[key] for key in ("tenancy_id", "revision", "generated_at", "counts")}
        currency_code = get_currency(preview["statement"].get("currency_code", DEFAULT_CURRENCY)).code
        manifest.update(format="grihalekha-tenant-csv", format_version=1, schema_version=m.SCHEMA_VERSION,
                        currency_code=currency_code,
                        money_unit=f"integer paise (hundredths) in history CSVs; {currency_code} in statement CSVs",
                        notice="CSV and PDF records cannot be restored as an application database backup.")
        try:
            with tempfile.TemporaryDirectory(prefix=".tenant-export-", dir=target.parent) as folder:
                readable = export_csv(preview["statement"], Path(folder))
                with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for name, rows in preview["tables"].items():
                        archive.writestr(f"history/{name}.csv", _csv_bytes(m.metadata.tables[name], rows))
                    for path in readable:
                        archive.write(path, "statement/" + path.name)
                    for relative in sorted({row["proof_path"] for row in preview["tables"]["payments"] if row["proof_path"]}):
                        path = self._deletion_proof_path(relative)
                        if not path.is_file():
                            raise ValueError("A payment proof is missing. Restore it before creating the account backup.")
                        archive.write(path, relative)
            with partial.open("r+b") as stream:
                os.fsync(stream.fileno())
            # Check the finished bundle and latest revision before publication;
            # a failed export never replaces an existing good backup.
            artifact = self.tenancy_deletion_backup_file(preview, partial, kind="csv")
            os.replace(partial, target)
            artifact["path"] = str(target)
            return artifact
        finally:
            partial.unlink(missing_ok=True)

    def delete_archived_tenancy(self, tenancy_id, *, expected_revision, backups=()):
        """Erase one settled archived account in a single SQL transaction.

        A nonempty backup list must contain both completed CSV and PDF exports.
        Passing no exports is reserved for an explicit no-backup UI choice.
        Attachment cleanup follows commit so failed SQL can never lose files.
        """
        warnings = []
        with self._lock:
            with self._write() as conn:
                _check_revision(conn, expected_revision)
                self._deletable_tenancy(conn, tenancy_id)
                self._check_deletion_references(conn, tenancy_id)
                tables, redact = self._deletion_tables(conn, tenancy_id)
                if backups:
                    if len(backups) != 2 or {item.get("kind") for item in backups} != {"csv", "pdf"}:
                        raise ValueError("Complete both the CSV and PDF backups before deleting the tenant.")
                    for item in backups:
                        path = self._deletion_backup_target(item["path"])
                        if (item.get("tenancy_id") != tenancy_id or item.get("revision") != expected_revision
                                or not path.is_file() or path.stat().st_size != item.get("size")
                                or _sha256(path) != item.get("sha256")):
                            raise ValueError("An account backup is missing or changed. Export both files again before deletion.")
                shared = {Path(self.data_dir / value).resolve() for value in conn.execute(
                    select(m.payments.c.proof_path).where(m.payments.c.tenancy_id != tenancy_id,
                                                       m.payments.c.proof_path.is_not(None))).scalars() if value}
                proofs = {self._deletion_proof_path(row["proof_path"]) for row in tables["payments"] if row["proof_path"]}
                proofs = {path for path in proofs if path.resolve() not in shared}
                conn.execute(update(m.tenancy_occupancies).where(
                    m.tenancy_occupancies.c.paired_tenancy_id == tenancy_id).values(paired_tenancy_id=None))
                for row_id, details in redact:
                    conn.execute(update(m.audit).where(m.audit.c.id == row_id).values(
                        details=json.dumps(details, ensure_ascii=False)))
                conn.execute(delete(m.audit).where(m.audit.c.id.in_([row["id"] for row in tables["audit"]])))
                conn.execute(update(m.charges).where(m.charges.c.tenancy_id == tenancy_id).values(
                    replaces_charge_id=None, electricity_covered_by_charge_id=None))
                for name in ("deposit_sources", "refund_sources", "allocations", "deposits", "charges", "payments", "refunds",
                             "cycle_payment_locks", "tenancy_occupancies", "rent_rates", "tenancies"):
                    table = m.metadata.tables[name]
                    if name in {"allocations", "refund_sources", "deposit_sources"}:
                        condition = table.c.id.in_([row["id"] for row in tables[name]])
                    else:
                        owner = table.c.id if table is m.tenancies else table.c.tenancy_id
                        condition = owner == tenancy_id
                    conn.execute(delete(table).where(condition))
                conn.execute(delete(m.settings).where(m.settings.c.key == f"opening_initialized_{tenancy_id}"))
                # Do not retain the erased name, account ID, notes or file paths.
                self._audit(conn, "delete_archived_tenant", "account_deletion", None,
                            backup_formats=sorted(item["kind"] for item in backups),
                            records_removed=sum(len(tables[name]) for name in tables if name not in {"flats", "buildings"}))
            for path in proofs:
                try:
                    # Recheck links immediately before unlinking after commit.
                    self._deletion_proof_path(path.relative_to(self.data_dir).as_posix()).unlink(missing_ok=True)
                except (OSError, ValueError) as exc:
                    warnings.append(f"Could not remove payment proof {path.name}: {exc}")
        return {"deleted_tenancy_id": tenancy_id, "cleanup_warnings": warnings}


def tenancy_deletion_html(preview, language="en", document_id=None):
    """Lifetime statement with all bill history and dated flat history appended."""
    from .reports import _date, _electricity_details, _label, _money, _table, _text, statement_html

    html = statement_html(preview["statement"], language, document_id)
    currency_code = preview["statement"].get("currency_code", DEFAULT_CURRENCY)
    def label(english, hindi):
        return _text(hindi if language == "hi" else english if language == "en" else f"{english} / {hindi}")

    body = [f'<h2>{label("Archived account history", "संग्रहीत खाते का इतिहास")}</h2>',
            f'<p>{label("Includes cancelled and deleted bills for reference; these are not additional amounts due.", "संदर्भ के लिए रद्द और हटाए गए बिल भी शामिल हैं; ये अतिरिक्त देय राशि नहीं हैं।")}</p>']
    rows = []
    covered = {}
    for source in preview["tables"]["charges"]:
        if source.get("electricity_deferred"):
            covered.setdefault(source.get("electricity_covered_by_charge_id"), []).append(source)
    for original in preview["tables"]["charges"]:
        bill = dict(original)
        sources = covered.get(bill["id"], [])
        bill["coverage_period_start"] = min([bill["period_start"], *(source["period_start"] for source in sources)])
        bill["coverage_period_end"] = bill["period_end"]
        state = (label("Deleted", "हटाया गया") if bill["deleted_on"] else
                 _label("cancelled" if bill["status"] == "void" else "deferred" if bill.get("electricity_deferred")
                        else "review_pending" if bill["status"] == "draft" else "posted", language))
        amount = _money(bill["amount_paise"], currency_code) if bill["amount_paise"] is not None else "—"
        rows.append(f'<tr><td>#{bill["id"]}<br>{_label(bill["kind"], language)}</td>'
                    f'<td>{_date(bill["period_start"], language)}<br>{_date(bill["period_end"], language)}</td>'
                    f'<td>{_text(bill["description"])}{_electricity_details(bill, language)}</td><td class="num">{amount}</td>'
                    f'<td>{state}<br>{_date(bill["deleted_on"] or bill["voided_on"], language)}</td></tr>')
    body.append(_table([(_label("bill", language), ""), (_label("period", language), ""),
                        (_label("description", language), ""), (_label("amount", language), "num"),
                        (_label("status", language), "")], rows))
    body.append(f'<h2>{label("Flat history", "फ्लैट का इतिहास")}</h2>')
    locations = preview["tenancy"]["occupancy_history"]
    body.append(_table([(_label("property", language), ""), (_label("flat", language), ""),
                        (_label("period", language), "")], [
        f'<tr><td>{_text(row["building_name"])}</td><td>{_text(row["number"])}</td>'
        f'<td>{_date(row["starts_on"], language)} — {_date(row["ends_on"], language)}</td></tr>'
        for row in locations]))
    # Legacy accounts can contain future-dated entries which offset each
    # other. Keep the normal statement's current balance, but include those
    # records in the archival PDF as well as the complete CSV history.
    future = []
    cutoff = preview["statement"]["balance_as_of"]
    for name, title in (("payments", "payment"), ("refunds", "refund"), ("deposits", "deposit")):
        for row in preview["tables"][name]:
            if row["paid_on"] > cutoff or (row["reversed_on"] and row["reversed_on"] > cutoff):
                future.append((title, row))
    if future:
        body.append(f'<h2>{label("Future-dated transaction history", "भविष्य की तारीख वाले लेनदेन का इतिहास")}</h2>')
        body.append(f'<p>{label("These records are excluded from today’s statement balance until their dates.", "इन प्रविष्टियों को उनकी तारीख आने तक आज के विवरण के शेष में शामिल नहीं किया गया है।")}</p>')
        body.append(_table([(_label("date", language), ""), (_label("description", language), ""),
                            (_label("amount", language), "num"), (_label("status", language), "")], [
            f'<tr><td>{_date(row["paid_on"], language)}</td>'
            f'<td>{_text(title)} #{row["id"]}<br>{_text(row.get("reference", ""))}<br>{_text(row["notes"])}</td>'
            f'<td class="num">{_money(row["amount_paise"], currency_code)}</td><td>'
            + (f'{_label("reversed", language)}<br>{_date(row["reversed_on"], language)}' if row["reversed_on"] else "—")
            + '</td></tr>' for title, row in sorted(future, key=lambda item: (item[1]["paid_on"], item[0], item[1]["id"]))]))
    return html.replace('<div class="footer">', "".join(body) + '<div class="footer">', 1)
