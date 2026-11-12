"""Readability regressions for the inherited-palette and invisible-row failures."""

from datetime import date

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QMenu, QMessageBox, QSpinBox, QStyle, QStyleOptionButton, QStyleOptionComboBox, QStyleOptionSpinBox, QVBoxLayout, QWidget

from grihalekha.paths import asset_dir
from grihalekha.ui.dialogs import BuildingDialog, ChargeDialog
from grihalekha.ui.bill_dialogs import BillPaymentDialog
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.widgets import set_rows, table


def _luminance(color):
    values = (color.redF(), color.greenF(), color.blueF())
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.fixture
def restore_application_theme(qapp):
    previous_palette = QPalette(qapp.palette())
    previous_stylesheet = qapp.styleSheet()
    previous_style = qapp.style().objectName()
    previous_font = QFont(qapp.font())
    previous_scheme = qapp.styleHints().colorScheme()
    yield
    qapp.setStyleSheet("")
    qapp.setStyle(previous_style)
    qapp.setPalette(previous_palette)
    qapp.setFont(previous_font)
    qapp.styleHints().setColorScheme(previous_scheme)
    qapp.setStyleSheet(previous_stylesheet)


@pytest.fixture
def dark_application(qapp, restore_application_theme):
    for path in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(path))
    qapp.setFont(QFont("Noto Sans", 10))
    apply_theme(qapp)
    return qapp


@pytest.mark.parametrize("inherited_foreground,inherited_background", [("#ffffff", "#ffffff"), ("#151515", "#151515")])
def test_theme_replaces_conflicting_host_colors(qapp, restore_application_theme, inherited_foreground, inherited_background):
    # A table background from CSS and text from the OS caused the reported bug.
    inherited = QPalette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.HighlightedText, QPalette.ColorRole.PlaceholderText):
            inherited.setColor(group, role, QColor(inherited_foreground))
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase, QPalette.ColorRole.Button, QPalette.ColorRole.Highlight):
            inherited.setColor(group, role, QColor(inherited_background))
    qapp.setStyleSheet("")
    qapp.setPalette(inherited)
    apply_theme(qapp)
    palette = qapp.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for text_role, background_role in (
            (QPalette.ColorRole.WindowText, QPalette.ColorRole.Window),
            (QPalette.ColorRole.Text, QPalette.ColorRole.Base),
            (QPalette.ColorRole.Text, QPalette.ColorRole.AlternateBase),
            (QPalette.ColorRole.ButtonText, QPalette.ColorRole.Button),
            (QPalette.ColorRole.PlaceholderText, QPalette.ColorRole.Base),
            (QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Highlight),
        ):
            assert _contrast(palette.color(group, text_role), palette.color(group, background_role)) >= 4.5, (group, text_role, background_role)
        assert _luminance(palette.color(group, QPalette.ColorRole.Window)) < 0.1
        assert _luminance(palette.color(group, QPalette.ColorRole.Base)) < 0.1
    disabled = QPalette.ColorGroup.Disabled
    assert _contrast(palette.color(disabled, QPalette.ColorRole.Text), palette.color(disabled, QPalette.ColorRole.Base)) >= 3


def _readable_glyph_pixels(view, index, qapp):
    """Compare real rendered text with a blank cell, excluding borders/background."""
    rectangle = view.visualRect(index).intersected(view.viewport().rect()).adjusted(3, 3, -3, -3)
    assert rectangle.isValid()
    qapp.processEvents()
    actual = view.viewport().grab().toImage()
    original = index.data(Qt.ItemDataRole.EditRole)
    try:
        assert view.model().setData(index, "", Qt.ItemDataRole.EditRole)
        qapp.processEvents()
        blank = view.viewport().grab().toImage()
    finally:
        view.model().setData(index, original, Qt.ItemDataRole.EditRole)
        qapp.processEvents()
    ratio = actual.devicePixelRatio()
    readable = 0
    for y in range(round(rectangle.top() * ratio), round((rectangle.bottom() + 1) * ratio)):
        for x in range(round(rectangle.left() * ratio), round((rectangle.right() + 1) * ratio)):
            if actual.pixel(x, y) != blank.pixel(x, y) and _contrast(actual.pixelColor(x, y), blank.pixelColor(x, y)) >= 4.5:
                readable += 1
    return readable


