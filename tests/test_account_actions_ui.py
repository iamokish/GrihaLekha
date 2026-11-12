"""Exercise new account actions through the real dialogs and window callbacks."""

from datetime import datetime

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from grihalekha import clock
from grihalekha.ledger import Ledger
from grihalekha.reports import export_csv, statement_html
from grihalekha.ui.bill_dialogs import BillAmountDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.dialogs import AmountActionDialog, PostDraftDialog
from grihalekha.ui.lifecycle_dialogs import CancelUpcomingTenantDialog
from grihalekha.ui.window import MainWindow, _tenancy_status


@pytest.fixture
def workspace(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    other = ledger.add_flat(building, "2", "Ground", 1)
    current = ledger.add_tenancy(flat, "Current", "2024-01-01", 1, 10_000)
    upcoming = ledger.add_tenancy(other, "Upcoming", "2024-02-01", 1, 20_000)
    ledger.prepare_monthly_drafts("2024-02-01")
    errors = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, error: errors.append(error))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(current)
    yield window, ledger, current, upcoming, other, errors
    window.close()
    ledger.close()


def test_balance_to_deposit_dialog_transfers_existing_money(workspace, monkeypatch):
    window, ledger, current, _, _, errors = workspace
    ledger.deposit_payment(current, 50_000, "2024-01-10", "bank")
    window.refresh()
    def accept(dialog):
        assert dialog.windowTitle() == "Transfer balance to security deposit"
        assert dialog.method.isHidden()
        assert dialog.reason_required
        dialog.amount.set_paise(12_345)
        dialog.reason.setText("Set aside agreed deposit")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(AmountActionDialog, "exec", accept)
    window.deposit_action("from_balance")
    assert not errors
    assert ledger.balance(current)["credit_paise"] == 37_655
    assert ledger.balance(current)["deposit_paise"] == 12_345
    assert len(ledger.list_payments(current)) == 1
    assert any(item["record"].get("action") == "from_balance" for item in window._movements)


@pytest.mark.parametrize("accepted,amount", [(False, 2_000), (True, 99_999)])
def test_dismissed_or_unfunded_transfer_keeps_money(workspace, monkeypatch, accepted, amount):
    window, ledger, current, _, _, errors = workspace
    ledger.deposit_payment(current, 5_000, "2024-01-10", "cash")
    before = ledger.get_revision()
    def respond(dialog):
        dialog.amount.set_paise(amount)
        dialog.reason.setText("Deposit")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    monkeypatch.setattr(AmountActionDialog, "exec", respond)
    window.deposit_action("from_balance")
    assert ledger.get_revision() == before
    assert ledger.balance(current)["credit_paise"] == 5_000
    assert ledger.balance(current)["deposit_paise"] == 0
    assert bool(errors) == accepted


def test_correct_unpaid_misc_and_review_draft_from_same_menu(workspace, monkeypatch):
    window, ledger, current, _, _, errors = workspace
    fee = ledger.add_charge(current, "misc", 1_234, "2024-01-01", "2024-01-31", "2024-01-31", "Repair")
    window.refresh()
    select_bill(window.charges_table, fee)
    assert window.correct_bill_action.isEnabled()
    def correct(dialog):
        dialog.amount.set_paise(567)
        dialog.reason.setText("Typing mistake")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(BillAmountDialog, "exec", correct)
    window.correct_selected_bill_amount()
    revised = next(row for row in ledger.list_charges(current) if row.get("replaces_charge_id") == fee)
    assert revised["amount_paise"] == revised["outstanding_paise"] == 567
    assert ledger.get_bill(fee)["amount_paise"] == 1_234
    draft = next(row for row in ledger.list_charges(current) if row["kind"] == "rent" and row["status"] == "draft")
    select_bill(window.charges_table, draft["id"])
    def review(dialog):
        dialog.amount.set_paise(8_000)
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(PostDraftDialog, "exec", review)
    window.correct_selected_bill_amount()
    assert ledger.get_bill(draft["id"])["amount_paise"] == 8_000
    assert not errors


@pytest.mark.parametrize("accept_reason,confirm", [(False, True), (True, False), (True, True)])
def test_cancel_upcoming_confirmation_preserves_history_and_releases_flat(workspace, monkeypatch, accept_reason, confirm):
    window, ledger, current, upcoming, flat, errors = workspace
    assert not window.cancel_upcoming_action.isEnabled()
    window.select_account(upcoming)
    assert window.cancel_upcoming_action.isEnabled()
    before = ledger.get_revision()
    def respond(dialog):
        with pytest.raises(ValueError):
            dialog.validate()
        dialog.reason.setText("Booking cancelled by tenant")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted if accept_reason else QDialog.DialogCode.Rejected
    monkeypatch.setattr(CancelUpcomingTenantDialog, "exec", respond)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes if confirm else QMessageBox.StandardButton.No)
    window.cancel_upcoming_tenant()
    assert not errors
    tenant = ledger.get_tenancy(upcoming)
    if not (accept_reason and confirm):
        assert ledger.get_revision() == before
        assert tenant["cancelled_on"] is None
        return
    assert _tenancy_status(tenant) == "Cancelled"
    assert tenant["cancellation_reason"] == "Booking cancelled by tenant"
    assert "Cancelled" in window.account_subtitle.text()
    assert not window.cancel_upcoming_action.isEnabled()
    assert not window._charges
    assert window.archive_action.isEnabled()
    assert all(not action.isEnabled() for action in window._occupancy_actions)
    replacement = ledger.add_tenancy(flat, "Replacement", "2024-02-01", 1, 30_000)
    assert replacement != upcoming
    window.archive_account()
    assert ledger.get_tenancy(upcoming)["archived_on"] == "2024-01-15"
    window.show_archived.setChecked(True)
    window.select_account(upcoming)
    window.reopen_account()
    assert ledger.get_tenancy(upcoming)["archived_on"] is None
    assert ledger.get_tenancy(upcoming)["cancelled_on"] == "2024-01-15"
    assert not errors


def test_cancelled_account_reports_identify_cancellation_and_escape_reason(workspace, tmp_path):
    _, ledger, _, upcoming, _, _ = workspace
    ledger.cancel_upcoming_tenancy(upcoming, '<script>alert(1)</script> & booking cancelled')
    snapshot = ledger.statement(upcoming, "2024-01-01", "2024-03-01")
    html = statement_html(snapshot, "bilingual")
    assert "Cancelled upcoming tenancy" in html
    assert "आगामी किरायेदारी रद्द" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    paths = export_csv(snapshot, tmp_path / "csv")
    assert all("cancelled_on" in path.read_text(encoding="utf-8-sig").splitlines()[0] for path in paths)
