"""Bill correction actions retain funds, respect locks and select stable IDs."""

from datetime import timedelta

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from grihalekha.clock import today_ist
from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillAmountDialog, BillCorrectionDialog, BillDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.dialogs import ChargeDialog
from grihalekha.ui.widgets import selected_id
from grihalekha.ui.window import MainWindow
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    today = today_ist()
    tenant = ledger.add_tenancy(flat, "Tenant", today.isoformat(), today.day, 100_000)
    errors = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, error: errors.append(error))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(tenant)
    yield window, ledger, tenant, today.isoformat(), errors
    window.close()
    ledger.close()


def accept_reason(dialog):
    dialog.reason.setText("Corrected an accidental action")
    dialog.result_data = dialog.validate()
    return QDialog.DialogCode.Accepted


def test_undo_cancelled_paid_rent_restores_unpaid_and_selects_bill(account, monkeypatch):
    window, ledger, tenant, today, errors = account
    rent = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "rent")
    ledger.post_charge(rent["id"], 100_000)
    ledger.deposit_payment(tenant, 100_000, today, "cash")
    ledger.mark_bill_paid(rent["id"])
    window.refresh()
    select_bill(window.charges_table, rent["id"])
    assert not window.undo_bill_action.isEnabled()
    assert not window.delete_bill_action.isEnabled()
    cancel_historical_bill(ledger, rent["id"], "Old accidental cancellation", prepare_replacement=True)
    window.refresh()
    select_bill(window.charges_table, rent["id"])
    assert window.undo_bill_action.isEnabled()
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    monkeypatch.setattr(BillCorrectionDialog, "exec", accept_reason)
    window.undo_bill_action.trigger()
    assert not errors
    restored = ledger.get_bill(selected_id(window.charges_table))
    assert restored["id"] != rent["id"]
    assert restored["status"] == "posted"
    assert restored["outstanding_paise"] == 100_000
    assert restored["paid_paise"] == 0
    assert ledger.get_bill(rent["id"])["status"] == "void"
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert len([bill for bill in ledger.list_charges(tenant)
                if bill["kind"] == "rent" and bill["status"] != "void"]) == 1


def test_delete_paid_miscellaneous_bill_retains_receipt_and_returns_balance(account, monkeypatch):
    window, ledger, tenant, today, errors = account
    misc = ledger.add_charge(tenant, "misc", 55_500, today, today, today, "Wrong repair bill")
    ledger.deposit_payment(tenant, 55_500, today, "cash")
    ledger.mark_bill_paid(misc)
    window.refresh()
    select_bill(window.charges_table, misc)
    assert window.delete_bill_action.isEnabled()
    monkeypatch.setattr(BillCorrectionDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window.delete_bill_action.trigger()
    assert ledger.get_bill(misc).get("deleted_on") is None
    assert ledger.balance(tenant)["credit_paise"] == 0
    window.toggle_cycle_lock()
    assert not window.delete_bill_action.isEnabled()
    window.delete_selected_bill()
    assert len(errors) == 1 and "Unlock" in str(errors.pop())
    assert ledger.get_bill(misc).get("deleted_on") is None
    window.toggle_cycle_lock()
    monkeypatch.setattr(BillCorrectionDialog, "exec", accept_reason)
    window.delete_bill_action.trigger()
    assert not errors
    assert ledger.get_bill(misc)["deleted_on"] == today
    assert misc not in [bill["id"] for bill in window._charges]
    assert ledger.balance(tenant)["credit_paise"] == 55_500
    assert all(not payment["reversed_on"] for payment in ledger.list_payments(tenant))
    assert any(bill["id"] == misc for bill in ledger.list_charges(tenant, include_deleted=True))
    assert not window.delete_bill_action.isEnabled()


def test_bill_details_offer_only_eligible_correction_types(account, qtbot):
    window, ledger, tenant, today, errors = account
    rent = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "rent")
    ledger.post_charge(rent["id"], 100_000)
    rent_dialog = BillDialog(ledger.get_bill(rent["id"]), lambda: None, lambda: None,
                            undo_cancel=lambda: None, delete_bill=lambda: None)
    qtbot.addWidget(rent_dialog)
    assert not hasattr(rent_dialog, "delete_button")
    assert not hasattr(rent_dialog, "restore_button")
    window.refresh()
    select_bill(window.charges_table, rent["id"])
    window.delete_selected_bill()
    assert len(errors) == 1 and "cannot be deleted" in str(errors.pop())
    misc = ledger.add_charge(tenant, "misc", 200, today, today, today, "Repair")
    ledger.void_charge(misc, "Wrong bill")
    bill = ledger.get_bill(misc)
    for archived, locked in ((False, False), (True, False), (False, True)):
        dialog = BillDialog(dict(bill, payment_locked=locked), lambda: None, lambda: None,
                            archived=archived, undo_cancel=lambda: None, delete_bill=lambda: None)
        qtbot.addWidget(dialog)
        assert dialog.restore_button.isEnabled() == (not archived and not locked)
        assert dialog.delete_button.isEnabled() == (not archived and not locked)


