"""Integration checks for decimal input, selective payments and the desktop ledger."""

from datetime import date, timedelta

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from grihalekha.ledger import Ledger
from grihalekha.paths import asset_dir
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.bill_dialogs import BillPaymentDialog
from grihalekha.ui.bill_table import select_bill
from grihalekha.ui.dialogs import (
    BuildingDialog,
    BackupImportDialog,
    ChargeDialog,
    FlatDialog,
    HouseholdDialog,
    PostDraftDialog,
    ReasonDialog,
)
from grihalekha.ui.widgets import AmountInput, money
from grihalekha.ui.window import MainWindow
from grihalekha.backups import BackupManager
from legacy_ledger import cancel_historical_bill


@pytest.fixture(autouse=True)
def application_fonts(qapp):
    # Match the actual entry point, including a font-less/offscreen test host.
    apply_theme(qapp)
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))
    qapp.setFont(QFont("Noto Sans", 10))


def test_amount_editor_keeps_paise_and_rejects_rounding(qtbot):
    editor = AmountInput()
    qtbot.addWidget(editor)
    editor.setText("12500.05")
    assert editor.paise() == 1_250_005
    assert money(editor.paise()) == "₹12,500.05"
    assert money(123456789) == "₹12,34,567.89"
    for invalid in ("1.001", "1e3", "NaN", "-1", "1,000.00", ""):
        editor.setText(invalid)
        with pytest.raises(ValueError):
            editor.paise()


def test_property_removal_confirm_cancel_and_protected_accounts(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    building = ledger.add_building("Example", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.flats_table.selectRow(0)
    window.remove_flat()
    assert len(ledger.list_flats()) == 1
    tenant = ledger.add_tenancy(flat, "Tenant", date.today().isoformat(), 1, 0)
    window.refresh()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.flats_table.selectRow(0)
    window.remove_flat()
    assert "cannot be removed" in messages[-1]
    assert ledger.get_tenancy(tenant)
    empty = ledger.add_building("Unused", "Address")
    spare = ledger.add_flat(empty, "2", "Ground", 1)
    window.refresh()
    window.flats_table.selectRow(next(i for i, f in enumerate(window._flats) if f["id"] == spare))
    window.remove_flat()
    assert [f["id"] for f in ledger.list_flats()] == [flat]
    window.buildings_table.selectRow(next(i for i, b in enumerate(window._buildings) if b["id"] == empty))
    window.remove_building()
    assert [b["id"] for b in ledger.list_buildings()] == [building]
    window.close()


@pytest.mark.parametrize("accept", [False, True])
def test_backup_import_reviews_archive_and_can_restore_previous_workspace(qtbot, tmp_path, monkeypatch, accept):
    source = Ledger(tmp_path / "source")
    source.add_building("Imported House", "Other address")
    documents = source.data_dir / "documents"
    documents.mkdir(exist_ok=True)
    (documents / "issued.pdf").write_bytes(b"%PDF-1.4 fixture")
    archive = BackupManager(source.data_dir, source.db_path).create(tmp_path / "transfer.zip")
    source.close()
    ledger = Ledger(tmp_path / "current")
    ledger.add_building("Current House", "Current address")
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(archive), ""))
    previews, errors = [], []
    def review(dialog):
        previews.append(dialog)
        assert window._restoring
        window._automatic_backup()
        assert window._backup_thread is None
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected
    monkeypatch.setattr(BackupImportDialog, "exec", review)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(window, "_error", errors.append)
    window.restore_backup()
    assert len(previews) == 1 and not errors
    assert not window._restoring
    assert window.ledger.list_buildings()[0]["name"] == ("Imported House" if accept else "Current House")
    if accept:
        assert (window.ledger.data_dir / "documents" / "issued.pdf").is_file()
        assert len(window.backups.list_backups()) == 1
        window.backups_table.selectRow(0)
        window.restore_selected_backup()
        assert window.ledger.list_buildings()[0]["name"] == "Current House"
        assert not (window.ledger.data_dir / "documents" / "issued.pdf").exists()
        assert not errors
    window.close()


