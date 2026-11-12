"""Electricity readings and deferral preserve bill/payment behavior in the UI."""

from datetime import timedelta

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from grihalekha.clock import today_ist
from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillDialog, MeterReadingDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.dialogs import HouseholdDialog, PostDraftDialog, TenancyDialog
from grihalekha.ui.widgets import MeterReadingInput, selected_id
from grihalekha.ui.window import MainWindow


@pytest.fixture
def account(qtbot, tmp_path, monkeypatch):
    today = today_ist()
    current = today.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    first = (previous - timedelta(days=1)).replace(day=1)
    ledger = Ledger(tmp_path)
    building = ledger.add_building("Example house", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(flat, "Tenant", first.isoformat(), 1, 10_000,
                                initial_meter_reading_hundredths=100_025)
    errors = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, error: errors.append(error))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(tenancy)
    yield window, ledger, tenancy, errors
    window.close()
    ledger.close()


def electricity_bills(ledger, tenancy):
    return sorted((bill for bill in ledger.list_charges(tenancy) if bill["kind"] == "electricity"),
                  key=lambda bill: bill["period_start"])


def test_optional_reading_widget_rejects_rounding_and_distinguishes_zero(qtbot):
    editor = MeterReadingInput()
    qtbot.addWidget(editor)
    assert editor.reading() is None
    for text, value in (("0", 0), ("123.4", 12_340), ("123.45", 12_345)):
        editor.setText(text)
        assert editor.reading() == value
    editor.set_reading(0)
    assert editor.text() == "0.00"
    for text in ("-1", "1.234", "NaN", "1e3", "1,000"):
        editor.setText(text)
        with pytest.raises(ValueError):
            editor.reading()
    editor.clear()
    assert editor.reading() is None


def test_initial_reading_is_optional_on_creation_and_editable_in_household(account, qtbot, monkeypatch):
    window, ledger, tenancy, errors = account
    create = TenancyDialog(ledger.list_flats())
    qtbot.addWidget(create)
    create.name.setText("New tenant")
    create.rent.set_paise(0)
    assert create.validate()["initial_meter_reading_hundredths"] is None
    create.initial_meter_reading.setText("0.00")
    assert create.validate()["initial_meter_reading_hundredths"] == 0
    assert "1000.25 kWh" in window.household_details.text()

    def accept(dialog):
        assert dialog.initial_meter_reading.reading() == 100_025
        dialog.initial_meter_reading.clear()
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(HouseholdDialog, "exec", accept)
    window.edit_household()
    assert ledger.get_tenancy(tenancy)["initial_meter_reading_hundredths"] is None
    assert "Initial electric meter reading: Not recorded" in window.household_details.text()
    assert not errors


def test_post_then_edit_paid_locked_reading_keeps_money_and_bill_identity(account, monkeypatch, qtbot):
    window, ledger, tenancy, errors = account
    bill = electricity_bills(ledger, tenancy)[-1]
    select_bill(window.charges_table, bill["id"])

    def post(dialog):
        dialog.amount.set_paise(12_345)
        dialog.meter_reading.setText("1250.75")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PostDraftDialog, "exec", post)
    window.open_selected_bill()
    assert ledger.get_bill(bill["id"])["meter_reading_hundredths"] == 125_075
    ledger.deposit_payment(tenancy, 12_345, today_ist().isoformat(), "cash")
    ledger.mark_bill_paid(bill["id"])
    ledger.set_cycle_payment_lock(tenancy, bill["electricity_cycle_start"], True)
    window.refresh()
    select_bill(window.charges_table, bill["id"])
    assert window.meter_bill_action.isEnabled()
    assert not window.correct_bill_action.isEnabled()
    before = ledger.balance(tenancy)
    original = ledger.get_bill(bill["id"])

    def edit(dialog):
        assert dialog.meter_reading.reading() == 125_075
        dialog.meter_reading.setText("1250.76")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(MeterReadingDialog, "exec", edit)
    window.meter_bill_action.trigger()
    changed = ledger.get_bill(bill["id"])
    assert changed["meter_reading_hundredths"] == 125_076
    assert selected_id(window.charges_table) == bill["id"]
    assert changed["amount_paise"] == original["amount_paise"]
    assert changed["payments"] == original["payments"]
    assert ledger.balance(tenancy) == before
    details = BillDialog(changed, lambda: None, lambda: None, edit_meter=lambda: None)
    qtbot.addWidget(details)
    assert details.meter_button.isEnabled()
    assert not details.paid_button.isEnabled()
    assert "1250.76 kWh" in " ".join(label.text() for label in details.findChildren(QLabel))
    archived = BillDialog(changed, lambda: None, lambda: None, archived=True, edit_meter=lambda: None)
    qtbot.addWidget(archived)
    assert not archived.meter_button.isEnabled()
    assert not errors