def test_undo_menu_requires_cancelled_bill_not_cycle_heading(account):
    window, ledger, tenant, today, errors = account
    window.charges_table.selectRow(0)
    assert not window.undo_bill_action.isEnabled()
    assert not window.delete_bill_action.isEnabled()
    window.undo_cancelled_bill()
    window.delete_selected_bill()
    assert not errors


def test_future_cancellation_can_be_undone_from_bill_details(account, qtbot, monkeypatch):
    window, ledger, tenant, today, errors = account
    rent = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "rent")
    ledger.post_charge(rent["id"], 100_000)
    cancellation = (today_ist() + timedelta(days=5)).isoformat()
    cancel_historical_bill(ledger, rent["id"], "Scheduled correction", cancellation, prepare_replacement=True)
    window.refresh()
    select_bill(window.charges_table, rent["id"])
    assert window.undo_bill_action.isEnabled()
    assert window.charges_table.item(window.charges_table.currentRow(), 2).text() == "Cancellation scheduled"
    charge = ledger.get_bill(rent["id"])
    assert charge["status"] == "posted"
    dialog = BillDialog(charge, lambda: None, lambda: None, undo_cancel=window.undo_cancelled_bill)
    qtbot.addWidget(dialog)
    assert dialog.restore_button.isEnabled()
    assert not dialog.paid_button.isEnabled()
    monkeypatch.setattr(BillCorrectionDialog, "exec", accept_reason)
    dialog.restore_button.click()
    assert not errors
    restored = ledger.get_bill(selected_id(window.charges_table))
    assert restored["id"] != rent["id"]
    assert restored["charge_date"] == cancellation
    assert ledger.balance(tenant)["outstanding_paise"] == 100_000


def test_manual_misc_replacement_blocks_undo_of_original(account, monkeypatch):
    window, ledger, tenant, today, errors = account
    original = ledger.add_charge(tenant, "misc", 5_000, today, today, today, "Original repair fee")
    ledger.void_charge(original, "Wrong amount")
    window.refresh()
    select_bill(window.charges_table, original)

    def replace(dialog):
        assert dialog.items.cellWidget(0, 0).currentData() == "misc"
        assert not dialog.items.cellWidget(0, 0).isEnabled()
        dialog.items.cellWidget(0, 1).set_paise(3_000)
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ChargeDialog, "exec", replace)
    window.replace_cancelled_charge()
    assert not errors
    replacement = ledger.get_bill(selected_id(window.charges_table))
    assert replacement["replaces_charge_id"] == original
    assert replacement["amount_paise"] == 3_000
    select_bill(window.charges_table, original)
    monkeypatch.setattr(BillCorrectionDialog, "exec", accept_reason)
    window.undo_cancelled_bill()
    assert len(errors) == 1 and "replacement" in str(errors.pop()).lower()
    assert ledger.balance(tenant)["outstanding_paise"] == 3_000
    assert len([bill for bill in ledger.list_charges(tenant)
                if bill["kind"] == "misc" and bill["status"] != "void"]) == 1


def test_old_unlinked_fee_requires_review_before_undo(account, monkeypatch):
    window, ledger, tenant, today, errors = account
    original = ledger.add_charge(tenant, "misc", 5_000, today, today, today, "Repair")
    ledger.void_charge(original, "Wrong amount")
    # Earlier releases created manual miscellaneous replacements without links.
    ledger.add_charge(tenant, "misc", 3_000, today, today, today, "Repair correction")
    window.refresh()
    select_bill(window.charges_table, original)
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: prompts.append(args[2]) or QMessageBox.StandardButton.No)
    monkeypatch.setattr(BillCorrectionDialog, "exec", accept_reason)
    window.undo_cancelled_bill()
    assert prompts and "Repair correction" in prompts[0] and "separate fees" in prompts[0]
    assert ledger.balance(tenant)["outstanding_paise"] == 3_000
    assert ledger.get_bill(original)["status"] == "void"
    assert not errors
