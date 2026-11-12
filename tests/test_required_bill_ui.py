"""Required bills retain a reviewed amount, including zero, without cancellation."""

from datetime import timedelta

import pytest
from PySide6.QtWidgets import QDialog, QLabel

from grihalekha.clock import today_ist
from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillAmountDialog, BillDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.widgets import selected_id
from grihalekha.ui.window import MainWindow


@pytest.fixture
def account(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    today = today_ist().isoformat()
    tenancy = ledger.add_tenancy(flat, "Tenant", today, today_ist().day, 100_000)
    errors = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, error: errors.append(error))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(tenancy)
    yield window, ledger, tenancy, today, errors
    window.close()
    ledger.close()


@pytest.mark.parametrize("kind", ["rent", "electricity"])
def test_correct_paid_bill_to_zero_retains_money_receipt_and_selected_bill(account, monkeypatch, kind):
    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == kind)
    ledger.post_charge(bill["id"], 10_000)
    ledger.deposit_payment(tenancy, 10_000, today, "cash")
    ledger.mark_bill_paid(bill["id"])
    window.refresh()
    select_bill(window.charges_table, bill["id"])

    def accept(dialog):
        assert dialog.amount.paise() == 10_000
        assert "available balance" in " ".join(item.text() for item in dialog.findChildren(QLabel))
        dialog.amount.set_paise(0)
        dialog.reason.setText("No amount due by agreement")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(BillAmountDialog, "exec", accept)
    window.correct_selected_bill_amount()
    assert not errors
    corrected = ledger.get_bill(selected_id(window.charges_table))
    assert corrected["id"] != bill["id"]
    assert corrected["replaces_charge_id"] == bill["id"]
    assert corrected["amount_paise"] == corrected["outstanding_paise"] == 0
    assert corrected["status"] == "posted"
    assert ledger.balance(tenancy)["credit_paise"] == 10_000
    assert len(ledger.list_payments(tenancy)) == 1


def test_dismissed_correction_keeps_original_bill_and_balance(account, monkeypatch):
    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "rent")
    ledger.post_charge(bill["id"])
    window.refresh()
    select_bill(window.charges_table, bill["id"])
    before = ledger.get_revision()
    monkeypatch.setattr(BillAmountDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window.correct_selected_bill_amount()
    assert not errors
    assert ledger.get_revision() == before
    assert ledger.get_bill(bill["id"])["amount_paise"] == 100_000


@pytest.mark.parametrize("archived,locked", [(False, False), (True, False), (False, True)])
def test_bill_details_only_enable_amount_correction_for_editable_required_bills(account, qtbot, archived, locked):
    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "electricity")
    ledger.post_charge(bill["id"], 10_000)
    calls = []
    charge = dict(ledger.get_bill(bill["id"]), payment_locked=locked)
    dialog = BillDialog(charge, lambda: None, lambda: None, archived=archived,
                        correct_amount=lambda: calls.append("correct"), delete_bill=lambda: None)
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "delete_button")
    assert dialog.correct_button.isEnabled() == (not archived and not locked)
    dialog.correct_button.click()
    assert calls == (["correct"] if not archived and not locked else [])
    assert not errors


def test_future_bill_can_be_corrected_but_not_paid_early(account, qtbot):
    window, ledger, tenancy, today, errors = account
    charge = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "rent")
    ledger.post_charge(charge["id"])
    charge = ledger.get_bill(charge["id"])
    charge["charge_date"] = (today_ist() + timedelta(days=1)).isoformat()
    dialog = BillDialog(charge, lambda: None, lambda: None, balance_paise=100_000, correct_amount=lambda: None)
    qtbot.addWidget(dialog)
    assert dialog.correct_button.isEnabled()
    assert not dialog.paid_button.isEnabled()


def test_amount_dialog_requires_explicit_nonnegative_amount_and_reason(account, qtbot):
    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "rent")
    dialog = BillAmountDialog(bill)
    qtbot.addWidget(dialog)
    dialog.amount.set_paise(0)
    with pytest.raises(ValueError, match="Reason"):
        dialog.validate()
    dialog.reason.setText("Nothing owed")
    assert dialog.validate() == {"amount_paise": 0, "reason": "Nothing owed"}
    dialog.amount.clear()
    with pytest.raises(ValueError):
        dialog.validate()


def test_direct_required_bill_cancellation_is_rejected_without_prompt(account, monkeypatch):
    from grihalekha.ui.dialogs import ReasonDialog

    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "rent")
    window.refresh()
    select_bill(window.charges_table, bill["id"])
    before = ledger.get_revision()
    monkeypatch.setattr(ReasonDialog, "exec", lambda self: pytest.fail("Required cancellation must not open a confirmation"))
    window.cancel_charge()
    assert len(errors) == 1 and "cannot be cancelled" in str(errors.pop())
    assert ledger.get_revision() == before


def test_bill_menu_tracks_required_bill_status_and_cycle_lock(account):
    window, ledger, tenancy, today, errors = account
    bill = next(row for row in ledger.list_charges(tenancy) if row["kind"] == "rent")
    select_bill(window.charges_table, bill["id"])
    assert not window.cancel_bill_action.isEnabled()
    assert window.correct_bill_action.isEnabled()  # Draft amount can be reviewed.
    ledger.post_charge(bill["id"])
    window.refresh()
    select_bill(window.charges_table, bill["id"])
    assert window.correct_bill_action.isEnabled()
    assert not window.cancel_bill_action.isEnabled()
    window.toggle_cycle_lock()
    assert not window.correct_bill_action.isEnabled()
    window.toggle_cycle_lock()
    misc = ledger.add_charge(tenancy, "misc", 200, today, today, today, "Optional repair")
    window.refresh()
    select_bill(window.charges_table, misc)
    assert window.cancel_bill_action.isEnabled()
    assert window.correct_bill_action.isEnabled()  # Unpaid miscellaneous correction.
    window.charges_table.selectRow(0)
    assert not window.cancel_bill_action.isEnabled()
    assert not window.correct_bill_action.isEnabled()
    assert not errors

