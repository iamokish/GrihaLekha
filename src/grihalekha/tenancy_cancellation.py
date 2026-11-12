"""Cancel a never-started reservation while retaining its settlement account."""

from sqlalchemy import select, update

from . import models as m
from .clock import today_ist


def _revision(conn):
    return int(conn.execute(select(m.settings.c.value).where(
        m.settings.c.key == "revision",
    )).scalar_one())


class TenancyCancellationMixin:
    @staticmethod
    def _ensure_tenancy_not_cancelled(tenancy):
        if tenancy.get("cancelled_on"):
            raise ValueError(
                "This upcoming tenancy was cancelled. Its account remains available "
                "for settlement; create a new tenancy for a new reservation."
            )
        return tenancy

    def _upcoming_cancellation_plan(self, conn, tenancy):
        self._ensure_tenancy_not_cancelled(tenancy)
        today = today_ist().isoformat()
        history = self._occupancy_history(conn, tenancy["id"])
        if (tenancy["move_in"] <= today or not history
                or any(row["starts_on"] <= today for row in history)):
            raise ValueError(
                "Only an upcoming tenancy whose move-in date is after today can be cancelled. "
                "Record a move-out for a tenant whose stay has already started."
            )
        if any(row["paired_tenancy_id"] for row in history):
            raise ValueError(
                "Cancel the linked planned flat exchange before cancelling this upcoming tenant."
            )

        bills = [dict(row) for row in conn.execute(
            select(m.charges).where(m.charges.c.tenancy_id == tenancy["id"])
            .order_by(m.charges.c.id)
        ).mappings()]
        stopped = [row for row in bills if not row["opening"] and not row.get("deleted_on")
                   and row["kind"] in {"rent", "electricity"}
                   and (row["status"] != "void"
                        or row["voided_on"] > max(today, row["charge_date"]))]
        # A future allocation/correction cannot be pulled into the past. The
        # existing lifecycle validator rejects that case before any write and
        # identifies legacy scheduled cancellations that can safely move earlier.
        advanced = self._scheduled_move_corrections(conn, stopped)
        changed_cycles = {self._bill_cycle(row, tenancy["billing_day"]) for row in stopped}
        stopped_ids = {row["id"] for row in stopped}
        newly_released = self._released_move_credit(conn, stopped_ids)
        returned = newly_released + sum(row["returned_paise"] for row in advanced)
        current_balance = self._balance(conn, tenancy["id"], today)
        future_balance = self._balance(conn, tenancy["id"], "9999-12-31")
        summary = {
            "revision": _revision(conn),
            "tenancy_id": tenancy["id"],
            "tenant_name": tenancy["tenant_name"],
            "planned_move_in": tenancy["move_in"],
            "cancelled_on": today,
            "released_flat_ids": sorted({row["flat_id"] for row in history}),
            "cancelled_bill_ids": sorted(stopped_ids),
            "draft_bill_ids": [row["id"] for row in stopped if row["posted_at"] is None],
            "posted_bill_ids": [row["id"] for row in stopped if row["posted_at"] is not None],
            "remaining_bill_ids": [row["id"] for row in bills
                                   if row["id"] not in stopped_ids and row["status"] != "void"
                                   and not row.get("deleted_on")],
            "returned_paise": returned,
            "locked_cycle_starts": self._move_locks(conn, tenancy["id"], changed_cycles),
            "credit_paise": current_balance["credit_paise"],
            "deposit_paise": current_balance["deposit_paise"],
            # Moving an already-scheduled release earlier does not add to the
            # account's eventual balance a second time.
            "future_credit_paise": future_balance["credit_paise"] + newly_released,
            "future_deposit_paise": future_balance["deposit_paise"],
            "advanced_cancellations": advanced,
        }
        return history, stopped, summary

    def preview_cancel_upcoming_tenancy(self, tenancy_id):
        """Describe affected reservations and bills without modifying records."""
        with self._read() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            return self._upcoming_cancellation_plan(conn, tenancy)[-1]

    def cancel_upcoming_tenancy(self, tenancy_id, reason, *, expected_revision=None):
        """Release the reservation, void required service bills, and retain money.

        Miscellaneous fees and opening dues remain explicit settlement items.
        Original receipts, deposits, occupancy records, and issued documents are
        preserved. A new tenancy is required if the tenant books again later.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10_000:
            raise ValueError("Enter a cancellation reason of at most 10,000 characters.")
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("Review a valid cancellation preview before continuing.")
        reason = reason.strip()
        with self._write() as conn:
            if expected_revision is not None and _revision(conn) != expected_revision:
                raise ValueError(
                    "Account records changed after this cancellation preview. "
                    "Review a fresh preview before cancelling the upcoming tenant."
                )
            tenancy = self._ensure_account_open(conn, tenancy_id)
            history, stopped, summary = self._upcoming_cancellation_plan(conn, tenancy)
            self._check_move_locks(summary)
            self._advance_move_cancellations(conn, summary["advanced_cancellations"], reason)
            for bill in stopped:
                if bill["status"] != "void":
                    self._void_charge(conn, bill["id"], reason,
                                      max(summary["cancelled_on"], bill["charge_date"]))
            # Set the marker after service-bill corrections. Their existing
            # helpers still operate on the active account in this transaction.
            conn.execute(update(m.tenancies).where(m.tenancies.c.id == tenancy_id).values(
                cancelled_on=summary["cancelled_on"], cancellation_reason=reason,
            ))
            self._audit(conn, "cancel_upcoming_tenancy", "tenancy", tenancy_id,
                        reason=reason, previous_tenancy=tenancy,
                        cancelled_reservations=history, **summary)
            return summary
