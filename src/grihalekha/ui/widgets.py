"""Small widgets shared by the desktop screens and dialogs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QPersistentModelIndex, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .calendar import DateInput as DateInput
from ..currencies import CURRENCIES, DEFAULT_CURRENCY, get_currency
from ..meter import format_meter_reading, parse_meter_reading
from ..money import format_money


def money(amount: int | None, *, currency_code: str = DEFAULT_CURRENCY) -> str:
    """Format integer paise without converting through floating point."""
    if amount is None:
        return "—"
    return format_money(amount, currency_code=currency_code).replace("-", "−", 1)


_MONEY_PREFIXES = tuple(
    prefix + currency.symbol for currency in CURRENCIES for prefix in ("", "−", "-")
)


def date_text(value: str | date | None) -> str:
    if not value:
        return "—"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def label(text: str, kind: str = "", wrap: bool = False) -> QLabel:
    result = QLabel(text)
    if kind:
        result.setObjectName(kind)
    result.setWordWrap(wrap)
    return result


def button(
    text: str, callback=None, primary: bool = False, danger: bool = False
) -> QPushButton:
    result = QPushButton(text)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        result.setObjectName("primary")
    elif danger:
        result.setObjectName("danger")
    if callback:
        result.clicked.connect(callback)
    return result


class _TableHeader(QHeaderView):
    """Keep each title readable, including during an interactive resize."""

    def minimum_width(self, column: int) -> int:
        return max(self.minimumSectionSize(), self.sectionSizeFromContents(column).width())


class _RowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        option.state &= ~(QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        if index.row() == self.parent().hovered_row:
            option.state |= QStyle.StateFlag.State_MouseOver
        super().paint(painter, option, index)


class _RowInteraction(QObject):
    def eventFilter(self, watched, event):
        view = self.parent()
        # Qt also dispatches child events while tearing down the table, after
        # its Python subclass has gone away. No row interaction is needed then.
        if isinstance(view, TableWidget):
            view._track_row_event(watched, event)
        return False


class TableWidget(QTableWidget):
    """Stable column sizing and row feedback shared by every account table."""

    def __init__(self, columns: int):
        super().__init__(0, columns)
        self.hovered_row = -1
        self._preferred_widths: dict[int, int] = {}
        self._has_been_shown = False
        self._user_sized = False
        self._sizing = False
        self._cell_controls: dict[QWidget, QPersistentModelIndex] = {}
        self._row_interaction = _RowInteraction(self)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._fit_columns)
        header = _TableHeader(Qt.Orientation.Horizontal, self)
        self.setHorizontalHeader(header)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setResizeContentsPrecision(100)
        header.sectionResized.connect(self._section_resized)
        self.setItemDelegate(_RowDelegate(self))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self._row_interaction)
        self.itemSelectionChanged.connect(self._update_control_states)
        self.model().rowsInserted.connect(self._rows_changed)
        self.model().rowsRemoved.connect(self._rows_changed)
        self.model().modelReset.connect(self._rows_changed)
        self.model().headerDataChanged.connect(self._schedule_columns)

    def _schedule_columns(self, *args):
        self._layout_timer.start(0)

    def _rows_changed(self, *args):
        self._set_hovered_row(-1)
        self._schedule_columns()

    def _section_resized(self, column, old_size, new_size):
        if self._sizing:
            return
        header = self.horizontalHeader()
        if header.isSectionHidden(column) or old_size == 0 or new_size == 0:
            # Hiding/restoring a section also emits sectionResized. Its saved
            # width must not turn an otherwise automatic table into a manual one.
            self._schedule_columns()
            return
        if self._has_been_shown and not self._user_sized:
            # The displayed widths include the initial automatic fill. Freeze
            # all of them when the user first resizes: redistributing that fill
            # during a drag moves other boundaries and can reverse the drag.
            self._preferred_widths.update(
                (i, header.sectionSize(i))
                for i in range(self.columnCount()) if not header.isSectionHidden(i)
            )
            self._user_sized = True
        minimum = header.minimum_width(column)
        self._preferred_widths[column] = max(minimum, new_size)
        if new_size < minimum:
            self._sizing = True
            try:
                header.resizeSection(column, minimum)
            finally:
                self._sizing = False
        if not self._has_been_shown:
            # Constructor-provided widths are initial preferences. The first
            # visible layout can still distribute the available space.
            self._schedule_columns()

    def resizeColumnsToContents(self):
        # Do not shrink saved/user widths during a refresh. Qt's native
        # stretch-last-section pass can otherwise run against stale geometry.
        self.ensurePolished()
        header = self.horizontalHeader()
        for column in range(self.columnCount()):
            if self._user_sized and column in self._preferred_widths:
                continue
            self._preferred_widths[column] = max(
                self._preferred_widths.get(column, 0),
                header.minimum_width(column),
                min(440, self.sizeHintForColumn(column)),
            )
        self._fit_columns()
        self._schedule_columns()

    def _fit_columns(self):
        if self._sizing or not self.columnCount():
            return
        header = self.horizontalHeader()
        columns = [i for i in range(self.columnCount()) if not self.isColumnHidden(i)]
        if not columns:
            return
        widths = [max(header.minimum_width(i), self._preferred_widths.get(i, 0)) for i in columns]
        extra, remainder = (0, 0) if self._user_sized else divmod(
            max(0, self.viewport().width() - sum(widths)), len(columns)
        )
        self._sizing = True
        try:
            for position, (column, width) in enumerate(zip(columns, widths)):
                header.resizeSection(column, width + extra + (position < remainder))
        finally:
            self._sizing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()
        self._schedule_columns()

    def showEvent(self, event):
        self._has_been_shown = True
        super().showEvent(event)
        self._fit_columns()
        self._schedule_columns()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._schedule_columns()

    def _set_hovered_row(self, row):
        if row == self.hovered_row:
            return
        previous, self.hovered_row = self.hovered_row, row
        for changed in (previous, row):
            if 0 <= changed < self.rowCount():
                self.viewport().update(0, self.rowViewportPosition(changed), self.viewport().width(), self.rowHeight(changed))
        self._update_control_states()

    def _update_control_states(self):
        # Only tables with persistent editors have controls to restyle.
        for control, index in tuple(self._cell_controls.items()):
            if not index.isValid() or self.cellWidget(index.row(), index.column()) is not control:
                continue
            row = index.row()
            states = {"rowSelected": self.selectionModel().isRowSelected(row), "rowHovered": row == self.hovered_row}
            changed = False
            for name, value in states.items():
                if control.property(name) != value:
                    control.setProperty(name, value)
                    changed = True
            if changed:
                control.style().unpolish(control)
                control.style().polish(control)
                control.update()

    def setCellWidget(self, row, column, widget):
        super().setCellWidget(row, column, widget)
        self._cell_controls[widget] = QPersistentModelIndex(self.model().index(row, column))
        widget.destroyed.connect(lambda: self._cell_controls.pop(widget, None))
        widget.installEventFilter(self._row_interaction)
        widget.setMouseTracking(True)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self._row_interaction)
            child.setMouseTracking(True)

    def _track_row_event(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.Leave):
            point = self.viewport().mapFromGlobal(QCursor.pos())
            self._set_hovered_row(self.indexAt(point).row() if self.viewport().rect().contains(point) else -1)
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn) and watched is not self.viewport():
            point = watched.mapTo(self.viewport(), watched.rect().center())
            index = self.indexAt(point)
            if index.isValid() and not self.selectionModel().isRowSelected(index.row()):
                self.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                )


def table(headers: list[str], *, editable: bool = False) -> QTableWidget:
    result = TableWidget(len(headers))
    result.setHorizontalHeaderLabels(headers)
    result.verticalHeader().hide()
    result.verticalHeader().setDefaultSectionSize(max(45 if editable else 43, result.fontMetrics().height() + 20))
    result.setAlternatingRowColors(True)
    result.setShowGrid(False)
    result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    result.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    result.setEditTriggers(
        QAbstractItemView.EditTrigger.DoubleClicked
        | QAbstractItemView.EditTrigger.EditKeyPressed
        | QAbstractItemView.EditTrigger.AnyKeyPressed
        if editable
        else QAbstractItemView.EditTrigger.NoEditTriggers
    )
    result.setWordWrap(False)
    result.setTextElideMode(Qt.TextElideMode.ElideRight)
    result.setSortingEnabled(False)
    return result


def set_rows(
    target: QTableWidget, rows: list[tuple], ids: list[int | str] | None = None
) -> None:
    previous_id = selected_id(target) if ids is not None else None
    blocker = QSignalBlocker(target)
    target.setRowCount(0)
    target.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            cell = QTableWidgetItem("" if value is None else str(value))
            cell.setToolTip(cell.text())
            if ids is not None:
                cell.setData(Qt.ItemDataRole.UserRole, ids[row_index])
            if str(value).startswith(_MONEY_PREFIXES):
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            target.setItem(row_index, col_index, cell)
    target.resizeColumnsToContents()
    if previous_id is not None and previous_id in ids:
        target.selectRow(ids.index(previous_id))
    blocker.unblock()


def selected_id(target: QTableWidget) -> int | str | None:
    row = target.currentRow()
    item = target.item(row, 0) if row >= 0 else None
    return item.data(Qt.ItemDataRole.UserRole) if item else None


class AmountInput(QLineEdit):
    """A decimal amount editor that refuses silent rounding."""

    def __init__(self, amount: int | None = 0, parent: QWidget | None = None,
                 *, currency_code: str = DEFAULT_CURRENCY):
        super().__init__(parent)
        self.setPlaceholderText("0.00")
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMaxLength(19)
        currency = get_currency(currency_code)
        self.setToolTip(f"Amount in {currency.code} ({currency.symbol}), up to two decimal places")
        if amount is not None:
            self.set_paise(amount)

    def set_paise(self, amount: int) -> None:
        sign = "-" if amount < 0 else ""
        whole, fraction = divmod(abs(int(amount)), 100)
        self.setText(f"{sign}{whole}.{fraction:02d}")

    def paise(self, allow_zero: bool = True) -> int:
        text = self.text().strip()
        if not text:
            raise ValueError("Please enter an amount.")
        # Avoid float/Decimal exponent notation and ambiguous separators.
        parts = text.split(".")
        if len(parts) > 2 or not parts[0].isascii() or not parts[0].isdigit():
            raise ValueError(
                "Enter an amount such as 1200.50, without commas or currency symbols."
            )
        if len(parts) == 2 and (
            not parts[1]
            or len(parts[1]) > 2
            or not parts[1].isascii()
            or not parts[1].isdigit()
        ):
            raise ValueError("Amounts can have up to two decimal places.")
        try:
            amount = int(Decimal(text) * 100)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ValueError("Please enter a valid amount.") from exc
        if amount < 0 or (not allow_zero and amount == 0):
            raise ValueError(
                "The amount must be greater than zero."
                if not allow_zero
                else "The amount cannot be negative."
            )
        if amount > 999_999_999_999_99:
            raise ValueError("This amount exceeds the supported limit.")
        return amount


class MeterReadingInput(QLineEdit):
    """Optional exact kWh reading, distinct from a monetary amount."""

    def __init__(self, reading: int | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText("Optional, e.g. 1250.75")
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setMaxLength(19)
        self.setToolTip("Electric meter reading in kWh, up to two decimal places. Leave blank if unknown.")
        self.set_reading(reading)

    def set_reading(self, reading: int | None) -> None:
        self.setText(format_meter_reading(reading))

    def reading(self) -> int | None:
        return parse_meter_reading(self.text())


class FormDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None, width: int = 540):
        super().__init__(parent)
        self.currency_code = getattr(parent, "currency_code", DEFAULT_CURRENCY)
        self.currency_symbol = get_currency(self.currency_code).symbol
        self.setWindowTitle(title)
        self._preferred_width = width
        self._initial_size_set = False
        screen = self.screen()
        self.setMinimumWidth(min(width, screen.availableGeometry().width() - 48) if screen else width)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 22)
        self.root.setSpacing(15)
        self.heading = label(title, "sectionTitle")
        self.root.addWidget(self.heading)
        self.form_body = QWidget()
        self.form = QFormLayout(self.form_body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(12)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.form_scroll.setWidget(self.form_body)
        self.root.addWidget(self.form_scroll, 1)
        self.error = label("", "error", wrap=True)
        self.error.hide()
        self.root.addWidget(self.error)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName(
            "primary"
        )
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        self.root.addWidget(self.buttons)
        self.result_data = None

    def money(self, amount: int | None) -> str:
        return money(amount, currency_code=self.currency_code)

    def sizeHint(self):
        result = super().sizeHint()
        if not hasattr(self, "buttons"):
            return result
        margins = self.root.contentsMargins()
        height = (
            self.form.sizeHint().height()
            + self.heading.sizeHint().height()
            + self.buttons.sizeHint().height()
            + margins.top()
            + margins.bottom()
            + self.root.spacing() * 3
        )
        screen = self.screen()
        if screen is not None:
            # Keep the actions on screen; only the form scrolls on small displays.
            height = min(height, max(240, screen.availableGeometry().height() - 80))
        result.setHeight(height)
        return result

    def showEvent(self, event):
        if not self._initial_size_set:
            self._initial_size_set = True
            size = self.sizeHint()
            width = max(self._preferred_width, size.width())
            height = size.height()
            screen = self.screen()
            if screen is not None:
                available = screen.availableGeometry()
                width = min(width, max(320, available.width() - 48))
                height = min(height, max(240, available.height() - 80))
                self.setMinimumWidth(min(self._preferred_width, width))
            # Qt otherwise limits a dialog's automatic initial size to roughly
            # two thirds of the display, hiding useful allocation rows early.
            self.resize(width, height)
            if screen is not None:
                frame = self.frameGeometry()
                self.move(
                    max(available.left() + 16, min(self.x(), available.right() - frame.width() - 16)),
                    max(available.top() + 16, min(self.y(), available.bottom() - frame.height() - 16)),
                )
        super().showEvent(event)

    def add_hint(self, text: str) -> QLabel:
        hint = label(text, "muted", wrap=True)
        self.form.addRow(hint)
        return hint

    def field(self, name: str, value: str = "", placeholder: str = "") -> QLineEdit:
        result = QLineEdit(value)
        result.setPlaceholderText(placeholder)
        self.form.addRow(name, result)
        return result

    def validate(self):
        return None

    def submit(self) -> None:
        try:
            self.result_data = self.validate()
        except (ValueError, TypeError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class MetricCard(QFrame):
    def __init__(self, title: str, subtitle: str = "", alert: bool = False):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 17, 20, 17)
        layout.setSpacing(9)
        layout.addWidget(label(title.upper(), "eyebrow"))
        self.value = label("—", "metricAlert" if alert else "metric")
        layout.addWidget(self.value)
        self.subtitle = label(subtitle, "muted")
        layout.addWidget(self.subtitle)


def horizontal(*widgets: QWidget, stretch: bool = False) -> QWidget:
    result = QWidget()
    layout = QHBoxLayout(result)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch()
    return result
