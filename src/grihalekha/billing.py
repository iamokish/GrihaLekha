"""Monthly electricity and bill-level payment operations for the ledger."""

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
import json

from sqlalchemy import func, insert, or_, select, update

from . import models as m
from .clock import today_ist
from .dates import cycle_bounds, iso_date, iter_account_periods
from .money import validate_paise


class BillingMixin:
    def _ensure_account_open(self, conn, tenancy_id):
        tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
        if tenancy.get("archived_on"):
            raise ValueError("Reopen this archived account before changing its records.")
        return tenancy

    @staticmethod
    def _bill_cycle(charge, billing_day):
        return (charge["cycle_start"] or charge["electricity_cycle_start"] or
                cycle_bounds(charge["period_start"], billing_day)[0].isoformat())

    def _ensure_cycle_unlocked(self, conn, charge):
        if charge.get("deleted_on"):
            raise ValueError("This bill has been deleted. Its saved history cannot be changed.")
        tenancy = self._get(conn, m.tenancies, charge["tenancy_id"], "Tenancy")
        cycle = self._bill_cycle(charge, tenancy["billing_day"])
        cycles = {cycle}
        # A combined bill changes the payment status of the electricity carried
        # forward from its earlier cycles as well as its own receiving cycle.
        if charge.get("id") and charge.get("kind") == "electricity":
            sources = conn.execute(select(m.charges).where(
                m.charges.c.electricity_covered_by_charge_id == charge["id"],
                m.charges.c.status != "void",
            )).mappings()
            cycles.update(self._bill_cycle(row, tenancy["billing_day"]) for row in sources)
        locked = conn.execute(select(m.cycle_payment_locks.c.locked).where(
            m.cycle_payment_locks.c.tenancy_id == charge["tenancy_id"],
            m.cycle_payment_locks.c.cycle_start.in_(cycles),
            m.cycle_payment_locks.c.locked.is_(True),
        ).limit(1)).scalar_one_or_none()
        if locked:
            raise ValueError("Payments for this billing cycle are locked. Unlock the cycle first.")

    @staticmethod
    def _electricity_target_active(target, cutoff=None):
        if not target or target["posted_at"] is None:
            return False
        if cutoff is None:
            return target["status"] == "posted"
        return (target["charge_date"] <= cutoff
                and (target["voided_on"] is None or target["voided_on"] > cutoff))

    def _electricity_rows(self, conn, tenancy_id):
        return [dict(row) for row in conn.execute(select(m.charges).where(
            m.charges.c.tenancy_id == tenancy_id, m.charges.c.kind == "electricity",
            m.charges.c.opening.is_(False),
        ).order_by(m.charges.c.period_start, m.charges.c.id)).mappings()]

    def _electricity_pending_map(self, tenancy, rows):
        """The consecutive deferred cycles immediately before a monthly draft.

        An unreviewed or separately billed cycle is a boundary: it must never
        silently become part of another bill's service period.
        """
        if not rows:
            return {}
        by_id = {row["id"]: row for row in rows}
        by_cycle = {}
        for row in rows:
            if row["status"] != "void":
                by_cycle.setdefault(self._bill_cycle(row, tenancy["billing_day"]), []).append(row)
        intervals = []
        for start, end in sorted((row["period_start"], row["period_end"])
                                 for row in rows if row["status"] != "void"
                                 and not row["electricity_cycle_start"]):
            if intervals and start <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
            else:
                intervals.append((start, end))
        starts = [start for start, _ in intervals]
        result, sources = {}, []
        for period in iter_account_periods(tenancy, max(row["period_start"] for row in rows)):
            candidates = by_cycle.get(period["cycle_start"], [])
            if len(candidates) != 1:
                sources = []
                continue
            source = candidates[0]
            position = bisect_right(starts, period["period_end"]) - 1
            overlap = position >= 0 and intervals[position][1] >= period["period_start"]
            if (source["status"] == "draft" and not source["electricity_deferred"]
                    and source["electricity_cycle_start"] and not overlap):
                result[source["id"]] = sources
                sources = []
                continue
            if (source["status"] != "draft" or not source["electricity_deferred"]
                    or source["period_start"] != period["period_start"]
                    or source["period_end"] != period["period_end"]
                    or overlap
                    or self._electricity_target_active(by_id.get(source["electricity_covered_by_charge_id"]))):
                sources = []
                continue
            sources.append(source)
        return result

    def _pending_electricity_sources(self, tenancy, charge, rows):
        if (charge["kind"] != "electricity" or charge["opening"]
                or not charge["electricity_cycle_start"] or charge["status"] != "draft"
                or charge["electricity_deferred"]):
            return []
        return self._electricity_pending_map(tenancy, rows).get(charge["id"], [])

    def _electricity_coverage_map(self, conn, rows):
        """Read saved service links, including earlier corrected bill versions.

        The foreign key identifies the current owner; dated audit associations
        retain the original coverage when a replacement takes that ownership.
        """
        if not rows:
            return {}
        by_id = {row["id"]: row for row in rows}
        covered = {}
        for source in rows:
            target_id = source["electricity_covered_by_charge_id"]
            if source["electricity_deferred"] and target_id is not None:
                covered.setdefault(target_id, []).append(source)
        ids = select(m.charges.c.id).where(m.charges.c.tenancy_id == rows[0]["tenancy_id"])
        history = conn.execute(select(m.audit.c.entity_id, m.audit.c.details).where(
            m.audit.c.entity == "charge", m.audit.c.action == "cover_deferred_electricity",
            m.audit.c.entity_id.in_(ids),
        ).order_by(m.audit.c.id)).mappings()
        for entry in history:
            details = json.loads(entry["details"])
            covered[entry["entity_id"]] = [by_id[source_id]
                for source_id in details.get("source_charge_ids", []) if source_id in by_id]
        return covered

    def _annotate_electricity_coverage(self, conn, tenancy, charges, *, cutoff=None):
        """Annotate service coverage in batches using account rows and audit links."""
        if not any(row["kind"] == "electricity" for row in charges):
            return
        rows = self._electricity_rows(conn, tenancy["id"])
        by_id = {row["id"]: row for row in rows}
        pending = self._electricity_pending_map(tenancy, rows)
        by_target = self._electricity_coverage_map(conn, rows)
        locked_cycles = set(conn.execute(select(m.cycle_payment_locks.c.cycle_start).where(
            m.cycle_payment_locks.c.tenancy_id == tenancy["id"],
            m.cycle_payment_locks.c.locked.is_(True),
        )).scalars())
        active_targets = {}
        for target_id, sources in by_target.items():
            target = by_id.get(target_id)
            if self._electricity_target_active(target, cutoff):
                for source in sources:
                    previous = active_targets.get(source["id"])
                    if previous is None or (target["charge_date"], target["id"]) > (previous["charge_date"], previous["id"]):
                        active_targets[source["id"]] = target
        for charge in charges:
            if charge["kind"] != "electricity":
                continue
            raw = by_id.get(charge["id"], charge)
            sources = by_target.get(charge["id"], [])
            if raw["status"] == "draft" and not raw["electricity_deferred"]:
                sources = pending.get(charge["id"], [])
            charge["coverage_period_start"] = min(
                [charge["period_start"], *(source["period_start"] for source in sources)])
            charge["coverage_period_end"] = charge["period_end"]
            charge["electricity_deferred_charge_ids"] = [source["id"] for source in sources]
            charge["electricity_deferred_cycle_starts"] = [
                self._bill_cycle(source, tenancy["billing_day"]) for source in sources]
            affected_cycles = {*charge["electricity_deferred_cycle_starts"],
                               self._bill_cycle(raw, tenancy["billing_day"])}
            charge["affected_locked_cycle_starts"] = sorted(affected_cycles & locked_cycles)
            charge["payment_locked"] = bool(charge["affected_locked_cycle_starts"])
            target = active_targets.get(charge["id"])
            charge["electricity_covered_by_charge_id"] = target["id"] if target else None
            charge["electricity_covered_by_cycle_start"] = (
                self._bill_cycle(target, tenancy["billing_day"]) if target else None)
            recorded_target = by_id.get(raw.get("electricity_covered_by_charge_id"))
            charge["electricity_scheduled_covering_charge_id"] = (
                recorded_target["id"] if not target and cutoff is not None
                and self._electricity_target_active(recorded_target)
                and recorded_target["charge_date"] > cutoff else None)
            if target and not self._electricity_target_active(raw, cutoff):
                # A source may have been reopened and separately billed after
                # its old covering bill was cancelled. At an earlier cutoff it
                # still had no amount or reading of its own.
                charge.update(electricity_deferred=True, status="draft",
                              amount_paise=None, meter_reading_hundredths=None,
                              needs_amount=False)

    def set_electricity_deferred(self, charge_id, deferred):
        """Skip a monthly electricity draft until a later combined bill.

        Deferral has neither an amount nor a meter reading. An already covered
        cycle cannot be made billable again and charged twice.
        """
        if not isinstance(deferred, bool):
            raise ValueError("Choose whether to defer this electricity cycle.")
        with self._write() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            tenancy = self._ensure_account_open(conn, charge["tenancy_id"])
            self._ensure_tenancy_not_cancelled(tenancy)
            self._ensure_cycle_unlocked(conn, charge)
            if (charge["kind"] != "electricity" or charge["opening"]
                    or not charge["electricity_cycle_start"] or charge["status"] != "draft"
                    or charge["posted_at"] is not None):
                raise ValueError("Only an unposted monthly electricity draft can be deferred.")
            if charge["electricity_deferred"] == deferred:
                return
            rows = self._electricity_rows(conn, tenancy["id"])
            by_id = {row["id"]: row for row in rows}
            target = by_id.get(charge["electricity_covered_by_charge_id"])
            if self._electricity_target_active(target):
                raise ValueError("This deferred cycle is already included in a later bill and cannot be billed again.")
            if deferred and any(row["id"] != charge_id and row["status"] != "void"
                                and row["period_start"] <= charge["period_end"]
                                and row["period_end"] >= charge["period_start"] for row in rows):
                raise ValueError("Another electricity bill already covers these service dates.")
            values = {"electricity_deferred": deferred}
            if deferred:
                values.update(amount_paise=None, meter_reading_hundredths=None,
                              electricity_covered_by_charge_id=None)
            conn.execute(update(m.charges).where(m.charges.c.id == charge_id).values(**values))
            self._audit(conn, "defer_electricity" if deferred else "undo_electricity_deferral",
                        "charge", charge_id, original=charge)

    def _cover_deferred_electricity(self, conn, tenancy, charge):
        rows = self._electricity_rows(conn, tenancy["id"])
        sources = self._pending_electricity_sources(tenancy, charge, rows)
        by_id = {row["id"]: row for row in rows}
        earliest = charge["charge_date"]
        affected_cycles = {self._bill_cycle(row, tenancy["billing_day"]) for row in [charge, *sources]}
        # Toggles and regenerated draft IDs must not erase a previous combined
        # bill's cancellation boundary. Match its saved service cycles as well
        # as the current foreign keys before any new amount takes effect.
        for target_id, historical_sources in self._electricity_coverage_map(conn, rows).items():
            target = by_id.get(target_id)
            if target and target["voided_on"] and any(
                self._bill_cycle(source, tenancy["billing_day"]) in affected_cycles
                for source in historical_sources
            ):
                earliest = max(earliest, target["voided_on"])
        # An undone deferral may itself have belonged to a cancelled combined
        # bill. Preserve that cancellation boundary when posting it separately.
        previous = by_id.get(charge["electricity_covered_by_charge_id"])
        if previous and previous["voided_on"]:
            earliest = max(earliest, previous["voided_on"])
        for source in sources:
            self._ensure_cycle_unlocked(conn, source)
            previous = by_id.get(source["electricity_covered_by_charge_id"])
            if previous and previous["voided_on"]:
                earliest = max(earliest, previous["voided_on"])
        if earliest != charge["charge_date"]:
            conn.execute(update(m.charges).where(m.charges.c.id == charge["id"]).values(charge_date=earliest))
            charge["charge_date"] = earliest
        if sources:
            conn.execute(update(m.charges).where(m.charges.c.id.in_([row["id"] for row in sources])).values(
                electricity_covered_by_charge_id=charge["id"]))
            self._audit(conn, "cover_deferred_electricity", "charge", charge["id"],
                        source_charge_ids=[row["id"] for row in sources],
                        coverage_period_start=sources[0]["period_start"],
                        coverage_period_end=charge["period_end"], charge_date=earliest)

    def _carry_electricity_metadata(self, conn, original, replacement_id):
        if original["kind"] != "electricity":
            return
        rows = self._electricity_rows(conn, original["tenancy_id"])
        by_id = {row["id"]: row for row in rows}
        sources = self._electricity_coverage_map(conn, rows).get(original["id"], [])
        for source in sources:
            target = by_id.get(source["electricity_covered_by_charge_id"])
            if (source["status"] != "draft" or not source["electricity_deferred"]
                    or (target and target["id"] != original["id"]
                        and self._electricity_target_active(target))):
                raise ValueError("Earlier electricity service dates have changed or are billed elsewhere. Review the current monthly drafts instead.")
            self._ensure_cycle_unlocked(conn, source)
        conn.execute(update(m.charges).where(m.charges.c.id == replacement_id).values(
            meter_reading_hundredths=original["meter_reading_hundredths"],
            electricity_deferred=original["electricity_deferred"],
            electricity_covered_by_charge_id=original["electricity_covered_by_charge_id"]))
        if sources:
            source_ids = [source["id"] for source in sources]
            conn.execute(update(m.charges).where(m.charges.c.id.in_(source_ids)).values(
                electricity_covered_by_charge_id=replacement_id))
            self._audit(conn, "cover_deferred_electricity", "charge", replacement_id,
                        source_charge_ids=source_ids, replaces_charge_id=original["id"],
                        coverage_period_start=min(source["period_start"] for source in sources),
                        coverage_period_end=original["period_end"])

    def _has_unreviewed_bills(self, conn, tenancy_id):
        """Covered deferred rows are records, not outstanding draft reviews."""
        target = m.charges.alias("electricity_covering_bill")
        covered = select(target.c.id).where(
            target.c.id == m.charges.c.electricity_covered_by_charge_id,
            target.c.tenancy_id == tenancy_id, target.c.status == "posted",
            target.c.kind == "electricity", target.c.posted_at.is_not(None),
        ).exists()
        return conn.execute(select(m.charges.c.id).where(
            m.charges.c.tenancy_id == tenancy_id, m.charges.c.status == "draft",
            or_(m.charges.c.electricity_deferred.is_(False), ~covered),
        ).limit(1)).first() is not None

    def set_cycle_payment_lock(self, tenancy_id, cycle_start, locked):
        cycle = iso_date(cycle_start)
        if not isinstance(locked, bool):
            raise ValueError("Choose whether to lock or unlock this billing cycle.")
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            if cycle_bounds(cycle, tenancy["billing_day"])[0].isoformat() != cycle:
                raise ValueError("Choose a billing-cycle start date.")
            rows = conn.execute(select(m.charges).where(
                m.charges.c.tenancy_id == tenancy_id,
            )).mappings()
            if not any(self._bill_cycle(row, tenancy["billing_day"]) == cycle for row in rows):
                raise ValueError("This account has no bills for that cycle.")
            where = (m.cycle_payment_locks.c.tenancy_id == tenancy_id,
                     m.cycle_payment_locks.c.cycle_start == cycle)
            existing = conn.execute(select(m.cycle_payment_locks.c.locked).where(*where)).scalar_one_or_none()
            if existing is not None and existing == locked:
                return
            values = {"locked": locked, "updated_at": datetime.now(UTC).isoformat(timespec="microseconds")}
            if existing is None:
                conn.execute(insert(m.cycle_payment_locks).values(
                    tenancy_id=tenancy_id, cycle_start=cycle, **values))
            else:
                conn.execute(update(m.cycle_payment_locks).where(*where).values(**values))
            self._audit(conn, "lock_cycle" if locked else "unlock_cycle", "tenancy", tenancy_id,
                        cycle_start=cycle)

    def list_bill_cycles(self, tenancy_id, as_of=None, *, include_deleted=False, include_stopped=False):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            locks = dict(conn.execute(select(
                m.cycle_payment_locks.c.cycle_start, m.cycle_payment_locks.c.locked,
            ).where(m.cycle_payment_locks.c.tenancy_id == tenancy_id)).all())
            paid = dict(conn.execute(select(
                m.allocations.c.charge_id, func.sum(m.allocations.c.amount_paise),
            ).join(m.charges).where(
                m.charges.c.tenancy_id == tenancy_id,
                m.allocations.c.effective_on <= cutoff,
                or_(m.allocations.c.cancelled_on.is_(None), m.allocations.c.cancelled_on > cutoff),
            ).group_by(m.allocations.c.charge_id)).all())
            query = select(m.charges).where(m.charges.c.tenancy_id == tenancy_id).order_by(
                m.charges.c.period_start, m.charges.c.id)
            if not include_deleted:
                query = query.where(m.charges.c.deleted_on.is_(None))
            if as_of is not None:
                query = query.where(m.charges.c.charge_date <= cutoff)
            cycles = {}
            all_bills = []
            final_cycle = (cycle_bounds(tenancy["move_out"], tenancy["billing_day"])[0].isoformat()
                           if tenancy["move_out"] else None)
            for row in conn.execute(query).mappings():
                if (not include_stopped and tenancy.get("cancelled_on") and not row["opening"]
                        and row["kind"] in {"rent", "electricity"}):
                    continue
                cycle = self._bill_cycle(row, tenancy["billing_day"])
                bill = self._charge_dict(conn, row, cutoff, allocated=paid.get(row["id"], 0))
                if (not include_stopped and tenancy["move_out"] and not row["opening"]
                        and row["period_start"] > tenancy["move_out"] and bill["status"] == "void"):
                    continue
                _, end = cycle_bounds(cycle, tenancy["billing_day"])
                group = cycles.setdefault(cycle, {
                    "cycle_start": cycle, "cycle_end": (end - timedelta(days=1)).isoformat(),
                    "period_start": row["period_start"], "period_end": row["period_end"],
                    "locked": locks.get(cycle, False), "charges": [],
                    "total_paise": 0, "paid_paise": 0, "pending_paise": 0, "review_pending": 0,
                    "moved_out_cycle": cycle == final_cycle,
                    "last_occupied_day": tenancy["move_out"] if cycle == final_cycle else None,
                })
                bill.update(billing_cycle_start=cycle, payment_locked=group["locked"])
                group["charges"].append(bill)
                all_bills.append(bill)
                group["period_start"] = min(group["period_start"], row["period_start"])
                group["period_end"] = max(group["period_end"], row["period_end"])
                if bill["status"] == "draft" and not bill["electricity_deferred"]:
                    group["review_pending"] += 1
                elif bill["status"] == "posted" and bill["charge_date"] <= cutoff:
                    group["total_paise"] += bill["amount_paise"] or 0
                    group["paid_paise"] += bill["paid_paise"]
                    group["pending_paise"] += bill["outstanding_paise"]
            self._annotate_electricity_coverage(conn, tenancy, all_bills, cutoff=cutoff)
            for group in cycles.values():
                group["electricity_deferred"] = any(
                    bill["status"] == "draft" and bill["electricity_deferred"] for bill in group["charges"])
            return [cycles[key] for key in sorted(cycles)]

    def _prepare_electricity_drafts(self, conn, tenancy, through):
        if tenancy.get("archived_on") or tenancy.get("cancelled_on"):
            return 0
        existing = conn.execute(select(m.charges).where(
            m.charges.c.tenancy_id == tenancy["id"],
            m.charges.c.kind == "electricity",
            m.charges.c.opening.is_(False),
        )).mappings().all()
        periods = list(iter_account_periods(tenancy, through))
        by_cycle = {period["cycle_start"]: period for period in periods}
        automatic = {row["electricity_cycle_start"]: dict(row) for row in existing
                     if row["electricity_cycle_start"]}
        cancelled = {}
        for row in existing:
            if row["status"] == "void":
                cycle = self._bill_cycle(row, tenancy["billing_day"])
                if cycle not in cancelled or (row["voided_on"], row["id"]) > (cancelled[cycle]["voided_on"], cancelled[cycle]["id"]):
                    cancelled[cycle] = row
        # A legacy manual bill can span several cycles or have a later dated
        # cancellation than the automatic anchor. Replacements must wait for
        # every overlapping cancellation, including outside their own cycle.
        # Sweep the intervals once instead of scanning all bills per month.
        cancelled_intervals = sorted(
            (row for row in existing if row["status"] == "void"),
            key=lambda row: (row["period_start"], row["id"]),
        )
        pending, position = [], 0
        for period in periods:
            while (position < len(cancelled_intervals)
                   and cancelled_intervals[position]["period_start"] <= period["period_end"]):
                row = cancelled_intervals[position]
                heappush(pending, (-int(row["voided_on"].replace("-", "")), -row["id"], row["period_end"], row))
                position += 1
            while pending and pending[0][2] < period["period_start"]:
                heappop(pending)
            if pending:
                latest = pending[0][3]
                previous = cancelled.get(period["cycle_start"])
                if previous is None or (latest["voided_on"], latest["id"]) > (previous["voided_on"], previous["id"]):
                    cancelled[period["cycle_start"]] = latest
        count = 0
        for cycle, row in automatic.items():
            if row["posted_at"] is not None:
                continue
            period = by_cycle.get(cycle)
            if tenancy["move_out"] and row["period_start"] > tenancy["move_out"]:
                if row["status"] == "draft":
                    conn.execute(update(m.charges).where(m.charges.c.id == row["id"]).values(
                        status="void", voided_on=tenancy["move_out"],
                        void_reason="Outside recorded occupancy",
                    ))
                    row.update(status="void", voided_on=tenancy["move_out"], void_reason="Outside recorded occupancy")
                    count += 1
            elif period and (row["status"] == "draft" or
                             row["void_reason"] == "Outside recorded occupancy"):
                if row["status"] == "void" or any(row[key] != period[key]
                    for key in ("period_start", "period_end", "partial")):
                    conn.execute(update(m.charges).where(m.charges.c.id == row["id"]).values(
                        period_start=period["period_start"], period_end=period["period_end"],
                        due_date=period["period_end"],
                        charge_date=max(period["period_start"], row["charge_date"])
                        if row["replaces_charge_id"] else period["period_start"],
                        partial=period["partial"], amount_paise=None, status="draft",
                        voided_on=None, void_reason=None,
                    ))
                    row.update(status="draft", voided_on=None, void_reason=None)
                    count += 1
        # Merge legacy service intervals once, so draft preparation remains
        # linearithmic even for long accounts with many manually entered bills.
        intervals = []
        for start, end in sorted((row["period_start"], row["period_end"])
                                 for row in existing if row["status"] != "void"
                                 and not row["electricity_cycle_start"]):
            if intervals and start <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
            else:
                intervals.append((start, end))
        starts = [start for start, _ in intervals]
        manual_cycles = {
            cycle_bounds(row["period_start"], tenancy["billing_day"])[0].isoformat()
            for row in existing if not row["electricity_cycle_start"] and row["status"] != "void"
        }
        for period in periods:
            cycle = period["cycle_start"]
            anchor = automatic.get(cycle)
            if (anchor and anchor["status"] != "void") or cycle in manual_cycles:
                continue
            # Existing manual electricity bills already cover these service
            # dates. Do not manufacture duplicate bills during an upgrade.
            position = bisect_right(starts, period["period_start"]) - 1
            if position >= 0 and intervals[position][1] >= period["period_end"]:
                continue
            original = cancelled.get(cycle) or anchor
            if original:
                replacement = self._insert_charge(
                    conn, tenancy, "electricity", None,
                    period["period_start"], period["period_end"], period["period_end"],
                    "Monthly electricity — review replacement bill",
                    max(period["period_start"], original["voided_on"]), draft=True,
                )
                if anchor:
                    conn.execute(update(m.charges).where(m.charges.c.id == anchor["id"]).values(
                        electricity_cycle_start=None))
                conn.execute(update(m.charges).where(m.charges.c.id == replacement).values(
                    replaces_charge_id=original["id"], electricity_cycle_start=cycle,
                    partial=period["partial"]))
                count += 1
                continue
            conn.execute(insert(m.charges).values(
                tenancy_id=tenancy["id"], kind="electricity", amount_paise=None,
                description="Monthly electricity — enter the bill amount",
                period_start=period["period_start"], period_end=period["period_end"],
                due_date=period["period_end"], charge_date=period["period_start"],
                electricity_cycle_start=cycle, status="draft",
                partial=period["partial"], opening=False,
                created_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            ))
            count += 1
        return count

    def prepare_monthly_drafts(self, as_of=None):
        through = iso_date(as_of or today_ist())
        with self._write() as conn:
            total = 0
            for row in conn.execute(select(m.tenancies)).mappings():
                tenancy = dict(row)
                if tenancy.get("archived_on") or tenancy.get("cancelled_on"):
                    continue
                total += self._prepare_drafts(conn, tenancy, through)
                total += self._prepare_electricity_drafts(conn, tenancy, through)
            if total:
                self._audit(conn, "prepare_monthly", "charge", count=total, through=through)
            return total

    def get_bill(self, charge_id, as_of=None):
        cutoff = iso_date(as_of or today_ist())
        with self._read() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            result = self._charge_dict(conn, charge, cutoff)
            tenancy = self._get(conn, m.tenancies, charge["tenancy_id"], "Tenancy")
            self._annotate_electricity_coverage(conn, tenancy, [result], cutoff=cutoff)
            result["billing_cycle_start"] = self._bill_cycle(charge, tenancy["billing_day"])
            result["payment_locked"] = result.get("payment_locked", False) or bool(conn.execute(select(m.cycle_payment_locks.c.locked).where(
                m.cycle_payment_locks.c.tenancy_id == charge["tenancy_id"],
                m.cycle_payment_locks.c.cycle_start == result["billing_cycle_start"],
            )).scalar_one_or_none())
            rows = conn.execute(select(
                m.allocations, m.payments.c.paid_on, m.payments.c.method,
                m.payments.c.reference, m.payments.c.reversed_on,
            ).join(m.payments).where(
                m.allocations.c.charge_id == charge_id,
                m.allocations.c.effective_on <= cutoff,
            ).order_by(m.allocations.c.effective_on, m.allocations.c.id)).mappings()
            result["payments"] = [dict(row) for row in rows]
            return result

    def deposit_payment(self, tenancy_id, amount_paise, paid_on, method, reference="",
                        notes="", proof_path=None):
        """Receive money into balance without paying any bills automatically."""
        paid = iso_date(paid_on)
        if paid > today_ist().isoformat():
            raise ValueError("A received payment cannot have a future date.")
        return self.record_payment(tenancy_id, amount_paise, paid, method,
                                   reference, notes, allocations={}, proof_path=proof_path)

    def mark_bill_paid(self, charge_id, effective_on=None):
        effective = iso_date(effective_on or today_ist())
        if effective > today_ist().isoformat():
            raise ValueError("A bill payment cannot have a future date.")
        with self._write() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            self._ensure_account_open(conn, charge["tenancy_id"])
            self._ensure_cycle_unlocked(conn, charge)
            if charge["status"] != "posted":
                raise ValueError("Review and post this bill before marking it Paid.")
            if effective < charge["charge_date"]:
                raise ValueError("The payment date cannot precede this bill's date.")
            remaining = charge["amount_paise"] - self._allocation_total(
                conn, m.allocations.c.charge_id, charge_id, effective)
            if not remaining:
                return 0
            self._apply_credit(conn, charge["tenancy_id"], {charge_id: remaining}, effective)
            self._audit(conn, "mark_paid", "charge", charge_id,
                        effective_on=effective, amount_paise=remaining)
            return remaining

    def pay_bill(self, charge_id, amount_paise, paid_on, method, reference="",
                 notes="", proof_path=None):
        paid = iso_date(paid_on)
        validate_paise(amount_paise, allow_zero=False)
        if method not in ("cash", "bank", "upi"):
            raise ValueError("Choose cash, bank transfer or UPI.")
        if paid > today_ist().isoformat():
            raise ValueError("A received payment cannot have a future date.")
        copied = None
        try:
            with self._write() as conn:
                charge = self._get(conn, m.charges, charge_id, "Bill")
                self._ensure_account_open(conn, charge["tenancy_id"])
                if charge["status"] != "posted":
                    raise ValueError("Review and post this bill before recording payment.")
                self._validate_full_bill_payment(conn, charge, amount_paise, paid)
                # The allocation layer checks remaining amounts throughout
                # history, preventing a backdated payment from overpaying it.
                copied, original_name = self._copy_proof(proof_path)
                payment_id = self._record_payment(
                    conn, charge["tenancy_id"], amount_paise, paid, method,
                    reference, notes, {charge_id: amount_paise}, copied, original_name,
                )
                self._audit(conn, "receive", "payment", payment_id, charge_id=charge_id)
                return payment_id
        except BaseException:
            if copied:
                (self.data_dir / copied).unlink(missing_ok=True)
            raise

    def mark_bill_unpaid(self, charge_id, reason, effective_on=None, *, keep_credit=True):
        """Release a bill's applied money back to balance; receipts are retained."""
        effective = iso_date(effective_on or today_ist())
        if effective > today_ist().isoformat():
            raise ValueError("A payment correction cannot have a future date.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
            raise ValueError("Enter a reason for the payment correction.")
        reason = reason.strip()
        with self._write() as conn:
            charge = self._get(conn, m.charges, charge_id, "Bill")
            self._ensure_account_open(conn, charge["tenancy_id"])
            self._ensure_cycle_unlocked(conn, charge)
            if charge["status"] != "posted":
                raise ValueError("Choose a posted bill with a recorded payment.")
            allocations = conn.execute(select(m.allocations).where(
                m.allocations.c.charge_id == charge_id,
                m.allocations.c.cancelled_on.is_(None),
            )).mappings().all()
            if not allocations:
                raise ValueError("This bill has no active payments to correct.")
            if effective < charge["charge_date"] or any(
                row["effective_on"] > effective for row in allocations
            ):
                raise ValueError("The correction date must be on or after this bill's payments.")
            conn.execute(update(m.allocations).where(
                m.allocations.c.charge_id == charge_id,
                m.allocations.c.cancelled_on.is_(None),
            ).values(cancelled_on=effective, cancel_reason=reason))
            self._audit(conn, "mark_unpaid", "charge", charge_id,
                        effective_on=effective, reason=reason, keep_credit=True)
