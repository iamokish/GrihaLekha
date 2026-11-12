"""The desktop workspace: flat selection, tenant ledgers and recovery tools."""

# Qt callback boundaries report service/filesystem failures through _error or a
# visible backup status. They must not let an exception escape the event loop.
# ruff: noqa: BLE001

from __future__ import annotations

import logging
import os
import shutil
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..backups import BackupManager
from ..clock import today_ist
from ..currencies import CURRENCIES, get_currency
from ..meter import format_meter_reading
from .dialogs import (
    AmountActionDialog,
    BackupImportDialog,
    BuildingDialog,
    ChargeDialog,
    ExportDialog,
    FlatDialog,
    HouseholdDialog,
    MoveOutDialog,
    OpeningDialog,
    PostDraftDialog,
    RateDialog,
    ReasonDialog,
    TenancyDialog,
    flat_label,
)
from .bill_dialogs import BillAmountDialog, BillCorrectionDialog, BillDialog, BillPaymentDialog, MeterReadingDialog, UnpaidDialog
from .bill_table import render_bill_cycles, select_bill
from .lifecycle_dialogs import (
    CancelMoveDialog, CancelMoveOutDialog, CancelUpcomingTenantDialog, DeleteArchivedTenantDialog, ExchangeDialog, TransferDialog,
)
from .widgets import (
    MetricCard,
    button,
    date_text,
    horizontal,
    label,
    money,
    selected_id,
    set_rows,
    table,
)

logger = logging.getLogger(__name__)


def _account_action(method):
    """Keep database/read failures inside the Qt callback boundary."""
    @wraps(method)
    def run(self):
        try:
            return method(self)
        except Exception as exc:
            self._error(exc)
    return run


def _tenancy_status(tenancy: dict) -> str:
    if tenancy.get("archived_on"):
        return "Archived"
    if tenancy.get("cancelled_on"):
        return "Cancelled"
    today = today_ist().isoformat()
    if tenancy["move_in"] > today:
        return "Upcoming"
    last_day = tenancy.get("move_out")
    if last_day and last_day < today:
        return "Former tenant"
    if last_day:
        return f"Leaving {date_text(last_day)}"
    return "Active"


class _BackupWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, manager: BackupManager, destination: Path | None = None):
        super().__init__()
        self.manager = manager
        self.destination = destination

    @Slot()
    def run(self):
        try:
            path = self.manager.create(self.destination)
        except Exception as exc:
            logger.exception("Backup failed")
            self.failed.emit(str(exc))
        else:
            self.finished.emit(str(path))


class _RestoreWorker(QObject):
    finished = Signal()

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.result = None
        self.error = None

    @Slot()
    def run(self):
        try:
            self.result = self.operation()
        except Exception as exc:
            self.error = exc
        finally:
            self.finished.emit()


class _RestoreProgress(QDialog):
    def __init__(self, message, parent):
        super().__init__(parent)
        self.setWindowTitle("Import from backup")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(label(message))
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)

    def reject(self):
        # File replacement cannot be interrupted halfway through a restore.
        pass

    def closeEvent(self, event):
        event.ignore()


