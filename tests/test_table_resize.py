"""Real header gestures must track the pointer and survive account refreshes."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QPalette

from grihalekha.ui.dialogs import ChargeDialog
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.widgets import set_rows, table


@pytest.fixture
def resize_application(qapp):
    previous_palette = QPalette(qapp.palette())
    previous_stylesheet = qapp.styleSheet()
    previous_style = qapp.style().objectName()
    previous_font = QFont(qapp.font())
    previous_scheme = qapp.styleHints().colorScheme()
    apply_theme(qapp)
    yield qapp
    qapp.setStyleSheet("")
    qapp.setStyle(previous_style)
    qapp.setPalette(previous_palette)
    qapp.setFont(previous_font)
    qapp.styleHints().setColorScheme(previous_scheme)
    qapp.setStyleSheet(previous_stylesheet)


@pytest.fixture
def resize_table(resize_application, qtbot):
    view = table(["Building", "Flat", "Outstanding balance"])
    qtbot.addWidget(view)
    view.resize(1000, 300)
    set_rows(view, [("Example Road", "101", "100.00")], [1])
    view.show()
    qtbot.waitExposed(view)
    resize_application.processEvents()
    yield view
    view.close()


def _widths(view):
    return [view.columnWidth(column) for column in range(view.columnCount())]


def _handle(view, column):
    header = view.horizontalHeader()
    return QPoint(header.sectionViewportPosition(column) + header.sectionSize(column) - 1,
                  header.height() // 2)


@pytest.mark.parametrize("column", [0, 1, 2])
@pytest.mark.parametrize("distance", [-72, 72])
def test_drag_tracks_pointer_without_changing_other_columns(qapp, qtbot, resize_table, column, distance):
    view = resize_table
    # Leave room on the right to reach the last section's handle.
    if column == 2:
        view.setColumnWidth(0, view.columnWidth(0) - 100)
        qapp.processEvents()
    before = _widths(view)
    viewport = view.horizontalHeader().viewport()
    start = _handle(view, column)
    qtbot.mouseMove(viewport, start)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    for step in range(1, 4):
        delta = distance * step // 3
        qtbot.mouseMove(viewport, start + QPoint(delta, 0))
        qapp.processEvents()
        expected = list(before)
        expected[column] += delta
        assert _widths(view) == expected
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start + QPoint(distance, 0))
    qapp.processEvents()
    assert _widths(view) == expected


def test_user_widths_survive_refresh_hidden_updates_and_window_resize(qapp, qtbot, resize_table):
    view = resize_table
    view.setColumnWidth(1, 120)
    before = _widths(view)
    view.selectRow(0)
    set_rows(view, [("An exceptionally long changed address " * 12, "101", "10,000.00")], [1])
    qapp.processEvents()
    assert _widths(view) == before
    assert len(view.selectedIndexes()) == view.columnCount()
    view.hide()
    set_rows(view, [("Changed while hidden", "102", "500.00")], [1])
    view.resize(1100, 300)
    view.show()
    qapp.processEvents()
    assert _widths(view) == before
    view.resize(300, 300)
    qapp.processEvents()
    assert _widths(view) == before
    assert view.horizontalScrollBar().maximum() > 0


def test_drag_stops_at_title_minimum_and_can_grow_again(qapp, qtbot, resize_table):
    view = resize_table
    header = view.horizontalHeader()
    before = _widths(view)
    minimum = header.minimum_width(0)
    start = _handle(view, 0)
    viewport = header.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, QPoint(2, start.y()))
    qapp.processEvents()
    assert view.columnWidth(0) == minimum
    qtbot.mouseMove(viewport, start + QPoint(35, 0))
    qapp.processEvents()
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start + QPoint(35, 0))
    assert view.columnWidth(0) == before[0] + 35
    assert _widths(view)[1:] == before[1:]

    # A new drag starts at the clamped boundary, without a stale drag offset.
    view.setColumnWidth(0, 1)
    start = _handle(view, 0)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, start + QPoint(22, 0))
    qapp.processEvents()
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start + QPoint(22, 0))
    qapp.processEvents()
    assert view.columnWidth(0) == minimum + 22


def test_double_click_fits_only_requested_column(qapp, qtbot, resize_table):
    view = resize_table
    before = _widths(view)
    header = view.horizontalHeader()
    qtbot.mouseDClick(header.viewport(), Qt.MouseButton.LeftButton, pos=_handle(view, 0))
    qapp.processEvents()
    assert view.columnWidth(0) == max(header.minimum_width(0), view.sizeHintForColumn(0))
    assert _widths(view)[1:] == before[1:]
    chosen = _widths(view)
    set_rows(view, [("Short", "101", "0")], [1])
    qapp.processEvents()
    assert _widths(view) == chosen


def test_scrolled_header_drag_keeps_its_direction(qapp, qtbot, resize_table):
    view = resize_table
    view.setColumnWidth(0, 500)
    view.resize(700, 300)
    qapp.processEvents()
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    qapp.processEvents()
    before = _widths(view)
    header = view.horizontalHeader()
    start = _handle(view, 1)
    qtbot.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(header.viewport(), start - QPoint(40, 0))
    qapp.processEvents()
    qtbot.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=start - QPoint(40, 0))
    assert view.columnWidth(1) == before[1] - 40
    assert view.columnWidth(0) == before[0]
    assert view.columnWidth(2) == before[2]


def test_hiding_and_restoring_columns_preserves_automatic_fill(qapp, qtbot, resize_table):
    view = resize_table
    view.hideColumn(1)
    qapp.processEvents()
    view.resize(1100, 300)
    qapp.processEvents()
    assert view.columnWidth(0) + view.columnWidth(2) == view.viewport().width()
    view.showColumn(1)
    qapp.processEvents()
    view.resize(1200, 300)
    qapp.processEvents()
    assert sum(_widths(view)) == view.viewport().width()


def test_hiding_and_restoring_columns_preserves_manual_widths(qapp, qtbot, resize_table):
    view = resize_table
    view.setColumnWidth(1, 120)
    before = _widths(view)
    view.hideColumn(1)
    qapp.processEvents()
    view.resize(1100, 300)
    qapp.processEvents()
    view.showColumn(1)
    qapp.processEvents()
    assert _widths(view) == before


def test_charge_dialog_initial_widths_fill_space_then_respect_header_drag(resize_application, qtbot):
    app = resize_application
    dialog = ChargeDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    app.processEvents()
    view = dialog.items
    initial = _widths(view)
    assert sum(initial) == view.viewport().width()
    assert initial[0] >= 185
    assert initial[1] >= 140
    reason = view.cellWidget(0, 2)
    # The editor keeps the table's normal cell padding at the right edge.
    assert reason.geometry().right() >= view.viewport().width() - 16

    header = view.horizontalHeader()
    start = _handle(view, 0)
    qtbot.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(header.viewport(), start - QPoint(32, 0))
    app.processEvents()
    qtbot.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=start - QPoint(32, 0))
    app.processEvents()
    expected = list(initial)
    expected[0] -= 32
    assert _widths(view) == expected

    dialog.hide()
    dialog.add_item()
    dialog.resize(dialog.width() + 100, dialog.height())
    dialog.show()
    app.processEvents()
    assert _widths(view) == expected
