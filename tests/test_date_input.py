"""Exercise the actual date popup and keep native date-entry behavior intact."""

from datetime import date, datetime

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QCalendarWidget, QDateTimeEdit, QSpinBox, QTableView, QToolButton

from grihalekha.paths import asset_dir
from grihalekha import clock
from grihalekha.ui.dialogs import AmountActionDialog
from grihalekha.ui.calendar import DateInput
from grihalekha.ui.theme import apply_theme


def test_default_today_and_financial_date_limit_use_ist(qtbot, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 2, 0, 15, tzinfo=clock.IST))
    field = DateInput()
    qtbot.addWidget(field)
    assert field.iso() == "2024-01-02"
    calendar = field.calendarWidget()
    calendar.setSelectedDate(QDate(2024, 1, 1))
    calendar._choose_today()
    assert field.iso() == "2024-01-02"
    dialog = AmountActionDialog("Return balance")
    qtbot.addWidget(dialog)
    assert dialog.when.iso() == "2024-01-02"
    assert dialog.when.maximumDate() == QDate(2024, 1, 2)


@pytest.fixture
def date_field(qapp, qtbot):
    previous_palette = QPalette(qapp.palette())
    previous_stylesheet = qapp.styleSheet()
    previous_style = qapp.style().objectName()
    previous_font = QFont(qapp.font())
    previous_scheme = qapp.styleHints().colorScheme()
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))
    qapp.setFont(QFont("Noto Sans", 10))
    apply_theme(qapp)
    field = DateInput("2024-02-29")
    qtbot.addWidget(field)
    field.resize(300, 44)
    field.show()
    qtbot.waitExposed(field)
    qtbot.mouseMove(field, QPoint(1, 1))
    yield field
    field.close()
    qapp.setStyleSheet("")
    qapp.setStyle(previous_style)
    qapp.setPalette(previous_palette)
    qapp.setFont(previous_font)
    qapp.styleHints().setColorScheme(previous_scheme)
    qapp.setStyleSheet(previous_stylesheet)