class _CurrencyComboBox(QComboBox):
    """Keep the current code readable while listing full names in the popup."""

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        code = self.currentData()
        if code is not None:
            currency = get_currency(code)
            option.currentText = f"{currency.code} ({currency.symbol})"
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class MainWindow(QMainWindow):
    """Owns UI state; all financial rules are enforced by the Ledger service."""

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.currency_code = ledger.get_currency_code()
        self.backups = BackupManager(ledger.data_dir, ledger.db_path)
        self.setWindowTitle("GrihaLekha · Rental accounts")
        screen = self.screen().availableGeometry()
        available_width = max(320, screen.width() - 32)
        available_height = max(240, screen.height() - 56)
        self.setMinimumSize(min(1040, available_width), min(690, available_height))
        self.resize(min(1360, available_width), min(890, available_height))
        self._dirty = False
        self._close_requested = False
        self._skip_close_backup = False
        self._backup_thread: QThread | None = None
        self._backup_worker = None
        self._restoring = False
        self._pdf_jobs = {}
        self._backup_revision = ledger.get_revision()
        self._document_revision = 0
        self._backup_capture = None
        self._last_calendar_date = today_ist()
        self._buildings = []
        self._flats = []
        self._tenancies = []
        self._charges = []
        self._bill_cycles = []
        self._account_is_archived = False
        self._payments = []
        self._movements = []
        self._rates = []
        self._selected_tenancy = None
        self._build_shell()
        self._build_overview()
        self._build_properties()
        self._build_accounts()
        self._build_backups()
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(30 * 60 * 1000)
        self._backup_timer.timeout.connect(self._automatic_backup)
        self._backup_timer.start()
        self._calendar_timer = QTimer(self)
        self._calendar_timer.setInterval(60 * 1000)
        self._calendar_timer.timeout.connect(self._refresh_calendar)
        self._calendar_timer.start()
        try:
            if self.ledger.prepare_monthly_drafts():
                self._dirty = True
        except Exception:
            logger.exception("Could not prepare monthly drafts on startup")
        self.refresh()
        self.navigate(0)

    def _build_shell(self):
        shell = QWidget()
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 32, 18, 22)
        side.setSpacing(9)
        side.addWidget(label("GrihaLekha", "brand"))
        side.addWidget(label("गृह लेखा  /  RENTAL ACCOUNTS", "brandHint"))
        side.addSpacing(39)
        self.nav_buttons = []
        for i, title in enumerate(
            ["Overview", "Buildings and flats", "Tenant accounts", "Backups and data"]
        ):
            nav = button(title, lambda checked=False, index=i: self.navigate(index))
            nav.setObjectName("nav")
            nav.setCheckable(True)
            self.nav_buttons.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(label("YOUR RECORDS, TOGETHER", "brandHint"))
        side.addWidget(
            label(
                "Rent. Bills. Every payment.\nStored on this computer.",
                "sideFoot",
                True,
            )
        )
        side.addSpacing(12)
        currency_label = label("Display currency", "sideFoot")
        side.addWidget(currency_label)
        self.currency_combo = _CurrencyComboBox()
        self.currency_combo.setAccessibleName("Display currency")
        self.currency_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.currency_combo.setMinimumContentsLength(8)
        for currency in CURRENCIES:
            self.currency_combo.addItem(
                f"{currency.code} ({currency.symbol}) · {currency.name}", currency.code
            )
        self.currency_combo.view().setMinimumWidth(300)
        self.currency_combo.setCurrentIndex(self.currency_combo.findData(self.currency_code))
        self.currency_combo.currentIndexChanged.connect(self._currency_changed)
        self.currency_combo.setToolTip("Only symbols and labels change. Amounts stay the same.")
        currency_label.setBuddy(self.currency_combo)
        side.addWidget(self.currency_combo)
        side.addWidget(label("Only symbols and labels change. Amounts stay the same.", "sideFoot", True))
        self.currency_footer = label(f"{self.currency_code} {self.currency_symbol}  ·  Offline desktop", "sideFoot")
        side.addWidget(self.currency_footer)
        outer.addWidget(sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(30, 25, 30, 20)
        content_layout.setSpacing(23)
        header = QHBoxLayout()
        self.page_title = label("Overview", "pageTitle")
        header.addWidget(self.page_title)
        header.addStretch()
        header.addWidget(label("OFFLINE", "badge"))
        header.addSpacing(12)
        self.header_date = label(today_ist().strftime("%d %B %Y"), "muted")
        header.addWidget(self.header_date)
        content_layout.addLayout(header)
        self.pages = QStackedWidget()
        content_layout.addWidget(self.pages)
        outer.addWidget(content, 1)
        self.setCentralWidget(shell)
        self.statusBar().showMessage(
            f"Ready · All amounts in {self.currency_code} ({self.currency_symbol}), with two decimal places"
        )

    @property
    def currency_symbol(self):
        return get_currency(self.currency_code).symbol

    def money(self, amount):
        return money(amount, currency_code=self.currency_code)

    def _sync_currency(self):
        """Reload the preference from this account folder, including after restore."""
        self.currency_code = self.ledger.get_currency_code()
        with QSignalBlocker(self.currency_combo):
            self.currency_combo.setCurrentIndex(self.currency_combo.findData(self.currency_code))
        self.currency_footer.setText(f"{self.currency_code} {self.currency_symbol}  ·  Offline desktop")

    def _currency_changed(self, *args):
        code = self.currency_combo.currentData()
        if code is None or code == self.currency_code:
            return
        try:
            self.ledger.set_currency_code(code)
        except Exception as exc:
            with QSignalBlocker(self.currency_combo):
                self.currency_combo.setCurrentIndex(self.currency_combo.findData(self.currency_code))
            self._error(exc)
            return
        self._dirty = True
        self.refresh()
        self.statusBar().showMessage(
            f"Display currency changed to {self.currency_code} ({self.currency_symbol}). Amounts are unchanged.", 10000
        )

    def _page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setWidget(page)
        self.pages.addWidget(scroller)
        return layout

    def _build_overview(self):
        page = self._page()
        welcome = QHBoxLayout()
        welcome.addWidget(label("A clear view of your rental accounts.", "muted"))
        welcome.addStretch()
        welcome.addWidget(button("Review monthly bills", self.review_drafts))
        page.addLayout(welcome)
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_due = MetricCard(
            "Unpaid bills", "Posted charges still to settle", True
        )
        self.metric_credit = MetricCard(
            "Advance credit", "Available against future bills"
        )
        self.metric_occupancy = MetricCard(
            "Occupied flats", "Space to grow, with no fixed flat limit"
        )
        self.metric_drafts = MetricCard("Review pending", "Rent and electricity drafts")
        for card in (
            self.metric_due,
            self.metric_credit,
            self.metric_occupancy,
            self.metric_drafts,
        ):
            metrics.addWidget(card)
        page.addLayout(metrics)
        self.welcome_card = QFrame()
        self.welcome_card.setObjectName("notice")
        welcome_layout = QHBoxLayout(self.welcome_card)
        welcome_layout.setContentsMargins(21, 19, 21, 19)
        words = QVBoxLayout()
        words.addWidget(label("Welcome to your new rental workspace", "sectionTitle"))
        words.addWidget(
            label(
                "Start with a building, add its flats, then create a tenant household.\nYour opening dues and advances can be entered after setup.",
                "muted",
                True,
            )
        )
        welcome_layout.addLayout(words, 1)
        welcome_layout.addWidget(
            button("Add your first building", self.add_building, primary=True)
        )
        welcome_layout.addWidget(button("Import from backup…", self.restore_backup))
        page.addWidget(self.welcome_card)
        row = QHBoxLayout()
        row.addWidget(label("Accounts at a glance", "sectionTitle"))
        row.addStretch()
        row.addWidget(button("+ Add tenant", self.add_tenancy, primary=True))
        page.addLayout(row)
        self.overview_table = table(
            [
                "Building / flat",
                "Tenant",
                "Amount pending",
                "Past due date",
                "Advance",
                "Tenancy",
            ]
        )
        self.overview_table.setMinimumHeight(255)
        self.overview_table.cellDoubleClicked.connect(self._open_overview_account)
        page.addWidget(self.overview_table, 1)
        page.addWidget(
            label(
                "Double-click an account to review its bills and payment history. Former tenants keep their own balances.",
                "muted",
                True,
            )
        )
        self.overview_footer = label("", "muted", True)
        page.addWidget(self.overview_footer)

    def _build_properties(self):
        page = self._page()
        actions = QHBoxLayout()
        actions.addWidget(label("Keep every address and flat in one place.", "muted"))
        actions.addStretch()
        actions.addWidget(button("+ Building", self.add_building))
        actions.addWidget(button("+ Flat", self.add_flat, primary=True))
        page.addLayout(actions)
        page.addWidget(horizontal(
            label("Buildings", "sectionTitle"),
            button("Edit building", self.edit_building),
            button("Remove building", self.remove_building, danger=True),
            stretch=True,
        ))
        self.buildings_table = table(["Building", "Address", "Flats"])
        self.buildings_table.setMaximumHeight(180)
        self.buildings_table.cellDoubleClicked.connect(
            lambda row, column: self.edit_building()
        )
        self.buildings_table.itemSelectionChanged.connect(self._building_selected)
        page.addWidget(self.buildings_table)
        flats_heading = QHBoxLayout()
        flats_heading.addWidget(label("Flats", "sectionTitle"))
        flats_heading.addStretch()
        self.flat_filter = QComboBox()
        self.flat_filter.setMinimumWidth(210)
        self.flat_filter.currentIndexChanged.connect(self._refresh_flats_table)
        flats_heading.addWidget(self.flat_filter)
        flats_heading.addWidget(button("Edit flat", self.edit_flat))
        flats_heading.addWidget(button("Remove flat", self.remove_flat, danger=True))
        flats_heading.addWidget(button("Add tenant to a flat", self.add_tenancy))
        page.addLayout(flats_heading)
        self.flats_table = table(
            ["Building", "Flat", "Floor", "Type", "Tenant", "Status"]
        )
        self.flats_table.setMinimumHeight(255)
        self.flats_table.cellDoubleClicked.connect(self._open_flat_account)
        page.addWidget(self.flats_table, 1)
        page.addWidget(
            label(
                "Flat numbers are unique within a building. Remove unused flats, then empty buildings. Flats with tenant history are kept for their accounts.",
                "muted",
                True,
            )
        )

    def _build_accounts(self):
        page = self._page()
        self._account_mutations = []
        page.setSpacing(13)
        selector = QHBoxLayout()
        selector.addWidget(label("SELECT FLAT / HOUSEHOLD", "eyebrow"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(300)
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        selector.addWidget(self.account_combo, 1)
        self.show_archived = QCheckBox("Show archived")
        self.show_archived.setToolTip("Include closed accounts for viewing and reopening")
        self.show_archived.toggled.connect(self.refresh)
        selector.addWidget(self.show_archived)
        selector.addWidget(button("+ Tenant", self.add_tenancy))
        page.addLayout(selector)
        self.account_header = label(
            "Add a tenant to start an account", "sectionTitle", True
        )
        self.account_subtitle = label(
            "Choose a flat and household to see bills, payments and available credit.",
            "muted",
            True,
        )
        page.addWidget(self.account_header)
        page.addWidget(self.account_subtitle)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.account_due = MetricCard("Amount pending", "Unpaid posted bills", True)
        self.account_overdue = MetricCard(
            "Past due date", "After any agreed grace days", True
        )
        self.account_credit = MetricCard("Available balance", "Add payments, then mark bills Paid")
        self.account_deposit = MetricCard(
            "Security deposit", "Held separately from rent"
        )
        for card in (
            self.account_due,
            self.account_overdue,
            self.account_credit,
            self.account_deposit,
        ):
            cards.addWidget(card)
        page.addLayout(cards)
        toolbar = QHBoxLayout()
        self.receive_button = self._account_button("+ Add payment", self.receive_payment, primary=True)
        self.charge_button = self._account_button("+ Add charges", self.add_charges)
        self.export_button = button("Export statement…", self.export_statement)
        self.more_button = button("Account actions")
        self.more_button.setObjectName("menuButton")
        more = QMenu(self)
        self._occupancy_actions = []
        for text, action in [
            ("Edit household details", self.edit_household),
            ("Set opening balances", self.opening_balances),
            ("Change monthly rent", self.change_rent),
            ("Refund available balance", self.refund_advance),
            ("Receive security deposit", lambda: self.deposit_action("receive")),
            ("Return security deposit", lambda: self.deposit_action("return")),
            ("Transfer deposit to balance", lambda: self.deposit_action("apply")),
            ("Transfer balance to security deposit", lambda: self.deposit_action("from_balance")),
            ("Record / change move-out", self.end_tenancy),
            ("Move to another flat", self.transfer_tenant),
            ("Exchange flats with another tenant", self.exchange_tenants),
        ]:
            entry = more.addAction(text, action)
            self._account_mutations.append(entry)
            if text in {"Set opening balances", "Change monthly rent", "Receive security deposit",
                        "Transfer balance to security deposit", "Record / change move-out",
                        "Move to another flat", "Exchange flats with another tenant"}:
                self._occupancy_actions.append(entry)
        self.cancel_upcoming_action = more.addAction("Cancel upcoming tenant…", self.cancel_upcoming_tenant)
        self._account_mutations.append(self.cancel_upcoming_action)
        self.cancel_transfer_action = more.addAction("Cancel planned flat move", self.cancel_planned_move)
        self._account_mutations.append(self.cancel_transfer_action)
        self.cancel_moveout_action = more.addAction("Cancel planned move-out", self.cancel_planned_move_out)
        self._account_mutations.append(self.cancel_moveout_action)
        more.addSeparator()
        self.archive_action = more.addAction("Close and archive account", self.archive_account)
        self.archive_action.setToolTip("Available after departure; bills, advance and deposit must be settled")
        self.reopen_action = more.addAction("Reopen archived account", self.reopen_account)
        self.delete_tenant_action = more.addAction("Delete archived tenant data…", self.delete_archived_account)
        self.more_button.setMenu(more)
        for item in (self.receive_button, self.charge_button, self.more_button):
            toolbar.addWidget(item)
        toolbar.addStretch()
        toolbar.addWidget(self.export_button)
        page.addLayout(toolbar)
        self.account_tabs = QTabWidget()
        charge_tab = QWidget()
        charge_layout = QVBoxLayout(charge_tab)
        charge_layout.setContentsMargins(13, 13, 13, 13)
        self.charges_table = table(
            [
                "Period",
                "Charge",
                "Status",
                "Billed",
                "Amount paid",
                "Amount pending",
                "Due date",
                "Reason",
            ]
        )
        self.charges_table.setMinimumHeight(240)
        self.charges_table.cellDoubleClicked.connect(
            lambda row, col: self.open_selected_bill()
        )
        self.charges_table.itemSelectionChanged.connect(self._bill_selection_changed)
        self.cycle_lock_button = self._account_button("Lock cycle", self.toggle_cycle_lock)
        self.cycle_lock_button.setEnabled(False)
        bill_actions = self._account_button("Bill actions", None)
        bill_menu = QMenu(self)
        self.correct_bill_action = bill_menu.addAction("Correct bill amount…", self.correct_selected_bill_amount)
        self.meter_bill_action = bill_menu.addAction("Edit electric meter reading…", self.edit_selected_meter_reading)
        self.defer_bill_action = bill_menu.addAction("Skip electricity this cycle…", self.toggle_electricity_deferral)
        self.cancel_bill_action = bill_menu.addAction("Cancel miscellaneous charge", self.cancel_charge)
        self._account_mutations.extend((self.correct_bill_action, self.cancel_bill_action,
                                        self.meter_bill_action, self.defer_bill_action))
        for text, action in (
            ("Replace cancelled bill", self.replace_cancelled_charge),
            ("Prepare missing drafts", self.prepare_drafts),
        ):
            self._account_mutations.append(bill_menu.addAction(text, action))
        bill_menu.addSeparator()
        self.undo_bill_action = bill_menu.addAction("Undo bill cancellation…", self.undo_cancelled_bill)
        self.delete_bill_action = bill_menu.addAction("Delete miscellaneous bill…", self.delete_selected_bill)
        self.delete_bill_action.setToolTip("Delete bills other than rent or electricity; internal history is retained")
        self._account_mutations.extend((self.undo_bill_action, self.delete_bill_action))
        bill_actions.setMenu(bill_menu)
        charge_layout.addWidget(
            horizontal(
                button("Open / review bill", self.open_selected_bill, primary=True),
                self.cycle_lock_button,
                bill_actions,
                stretch=True,
            )
        )
        charge_layout.addWidget(self.charges_table)
        self.account_tabs.addTab(charge_tab, "Bills and monthly drafts")
        payment_tab = QWidget()
        payment_layout = QVBoxLayout(payment_tab)
        payment_layout.setContentsMargins(13, 13, 13, 13)
        self.payments_table = table(
            [
                "Actual date",
                "Amount",
                "Method",
                "Reference",
                "Available balance",
                "Proof",
                "Status",
            ]
        )
        self.payments_table.setMinimumHeight(240)
        payment_layout.addWidget(self.payments_table)
        payment_layout.addWidget(
            horizontal(
                button("Payment receipt…", self.export_receipt),
                button("Open selected proof", self.open_proof),
                self._account_button("Reverse payment", self.reverse_payment, danger=True),
                stretch=True,
            )
        )
        self.account_tabs.addTab(payment_tab, "Payments")
        family_tab = QWidget()
        family_layout = QVBoxLayout(family_tab)
        family_layout.setContentsMargins(18, 18, 18, 18)
        self.household_details = label("", "muted", True)
        family_layout.addWidget(self.household_details)
        family_layout.addWidget(label("Flat history", "sectionTitle"))
        self.occupancy_table = table(["First day", "Last day", "Building / flat"])
        self.occupancy_table.setMinimumHeight(110)
        self.occupancy_table.setMaximumHeight(155)
        family_layout.addWidget(self.occupancy_table)
        self.family_table = table(["Family member", "Relationship"])
        self.family_table.setMaximumHeight(130)
        family_layout.addWidget(self.family_table)
        family_layout.addWidget(self._account_button("Edit household details", self.edit_household))
        family_layout.addWidget(label("Agreed rent and future changes", "sectionTitle"))
        self.rates_table = table(["Effective from", "Monthly rent", "Status"])
        self.rates_table.setMaximumHeight(150)
        family_layout.addWidget(self.rates_table)
        family_layout.addWidget(
            horizontal(
                self._account_button("Schedule new rent", self.change_rent),
                self._account_button("Cancel selected future rate", self.cancel_future_rate),
                stretch=True,
            )
        )
        self.account_tabs.addTab(family_tab, "Household and rent")
        movements_tab = QWidget()
        movements_layout = QVBoxLayout(movements_tab)
        movements_layout.setContentsMargins(13, 13, 13, 13)
        movements_layout.addWidget(
            label(
                "Deposits are separate. Refunds record actual money returned; corrections preserve the original history.",
                "muted",
                True,
            )
        )
        self.movements_table = table(
            ["Date", "Movement", "Amount", "Method", "Reason / notes", "Status"]
        )
        movements_layout.addWidget(self.movements_table)
        movements_layout.addWidget(
            horizontal(
                self._account_button(
                    "Correct selected refund / deposit",
                    self.reverse_movement,
                    danger=True,
                ),
                stretch=True,
            )
        )
        self.account_tabs.addTab(movements_tab, "Deposits and adjustments")
        page.addWidget(self.account_tabs, 1)
        self.account_net = label("", "muted", True)
        page.addWidget(self.account_net)

    def _account_button(self, text, callback, **options):
        control = button(text, callback, **options)
        self._account_mutations.append(control)
        return control

    def _set_account_permissions(self, tenancy=None):
        archived = bool(tenancy and tenancy.get("archived_on"))
        cancelled = bool(tenancy and tenancy.get("cancelled_on"))
        self._account_is_archived = archived
        for control in self._account_mutations:
            control.setEnabled(tenancy is not None and not archived)
        for control in self._occupancy_actions:
            control.setEnabled(tenancy is not None and not archived and not cancelled)
        history = tenancy.get("occupancy_history", []) if tenancy else []
        self.cancel_upcoming_action.setEnabled(bool(tenancy and not archived and not cancelled and tenancy["move_in"] > today_ist().isoformat()))
        self.cancel_transfer_action.setEnabled(bool(not archived and not cancelled and len(history) > 1 and history[-1]["starts_on"] > today_ist().isoformat()))
        self.cancel_moveout_action.setEnabled(bool(tenancy and not archived and not cancelled and tenancy.get("move_out") and tenancy["move_out"] > today_ist().isoformat()))
        self.archive_action.setVisible(not archived)
        self.archive_action.setEnabled(bool(tenancy and (cancelled or (tenancy.get("move_out") and tenancy["move_out"] < today_ist().isoformat()))) and not archived)
        self.reopen_action.setVisible(archived)
        self.reopen_action.setEnabled(archived)
        self.delete_tenant_action.setVisible(archived)
        self.delete_tenant_action.setEnabled(archived)

    def _build_backups(self):
        page = self._page()
        page.addWidget(
            label(
                "Two complete restore points. Your proofs travel with your accounts.",
                "muted",
                True,
            )
        )
        info = QFrame()
        info.setObjectName("notice")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(21, 18, 21, 18)
        info_layout.addWidget(label("Automatic backups", "sectionTitle"))
        info_layout.addWidget(
            label(
                "GrihaLekha keeps the latest two successful backups. Changes are backed up every 30 minutes and when you close the app normally.\nEach backup includes the database, payment proofs and issued PDFs.",
                "muted",
                True,
            )
        )
        self.backup_status = label("", wrap=True)
        info_layout.addWidget(self.backup_status)
        page.addWidget(info)
        backup_actions = QHBoxLayout()
        self.backup_now_button = button("Back up now", self.backup_now, primary=True)
        self.backup_export_button = button("Export to drive…", self.export_backup)
        backup_actions.addWidget(self.backup_now_button)
        backup_actions.addWidget(self.backup_export_button)
        backup_actions.addStretch()
        backup_actions.addWidget(button("Import from backup…", self.restore_backup))
        page.addLayout(backup_actions)
        restore_heading = QHBoxLayout()
        restore_heading.addWidget(label("Managed restore points", "sectionTitle"))
        restore_heading.addStretch()
        restore_heading.addWidget(button("Restore selected…", self.restore_selected_backup))
        page.addLayout(restore_heading)
        self.backups_table = table(["Backup file", "Size", "Location"])
        self.backups_table.setMinimumHeight(220)
        self.backups_table.cellDoubleClicked.connect(lambda row, column: self.restore_selected_backup())
        page.addWidget(self.backups_table, 1)
        page.addWidget(
            label(
                "An external-drive copy also protects you if this computer's drive fails. Manual exports are kept until you remove them.",
                "muted",
                True,
            )
        )
        page.addWidget(label("Application data", "sectionTitle"))
        self.data_path_label = label(str(self.ledger.data_dir), "muted", True)
        self.data_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        page.addWidget(self.data_path_label)
        page.addWidget(
            horizontal(
                button(
                    "Open data folder",
                    lambda: QDesktopServices.openUrl(
                        QUrl.fromLocalFile(str(self.ledger.data_dir))
                    ),
                ),
                stretch=True,
            )
        )

    def navigate(self, index: int):
        self.pages.setCurrentIndex(index)
        self.page_title.setText(
            ["Overview", "Buildings & flats", "Tenant accounts", "Backups & data"][
                index
            ]
        )
        self.page_title.updateGeometry()
        for i, nav in enumerate(self.nav_buttons):
            nav.setChecked(i == index)
        if index == 3:
            self._refresh_backups()

    def _refresh_calendar(self):
        if self._restoring:
            return
        today = today_ist()
        if today == self._last_calendar_date:
            return
        self._last_calendar_date = today
        self.header_date.setText(today.strftime("%d %B %Y"))
        try:
            if self.ledger.prepare_monthly_drafts(as_of=today.isoformat()):
                self._dirty = True
            self.refresh()
        except Exception as exc:
            self._error(exc)

    def _error(self, exc: Exception):
        if isinstance(exc, (ValueError, FileNotFoundError)):
            QMessageBox.warning(self, "Please check this entry", str(exc))
        else:
            logger.exception("Desktop operation failed", exc_info=exc)
            QMessageBox.critical(
                self,
                "Unable to complete this action",
                f"Your saved records have been kept.\n\n{exc}",
            )

    def _write(self, operation, message: str, refresh: bool = True):
        try:
            result = operation()
            self._dirty = True
            if refresh:
                self.refresh()
            self.statusBar().showMessage(message, 10000)
            return True, result
        except Exception as exc:
            self._error(exc)
            return False, None

    def refresh(self):
        try:
            self._sync_currency()
            self._buildings = self.ledger.list_buildings()
            self._flats = self.ledger.list_flats()
            self._tenancies = self.ledger.list_tenancies(include_archived=True)
            dashboard = self.ledger.dashboard()
            self.metric_due.value.setText(self.money(dashboard["outstanding_paise"]))
            self.metric_credit.value.setText(self.money(dashboard["credit_paise"]))
            self.metric_occupancy.value.setText(
                f"{dashboard['occupied_flats']} / {dashboard['total_flats']}"
            )
            self.metric_drafts.value.setText(str(dashboard["draft_count"]))
            self.welcome_card.setVisible(not self._buildings)
            self.overview_footer.setText(
                f"Past due date: {self.money(dashboard['overdue_paise'])}    ·    Deposits held separately: {self.money(dashboard['deposit_paise'])}"
            )
            overview_rows, overview_ids = [], []
            for tenancy in self._tenancies:
                if tenancy.get("archived_on"):
                    continue
                balance = self.ledger.balance(tenancy["id"])
                state = _tenancy_status(tenancy)
                if (
                    state == "Former tenant"
                    and not balance["outstanding_paise"]
                    and not balance["credit_paise"]
                    and not balance["deposit_paise"]
                ):
                    continue
                overview_rows.append(
                    (
                        flat_label(tenancy),
                        tenancy["tenant_name"],
                        self.money(balance["outstanding_paise"]),
                        self.money(balance["overdue_paise"]),
                        self.money(balance["credit_paise"]),
                        state,
                    )
                )
                overview_ids.append(tenancy["id"])
            set_rows(self.overview_table, overview_rows, overview_ids)
            set_rows(
                self.buildings_table,
                [
                    (
                        b["name"],
                        b["address"],
                        sum(f["building_id"] == b["id"] for f in self._flats),
                    )
                    for b in self._buildings
                ],
                [b["id"] for b in self._buildings],
            )
            filtered = self.flat_filter.currentData()
            self.flat_filter.blockSignals(True)
            self.flat_filter.clear()
            self.flat_filter.addItem("All buildings", None)
            for building in self._buildings:
                self.flat_filter.addItem(building["name"], building["id"])
            index = self.flat_filter.findData(filtered)
            self.flat_filter.setCurrentIndex(max(0, index))
            self.flat_filter.blockSignals(False)
            self._refresh_flats_table()
            selected = self._selected_tenancy
            self.account_combo.blockSignals(True)
            self.account_combo.clear()
            for tenancy in self._tenancies:
                if tenancy.get("archived_on") and not self.show_archived.isChecked():
                    continue
                state = _tenancy_status(tenancy)
                suffix = f" · {state}" if state != "Active" else ""
                self.account_combo.addItem(
                    f"{flat_label(tenancy)}  —  {tenancy['tenant_name']}{suffix}",
                    tenancy["id"],
                )
            index = self.account_combo.findData(selected)
            self.account_combo.setCurrentIndex(max(0, index) if self.account_combo.count() else -1)
            self.account_combo.blockSignals(False)
            self._account_changed()
            self._refresh_backups()
        except Exception as exc:
            self._error(exc)

    def _building_selected(self):
        if not self.buildings_table.selectedItems():
            return
        building_id = selected_id(self.buildings_table)
        index = self.flat_filter.findData(building_id)
        if index >= 0:
            self.flat_filter.setCurrentIndex(index)

    def _refresh_flats_table(self):
        if not hasattr(self, "flats_table"):
            return
        selected = self.flat_filter.currentData()
        # Both selectors describe the same view. Block the table's selection
        # callback while reflecting a dropdown change or restoring a refresh.
        previous = self.buildings_table.blockSignals(True)
        try:
            building_row = next(
                (i for i, building in enumerate(self._buildings) if building["id"] == selected),
                None,
            )
            if building_row is None:
                self.buildings_table.clearSelection()
                self.buildings_table.setCurrentCell(-1, -1)
            else:
                self.buildings_table.selectRow(building_row)
        finally:
            self.buildings_table.blockSignals(previous)
        flats = [
            f for f in self._flats if selected is None or f["building_id"] == selected
        ]
        set_rows(
            self.flats_table,
            [
                (
                    f["building_name"],
                    f["number"],
                    f["floor"],
                    f"{f['bhk']} BHK",
                    f.get("tenant_name") or "—",
                    "Occupied" if f.get("tenancy_id") else "Vacant",
                )
                for f in flats
            ],
            [f["id"] for f in flats],
        )

    def _account_changed(self):
        self._selected_tenancy = self.account_combo.currentData()
        enabled = self._selected_tenancy is not None
        self._set_account_permissions()
        for control in (
            self.receive_button,
            self.charge_button,
            self.export_button,
            self.more_button,
            self.account_tabs,
        ):
            control.setEnabled(enabled)
        if not enabled:
            self._charges, self._payments, self._movements, self._rates = [], [], [], []
            self._bill_cycles = []
            for control in (
                self.charges_table,
                self.payments_table,
                self.family_table,
                self.movements_table,
                self.rates_table,
                self.occupancy_table,
            ):
                control.setRowCount(0)
            for card in (
                self.account_due,
                self.account_overdue,
                self.account_credit,
                self.account_deposit,
            ):
                card.value.setText(self.money(0))
            self.account_header.setText("Add a tenant to start an account")
            self.account_subtitle.setText(
                "Choose a flat and household to see bills, payments and available credit."
            )
            self.account_net.setText("")
            self.household_details.setText("")
            return
        try:
            tenancy = self.ledger.get_tenancy(self._selected_tenancy)
            self._set_account_permissions(tenancy)
            balance = self.ledger.balance(self._selected_tenancy)
            self.account_header.setText(
                f"{tenancy['tenant_name']}  ·  {flat_label(tenancy)}"
            )
            state = _tenancy_status(tenancy)
            if tenancy.get("cancelled_on"):
                status = f"Cancelled {date_text(tenancy['cancelled_on'])} · Flat reservation released · {tenancy.get('cancellation_reason', '')}"
                if tenancy.get("archived_on"):
                    status += f" · Archived {date_text(tenancy['archived_on'])} · Read only"
            elif state == "Archived":
                status = f"Moved out {date_text(tenancy['move_out'])} · Archived {date_text(tenancy['archived_on'])} · Read only; reopen to make changes"
            elif state == "Former tenant":
                status = f"Moved out {date_text(tenancy['move_out'])}"
            elif state == "Upcoming":
                status = f"Moves in {date_text(tenancy['move_in'])}"
            else:
                status = f"Since {date_text(tenancy['move_in'])}" + (
                    f" · {state}" if state != "Active" else ""
                )
            self.account_subtitle.setText(
                f"{tenancy['address']}  ·  Floor {tenancy['floor']}  ·  {tenancy['bhk']} BHK\n{status}  ·  Monthly rent {self.money(tenancy['rent_paise'])}  ·  Cycle starts on day {tenancy['billing_day']}  ·  Grace: {tenancy['grace_days']} days"
            )
            for card, key in (
                (self.account_due, "outstanding_paise"),
                (self.account_overdue, "overdue_paise"),
                (self.account_credit, "credit_paise"),
                (self.account_deposit, "deposit_paise"),
            ):
                card.value.setText(self.money(balance[key]))
            net = balance["net_paise"]
            self.account_net.setText(
                f"Balance as of {date_text(today_ist())}: {self.money(abs(net))} {'credit' if net < 0 else 'payable'}    ·    Unposted drafts and separately held deposits are excluded."
            )
            self._bill_cycles = self.ledger.list_bill_cycles(self._selected_tenancy)
            self._charges = [bill for cycle in self._bill_cycles for bill in cycle["charges"]]
            render_bill_cycles(self.charges_table, self._bill_cycles, currency_code=self.currency_code)
            self._bill_selection_changed()
            self._payments = self.ledger.list_payments(self._selected_tenancy)
            set_rows(
                self.payments_table,
                [
                    (
                        date_text(p["paid_on"]),
                        self.money(p["amount_paise"]),
                        p["method"].upper()
                        if p["method"] == "upi"
                        else p["method"].title(),
                        p.get("reference", ""),
                        self.money(p["available_paise"]),
                        "Attached" if p.get("proof_path") else "—",
                        "Reversed"
                        if p.get("reversed_on")
                        else "Opening entry"
                        if p.get("opening")
                        else "Recorded",
                    )
                    for p in self._payments
                ],
                [p["id"] for p in self._payments],
            )
            self.household_details.setText(
                f"Primary tenant: {tenancy['tenant_name']}\nPhone: {tenancy.get('phone') or 'Not provided'}\n"
                + ("Planned move-in: " if tenancy.get("cancelled_on") else "Move-in: ") + date_text(tenancy["move_in"])
                + (
                    f"\nLast occupied day: {date_text(tenancy['move_out'])}"
                    if tenancy.get("move_out")
                    else ""
                )
                + "\nInitial electric meter reading: "
                + (f"{format_meter_reading(tenancy['initial_meter_reading_hundredths'])} kWh"
                   if tenancy.get("initial_meter_reading_hundredths") is not None else "Not recorded")
            )
            set_rows(
                self.family_table,
                [(m["name"], m["relationship"]) for m in tenancy.get("members", [])],
            )
            set_rows(self.occupancy_table, [
                (date_text(occupancy["starts_on"]), "Cancelled reservation" if tenancy.get("cancelled_on") else date_text(occupancy.get("ends_on")) if occupancy.get("ends_on") else "Continuing", flat_label(occupancy))
                for occupancy in tenancy.get("occupancy_history", [])
            ])
            self._rates = tenancy.get("rent_rates", [])
            today = today_ist().isoformat()
            effective_rates = [
                rate for rate in self._rates if rate["effective_from"] <= today
            ]
            current_rate_id = (
                max(effective_rates, key=lambda rate: rate["effective_from"])["id"]
                if effective_rates
                else None
            )
            set_rows(
                self.rates_table,
                [
                    (
                        date_text(rate["effective_from"]),
                        self.money(rate["amount_paise"]),
                        "Cancelled reservation" if tenancy.get("cancelled_on") else "Scheduled"
                        if rate["effective_from"] > today
                        else "Current agreed rate"
                        if rate["id"] == current_rate_id
                        else "Earlier rate",
                    )
                    for rate in self._rates
                ],
                [rate["id"] for rate in self._rates],
            )
            snapshot = self.ledger.statement(
                self._selected_tenancy, "1900-01-01", today_ist().isoformat()
            )
            movements = []
            for collection, fallback in (
                ("deposits", "Deposit"),
                ("refunds", "Refund"),
                ("adjustments", "Correction"),
            ):
                for item in snapshot.get(collection, []):
                    when = (
                        item.get("paid_on")
                        or item.get("effective_on")
                        or item.get("date", "")
                    )
                    description = item.get("reason") or item.get("notes") or ""
                    action = item.get("action", item.get("kind", "")).replace("_", " ")
                    is_correction = (
                        collection == "adjustments" or item.get("kind") == "reversal"
                    )
                    state = "Correction" if is_correction else "Recorded"
                    if item.get("reversed_on") and not is_correction:
                        state = (
                            f"Reversed {date_text(item['reversed_on'])}"
                            if item["reversed_on"] <= today
                            else f"Reverses {date_text(item['reversed_on'])}"
                        )
                    title = (
                        "Opening deposit"
                        if collection == "deposits" and item.get("opening")
                        else f"{fallback} · {action}".rstrip(" ·")
                    )
                    movements.append(
                        {
                            "source": collection,
                            "record": item,
                            "when": when,
                            "values": (
                                date_text(when),
                                title,
                                self.money(item.get("amount_paise", 0)),
                                item.get("method", "—"),
                                description,
                                state,
                            ),
                        }
                    )
            movements.sort(key=lambda row: row["when"])
            self._movements = movements
            set_rows(self.movements_table, [row["values"] for row in movements])
        except Exception as exc:
            self._error(exc)

    def _open_overview_account(self, row, column):
        tenancy_id = selected_id(self.overview_table)
        if tenancy_id:
            self.select_account(tenancy_id)

    def _open_flat_account(self, row, column):
        flat_id = selected_id(self.flats_table)
        flat = next((f for f in self._flats if f["id"] == flat_id), None)
        if flat and flat.get("tenancy_id"):
            self.select_account(flat["tenancy_id"])
        elif flat:
            self.add_tenancy()

    def select_account(self, tenancy_id):
        self._selected_tenancy = tenancy_id
        index = self.account_combo.findData(tenancy_id)
        if index < 0 and any(tenancy["id"] == tenancy_id and tenancy.get("archived_on") for tenancy in self._tenancies):
            self.show_archived.setChecked(True)
            index = self.account_combo.findData(tenancy_id)
        if index >= 0:
            self.account_combo.setCurrentIndex(index)
            self._account_changed()
        self.navigate(2)

    def add_building(self):
        dialog = BuildingDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, _ = self._write(
                lambda: self.ledger.add_building(**dialog.result_data),
                "Building added. You can now add its flats.",
            )
            if ok:
                self.navigate(1)

    def add_flat(self):
        if not self._buildings:
            self.add_building()
            if not self._buildings:
                return
        dialog = FlatDialog(self._buildings, self)
        selected_building = self.flat_filter.currentData()
        if selected_building is not None:
            dialog.building.setCurrentIndex(dialog.building.findData(selected_building))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(
                lambda: self.ledger.add_flat(**dialog.result_data), "Flat added."
            )

    def edit_building(self):
        building_id = selected_id(self.buildings_table)
        building = next((b for b in self._buildings if b["id"] == building_id), None)
        if not building:
            QMessageBox.information(
                self, "Select a building", "Choose a building in the table first."
            )
            return
        dialog = BuildingDialog(self)
        dialog.setWindowTitle("Edit building")
        dialog.heading.setText("Edit building")
        dialog.name.setText(building["name"])
        dialog.address.setPlainText(building["address"])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(
                lambda: self.ledger.update_building(building_id, **dialog.result_data),
                "Building details updated.",
            )

    def edit_flat(self):
        flat_id = selected_id(self.flats_table)
        flat = next((f for f in self._flats if f["id"] == flat_id), None)
        if not flat:
            QMessageBox.information(
                self, "Select a flat", "Choose a flat in the table first."
            )
            return
        dialog = FlatDialog(self._buildings, self)
        dialog.setWindowTitle("Edit flat")
        dialog.heading.setText("Edit flat")
        dialog.building.setCurrentIndex(dialog.building.findData(flat["building_id"]))
        dialog.number.setText(flat["number"])
        dialog.floor.setText(flat["floor"])
        dialog.bhk.setCurrentIndex(dialog.bhk.findData(flat["bhk"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(
                lambda: self.ledger.update_flat(flat_id, **dialog.result_data),
                "Flat details updated.",
            )

    def _account_dialog(self, dialog):
        """Keep the destination account visible while entering financial data."""
        if self._selected_tenancy is not None:
            tenancy = self.ledger.get_tenancy(self._selected_tenancy)
            context = label(
                f"{tenancy['tenant_name']}  ·  {flat_label(tenancy)}", "muted", True
            )
            dialog.root.insertWidget(1, context)
        return dialog

    def remove_building(self):
        building_id = selected_id(self.buildings_table)
        building = next((b for b in self._buildings if b["id"] == building_id), None)
        if building is None:
            QMessageBox.information(self, "Select a building", "Choose a building in the table first.")
            return
        if any(f["building_id"] == building_id for f in self._flats):
            QMessageBox.information(self, "Building still has flats", "Remove or move this building's flats first. Flats with tenant history cannot be removed.")
            return
        if QMessageBox.question(
            self, "Remove building?", f"Permanently remove {building['name']}? This building has no flats.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.remove_building(building_id), "Building removed.")

    def remove_flat(self):
        flat_id = selected_id(self.flats_table)
        flat = next((f for f in self._flats if f["id"] == flat_id), None)
        if flat is None:
            QMessageBox.information(self, "Select a flat", "Choose a flat in the table first.")
            return
        if any(t["flat_id"] == flat_id for t in self._tenancies):
            QMessageBox.information(self, "Flat has tenant history", "This flat's tenant accounts and payments must remain available, so it cannot be removed.")
            return
        if QMessageBox.question(
            self, "Remove flat?", f"Permanently remove {flat_label(flat)}? This flat has no tenant history.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.remove_flat(flat_id), "Flat removed.")

    def add_tenancy(self):
        latest_tenancies = {}
        for tenancy in self._tenancies:
            prior = latest_tenancies.get(tenancy["flat_id"])
            if prior is None or tenancy["move_in"] > prior["move_in"]:
                latest_tenancies[tenancy["flat_id"]] = tenancy
        vacant = [
            flat
            for flat in self._flats
            if flat["id"] not in latest_tenancies
            or latest_tenancies[flat["id"]].get("move_out")
        ]
        if not vacant:
            QMessageBox.information(
                self,
                "Add a vacant flat",
                "Add a flat first, or record the previous tenant's move-out before creating a new tenancy.",
            )
            self.navigate(1)
            return
        dialog = TenancyDialog(vacant, self)
        selected_flat = selected_id(self.flats_table)
        if selected_flat:
            index = dialog.flat.findData(selected_flat)
            if index >= 0:
                dialog.flat.setCurrentIndex(index)

        def propose_move_in():
            previous = latest_tenancies.get(dialog.flat.currentData())
            earliest = today_ist()
            if previous and previous.get("move_out"):
                earliest = max(
                    earliest,
                    date.fromisoformat(previous["move_out"]) + timedelta(days=1),
                )
            dialog.move_in.set_iso(earliest.isoformat())

        dialog.flat.currentIndexChanged.connect(propose_move_in)
        propose_move_in()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, tenancy_id = self._write(
                lambda: self.ledger.add_tenancy(**dialog.result_data),
                "Tenant household added.",
            )
            if ok:
                self._write(
                    lambda: self.ledger.prepare_monthly_drafts(),
                    "Monthly rent and electricity drafts prepared for review.",
                )
                self.select_account(tenancy_id)
                if dialog.existing.isChecked():
                    self.opening_balances(default_date=dialog.account_start.iso())

    def edit_household(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        dialog = self._account_dialog(HouseholdDialog(tenancy, self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            self._write(
                lambda: self.ledger.update_tenancy(
                    self._selected_tenancy,
                    values["tenant_name"],
                    phone=values["phone"],
                    members=values["members"],
                    grace_days=values["grace_days"],
                    initial_meter_reading_hundredths=values["initial_meter_reading_hundredths"],
                ),
                "Household details updated.",
            )

    def prepare_drafts(self):
        self._write(
            lambda: self.ledger.prepare_monthly_drafts(),
            "Missing rent and electricity drafts prepared. Review each amount before posting.",
        )

    def review_drafts(self):
        self.prepare_drafts()
        for tenancy in self._tenancies:
            charges = self.ledger.list_charges(tenancy["id"])
            if any(c["status"] == "draft" and not c.get("electricity_deferred") for c in charges):
                self.select_account(tenancy["id"])
                self.account_tabs.setCurrentIndex(0)
                for charge in self._charges:
                    if charge["status"] == "draft" and not charge.get("electricity_deferred"):
                        select_bill(self.charges_table, charge["id"])
                        break
                return
        self.navigate(2)
        self.statusBar().showMessage("There are no monthly bills awaiting review.", 10000)

    def post_selected_draft(self, *, defer=False):
        charge_id = selected_id(self.charges_table)
        charge = next((c for c in self._charges if c["id"] == charge_id), None)
        if not charge or charge["status"] != "draft" or charge.get("electricity_deferred"):
            self.statusBar().showMessage(
                "Select a monthly draft in the bills table first.", 7000
            )
            return
        charge = self.ledger.get_bill(charge_id)
        dialog = self._account_dialog(PostDraftDialog(charge, self))
        if defer and hasattr(dialog, "defer_electricity"):
            dialog.defer_electricity.setChecked(True)
        balance = self.ledger.balance(self._selected_tenancy)
        dialog.add_hint(
            f"Available balance: {self.money(balance['credit_paise'])}. After posting, open the bill to mark it Paid."
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.result_data.get("defer_electricity"):
                self._write(lambda: self.ledger.set_electricity_deferred(charge_id, True),
                            "Electricity deferred. This cycle is carried forward for a later combined bill.")
            else:
                self._write(
                    lambda: self.ledger.post_charge(charge_id, **dialog.result_data),
                    "Bill posted. Open it any time to review payments.",
                )

    def _selected_cycle(self):
        selection = selected_id(self.charges_table)
        cycle_start = selection.removeprefix("cycle:") if isinstance(selection, str) else next(
            (bill["billing_cycle_start"] for bill in self._charges if bill["id"] == selection), None
        )
        return next((cycle for cycle in self._bill_cycles if cycle["cycle_start"] == cycle_start), None)

    def _bill_selection_changed(self):
        cycle = self._selected_cycle()
        self.cycle_lock_button.setText("Unlock cycle" if cycle and cycle["locked"] else "Lock cycle")
        self.cycle_lock_button.setEnabled(bool(cycle) and not self._account_is_archived)
        self.cycle_lock_button.setToolTip("Select a cycle heading or one of its bills. Locked cycles block Paid/Unpaid changes; payments can still be added to the balance.")
        charge_id = selected_id(self.charges_table)
        charge = next((bill for bill in self._charges if bill["id"] == charge_id), None)
        editable = bool(charge) and not self._account_is_archived and not charge.get("payment_locked") and not charge.get("deleted_on")
        self.undo_bill_action.setEnabled(editable and bool(charge.get("voided_on")))
        self.delete_bill_action.setEnabled(editable and charge["kind"] == "misc")
        self.cancel_bill_action.setEnabled(editable and charge["kind"] == "misc" and not charge.get("voided_on"))
        self.correct_bill_action.setEnabled(editable and charge["status"] in {"draft", "posted"}
                                           and not charge.get("voided_on")
                                           and not charge.get("electricity_deferred")
                                           and (charge["kind"] != "misc" or not charge.get("paid_paise")))
        electricity = bool(charge) and charge["kind"] == "electricity"
        self.meter_bill_action.setEnabled(electricity and not self._account_is_archived
                                         and charge["status"] in {"draft", "posted"}
                                         and not charge.get("voided_on") and not charge.get("deleted_on")
                                         and not charge.get("electricity_deferred"))
        self.defer_bill_action.setText("Undo electricity deferral" if electricity and charge.get("electricity_deferred")
                                      else "Skip electricity this cycle…")
        self.defer_bill_action.setEnabled(editable and electricity and charge["status"] == "draft"
                                         and not charge.get("opening") and bool(charge.get("electricity_cycle_start"))
                                         and not charge.get("voided_on") and not charge.get("electricity_covered_by_charge_id")
                                         and not charge.get("electricity_scheduled_covering_charge_id"))

    @_account_action
    def edit_selected_meter_reading(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            return
        charge = self.ledger.get_bill(charge_id)
        if (charge["kind"] != "electricity" or charge["status"] not in {"draft", "posted"}
                or charge.get("voided_on") or charge.get("electricity_deferred")):
            return
        dialog = self._account_dialog(MeterReadingDialog(charge, self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(lambda: self.ledger.set_meter_reading(charge_id, **dialog.result_data),
                        "Electric meter reading updated.")

    @_account_action
    def toggle_electricity_deferral(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            return
        charge = self.ledger.get_bill(charge_id)
        if (charge["kind"] != "electricity" or charge["status"] != "draft"
                or charge.get("opening") or not charge.get("electricity_cycle_start")
                or charge.get("voided_on") or charge.get("electricity_covered_by_charge_id")
                or charge.get("electricity_scheduled_covering_charge_id")):
            return
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before changing electricity deferral.")
        if charge.get("electricity_deferred"):
            self._write(lambda: self.ledger.set_electricity_deferred(charge_id, False),
                        "Electricity deferral undone. Review this cycle's electricity bill when ready.")
        else:
            self.post_selected_draft(defer=True)

    def _open_covering_bill(self, charge_id):
        select_bill(self.charges_table, charge_id)
        self.open_selected_bill()

    @_account_action
    def toggle_cycle_lock(self):
        cycle = self._selected_cycle()
        if not cycle or self._selected_tenancy is None:
            return
        locked = not cycle["locked"]
        self._write(lambda: self.ledger.set_cycle_payment_lock(
            self._selected_tenancy, cycle["cycle_start"], locked),
            "Cycle locked. Payment status cannot change until unlocked." if locked
            else "Cycle unlocked. Bill payment status can now be changed.")

    def open_selected_bill(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            self.statusBar().showMessage("Select a bill or monthly draft first.", 7000)
            return
        try:
            charge = self.ledger.get_bill(charge_id)
            if charge["status"] == "draft" and not charge.get("electricity_deferred"):
                self.post_selected_draft()
                return
            tenancy = self.ledger.get_tenancy(charge["tenancy_id"])
            balance = self.ledger.balance(charge["tenancy_id"])
            dialog = self._account_dialog(BillDialog(
                charge, self.mark_selected_paid, self.mark_selected_unpaid, self,
                archived=bool(tenancy.get("archived_on")),
                balance_paise=balance["credit_paise"],
                undo_cancel=self.undo_cancelled_bill, delete_bill=self.delete_selected_bill,
                correct_amount=self.correct_selected_bill_amount,
                edit_meter=self.edit_selected_meter_reading,
                undo_deferral=self.toggle_electricity_deferral,
                open_covering_bill=lambda: self._open_covering_bill(
                    charge.get("electricity_covered_by_charge_id") or charge.get("electricity_scheduled_covering_charge_id")),
            ))
            dialog.exec()
        except Exception as exc:
            self._error(exc)

    @_account_action
    def correct_selected_bill_amount(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            return
        charge = self.ledger.get_bill(charge_id)
        if charge.get("electricity_deferred"):
            return
        if charge["status"] == "draft":
            self.post_selected_draft()
            return
        if charge["status"] != "posted" or charge.get("voided_on") or charge.get("deleted_on"):
            self.statusBar().showMessage("Select an unpaid bill or a draft to correct its amount.", 7000)
            return
        if charge["kind"] == "misc" and charge.get("paid_paise"):
            raise ValueError("Mark this miscellaneous bill Unpaid before correcting its amount.")
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before correcting a bill.")
        dialog = self._account_dialog(BillAmountDialog(charge, self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, revised = self._write(
                lambda: self.ledger.revise_bill_amount(charge_id, **dialog.result_data),
                "Bill amount corrected. Any applied payment is back in the balance; review and mark the revised bill Paid.",
            )
            if ok:
                select_bill(self.charges_table, revised)
                self._bill_selection_changed()

    @_account_action
    def mark_selected_paid(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            return
        charge = self.ledger.get_bill(charge_id)
        balance = self.ledger.balance(charge["tenancy_id"])
        amount = charge["outstanding_paise"]
        if charge["status"] != "posted" or amount <= 0:
            return
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before changing payment status.")
        if balance["credit_paise"] < amount:
            raise ValueError(f"Add {self.money(amount - balance['credit_paise'])} to the balance first. Bills must be paid in full.")
        if QMessageBox.question(
            self, "Mark bill Paid",
            f"Deduct {self.money(amount)} from the balance to pay this {charge['kind']} bill in full?\nBalance afterwards: {self.money(balance['credit_paise'] - amount)}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.mark_bill_paid(charge_id),
                        "Bill marked Paid. Its full amount was deducted from the balance.")

    @_account_action
    def mark_selected_unpaid(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            return
        charge = self.ledger.get_bill(charge_id)
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before changing payment status.")
        if charge["status"] != "posted" or charge["paid_paise"] <= 0:
            return
        dialog = self._account_dialog(UnpaidDialog(charge, self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(lambda: self.ledger.mark_bill_unpaid(charge_id, **dialog.result_data),
                        "Bill marked Unpaid. Its applied amount was returned to the balance.")

    def add_charges(self):
        if self._selected_tenancy is None:
            return
        rent = [
            c for c in self._charges if c["kind"] == "rent" and c["status"] != "void"
        ]
        latest = max(rent, key=lambda c: c["period_start"]) if rent else None
        dialog = self._account_dialog(
            ChargeDialog(
                self,
                start=latest["period_start"] if latest else None,
                end=latest["period_end"] if latest else None,
            )
        )
        balance = self.ledger.balance(self._selected_tenancy)
        dialog.add_hint(f"Available balance: {self.money(balance['credit_paise'])}.")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            items = dialog.result_data
            self._write(
                lambda: self.ledger.add_charges(
                    self._selected_tenancy,
                    items,
                    apply_credit=False,
                ),
                f"{len(items)} charge(s) posted.",
            )

    @_account_action
    def receive_payment(self):
        if self._selected_tenancy is None:
            return
        dialog = self._account_dialog(BillPaymentDialog(self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.result_data
            duplicate = any(
                p["amount_paise"] == data["amount_paise"]
                and p["paid_on"] == data["paid_on"]
                and p["method"] == data["method"]
                and not p.get("reversed_on")
                for p in self._payments
            )
            if (
                duplicate
                and QMessageBox.question(
                    self,
                    "Possible duplicate payment",
                    "A payment with this date, amount and method already exists. Record this as another payment?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self._write(
                lambda: self.ledger.deposit_payment(self._selected_tenancy, **data),
                "Payment added to the available balance. Open a bill to mark it Paid.",
            )

    def opening_balances(self, checked=False, default_date=None):
        if self._selected_tenancy is None:
            return
        dialog = self._account_dialog(OpeningDialog(self))
        if default_date:
            dialog.when.set_iso(default_date)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(
                lambda: self.ledger.set_opening_balances(
                    self._selected_tenancy, **dialog.result_data
                ),
                "Opening balances recorded.",
            )

    def change_rent(self):
        if self._selected_tenancy is None:
            return
        from ..dates import cycle_bounds

        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        dialog = self._account_dialog(RateDialog(self))
        dialog.amount.set_paise(tenancy["rent_paise"])
        earliest = max(
            today_ist(),
            date.fromisoformat(tenancy["move_in"]),
            date.fromisoformat(tenancy.get("opening_date") or tenancy["move_in"]),
        )
        dialog.effective.set_iso(
            cycle_bounds(earliest, tenancy["billing_day"])[1].isoformat()
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            self._write(
                lambda: self.ledger.change_rent(
                    self._selected_tenancy,
                    effective_from=values["effective_date"],
                    rent_paise=values["rent_paise"],
                ),
                "Future rent rate updated. Posted history is unchanged.",
            )

    def cancel_future_rate(self):
        rate_id = selected_id(self.rates_table)
        rate = next((row for row in self._rates if row["id"] == rate_id), None)
        if not rate or rate["effective_from"] <= today_ist().isoformat():
            QMessageBox.information(
                self,
                "Choose a scheduled change",
                "Only a future rent change can be cancelled. Earlier agreed rates remain in the history.",
            )
            return
        if (
            QMessageBox.question(
                self,
                "Cancel scheduled rent change?",
                f"Cancel the planned rate of {self.money(rate['amount_paise'])} from {date_text(rate['effective_from'])}? The previous agreed rate will continue until another scheduled change.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._write(
            lambda: self.ledger.cancel_rent_change(rate_id),
            "Scheduled rent change cancelled.",
        )

    def refund_advance(self):
        if self._selected_tenancy is None:
            return
        balance = self.ledger.balance(self._selected_tenancy)
        dialog = self._account_dialog(
            AmountActionDialog(
                "Refund advance credit",
                self,
                f"Available advance credit: {self.money(balance['credit_paise'])}. This records money actually returned to the tenant.",
                reason_required=True,
            )
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            self._write(
                lambda: self.ledger.refund_credit(
                    self._selected_tenancy,
                    values["amount_paise"],
                    values["date"],
                    values["method"],
                    values["reason"],
                ),
                "Advance refund recorded.",
            )

    def deposit_action(self, action: str):
        try:
            self._deposit_action(action)
        except Exception as exc:
            self._error(exc)

    def _deposit_action(self, action: str):
        if self._selected_tenancy is None:
            return
        balance = self.ledger.balance(self._selected_tenancy)
        titles = {
            "receive": "Receive security deposit",
            "return": "Return security deposit",
            "apply": "Transfer deposit to balance",
            "from_balance": "Transfer balance to security deposit",
        }
        description = f"Deposit currently held: {self.money(balance['deposit_paise'])}. "
        description += (
            "This transfers deposit money into the available balance. Open individual bills to mark them Paid afterwards."
            if action == "apply"
            else "Deposit money is kept separate from rent and advance credit."
        )
        if action == "from_balance":
            description = (
                f"Available balance: {self.money(balance['credit_paise'])}. Deposit held: {self.money(balance['deposit_paise'])}. "
                "Move existing available money into the security deposit. This reduces the available balance "
                "by the same amount and does not record a new receipt."
            )
        dialog = self._account_dialog(
            AmountActionDialog(
                titles[action],
                self,
                description,
                reason_required=action in {"apply", "return", "from_balance"},
            )
        )
        if action in {"apply", "from_balance"}:
            dialog.method.hide()
            method_label = dialog.form.labelForField(dialog.method)
            if method_label is not None:
                method_label.hide()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            if action == "from_balance":
                self._write(
                    lambda: self.ledger.transfer_balance_to_deposit(
                        self._selected_tenancy, values["amount_paise"], values["date"], values["reason"],
                    ),
                    "Available balance transferred to the security deposit. No new receipt was recorded.",
                )
                return
            self._write(
                lambda: self.ledger.record_deposit(
                    self._selected_tenancy,
                    values["amount_paise"],
                    values["date"],
                    action,
                    values["method"],
                    notes=values["reason"],
                ),
                "Deposit movement recorded separately from rent.",
            )

    @_account_action
    def end_tenancy(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        if tenancy.get("archived_on"):
            return
        dialog = self._account_dialog(MoveOutDialog(self, tenancy))
        previous_final = next((charge for charge in reversed(self._charges)
                               if charge["kind"] == "rent" and charge["status"] != "void"
                               and charge["period_end"] == tenancy.get("move_out") and charge["amount_paise"] is not None), None)
        if previous_final:
            dialog.rent.set_paise(previous_final["amount_paise"])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            preview = self.ledger.preview_move_out(tenancy["id"], values["move_out"], values["final_rent_paise"])
            if preview["locked_cycle_starts"]:
                locked = ", ".join(date_text(day) for day in preview["locked_cycle_starts"])
                raise ValueError(f"Unlock the affected cycles before changing move-out: {locked}.")
            effects = (
                f"Moved out cycle: {date_text(preview['moved_out_cycle_start'])} – {date_text(preview['moved_out_cycle_end'])}.\n"
                f"Remove {len(preview['stopped_bill_ids'])} later bills across {len(preview['stopped_cycle_starts'])} cycles.\n"
                f"Correct {len(preview['corrected_bill_ids'])} posted bills for the revised stay.\n"
                f"Return {self.money(preview['returned_paise'])} of applied payments to available balance.\n\n"
                "Review revised rent and electricity drafts. Earlier dues and receipts stay on this account; "
                "payments can still be received after departure."
            )
            if (
                QMessageBox.question(
                    self,
                    "Confirm change of move-out" if tenancy.get("move_out") else "Confirm move-out",
                    f"Set {tenancy['tenant_name']}'s last occupied day to {date_text(values['move_out'])} with final-period rent of {self.money(values['final_rent_paise'])}?\n\n{effects}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self._write(
                lambda: self.ledger.move_out(
                    self._selected_tenancy,
                    values["move_out"],
                    values["final_rent_paise"],
                    reason=values["reason"] or "Final rent agreed at move-out",
                ),
                "Move-out updated. Review revised monthly drafts and settle the remaining bills and deposit.",
            )

    @_account_action
    def transfer_tenant(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        if tenancy.get("archived_on"):
            return
        dialog = TransferDialog(tenancy, self._flats, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            target = next(flat for flat in self._flats if flat["id"] == values["flat_id"])
            rent = f"\nNew monthly rent: {self.money(values['rent_paise'])}. {dialog.rent_hint.text()}" if values["rent_paise"] is not None else "\nMonthly rent remains unchanged."
            if QMessageBox.question(self, "Confirm flat transfer",
                                    f"Move {tenancy['tenant_name']} to {flat_label(target)} from {date_text(values['effective_on'])}?{rent}\n\nBills and payments stay on this account.",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self._write(lambda: self.ledger.transfer_tenancy(tenancy["id"], **values),
                        "Flat transfer recorded. The account and dated flat history are preserved.")

    @_account_action
    def exchange_tenants(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        if tenancy.get("archived_on"):
            return
        others = [other for other in self._tenancies if other["id"] != tenancy["id"] and not other.get("archived_on")]
        if not others:
            QMessageBox.information(self, "Choose another tenant", "An exchange needs a second open tenant account in another flat.")
            return
        dialog = ExchangeDialog(tenancy, others, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            other = next(other for other in others if other["id"] == values["second_id"])
            if QMessageBox.question(self, "Confirm flat exchange",
                                    f"Exchange the flats of {tenancy['tenant_name']} and {other['tenant_name']} on {date_text(values['effective_on'])}?\n\n{dialog.rent_hint.text()}\nEach tenant keeps their account, bills and payments.",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self._write(lambda: self.ledger.exchange_tenancies(tenancy["id"], **values),
                        "Both flat moves recorded together. Each tenant retains their own account.")

    @_account_action
    def cancel_planned_move(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        history = tenancy.get("occupancy_history", [])
        if tenancy.get("archived_on") or len(history) < 2 or history[-1]["starts_on"] <= today_ist().isoformat():
            return
        other_id = history[-1].get("paired_tenancy_id")
        other = self.ledger.get_tenancy(other_id) if other_id else None
        dialog = CancelMoveDialog(tenancy, other, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            detail = f"Cancel both {tenancy['tenant_name']}'s and {other['tenant_name']}'s planned moves?" if other else f"Cancel {tenancy['tenant_name']}'s planned move?"
            if QMessageBox.question(self, "Confirm cancellation", detail + "\nNew rent scheduled with the move will also be cancelled.",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self._write(lambda: self.ledger.cancel_scheduled_transfer(tenancy["id"], **dialog.result_data),
                            "Planned flat move cancelled. The previous occupancy continues.")

    @_account_action
    def cancel_planned_move_out(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        if tenancy.get("archived_on") or not tenancy.get("move_out") or tenancy["move_out"] <= today_ist().isoformat():
            return
        preview = self.ledger.preview_cancel_move_out(tenancy["id"])
        if preview["locked_cycle_starts"]:
            locked = ", ".join(date_text(day) for day in preview["locked_cycle_starts"])
            raise ValueError(f"Unlock the affected cycles before cancelling move-out: {locked}.")
        dialog = CancelMoveOutDialog(tenancy, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if QMessageBox.question(
            self, "Cancel planned move-out",
            f"Cancel {tenancy['tenant_name']}'s move-out on {date_text(tenancy['move_out'])}?\n\n"
            f"Monthly billing will continue. {len(preview['corrected_bill_ids'])} posted bills will be corrected "
            f"and {self.money(preview['returned_paise'])} returned to available balance. Review restored drafts before paying them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.cancel_scheduled_move_out(tenancy["id"], **dialog.result_data),
                        "Planned move-out cancelled. Occupancy and monthly billing continue; review restored drafts.")

    @_account_action
    def cancel_upcoming_tenant(self):
        if self._selected_tenancy is None:
            return
        tenancy_id = self._selected_tenancy
        preview = self.ledger.preview_cancel_upcoming_tenancy(tenancy_id)
        if preview["locked_cycle_starts"]:
            locked = ", ".join(date_text(day) for day in preview["locked_cycle_starts"])
            raise ValueError(f"Unlock the affected cycles before cancelling this tenant: {locked}.")
        detail = (
            f"Cancel {preview['tenant_name']}'s planned move-in on {date_text(preview['planned_move_in'])}?\n\n"
            f"Release {len(preview['released_flat_ids'])} flat reservation(s) and cancel "
            f"{len(preview['cancelled_bill_ids'])} rent/electricity bill(s). "
            f"{len(preview['remaining_bill_ids'])} other bill(s) remain for review.\n\n"
            f"Balance retained, including future entries: {self.money(preview['future_credit_paise'])}. "
            f"Deposit retained: {self.money(preview['future_deposit_paise'])}.\n"
            "Receipts, deposits and cancellation history will remain available. "
            "Record any actual refund separately."
        )
        dialog = self._account_dialog(CancelUpcomingTenantDialog(detail, self))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if QMessageBox.question(self, "Confirm tenant cancellation", detail,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.cancel_upcoming_tenancy(tenancy_id, **dialog.result_data, expected_revision=preview["revision"]),
                        "Upcoming tenancy cancelled. The flat is available; retained money and fees can be settled from this account.")

    @_account_action
    def archive_account(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        balance = self.ledger.balance(tenancy["id"])
        pending_review = any(charge["status"] == "draft" and not (
            charge.get("electricity_deferred") and (charge.get("electricity_covered_by_charge_id")
                                                    or charge.get("electricity_scheduled_covering_charge_id"))
        ) for charge in self._charges)
        if any(balance[key] for key in ("outstanding_paise", "credit_paise", "deposit_paise")) or pending_review:
            QMessageBox.information(self, "Settle account before closing",
                                    "Review pending monthly bills and deferred electricity, settle unpaid bills, and resolve advance credit and the security deposit before closing this account.")
            return
        closure = (f"cancellation on {date_text(tenancy['cancelled_on'])}" if tenancy.get("cancelled_on")
                   else f"departure on {date_text(tenancy.get('move_out'))}")
        if QMessageBox.question(self, "Close and archive account",
                                f"Close {tenancy['tenant_name']}'s account after {closure}?\n\nThe full history and statements remain available under Show archived. Reopen the account before making further changes.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.archive_tenancy(tenancy["id"]),
                        "Account closed and archived. Use Show archived to view or reopen it.")

    @_account_action
    def reopen_account(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        if not tenancy.get("archived_on"):
            return
        if QMessageBox.question(self, "Reopen account",
                                f"Reopen {tenancy['tenant_name']}'s account for settlement or corrections? Recorded departure or cancellation stays in place; this does not reserve a flat again.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._write(lambda: self.ledger.unarchive_tenancy(tenancy["id"]), "Archived account reopened.")

    @_account_action
    def delete_archived_account(self):
        if self._selected_tenancy is None:
            return
        if self._pdf_jobs:
            raise ValueError("Wait for the current PDF export to finish before deleting an account.")
        ledger = self.ledger
        preview = ledger.prepare_tenancy_deletion(self._selected_tenancy)
        dialog = DeleteArchivedTenantDialog(preview, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.result_data
        if not values["backup"]:
            self._confirm_tenant_deletion(ledger, preview, [])
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder for the CSV and PDF records")
        if not folder:
            return
        destination = Path(folder) / f"GrihaLekha-tenant-{preview['tenancy_id']}-{uuid4().hex[:8]}"
        csv_backup = ledger.export_tenancy_deletion_csv(preview, destination.with_suffix(".zip"))
        from ..reports import PdfExportJob
        from ..tenancy_deletion import tenancy_deletion_html

        document_id = destination.name
        job = PdfExportJob(tenancy_deletion_html(preview, values["language"]), destination.with_suffix(".pdf"), self)
        self._pdf_jobs[document_id] = job
        self.statusBar().showMessage("CSV records saved. Preparing the PDF before deletion review…")

        def complete(saved_path):
            try:
                pdf_backup = ledger.tenancy_deletion_backup_file(preview, saved_path, kind="pdf")
                self._confirm_tenant_deletion(ledger, preview, [csv_backup, pdf_backup])
            except Exception as exc:
                self._error(exc)
            finally:
                self._pdf_jobs.pop(document_id, None)

        def failed(message):
            self._pdf_jobs.pop(document_id, None)
            self._error(ValueError(f"PDF backup failed. No tenant data was deleted.\n{message}\nCSV records: {csv_backup['path']}"))

        job.finished.connect(complete)
        job.failed.connect(failed)
        try:
            job.start()
        except Exception:
            self._pdf_jobs.pop(document_id, None)
            raise

    def _confirm_tenant_deletion(self, ledger, preview, backups):
        if ledger is not self.ledger:
            raise ValueError("The workspace changed. Review the archived account again before deletion.")
        tenant = preview["tenancy"]
        files = "Saved both records:\n" + "\n".join(item["path"] for item in backups) if backups else "No CSV or PDF backup was requested."
        if QMessageBox.question(
            self, "Permanently delete archived tenant",
            f"Permanently delete {tenant['tenant_name']}'s archived account and payment proofs?\n\n{files}\n\n"
            "This cannot be undone within the account. Existing application backups and issued PDFs are retained.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        ok, result = self._write(
            lambda: ledger.delete_archived_tenancy(preview["tenancy_id"], expected_revision=preview["revision"], backups=backups),
            "Archived tenant data deleted.",
        )
        if ok and result["cleanup_warnings"]:
            QMessageBox.warning(self, "Account deleted; some files remain", "\n".join(result["cleanup_warnings"]))

    @_account_action
    def undo_cancelled_bill(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            self.statusBar().showMessage("Select a cancelled bill first.", 7000)
            return
        charge = self.ledger.get_bill(charge_id)
        if not charge.get("voided_on") or charge.get("deleted_on"):
            self.statusBar().showMessage("Select a cancelled bill first.", 7000)
            return
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before undoing a cancellation.")
        if charge["kind"] == "misc" and not any(
            bill.get("replaces_charge_id") == charge_id for bill in self._charges
        ):
            # Older replacements have no parent link. Let the owner distinguish
            # them from independent fees instead of guessing from their text.
            unlinked = [bill for bill in self._charges
                        if bill["id"] != charge_id and bill["kind"] == "misc"
                        and bill["status"] != "void" and not bill.get("replaces_charge_id")
                        and bill["charge_date"] >= charge["voided_on"]]
            if unlinked:
                details = "\n".join(
                    f"{self.money(bill['amount_paise'])} · {date_text(bill['period_start'])} · {bill.get('description', '')}"
                    for bill in unlinked[:5]
                )
                if len(unlinked) > 5:
                    details += f"\n…and {len(unlinked) - 5} more."
                if QMessageBox.question(
                    self, "Review existing miscellaneous bills",
                    "These bills were added on or after the cancellation and have no saved replacement link. "
                    "If one replaces the cancelled bill, keep it and leave the original cancelled.\n\n"
                    f"{details}\n\nAre these separate fees, and should the original bill also be restored?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                ) != QMessageBox.StandardButton.Yes:
                    return
        dialog = self._account_dialog(BillCorrectionDialog(charge, "restore", self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ok, restored = self._write(
                lambda: self.ledger.restore_cancelled_bill(charge_id, **dialog.result_data),
                "Cancellation undone. The restored bill is selected; its earlier history is retained.",
            )
            if ok:
                select_bill(self.charges_table, restored)
                self._bill_selection_changed()
                restored_bill = next((bill for bill in self._charges if bill["id"] == restored), None)
                if restored_bill and restored_bill["charge_date"] > today_ist().isoformat():
                    self.statusBar().showMessage(
                        f"Bill restored for {date_text(restored_bill['charge_date'])}; its future schedule is preserved.", 10000
                    )

    @_account_action
    def delete_selected_bill(self):
        charge_id = selected_id(self.charges_table)
        if not isinstance(charge_id, int):
            self.statusBar().showMessage("Select a miscellaneous bill first.", 7000)
            return
        charge = self.ledger.get_bill(charge_id)
        if charge["kind"] != "misc":
            raise ValueError(f"Rent and electricity bills cannot be deleted or cancelled. Review or correct their amount, including {self.currency_symbol}0.")
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before deleting a bill.")
        dialog = self._account_dialog(BillCorrectionDialog(charge, "delete", self))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._write(
                lambda: self.ledger.delete_bill(charge_id, **dialog.result_data),
                "Miscellaneous bill deleted. Any applied payment was returned to the balance; internal history is retained.",
            )

    @_account_action
    def cancel_charge(self):
        charge_id = selected_id(self.charges_table)
        charge = next((c for c in self._charges if c["id"] == charge_id), None)
        if not charge or charge.get("voided_on"):
            self.statusBar().showMessage(
                "Select an active charge or draft first.", 7000
            )
            return
        if charge["kind"] in {"rent", "electricity"}:
            raise ValueError(f"Rent and electricity bills cannot be cancelled. Review or correct their amount, including {self.currency_symbol}0.")
        if charge.get("payment_locked"):
            raise ValueError("Unlock this cycle before cancelling a bill.")
        dialog = self._account_dialog(
            ReasonDialog(
                "Cancel selected charge",
                f"Cancel {charge['kind']} for {date_text(charge['period_start'])} – {date_text(charge['period_end'])}, amount {self.money(charge['amount_paise'])}? Applied payments return to the balance.",
                self,
            )
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            self._write(
                lambda: self.ledger.void_charge(
                    charge_id, values["reason"], effective_on=values["date"], prepare_replacement=True
                ),
                "Charge cancelled with a recorded reason.",
            )

    def replace_cancelled_charge(self):
        charge_id = selected_id(self.charges_table)
        charge = next((c for c in self._charges if c["id"] == charge_id), None)
        if not charge or charge["status"] != "void":
            QMessageBox.information(
                self,
                "Select a cancelled bill",
                "Choose a cancelled charge in the table. The replacement will keep the original cancellation in the account history.",
            )
            return
        replacement = next((bill for bill in self._charges
                            if bill["status"] != "void" and (
                                bill.get("replaces_charge_id") == charge_id or (
                                    charge["kind"] in {"rent", "electricity"}
                                    and not charge.get("opening") and not bill.get("opening")
                                    and bill["kind"] == charge["kind"]
                                    and bill.get("billing_cycle_start") == charge.get("billing_cycle_start")
                                )
                            )), None)
        if replacement:
            select_bill(self.charges_table, replacement["id"])
            self.open_selected_bill()
            return
        dialog = self._account_dialog(
            ChargeDialog(self, charge["period_start"], charge["period_end"], replacement_kind=charge["kind"])
        )
        dialog.setWindowTitle("Replace cancelled bill")
        dialog.heading.setText("Replace cancelled bill")
        dialog.due.set_iso(charge["due_date"])
        dialog.charged.set_iso(
            max(
                today_ist().isoformat(),
                charge.get("voided_on") or charge["charge_date"],
            )
        )
        kind = dialog.items.cellWidget(0, 0)
        kind.setCurrentIndex(kind.findData(charge["kind"]))
        if charge["amount_paise"] is None:
            dialog.items.cellWidget(0, 1).clear()
        else:
            dialog.items.cellWidget(0, 1).set_paise(charge["amount_paise"])
        dialog.items.cellWidget(0, 2).setText(
            charge.get("description") or "Corrected bill"
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.result_data[0]["opening"] = bool(charge.get("opening"))
            ok, replacements = self._write(
                lambda: self.ledger.replace_cancelled_bill(charge_id, dialog.result_data),
                "Replacement bill posted; the original cancellation remains in history.",
            )
            if ok and replacements:
                select_bill(self.charges_table, replacements[0])

    def reverse_payment(self):
        payment_id = selected_id(self.payments_table)
        payment = next((p for p in self._payments if p["id"] == payment_id), None)
        if not payment or payment.get("reversed_on"):
            self.statusBar().showMessage("Select a recorded payment first.", 7000)
            return
        dialog = self._account_dialog(
            ReasonDialog(
                "Reverse selected payment",
                f"Reverse the {self.money(payment['amount_paise'])} entry dated {date_text(payment['paid_on'])}? This corrects an entry and reopens bills. To return real money, use Refund advance credit instead.",
                self,
            )
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            self._write(
                lambda: self.ledger.reverse_payment(
                    payment_id, values["reason"], effective_on=values["date"]
                ),
                "Payment reversed with its original history preserved.",
            )

    def reverse_movement(self):
        row = self.movements_table.currentRow()
        if row < 0 or row >= len(self._movements):
            QMessageBox.information(
                self,
                "Select a movement",
                "Select a refund or deposit movement to correct.",
            )
            return
        selected = self._movements[row]
        item, source = selected["record"], selected["source"]
        if source not in {"refunds", "deposits"} or item.get("reversed_on"):
            QMessageBox.information(
                self,
                "Choose an original movement",
                "Select an unreversed refund or deposit entry. Correction history remains unchanged.",
            )
            return
        description = (
            "This reverses an incorrect refund entry and restores the advance it used. It does not record a new cash receipt."
            if source == "refunds"
            else "This reverses the selected deposit entry with a recorded reason. To return real deposit money, use Return security deposit instead."
        )
        dialog = self._account_dialog(
            ReasonDialog(
                "Correct refund or deposit",
                f"{selected['values'][1]} · {self.money(item['amount_paise'])} · {date_text(selected['when'])}\n\n{description}",
                self,
            )
        )
        dialog.when.set_iso(max(today_ist().isoformat(), selected["when"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.result_data
            operation = (
                self.ledger.reverse_refund
                if source == "refunds"
                else self.ledger.reverse_deposit
            )
            self._write(
                lambda: operation(
                    item["id"], values["reason"], effective_on=values["date"]
                ),
                "Movement corrected with the original history preserved.",
            )

    def open_proof(self):
        payment_id = selected_id(self.payments_table)
        payment = next((p for p in self._payments if p["id"] == payment_id), None)
        if not payment or not payment.get("proof_path"):
            QMessageBox.information(
                self, "No proof attached", "Select a payment with an attached proof."
            )
            return
        try:
            root = Path(self.ledger.data_dir).resolve()
            path = (root / payment["proof_path"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(
                    "The proof file is missing or outside application storage."
                )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            self._error(exc)

    def export_statement(self):
        if self._selected_tenancy is None:
            return
        tenancy = self.ledger.get_tenancy(self._selected_tenancy)
        dialog = self._account_dialog(
            ExportDialog(self, tenancy.get("opening_date") or tenancy["move_in"])
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.result_data
        choices = ["PDF statement", "CSV tables"]
        choice, accepted = QInputDialog.getItem(
            self, "Export format", "Save as", choices, 0, False
        )
        if not accepted:
            return
        try:
            snapshot = self.ledger.statement(
                self._selected_tenancy, values["start"], values["end"]
            )
            if choice == "CSV tables":
                folder = QFileDialog.getExistingDirectory(
                    self, "Choose a folder for CSV tables"
                )
                if folder:
                    from ..reports import export_csv

                    # A unique subdirectory prevents accidental replacement of older exports.
                    destination = (
                        Path(folder) / f"GrihaLekha-{values['end']}-{uuid4().hex[:6]}"
                    )
                    files = export_csv(snapshot, destination)
                    QMessageBox.information(
                        self,
                        "CSV export complete",
                        f"Saved {len(files)} CSV tables in:\n{destination}",
                    )
            else:
                from ..reports import statement_html

                document_id = f"GL-{today_ist():%Y%m%d}-{uuid4().hex[:8].upper()}"
                self._save_pdf(
                    statement_html(snapshot, values["language"], document_id),
                    document_id,
                )
        except Exception as exc:
            self._error(exc)

    def export_receipt(self):
        payment_id = selected_id(self.payments_table)
        payment = next((p for p in self._payments if p["id"] == payment_id), None)
        if not payment:
            QMessageBox.information(
                self,
                "Select a payment",
                "Choose a payment in the table to generate its receipt.",
            )
            return
        if payment.get("opening") or payment.get("method") == "deposit":
            QMessageBox.information(
                self,
                "Use an account statement",
                "An opening balance or deposit transfer is not a new payment receipt. Export the account statement to show this movement.",
            )
            return
        language, accepted = QInputDialog.getItem(
            self,
            "Receipt language",
            "Language",
            ["English", "हिन्दी", "English + हिन्दी"],
            0,
            False,
        )
        if not accepted:
            return
        try:
            from ..reports import receipt_html

            tenancy = self.ledger.get_tenancy(self._selected_tenancy)
            start = min(tenancy["move_in"], payment["paid_on"])
            end = max(today_ist().isoformat(), payment["paid_on"])
            snapshot = self.ledger.statement(self._selected_tenancy, start, end)
            document_id = f"GL-R-{today_ist():%Y%m%d}-{uuid4().hex[:8].upper()}"
            code = {"English": "en", "हिन्दी": "hi", "English + हिन्दी": "bilingual"}[
                language
            ]
            self._save_pdf(
                receipt_html(snapshot, payment_id, code, document_id), document_id
            )
        except Exception as exc:
            self._error(exc)

    def _save_pdf(self, html: str, document_id: str):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", f"{document_id}.pdf", "PDF documents (*.pdf)"
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != ".pdf":
            destination = destination.with_suffix(".pdf")
        from ..reports import PdfExportJob

        job = PdfExportJob(html, destination, self)
        self._pdf_jobs[document_id] = job
        self.statusBar().showMessage(
            "Preparing PDF with embedded Hindi and English fonts…"
        )

        def complete(saved_path):
            try:
                issued = Path(self.ledger.data_dir) / "documents" / f"{document_id}.pdf"
                issued.parent.mkdir(parents=True, exist_ok=True)
                if Path(saved_path).resolve() != issued.resolve():
                    staging = issued.with_suffix(".pdf.partial")
                    try:
                        shutil.copy2(saved_path, staging)
                        os.replace(staging, issued)
                    finally:
                        staging.unlink(missing_ok=True)
                self._document_revision += 1
                self._dirty = True
                self.statusBar().showMessage(f"PDF saved: {saved_path}", 15000)
                message = f"Saved your PDF and a permanent issued copy:\n{saved_path}"
                if getattr(job, "used_native_fallback", False):
                    message += "\n\nHindi text is formatted correctly for printing. Copying or searching some Hindi words in this PDF may be inaccurate."
                QMessageBox.information(self, "PDF saved", message)
            except Exception as exc:
                self._error(exc)
            finally:
                self._pdf_jobs.pop(document_id, None)

        def failed(message):
            self._pdf_jobs.pop(document_id, None)
            QMessageBox.warning(self, "PDF export failed", message)

        job.finished.connect(complete)
        job.failed.connect(failed)
        job.start()

    def _refresh_backups(self):
        if not hasattr(self, "backups_table"):
            return
        try:
            self._backup_files = files = self.backups.list_backups()
            set_rows(
                self.backups_table,
                [
                    (
                        path.name,
                        f"{path.stat().st_size / 1024:.1f} KB",
                        str(path.parent),
                    )
                    for path in files
                ],
            )
            self.backup_status.setText(self.backups.latest_status())
        except Exception as exc:
            self.backup_status.setText(f"Backup status unavailable: {exc}")

    def _automatic_backup(self):
        if self._restoring:
            return
        if self._dirty or self.ledger.get_revision() != self._backup_revision:
            self._start_backup()

    def backup_now(self):
        self._start_backup()

    def export_backup(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export a complete backup",
            f"GrihaLekha-backup-{today_ist()}.zip",
            "Backup archives (*.zip)",
        )
        if path:
            destination = Path(path)
            if destination.suffix.lower() != ".zip":
                destination = destination.with_suffix(".zip")
            self._start_backup(destination)

    def _start_backup(self, destination: Path | None = None):
        if self._restoring:
            return
        if self._backup_thread is not None:
            self.statusBar().showMessage("A backup is already running.", 8000)
            return
        revision = self.ledger.get_revision()
        self._backup_capture = (revision, self._document_revision, destination is None)
        self.backup_now_button.setEnabled(False)
        self.backup_export_button.setEnabled(False)
        self.backup_status.setText("Creating and verifying a complete backup…")
        self._backup_thread = QThread(self)
        self._backup_worker = _BackupWorker(self.backups, destination)
        self._backup_worker.moveToThread(self._backup_thread)
        self._backup_thread.started.connect(self._backup_worker.run)
        self._backup_worker.finished.connect(self._backup_finished)
        self._backup_worker.failed.connect(self._backup_failed)
        self._backup_worker.finished.connect(self._backup_thread.quit)
        self._backup_worker.failed.connect(self._backup_thread.quit)
        self._backup_thread.finished.connect(self._backup_worker.deleteLater)
        self._backup_thread.finished.connect(self._backup_thread.deleteLater)
        self._backup_thread.finished.connect(self._clear_backup_worker)
        self._backup_thread.start()

    @Slot(str)
    def _backup_finished(self, path: str):
        revision, document_revision, managed = self._backup_capture
        if managed:
            self._backup_revision = revision
            self._dirty = (
                self.ledger.get_revision() != revision
                or self._document_revision != document_revision
            )
        self._refresh_backups()
        self.statusBar().showMessage(f"Backup verified: {path}", 12000)

    @Slot(str)
    def _backup_failed(self, message: str):
        self.backup_status.setText(
            f"Backup failed: {message}. Existing backups are preserved."
        )
        if self._close_requested:
            # A message box runs a nested Qt event loop. Do not let the worker's
            # finished signal start another close/backup while the choice is open.
            self._close_requested = False
            result = QMessageBox.question(
                self,
                "Backup could not finish",
                f"{message}\n\nYour live database is still saved. Close without a new backup?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self._skip_close_backup = result == QMessageBox.StandardButton.Yes
            self._close_requested = self._skip_close_backup
            if self._close_requested and self._backup_thread is None:
                QTimer.singleShot(0, self.close)
        else:
            QMessageBox.warning(
                self,
                "Backup failed",
                f"{message}\n\nPrevious successful backups have been kept. You can retry from Backups & data.",
            )

    def _clear_backup_worker(self):
        self._backup_thread = None
        self._backup_worker = None
        self.backup_now_button.setEnabled(True)
        self.backup_export_button.setEnabled(True)
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def restore_backup(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a complete GrihaLekha backup", str(self.backups.backup_dir), "GrihaLekha backup (*.zip)"
        )
        if path:
            self._restore_from_path(Path(path))

    def restore_selected_backup(self):
        row = self.backups_table.currentRow()
        if row < 0 or row >= len(getattr(self, "_backup_files", [])):
            QMessageBox.information(self, "Select a backup", "Choose a managed restore point in the table first, or use Import from backup to browse for a ZIP file.")
            return
        self._restore_from_path(self._backup_files[row])

    def _run_restore_task(self, operation, message):
        """Keep the UI responsive while validation and disk operations run."""
        progress = _RestoreProgress(message, self)
        thread = QThread(self)
        worker = _RestoreWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(progress.accept)
        thread.finished.connect(worker.deleteLater)
        QTimer.singleShot(0, thread.start)
        progress.exec()
        thread.wait()
        result, error = worker.result, worker.error
        thread.deleteLater()
        progress.deleteLater()
        if error is not None:
            raise error
        return result

    def _restore_from_path(self, path: Path):
        if self._restoring or self._backup_thread is not None or self._pdf_jobs:
            QMessageBox.information(
                self,
                "Finish the current export",
                "Please wait for the current backup or PDF export to finish before restoring data.",
            )
            return
        self._restoring = True
        try:
            manifest = self._run_restore_task(lambda: self.backups.validate(path), "Verifying accounts, payment proofs and documents…")
            dialog = BackupImportDialog(path, manifest, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            data_dir = self.ledger.data_dir
            ledger_type = type(self.ledger)
            self.ledger.close()
            try:
                safety_copy = self._run_restore_task(lambda: self.backups.restore(path), "Saving a safety backup and importing your accounts…")
            finally:
                self.ledger = ledger_type(data_dir)
            self.backups = BackupManager(self.ledger.data_dir, self.ledger.db_path)
            self._backup_revision = self.ledger.get_revision()
            self._document_revision = 0
            self._dirty = False
            self._selected_tenancy = None
            self.account_combo.setCurrentIndex(-1)
            self.refresh()
            self.navigate(0)
            QMessageBox.information(
                self, "Import complete",
                "Your accounts, payment proofs and issued documents have been imported and verified.\n\n"
                + ("Your previous data is saved in Backups & data → Managed restore points."
                   if safety_copy.is_file() else "Your previous files could not be verified as a backup. They have been kept for recovery.")
                + f"\n{safety_copy}",
            )
        except Exception as exc:
            self._error(exc)
        finally:
            self._restoring = False
            self._refresh_backups()

    def closeEvent(self, event: QCloseEvent):
        if self._restoring:
            event.ignore()
            return
        if self._pdf_jobs:
            QMessageBox.information(
                self,
                "PDF export in progress",
                "Please wait for the PDF export to finish before closing.",
            )
            event.ignore()
            return
        if self._backup_thread is not None:
            self._close_requested = True
            self.statusBar().showMessage("Finishing the backup before closing…")
            event.ignore()
            return
        if not self._skip_close_backup and (
            self._dirty or self.ledger.get_revision() != self._backup_revision
        ):
            self._close_requested = True
            self._start_backup()
            event.ignore()
            return
        self._backup_timer.stop()
        self._calendar_timer.stop()
        self.ledger.close()
        event.accept()
