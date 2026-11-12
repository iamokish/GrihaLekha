"""Explicit dark colors for every screen, popup and widget state.

Both the palette and stylesheet are needed: styling a background alone can
leave its text inherited from an incompatible operating-system theme. Fusion
gives Windows and Linux the same palette-aware control rendering.
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QSpinBox

from ..paths import asset_dir


class _ControlStyle(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Polish and isinstance(watched, (QSpinBox, QDoubleSpinBox)):
            # Qt's stylesheet spin-box edit rectangle starts at x=0 even with
            # outer padding. Inset the text itself so it clears the left frame.
            watched.lineEdit().setTextMargins(9, 0, 3, 0)
        return False


def apply_theme(app: QApplication) -> None:
    """Apply the dark theme globally, including dialogs without a parent."""
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#14161b",
        QPalette.ColorRole.WindowText: "#e8eaf0",
        QPalette.ColorRole.Base: "#181a20",
        QPalette.ColorRole.AlternateBase: "#20232b",
        QPalette.ColorRole.Text: "#e8eaf0",
        QPalette.ColorRole.Button: "#292d37",
        QPalette.ColorRole.ButtonText: "#e8eaf0",
        QPalette.ColorRole.BrightText: "#ffb4a6",
        QPalette.ColorRole.Highlight: "#304b7a",
        QPalette.ColorRole.HighlightedText: "#f6f8ff",
        QPalette.ColorRole.ToolTipBase: "#292d37",
        QPalette.ColorRole.ToolTipText: "#f6f8ff",
        QPalette.ColorRole.PlaceholderText: "#a8afbe",
        QPalette.ColorRole.Link: "#9bbdff",
        QPalette.ColorRole.LinkVisited: "#cbb9ee",
        QPalette.ColorRole.Light: "#7f8a9e",
        QPalette.ColorRole.Midlight: "#515b6d",
        QPalette.ColorRole.Mid: "#3b414e",
        QPalette.ColorRole.Dark: "#101115",
        QPalette.ColorRole.Shadow: "#090a0d",
        QPalette.ColorRole.Accent: "#8baeff",
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role, color in colors.items():
            palette.setColor(group, role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, QColor("#2c3c58"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#979eac"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#2b3240"))
    app.setPalette(palette)
    icon_directory = (asset_dir() / "ui").as_posix().replace('"', '\\"')
    app.setStyleSheet(STYLESHEET.replace("@UI_ASSETS@", icon_directory))
    if not hasattr(app, "_control_style"):
        app._control_style = _ControlStyle(app)
        app.installEventFilter(app._control_style)


STYLESHEET = """
QWidget { color: #e8eaf0; font-family: 'Noto Sans', 'Noto Sans Devanagari', 'Segoe UI', sans-serif; font-size: 10pt; }
QMainWindow, QDialog { background: #14161b; color: #e8eaf0; }
QWidget:disabled { color: #979eac; }
QLabel { color: #e8eaf0; background: transparent; }
QLabel:disabled { color: #979eac; }
QFrame#sidebar { background: #181a20; border: none; border-right: 1px solid #303540; }
QLabel#brand { color: #f4f6fb; font-size: 21pt; font-weight: 700; }
QLabel#brandHint { color: #a8afbe; font-size: 9pt; }
QLabel#sideFoot { color: #a8afbe; font-size: 9pt; }
QPushButton#nav { text-align: left; color: #c8d0df; background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 13px 14px; font-weight: 600; }
QPushButton#nav:hover { background: #292d37; color: #ffffff; }
QPushButton#nav:checked { background: #293e60; color: #f6f8ff; border-color: #5274a6; }
QPushButton#nav:focus { border-color: #a9c4ff; }
QLabel#pageTitle { font-size: 23pt; font-weight: 700; color: #f4f6fb; }
QLabel#sectionTitle { font-size: 13pt; font-weight: 600; color: #d1d7e2; }
QLabel#muted { color: #a8afbe; }
QLabel#eyebrow { color: #b4bdcd; font-size: 9pt; font-weight: 600; }
QLabel#metric { font-size: 22pt; font-weight: 600; color: #d7e3ff; }
QLabel#metricAlert { font-size: 22pt; font-weight: 600; color: #f4c283; }
QLabel#badge { color: #c5d8ff; background: #27364e; border: 1px solid #455b7c; padding: 6px 10px; border-radius: 9px; font-size: 9pt; font-weight: 600; }
QLabel#error { color: #ffb4a6; background: #3b231e; border: 1px solid #805449; border-radius: 5px; padding: 9px; }
QFrame#card { background: #20232b; border: 1px solid #353b48; border-radius: 10px; }
QFrame#notice { background: #202936; border: 1px solid #414f66; border-radius: 8px; }
QPushButton, QToolButton { background: #292d37; color: #e8eaf0; border: 1px solid #596273; border-radius: 6px; padding: 9px 13px; font-weight: 600; }
QPushButton:hover, QToolButton:hover { background: #353c49; border-color: #8b9ab2; color: #ffffff; }
QPushButton:pressed, QToolButton:pressed { background: #20232b; }
QPushButton:focus, QToolButton:focus { border-color: #a9c4ff; }
QPushButton:disabled, QToolButton:disabled { color: #979eac; background: #20232b; border-color: #3b414e; }
QPushButton#primary { background: #8baeff; color: #101b31; border-color: #8baeff; }
QPushButton#primary:hover { background: #adc5ff; color: #101b31; border-color: #d6e3ff; }
QPushButton#primary:pressed { background: #7396df; }
QPushButton#primary:focus { border-color: #f6f8ff; }
QPushButton#primary:disabled { background: #2b3b56; border-color: #465773; color: #b6bfd0; }
QPushButton#danger { color: #ffbfb0; border-color: #956053; }
QPushButton#danger:hover { background: #492d25; color: #ffded5; }
QPushButton#menuButton { padding-right: 30px; }
QPushButton::menu-indicator { image: url("@UI_ASSETS@/chevron-down.svg"); width: 12px; height: 12px; subcontrol-position: right center; right: 9px; }
QLineEdit, QComboBox, QAbstractSpinBox, QTextEdit, QPlainTextEdit {
    background: #181a20; color: #e8eaf0; border: 1px solid #626b7e;
    border-radius: 5px; padding: 8px; selection-background-color: #304b7a; selection-color: #f6f8ff; placeholder-text-color: #a8afbe;
}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover, QTextEdit:hover, QPlainTextEdit:hover { border-color: #8b9ab2; }
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus { border-color: #a9c4ff; }
QLineEdit:read-only { background: #242832; color: #d1d7e2; border-color: #515b6d; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled { color: #979eac; background: #1c1e25; border-color: #3b414e; }
QComboBox, QAbstractSpinBox { padding: 8px 38px 8px 10px; min-height: 22px; }
/* Use one framed list popup. The menu-style popup adds unframed margins
   outside the list view, especially for compact selectors inside tables. */
QComboBox { combobox-popup: 0; }
QComboBox::drop-down, QDateEdit::drop-down, QDateTimeEdit::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right; width: 30px;
    background: #292d37; border: none; border-left: 1px solid #596273;
    border-top-right-radius: 4px; border-bottom-right-radius: 4px;
}
QComboBox::drop-down:hover, QDateEdit::drop-down:hover, QDateTimeEdit::drop-down:hover { background: #353c49; }
QComboBox::drop-down:pressed, QDateEdit::drop-down:pressed, QDateTimeEdit::drop-down:pressed { background: #20232b; }
QComboBox::down-arrow, QDateEdit::down-arrow, QDateTimeEdit::down-arrow { image: url("@UI_ASSETS@/chevron-down.svg"); width: 14px; height: 14px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: padding; width: 30px; background: #292d37;
    border: none; border-left: 1px solid #596273;
}
QAbstractSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 4px; border-bottom: 1px solid #596273; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 4px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: #353c49; }
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed { background: #20232b; }
QAbstractSpinBox::up-button:off, QAbstractSpinBox::down-button:off,
QAbstractSpinBox::up-button:disabled, QAbstractSpinBox::down-button:disabled,
QComboBox::drop-down:disabled, QDateEdit::drop-down:disabled { background: #1c1e25; border-color: #3b414e; }
QAbstractSpinBox::up-arrow { image: url("@UI_ASSETS@/chevron-up.svg"); width: 12px; height: 12px; }
QAbstractSpinBox::down-arrow { image: url("@UI_ASSETS@/chevron-down.svg"); width: 12px; height: 12px; }
QComboBox QLineEdit, QAbstractSpinBox QLineEdit { background: transparent; border: none; border-radius: 0; padding: 0; min-height: 0; }
QComboBox QAbstractItemView { background: #20232b; color: #e8eaf0; border: 1px solid #76839a; border-radius: 4px; padding: 4px; selection-background-color: #304b7a; selection-color: #f6f8ff; outline: none; }
QComboBox QAbstractItemView::item { min-height: 24px; padding: 7px 10px; border: none; }
QAbstractItemView {
    background: #181a20; alternate-background-color: #20232b; color: #e8eaf0;
    border: 1px solid #414959; border-radius: 6px; gridline-color: #303540;
    selection-background-color: #304b7a; selection-color: #f6f8ff;
}
QAbstractItemView:focus { border-color: #8baeff; }
QAbstractItemView::item { color: #e8eaf0; }
QTableView::item { padding: 8px; border-bottom: 1px solid #2b303a; }
QTableView QLineEdit, QTableView QComboBox, QTableView QAbstractSpinBox { padding: 3px 6px; border-radius: 4px; }
QTableView QComboBox, QTableView QAbstractSpinBox { padding-right: 36px; min-height: 20px; }
QTableView QLineEdit[rowHovered="true"], QTableView QComboBox[rowHovered="true"], QTableView QAbstractSpinBox[rowHovered="true"] { background: #2d3442; border-color: #8b9ab2; }
QTableView QLineEdit[rowSelected="true"], QTableView QComboBox[rowSelected="true"], QTableView QAbstractSpinBox[rowSelected="true"] { background: #304b7a; border-color: #a9c4ff; color: #f6f8ff; }
QListView::item { padding: 7px 9px; }
QAbstractItemView::item:hover { color: #f6f8ff; background: #2d3442; }
QAbstractItemView::item:selected { color: #f6f8ff; background: #304b7a; }
QAbstractItemView::item:selected:!active { color: #f6f8ff; background: #2c3c58; }
QAbstractItemView::item:disabled { color: #979eac; }
QHeaderView { background: #262a33; }
QHeaderView::section { background: #262a33; color: #c8d0df; border: none; border-bottom: 1px solid #515b6d; padding: 10px 9px; font-size: 9pt; font-weight: 600; }
QHeaderView::section:checked { background: #303b4e; color: #f6f8ff; }
QTableCornerButton::section { background: #262a33; border: none; }
QTabWidget::pane { background: #181a20; border: 1px solid #414959; border-radius: 6px; top: -1px; }
QTabBar::tab { background: #20232b; color: #b4bdcd; padding: 11px 17px; border: 1px solid #414959; margin-right: 3px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:hover { background: #2d323e; color: #f6f8ff; }
QTabBar::tab:selected { background: #303b4e; color: #f6f8ff; border-bottom-color: #8baeff; font-weight: 600; }
QTabBar::tab:disabled { color: #979eac; background: #1c1e25; }
QGroupBox { color: #d1d7e2; border: 1px solid #515b6d; border-radius: 7px; margin-top: 14px; padding-top: 15px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #d0d9e9; font-weight: 600; }
QCheckBox, QRadioButton { spacing: 8px; color: #e0e4ec; background: transparent; }
QCheckBox:disabled, QRadioButton:disabled { color: #979eac; }
QCheckBox::indicator { width: 18px; height: 18px; background: #181a20; border: 2px solid #a2aec2; border-radius: 4px; }
QCheckBox::indicator:hover { border-color: #cbdcff; }
QCheckBox::indicator:checked { background: #8baeff; border-color: #8baeff; image: url("@UI_ASSETS@/check.svg"); }
QCheckBox::indicator:disabled { border-color: #6b7382; }
QRadioButton::indicator { width: 18px; height: 18px; background: #181a20; border: 2px solid #a2aec2; border-radius: 11px; }
QRadioButton::indicator:checked { background: #8baeff; border: 5px solid #425a83; }
QCheckBox:focus, QRadioButton:focus { color: #d6e3ff; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: #181a20; width: 14px; border: 0; margin: 2px 0; border-radius: 7px; }
QScrollBar:horizontal { background: #181a20; height: 14px; border: 0; margin: 0 2px; border-radius: 7px; }
QScrollBar::handle:vertical { background: #687387; border: 3px solid #181a20; border-radius: 7px; min-height: 36px; }
QScrollBar::handle:horizontal { background: #687387; border: 3px solid #181a20; border-radius: 7px; min-width: 36px; }
QScrollBar::handle:hover { background: #a0aec4; }
QScrollBar::handle:pressed { background: #adc5ff; }
QScrollBar::handle:disabled { background: #3b414e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: transparent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; background: transparent; }
QScrollBar::up-arrow, QScrollBar::down-arrow, QScrollBar::left-arrow, QScrollBar::right-arrow { width: 0; height: 0; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; border: none; }
QAbstractScrollArea::corner { background: #181a20; border: none; }
QMenuBar { color: #e8eaf0; background: #20232b; }
QMenuBar::item:selected { background: #304b7a; color: #f6f8ff; }
QMenu { color: #e8eaf0; background: #20232b; border: 1px solid #626b7e; padding: 5px; }
QMenu::item { color: #e8eaf0; padding: 9px 28px 9px 14px; border-radius: 4px; }
QMenu::item:selected { background: #304b7a; color: #f6f8ff; }
QMenu::item:disabled { color: #979eac; }
QMenu::separator { background: #414959; height: 1px; margin: 5px 8px; }
QStatusBar { background: #20232b; color: #b4bdcd; border-top: 1px solid #353b48; padding: 4px; }
QStatusBar::item { border: none; }
QToolTip { background: #292d37; color: #f6f8ff; border: 1px solid #76839a; padding: 7px; }
QCalendarWidget { background: #181a20; border: 1px solid #515b6d; border-radius: 10px; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #20232b; border: none; border-top-left-radius: 9px; border-top-right-radius: 9px; }
QCalendarWidget QToolButton { color: #e8eaf0; background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 7px 9px; font-weight: 600; }
QCalendarWidget QToolButton:hover { background: #353c49; }
QCalendarWidget QToolButton:pressed { background: #293e60; }
QCalendarWidget QToolButton:focus { border-color: #8baeff; }
QCalendarWidget QToolButton:disabled { color: #979eac; background: transparent; }
QCalendarWidget QToolButton::menu-indicator { image: none; width: 0; }
QCalendarWidget QToolButton#qt_calendar_monthbutton { padding-right: 24px; }
QCalendarWidget QToolButton#qt_calendar_monthbutton::menu-indicator { image: url("@UI_ASSETS@/chevron-down.svg"); width: 10px; height: 10px; subcontrol-position: right center; right: 7px; }
QCalendarWidget QToolButton#qt_calendar_prevmonth { qproperty-icon: url("@UI_ASSETS@/chevron-left.svg"); }
QCalendarWidget QToolButton#qt_calendar_nextmonth { qproperty-icon: url("@UI_ASSETS@/chevron-right.svg"); }
QCalendarWidget QAbstractItemView { color: #e8eaf0; background: #181a20; alternate-background-color: #181a20; border: none; border-radius: 0; padding: 4px; selection-background-color: #304b7a; selection-color: #f6f8ff; outline: none; }
QCalendarWidget QAbstractItemView::item { padding: 0; border: none; }
QCalendarWidget QAbstractItemView::item:hover { background: transparent; }
QCalendarWidget QAbstractItemView::item:selected { background: #304b7a; }
QCalendarWidget QAbstractItemView::item:selected:!active { background: #304b7a; }
QCalendarWidget QSpinBox { color: #e8eaf0; background: #181a20; border: 1px solid #8baeff; padding: 4px 30px 4px 6px; min-height: 22px; }
QCalendarWidget QWidget#calendarFooter { background: #20232b; border: none; border-top: 1px solid #353b48; border-bottom-left-radius: 9px; border-bottom-right-radius: 9px; }
QCalendarWidget QLabel#calendarSelection { color: #a8afbe; font-size: 9pt; }
QCalendarWidget QPushButton#calendarToday { color: #b8ceff; background: transparent; border: 1px solid transparent; padding: 6px 10px; }
QCalendarWidget QPushButton#calendarToday:hover { background: #303b4e; }
QCalendarWidget QPushButton#calendarToday:focus { border-color: #8baeff; }
QCalendarWidget QPushButton#calendarToday:disabled { color: #979eac; }
QProgressBar { background: #181a20; color: #e8eaf0; border: 1px solid #626b7e; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #668ed2; }
"""
