"""Departure cycles remain selectable when the account was entered much later."""

from datetime import datetime

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtWidgets import QTableView

from grihalekha import clock
from grihalekha.ui.dialogs import MoveOutDialog
from grihalekha.ui.theme import apply_theme


@pytest.fixture
def move_out_dialog(qapp, qtbot, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2026, 11, 7, 12, tzinfo=clock.IST))
    apply_theme(qapp)

    def make(**changes):
        tenancy = {
            "move_in": "2024-01-15",
            "opening_date": "2024-01-15",
            "billing_day": 5,
            "move_out": None,
            "occupancy_history": [],
        }
        tenancy.update(changes)
        dialog = MoveOutDialog(tenancy=tenancy)
        qtbot.addWidget(dialog)
        return dialog

    return make


def test_past_cycle_before_account_opening_retains_actual_departure(move_out_dialog):
    dialog = move_out_dialog(opening_date="2026-11-07")
    dialog.rent.set_paise(25_000)
    assert dialog.when.minimumDate() == QDate(2024, 1, 15)
    index = dialog.cycle.findData("2026-07-05")
    assert index >= 0
    dialog.cycle.setCurrentIndex(index)
    assert dialog.when.iso() == "2026-08-04"
    assert not dialog.rent.isEnabled()
    assert dialog.rent.paise() == 0
    assert dialog.validate()["final_rent_paise"] == 0
    assert "opening balances" in dialog.cycle_hint.text()

    dialog.when.set_iso("2026-11-07")
    assert dialog.rent.isEnabled()
    assert dialog.rent.paise() == 25_000
    assert dialog.cycle.currentData() == "2026-11-05"


def test_selecting_past_day_in_real_calendar_updates_cycle(move_out_dialog, qtbot):
    dialog = move_out_dialog(opening_date="2026-11-07")
    dialog.show()
    qtbot.waitExposed(dialog)
    field = dialog.when
    qtbot.mouseClick(field, Qt.MouseButton.LeftButton,
                     pos=QPoint(field.width() - 15, field.height() // 2))
    calendar = field.calendarWidget()
    qtbot.waitUntil(calendar.isVisible)
    calendar.setCurrentPage(2026, 7)
    view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    cells = [view.model().index(row, column)
             for row in range(1, view.model().rowCount())
             for column in range(view.model().columnCount())
             if str(view.model().index(row, column).data()) == "15"]
    assert len(cells) == 1
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=view.visualRect(cells[0]).center())
    assert field.iso() == "2026-07-15"
    assert dialog.cycle.currentData() == "2026-07-05"
    qtbot.waitUntil(lambda: not calendar.isVisible())
    assert dialog.validate()["move_out"] == "2026-07-15"


def test_cycle_anchor_31_survives_leap_february(move_out_dialog):
    dialog = move_out_dialog(billing_day=31)
    dialog.cycle.setCurrentIndex(dialog.cycle.findData("2024-01-31"))
    assert dialog.when.iso() == "2024-02-28"
    dialog.cycle.setCurrentIndex(dialog.cycle.findData("2024-02-29"))
    assert dialog.when.iso() == "2024-03-30"
    dialog.when.set_iso("2024-02-29")
    assert dialog.cycle.currentData() == "2024-02-29"
    assert dialog.when.iso() == "2024-02-29"


def test_existing_move_out_and_future_extension_stay_in_sync(move_out_dialog):
    dialog = move_out_dialog(move_out="2026-07-19")
    assert dialog.when.iso() == "2026-07-19"
    assert dialog.cycle.currentData() == "2026-07-05"
    dialog.when.set_iso("2027-02-14")
    assert dialog.cycle.currentData() == "2027-02-05"
    assert dialog.when.iso() == "2027-02-14"
    dialog.cycle.setCurrentIndex(dialog.cycle.findData("2026-06-05"))
    dialog.rent.set_paise(0)
    assert dialog.validate()["move_out"] == "2026-07-04"
    assert dialog.validate()["final_rent_paise"] == 0


def test_move_in_and_latest_transfer_still_limit_departure(move_out_dialog):
    dialog = move_out_dialog(occupancy_history=[{"starts_on": "2026-06-19"}])
    assert dialog.when.minimumDate() == QDate(2026, 6, 19)
    assert dialog.cycle.findData("2026-05-05") == -1
    assert dialog.cycle.itemData(0) == "2026-06-05"
    dialog.when.set_iso("2026-06-01")
    assert dialog.when.iso() == "2026-06-19"


def test_existing_departure_before_opening_needs_no_final_rent(move_out_dialog):
    dialog = move_out_dialog(move_out="2026-07-19", opening_date="2026-11-07")
    assert dialog.when.iso() == "2026-07-19"
    assert not dialog.rent.isEnabled()
    assert dialog.validate()["final_rent_paise"] == 0
