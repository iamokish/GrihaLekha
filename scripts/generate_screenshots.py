"""Regenerate README images from the application using fictional accounts.

Run with the development dependencies installed:
    uv run --frozen python scripts/generate_screenshots.py

The account database and PDF exist only in a temporary directory under build/.
Only the three documentation PNGs are retained.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def seed_accounts(ledger) -> tuple[int, int]:
    """Create two buildings and three households with different account states."""
    maple = ledger.add_building("Maple Residency", "24 Garden Road, Pune")
    lakeview = ledger.add_building("Lakeview Apartments", "18 Lake Road, Pune")
    flats = [
        ledger.add_flat(maple, "101", "First", 2),
        ledger.add_flat(maple, "202", "Second", 2),
        ledger.add_flat(lakeview, "301", "Third", 1),
    ]
    ledger.add_flat(maple, "102", "First", 2)
    ledger.add_flat(lakeview, "302", "Third", 1)
    households = [
        ("Aarav Mehta", 1_250_000, 80_000, 85_000, 2_500_000),
        ("Neha Rao", 1_500_000, 90_000, 92_000, 3_000_000),
        ("Kabir Shah", 1_050_000, 64_000, None, 2_100_000),
    ]
    tenants = [
        ledger.add_tenancy(
            flat, name, "2026-10-01", 1, rent,
            initial_meter_reading_hundredths=120_000,
        )
        for flat, (name, rent, _, _, _) in zip(flats, households)
    ]
    ledger.prepare_monthly_drafts("2026-11-12")
    selected_bill = None
    for index, (tenant, (_, rent, october_power, november_power, deposit)) in enumerate(zip(tenants, households)):
        bills = {(bill["period_start"], bill["kind"]): bill for bill in ledger.list_charges(tenant)}
        october_rent = bills["2026-10-01", "rent"]["id"]
        october_electricity = bills["2026-10-01", "electricity"]["id"]
        ledger.post_charge(october_rent)
        ledger.post_charge(october_electricity, october_power, meter_reading_hundredths=125_000)
        received = rent + (october_power if index < 2 else 0)
        ledger.deposit_payment(tenant, received, "2026-10-05", "bank", reference="October payment")
        ledger.mark_bill_paid(october_rent, "2026-10-05")
        if index < 2:
            ledger.mark_bill_paid(october_electricity, "2026-10-05")
            ledger.set_cycle_payment_lock(tenant, "2026-10-01", True)
        ledger.record_deposit(tenant, deposit, "2026-10-01", "receive", "bank", "Refundable security deposit")
        if november_power is None:
            continue
        november_rent = bills["2026-11-01", "rent"]["id"]
        november_electricity = bills["2026-11-01", "electricity"]["id"]
        ledger.post_charge(november_rent)
        ledger.post_charge(november_electricity, november_power, meter_reading_hundredths=130_000)
        received = rent if index == 0 else rent + november_power + 50_000
        ledger.deposit_payment(tenant, received, "2026-11-05", "upi", reference="November payment")
        ledger.mark_bill_paid(november_rent, "2026-11-05")
        if index == 0:
            selected_bill = november_electricity
        else:
            ledger.mark_bill_paid(november_electricity, "2026-11-05")
    if selected_bill is None:
        raise RuntimeError("The sample account has no electricity bill to display.")
    return tenants[0], selected_bill


def settle_layout() -> None:
    """Allow the window's queued layout and column-sizing work to finish."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()


def export_statement_image(ledger, tenant: int, directory: Path) -> int:
    import pymupdf
    from PySide6.QtCore import QEventLoop, QTimer

    from grihalekha.reports import PdfExportJob, statement_html

    snapshot = ledger.statement(tenant, "2026-11-01", "2026-11-30")
    html = statement_html(snapshot, "en", "GL-S-20261112-001")
    destination = directory / "statement.pdf"
    job = PdfExportJob(html, destination, renderer="native")
    loop = QEventLoop()
    completed = []
    errors = []

    def finished(path):
        completed.append(path)
        loop.quit()

    def failed(message):
        errors.append(message)
        loop.quit()

    job.finished.connect(finished)
    job.failed.connect(failed)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(30_000)
    job.start()
    loop.exec()
    timer.stop()
    if errors:
        raise RuntimeError(errors[0])
    if not completed:
        raise TimeoutError("Statement export did not finish within 30 seconds.")
    with pymupdf.open(destination) as document:
        document[0].get_pixmap(dpi=160, alpha=False).save(directory / "statement.png")
        return document.page_count


def generate(output: Path) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    from grihalekha import clock
    from grihalekha.ledger import Ledger
    from grihalekha.paths import asset_dir
    from grihalekha.ui.bill_table import select_bill
    from grihalekha.ui.theme import apply_theme
    from grihalekha.ui.window import MainWindow

    class ScreenshotWindow(MainWindow):
        def _error(self, error):
            # Headless generation must fail rather than wait on a message box.
            raise RuntimeError("Could not render the sample accounts.") from error

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_Use96Dpi)
    app = QApplication([])
    app.setApplicationName("GrihaLekha")
    app.setOrganizationName("GrihaLekha")
    apply_theme(app)
    for font in sorted((asset_dir() / "fonts").glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(font)) < 0:
            raise RuntimeError(f"Could not load bundled font: {font.name}")
    app.setFont(QFont("Noto Sans", 10))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_root = (ROOT / "build").resolve()
    work_root.mkdir(exist_ok=True)
    original_clock = clock.now_ist
    clock.now_ist = lambda: datetime(2026, 11, 12, 10, 30, tzinfo=clock.IST)
    try:
        with tempfile.TemporaryDirectory(prefix="screenshots-", dir=work_root) as temporary:
            directory = Path(temporary).resolve()
            directory.relative_to(work_root)
            ledger = Ledger(directory / "accounts")
            window = None
            try:
                tenant, unpaid_bill = seed_accounts(ledger)
                window = ScreenshotWindow(ledger)
                window._skip_close_backup = True
                window.resize(1440, 960)
                window.show()
                settle_layout()
                if not window.grab().save(str(directory / "overview.png")):
                    raise RuntimeError("Could not save the overview screenshot.")
                window.select_account(tenant)
                select_bill(window.charges_table, unpaid_bill)
                window.charges_table.setFocus()
                settle_layout()
                if not window.grab().save(str(directory / "tenant-account.png")):
                    raise RuntimeError("Could not save the account screenshot.")
                pages = export_statement_image(ledger, tenant, directory)
                for name in ("overview.png", "tenant-account.png", "statement.png"):
                    # Create beside the destination so published images inherit
                    # its permissions rather than the private temp directory's.
                    staged = output / f".{name}.tmp"
                    try:
                        staged.write_bytes((directory / name).read_bytes())
                        staged.replace(output / name)
                    finally:
                        staged.unlink(missing_ok=True)
            finally:
                if window is not None:
                    window.close()
                    window.deleteLater()
                    app.processEvents()
                ledger.close()
    finally:
        clock.now_ist = original_clock
        app.quit()
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "images",
                        help="Where to save the three documentation PNGs")
    args = parser.parse_args(argv)
    pages = generate(args.output_dir)
    print(f"Saved overview.png, tenant-account.png and statement.png to {args.output_dir.resolve()}")
    print(f"The statement image shows page 1 of {pages}; all accounts are fictional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