def test_rows_are_readable_before_selection_and_after_focus_leaves(qtbot, dark_application, tmp_path):
    view = table(["Charge / amount"])
    qtbot.addWidget(view)
    set_rows(view, [("Monthly rent ₹10,000.50",), ("Electricity बिल ₹800.25",)])
    view.resize(430, 240)
    view.setColumnWidth(0, 380)
    view.show()
    view.activateWindow()
    qtbot.waitExposed(view)
    view.clearSelection()
    view.setCurrentCell(-1, -1)
    for row in range(2):
        assert _readable_glyph_pixels(view, view.model().index(row, 0), dark_application) >= 30, f"Unselected row {row} is unreadable"
    assert view.grab().save(str(tmp_path / "unselected-rows.png"))
    view.selectRow(1)
    assert _readable_glyph_pixels(view, view.model().index(1, 0), dark_application) >= 30
    other_window = QWidget()
    qtbot.addWidget(other_window)
    other_window.show()
    other_window.activateWindow()
    dark_application.processEvents()
    qtbot.waitUntil(lambda: not view.isActiveWindow())
    assert _readable_glyph_pixels(view, view.model().index(1, 0), dark_application) >= 30, "Inactive selected row is unreadable"


def test_parentless_form_errors_and_popups_remain_readable(qtbot, dark_application):
    dialog = BuildingDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.submit()
    dark_application.processEvents()
    assert dialog.error.isVisible()
    background = dialog.palette().color(QPalette.ColorRole.Window)
    assert _luminance(background) < 0.1
    for field_label in dialog.findChildren(QLabel):
        if field_label.isVisible() and field_label.text():
            assert _contrast(field_label.palette().color(QPalette.ColorRole.WindowText), background) >= 4.5, field_label.text()
    editor_palette = dialog.name.palette()
    assert _contrast(editor_palette.color(QPalette.ColorRole.PlaceholderText), editor_palette.color(QPalette.ColorRole.Base)) >= 4.5
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItems(["Bank transfer", "UPI payment", "Cash payment"])
    combo.resize(350, 44)
    combo.show()
    combo.showPopup()
    try:
        dark_application.processEvents()
        assert _readable_glyph_pixels(combo.view(), combo.model().index(1, 0), dark_application) >= 30
    finally:
        combo.hidePopup()
    menu = QMenu()
    qtbot.addWidget(menu)
    menu.addAction("Receive payment")
    menu.ensurePolished()
    assert _contrast(menu.palette().color(QPalette.ColorRole.WindowText), menu.palette().color(QPalette.ColorRole.Window)) >= 4.5
    notice = QMessageBox(QMessageBox.Icon.Warning, "Check amount", "Enter a valid amount in rupees.")
    qtbot.addWidget(notice)
    notice.show()
    dark_application.processEvents()
    notice_background = notice.palette().color(QPalette.ColorRole.Window)
    assert _luminance(notice_background) < 0.1
    for field_label in notice.findChildren(QLabel):
        if field_label.text():
            assert _contrast(field_label.palette().color(QPalette.ColorRole.WindowText), notice_background) >= 4.5


def _region_colors(widget, rectangle):
    image = widget.grab().toImage()
    scale = image.devicePixelRatio()
    return [image.pixelColor(x, y) for y in range(round(rectangle.top() * scale), round((rectangle.bottom() + 1) * scale)) for x in range(round(rectangle.left() * scale), round((rectangle.right() + 1) * scale))]