def _open_popup(field, qtbot):
    qtbot.mouseClick(
        field, Qt.MouseButton.LeftButton,
        pos=QPoint(field.width() - 15, field.height() // 2),
    )
    calendar = field.calendarWidget()
    qtbot.waitUntil(calendar.isVisible)
    return calendar


def _date_cell(calendar, day):
    view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    # Day 15 is unique in the six-week grid, even in short months.
    matches = [
        view.model().index(row, column)
        for row in range(1, view.model().rowCount())
        for column in range(view.model().columnCount())
        if str(view.model().index(row, column).data()) == str(day)
    ]
    assert len(matches) == 1
    return view, view.visualRect(matches[0]).center()


def _dated_cell(calendar, day):
    """Locate a complete date relative to the unambiguous middle of the month."""
    view, anchor_point = _date_cell(calendar, 15)
    anchor = view.indexAt(anchor_point)
    anchor_day = QDate(calendar.yearShown(), calendar.monthShown(), 15)
    offset = (anchor.row() - 1) * 7 + anchor.column() + anchor_day.daysTo(day)
    index = view.model().index(offset // 7 + 1, offset % 7)
    assert index.isValid()
    assert str(index.data()) == str(day.day())
    return view, view.visualRect(index)


def _cell_background(view, rectangle):
    # Sample inside the rounded highlight, above the date text. Qt's grab is
    # device-scaled, while native cell rectangles use logical coordinates.
    rendered = view.viewport().grab().toImage()
    scale = rendered.devicePixelRatio()
    return rendered.pixelColor(
        int(rectangle.center().x() * scale), int((rectangle.top() + 7) * scale),
    )


def test_iso_values_leap_day_ranges_and_keyboard_editing(date_field, qtbot):
    field = date_field
    assert field.iso() == "2024-02-29"
    assert field.minimumDate() == QDate(1900, 1, 1)
    assert field.maximumDate() == QDate(2199, 12, 31)
    field.set_iso("2023-02-29")
    assert field.iso() == "2024-02-29"
    field.set_iso("2024-02-28")
    field.setFocus()
    field.setCurrentSection(QDateTimeEdit.Section.DaySection)
    qtbot.keyClick(field, Qt.Key.Key_Up)
    assert field.iso() == "2024-02-29"
    qtbot.keyClick(field, Qt.Key.Key_Down)
    assert field.iso() == "2024-02-28"
    field.set_iso("1899-12-31")
    assert field.iso() == "1900-01-01"
    field.set_iso("2200-01-01")
    assert field.iso() == "2199-12-31"


def test_clicking_a_day_updates_field_and_closes_popup(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    assert calendar.verticalHeaderFormat() == QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    view, point = _date_cell(calendar, 15)
    with qtbot.waitSignal(date_field.dateChanged):
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert date_field.iso() == "2024-02-15"
    qtbot.waitUntil(lambda: not calendar.isVisible())
    assert date_field.hasFocus()


def test_today_updates_and_accepts_popup(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    qtbot.mouseClick(calendar.today_button, Qt.MouseButton.LeftButton)
    assert date_field.iso() == date.today().isoformat()
    qtbot.waitUntil(lambda: not calendar.isVisible())


def test_month_year_navigation_and_escape_preserve_original_date(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    next_month = calendar.findChild(QToolButton, "qt_calendar_nextmonth")
    qtbot.mouseClick(next_month, Qt.MouseButton.LeftButton)
    assert (calendar.yearShown(), calendar.monthShown()) == (2024, 3)
    previous_month = calendar.findChild(QToolButton, "qt_calendar_prevmonth")
    qtbot.mouseClick(previous_month, Qt.MouseButton.LeftButton)
    assert (calendar.yearShown(), calendar.monthShown()) == (2024, 2)
    month = calendar.findChild(QToolButton, "qt_calendar_monthbutton")
    month.menu().actions()[4].trigger()
    assert (calendar.yearShown(), calendar.monthShown()) == (2024, 5)
    year = calendar.findChild(QToolButton, "qt_calendar_yearbutton")
    qtbot.mouseClick(year, Qt.MouseButton.LeftButton)
    year_editor = calendar.findChild(QSpinBox, "qt_calendar_yearedit")
    qtbot.waitUntil(year_editor.isVisible)
    year_editor.selectAll()
    qtbot.keyClicks(year_editor, "2025")
    qtbot.keyClick(year_editor, Qt.Key.Key_Enter)
    assert (calendar.yearShown(), calendar.monthShown()) == (2025, 5)
    qtbot.keyClick(calendar, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not calendar.isVisible())
    assert date_field.iso() == "2024-02-29"


def test_restricted_range_disables_today_and_outside_months(date_field, qtbot):
    date_field.setDateRange(QDate(2024, 2, 10), QDate(2024, 2, 20))
    calendar = _open_popup(date_field, qtbot)
    assert not calendar.today_button.isEnabled()
    assert not calendar.findChild(QToolButton, "qt_calendar_prevmonth").isEnabled()
    assert not calendar.findChild(QToolButton, "qt_calendar_nextmonth").isEnabled()
    view, point = _date_cell(calendar, 15)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert date_field.iso() == "2024-02-15"
    qtbot.waitUntil(lambda: not calendar.isVisible())


def test_keyboard_day_selection_accepts_and_escape_closes(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Left)
    qtbot.keyClick(view, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not calendar.isVisible())
    assert date_field.iso() == "2024-02-28"
    calendar = _open_popup(date_field, qtbot)
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Left)
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not calendar.isVisible())
    # Native QDateEdit applies keyboard day changes immediately. Escape closes
    # its popup while retaining that date; preserve this existing behavior.
    assert date_field.iso() == "2024-02-27"


def test_date_hover_is_visible_without_changing_selection(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    view, first = _dated_cell(calendar, QDate(2024, 2, 15))
    _, second = _dated_cell(calendar, QDate(2024, 2, 16))
    _, selected = _dated_cell(calendar, calendar.selectedDate())
    base = _cell_background(view, first)
    selected_background = _cell_background(view, selected)

    qtbot.mouseMove(view.viewport(), first.center())
    qtbot.waitUntil(lambda: _cell_background(view, first) != base)
    assert _cell_background(view, second) == base
    assert date_field.iso() == "2024-02-29"

    qtbot.mouseMove(view.viewport(), second.center())
    qtbot.waitUntil(lambda: _cell_background(view, second) != base)
    assert _cell_background(view, first) == base
    qtbot.mouseMove(view.viewport(), selected.center())
    assert _cell_background(view, selected) == selected_background
    assert date_field.iso() == "2024-02-29"

    qtbot.mouseMove(view.viewport(), first.center())
    qtbot.waitUntil(lambda: _cell_background(view, first) != base)
    qtbot.mouseMove(calendar.today_button)
    qtbot.waitUntil(lambda: _cell_background(view, first) == base)


@pytest.mark.parametrize("direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft])
def test_hover_distinguishes_same_day_number_in_adjacent_months(date_field, qtbot, direction):
    date_field.set_iso("2024-02-15")
    calendar = _open_popup(date_field, qtbot)
    calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
    calendar.setLayoutDirection(direction)
    view, previous_month = _dated_cell(calendar, QDate(2024, 1, 29))
    _, current_month = _dated_cell(calendar, QDate(2024, 2, 29))
    base = _cell_background(view, previous_month)

    qtbot.mouseMove(view.viewport(), previous_month.center())
    qtbot.waitUntil(lambda: _cell_background(view, previous_month) != base)
    assert _cell_background(view, current_month) == base
    assert date_field.iso() == "2024-02-15"
    qtbot.mouseMove(view.viewport(), current_month.center())
    qtbot.waitUntil(lambda: _cell_background(view, current_month) != base)
    assert _cell_background(view, previous_month) == base
    assert date_field.iso() == "2024-02-15"


def test_hover_respects_date_range_and_disabled_calendar(date_field, qtbot):
    date_field.setDateRange(QDate(2024, 2, 10), QDate(2024, 2, 20))
    calendar = _open_popup(date_field, qtbot)
    view, allowed = _dated_cell(calendar, QDate(2024, 2, 15))
    _, outside_range = _dated_cell(calendar, QDate(2024, 2, 9))
    base = _cell_background(view, allowed)

    qtbot.mouseMove(view.viewport(), allowed.center())
    qtbot.waitUntil(lambda: _cell_background(view, allowed) != base)
    qtbot.mouseMove(view.viewport(), outside_range.center())
    assert _cell_background(view, outside_range) == base
    assert _cell_background(view, allowed) == base
    assert date_field.iso() == "2024-02-20"
    qtbot.mouseMove(view.viewport(), allowed.center())
    qtbot.waitUntil(lambda: _cell_background(view, allowed) != base)
    calendar.setEnabled(False)
    assert _cell_background(view, allowed) == base
    calendar.setEnabled(True)
    assert _cell_background(view, allowed) == base


def test_page_changes_and_hiding_clear_hover(date_field, qtbot):
    calendar = _open_popup(date_field, qtbot)
    view, cell = _dated_cell(calendar, QDate(2024, 2, 15))
    base = _cell_background(view, cell)
    qtbot.mouseMove(view.viewport(), cell.center())
    qtbot.waitUntil(lambda: _cell_background(view, cell) != base)
    calendar.showNextMonth()
    calendar.showPreviousMonth()
    assert _cell_background(view, cell) == base
    qtbot.mouseMove(view.viewport(), QPoint(cell.center().x() + 1, cell.center().y()))
    qtbot.waitUntil(lambda: _cell_background(view, cell) != base)
    calendar.hide()
    calendar.show()
    assert _cell_background(view, cell) == base
