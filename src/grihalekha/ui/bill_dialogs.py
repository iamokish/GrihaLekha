"""Payment receipts and full-bill actions against the tenant's balance."""

from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDialogButtonBox, QFileDialog, QLineEdit

from ..clock import today_ist
from ..meter import format_meter_reading
from .dialogs import combo, required
from .widgets import AmountInput, DateInput, FormDialog, MeterReadingInput, button, date_text, horizontal, set_rows, table


class BillPaymentDialog(FormDialog):
    def __init__(self, parent=None):
        super().__init__("Add payment", parent, width=600)
        self.amount = AmountInput(None, currency_code=self.currency_code)
        self.paid = DateInput(today_ist().isoformat())
        self.paid.setMaximumDate(QDate.fromString(today_ist().isoformat(), "yyyy-MM-dd"))
        self.method = combo([("UPI", "upi"), ("Bank transfer", "bank"), ("Cash", "cash")])
        self.form.addRow(f"Amount received ({self.currency_symbol})", self.amount)
        self.form.addRow("Payment date", self.paid)
        self.form.addRow("Payment method", self.method)
        self.reference = self.field("Reference (optional)")
        self.notes = self.field("Notes (optional)")
        self.proof = QLineEdit()
        self.proof.setReadOnly(True)
        self.proof.setPlaceholderText("Optional JPG, PNG or PDF")
        self.proof_row = horizontal(self.proof, button("Browse…", self.pick_proof), button("Clear", self.proof.clear))
        self.form.addRow("Payment proof", self.proof_row)
        self.method.currentIndexChanged.connect(self.update_method)
        self.update_method()
        self.add_hint("This payment adds to the tenant's available balance. Open a bill and mark it Paid to deduct its full amount.")

    def update_method(self):
        cash = self.method.currentData() == "cash"
        self.proof_row.setEnabled(not cash)
        if cash:
            self.proof.clear()

    def pick_proof(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach payment proof", "", "Payment proofs (*.png *.jpg *.jpeg *.pdf)")
        if path:
            self.proof.setText(path)

    def validate(self):
        amount = self.amount.paise(allow_zero=False)
        proof = self.proof.text().strip()
        if proof and not Path(proof).is_file():
            raise ValueError("The selected proof file no longer exists.")
        return dict(amount_paise=amount, paid_on=self.paid.iso(),
                    method=self.method.currentData(), reference=self.reference.text().strip(),
                    notes=self.notes.text().strip(), proof_path=proof or None)


class BillDialog(FormDialog):
    def __init__(self, charge, pay, unpaid, parent=None, *, archived=False, balance_paise=0,
                 undo_cancel=None, delete_bill=None, correct_amount=None,
                 edit_meter=None, undo_deferral=None, open_covering_bill=None):
        super().__init__(f"{charge['kind'].title()} bill", parent, width=730)
        self.add_hint(f"{date_text(charge['period_start'])} – {date_text(charge['period_end'])}\n"
                      f"Bill date: {date_text(charge['charge_date'])} · Due: {date_text(charge['due_date'])}")
        pending = charge["outstanding_paise"]
        future = charge["charge_date"] > today_ist().isoformat()
        deferred = charge.get("electricity_deferred", False)
        scheduled_cover = charge.get("electricity_scheduled_covering_charge_id")
        covered_by = charge.get("electricity_covered_by_charge_id") or scheduled_cover
        state = ("Cancelled" if charge["status"] == "void"
                 else f"Scheduled in electricity bill #{scheduled_cover}" if scheduled_cover
                 else f"Included in electricity bill #{covered_by}" if covered_by
                 else "Deferred · bill later" if deferred else "Future bill" if future
                 else "Paid" if pending == 0 else "Unpaid")
        if deferred and charge["status"] != "void":
            self.add_hint(state)
            self.add_hint("This cycle has no separate electricity amount or meter reading."
                          + (" Its electricity is included in the linked bill." if covered_by
                             else " Its electricity is carried forward for a later combined bill."))
        else:
            self.add_hint(f"{state}\nBill amount: {self.money(charge['amount_paise'])} · "
                          f"Amount paid: {self.money(charge['paid_paise'])} · Amount pending: {self.money(pending)}")
        if charge["kind"] == "electricity" and not deferred:
            reading = format_meter_reading(charge.get("meter_reading_hundredths"))
            self.add_hint("Electric meter reading: " + (f"{reading} kWh" if reading else "Not recorded"))
            coverage_start = charge.get("coverage_period_start") or charge["period_start"]
            if coverage_start != charge["period_start"]:
                self.add_hint(f"Electricity billed for: {date_text(coverage_start)} – "
                              f"{date_text(charge.get('coverage_period_end') or charge['period_end'])}")
        self.add_hint(f"Available balance: {self.money(balance_paise)}")
        if charge.get("description"):
            self.add_hint(charge["description"])
        if charge.get("void_reason"):
            scheduled = charge.get("voided_on") and charge["voided_on"] > today_ist().isoformat()
            heading = f"Cancellation scheduled for {date_text(charge['voided_on'])}" if scheduled else "Cancellation"
            self.add_hint(f"{heading}: {charge['void_reason']}")
        entries = table(["Payment date", "Amount", "Method", "Reference", "Status"])
        set_rows(entries, [(
            date_text(row["paid_on"]), self.money(row["amount_paise"]), row["method"].upper(),
            row["reference"], "Returned to balance / corrected" if row["cancelled_on"] else "Applied to bill",
        ) for row in charge.get("payments", [])])
        entries.setMinimumHeight(160)
        self.form.addRow(entries)
        locked = charge.get("payment_locked", False)
        editable = not archived and not locked and not future and not deferred and not charge.get("voided_on") and charge["status"] == "posted"
        self.paid_button = button("Mark Paid from Balance", lambda: self._act(pay), primary=True)
        self.unpaid_button = button("Mark Unpaid…", lambda: self._act(unpaid))
        self.paid_button.setEnabled(editable and pending > 0 and balance_paise >= pending)
        self.unpaid_button.setEnabled(editable and charge["paid_paise"] > 0)
        if locked:
            affected = charge.get("affected_locked_cycle_starts", [])
            self.add_hint(
                "Unlock the following billing cycles to change payment status: "
                + ", ".join(date_text(value) for value in affected) + "."
                if affected else "This cycle is locked. Unlock it in the bills table to change payment status."
            )
        elif editable and pending > balance_paise:
            self.add_hint(f"Add {self.money(pending - balance_paise)} to the balance before paying this bill. Bills must be paid in full.")
        elif editable and pending > 0:
            self.add_hint(f"Marking Paid deducts {self.money(pending)} from the balance. Marking Unpaid returns the applied amount to the balance.")
        actions = [self.paid_button, self.unpaid_button]
        if deferred and charge["status"] != "void":
            self.paid_button.hide()
            self.unpaid_button.hide()
            actions = []
            if covered_by and open_covering_bill:
                self.covering_bill_button = button("Open combined electricity bill", lambda: self._act(open_covering_bill), primary=True)
                actions.append(self.covering_bill_button)
            elif undo_deferral:
                self.undo_deferral_button = button("Undo electricity deferral", lambda: self._act(undo_deferral))
                self.undo_deferral_button.setEnabled(not archived and not locked)
                actions.append(self.undo_deferral_button)
        if charge.get("voided_on") and undo_cancel:
            self.paid_button.hide()
            self.unpaid_button.hide()
            self.restore_button = button("Undo cancellation…", lambda: self._act(undo_cancel), primary=True)
            self.restore_button.setEnabled(not archived and not locked and not charge.get("deleted_on"))
            actions = [self.restore_button]
        if charge["kind"] == "misc" and delete_bill and not charge.get("deleted_on"):
            self.delete_button = button("Delete bill…", lambda: self._act(delete_bill), danger=True)
            self.delete_button.setEnabled(not archived and not locked)
            actions.append(self.delete_button)
        if (charge["status"] == "posted"
                and not charge.get("voided_on") and not charge.get("deleted_on")):
            required_bill = charge["kind"] in {"rent", "electricity"}
            if required_bill:
                self.add_hint(f"Rent and electricity bills cannot be cancelled or deleted. Correct the amount when needed, including {self.currency_symbol}0.")
            elif charge["paid_paise"]:
                self.add_hint("Mark this bill Unpaid before correcting its amount. Its payment receipts stay recorded.")
            if correct_amount:
                self.correct_button = button("Correct amount…", lambda: self._act(correct_amount))
                self.correct_button.setEnabled(not archived and not locked and (required_bill or not charge["paid_paise"]))
                actions.append(self.correct_button)
        if charge["kind"] == "electricity" and edit_meter and not deferred and not charge.get("voided_on"):
            self.meter_button = button("Edit meter reading…", lambda: self._act(edit_meter))
            self.meter_button.setEnabled(not archived and not charge.get("deleted_on"))
            self.form.addRow(horizontal(self.meter_button, stretch=True))
        self.form.addRow(horizontal(*actions, stretch=True))
        self.buttons.hide()
        self.root.addWidget(button("Close", self.accept))

    def _act(self, callback):
        self.accept()
        callback()


class MeterReadingDialog(FormDialog):
    """Edit electricity metadata without posting or changing its payment."""

    def __init__(self, charge, parent=None):
        super().__init__("Edit electric meter reading", parent, width=600)
        self.add_hint(f"{date_text(charge['period_start'])} – {date_text(charge['period_end'])}")
        self.meter_reading = MeterReadingInput(charge.get("meter_reading_hundredths"))
        self.form.addRow("Electric meter reading (kWh, optional)", self.meter_reading)
        self.add_hint("Enter up to two decimal places, or leave blank to clear the reading. Saving changes only the reading; the bill amount and payments stay as recorded.")

    def validate(self):
        return {"meter_reading_hundredths": self.meter_reading.reading()}


class UnpaidDialog(FormDialog):
    def __init__(self, charge, parent=None):
        super().__init__("Mark bill Unpaid", parent)
        self.add_hint(f"Reopen {charge['kind']} for {date_text(charge['period_start'])} – "
                      f"{date_text(charge['period_end'])}. Return {self.money(charge['paid_paise'])} to the tenant's available balance.")
        self.reason = self.field("Reason")
        self.add_hint("The original payment receipts stay recorded. This action keeps a dated history and does not refund money.")

    def validate(self):
        return dict(reason=required(self.reason, "Reason"))


class BillAmountDialog(FormDialog):
    """Review a posted bill's new amount while retaining its dated history."""

    def __init__(self, charge, parent=None):
        super().__init__(f"Correct {charge['kind']} amount", parent, width=620)
        self.add_hint(f"{date_text(charge['period_start'])} – {date_text(charge['period_end'])}\n"
                      f"Current bill amount: {self.money(charge['amount_paise'])}")
        self.amount = AmountInput(charge["amount_paise"], currency_code=self.currency_code)
        self.form.addRow(f"Correct amount ({self.currency_symbol})", self.amount)
        self.reason = self.field("Reason")
        self.add_hint(f"Enter {self.currency_symbol}0 if nothing is owed. The bill stays recorded for this cycle.")
        if charge.get("paid_paise", 0):
            self.add_hint(f"Changing the amount returns {self.money(charge['paid_paise'])} to the tenant's available balance. "
                          "Pay the corrected bill separately from that balance when ready.")
        self.add_hint(f"The original amount and payment history are retained. The corrected bill is Unpaid unless its amount is {self.currency_symbol}0.")
        effective = max(today_ist().isoformat(), charge["charge_date"])
        self.add_hint(f"Correction date: {date_text(effective)}")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save corrected amount")

    def validate(self):
        return dict(amount_paise=self.amount.paise(), reason=required(self.reason, "Reason"))


class BillCorrectionDialog(FormDialog):
    """Review one bill correction and collect its audit reason."""

    def __init__(self, charge, action, parent=None):
        deleting = action == "delete"
        super().__init__("Delete miscellaneous bill" if deleting else "Undo bill cancellation", parent, width=620)
        self.add_hint(f"{charge['kind'].title()} · {date_text(charge['period_start'])} – "
                      f"{date_text(charge['period_end'])}\nAmount: {self.money(charge['amount_paise'])}")
        if charge.get("description"):
            self.add_hint(charge["description"])
        if deleting:
            self.add_hint("Remove this bill from the bills table and newly generated statements. Its internal history and existing payment receipts will be retained.")
            if charge.get("paid_paise", 0):
                self.add_hint(f"Return {self.money(charge['paid_paise'])} to the tenant's available balance.")
        elif not charge.get("posted_at"):
            self.add_hint("Restore the original draft for review. An unused automatic replacement draft will be reused when available.")
        else:
            self.add_hint("Restore the original bill amount as Unpaid. Pay it separately from the tenant's balance when ready. A zero-value bill needs no payment.")
            self.add_hint("The bill keeps its original service period. A replacement bill that has already been posted must be corrected first.")
        if not deleting and max(charge["charge_date"], charge.get("voided_on") or "") > today_ist().isoformat():
            self.add_hint("This bill has a future schedule. Its restoration takes effect on or after the scheduled cancellation date.")
        self.reason = self.field("Reason")
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Delete bill" if deleting else "Undo cancellation")
        if deleting:
            save.setObjectName("danger")

    def validate(self):
        return {"reason": required(self.reason, "Reason")}
