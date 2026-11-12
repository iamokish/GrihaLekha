"""Input dialogs validate all fields before any financial write is requested."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTableWidgetItem,
)

from ..clock import today_ist
from ..dates import cycle_bounds
from .widgets import (
    AmountInput,
    DateInput,
    FormDialog,
    MeterReadingInput,
    button,
    date_text,
    horizontal,
    label,
    table,
)


def required(field: QLineEdit, title: str) -> str:
    value = field.text().strip()
    if not value:
        raise ValueError(f"{title} is required.")
    return value


def combo(items: list[tuple[str, object]]) -> QComboBox:
    result = QComboBox()
    for text, value in items:
        result.addItem(text, value)
    return result


def flat_label(flat: dict) -> str:
    building = flat.get("building_name", flat.get("building", ""))
    number = flat.get("number", flat.get("flat_number", ""))
    return f"{building} · Flat {number}" if building else f"Flat {number}"


class BuildingDialog(FormDialog):
    def __init__(self, parent=None):
        super().__init__("Add a building", parent)
        self.name = self.field("Building name", placeholder="e.g. Krishna Nivas")
        self.address = QPlainTextEdit()
        self.address.setPlaceholderText("Street, locality, city and PIN code")
        self.address.setMaximumHeight(110)
        self.form.addRow("Full address", self.address)

    def validate(self):
        address = self.address.toPlainText().strip()
        if not address:
            raise ValueError("The building address is required.")
        return {"name": required(self.name, "Building name"), "address": address}


class BackupImportDialog(FormDialog):
    """Review a verified archive before replacing the current workspace."""

    def __init__(self, path: Path, manifest: dict, parent=None):
        super().__init__("Import from backup", parent, width=610)
        self.form.addRow("Backup file", label(path.name, wrap=True))
        self.form.addRow("Location", label(str(path.parent), "muted", True))
        try:
            created = datetime.fromisoformat(manifest["created_at"]).astimezone().strftime("%d %b %Y, %H:%M %Z")
        except (KeyError, TypeError, ValueError):
            created = "Unknown"
        self.form.addRow("Created", label(created))
        files = manifest["files"]
        proofs = sum(name.startswith("attachments/") for name in files)
        documents = sum(name.startswith("documents/") for name in files)
        self.form.addRow("Contents", label(f"All accounts · {proofs} payment proofs · {documents} issued documents", wrap=True))
        self.form.addRow("Unpacked size", label(f"{sum(info['size'] for info in files.values()) / 1024**2:.2f} MB"))
        self.add_hint("The backup has been verified. Import replaces all current buildings, flats, tenant accounts, proofs and issued documents. It does not merge accounts.")
        self.add_hint("A safety copy of your current data will be kept before anything is replaced. Healthy account data is saved in Managed restore points so you can undo this import.")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Import and replace")


class FlatDialog(FormDialog):
    def __init__(self, buildings: list[dict], parent=None):
        super().__init__("Add a flat", parent)
        self.building = combo([(b["name"], b["id"]) for b in buildings])
        self.form.addRow("Building", self.building)
        self.number = self.field("Flat number", placeholder="e.g. 201 or A-3")
        self.floor = self.field("Floor", placeholder="e.g. 2 or Ground")
        self.bhk = combo([(f"{i} BHK", i) for i in range(1, 5)])
        self.form.addRow("Flat type", self.bhk)
        self.add_hint(
            "You can keep adding buildings and flats as your portfolio grows."
        )

    def validate(self):
        if self.building.currentData() is None:
            raise ValueError("Add a building before adding a flat.")
        return {
            "building_id": self.building.currentData(),
            "number": required(self.number, "Flat number"),
            "floor": required(self.floor, "Floor"),
            "bhk": self.bhk.currentData(),
        }


class TenancyDialog(FormDialog):
    def __init__(self, flats: list[dict], parent=None):
        super().__init__("Add a tenant household", parent, width=660)
        self.flat = combo([(flat_label(f), f["id"]) for f in flats])
        self.form.addRow("Flat", self.flat)
        self.name = self.field("Primary tenant name", placeholder="English or Hindi")
        self.phone = self.field("Phone (optional)")
        self.move_in = DateInput()
        self.form.addRow("Move-in date", self.move_in)
        self.initial_meter_reading = MeterReadingInput()
        self.form.addRow("Initial electric meter reading (kWh, optional)", self.initial_meter_reading)
        self.existing = QCheckBox("Existing tenant — start with opening balances")
        self.account_start = DateInput()
        self.account_start.setEnabled(False)
        self.existing.toggled.connect(self.account_start.setEnabled)
        self.form.addRow(self.existing)
        self.form.addRow("Opening / account start date", self.account_start)
        self.rent = AmountInput(None, currency_code=self.currency_code)
        self.form.addRow(f"Monthly rent ({self.currency_symbol})", self.rent)
        self.billing = QSpinBox()
        self.billing.setRange(1, 31)
        self.billing.setValue(today_ist().day)
        self.grace = QSpinBox()
        self.grace.setRange(0, 365)
        self.grace.setSuffix(" days")
        self.form.addRow("Cycle starts on day", self.billing)
        self.form.addRow("Grace after due date", self.grace)
        self.billing_hint = self.add_hint(
            "Rent is due on the last day of each cycle. Partial months require your chosen amount."
        )
        self.members = table(["Family member name", "Relationship (optional)"], editable=True)
        self.members.setMinimumHeight(135)
        self.members.setMaximumHeight(180)
        self.form.addRow("Family (optional)", self.members)
        self.form.addRow(
            "",
            horizontal(
                button("+ Family member", self.add_member),
                button("Remove selected", self.remove_member),
                stretch=True,
            ),
        )

    def add_member(self):
        row = self.members.rowCount()
        self.members.insertRow(row)
        self.members.setItem(row, 0, QTableWidgetItem(""))
        self.members.setItem(row, 1, QTableWidgetItem(""))
        self.members.setCurrentCell(row, 0)
        self.members.editItem(self.members.item(row, 0))

    def remove_member(self):
        row = self.members.currentRow()
        if row >= 0:
            self.members.removeRow(row)

    def validate(self):
        if self.flat.currentData() is None:
            raise ValueError("Add a vacant flat before creating a tenancy.")
        members = []
        for row in range(self.members.rowCount()):
            name = (
                self.members.item(row, 0).text().strip()
                if self.members.item(row, 0)
                else ""
            )
            relationship = (
                self.members.item(row, 1).text().strip()
                if self.members.item(row, 1)
                else ""
            )
            if not name and not relationship:
                continue
            if not name:
                raise ValueError("Enter a name for each family member.")
            members.append({"name": name, "relationship": relationship})
        return {
            "flat_id": self.flat.currentData(),
            "tenant_name": required(self.name, "Tenant name"),
            "phone": self.phone.text().strip(),
            "move_in": self.move_in.iso(),
            "initial_meter_reading_hundredths": self.initial_meter_reading.reading(),
            "rent_paise": self.rent.paise(),
            "billing_day": self.billing.value(),
            "grace_days": self.grace.value(),
            "members": members,
            "opening_date": self.account_start.iso()
            if self.existing.isChecked()
            else None,
        }


class HouseholdDialog(TenancyDialog):
    """Edit contact/family details without exposing unrelated tenancy settings."""

    def __init__(self, tenancy: dict, parent=None):
        super().__init__([tenancy | {"id": tenancy["flat_id"]}], parent)
        self.setWindowTitle("Edit household details")
        self.heading.setText("Edit household details")
        self.name.setText(tenancy["tenant_name"])
        self.phone.setText(tenancy.get("phone", ""))
        self.move_in.set_iso(tenancy["move_in"])
        self.initial_meter_reading.set_reading(tenancy.get("initial_meter_reading_hundredths"))
        self.rent.set_paise(tenancy["rent_paise"])
        self.billing.setValue(tenancy["billing_day"])
        self.grace.setValue(tenancy["grace_days"])
        for field in (
            self.flat,
            self.move_in,
            self.existing,
            self.account_start,
            self.rent,
            self.billing,
            self.billing_hint,
        ):
            self.form.setRowVisible(field, False)
        for member in tenancy.get("members", []):
            self.add_member()
            row = self.members.rowCount() - 1
            self.members.item(row, 0).setText(member["name"])
            self.members.item(row, 1).setText(member.get("relationship", ""))

    def validate(self):
        values = super().validate()
        return {
            key: values[key]
            for key in ("tenant_name", "phone", "members", "grace_days", "initial_meter_reading_hundredths")
        }


class ChargeDialog(FormDialog):
    def __init__(self, parent=None, start: str | None = None, end: str | None = None,
                 *, replacement_kind: str | None = None):
        super().__init__("Add charges", parent, width=720)
        self.replacement_kind = replacement_kind
        self.start = DateInput(start)
        self.end = DateInput(end)
        self.due = DateInput(end)
        self.charged = DateInput()
        self.form.addRow("Period starts", self.start)
        self.form.addRow("Period ends (included)", self.end)
        self.form.addRow("Charge date", self.charged)
        self.form.addRow("Payment due date", self.due)
        self.items = table(["Charge type", f"Amount ({self.currency_symbol})", "Reason / description"])
        self.items.setColumnWidth(0, 185)
        self.items.setColumnWidth(1, 140)
        self.items.setMinimumHeight(145)
        self.form.addRow(self.items)
        self.form.addRow(
            horizontal(
                button("+ Another charge", self.add_item),
                button("Remove selected", self.remove_item),
                stretch=True,
            )
        )
        self.add_hint(
            "Rent and electricity drafts are prepared monthly. Use this form for additional charges or corrections. Every miscellaneous fee needs a reason."
        )
        self.add_hint("After saving an electricity bill, open it to add or edit its optional meter reading.")
        self.add_item()

    def add_item(self):
        row = self.items.rowCount()
        self.items.insertRow(row)
        self.items.setRowHeight(row, max(45, self.items.fontMetrics().height() + 22))
        self.items.setCellWidget(
            row,
            0,
            combo(
                [
                    ("Electricity", "electricity"),
                    ("Miscellaneous", "misc"),
                    ("Rent replacement", "rent"),
                ]
            ),
        )
        self.items.setCellWidget(row, 1, AmountInput(None, currency_code=self.currency_code))
        if self.replacement_kind:
            kind = self.items.cellWidget(row, 0)
            kind.setCurrentIndex(kind.findData(self.replacement_kind))
            kind.setEnabled(False)
        reason = QLineEdit()
        reason.setPlaceholderText("e.g. lock repair / ताले की मरम्मत")
        self.items.setCellWidget(row, 2, reason)

    def remove_item(self):
        row = self.items.currentRow()
        if row >= 0:
            self.items.removeRow(row)

    def validate(self):
        if self.end.iso() < self.start.iso():
            raise ValueError("The period end must be on or after its start.")
        if not self.items.rowCount():
            raise ValueError("Add at least one charge.")
        items = []
        for row in range(self.items.rowCount()):
            kind = self.items.cellWidget(row, 0).currentData()
            reason = self.items.cellWidget(row, 2).text().strip()
            if kind == "misc" and not reason:
                raise ValueError(
                    f"A reason is required for miscellaneous fee {row + 1}."
                )
            if kind == "rent" and not reason:
                raise ValueError("Enter a reason for the manual rent replacement.")
            items.append(
                {
                    "kind": kind,
                    "amount_paise": self.items.cellWidget(row, 1).paise(),
                    "description": reason,
                    "period_start": self.start.iso(),
                    "period_end": self.end.iso(),
                    "due_date": self.due.iso(),
                    "charge_date": self.charged.iso(),
                }
            )
        return items


class PostDraftDialog(FormDialog):
    def __init__(self, charge: dict, parent=None):
        super().__init__(f"Review {charge['kind']} draft", parent)
        self.add_hint(
            f"{date_text(charge['period_start'])} – {date_text(charge['period_end'])}\nDue {date_text(charge['due_date'])}"
        )
        self.amount = AmountInput(charge.get("amount_paise"), currency_code=self.currency_code)
        self.form.addRow(f"Bill amount for this period ({self.currency_symbol})", self.amount)
        if charge["kind"] == "electricity":
            self.meter_reading = MeterReadingInput(charge.get("meter_reading_hundredths"))
            self.form.addRow("Electric meter reading (kWh, optional)", self.meter_reading)
            coverage_start = charge.get("coverage_period_start") or charge["period_start"]
            coverage_end = charge.get("coverage_period_end") or charge["period_end"]
            if coverage_start != charge["period_start"]:
                self.add_hint(f"Includes deferred electricity: {date_text(coverage_start)} – {date_text(coverage_end)}. Enter the combined bill amount and latest meter reading, if known.")
            if not charge.get("opening") and charge.get("electricity_cycle_start"):
                self.defer_electricity = QCheckBox("Skip electricity billing this cycle — bill it later")
                self.form.addRow(self.defer_electricity)
                self.defer_hint = self.add_hint("Skipping clears any entered draft amount and meter reading. This cycle will have no separate electricity bill. Its electricity is carried forward for a later combined bill.")
                self._deferred_values = None
                self.defer_electricity.toggled.connect(self._defer_changed)
        self.amount_hint = None
        if charge.get("amount_paise") is None or charge.get("needs_amount"):
            self.amount_hint = self.add_hint(
                "Enter this month's electricity bill amount." if charge["kind"] == "electricity"
                else "This is a partial month. Enter the amount you agreed with the tenant."
            )
        self.post_hint = self.add_hint(f"Posting confirms the bill amount. Mark it Paid afterwards to use the available balance. A reviewed electricity bill can be {self.currency_symbol}0.")
        if hasattr(self, "defer_electricity"):
            self._defer_changed(False)

    def validate(self):
        if hasattr(self, "defer_electricity") and self.defer_electricity.isChecked():
            return {"defer_electricity": True}
        values = {
            "amount_paise": self.amount.paise(),
            "apply_credit": False,
        }
        if hasattr(self, "meter_reading"):
            values["meter_reading_hundredths"] = self.meter_reading.reading()
        return values

    def _defer_changed(self, deferred):
        if deferred:
            self._deferred_values = (self.amount.text(), self.meter_reading.text())
            self.amount.clear()
            self.meter_reading.clear()
        elif self._deferred_values is not None:
            self.amount.setText(self._deferred_values[0])
            self.meter_reading.setText(self._deferred_values[1])
            self._deferred_values = None
        self.amount.setEnabled(not deferred)
        self.meter_reading.setEnabled(not deferred)
        self.defer_hint.setVisible(deferred)
        if self.amount_hint is not None:
            self.amount_hint.setVisible(not deferred)
        self.post_hint.setVisible(not deferred)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Skip this cycle" if deferred else "Save")



class AmountActionDialog(FormDialog):
    def __init__(
        self,
        title: str,
        parent=None,
        description: str = "",
        date_label: str = "Date",
        reason_required: bool = False,
    ):
        super().__init__(title, parent)
        self.reason_required = reason_required
        if description:
            self.add_hint(description)
        self.amount = AmountInput(None, currency_code=self.currency_code)
        self.when = DateInput()
        self.when.setMaximumDate(QDate.fromString(today_ist().isoformat(), "yyyy-MM-dd"))
        self.method = combo(
            [("Cash", "cash"), ("Bank transfer", "bank"), ("UPI", "upi")]
        )
        self.form.addRow(f"Amount ({self.currency_symbol})", self.amount)
        self.form.addRow(date_label, self.when)
        self.form.addRow("Method", self.method)
        self.reason = self.field("Reason" if reason_required else "Notes (optional)")

    def validate(self):
        reason = self.reason.text().strip()
        if self.reason_required and not reason:
            raise ValueError("Please enter the reason.")
        return {
            "amount_paise": self.amount.paise(allow_zero=False),
            "date": self.when.iso(),
            "method": self.method.currentData(),
            "reason": reason,
        }


class OpeningDialog(FormDialog):
    def __init__(self, parent=None):
        super().__init__("Set up opening balances", parent)
        self.add_hint(
            "Enter balances carried into GrihaLekha on the opening date. These are not new receipts. Do not also enter the same historic transactions."
        )
        self.when = DateInput()
        self.form.addRow("Opening date", self.when)
        self.rent = AmountInput(currency_code=self.currency_code)
        self.electricity = AmountInput(currency_code=self.currency_code)
        self.misc = AmountInput(currency_code=self.currency_code)
        self.advance = AmountInput(currency_code=self.currency_code)
        self.deposit = AmountInput(currency_code=self.currency_code)
        self.form.addRow(f"Old unpaid rent ({self.currency_symbol})", self.rent)
        self.form.addRow(f"Old electricity due ({self.currency_symbol})", self.electricity)
        self.form.addRow(f"Other old fees due ({self.currency_symbol})", self.misc)
        self.reason = self.field("Reason for other fees")
        self.form.addRow(f"Advance already paid ({self.currency_symbol})", self.advance)
        self.form.addRow(f"Deposit already held ({self.currency_symbol})", self.deposit)

    def validate(self):
        misc = self.misc.paise()
        reason = self.reason.text().strip()
        if misc and not reason:
            raise ValueError("Enter a reason for the old miscellaneous fees.")
        return {
            "opening_date": self.when.iso(),
            "rent_paise": self.rent.paise(),
            "electricity_paise": self.electricity.paise(),
            "misc_paise": misc,
            "misc_reason": reason,
            "advance_paise": self.advance.paise(),
            "deposit_paise": self.deposit.paise(),
        }


class RateDialog(FormDialog):
    def __init__(self, parent=None):
        super().__init__("Change the agreed rent", parent)
        self.amount = AmountInput(None, currency_code=self.currency_code)
        self.effective = DateInput()
        self.form.addRow(f"New monthly rent ({self.currency_symbol})", self.amount)
        self.form.addRow("Effective from", self.effective)
        self.add_hint(
            "Choose a future cycle boundary. Existing posted bills keep their original amounts."
        )

    def validate(self):
        return {
            "rent_paise": self.amount.paise(),
            "effective_date": self.effective.iso(),
        }


class MoveOutDialog(FormDialog):
    def __init__(self, parent=None, tenancy=None):
        previous = tenancy.get("move_out") if tenancy else None
        super().__init__("Change move-out date" if previous else "Record move-out", parent)
        self.tenancy = tenancy
        self.when = DateInput(previous or today_ist().isoformat())
        self.cycle = None
        if tenancy:
            # An account may be opened after the tenant actually departed.
            # The recorded stay, not the bookkeeping start, limits departure.
            earliest = max([tenancy["move_in"],
                            *[row["starts_on"] for row in tenancy.get("occupancy_history", [])]])
            self.when.setMinimumDate(QDate.fromString(earliest, "yyyy-MM-dd"))
            self.cycle = combo([])
            self.cycle.setAccessibleName("Moved out cycle")
            self.form.addRow("Moved out cycle", self.cycle)
            self._cycle_through = max(today_ist().isoformat(), self.when.iso())
            self._populate_cycles()
            self.cycle.currentIndexChanged.connect(self._cycle_changed)
            self.when.dateChanged.connect(self._day_changed)
        self.rent = AmountInput(None, currency_code=self.currency_code)
        self._rent_before_opening = None
        self.form.addRow("Last occupied day", self.when)
        self.form.addRow(f"Final period rent ({self.currency_symbol})", self.rent)
        self.reason = self.field("Settlement notes", placeholder="Optional")
        if previous:
            self.add_hint(f"Recorded last occupied day: {date_text(previous)}. Choose an earlier or later date to shorten or extend the stay.")
        self.cycle_hint = self.add_hint("")
        self.when.dateChanged.connect(self._update_hint)
        self._update_hint()
        self.add_hint(
            "Choose the final occupied cycle, then the exact last occupied day. "
            f"Enter the agreed final rent, including {self.currency_symbol}0 when nothing is owed. "
            "Earlier dues remain available for payments received months after departure."
        )

    def _populate_cycles(self):
        selected, _ = cycle_bounds(self.when.iso(), self.tenancy["billing_day"])
        first = self.when.minimumDate().toString("yyyy-MM-dd")
        start, following = cycle_bounds(first, self.tenancy["billing_day"])
        self.cycle.blockSignals(True)
        try:
            self.cycle.clear()
            while start.isoformat() <= self._cycle_through:
                self.cycle.addItem(
                    f"{date_text(start)} – {date_text(following - timedelta(days=1))}",
                    start.isoformat(),
                )
                start, following = cycle_bounds(following, self.tenancy["billing_day"])
            self.cycle.setCurrentIndex(self.cycle.findData(selected.isoformat()))
        finally:
            self.cycle.blockSignals(False)

    def _cycle_changed(self, *args):
        selected = self.cycle.currentData()
        if selected is None:
            return
        _, following = cycle_bounds(selected, self.tenancy["billing_day"])
        self.when.set_iso((following - timedelta(days=1)).isoformat())

    def _day_changed(self, *args):
        selected, _ = cycle_bounds(self.when.iso(), self.tenancy["billing_day"])
        index = self.cycle.findData(selected.isoformat())
        if index < 0:
            self._cycle_through = max(self._cycle_through, self.when.iso())
            self._populate_cycles()
            return
        self.cycle.blockSignals(True)
        try:
            self.cycle.setCurrentIndex(index)
        finally:
            self.cycle.blockSignals(False)

    def _update_hint(self, *args):
        before_opening = self._before_opening()
        if before_opening:
            if self.rent.isEnabled():
                self._rent_before_opening = self.rent.text()
            self.rent.set_paise(0)
            self.rent.setEnabled(False)
            self.cycle_hint.setText(
                f"Departure is before the account opened on {date_text(self.tenancy['opening_date'])}. "
                "Older dues belong in opening balances; no monthly final rent is added. "
                "Later cycle bills are removed; any applied payments return to available balance."
            )
        else:
            if not self.rent.isEnabled() and self._rent_before_opening is not None:
                self.rent.setText(self._rent_before_opening)
            self.rent.setEnabled(True)
            self.cycle_hint.setText(
                f"Billing stops after {date_text(self.when.iso())}. "
                "Later cycle bills are removed; any applied payments return to available balance."
            )

    def _before_opening(self):
        return bool(self.tenancy and self.tenancy.get("opening_date")
                    and self.when.iso() < self.tenancy["opening_date"])

    def validate(self):
        return {
            "move_out": self.when.iso(),
            "final_rent_paise": 0 if self._before_opening() else self.rent.paise(),
            "reason": self.reason.text().strip(),
        }


class ReasonDialog(FormDialog):
    def __init__(self, title: str, description: str, parent=None):
        super().__init__(title, parent)
        self.add_hint(description)
        self.when = DateInput()
        self.form.addRow("Correction date", self.when)
        self.reason = self.field("Reason")

    def validate(self):
        return {
            "reason": required(self.reason, "Correction reason"),
            "date": self.when.iso(),
        }


class ExportDialog(FormDialog):
    def __init__(self, parent=None, start: str | None = None):
        super().__init__("Export an account statement", parent)
        self.start = DateInput(start)
        self.end = DateInput()
        self.language = combo(
            [("English", "en"), ("हिन्दी", "hi"), ("English + हिन्दी", "bilingual")]
        )
        self.form.addRow("From", self.start)
        self.form.addRow("Through", self.end)
        self.form.addRow("PDF language", self.language)
        self.add_hint(
            "The statement includes its opening balance, itemized charges, payments and closing balance. Names and reasons print as entered."
        )

    def validate(self):
        if self.end.iso() < self.start.iso():
            raise ValueError("The statement end cannot be before its start.")
        return {
            "start": self.start.iso(),
            "end": self.end.iso(),
            "language": self.language.currentData(),
        }
