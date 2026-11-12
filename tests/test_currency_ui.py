"""Display currencies stay local to an account window and preserve input values."""

from datetime import datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from grihalekha import clock
from grihalekha.backups import BackupManager
from grihalekha.currencies import CURRENCIES
from grihalekha.ledger import Ledger
from grihalekha.ui.bill_dialogs import BillAmountDialog, BillDialog, BillPaymentDialog
from grihalekha.ui.dialogs import (
    AmountActionDialog, BackupImportDialog, ChargeDialog, MoveOutDialog,
    OpeningDialog, PostDraftDialog, RateDialog, TenancyDialog,
)
from grihalekha.ui.lifecycle_dialogs import ExchangeDialog, TransferDialog
from grihalekha.ui.widgets import AmountInput, money, set_rows, table
from grihalekha.ui.window import MainWindow


@pytest.fixture
def currency_window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("Example House", "Example address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    ledger.add_flat(building, "2", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant ₹50 note", "2024-01-01", 1, 123_456_789)
    fee = ledger.add_charge(tenant, "misc", 12_345, "2024-01-01", "2024-01-10",
                            "2024-01-10", "Original note ₹50", charge_date="2024-01-10")
    ledger.deposit_payment(tenant, 2_345, "2024-01-10", "cash")
    errors = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, error: errors.append(error))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.select_account(tenant)
    yield window, tenant, fee, errors
    window.close()


def test_switching_all_currencies_refreshes_existing_accounts_without_changing_values(currency_window):
    window, tenant, fee, errors = currency_window
    ledger = window.ledger
    before = ledger.balance(tenant)
    bills = ledger.list_charges(tenant)
    payments = ledger.list_payments(tenant)
    assert window.currency_combo.currentData() == "INR"
    assert [window.currency_combo.itemData(i) for i in range(window.currency_combo.count())] == [
        currency.code for currency in CURRENCIES
    ]
    for currency in (*CURRENCIES, CURRENCIES[0]):
        window.currency_combo.setCurrentIndex(window.currency_combo.findData(currency.code))
        assert window.currency_code == ledger.get_currency_code() == currency.code
        assert window.account_due.value.text() == f"{currency.symbol}123.45"
        assert window.account_credit.value.text() == f"{currency.symbol}23.45"
        assert window.metric_due.value.text() == f"{currency.symbol}123.45"
        assert f"Monthly rent {currency.symbol}12,34,567.89" in window.account_subtitle.text()
        assert window.rates_table.item(0, 1).text() == f"{currency.symbol}12,34,567.89"
        assert window.payments_table.item(0, 1).text() == f"{currency.symbol}23.45"
        row = next(i for i in range(window.charges_table.rowCount())
                   if window.charges_table.item(i, 0).data(Qt.ItemDataRole.UserRole) == fee)
        assert window.charges_table.item(row, 3).text() == f"{currency.symbol}123.45"
        assert window.charges_table.item(row, 7).text() == "Original note ₹50"
        assert f"{currency.code} {currency.symbol}" in window.currency_footer.text()
        assert ledger.balance(tenant) == before
        assert ledger.list_charges(tenant) == bills
        assert ledger.list_payments(tenant) == payments
    assert not errors


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda item: item.code)
def test_forms_use_selected_currency_and_preserve_exact_decimal_input(currency_window, qtbot, currency):
    window, tenant, fee, errors = currency_window
    window.currency_combo.setCurrentIndex(window.currency_combo.findData(currency.code))
    tenancy = window.ledger.get_tenancy(tenant)
    bill = window.ledger.get_bill(fee)
    draft = next(row for row in window.ledger.list_charges(tenant) if row["kind"] == "electricity")
    dialogs = [
        BillPaymentDialog(window), BillAmountDialog(bill, window),
        TenancyDialog(window._flats, window), PostDraftDialog(draft, window),
        OpeningDialog(window), RateDialog(window), MoveOutDialog(window, tenancy),
        AmountActionDialog("Deposit", window), TransferDialog(tenancy, window._flats, window),
        ExchangeDialog(tenancy, [], window), ChargeDialog(window),
    ]
    for dialog in dialogs:
        qtbot.addWidget(dialog)
        assert dialog.currency_code == currency.code
        for editor in dialog.findChildren(AmountInput):
            assert f"{currency.code} ({currency.symbol})" in editor.toolTip()
            editor.setText("1234567.89")
            assert editor.paise() == 123_456_789
            editor.setText("0.01")
            assert editor.paise() == 1
            editor.setText("1.001")
            with pytest.raises(ValueError, match="two decimal"):
                editor.paise()
        labels = [item.text() for item in dialog.findChildren(QLabel)]
        if isinstance(dialog, ChargeDialog):
            assert dialog.items.horizontalHeaderItem(1).text() == f"Amount ({currency.symbol})"
            dialog.add_item()
            assert currency.code in dialog.items.cellWidget(1, 1).toolTip()
        else:
            assert any(f"({currency.symbol})" in text for text in labels)
    payment = dialogs[0]
    payment.amount.setText("0.01")
    assert payment.validate()["amount_paise"] == 1
    details = BillDialog(bill, lambda: None, lambda: None, window, balance_paise=2_345)
    qtbot.addWidget(details)
    text = "\n".join(item.text() for item in details.findChildren(QLabel))
    assert f"Bill amount: {currency.symbol}123.45" in text
    assert f"Available balance: {currency.symbol}23.45" in text
    assert "Original note ₹50" in text
    assert not errors


