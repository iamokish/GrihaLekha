"""Move existing receipt credit into deposits without inventing a cash movement."""

from sqlalchemy import func, insert, select

from . import models as m
from .clock import today_ist
from .dates import iso_date
from .money import validate_paise


class DepositTransferMixin:
    def _payment_deposit_total(self, conn, payment_id, cutoff):
        from .ledger import _active

        return conn.execute(
            select(func.coalesce(func.sum(m.deposit_sources.c.amount_paise), 0))
            .select_from(m.deposit_sources.join(m.deposits))
            .where(
                m.deposit_sources.c.payment_id == payment_id,
                m.deposits.c.paid_on <= cutoff,
                _active(m.deposits.c.reversed_on, cutoff),
            )
        ).scalar_one()

    def _payment_deposit_dates(self, conn, payment_id, effective):
        rows = conn.execute(
            select(m.deposits.c.paid_on, m.deposits.c.reversed_on)
            .select_from(m.deposit_sources.join(m.deposits))
            .where(m.deposit_sources.c.payment_id == payment_id)
        )
        return {value for row in rows for value in row if value and value >= effective}

    def _ensure_payment_deposit_reversal(self, conn, payment_id, effective):
        """A receipt must outlive every transfer it funded, including corrections."""
        corrections = conn.execute(
            select(m.deposits.c.reversed_on)
            .select_from(m.deposit_sources.join(m.deposits))
            .where(m.deposit_sources.c.payment_id == payment_id)
        ).scalars().all()
        if any(value is None for value in corrections):
            raise ValueError(
                "Some of this payment was transferred to the security deposit. "
                "Correct that deposit transfer before reversing the payment."
            )
        if any(value > effective for value in corrections):
            raise ValueError(
                "The reversal date must be on or after related security deposit transfer corrections."
            )

    def _balance_deposit_total(self, conn, tenancy_id, cutoff):
        from .ledger import _active

        return conn.execute(
            select(func.coalesce(func.sum(m.deposit_sources.c.amount_paise), 0))
            .select_from(m.deposit_sources.join(m.deposits))
            .where(
                m.deposits.c.tenancy_id == tenancy_id,
                m.deposits.c.paid_on <= cutoff,
                _active(m.deposits.c.reversed_on, cutoff),
            )
        ).scalar_one()

    @staticmethod
    def _balance_deposit_adjustments(deposits, start, end):
        """Internal transfers change account credit, not cash payment/refund totals."""
        result = []
        for deposit in deposits:
            if deposit["kind"] != "receipt" or deposit["method"] != "internal":
                continue
            for date_key, reason_key, kind, sign in (
                ("paid_on", "notes", "balance_to_deposit", 1),
                ("reversed_on", "reverse_reason", "balance_to_deposit_reversed", -1),
            ):
                effective = deposit[date_key]
                if effective and start <= effective <= end:
                    result.append({
                        "kind": kind,
                        "effective_on": effective,
                        "date": effective,
                        "amount_paise": sign * deposit["amount_paise"],
                        "reason": deposit[reason_key],
                        "deposit_id": deposit["id"],
                    })
        return result

    def transfer_balance_to_deposit(self, tenancy_id, amount_paise, paid_on, notes):
        """Return the deposit movement ID for a dated, reversible internal transfer.

        Original receipt rows stay intact. Source links reserve only money that
        remains unspent at every later accounting boundary, so a backdated
        transfer cannot invalidate an existing bill payment or refund.
        """
        from .ledger import _now, _text

        validate_paise(amount_paise, allow_zero=False)
        paid = iso_date(paid_on)
        notes = _text(notes, "Transfer reason", True)
        if paid > today_ist().isoformat():
            raise ValueError("A balance-to-deposit transfer cannot have a future date.")
        with self._write() as conn:
            tenancy = self._ensure_account_open(conn, tenancy_id)
            if tenancy.get("cancelled_on"):
                raise ValueError("A cancelled tenancy cannot receive a new balance-to-deposit transfer.")
            if paid < tenancy["opening_date"]:
                raise ValueError("The transfer date cannot precede the account opening date.")

            sources = []
            remaining = amount_paise
            for payment in self._open_payments(conn, tenancy_id):
                if payment["paid_on"] > paid:
                    continue
                available = self._available_since(conn, payment, paid)
                if available > 0:
                    amount = min(remaining, available)
                    sources.append({"payment_id": payment["id"], "amount_paise": amount})
                    remaining -= amount
                if remaining == 0:
                    break
            if remaining:
                raise ValueError(
                    "There is not enough available balance on that date after protecting later payments, refunds, and transfers."
                )

            deposit_id = conn.execute(insert(m.deposits).values(
                tenancy_id=tenancy_id, kind="receipt", amount_paise=amount_paise,
                paid_on=paid, method="internal", notes=notes, created_at=_now(),
            )).inserted_primary_key[0]
            conn.execute(insert(m.deposit_sources), [
                {"deposit_id": deposit_id, **source} for source in sources
            ])
            self._audit(
                conn, "balance_to_deposit", "deposit", deposit_id,
                tenancy_id=tenancy_id, amount_paise=amount_paise, paid_on=paid,
                reason=notes, sources=sources,
            )
            return deposit_id
