from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QMessageBox

from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillAmountDialog, BillDialog, BillPaymentDialog, UnpaidDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.widgets import selected_id
from grihalekha.ui.window import MainWindow


def test_add_payment_then_pay_full_bill_and_return_to_balance(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "data")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "1", 1)
    today = date.today().isoformat()
    tenant = ledger.add_tenancy(flat, "Tenant", today, date.today().day, 100_000)
    window = MainWindow(ledger)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    try:
        window.show()
        window.select_account(tenant)
        rent = next(row for row in ledger.list_charges(tenant) if row["kind"] == "rent")
        ledger.post_charge(rent["id"], 100_000)
        window.refresh()
        select_bill(window.charges_table, rent["id"])
        opened = []

        def open_bill(dialog):
            opened.append(dialog.windowTitle())
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(BillDialog, "exec", open_bill)
        window.open_selected_bill()
        assert opened == ["Rent bill"]

        def accept_payment(dialog):
            assert not hasattr(dialog, "allocation_mode")
            assert [control.currentText() for control in dialog.findChildren(QComboBox)] == ["UPI"]
            dialog.amount.set_paise(40_000)
            dialog.result_data = dialog.validate()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(BillPaymentDialog, "exec", accept_payment)
        window.receive_payment()
        assert ledger.get_bill(rent["id"])["outstanding_paise"] == 100_000
        assert ledger.balance(tenant)["credit_paise"] == 40_000
        window.mark_selected_paid()
        assert len(errors) == 1 and "paid in full" in str(errors.pop())
        assert ledger.get_bill(rent["id"])["paid_paise"] == 0
        ledger.deposit_payment(tenant, 60_000, today, "cash")
        window.refresh()
        assert selected_id(window.charges_table) == rent["id"]
        window.mark_selected_paid()
        assert not errors
        assert ledger.get_bill(rent["id"])["outstanding_paise"] == 0
        assert ledger.balance(tenant)["credit_paise"] == 0
        assert window.charges_table.item(window.charges_table.currentRow(), 2).text() == "Paid"

        window.toggle_cycle_lock()
        assert window.cycle_lock_button.text() == "Unlock cycle"
        window.mark_selected_unpaid()
        assert len(errors) == 1 and "Unlock" in str(errors.pop())
        assert ledger.get_bill(rent["id"])["paid_paise"] == 100_000
        # Adding money remains available even when the selected bill is locked.
        window.receive_payment()
        assert ledger.balance(tenant)["credit_paise"] == 40_000
        assert ledger.get_bill(rent["id"])["paid_paise"] == 100_000
        window.toggle_cycle_lock()

        def accept_unpaid(dialog):
            assert not hasattr(dialog, "mode")
            dialog.reason.setText("Keep money available for a different bill")
            dialog.result_data = dialog.validate()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(UnpaidDialog, "exec", accept_unpaid)
        window.mark_selected_unpaid()
        assert ledger.get_bill(rent["id"])["outstanding_paise"] == 100_000
        assert ledger.balance(tenant)["credit_paise"] == 140_000
        assert all(not payment["reversed_on"] for payment in ledger.list_payments(tenant))
        assert not errors

        def failed_read(*args):
            raise RuntimeError("Account is temporarily unavailable")

        monkeypatch.setattr(ledger, "get_tenancy", failed_read)
        action = next(action for action in window.more_button.menu().actions()
                      if action.text() == "Move to another flat")
        action.trigger()
        assert len(errors) == 1 and "temporarily unavailable" in str(errors[0])
    finally:
        window.close()
        ledger.close()


def test_cycle_headers_do_not_open_as_bills_and_locks_disable_bill_actions(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "1", 1)
    today = date.today()
    current = today.replace(day=1)
    previous = date(current.year - (current.month == 1), 12 if current.month == 1 else current.month - 1, 1)
    tenant = ledger.add_tenancy(flat, "Tenant", previous.isoformat(), 1, 100_000)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    try:
        window.show()
        window.select_account(tenant)
        qtbot.waitExposed(window)
        qtbot.wait(10)
        headings = [row for row in range(window.charges_table.rowCount())
                    if isinstance(window.charges_table.item(row, 0).data(Qt.ItemDataRole.UserRole), str)]
        assert len(headings) == 2
        assert window.charges_table.rowCount() == 6
        assert all(window.charges_table.columnSpan(row, 0) == 8 for row in headings)
        window.charges_table.setColumnWidth(0, 340)
        widths = [window.charges_table.columnWidth(i) for i in range(8)]
        window.refresh()
        assert [window.charges_table.columnWidth(i) for i in range(8)] == widths
        window.charges_table.selectRow(headings[0])
        window.open_selected_bill()
        assert "Select a bill" in window.statusBar().currentMessage()
        window.toggle_cycle_lock()
        first = window._bill_cycles[0]
        assert first["locked"] and not window._bill_cycles[1]["locked"]
        assert "Locked" in window.charges_table.item(headings[0], 0).text()
        window.toggle_cycle_lock()
        rent = next(bill for bill in first["charges"] if bill["kind"] == "rent")
        ledger.post_charge(rent["id"])
        ledger.deposit_payment(tenant, 100_000, today.isoformat(), "cash")
        ledger.set_cycle_payment_lock(tenant, first["cycle_start"], True)
        bill = ledger.get_bill(rent["id"])
        dialog = BillDialog(bill, lambda: None, lambda: None, balance_paise=100_000)
        qtbot.addWidget(dialog)
        assert not dialog.paid_button.isEnabled()
        assert not dialog.unpaid_button.isEnabled()
        # Correcting an unlocked required bill keeps a reviewed replacement.
        ledger.set_cycle_payment_lock(tenant, first["cycle_start"], False)
        window.refresh()
        select_bill(window.charges_table, rent["id"])

        def accept_correction(dialog):
            dialog.amount.set_paise(90_000)
            dialog.reason.setText("Correct the agreed rent")
            dialog.result_data = dialog.validate()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(BillAmountDialog, "exec", accept_correction)
        window.correct_selected_bill_amount()
        replacement = next(bill for bill in window._charges if bill.get("replaces_charge_id") == rent["id"])
        assert replacement["status"] == "posted"
        assert replacement["amount_paise"] == 90_000
        assert ledger.balance(tenant)["credit_paise"] == 100_000
        assert not errors
    finally:
        window.close()
        ledger.close()