def test_checkbox_and_combo_indicators_are_visible(qtbot, dark_application):
    checkbox = QCheckBox("Existing tenant")
    qtbot.addWidget(checkbox)
    checkbox.show()
    dark_application.processEvents()
    option = QStyleOptionButton()
    checkbox.initStyleOption(option)
    indicator = checkbox.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, checkbox)
    background = checkbox.palette().color(QPalette.ColorRole.Window)
    assert sum(_contrast(color, background) >= 3 for color in _region_colors(checkbox, indicator)) >= 12, "Unchecked checkbox outline is invisible"
    checkbox.setChecked(True)
    dark_application.processEvents()
    inner_colors = _region_colors(checkbox, indicator.adjusted(4, 4, -4, -4))
    lightest = max(inner_colors, key=_luminance)
    assert sum(_contrast(color, lightest) >= 3 for color in inner_colors) >= 8, "Checked checkbox has no visible check mark"
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItem("UPI")
    combo.resize(240, 44)
    combo.show()
    dark_application.processEvents()
    combo_option = QStyleOptionComboBox()
    combo.initStyleOption(combo_option)
    arrow = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, combo_option, QStyle.SubControl.SC_ComboBoxArrow, combo)
    glyph = QRect(arrow.center().x() - 8, arrow.center().y() - 6, 16, 12)
    base = combo.palette().color(QPalette.ColorRole.Base)
    assert sum(_contrast(color, base) >= 3 for color in _region_colors(combo, glyph)) >= 8, "Combo box dropdown chevron is invisible"


@pytest.mark.parametrize("kind", ["charge", "payment"])
def test_inline_amount_and_reason_editors_fit_their_rows(qtbot, dark_application, kind):
    if kind == "charge":
        dialog = ChargeDialog()
        editors = [dialog.items.cellWidget(0, column) for column in range(3)]
    else:
        dialog = BillPaymentDialog()
        editors = [dialog.amount, dialog.reference, dialog.notes]
    qtbot.addWidget(dialog)
    dialog.show()
    dark_application.processEvents()
    for editor in editors:
        assert editor.height() >= editor.minimumSizeHint().height(), f"{kind} editor clips its contents: {editor.height()}px available, {editor.minimumSizeHint().height()}px needed"


def _row_backgrounds(view, row):
    image = view.viewport().grab().toImage()
    scale = image.devicePixelRatio()
    return [image.pixelColor(round((view.columnViewportPosition(col) + view.columnWidth(col) - 12) * scale),
                             round((view.rowViewportPosition(row) + 15) * scale)).name()
            for col in range(view.columnCount())]


def test_hover_and_selection_cover_whole_row_and_clear_on_leave(qtbot, dark_application):
    view = table(["Building", "Flat", "Outstanding"])
    qtbot.addWidget(view)
    set_rows(view, [("House", "101", "100"), ("House", "102", "200")])
    view.resize(740, 250)
    view.show()
    qtbot.waitExposed(view)
    qtbot.mouseMove(view.viewport(), view.visualRect(view.model().index(1, 0)).center())
    qtbot.waitUntil(lambda: view.hovered_row == 1)
    assert _row_backgrounds(view, 1) == ["#2d3442"] * 3
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(view.model().index(1, 1)).center())
    assert len(view.selectedIndexes()) == 3
    selected = _row_backgrounds(view, 1)
    assert len(set(selected)) == 1 and selected[0] in ("#304b7a", "#2c3c58")
    qtbot.mouseMove(view.viewport(), QPoint(20, view.viewport().height() - 20))
    qtbot.waitUntil(lambda: view.hovered_row == -1)
    view.clearSelection()
    assert _row_backgrounds(view, 1) == ["#20232b"] * 3