def test_reading_only_edit_keeps_unknown_bill_unposted_and_cancel_is_noop(account, monkeypatch):
    window, ledger, tenancy, errors = account
    bill = electricity_bills(ledger, tenancy)[0]
    select_bill(window.charges_table, bill["id"])

    def edit(dialog):
        dialog.meter_reading.setText("0.00")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(MeterReadingDialog, "exec", edit)
    window.edit_selected_meter_reading()
    changed = ledger.get_bill(bill["id"])
    assert changed["meter_reading_hundredths"] == 0
    assert changed["amount_paise"] is None
    assert changed["status"] == "draft"
    revision = ledger.get_revision()
    monkeypatch.setattr(MeterReadingDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window.edit_selected_meter_reading()
    assert ledger.get_revision() == revision
    assert not errors


def test_skip_checkbox_clears_visible_inputs_and_can_restore_unsaved_values(account, qtbot):
    _, ledger, tenancy, _ = account
    bill = electricity_bills(ledger, tenancy)[0]
    dialog = PostDraftDialog(bill)
    qtbot.addWidget(dialog)
    dialog.amount.setText("25.50")
    dialog.meter_reading.setText("1600.01")
    dialog.defer_electricity.setChecked(True)
    assert not dialog.amount.isEnabled() and not dialog.meter_reading.isEnabled()
    assert dialog.amount.text() == dialog.meter_reading.text() == ""
    assert "clears" in dialog.defer_hint.text()
    assert dialog.validate() == {"defer_electricity": True}
    dialog.defer_electricity.setChecked(False)
    assert dialog.amount.text() == "25.50"
    assert dialog.meter_reading.text() == "1600.01"
    assert dialog.validate()["meter_reading_hundredths"] == 160_001
    rent = next(item for item in ledger.list_charges(tenancy) if item["kind"] == "rent")
    rent_dialog = PostDraftDialog(rent)
    qtbot.addWidget(rent_dialog)
    assert not hasattr(rent_dialog, "meter_reading")
    assert not hasattr(rent_dialog, "defer_electricity")


def test_defer_two_months_then_bill_once_shows_coverage_and_protects_included_cycles(account, monkeypatch, qtbot):
    window, ledger, tenancy, errors = account
    first, second, third = electricity_bills(ledger, tenancy)

    def skip(dialog):
        assert dialog.defer_electricity.isChecked()
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PostDraftDialog, "exec", skip)
    for bill in (first, second):
        select_bill(window.charges_table, bill["id"])
        assert window.defer_bill_action.isEnabled()
        window.defer_bill_action.trigger()
        assert window.charges_table.item(window.charges_table.currentRow(), 2).text() == "Deferred · bill later"
        assert not window.meter_bill_action.isEnabled()
        assert not window.correct_bill_action.isEnabled()
    # Undo remains available until a later bill includes this cycle.
    window.defer_bill_action.trigger()
    assert not ledger.get_bill(second["id"])["electricity_deferred"]
    window.defer_bill_action.trigger()

    def post(dialog):
        hints = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "Includes deferred electricity" in hints
        dialog.amount.set_paise(35_000)
        dialog.meter_reading.setText("2000.25")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PostDraftDialog, "exec", post)
    select_bill(window.charges_table, third["id"])
    window.open_selected_bill()
    posted = ledger.get_bill(third["id"])
    assert posted["coverage_period_start"] == first["period_start"]
    assert posted["amount_paise"] == 35_000
    for bill in (first, second):
        select_bill(window.charges_table, bill["id"])
        assert window.charges_table.item(window.charges_table.currentRow(), 2).text() == f"Included in bill #{third['id']}"
        assert not window.defer_bill_action.isEnabled()
        assert not window.meter_bill_action.isEnabled()
        covered = ledger.get_bill(bill["id"])
        details = BillDialog(covered, lambda: None, lambda: None,
                             open_covering_bill=lambda: None, undo_deferral=lambda: None)
        qtbot.addWidget(details)
        assert hasattr(details, "covering_bill_button")
        assert not hasattr(details, "undo_deferral_button")
        assert details.paid_button.isHidden()
    assert not errors