def test_failed_import_leaves_current_ledger_usable(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "current")
    ledger.add_building("Current House", "Address")
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    archive = window.backups.create(tmp_path / "backup.zip")
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(BackupImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    def fail_restore(path):
        raise OSError("Disk write failed")
    monkeypatch.setattr(window.backups, "restore", fail_restore)
    window._restore_from_path(archive)
    assert len(errors) == 1
    assert not window._restoring
    window.ledger.add_building("Still writable", "Address")
    assert len(window.ledger.list_buildings()) == 2
    window.close()


def test_payment_editor_records_receipt_without_bill_allocation(qtbot):
    dialog = BillPaymentDialog()
    qtbot.addWidget(dialog)
    dialog.amount.setText("10000.00")
    values = dialog.validate()
    assert values["amount_paise"] == 1_000_000
    assert "allocations" not in values
    assert not hasattr(dialog, "allocation_mode")


def test_main_window_renders_real_balances_and_family(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    building = ledger.add_building("कृष्णा निवास", "12 Example Road")
    flat = ledger.add_flat(building, "201", "2", 2)
    today = date.today()
    beginning = today.replace(day=1).isoformat()
    tenancy = ledger.add_tenancy(
        flat,
        "अमित कुमार",
        beginning,
        1,
        1_000_000,
        members=[{"name": "सीमा", "relationship": "Spouse"}],
    )
    ledger.prepare_rent_drafts()
    draft = next(c for c in ledger.list_charges(tenancy) if c["kind"] == "rent")
    ledger.post_charge(draft["id"])
    ledger.add_charge(
        tenancy, "electricity", 80_000, beginning, today.isoformat(), today.isoformat()
    )
    ledger.record_payment(
        tenancy,
        1_000_000,
        today.isoformat(),
        "upi",
        allocations={draft["id"]: 1_000_000},
    )
    failures = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, exc: failures.append(exc))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.select_account(tenancy)
    qtbot.waitExposed(window)
    qtbot.wait(30)
    assert not failures
    assert window.account_due.value.text() == "₹800.00"
    assert window.account_credit.value.text() == "₹0.00"
    assert window.family_table.item(0, 0).text() == "सीमा"
    assert window.payments_table.rowCount() == 1
    assert window.charges_table.rowCount() == 3  # one cycle heading and two bills
    assert window.account_combo.count() == 1
    assert window.grab().save(str(tmp_path / "account-preview.png"))
    window.close()


def test_desktop_add_building_and_flats_without_fixed_limit(
    qtbot, tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path)
    failures = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, exc: failures.append(exc))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)

    def building_input(dialog):
        dialog.name.setText("Lotus House")
        dialog.address.setPlainText("24 Main Road")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(BuildingDialog, "exec", building_input)
    window.add_building()
    assert len(ledger.list_buildings()) == 1

    def flat_input(dialog):
        dialog.number.setText(str(101 + len(ledger.list_flats())))
        dialog.floor.setText("1")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FlatDialog, "exec", flat_input)
    for _ in range(12):
        window.add_flat()
    assert len(ledger.list_flats()) == 12
    assert window.flats_table.rowCount() == 12
    assert not failures
    window.close()


def test_backup_completion_does_not_clear_new_document_changes(qtbot, tmp_path):
    ledger = Ledger(tmp_path)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window._backup_capture = (ledger.get_revision(), 0, True)
    window._document_revision = 1  # An issued PDF arrived after the snapshot began.
    window._dirty = True
    window._backup_finished(str(tmp_path / "backup.zip"))
    assert window._dirty is True
    window.close()


def test_closing_changed_window_waits_for_verified_backup(qtbot, tmp_path):
    ledger = Ledger(tmp_path)
    window = MainWindow(ledger)
    qtbot.addWidget(window)
    window.show()
    ledger.add_building("Backup House", "Test address")
    window.close()
    qtbot.waitUntil(
        lambda: window._backup_thread is None and not window.isVisible(), timeout=15000
    )
    backups = window.backups.list_backups()
    assert len(backups) == 1
    assert window.backups.validate(backups[0])


def test_failed_close_backup_keeps_window_open_when_owner_declines(
    qtbot, tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path)
    window = MainWindow(ledger)
    qtbot.addWidget(window)
    window.show()
    ledger.add_building("Protected House", "Test address")
    attempts = []

    def fail_backup(destination=None):
        attempts.append(1)
        raise OSError("Backup destination is unavailable")

    monkeypatch.setattr(window.backups, "create", fail_backup)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
    )
    window.close()
    qtbot.waitUntil(lambda: window._backup_thread is None, timeout=10000)
    assert window.isVisible()
    assert not window._close_requested
    assert attempts == [1]
    assert ledger.list_buildings()[0]["name"] == "Protected House"
    window._skip_close_backup = True
    window.close()


