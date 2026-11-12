"""Dated flat transfers and exchanges that retain the tenant's account."""

from datetime import date, timedelta

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QCheckBox, QDialogButtonBox

from ..dates import as_date, cycle_bounds
from .dialogs import combo, flat_label, required
from .widgets import AmountInput, DateInput, FormDialog, date_text


def _latest_occupancy(tenancy):
    history = tenancy.get("occupancy_history", [])
    return history[-1] if history else dict(tenancy, starts_on=tenancy["move_in"])


def _rent_start(tenancy, when):
    start, next_start = cycle_bounds(when, tenancy["billing_day"])
    return start if start == as_date(when) else next_start


class TransferDialog(FormDialog):
    def __init__(self, tenancy, flats, parent=None):
        super().__init__("Move tenant to another flat", parent, width=620)
        self.tenancy = tenancy
        source = _latest_occupancy(tenancy)
        self.add_hint(f"{tenancy['tenant_name']} · From {flat_label(source)}")
        self.flat = combo([
            (flat_label(flat) + (f" · Occupied by {flat['tenant_name']}" if flat.get("tenant_name") else " · Vacant now"), flat["id"])
            for flat in flats if flat["id"] != source["flat_id"]
        ])
        self.form.addRow("Destination flat", self.flat)
        self.when = DateInput()
        self.when.setMinimumDate(QDate.fromString(source["starts_on"], "yyyy-MM-dd").addDays(1))
        if tenancy.get("move_out"):
            self.when.setMaximumDate(QDate.fromString(tenancy["move_out"], "yyyy-MM-dd"))
        self.form.addRow("First day in new flat", self.when)
        self.change_rent = QCheckBox("Change monthly rent")
        self.rent = AmountInput(tenancy["rent_paise"], currency_code=self.currency_code)
        self.rent.setEnabled(False)
        self.form.addRow(self.change_rent)
        self.form.addRow(f"New monthly rent ({self.currency_symbol})", self.rent)
        self.rent_hint = self.add_hint("")
        self.reason = self.field("Notes", "Flat transfer")
        self.add_hint("Bills, payments, advance and deposit remain on this tenant's account. Flat history records the move. The destination must be available for the selected dates.")
        self.change_rent.toggled.connect(self.rent.setEnabled)
        self.change_rent.toggled.connect(self._update_rent_hint)
        self.when.dateChanged.connect(self._update_rent_hint)
        self._update_rent_hint()

    def _update_rent_hint(self, *args):
        if self.change_rent.isChecked():
            self.rent_hint.setText(f"New rent starts {date_text(_rent_start(self.tenancy, self.when.iso()))}. A move during a cycle changes rent from the next full cycle; no automatic prorating.")
        else:
            self.rent_hint.setText(f"Monthly rent remains {self.money(self.tenancy['rent_paise'])}.")

    def validate(self):
        if self.flat.currentData() is None:
            raise ValueError("Add or choose a different destination flat.")
        if self.when.iso() <= _latest_occupancy(self.tenancy)["starts_on"]:
            raise ValueError("Choose a date after the latest recorded or scheduled move-in.")
        return dict(flat_id=self.flat.currentData(), effective_on=self.when.iso(),
                    rent_paise=self.rent.paise() if self.change_rent.isChecked() else None,
                    reason=self.reason.text().strip() or "Flat transfer")


class ExchangeDialog(FormDialog):
    def __init__(self, tenancy, others, parent=None):
        super().__init__("Exchange flats with another tenant", parent, width=680)
        self.tenancy = tenancy
        self.others = {other["id"]: other for other in others if other["id"] != tenancy["id"] and not other.get("archived_on") and not other.get("cancelled_on")}
        self.add_hint(f"{tenancy['tenant_name']} · {flat_label(_latest_occupancy(tenancy))}")
        self.other = combo([(f"{other['tenant_name']} · {flat_label(_latest_occupancy(other))}", other["id"])
                            for other in self.others.values()])
        self.form.addRow("Exchange with", self.other)
        self.when = DateInput()
        self.form.addRow("Exchange date", self.when)
        self.first_change = QCheckBox(f"Change rent for {tenancy['tenant_name']}")
        self.first_rent = AmountInput(tenancy["rent_paise"], currency_code=self.currency_code)
        self.first_rent.setEnabled(False)
        self.form.addRow(self.first_change)
        self.form.addRow(f"New monthly rent ({self.currency_symbol})", self.first_rent)
        self.second_change = QCheckBox("Change other tenant's rent")
        self.second_rent = AmountInput(None, currency_code=self.currency_code)
        self.second_rent.setEnabled(False)
        self.form.addRow(self.second_change)
        self.form.addRow(f"Other tenant's new rent ({self.currency_symbol})", self.second_rent)
        self.rent_hint = self.add_hint("")
        self.reason = self.field("Notes", "Flat exchange")
        self.add_hint("Both moves are saved together. Each tenant retains their bills, payments, advance and deposit. New rent starts on the exchange date if it is a cycle boundary, otherwise on the next full cycle. No automatic prorating.")
        self.first_change.toggled.connect(self.first_rent.setEnabled)
        self.second_change.toggled.connect(self.second_rent.setEnabled)
        self.first_change.toggled.connect(self._update_rent_hint)
        self.second_change.toggled.connect(self._update_rent_hint)
        self.when.dateChanged.connect(self._update_rent_hint)
        self.other.currentIndexChanged.connect(self._other_changed)
        self._other_changed()

    def _other_changed(self, *args):
        other = self.others.get(self.other.currentData())
        if other:
            self.second_change.setText(f"Change rent for {other['tenant_name']}")
            self.second_rent.set_paise(other["rent_paise"])
            tenants = (self.tenancy, other)
            earliest = max(as_date(_latest_occupancy(tenant)["starts_on"]) for tenant in tenants) + timedelta(days=1)
            departures = [as_date(tenant["move_out"]) for tenant in tenants if tenant.get("move_out")]
            latest = min(departures) if departures else date(2199, 12, 31)
            if earliest <= latest:
                self.when.setDateRange(QDate(earliest.year, earliest.month, earliest.day), QDate(latest.year, latest.month, latest.day))
        self._update_rent_hint()

    def _update_rent_hint(self, *args):
        other = self.others.get(self.other.currentData())
        rows = []
        for tenancy, changed in ((self.tenancy, self.first_change.isChecked()), (other, self.second_change.isChecked())):
            if tenancy and changed:
                rows.append(f"{tenancy['tenant_name']}: new rent from {date_text(_rent_start(tenancy, self.when.iso()))}")
        self.rent_hint.setText("\n".join(rows) if rows else "Monthly rents remain unchanged for both tenants.")

    def validate(self):
        other = self.others.get(self.other.currentData())
        if other is None:
            raise ValueError("Choose another tenant account to exchange flats.")
        for tenancy in (self.tenancy, other):
            if self.when.iso() <= _latest_occupancy(tenancy)["starts_on"] or (tenancy.get("move_out") and self.when.iso() > tenancy["move_out"]):
                raise ValueError("Choose an exchange date after both latest move-ins and no later than either move-out.")
        if _latest_occupancy(other)["flat_id"] == _latest_occupancy(self.tenancy)["flat_id"]:
            raise ValueError("Choose a tenant from a different flat.")
        return dict(second_id=other["id"], effective_on=self.when.iso(),
                    first_rent_paise=self.first_rent.paise() if self.first_change.isChecked() else None,
                    second_rent_paise=self.second_rent.paise() if self.second_change.isChecked() else None,
                    reason=self.reason.text().strip() or "Flat exchange")