def test_table_columns_keep_titles_and_width_through_refresh_and_hidden_updates(qtbot, dark_application):
    titles = ["Building address", "Relationship (optional)", "Outstanding balance (₹)"]
    view = table(titles)
    qtbot.addWidget(view)
    view.resize(1000, 250)
    set_rows(view, [("Long address on Example Road", "Tenant", "1")], [1])
    view.show()
    qtbot.waitExposed(view)
    dark_application.processEvents()
    before = [view.columnWidth(i) for i in range(3)]
    view.selectRow(0)
    set_rows(view, [("Short", "", "2"), ("Other", "", "3")], [1, 2])
    dark_application.processEvents()
    assert [view.columnWidth(i) for i in range(3)] == before
    assert len(view.selectedIndexes()) == 3
    view.hide()
    set_rows(view, [("Changed while hidden", "", "")], [1])
    view.resize(1100, 250)
    view.show()
    dark_application.processEvents()
    assert sum(view.columnWidth(i) for i in range(3)) >= view.viewport().width()
    for column in range(3):
        view.setColumnWidth(column, 8)
        assert view.columnWidth(column) >= view.horizontalHeader().minimum_width(column)
    view.resize(260, 230)
    dark_application.processEvents()
    assert all(view.columnWidth(i) >= view.horizontalHeader().minimum_width(i) for i in range(3))
    assert view.horizontalScrollBar().maximum() > 0


def test_persistent_editors_select_rows_after_insertion_and_removal(qtbot, dark_application):
    dialog = ChargeDialog()
    qtbot.addWidget(dialog)
    dialog.add_item()
    dialog.show()
    dark_application.processEvents()
    view = dialog.items
    editor = view.cellWidget(1, 1)
    qtbot.mouseClick(editor, Qt.MouseButton.LeftButton)
    assert view.currentRow() == 1
    assert len(view.selectedIndexes()) == 3
    assert all(view.cellWidget(1, col).property("rowSelected") for col in range(3))
    view.removeRow(0)
    dark_application.processEvents()
    qtbot.mouseClick(editor, Qt.MouseButton.LeftButton)
    assert view.currentRow() == 0
    assert len(view.selectedIndexes()) == 3
    assert all(view.cellWidget(0, col).property("rowSelected") for col in range(3))


def test_spin_buttons_and_combo_popup_work_with_styled_geometry(qtbot, dark_application):
    host = QWidget()
    layout = QVBoxLayout(host)
    qtbot.addWidget(host)
    spin = QSpinBox()
    layout.addWidget(spin)
    spin.setRange(0, 31)
    spin.setValue(7)
    spin.setFixedHeight(44)
    combo = QComboBox()
    layout.addWidget(combo)
    combo.addItems(["Cash", "Bank transfer", "UPI"])
    combo.setFixedHeight(44)
    host.resize(300, 160)
    host.show()
    host.activateWindow()
    qtbot.waitExposed(host)
    dark_application.processEvents()
    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    buttons = [spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, part, spin)
               for part in (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown)]
    assert not buttons[0].intersects(buttons[1])
    assert spin.lineEdit().textMargins().left() >= 8
    for rectangle, expected in zip(buttons, (8, 7)):
        assert rectangle.width() >= 24 and rectangle.height() >= 16
        assert spin.rect().contains(rectangle)
        qtbot.mouseClick(spin, Qt.MouseButton.LeftButton, pos=rectangle.center())
        assert spin.value() == expected
    dark_application.processEvents()
    combo_option = QStyleOptionComboBox()
    combo.initStyleOption(combo_option)
    arrow = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, combo_option, QStyle.SubControl.SC_ComboBoxArrow, combo)
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton, pos=arrow.center())
    qtbot.waitUntil(lambda: combo.view().isVisible())
    dark_application.processEvents()
    target = combo.view().visualRect(combo.model().index(2, 0)).center()
    qtbot.mouseMove(combo.view().viewport(), target)
    # Qt suppresses a release immediately after opening to avoid closing the
    # popup from the same physical click that opened it.
    qtbot.wait(dark_application.doubleClickInterval() + 50)
    qtbot.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert combo.currentText() == "UPI", (combo.view().rect(), combo.view().visualRect(combo.model().index(2, 0)), target, combo.view().isVisible())
