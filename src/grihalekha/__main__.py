"""Desktop entry point, also usable as ``python -m grihalekha``."""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from . import __version__
from .paths import application_icon_path, asset_dir, default_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GrihaLekha — offline tenant accounts")
    parser.add_argument("--version", action="version", version=f"GrihaLekha {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override the user-data folder")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-pdf", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    from PySide6.QtCore import QCoreApplication, QLockFile, Qt, QTimer
    from PySide6.QtGui import QFont, QFontDatabase, QIcon
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    if sys.platform == "win32":
        import ctypes

        # Match installed shortcuts so Windows groups the app under its own icon.
        try:
            set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            set_app_id.argtypes = [ctypes.c_wchar_p]
            set_app_id.restype = ctypes.c_long
            set_app_id("GrihaLekha.Desktop")
        except OSError:
            pass  # Shell branding must not prevent access to account data.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("GrihaLekha")
    app.setOrganizationName("GrihaLekha")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("grihalekha")
    app.setWindowIcon(QIcon(str(application_icon_path())))
    from .ui.theme import apply_theme

    apply_theme(app)
    data_dir = (args.data_dir or default_data_dir()).resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        QMessageBox.critical(None, "Cannot open data folder", str(error))
        return 1
    lock = QLockFile(str(data_dir / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "GrihaLekha is already open", "This account folder is already open in another GrihaLekha window.")
        return 2

    log_dir = data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "grihalekha.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("grihalekha")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    for font in (asset_dir() / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Noto Sans", 10))

    def report_exception(exc_type, error, traceback):
        logger.error("Unexpected application error", exc_info=(exc_type, error, traceback))
        if args.smoke_test:
            app.exit(1)
        else:
            QMessageBox.critical(None, "Action could not be completed", "An unexpected error occurred. Your saved account data has been retained.\n\nDetails are in the application log.")

    sys.excepthook = report_exception
    ledger = None
    try:
        if args.smoke_test and app.windowIcon().pixmap(32, 32).isNull():
            raise RuntimeError("The application icon could not be loaded.")
        if args.smoke_test:
            for icon in (asset_dir() / "ui").glob("*.svg"):
                if QIcon(str(icon)).pixmap(18, 18).isNull():
                    raise RuntimeError(f"A control icon could not be loaded: {icon.name}")
        from .backups import BackupManager
        from .ledger import Ledger
        from .ui import MainWindow

        manager = BackupManager(data_dir, data_dir / "grihalekha.sqlite3")
        manager.recover_interrupted_restore()
        try:
            ledger = Ledger(data_dir)
        except Exception:
            logger.exception("Account database could not be opened")
            if args.smoke_test:
                raise
            recovery_prompt = QMessageBox(QMessageBox.Icon.Critical, "Account data could not be opened", "The database may be damaged or need a different application version. You can restore a complete backup. Your existing files will be preserved before replacement.", parent=None)
            restore_button = recovery_prompt.addButton("Restore a backup…", QMessageBox.ButtonRole.ActionRole)
            recovery_prompt.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
            recovery_prompt.exec()
            if recovery_prompt.clickedButton() != restore_button:
                return 1
            selected, _ = QFileDialog.getOpenFileName(None, "Select a complete GrihaLekha backup", str(manager.backup_dir), "Backup archives (*.zip)")
            if not selected:
                return 1
            manager.validate(Path(selected))
            confirmation = QMessageBox.question(None, "Restore verified backup?", "Replace the active account data with this verified backup? Current files will be protected before replacement.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if confirmation != QMessageBox.StandardButton.Yes:
                return 1
            manager.restore(Path(selected))
            ledger = Ledger(data_dir)
        window = MainWindow(ledger)
        window.show()
        if args.smoke_pdf:
            from datetime import date
            from .reports import PdfExportJob, statement_html

            accounts = ledger.list_tenancies()
            if not accounts:
                logger.error("PDF smoke check requires an explicitly prepared audit account folder")
                return 2
            tenant = accounts[0]
            snapshot = ledger.statement(tenant["id"], tenant.get("opening_date") or tenant["move_in"], date.today().isoformat())
            pdf_job = PdfExportJob(statement_html(snapshot, "bilingual", "GL-PACKAGED-CHECK"), args.smoke_pdf, window)

            def pdf_complete(path):
                logger.info("Packaged PDF check completed using %s", pdf_job.renderer_used)
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    window.grab().save(str(args.screenshot))
                window.close()

            def pdf_failed(message):
                logger.error("Packaged PDF check failed: %s", message)
                app.exit(1)

            pdf_job.finished.connect(pdf_complete)
            pdf_job.failed.connect(pdf_failed)
            QTimer.singleShot(250, pdf_job.start)
        elif args.smoke_test or args.screenshot:
            def capture_and_finish():
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    window.grab().save(str(args.screenshot))
                if args.smoke_test:
                    window.close()
            QTimer.singleShot(1500, capture_and_finish)
        result = app.exec()
        return result
    except Exception:
        logger.exception("Application startup failed")
        if not args.smoke_test:
            QMessageBox.critical(None, "GrihaLekha could not start", f"The account data could not be opened.\n\nData folder: {data_dir}\n\nSee logs/grihalekha.log for details. Existing files have not been replaced with an empty account.")
        return 1
    finally:
        if ledger is not None:
            ledger.close()
        handler.close()
        logger.removeHandler(handler)
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())

