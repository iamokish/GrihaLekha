"""Correct bills without rewriting earlier account balances."""

import json
from datetime import UTC, datetime

from sqlalchemy import select, update

from . import models as m
from .clock import today_ist
from .dates import cycle_bounds, iso_date
from .money import validate_paise


_REPLACEMENT_DRAFTS = {
    "Monthly rent — review replacement bill",
    "Monthly electricity — review replacement bill",
}


def _descendant_ids(bills, charge_id):
    children = {}
    for bill in bills:
        children.setdefault(bill["replaces_charge_id"], []).append(bill["id"])
    linked = {charge_id}
    pending = [charge_id]
    while pending:
        for child_id in children.get(pending.pop(), ()):
            if child_id not in linked:
                linked.add(child_id)
                pending.append(child_id)
    return linked


class BillCorrectionMixin:
    def revise_bill_amount(self, charge_id, amount_paise, reason, effective_on=None):
        """Correct a posted bill while preserving its original dated history.

        The original and its dated allocations remain in history. The returned
        bill keeps the service period and is unpaid unless its amount is zero.
        Repeating the existing amount is a no-op, including for a paid bill.
        Miscellaneous bills must have no applied money before an amount change;
        mark them Unpaid first. Required bills retain their existing correction
        behavior of returning applied money to balance.
        """
        validate_paise(amount_paise)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10_000:
            raise ValueError("Enter a reason for correcting the bill amount.")
        reason = reason.strip()
        requested = iso_date(effective_on) if effective_on is not None else None
        with self._write() as conn:
            original = self._get(conn, m.charges, charge_id, "Bill")
            tenancy = self._ensure_account_open(conn, original["tenancy_id"])
            if original["kind"] in {"rent", "electricity"} and not original["opening"] and tenancy.get("cancelled_on"):
                self._ensure_tenancy_not_cancelled(tenancy)
            self._ensure_cycle_unlocked(conn, original)
            if original["status"] != "posted":
                raise ValueError("Choose a posted bill to correct. Enter draft amounts when reviewing them.")
            effective = requested or max(today_ist().isoformat(), original["charge_date"])
            if effective < original["charge_date"]:
                raise ValueError("A correction cannot take effect before the charge date.")
            if amount_paise == original["amount_paise"]:
                return charge_id

            returned = self._allocation_total(conn, m.allocations.c.charge_id, charge_id, effective)
            if original["kind"] == "misc" and returned:
                raise ValueError("Mark this miscellaneous bill Unpaid before correcting its amount. Its payment receipts will be retained.")
            # The private cancellation validates allocation chronology before
            # releasing money; inserting/auditing below shares its transaction.
            self._void_charge(conn, charge_id, reason, effective)
            revised_id = self._insert_charge(
                conn, tenancy, original["kind"], amount_paise,
                original["period_start"], original["period_end"], original["due_date"],
                original["description"], effective, opening=original["opening"],
            )
            conn.execute(update(m.charges).where(m.charges.c.id == revised_id).values(
                replaces_charge_id=charge_id, partial=original["partial"],
            ))
            self._carry_electricity_metadata(conn, original, revised_id)
            # Keep the monthly electricity anchor on its active bill so later
            # draft preparation and move-out corrections cannot duplicate it.
            if original["electricity_cycle_start"]:
                conn.execute(update(m.charges).where(m.charges.c.id == charge_id).values(
                    electricity_cycle_start=None,
                ))
                conn.execute(update(m.charges).where(m.charges.c.id == revised_id).values(
                    electricity_cycle_start=original["electricity_cycle_start"],
                ))
            self._audit(conn, "revise_bill_amount", "charge", charge_id,
                        reason=reason, effective_on=effective, revised_charge_id=revised_id,
                        amount_paise=amount_paise, returned_to_balance_paise=returned,
                        original=original)
            return revised_id

    def replace_cancelled_bill(self, charge_id, items):
        """Post linked replacement lines in one atomic correction operation."""
        if not isinstance(items, list) or not items or len(items) > 100:
            raise ValueError("Provide between 1 and 100 replacement bill lines.")
        with self._write() as conn:
            original = self._get(conn, m.charges, charge_id, "Bill")
            tenancy = self._ensure_account_open(conn, original["tenancy_id"])
            if original["kind"] in {"rent", "electricity"} and not original["opening"] and tenancy.get("cancelled_on"):
                self._ensure_tenancy_not_cancelled(tenancy)
            self._ensure_cycle_unlocked(conn, original)
            if original.get("deleted_on"):
                raise ValueError("A deleted bill cannot be replaced. Add a new bill if a new charge is needed.")
            if original["status"] != "void":
                raise ValueError("Choose a cancelled bill to replace.")
            if original["kind"] in {"rent", "electricity"} and len(items) != 1:
                raise ValueError("Replace rent or electricity with one bill. Add unrelated charges separately.")
            bills = [dict(row) for row in conn.execute(select(m.charges).where(
                m.charges.c.tenancy_id == tenancy["id"],
            )).mappings()]
            linked = _descendant_ids(bills, charge_id)
            descendants = [bill for bill in bills if bill["id"] in linked and bill["id"] != charge_id]
            if any(bill.get("deleted_on") for bill in descendants):
                raise ValueError("A replacement of this bill was deleted. Add a new bill if a new charge is needed.")
            if any(bill["status"] != "void" for bill in descendants):
                raise ValueError("An active replacement already exists. Review or cancel it before replacing this bill again.")
            latest_correction = max(original["charge_date"], original["voided_on"])
            for bill in descendants:
                latest_correction = max(latest_correction, bill["charge_date"], bill["voided_on"] or bill["charge_date"])

            required = original["kind"] in {"rent", "electricity"} and not original["opening"]
            plans = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Each replacement bill must contain an amount, dates and charge type.")
                values = dict(item)
                if values.get("kind", original["kind"]) != original["kind"]:
                    raise ValueError("Every replacement must keep the original bill's charge type. Add other fees separately.")
                if values.get("opening", original["opening"]) != original["opening"]:
                    raise ValueError("A replacement must keep the original bill's opening-entry status.")
                values.update(kind=original["kind"], opening=original["opening"])
                values.setdefault("meter_reading_hundredths", original["meter_reading_hundredths"])
                try:
                    start, end = iso_date(values["period_start"]), iso_date(values["period_end"])
                    iso_date(values["due_date"])
                except KeyError as exc:
                    raise ValueError("The replacement bill fields are incomplete.") from exc
                self._ensure_cycle_unlocked(conn, {
                    "tenancy_id": tenancy["id"], "period_start": start,
                    "cycle_start": None, "electricity_cycle_start": None,
                })
                earliest = latest_correction
                if required:
                    cycles = {
                        self._bill_cycle(original, tenancy["billing_day"]),
                        cycle_bounds(start, tenancy["billing_day"])[0].isoformat(),
                    }
                    for bill in bills:
                        if bill["id"] == charge_id or bill["kind"] != original["kind"] or bill["opening"]:
                            continue
                        relevant = self._bill_cycle(bill, tenancy["billing_day"]) in cycles
                        if original["kind"] == "electricity":
                            relevant |= bill["period_start"] <= end and bill["period_end"] >= start
                            relevant |= bill["period_start"] <= original["period_end"] and bill["period_end"] >= original["period_start"]
                        if not relevant:
                            continue
                        if bill["status"] != "void":
                            raise ValueError("An active bill already covers this billing cycle. Review or cancel it before posting a replacement.")
                        earliest = max(earliest, bill["voided_on"] or bill["charge_date"])
                charged = (iso_date(values["charge_date"]) if values.get("charge_date") is not None
                           else max(today_ist().isoformat(), earliest, start))
                if charged < earliest:
                    raise ValueError("The replacement date must be on or after the original and later linked or overlapping bill cancellations.")
                values["charge_date"] = charged
                plans.append(values)

            result = []
            for values in plans:
                try:
                    new_id = self._insert_charge(conn, tenancy, **values)
                except TypeError as exc:
                    raise ValueError("The replacement bill fields are incomplete or invalid.") from exc
                conn.execute(update(m.charges).where(m.charges.c.id == new_id).values(replaces_charge_id=charge_id))
                if original["kind"] == "electricity":
                    # The replacement form may explicitly correct its reading.
                    reading = values["meter_reading_hundredths"]
                    self._carry_electricity_metadata(conn, original, new_id)
                    conn.execute(update(m.charges).where(m.charges.c.id == new_id).values(
                        meter_reading_hundredths=reading))
                result.append(new_id)
            self._audit(conn, "replace_cancelled_bill", "charge", charge_id,
                        original=original, replacement_charge_ids=result)
            return result

    def restore_cancelled_bill(self, charge_id, reason, effective_on=None):
        """Return a linked unpaid bill/draft, retaining the cancelled original.

        An automatic, unreviewed replacement may supply the new bill's ID.
        Earlier charge/payment history is never reactivated, and no available
        balance is consumed. The returned ID identifies the restored bill.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10_000:
            raise ValueError("Enter a reason for undoing the bill cancellation.")
        reason = reason.strip()
        requested = iso_date(effective_on) if effective_on is not None else None
        with self._write() as conn:
            original = self._get(conn, m.charges, charge_id, "Bill")
            tenancy = self._ensure_account_open(conn, original["tenancy_id"])
            if original["kind"] in {"rent", "electricity"} and not original["opening"] and tenancy.get("cancelled_on"):
                self._ensure_tenancy_not_cancelled(tenancy)
            self._ensure_cycle_unlocked(conn, original)
            if original.get("deleted_on"):
                raise ValueError("A deleted bill cannot be restored by undoing its cancellation.")
            if original["status"] != "void":
                raise ValueError("Choose a cancelled bill to undo its cancellation.")

            bills = [dict(row) for row in conn.execute(select(m.charges).where(
                m.charges.c.tenancy_id == tenancy["id"],
            )).mappings()]
            # Follow the linked bill history once, including repeated corrections.
            linked = _descendant_ids(bills, charge_id)
            descendants = [bill for bill in bills if bill["id"] in linked and bill["id"] != charge_id]
            if any(bill.get("deleted_on") for bill in descendants):
                raise ValueError("A replacement of this bill was deleted. Add a new bill if a new charge is needed.")

            required = original["kind"] in {"rent", "electricity"} and not original["opening"]
            cycle, following = cycle_bounds(original["period_start"], tenancy["billing_day"])
            cycle = cycle.isoformat()
            related = []
            for bill in bills:
                if bill["id"] == charge_id:
                    continue
                relevant = bill["id"] in linked
                if required and bill["kind"] == original["kind"] and not bill["opening"]:
                    relevant |= self._bill_cycle(bill, tenancy["billing_day"]) == cycle
                    if original["kind"] == "electricity":
                        relevant |= bill["period_start"] <= original["period_end"] and bill["period_end"] >= original["period_start"]
                if relevant:
                    related.append(bill)

            earliest = max(original["charge_date"], original["voided_on"])
            for bill in descendants:
                earliest = max(earliest, bill["charge_date"], bill["voided_on"] or bill["charge_date"])
            # A stored void bill may still have debt before its scheduled
            # cancellation, including a legacy unlinked electricity replacement.
            # Reissue only after every relevant cancellation has taken effect.
            for bill in related:
                if bill["status"] == "void":
                    earliest = max(earliest, bill["voided_on"] or bill["charge_date"])
            effective = requested or max(today_ist().isoformat(), earliest)
            if effective < earliest:
                raise ValueError("The restoration date must be on or after the bill's cancellation, later linked corrections and overlapping bill cancellations.")

            if required and (
                original["period_start"] < max(tenancy["move_in"], tenancy["opening_date"])
                or (tenancy["move_out"] and original["period_end"] > tenancy["move_out"])
            ):
                raise ValueError("This bill no longer fits the recorded stay. Review the current bill or change the move-out date first.")

            candidates = []
            for bill in related:
                if bill["status"] == "void":
                    continue
                self._ensure_cycle_unlocked(conn, bill)
                untouched = (
                    bill["id"] in linked
                    and bill["kind"] == original["kind"] and bill["opening"] == original["opening"]
                    and bill["status"] == "draft" and bill["posted_at"] is None
                    and not bill.get("deleted_on")
                    and bill["description"] in _REPLACEMENT_DRAFTS
                    and bill["period_start"] == original["period_start"]
                    and bill["period_end"] == original["period_end"]
                )
                if not untouched:
                    raise ValueError("An active replacement already covers this bill. Review or cancel that replacement before undoing the older cancellation.")
                candidates.append(bill)
            if len(candidates) > 1:
                raise ValueError("Several replacement drafts cover this bill. Review those drafts before undoing its cancellation.")
            if candidates and conn.execute(select(m.allocations.c.id).where(
                m.allocations.c.charge_id == candidates[0]["id"],
            ).limit(1)).first():
                raise ValueError("The replacement has payment history and cannot be reused as an untouched draft.")
            if candidates:
                # A restored draft can retain an automatic-replacement label if
                # its cancelled source was itself a replacement. Its audit entry
                # distinguishes it from a draft that has never been restored.
                previous_restores = conn.execute(select(m.audit.c.details).where(
                    m.audit.c.action == "undo_cancellation", m.audit.c.entity == "charge",
                    m.audit.c.entity_id.in_(linked),
                )).scalars()
                if any(json.loads(details).get("restored_charge_id") == candidates[0]["id"]
                       for details in previous_restores):
                    raise ValueError("This cancelled bill has already been restored. Review the restored draft instead.")

            was_posted = original["posted_at"] is not None
            if candidates:
                restored_id = candidates[0]["id"]
                conn.execute(update(m.charges).where(m.charges.c.id == restored_id).values(
                    amount_paise=original["amount_paise"], description=original["description"],
                    due_date=original["due_date"], charge_date=effective,
                    partial=original["partial"], status="posted" if was_posted else "draft",
                    posted_at=datetime.now(UTC).isoformat(timespec="microseconds") if was_posted else None,
                    replaces_charge_id=charge_id,
                ))
            else:
                restored_id = self._insert_charge(
                    conn, tenancy, original["kind"], original["amount_paise"],
                    original["period_start"], original["period_end"], original["due_date"],
                    original["description"], effective, draft=not was_posted,
                    opening=original["opening"],
                )
                conn.execute(update(m.charges).where(m.charges.c.id == restored_id).values(
                    replaces_charge_id=charge_id, partial=original["partial"],
                ))
                # Monthly electricity drafts need their anchor for subsequent
                # move-out adjustments. Multi-cycle legacy bills stay manual.
                if required and original["kind"] == "electricity" and original["period_end"] < following.isoformat():
                    conn.execute(update(m.charges).where(
                        m.charges.c.tenancy_id == tenancy["id"],
                        m.charges.c.electricity_cycle_start == cycle,
                        m.charges.c.status == "void",
                    ).values(electricity_cycle_start=None))
                    conn.execute(update(m.charges).where(m.charges.c.id == restored_id).values(
                        electricity_cycle_start=cycle,
                    ))
            self._carry_electricity_metadata(conn, original, restored_id)
            self._audit(conn, "undo_cancellation", "charge", charge_id,
                        reason=reason, effective_on=effective, restored_charge_id=restored_id,
                        original=original, reused_draft=candidates[0] if candidates else None)
            return restored_id