class CancelMoveDialog(FormDialog):
    def __init__(self, tenancy, other=None, parent=None):
        super().__init__("Cancel planned flat move", parent)
        latest = _latest_occupancy(tenancy)
        self.add_hint(f"Cancel {tenancy['tenant_name']}'s planned move to {flat_label(latest)} on {date_text(latest['starts_on'])}.")
        if other:
            self.add_hint(f"This is an exchange with {other['tenant_name']}. Both planned moves will be cancelled together, keeping both tenants in their previous flats.")
        self.reason = self.field("Reason", "Cancelled planned flat move")
        self.add_hint("Any new rent scheduled as part of this move is cancelled too. A move that has already started cannot be cancelled here; record another dated move instead.")

    def validate(self):
        return {"reason": required(self.reason, "Reason")}


class CancelMoveOutDialog(FormDialog):
    def __init__(self, tenancy, parent=None):
        super().__init__("Cancel planned move-out", parent)
        self.add_hint(
            f"{tenancy['tenant_name']} · Planned last occupied day: {date_text(tenancy['move_out'])}. "
            "The tenant will continue occupying the flat and monthly billing will resume."
        )
        self.reason = self.field("Reason", "Cancelled planned move-out")
        self.add_hint(
            "Review the restored rent and electricity drafts. Any money released by a bill correction "
            "stays in available balance until you mark a bill Paid."
        )

    def validate(self):
        return {"reason": required(self.reason, "Reason")}


class CancelUpcomingTenantDialog(FormDialog):
    def __init__(self, description, parent=None):
        super().__init__("Cancel upcoming tenant", parent, width=660)
        self.add_hint(description)
        self.reason = self.field("Cancellation reason")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Continue")

    def sizeHint(self):
        size = super().sizeHint()
        # Leave space for the wrapped impact summary and reason on first open.
        # FormDialog still clamps to small screens and keeps the actions fixed.
        size.setHeight(max(440, size.height()))
        return size

    def validate(self):
        return {"reason": required(self.reason, "Cancellation reason")}


class DeleteArchivedTenantDialog(FormDialog):
    def __init__(self, preview, parent=None):
        super().__init__("Delete archived tenant data", parent, width=660)
        tenancy = preview["tenancy"]
        counts = preview["counts"]
        self.add_hint(f"{tenancy['tenant_name']} · {flat_label(tenancy)}")
        self.add_hint(
            f"Permanently remove this archived household and its {counts['charges']} bill records, "
            f"{counts['payments']} payments, deposits, flat history and payment proofs. "
            "Other tenants and the property remain."
        )
        self.backup = QCheckBox("Save both CSV and PDF records before deletion")
        self.backup.setChecked(True)
        self.form.addRow(self.backup)
        self.language = combo([("English", "en"), ("हिन्दी", "hi"), ("English + हिन्दी", "bilingual")])
        self.form.addRow("PDF language", self.language)
        self.backup.toggled.connect(self.language.setEnabled)
        self.add_hint(
            "CSV records include the full account history and payment proofs. The PDF contains "
            "a readable statement and history. These exports are for reference; use a complete "
            "application backup for restoration. Existing backups and issued PDFs are retained."
        )
        self.add_hint("You will review the saved files and confirm deletion in the next step.")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Continue")

    def validate(self):
        return {"backup": self.backup.isChecked(), "language": self.language.currentData()}
