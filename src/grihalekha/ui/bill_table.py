"""Cycle headings within the shared, manually resizable bill table."""

from datetime import date

from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor

from ..clock import today_ist
from ..currencies import DEFAULT_CURRENCY
from .widgets import date_text, money, selected_id, set_rows


def select_bill(target, charge_id):
    for row in range(target.rowCount()):
        item = target.item(row, 0)
        if item and item.data(Qt.ItemDataRole.UserRole) == charge_id:
            target.selectRow(row)
            target.scrollToItem(item)
            return


def render_bill_cycles(target, cycles, *, currency_code=DEFAULT_CURRENCY):
    today = today_ist().isoformat()
    previous = selected_id(target)
    blocker = QSignalBlocker(target)
    target.clearSpans()
    rows, ids, headings = [], [], []
    for cycle in cycles:
        heading_row = len(rows)
        rows.append(("",) * target.columnCount())
        ids.append("cycle:" + cycle["cycle_start"])
        headings.append((heading_row, cycle))
        for bill in cycle["charges"]:
            future = bill["status"] == "posted" and bill["charge_date"] > today
            deferred = bill.get("electricity_deferred", False) and bill["status"] != "void"
            scheduled = bill.get("electricity_scheduled_covering_charge_id")
            covered_by = bill.get("electricity_covered_by_charge_id") or scheduled
            state = (f"Scheduled in bill #{scheduled}" if deferred and scheduled
                     else f"Included in bill #{covered_by}" if deferred and covered_by
                     else "Deferred · bill later" if deferred
                     else "Review pending · enter amount" if bill.get("needs_amount") and bill["status"] == "draft"
                     else "Review pending" if bill["status"] == "draft"
                     else "Cancelled" if bill["status"] == "void"
                     else "Cancellation scheduled" if bill.get("voided_on")
                     else "Upcoming bill" if future
                     else "Paid" if bill["outstanding_paise"] == 0 else "Unpaid")
            rows.append((
                f"{date_text(bill['period_start'])} – {date_text(bill['period_end'])}",
                "Opening " + bill["kind"] if bill.get("opening") else bill["kind"].title(),
                state, money(bill["amount_paise"], currency_code=currency_code),
                "—" if deferred else money(bill.get("paid_paise", 0), currency_code=currency_code),
                money(bill.get("outstanding_paise", 0), currency_code=currency_code) if bill["status"] == "posted" and not future and not deferred else "—",
                date_text(bill["due_date"]), bill.get("description", ""),
            ))
            ids.append(bill["id"])
    set_rows(target, rows, ids)
    for row, cycle in headings:
        cell = target.item(row, 0)
        period = date.fromisoformat(cycle["cycle_start"]).strftime("%B %Y")
        pending = cycle["review_pending"]
        heading = (f"{period}  ·  {date_text(cycle['period_start'])} – {date_text(cycle['period_end'])}"
                   f"  ·  {'Locked' if cycle['locked'] else 'Unlocked'}")
        if cycle.get("moved_out_cycle"):
            heading += "  ·  Moved out cycle"
        if pending:
            heading += f"  ·  {pending} review pending"
        cell.setText(heading)
        cell.setToolTip(f"{heading}\nAmount paid: {money(cycle['paid_paise'], currency_code=currency_code)} · Amount pending: {money(cycle['pending_paise'], currency_code=currency_code)}")
        font = cell.font()
        font.setBold(True)
        cell.setFont(font)
        cell.setBackground(QColor("#292f3b"))
        cell.setForeground(QColor("#bcd0f5"))
        # A spanning heading must not force the first data column to its width.
        cell.setSizeHint(QSize(0, max(43, target.fontMetrics().height() + 20)))
        target.setSpan(row, 0, 1, target.columnCount())
    if previous in ids:
        target.selectRow(ids.index(previous))
    blocker.unblock()
