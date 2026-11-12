"""Tenant moves and closure preserve identity and keep closed accounts read only."""

from datetime import date, timedelta

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton

from grihalekha.dates import cycle_bounds
from grihalekha.ledger import Ledger
from grihalekha.paths import asset_dir
from grihalekha.ui.bill_dialogs import BillDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.dialogs import MoveOutDialog
from grihalekha.ui.lifecycle_dialogs import CancelMoveDialog, CancelMoveOutDialog, ExchangeDialog, TransferDialog
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.window import MainWindow


@pytest.fixture
def lifecycle_window(qapp, qtbot, tmp_path, monkeypatch):
    apply_theme(qapp)
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))
    qapp.setFont(QFont("Noto Sans", 10))
    ledger = Ledger(tmp_path)
    first = ledger.add_building("First house", "First address")
    second = ledger.add_building("Second house", "Second address")
    flats = [ledger.add_flat(first, "101", "Ground", 1), ledger.add_flat(second, "201", "Ground", 1), ledger.add_flat(second, "202", "First", 2)]
    start = (date.today() - timedelta(days=10)).isoformat()
    tenants = [ledger.add_tenancy(flats[0], "First tenant", start, 1, 100_000),
               ledger.add_tenancy(flats[1], "Second tenant", start, 5, 200_000)]
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(tenants[0])
    qtbot.waitExposed(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    yield window, flats, tenants, errors
    window.close()
    ledger.close()


def test_transfer_dialog_explains_when_optional_rent_changes(lifecycle_window, qtbot):
    window, flats, tenants, errors = lifecycle_window
    tenancy = window.ledger.get_tenancy(tenants[0])
    dialog = TransferDialog(tenancy, window._flats)
    qtbot.addWidget(dialog)
    dialog.flat.setCurrentIndex(dialog.flat.findData(flats[2]))
    today = date.today()
    dialog.when.set_iso(today.isoformat())
    assert dialog.validate()["rent_paise"] is None
    dialog.change_rent.setChecked(True)
    dialog.rent.set_paise(150_000)
    values = dialog.validate()
    assert values["rent_paise"] == 150_000
    start, following = cycle_bounds(today, tenancy["billing_day"])
    effective = start if today == start else following
    assert effective.strftime("%d %b %Y") in dialog.rent_hint.text()
    assert "no automatic prorating" in dialog.rent_hint.text().lower()


def test_transfer_action_keeps_account_and_shows_both_occupancies(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    before = window.ledger.balance(tenants[0])

    def accept(dialog):
        dialog.flat.setCurrentIndex(dialog.flat.findData(flats[2]))
        dialog.when.set_iso(date.today().isoformat())
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TransferDialog, "exec", accept)
    window.transfer_tenant()
    assert not errors
    assert window._selected_tenancy == tenants[0]
    assert window.ledger.get_tenancy(tenants[0])["flat_id"] == flats[2]
    assert window.ledger.balance(tenants[0]) == before
    assert window.occupancy_table.rowCount() == 2
    assert "101" in window.occupancy_table.item(0, 2).text()
    assert "202" in window.occupancy_table.item(1, 2).text()


def test_exchange_action_moves_both_accounts_together(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window

    def accept(dialog):
        dialog.other.setCurrentIndex(dialog.other.findData(tenants[1]))
        dialog.when.set_iso(date.today().isoformat())
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExchangeDialog, "exec", accept)
    window.exchange_tenants()
    assert not errors
    assert window.ledger.get_tenancy(tenants[0])["flat_id"] == flats[1]
    assert window.ledger.get_tenancy(tenants[1])["flat_id"] == flats[0]
    assert window._selected_tenancy == tenants[0]


def test_existing_move_out_can_be_extended_and_shortened(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    previous = date.today() + timedelta(days=5)
    window.ledger.move_out(tenants[0], previous.isoformat(), 50_000)
    window.refresh()
    seen = []
    target = previous + timedelta(days=4)

    def accept(dialog):
        seen.append(dialog.when.iso())
        dialog.when.set_iso(target.isoformat())
        dialog.rent.set_paise(70_000)
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(MoveOutDialog, "exec", accept)
    window.end_tenancy()
    assert not errors
    assert seen == [previous.isoformat()]
    assert window.ledger.get_tenancy(tenants[0])["move_out"] == target.isoformat()
    target = previous - timedelta(days=2)
    window.end_tenancy()
    assert not errors
    assert window.ledger.get_tenancy(tenants[0])["move_out"] == target.isoformat()


def test_archive_filter_read_only_bill_and_reopen(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    ledger = window.ledger
    ledger.move_out(tenants[0], (date.today() - timedelta(days=1)).isoformat(), 0)
    for charge in ledger.list_charges(tenants[0]):
        if charge["status"] == "draft":
            ledger.post_charge(charge["id"], 0, apply_credit=False)
    window.refresh()
    window.archive_account()
    assert not errors
    assert ledger.get_tenancy(tenants[0])["archived_on"]
    assert window.account_combo.findData(tenants[0]) == -1
    window.show_archived.setChecked(True)
    window.select_account(tenants[0])
    assert window.export_button.isEnabled()
    assert window.account_tabs.isEnabled()
    assert not any(control.isEnabled() for control in window._account_mutations)
    assert window.reopen_action.isEnabled()
    assert "Read only" in window.account_subtitle.text()
    inspected = []

    def inspect(dialog):
        controls = dialog.findChildren(QPushButton)
        inspected.extend(control for control in controls if control.text().startswith("Mark "))
        assert not any(control.isEnabled() for control in inspected)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(BillDialog, "exec", inspect)
    posted = next(charge for charge in window._charges if charge["status"] == "posted")
    select_bill(window.charges_table, posted["id"])
    window.open_selected_bill()
    assert inspected and not errors
    window.reopen_account()
    assert not errors
    assert not ledger.get_tenancy(tenants[0])["archived_on"]
    selection_actions = {window.cancel_transfer_action, window.cancel_moveout_action, window.cancel_upcoming_action,
                         window.undo_bill_action, window.delete_bill_action,
                         window.correct_bill_action, window.cancel_bill_action,
                         window.meter_bill_action, window.defer_bill_action}
    assert all(control.isEnabled() for control in window._account_mutations if control not in selection_actions)
    assert not window.cancel_upcoming_action.isEnabled()  # A former tenant's stay already happened.
    assert not window.undo_bill_action.isEnabled()  # selected bill is already posted
    assert not window.delete_bill_action.isEnabled()  # selected bill is rent/electricity
    assert not window.reopen_action.isVisible()


def test_declined_transfer_confirmation_keeps_occupancy(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window

    def accept(dialog):
        dialog.flat.setCurrentIndex(dialog.flat.findData(flats[2]))
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TransferDialog, "exec", accept)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.transfer_tenant()
    assert not errors


def test_moveout_cycle_can_be_months_before_account_opening(qtbot):
    tenancy = dict(move_in="2023-01-05", opening_date="2024-01-05", billing_day=5,
                   move_out=None, occupancy_history=[])
    dialog = MoveOutDialog(tenancy=tenancy)
    qtbot.addWidget(dialog)
    assert dialog.when.minimumDate() == QDate(2023, 1, 5)
    index = dialog.cycle.findData("2023-08-05")
    assert index >= 0
    dialog.cycle.setCurrentIndex(index)
    assert dialog.when.iso() == "2023-09-04"
    dialog.when.set_iso("2023-08-15")
    assert dialog.cycle.currentData() == "2023-08-05"
    assert not dialog.rent.isEnabled()
    assert dialog.validate()["final_rent_paise"] == 0
    dialog.when.set_iso("2024-02-10")
    assert dialog.rent.isEnabled()
    with pytest.raises(ValueError):
        dialog.validate()  # Final rent still requires an explicit amount.


def test_backdated_moveout_previews_returned_balance_and_marks_final_cycle(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    ledger, tenant = window.ledger, tenants[0]
    today = date.today()
    later_start = cycle_bounds(today, 1)[1]
    ledger.prepare_monthly_drafts(later_start.isoformat())
    later = next(row for row in ledger.list_charges(tenant) if row["kind"] == "rent" and row["period_start"] == later_start.isoformat())
    ledger.post_charge(later["id"], 100_000)
    # Record a departure in the past while future drafts have already been prepared.
    last_day = (today - timedelta(days=2)).isoformat()
    reviewed = []

    def accept(dialog):
        dialog.when.set_iso(last_day)
        dialog.rent.set_paise(50_000)
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    def confirm(*args):
        reviewed.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(MoveOutDialog, "exec", accept)
    monkeypatch.setattr(QMessageBox, "question", confirm)
    window.end_tenancy()
    assert not errors
    assert reviewed and "Moved out cycle:" in reviewed[0] and "available balance" in reviewed[0]
    assert ledger.get_tenancy(tenant)["move_out"] == last_day
    assert all(bill["period_start"] <= last_day for bill in window._charges)
    assert any("Moved out cycle" in window.charges_table.item(row, 0).text()
               for row in range(window.charges_table.rowCount()))
    assert window.receive_button.isEnabled()
    assert not window.cancel_moveout_action.isEnabled()


def test_cancel_planned_moveout_restores_billing_and_disables_action(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    future = (date.today() + timedelta(days=14)).isoformat()
    window.ledger.move_out(tenants[0], future, 50_000)
    window.refresh()
    assert window.cancel_moveout_action.isEnabled()

    def accept(dialog):
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CancelMoveOutDialog, "exec", accept)
    window.cancel_planned_move_out()
    assert not errors
    tenancy = window.ledger.get_tenancy(tenants[0])
    assert tenancy["move_out"] is None
    assert tenancy["occupancy_history"][-1]["ends_on"] is None
    assert not window.cancel_moveout_action.isEnabled()
    assert window.receive_button.isEnabled()


def test_declining_planned_moveout_cancellation_preserves_departure(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    future = (date.today() + timedelta(days=14)).isoformat()
    window.ledger.move_out(tenants[0], future, 50_000)
    window.refresh()

    def accept(dialog):
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CancelMoveOutDialog, "exec", accept)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.cancel_planned_move_out()
    assert not errors
    assert window.ledger.get_tenancy(tenants[0])["move_out"] == future
    assert window.ledger.get_tenancy(tenants[0])["flat_id"] == flats[0]


def test_cancel_planned_exchange_warns_and_removes_both_plans(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    effective = (date.today() + timedelta(days=14)).isoformat()
    window.ledger.exchange_tenancies(tenants[0], tenants[1], effective)
    window.refresh()
    assert window.cancel_transfer_action.isEnabled()
    reviewed = []

    def accept(dialog):
        reviewed.append(dialog)
        dialog.reason.setText("Both households changed their plans")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CancelMoveDialog, "exec", accept)
    window.cancel_planned_move()
    assert reviewed and not errors
    first = window.ledger.get_tenancy(tenants[0])
    second = window.ledger.get_tenancy(tenants[1])
    assert first["flat_id"] == flats[0]
    assert second["flat_id"] == flats[1]
    assert len(first["occupancy_history"]) == len(second["occupancy_history"]) == 1
    assert not window.cancel_transfer_action.isEnabled()


def test_archive_action_explains_unreviewed_bills(lifecycle_window, monkeypatch):
    window, flats, tenants, errors = lifecycle_window
    window.ledger.move_out(tenants[0], (date.today() - timedelta(days=1)).isoformat(), 0)
    window.refresh()
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    window.archive_account()
    assert messages and "Review pending monthly bills and deferred electricity" in messages[0]
    assert not window.ledger.get_tenancy(tenants[0])["archived_on"]
    assert not errors