def test_household_edit_preserves_long_grace_and_optional_relationship(qtbot):
    tenancy = {
        "id": 3,
        "flat_id": 4,
        "number": "201",
        "building_name": "Example",
        "tenant_name": "अमित",
        "phone": "1234",
        "move_in": date.today().isoformat(),
        "billing_day": 1,
        "rent_paise": 1_000_000,
        "grace_days": 270,
        "members": [{"name": "सीमा", "relationship": ""}],
    }
    dialog = HouseholdDialog(tenancy)
    qtbot.addWidget(dialog)
    dialog.phone.setText("5678")
    values = dialog.validate()
    assert values["grace_days"] == 270
    assert values["members"] == [{"name": "सीमा", "relationship": ""}]
    assert set(values) == {"tenant_name", "phone", "members", "grace_days", "initial_meter_reading_hundredths"}
    assert values["initial_meter_reading_hundredths"] is None


def test_payment_editor_retains_date_and_validates_proof(qtbot, tmp_path):
    dialog = BillPaymentDialog()
    qtbot.addWidget(dialog)
    dialog.amount.setText("100.00")
    paid = (date.today() - timedelta(days=10)).isoformat()
    dialog.paid.set_iso(paid)
    assert dialog.validate()["paid_on"] == paid
    dialog.proof.setText(str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="no longer exists"):
        dialog.validate()
    dialog.method.setCurrentIndex(dialog.method.findData("cash"))
    assert dialog.validate()["proof_path"] is None


def test_refund_and_deposit_corrections_are_available_from_movements(
    qtbot, tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path)
    today = date.today().isoformat()
    building = ledger.add_building("Example", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(flat, "Tenant", today, date.today().day, 0)
    ledger.record_payment(tenancy, 100_000, today, "cash", allocations={})
    refund = ledger.refund_credit(tenancy, 20_000, today, "cash", "Entry to correct")
    deposit = ledger.record_deposit(tenancy, 50_000, today, "receive")
    failures = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, exc: failures.append(exc))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.select_account(tenancy)

    def correction_input(dialog):
        dialog.reason.setText("Incorrect entry")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReasonDialog, "exec", correction_input)
    for source, record_id in (("refunds", refund), ("deposits", deposit)):
        row = next(
            i
            for i, item in enumerate(window._movements)
            if item["source"] == source and item["record"].get("id") == record_id
        )
        window.movements_table.selectRow(row)
        window.reverse_movement()
    balance = ledger.balance(tenancy)
    assert not failures
    assert balance["credit_paise"] == 100_000
    assert balance["deposit_paise"] == 0
    window.close()


def test_future_rent_is_visible_and_cancellable_without_changing_current_rate(
    qtbot, tmp_path, monkeypatch
):
    from grihalekha.dates import cycle_bounds

    ledger = Ledger(tmp_path)
    today = date.today()
    building = ledger.add_building("Example", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(
        flat, "Tenant", today.isoformat(), today.day, 1_000_000
    )
    future = cycle_bounds(today, today.day)[1].isoformat()
    ledger.change_rent(tenancy, future, 1_200_000)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.select_account(tenancy)
    assert "₹10,000.00" in window.account_subtitle.text()
    assert window.rates_table.item(1, 1).text() == "₹12,000.00"
    assert window.rates_table.item(1, 2).text() == "Scheduled"
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
    )
    window.rates_table.selectRow(1)
    window.cancel_future_rate()
    assert len(ledger.get_tenancy(tenancy)["rent_rates"]) == 1
    window.close()


def test_cancelled_rent_can_be_replaced_from_ui(qtbot, tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    today = date.today()
    beginning = today.replace(day=1).isoformat()
    building = ledger.add_building("Example", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(flat, "Tenant", beginning, 1, 1_000_000)
    ledger.prepare_rent_drafts()
    charge = ledger.list_charges(tenancy)[0]
    ledger.post_charge(charge["id"])
    cancel_historical_bill(ledger, charge["id"], "Wrong agreed amount")
    failures = []
    monkeypatch.setattr(MainWindow, "_error", lambda self, exc: failures.append(exc))
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.select_account(tenancy)
    select_bill(window.charges_table, charge["id"])

    def replacement_input(dialog):
        dialog.amount.setText("9500.50")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PostDraftDialog, "exec", replacement_input)
    window.replace_cancelled_charge()
    assert not failures
    assert ledger.balance(tenancy)["outstanding_paise"] == 950_050
    charges = ledger.list_charges(tenancy)
    assert len([row for row in charges if row["kind"] == "rent"]) == 2
    assert any(row["kind"] == "electricity" and row["status"] == "draft" for row in charges)
    window.close()