def test_currency_is_persisted_and_independent_between_windows(currency_window, qtbot, tmp_path):
    first, _, _, errors = currency_window
    first.currency_combo.setCurrentIndex(first.currency_combo.findData("CAD"))
    second_ledger = Ledger(tmp_path / "second")
    second = MainWindow(second_ledger)
    second._skip_close_backup = True
    qtbot.addWidget(second)
    assert second.currency_code == "INR"
    second.currency_combo.setCurrentIndex(second.currency_combo.findData("JPY"))
    assert first.money(105) == "C$1.05"
    assert second.money(105) == "¥1.05"
    standalone = BillPaymentDialog()
    qtbot.addWidget(standalone)
    assert standalone.currency_code == "INR"
    assert money(105) == "₹1.05"
    parent_dialog = BillPaymentDialog(first)
    nested = BillPaymentDialog(parent_dialog)
    qtbot.addWidget(parent_dialog)
    qtbot.addWidget(nested)
    assert nested.currency_code == "CAD"
    directory = second_ledger.data_dir
    second.close()
    reopened = MainWindow(Ledger(directory))
    reopened._skip_close_backup = True
    qtbot.addWidget(reopened)
    assert reopened.currency_code == reopened.currency_combo.currentData() == "JPY"
    assert reopened.metric_due.value.text() == "¥0.00"
    reopened.close()
    assert not errors


def test_failed_currency_save_restores_selector_and_preserves_display(currency_window, monkeypatch):
    window, _, _, errors = currency_window
    before = window.ledger.get_revision()
    def fail(code):
        raise OSError("Disk write failed")
    monkeypatch.setattr(window.ledger, "set_currency_code", fail)
    window.currency_combo.setCurrentIndex(window.currency_combo.findData("EUR"))
    assert window.currency_combo.currentData() == window.currency_code == "INR"
    assert window.account_due.value.text() == "₹123.45"
    assert window.ledger.get_currency_code() == "INR"
    assert window.ledger.get_revision() == before
    assert len(errors) == 1 and "Disk write failed" in str(errors[0])


@pytest.mark.parametrize("source_code", ["INR", "JPY"])
def test_restoring_backup_reloads_saved_or_default_currency(currency_window, tmp_path, monkeypatch, source_code):
    window, _, _, errors = currency_window
    source = Ledger(tmp_path / "source")
    source.add_building("Imported House", "Address")
    source.set_currency_code(source_code)
    archive = BackupManager(source.data_dir, source.db_path).create(tmp_path / "import.zip")
    source.close()
    window.currency_combo.setCurrentIndex(window.currency_combo.findData("EUR"))
    monkeypatch.setattr(BackupImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    window._restore_from_path(archive)
    symbol = "₹" if source_code == "INR" else "¥"
    assert window.currency_code == window.currency_combo.currentData() == source_code
    assert window.metric_due.value.text() == f"{symbol}0.00"
    assert window.ledger.list_buildings()[0]["name"] == "Imported House"
    assert not errors


@pytest.mark.parametrize("currency", CURRENCIES, ids=lambda item: item.code)
def test_currency_amounts_remain_right_aligned(qtbot, currency):
    view = table(["Amount"])
    qtbot.addWidget(view)
    set_rows(view, [(money(105, currency_code=currency.code),), (money(-105, currency_code=currency.code),)])
    for row in (0, 1):
        assert view.item(row, 0).textAlignment() & Qt.AlignmentFlag.AlignRight


def test_failed_restore_retains_current_currency_and_reopened_ledger(currency_window, tmp_path, monkeypatch):
    window, _, _, errors = currency_window
    source = Ledger(tmp_path / "source")
    source.set_currency_code("JPY")
    archive = BackupManager(source.data_dir, source.db_path).create(tmp_path / "import.zip")
    source.close()
    window.currency_combo.setCurrentIndex(window.currency_combo.findData("AUD"))
    previous = window.ledger
    monkeypatch.setattr(BackupImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    def fail(path):
        raise OSError("Disk write failed")
    monkeypatch.setattr(window.backups, "restore", fail)
    window._restore_from_path(archive)
    assert window.ledger is not previous
    assert window.ledger.get_currency_code() == window.currency_code == "AUD"
    assert window.currency_combo.currentData() == "AUD"
    assert window.account_due.value.text() == "A$123.45"
    assert len(errors) == 1 and "Disk write failed" in str(errors[0])
