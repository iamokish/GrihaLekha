"""Building selection and dropdown geometry must match the visible controls."""

from datetime import date

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QComboBox, QDialog, QStyle, QStyleOptionComboBox, QVBoxLayout, QWidget

from grihalekha.ledger import Ledger
from grihalekha.paths import asset_dir
from grihalekha.ui.dialogs import FlatDialog
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.widgets import selected_id, table
from grihalekha.ui.window import MainWindow


@pytest.fixture(autouse=True)
def application_theme(qapp):
    apply_theme(qapp)
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))
    qapp.setFont(QFont("Noto Sans", 10))


@pytest.fixture
def property_window(qtbot, tmp_path):
    ledger = Ledger(tmp_path)
    first = ledger.add_building("First house", "First address")
    second = ledger.add_building("Second house", "Second address")
    ledger.add_flat(first, "101", "Ground", 1)
    ledger.add_flat(first, "102", "First", 2)
    ledger.add_flat(second, "201", "Ground", 1)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.navigate(1)
    window.show()
    qtbot.waitExposed(window)
    yield window, first, second
    window.close()
    ledger.close()


def _flat_numbers(window):
    return [window.flats_table.item(row, 1).text() for row in range(window.flats_table.rowCount())]


def _select_building(window, building_id):
    row = next(i for i, building in enumerate(window._buildings) if building["id"] == building_id)
    window.buildings_table.selectRow(row)


def test_building_selection_filters_flats_and_survives_refresh(property_window):
    window, first, second = property_window
    assert _flat_numbers(window) == ["101", "102", "201"]
    _select_building(window, second)
    assert window.flat_filter.currentData() == second
    assert _flat_numbers(window) == ["201"]
    window.ledger.add_flat(second, "202", "First", 2)
    window.refresh()
    assert selected_id(window.buildings_table) == second
    assert window.flat_filter.currentData() == second
    assert _flat_numbers(window) == ["201", "202"]
    _select_building(window, first)
    assert _flat_numbers(window) == ["101", "102"]


def test_dropdown_and_top_table_share_the_building_filter(property_window):
    window, first, second = property_window
    window.flat_filter.setCurrentIndex(window.flat_filter.findData(second))
    assert selected_id(window.buildings_table) == second
    assert len(window.buildings_table.selectedIndexes()) == window.buildings_table.columnCount()
    assert _flat_numbers(window) == ["201"]
    window.flat_filter.setCurrentIndex(window.flat_filter.findData(None))
    assert selected_id(window.buildings_table) is None
    assert not window.buildings_table.selectedIndexes()
    assert _flat_numbers(window) == ["101", "102", "201"]
    window.refresh()
    assert window.flat_filter.currentData() is None
    assert not window.buildings_table.selectedIndexes()


def test_empty_or_removed_selected_building_clears_stale_flats(property_window):
    window, first, second = property_window
    empty = window.ledger.add_building("Empty house", "Address")
    window.refresh()
    _select_building(window, empty)
    assert window.flats_table.rowCount() == 0
    window.ledger.remove_building(empty)
    window.refresh()
    assert window.flat_filter.currentData() is None
    assert selected_id(window.buildings_table) is None
    assert _flat_numbers(window) == ["101", "102", "201"]


def test_filtered_flat_opens_correct_account_and_add_uses_selected_building(property_window, monkeypatch):
    window, first, second = property_window
    second_flat = next(flat for flat in window._flats if flat["building_id"] == second)
    tenancy = window.ledger.add_tenancy(second_flat["id"], "Second tenant", date.today().isoformat(), 1, 0)
    window.refresh()
    _select_building(window, second)
    window.flats_table.selectRow(0)
    window._open_flat_account(0, 0)
    assert window._selected_tenancy == tenancy
    window.navigate(1)
    selected = []

    def add(dialog):
        selected.append(dialog.building.currentData())
        dialog.number.setText("202")
        dialog.floor.setText("First")
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FlatDialog, "exec", add)
    window.add_flat()
    assert selected == [second]
    assert _flat_numbers(window) == ["201", "202"]


@pytest.mark.parametrize("kind", ["regular", "editable", "inline"])
def test_dropdown_popup_uses_one_frame_and_keeps_selection_working(qapp, qtbot, kind):
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    combo = QComboBox()
    combo.addItems(["Electricity", "Miscellaneous", "Rent replacement"])
    if kind == "editable":
        combo.setEditable(True)
    if kind == "inline":
        view = table(["Charge type", "Reason"])
        view.setRowCount(1)
        view.setCellWidget(0, 0, combo)
        layout.addWidget(view)
    else:
        layout.addWidget(combo)
    host.resize(560, 220)
    host.show()
    qtbot.waitExposed(host)
    qapp.processEvents()
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    arrow = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxArrow, combo)
    edit = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo)
    assert combo.rect().contains(arrow) and combo.rect().contains(edit)
    assert arrow.width() >= 24 and not arrow.intersects(edit)
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton, pos=arrow.center())
    qtbot.waitUntil(combo.view().isVisible)
    qapp.processEvents()
    popup = combo.view().window()
    # Menu-style popup margins formerly exposed strips outside the view's
    # border. The framed view must occupy its entire popup container.
    assert combo.view().mapTo(popup, QPoint(0, 0)) == QPoint(0, 0)
    assert combo.view().size() == popup.size()
    image = popup.grab().toImage()
    mid_x = image.width() // 2
    assert image.pixelColor(mid_x, 0) == image.pixelColor(0, image.height() // 2)
    qtbot.wait(qapp.doubleClickInterval() + 50)
    target = combo.view().visualRect(combo.model().index(1, 0)).center()
    qtbot.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=target)
    qtbot.waitUntil(lambda: not combo.view().isVisible())
    assert combo.currentIndex() == 1
    combo.setFocus()
    qtbot.keyClick(combo, Qt.Key.Key_Down)
    assert combo.currentIndex() == 2
