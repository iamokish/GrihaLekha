"""Date entry with a clear, keyboard-accessible native calendar popup."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QEvent, QRect, QRectF, Qt
from PySide6.QtGui import QFont, QPalette, QPen, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QToolButton,
    QWidget,
)

from ..clock import today_ist


def _today():
    return QDate.fromString(today_ist().isoformat(), "yyyy-MM-dd")


class DateCalendar(QCalendarWidget):
    """Use Qt's date navigation and hit testing with a quieter presentation."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("dateCalendar")
        self.setGridVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.setMinimumSize(360, 354)
        self._today = _today()
        self._hovered_cell = QRect()
        self._date_view = self.findChild(QTableView, "qt_calendar_calendarview")
        self._date_viewport = self._date_view.viewport() if self._date_view is not None else None
        if self._date_view is not None:
            self._date_view.setMouseTracking(True)
            self._date_viewport.installEventFilter(self)

        footer = QWidget(self)
        footer.setObjectName("calendarFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 7, 8, 7)
        footer_layout.setSpacing(12)
        self.selection_label = QLabel(footer)
        self.selection_label.setObjectName("calendarSelection")
        footer_layout.addWidget(self.selection_label, 1)
        self.today_button = QPushButton("Today", footer)
        self.today_button.setObjectName("calendarToday")
        self.today_button.setAutoDefault(False)
        self.today_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.today_button.setToolTip("Choose today's date")
        self.today_button.clicked.connect(self._choose_today)
        footer_layout.addWidget(self.today_button)
        self.layout().addWidget(footer)

        for name, description in (
            ("qt_calendar_prevmonth", "Previous month"),
            ("qt_calendar_nextmonth", "Next month"),
            ("qt_calendar_monthbutton", "Choose month"),
            ("qt_calendar_yearbutton", "Change year"),
        ):
            control = self.findChild(QToolButton, name)
            if control is not None:
                control.setAccessibleName(description)
                control.setToolTip(description)
                control.setCursor(Qt.CursorShape.PointingHandCursor)

        self.selectionChanged.connect(self._sync_footer)
        self.currentPageChanged.connect(self._sync_footer)
        self.currentPageChanged.connect(self._clear_hover)
        self._apply_header_format()
        self._sync_footer()

    def _apply_header_format(self):
        header = QTextCharFormat()
        header.setForeground(self.palette().brush(QPalette.ColorRole.PlaceholderText))
        header.setBackground(self.palette().brush(QPalette.ColorRole.Base))
        header.setFontWeight(QFont.Weight.Medium)
        self.setHeaderTextFormat(header)
        # Qt otherwise emphasizes weekends with a hardcoded red foreground.
        weekday = QTextCharFormat()
        weekday.setForeground(self.palette().brush(QPalette.ColorRole.Text))
        self.setWeekdayTextFormat(Qt.DayOfWeek.Saturday, weekday)
        self.setWeekdayTextFormat(Qt.DayOfWeek.Sunday, weekday)

    def _sync_footer(self, *args):
        self.selection_label.setText(self.locale().toString(self.selectedDate(), "d MMM yyyy"))
        self.today_button.setEnabled(self.minimumDate() <= self._today <= self.maximumDate())

    def _choose_today(self):
        today = _today()
        if self.minimumDate() <= today <= self.maximumDate():
            self.setSelectedDate(today)
            # Follow the same native popup acceptance path as clicking a day.
            self.clicked.emit(today)

    def _set_hovered_cell(self, cell):
        if cell == self._hovered_cell:
            return
        previous = self._hovered_cell
        self._hovered_cell = cell
        if self._date_viewport is not None:
            if not previous.isEmpty():
                self._date_viewport.update(previous)
            if not cell.isEmpty():
                self._date_viewport.update(cell)

    def _clear_hover(self, *args):
        self._set_hovered_cell(QRect())

    def eventFilter(self, watched, event):
        if watched is self._date_viewport:
            if event.type() == QEvent.Type.MouseMove and self.isEnabled():
                index = self._date_view.indexAt(event.position().toPoint())
                self._set_hovered_cell(self._date_view.visualRect(index))
            elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.Resize):
                self._clear_hover()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        self._today = _today()
        self._sync_footer()
        self.updateCells()
        super().showEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.LayoutDirectionChange):
            self._clear_hover()
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._apply_header_format()
            self.updateCells()

    def paintCell(self, painter, rectangle, day):
        """Paint dates only; Qt retains selection, ranges and keyboard handling."""
        palette = self.palette()
        enabled = self.isEnabled() and self.minimumDate() <= day <= self.maximumDate()
        selected = day == self.selectedDate()
        # Use the native cell's geometry, not its displayed day number: the grid
        # includes dates from adjacent months which can have the same number.
        hovered = enabled and self._hovered_cell.contains(rectangle.center())
        in_month = day.year() == self.yearShown() and day.month() == self.monthShown()
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.fillRect(rectangle, palette.brush(QPalette.ColorRole.Base))
        cell = QRectF(rectangle).adjusted(4, 3, -4, -3)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.brush(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(cell, 8, 8)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight).darker(135))
            painter.drawRoundedRect(cell, 8, 8)
        if day == self._today and enabled:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Accent), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(cell, 8, 8)
        if not enabled:
            color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        elif selected or hovered:
            color = palette.color(QPalette.ColorRole.HighlightedText)
        elif in_month:
            color = palette.color(QPalette.ColorRole.Text)
        else:
            color = palette.color(QPalette.ColorRole.PlaceholderText)
        painter.setPen(color)
        font = QFont(self.font())
        font.setBold(selected or day == self._today)
        painter.setFont(font)
        painter.drawText(rectangle, Qt.AlignmentFlag.AlignCenter, str(day.day()))
        painter.restore()


class DateInput(QDateEdit):
    def __init__(self, value: str | date | None = None):
        super().__init__()
        self.setCalendarPopup(True)
        self.setCalendarWidget(DateCalendar(self))
        self.setDisplayFormat("dd MMM yyyy")
        self.setMinimumDate(QDate(1900, 1, 1))
        self.setMaximumDate(QDate(2199, 12, 31))
        self.set_iso(str(value)[:10] if value else today_ist().isoformat())

    def iso(self) -> str:
        return self.date().toString("yyyy-MM-dd")

    def set_iso(self, value: str) -> None:
        parsed = QDate.fromString(value, "yyyy-MM-dd")
        if parsed.isValid():
            self.setDate(parsed)