def test_deferral_cancel_keeps_saved_reading_and_review_navigation_skips_deferred(account, monkeypatch):
    window, ledger, tenancy, errors = account
    bills = electricity_bills(ledger, tenancy)
    ledger.set_meter_reading(bills[0]["id"], 123)
    window.refresh()
    select_bill(window.charges_table, bills[0]["id"])
    revision = ledger.get_revision()
    monkeypatch.setattr(PostDraftDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window.toggle_electricity_deferral()
    assert ledger.get_revision() == revision
    assert ledger.get_bill(bills[0]["id"])["meter_reading_hundredths"] == 123
    ledger.set_electricity_deferred(bills[0]["id"], True)
    for bill in ledger.list_charges(tenancy):
        if bill["kind"] == "rent":
            ledger.post_charge(bill["id"], 0)
    window.refresh()
    window.review_drafts()
    assert selected_id(window.charges_table) == bills[1]["id"]
    assert not errors


def test_archive_ui_accepts_settled_combined_bill_with_deferred_source(account, monkeypatch):
    window, ledger, tenancy, errors = account
    last_day = today_ist().replace(day=1) - timedelta(days=1)
    ledger.move_out(tenancy, last_day.isoformat(), 0)
    bills = [bill for bill in electricity_bills(ledger, tenancy) if bill["status"] == "draft"]
    assert len(bills) == 2
    ledger.set_electricity_deferred(bills[0]["id"], True)
    ledger.set_electricity_deferred(bills[1]["id"], True)
    for bill in ledger.list_charges(tenancy):
        if bill["kind"] == "rent" and bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.refresh()
    window.archive_account()
    assert messages and "deferred electricity" in messages[0]
    assert ledger.get_tenancy(tenancy)["archived_on"] is None
    messages.clear()
    ledger.set_electricity_deferred(bills[1]["id"], False)
    ledger.post_charge(bills[1]["id"], 0)
    window.refresh()
    window.archive_account()
    assert ledger.get_tenancy(tenancy)["archived_on"] == today_ist().isoformat()
    assert not messages
    assert not errors


@pytest.mark.parametrize("amount", [0, 100])
def test_scheduled_cover_disables_undo_and_only_settled_account_can_archive(account, monkeypatch, qtbot, amount):
    from sqlalchemy import update
    from grihalekha import models

    window, ledger, tenancy, errors = account
    last_day = today_ist().replace(day=1) - timedelta(days=1)
    ledger.move_out(tenancy, last_day.isoformat(), 0)
    bills = [bill for bill in electricity_bills(ledger, tenancy) if bill["status"] == "draft"]
    source, target = bills
    ledger.set_electricity_deferred(source["id"], True)
    with ledger._write() as connection:
        connection.execute(update(models.charges).where(models.charges.c.id == target["id"]).values(
            charge_date=(today_ist() + timedelta(days=1)).isoformat()))
    ledger.post_charge(target["id"], amount)
    for bill in ledger.list_charges(tenancy):
        if bill["kind"] == "rent" and bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    window.refresh()
    select_bill(window.charges_table, source["id"])
    assert not window.defer_bill_action.isEnabled()
    detail = ledger.get_bill(source["id"])
    assert detail["electricity_covered_by_charge_id"] is None
    assert detail["electricity_scheduled_covering_charge_id"] == target["id"]
    assert window.charges_table.item(window.charges_table.currentRow(), 2).text() == f"Scheduled in bill #{target['id']}"
    dialog = BillDialog(detail, lambda: None, lambda: None,
                        undo_deferral=lambda: None, open_covering_bill=lambda: None)
    qtbot.addWidget(dialog)
    assert hasattr(dialog, "covering_bill_button") and not hasattr(dialog, "undo_deferral_button")
    assert ledger.balance(tenancy)["outstanding_paise"] == 0
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.archive_account()
    assert not messages
    if amount:
        assert not ledger.get_tenancy(tenancy)["archived_on"]
        assert errors and "Settle" in str(errors[0])
    else:
        assert ledger.get_tenancy(tenancy)["archived_on"]
        assert not errors
