"""Dated occupancy changes and account closure without rewriting receipts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, insert, or_, select, update

from . import models as m
from .clock import today_ist
from .dates import as_date, cycle_bounds, iso_date, iter_account_periods
from .money import validate_paise


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
        raise ValueError("Enter a reason of at most 10,000 characters.")
    return value.strip()


class LifecycleMixin:
    def _scheduled_move_corrections(self, conn, charges):
        """Bring pending legacy cancellations forward without changing the past.

        Earlier versions allowed a bill and its payment releases to be cancelled
        on a future date. Its stored status is void, but it still contributes to
        today's balance until that date. Future allocations cannot safely be
        moved with the cancellation, so those require an explicit correction.
        """
        today = today_ist().isoformat()
        affected = {charge["id"]: charge for charge in charges}
        if not affected:
            return []
        allocations = {charge_id: [] for charge_id in affected}
        for row in conn.execute(select(m.allocations).where(
            m.allocations.c.charge_id.in_(affected),
        )).mappings():
            allocations[row["charge_id"]].append(dict(row))
        result = []
        for charge_id, charge in affected.items():
            effective = max(today, charge["charge_date"])
            rows = allocations[charge_id]
            future = [row["effective_on"] for row in rows if row["effective_on"] > effective]
            if future:
                raise ValueError(
                    f"Bill {charge_id} has a scheduled payment allocation on {min(future)}. "
                    "Correct that scheduled payment before changing the move-out; no records were changed."
                )
            if charge["status"] != "void":
                later_releases = [row["cancelled_on"] for row in rows
                                  if row["cancelled_on"] and row["cancelled_on"] > effective]
                if later_releases:
                    raise ValueError(
                        f"Bill {charge_id} has a scheduled payment correction on {min(later_releases)}. "
                        "Correct that scheduled payment change before changing the move-out; no records were changed."
                    )
                continue
            if charge["voided_on"] <= effective:
                continue
            result.append({
                "charge_id": charge_id, "previous_voided_on": charge["voided_on"],
                "effective_on": effective,
                # Allocations without a cancellation are included by the usual
                # credit summary. Count only newly advanced releases here.
                "returned_paise": sum(row["amount_paise"] for row in rows
                                      if row["cancelled_on"] and row["cancelled_on"] > effective),
            })
        return result

    def _advance_move_cancellations(self, conn, corrections, reason):
        for correction in corrections:
            charge_id, effective = correction["charge_id"], correction["effective_on"]
            charge = self._get(conn, m.charges, charge_id, "Bill")
            self._ensure_cycle_unlocked(conn, charge)
            allocations = [dict(row) for row in conn.execute(select(m.allocations).where(
                m.allocations.c.charge_id == charge_id,
                or_(m.allocations.c.cancelled_on.is_(None), m.allocations.c.cancelled_on > effective),
            )).mappings()]
            conn.execute(update(m.charges).where(m.charges.c.id == charge_id).values(
                voided_on=effective, void_reason=reason,
            ))
            if allocations:
                conn.execute(update(m.allocations).where(
                    m.allocations.c.id.in_([row["id"] for row in allocations]),
                ).values(cancelled_on=effective, cancel_reason=reason))
            self._audit(conn, "advance_move_out_cancellation", "charge", charge_id,
                        previous_bill=charge, previous_allocations=allocations,
                        effective_on=effective, reason=reason)

    def _released_move_credit(self, conn, charge_ids):
        if not charge_ids:
            return 0
        return conn.execute(select(func.coalesce(func.sum(m.allocations.c.amount_paise), 0)).where(
            m.allocations.c.charge_id.in_(charge_ids), m.allocations.c.cancelled_on.is_(None),
        )).scalar_one()

    def _move_locks(self, conn, tenancy_id, changed_cycles):
        return sorted(set(conn.execute(select(m.cycle_payment_locks.c.cycle_start).where(
            m.cycle_payment_locks.c.tenancy_id == tenancy_id,
            m.cycle_payment_locks.c.locked.is_(True),
        )).scalars()) & changed_cycles)

    @staticmethod
    def _check_move_locks(summary):
        if summary["locked_cycle_starts"]:
            raise ValueError("Payments for affected billing cycles are locked. Unlock these cycles first: "
                             + ", ".join(summary["locked_cycle_starts"]))

    def _occupancy_correction_date(self, conn, charge, requested):
        dates = [requested, charge["charge_date"]]
        for row in conn.execute(select(m.allocations.c.effective_on, m.allocations.c.cancelled_on).where(
            m.allocations.c.charge_id == charge["id"],
        )):
            dates.extend(value for value in row if value)
        return max(dates)

    def _occupancy_history(self, conn, tenancy_id):
        return [dict(row) for row in conn.execute(
            select(m.tenancy_occupancies, m.flats.c.building_id, m.flats.c.number,
                   m.flats.c.floor, m.flats.c.bhk, m.buildings.c.name.label("building_name"),
                   m.buildings.c.address)
            .join(m.flats, m.tenancy_occupancies.c.flat_id == m.flats.c.id)
            .join(m.buildings, m.flats.c.building_id == m.buildings.c.id)
            .where(m.tenancy_occupancies.c.tenancy_id == tenancy_id)
            .order_by(m.tenancy_occupancies.c.starts_on)
        ).mappings()]

    def _assert_flat_available(self, conn, flat_id, start, end, excluding=()):
        query = select(m.tenancy_occupancies.c.id).join(
            m.tenancies, m.tenancy_occupancies.c.tenancy_id == m.tenancies.c.id,
        ).where(
            m.tenancies.c.cancelled_on.is_(None),
            m.tenancy_occupancies.c.flat_id == flat_id,
            m.tenancy_occupancies.c.starts_on <= (end or "9999-12-31"),
            or_(m.tenancy_occupancies.c.ends_on.is_(None), m.tenancy_occupancies.c.ends_on >= start),
        )
        if excluding:
            query = query.where(m.tenancy_occupancies.c.tenancy_id.not_in(excluding))
        if conn.execute(query.limit(1)).first():
            raise ValueError("This flat has an overlapping tenancy for the selected dates.")

    def _transfer_source(self, conn, tenancy, effective):
        self._ensure_tenancy_not_cancelled(tenancy)
        history = self._occupancy_history(conn, tenancy["id"])
        latest = history[-1]
        if effective <= latest["starts_on"]:
            raise ValueError("Choose a transfer date after the latest recorded or scheduled occupancy began.")
        if tenancy["move_out"] and effective > tenancy["move_out"]:
            raise ValueError("The transfer date is after the tenant's last occupied day.")
        return latest

    def _transfer_rate(self, conn, tenancy, effective, amount):
        if amount is None:
            return None, None
        validate_paise(amount)
        start, next_start = cycle_bounds(effective, tenancy["billing_day"])
        rate_date = start.isoformat() if start.isoformat() == effective else next_start.isoformat()
        if tenancy["move_out"] and rate_date > tenancy["move_out"]:
            raise ValueError("The new monthly rent would start after move-out. Leave rent unchanged and review the final bill.")
        if conn.execute(select(m.rent_rates.c.id).where(
            m.rent_rates.c.tenancy_id == tenancy["id"], m.rent_rates.c.effective_from >= rate_date,
        ).limit(1)).first():
            raise ValueError("Cancel the conflicting scheduled rent change before setting a transfer rent.")
        if conn.execute(select(m.charges.c.id).where(
            m.charges.c.tenancy_id == tenancy["id"], m.charges.c.kind == "rent",
            m.charges.c.period_start >= rate_date, m.charges.c.posted_at.is_not(None),
        ).limit(1)).first():
            raise ValueError("Affected rent bills are already posted. Correct them explicitly before changing the rate.")
        rate_id = conn.execute(insert(m.rent_rates).values(
            tenancy_id=tenancy["id"], effective_from=rate_date, amount_paise=amount, created_at=_now(),
        )).inserted_primary_key[0]
        conn.execute(update(m.charges).where(
            m.charges.c.tenancy_id == tenancy["id"], m.charges.c.kind == "rent",
            m.charges.c.status == "draft", m.charges.c.partial.is_(False),
            m.charges.c.period_start >= rate_date,
        ).values(amount_paise=amount))
        return rate_date, rate_id

    def _move_occupancy(self, conn, tenancy, source, target, effective, rate_id=None, paired_tenancy_id=None):
        conn.execute(update(m.tenancy_occupancies).where(m.tenancy_occupancies.c.id == source["id"]).values(
            ends_on=(as_date(effective) - timedelta(days=1)).isoformat(),
        ))
        conn.execute(insert(m.tenancy_occupancies).values(
            tenancy_id=tenancy["id"], flat_id=target, starts_on=effective, ends_on=tenancy["move_out"],
            rent_rate_id=rate_id, paired_tenancy_id=paired_tenancy_id,
        ))
        conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy["id"]).values(flat_id=target))

    def transfer_tenancy(self, tenancy_id, flat_id, effective_on, rent_paise=None, reason="Flat transfer"):
        effective, reason = iso_date(effective_on), _reason(reason)
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            self._get(conn, m.flats, flat_id, "Flat")
            source = self._transfer_source(conn, tenancy, effective)
            if source["flat_id"] == flat_id:
                raise ValueError("Choose a different destination flat.")
            self._assert_flat_available(conn, flat_id, effective, tenancy["move_out"], (tenancy_id,))
            rate_date, rate_id = self._transfer_rate(conn, tenancy, effective, rent_paise)
            self._move_occupancy(conn, tenancy, source, flat_id, effective, rate_id)
            self._audit(conn, "transfer", "tenancy", tenancy_id, from_flat_id=source["flat_id"],
                        to_flat_id=flat_id, effective_on=effective, rent_effective_from=rate_date,
                        rent_paise=rent_paise, reason=reason)
            return {"rent_effective_from": rate_date}

    def exchange_tenancies(self, first_id, second_id, effective_on, first_rent_paise=None,
                          second_rent_paise=None, reason="Flat exchange"):
        effective, reason = iso_date(effective_on), _reason(reason)
        if first_id == second_id:
            raise ValueError("Choose two different tenant accounts.")
        with self._write() as conn:
            first = self._ensure_account_open(conn, first_id)
            second = self._ensure_account_open(conn, second_id)
            sources = [self._transfer_source(conn, tenant, effective) for tenant in (first, second)]
            if sources[0]["flat_id"] == sources[1]["flat_id"]:
                raise ValueError("The tenant accounts must occupy different flats.")
            dates = {}
            for tenant, source, target, amount in (
                (first, sources[0], sources[1]["flat_id"], first_rent_paise),
                (second, sources[1], sources[0]["flat_id"], second_rent_paise),
            ):
                self._assert_flat_available(conn, target, effective, tenant["move_out"], (first_id, second_id))
                dates[tenant["id"]], rate_id = self._transfer_rate(conn, tenant, effective, amount)
                self._move_occupancy(conn, tenant, source, target, effective, rate_id,
                                     second_id if tenant["id"] == first_id else first_id)
                self._audit(conn, "exchange", "tenancy", tenant["id"],
                            other_tenancy_id=second_id if tenant["id"] == first_id else first_id,
                            from_flat_id=source["flat_id"], to_flat_id=target,
                            effective_on=effective, rent_effective_from=dates[tenant["id"]],
                            rent_paise=amount, reason=reason)
            return dates

    def cancel_scheduled_transfer(self, tenancy_id, reason="Cancelled planned flat transfer"):
        reason = _reason(reason)
        today = today_ist().isoformat()
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            history = self._occupancy_history(conn, tenancy_id)
            self._ensure_tenancy_not_cancelled(tenancy)
            if len(history) < 2 or history[-1]["starts_on"] <= today:
                raise ValueError("Only a flat move that has not started can be cancelled.")
            plans = [(tenancy, history[-2], history[-1])]
            paired = history[-1]["paired_tenancy_id"]
            if paired:
                other = self._ensure_account_open(conn, paired)
                other_history = self._occupancy_history(conn, paired)
                if (len(other_history) < 2 or other_history[-1]["starts_on"] != history[-1]["starts_on"]
                        or other_history[-1]["paired_tenancy_id"] != tenancy_id):
                    raise ValueError("Cancel any later planned move before cancelling this exchange.")
                plans.append((other, other_history[-2], other_history[-1]))
            affected = [tenant["id"] for tenant, _, _ in plans]
            for tenant, previous, plan in plans:
                self._assert_flat_available(conn, previous["flat_id"], plan["starts_on"], tenant["move_out"], affected)
                rate = self._get(conn, m.rent_rates, plan["rent_rate_id"], "Rent rate") if plan["rent_rate_id"] else None
                changed_bills = []
                if rate:
                    for bill in conn.execute(select(m.charges).where(
                        m.charges.c.tenancy_id == tenant["id"], m.charges.c.kind == "rent",
                        m.charges.c.status != "void", m.charges.c.opening.is_(False),
                        m.charges.c.period_start >= rate["effective_from"],
                    )).mappings():
                        self._ensure_cycle_unlocked(conn, bill)
                        if bill["status"] == "posted":
                            self._void_charge(conn, bill["id"], reason,
                                              self._occupancy_correction_date(conn, bill, today))
                            changed_bills.append(dict(bill))
                conn.execute(delete(m.tenancy_occupancies).where(m.tenancy_occupancies.c.id == plan["id"]))
                conn.execute(update(m.tenancy_occupancies).where(m.tenancy_occupancies.c.id == previous["id"]).values(ends_on=tenant["move_out"]))
                conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenant["id"]).values(flat_id=previous["flat_id"]))
                if rate:
                    conn.execute(delete(m.rent_rates).where(m.rent_rates.c.id == rate["id"]))
                    for draft in conn.execute(select(m.charges).where(
                        m.charges.c.tenancy_id == tenant["id"], m.charges.c.kind == "rent",
                        m.charges.c.status == "draft", m.charges.c.partial.is_(False),
                        m.charges.c.period_start >= rate["effective_from"],
                    )).mappings():
                        conn.execute(update(m.charges).where(m.charges.c.id == draft["id"]).values(
                            amount_paise=self._rent_rate(conn, tenant["id"], draft["period_start"]),
                        ))
                    if changed_bills:
                        self._prepare_drafts(conn, tenant, max(bill["period_start"] for bill in changed_bills))
                self._audit(conn, "cancel_planned_transfer", "tenancy", tenant["id"],
                            cancelled_occupancy=plan, cancelled_rate=rate,
                            corrected_bill_ids=[bill["id"] for bill in changed_bills], reason=reason)
            return affected

    def archive_tenancy(self, tenancy_id):
        today = today_ist().isoformat()
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            if not tenancy.get("cancelled_on") and (not tenancy["move_out"] or tenancy["move_out"] >= today):
                raise ValueError("Close an account only after the tenant's last occupied day.")
            if not tenancy.get("cancelled_on"):
                self._prepare_drafts(conn, tenancy, tenancy["move_out"])
                self._prepare_electricity_drafts(conn, tenancy, tenancy["move_out"])
            # Future dated entries must not become hidden debt after closure.
            balances = [self._balance(conn, tenancy_id, cutoff) for cutoff in (today, "9999-12-31")]
            if any(balance[key] for balance in balances for key in ("outstanding_paise", "credit_paise", "deposit_paise")):
                raise ValueError("Settle all unpaid bills, advance credit and security deposits before closing this account.")
            if self._has_unreviewed_bills(conn, tenancy_id):
                raise ValueError("Review and post pending bill drafts and deferred electricity before closing this account; electricity may be zero.")
            conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy_id).values(archived_on=today))
            self._audit(conn, "archive", "tenancy", tenancy_id, archived_on=today)

    def unarchive_tenancy(self, tenancy_id):
        with self._write() as conn:
            tenancy = self._get(conn, m.tenancies, tenancy_id, "Tenancy")
            if not tenancy["archived_on"]:
                raise ValueError("This tenant account is already open.")
            conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy_id).values(archived_on=None))
            self._audit(conn, "unarchive", "tenancy", tenancy_id)

    def _move_out_plan(self, conn, tenancy, last, final_rent_paise):
        """Read the exact effects before writing any occupancy or bill changes."""
        self._ensure_tenancy_not_cancelled(tenancy)
        if last < tenancy["move_in"]:
            raise ValueError("The last occupied day cannot precede move-in.")
        history = self._occupancy_history(conn, tenancy["id"])
        latest = history[-1]
        if last < latest["starts_on"]:
            raise ValueError("The last occupied day cannot precede a recorded or scheduled flat transfer.")
        self._assert_flat_available(conn, latest["flat_id"], latest["starts_on"], last, (tenancy["id"],))
        opening_only = last < tenancy["opening_date"]
        if opening_only and final_rent_paise:
            raise ValueError("This departure is before the account opening date. Keep older rent in the opening balance and use zero final rent here.")
        revised = dict(tenancy, move_out=last)
        periods = {period["cycle_start"]: period for period in iter_account_periods(revised, last)}
        previous = tenancy["move_out"]
        first_affected = cycle_bounds(previous if previous and last > previous else last,
                                      tenancy["billing_day"])[0].isoformat()
        active = [dict(row) for row in conn.execute(select(m.charges).where(
            m.charges.c.tenancy_id == tenancy["id"], m.charges.c.opening.is_(False),
            or_(m.charges.c.status != "void", and_(
                m.charges.c.voided_on > today_ist().isoformat(),
                m.charges.c.voided_on > m.charges.c.charge_date,
            )),
            m.charges.c.deleted_on.is_(None),
        ).order_by(m.charges.c.id)).mappings()]
        stopped, corrected = [], []
        changed_cycles = set()
        for charge in active:
            cycle = self._bill_cycle(charge, tenancy["billing_day"])
            period = periods.get(cycle)
            if charge["period_start"] > last:
                stopped.append(charge)
                changed_cycles.add(cycle)
            elif charge["kind"] == "rent" and period and cycle >= first_affected:
                final = period["period_end"] == last
                if charge["posted_at"] is None:
                    changed_cycles.add(cycle)
                elif (charge["period_end"] != period["period_end"]
                      or (final and charge["amount_paise"] != final_rent_paise)):
                    corrected.append(charge)
                    changed_cycles.add(cycle)
            elif (charge["kind"] == "electricity" and charge["electricity_cycle_start"] and period
                  and any(charge[key] != period[key] for key in ("period_start", "period_end", "partial"))):
                changed_cycles.add(cycle)
                if charge["posted_at"] is not None:
                    corrected.append(charge)
        final_cycle, next_cycle = cycle_bounds(last, tenancy["billing_day"])
        # Posting a newly created final rent is also a financial change.
        if not opening_only and not any(
            charge["kind"] == "rent" and charge["status"] == "posted"
            and self._bill_cycle(charge, tenancy["billing_day"]) == final_cycle.isoformat()
            and charge["period_end"] == last and charge["amount_paise"] == final_rent_paise
            for charge in active
        ):
            changed_cycles.add(final_cycle.isoformat())
        released_ids = {row["id"] for row in (*stopped, *corrected)}
        if released_ids:
            changed_cycles.update(self._bill_cycle(row, tenancy["billing_day"]) for row in
                conn.execute(select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy["id"], m.charges.c.status != "void",
                    m.charges.c.electricity_covered_by_charge_id.in_(released_ids),
                )).mappings())
        advanced = self._scheduled_move_corrections(conn, [*stopped, *corrected])
        summary = {
            "last_day": last, "previous_last_day": previous,
            "moved_out_cycle_start": final_cycle.isoformat(),
            "moved_out_cycle_end": (next_cycle - timedelta(days=1)).isoformat(),
            "stopped_bill_ids": [row["id"] for row in stopped],
            "stopped_cycle_starts": sorted({self._bill_cycle(row, tenancy["billing_day"]) for row in stopped}),
            "corrected_bill_ids": [row["id"] for row in corrected],
            "electricity_review_bill_ids": [row["id"] for row in corrected if row["kind"] == "electricity"],
            "returned_paise": self._released_move_credit(conn, released_ids)
                              + sum(item["returned_paise"] for item in advanced),
            "advanced_cancellations": advanced,
            "locked_cycle_starts": self._move_locks(conn, tenancy["id"], changed_cycles),
            "final_rent_recorded": not opening_only,
        }
        return latest, summary

    def preview_move_out(self, tenancy_id, last_day, final_rent_paise):
        """Show stopped cycles, returned money and locks without changing data."""
        last = iso_date(last_day)
        validate_paise(final_rent_paise)
        with self._read() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            return self._move_out_plan(conn, tenancy, last, final_rent_paise)[1]

    def move_out(self, tenancy_id, last_day, final_rent_paise, reason="Final rent agreed at move-out"):
        last, reason = iso_date(last_day), _reason(reason)
        validate_paise(final_rent_paise)
        correction = today_ist().isoformat()
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            previous = tenancy["move_out"]
            latest, summary = self._move_out_plan(conn, tenancy, last, final_rent_paise)
            self._check_move_locks(summary)
            conn.execute(update(m.tenancy_occupancies).where(m.tenancy_occupancies.c.id == latest["id"]).values(ends_on=last))
            conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy_id).values(move_out=last))
            tenancy["move_out"] = last
            self._advance_move_cancellations(conn, summary["advanced_cancellations"], reason)
            self._prepare_drafts(conn, tenancy, last)
            active = [dict(row) for row in conn.execute(select(m.charges).where(
                m.charges.c.tenancy_id == tenancy_id,
                m.charges.c.opening.is_(False), m.charges.c.status != "void",
                m.charges.c.deleted_on.is_(None),
            ).order_by(m.charges.c.id)).mappings()]
            for charge in active:
                if (charge["period_start"] > last
                        or charge["id"] in summary["electricity_review_bill_ids"]):
                    self._void_charge(conn, charge["id"], reason, self._occupancy_correction_date(conn, charge, correction))
            rents = {self._bill_cycle(row, tenancy["billing_day"]): row for row in active
                     if row["kind"] == "rent" and row["period_start"] <= last}
            first_affected = cycle_bounds(previous, tenancy["billing_day"])[0].isoformat() if previous and last > previous else cycle_bounds(last, tenancy["billing_day"])[0].isoformat()
            for period in iter_account_periods(tenancy, last):
                if period["cycle_start"] < first_affected:
                    continue
                current = rents.get(period["cycle_start"])
                final = period["period_end"] == last
                amount = final_rent_paise if final else (None if period["partial"] else self._rent_rate(conn, tenancy_id, period["period_start"]))
                if current and current["status"] == "posted" and (current["period_end"] != period["period_end"] or (final and current["amount_paise"] != amount)):
                    self._void_charge(conn, current["id"], reason, self._occupancy_correction_date(conn, current, correction))
                    current = None
                if current is None:
                    # Earlier app versions dated some planned corrections at
                    # departure. A replacement must remain after every linked
                    # correction, even when that departure is shortened.
                    next_cycle = cycle_bounds(period["period_start"], tenancy["billing_day"])[1].isoformat()
                    correction_dates = conn.execute(select(m.charges.c.voided_on).where(
                        m.charges.c.tenancy_id == tenancy_id, m.charges.c.kind == "rent",
                        m.charges.c.period_start >= period["cycle_start"], m.charges.c.period_start < next_cycle,
                        m.charges.c.voided_on.is_not(None),
                    )).scalars().all()
                    replacement_date = max(correction, period["period_start"], *correction_dates)
                    self._insert_charge(conn, tenancy, "rent", amount, period["period_start"], period["period_end"],
                                        period["period_end"], reason if final else "Monthly rent — review revised stay",
                                        replacement_date, draft=not final)
                elif current["status"] == "draft":
                    conn.execute(update(m.charges).where(m.charges.c.id == current["id"]).values(
                        period_end=period["period_end"], due_date=period["period_end"], partial=period["partial"],
                        amount_paise=amount, description=reason if final else "Monthly rent — review revised stay",
                    ))
                    if final:
                        self._post_charge(conn, current["id"], amount, apply_credit=False)
            self._prepare_electricity_drafts(conn, tenancy, last)
            self._audit(conn, "move_out" if previous is None else "revise_move_out", "tenancy", tenancy_id,
                        previous_last_day=previous, last_day=last, final_rent_paise=final_rent_paise,
                        stopped_bill_ids=summary["stopped_bill_ids"],
                        corrected_bill_ids=summary["corrected_bill_ids"],
                        electricity_review_bill_ids=summary["electricity_review_bill_ids"],
                        advanced_cancellations=summary["advanced_cancellations"],
                        returned_paise=summary["returned_paise"], reason=reason)
            return summary

    def _cancel_move_out_plan(self, conn, tenancy):
        self._ensure_tenancy_not_cancelled(tenancy)
        previous = tenancy["move_out"]
        today = today_ist().isoformat()
        if not previous or previous <= today:
            raise ValueError("Only a future planned move-out can be cancelled. Revise the move-out date for a completed departure.")
        latest = self._occupancy_history(conn, tenancy["id"])[-1]
        self._assert_flat_available(conn, latest["flat_id"], latest["starts_on"], None, (tenancy["id"],))
        bills = [dict(row) for row in conn.execute(select(m.charges).where(
            m.charges.c.tenancy_id == tenancy["id"], m.charges.c.opening.is_(False),
            m.charges.c.deleted_on.is_(None),
        ).order_by(m.charges.c.id)).mappings()]
        through = max(today, previous, *(row["period_start"] for row in bills
                                        if row["kind"] in {"rent", "electricity"}))
        revised = dict(tenancy, move_out=None)
        periods = {period["cycle_start"]: period for period in iter_account_periods(revised, through)}
        active = [row for row in bills if row["status"] != "void"
                  or row["voided_on"] > max(today, row["charge_date"])]
        corrected, drafts, changed_cycles = [], [], set()
        former_final = cycle_bounds(previous, tenancy["billing_day"])[0].isoformat()
        for charge in active:
            cycle = self._bill_cycle(charge, tenancy["billing_day"])
            period = periods.get(cycle)
            if not period or cycle < former_final:
                continue
            dates_changed = any(charge[key] != period[key] for key in ("period_start", "period_end", "partial"))
            if charge["kind"] == "rent":
                amount = None if period["partial"] else self._rent_rate(conn, tenancy["id"], period["period_start"])
                if dates_changed or (not period["partial"] and charge["amount_paise"] != amount):
                    changed_cycles.add(cycle)
                    if charge["posted_at"] is not None:
                        corrected.append(charge)
                    else:
                        drafts.append((charge, period, amount))
            elif charge["kind"] == "electricity" and charge["electricity_cycle_start"] and dates_changed:
                changed_cycles.add(cycle)
                if charge["posted_at"] is not None:
                    corrected.append(charge)
        # Stopped monthly bills will be reissued for review, never restored as
        # paid bills. Their locks must be respected before resuming occupancy.
        active_kinds = {(self._bill_cycle(row, tenancy["billing_day"]), row["kind"]) for row in active}
        for cycle in periods:
            if cycle >= former_final and any((cycle, kind) not in active_kinds for kind in ("rent", "electricity")):
                changed_cycles.add(cycle)
        advanced = self._scheduled_move_corrections(conn, corrected)
        corrected_ids = {row["id"] for row in corrected}
        if corrected_ids:
            changed_cycles.update(self._bill_cycle(row, tenancy["billing_day"]) for row in
                conn.execute(select(m.charges).where(
                    m.charges.c.tenancy_id == tenancy["id"], m.charges.c.status != "void",
                    m.charges.c.electricity_covered_by_charge_id.in_(corrected_ids),
                )).mappings())
        summary = {
            "previous_last_day": previous,
            "corrected_bill_ids": [row["id"] for row in corrected],
            "electricity_review_bill_ids": [row["id"] for row in corrected if row["kind"] == "electricity"],
            "returned_paise": self._released_move_credit(conn, {row["id"] for row in corrected})
                              + sum(item["returned_paise"] for item in advanced),
            "advanced_cancellations": advanced,
            "locked_cycle_starts": self._move_locks(conn, tenancy["id"], changed_cycles),
            "resumed_through": through,
        }
        return latest, corrected, drafts, summary

    def preview_cancel_move_out(self, tenancy_id):
        """Review the effects of resuming an indefinitely occupied tenancy."""
        with self._read() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            return self._cancel_move_out_plan(conn, tenancy)[-1]

    def cancel_scheduled_move_out(self, tenancy_id, reason="Cancelled planned move-out"):
        reason = _reason(reason)
        correction = today_ist().isoformat()
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            latest, corrected, drafts, summary = self._cancel_move_out_plan(conn, tenancy)
            self._check_move_locks(summary)
            conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy_id).values(move_out=None))
            conn.execute(update(m.tenancy_occupancies).where(m.tenancy_occupancies.c.id == latest["id"]).values(ends_on=None))
            tenancy["move_out"] = None
            self._advance_move_cancellations(conn, summary["advanced_cancellations"], reason)
            for charge in corrected:
                if charge["status"] != "void":
                    self._void_charge(conn, charge["id"], reason, self._occupancy_correction_date(conn, charge, correction))
            for charge, period, amount in drafts:
                conn.execute(update(m.charges).where(m.charges.c.id == charge["id"]).values(
                    period_start=period["period_start"], period_end=period["period_end"],
                    due_date=period["period_end"], partial=period["partial"], amount_paise=amount,
                    description="Monthly rent — review resumed stay",
                ))
            self._prepare_drafts(conn, tenancy, summary["resumed_through"])
            self._prepare_electricity_drafts(conn, tenancy, summary["resumed_through"])
            self._audit(conn, "cancel_planned_move_out", "tenancy", tenancy_id, reason=reason, **summary)
            return summary
