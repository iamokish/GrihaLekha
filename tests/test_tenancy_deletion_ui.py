"""Deletion reaches the final confirmation only after the selected exports finish."""

from datetime import timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QLabel, QMessageBox

from grihalekha.clock import today_ist
from grihalekha.ledger import Ledger
from grihalekha.ui.lifecycle_dialogs import DeleteArchivedTenantDialog
from grihalekha.ui.theme import apply_theme
from grihalekha.ui.window import MainWindow


@pytest.fixture
def deletion_ui(qapp, qtbot, tmp_path, monkeypatch):
    from grihalekha import reports

    apply_theme(qapp)
    ledger = Ledger(tmp_path / "accounts")
    building = ledger.add_building("Archive house", "Address")
    flats = [ledger.add_flat(building, str(index), "Ground", 1) for index in (1, 2)]
    start = today_ist() - timedelta(days=10)
    tenant = ledger.add_tenancy(flats[0], "Archived household", start, 1, 0)
    other = ledger.add_tenancy(flats[1], "Current household", start, 1, 0)
    ledger.move_out(tenant, today_ist() - timedelta(days=1), 0)
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.archive_tenancy(tenant)
    window = MainWindow(ledger)
    window._skip_close_backup = True
    qtbot.addWidget(window)
    window.show()
    window.show_archived.setChecked(True)
    window.select_account(tenant)
    qtbot.waitExposed(window)
    state = {
        "window": window, "ledger": ledger, "tenant": tenant, "other": other,
        "errors": [], "questions": [], "information": [], "warnings": [], "jobs": [],
        "backup": True, "accept_dialog": True, "confirm": False,
        "folder": str(tmp_path / "exports"), "folder_requests": [],
    }

    class ControlledPdfJob(QObject):
        finished = Signal(str)
        failed = Signal(str)

        def __init__(self, html, destination, parent=None):
            super().__init__(parent)
            self.html = html
            self.destination = Path(destination)
            self.started = False
            state["jobs"].append(self)

        def start(self):
            self.started = True

        def complete(self, content=b"%PDF-1.4\ncompleted test document\n%%EOF\n"):
            self.destination.write_bytes(content)
            self.finished.emit(str(self.destination))

    def accept(dialog):
        if not state["accept_dialog"]:
            return QDialog.DialogCode.Rejected
        dialog.backup.setChecked(state["backup"])
        dialog.result_data = dialog.validate()
        return QDialog.DialogCode.Accepted

    def folder(*args):
        state["folder_requests"].append(args)
        return state["folder"]

    def confirm(*args):
        state["questions"].append(args)
        return QMessageBox.StandardButton.Yes if state["confirm"] else QMessageBox.StandardButton.No

    monkeypatch.setattr(reports, "PdfExportJob", ControlledPdfJob)
    monkeypatch.setattr(DeleteArchivedTenantDialog, "exec", accept)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", folder)
    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: state["information"].append(args))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: state["warnings"].append(args))
    monkeypatch.setattr(window, "_error", state["errors"].append)
    yield state
    window._pdf_jobs.clear()
    window.close()
    ledger.close()


def assert_account_kept(state):
    assert state["ledger"].get_tenancy(state["tenant"])["archived_on"]


def test_archive_deletion_dialog_defaults_to_both_exports_and_uses_theme_controls(deletion_ui, qtbot):
    state = deletion_ui
    preview = state["ledger"].prepare_tenancy_deletion(state["tenant"])
    dialog = DeleteArchivedTenantDialog(preview)
    qtbot.addWidget(dialog)
    assert dialog.validate() == {"backup": True, "language": "en"}
    assert dialog.language.isEnabled()
    dialog.backup.setChecked(False)
    assert not dialog.language.isEnabled()
    assert dialog.validate()["backup"] is False
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Save).text() == "Continue"
    hints = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "Archived household" in hints
    assert "full account history and payment proofs" in hints
    assert "Existing backups and issued PDFs are retained" in hints
    assert state["window"].delete_tenant_action.isVisible()
    assert state["window"].delete_tenant_action.isEnabled()
    state["window"].select_account(state["other"])
    assert not state["window"].delete_tenant_action.isVisible()
    assert not state["window"].delete_tenant_action.isEnabled()


@pytest.mark.parametrize("cancel_at", ["dialog", "folder"])
def test_cancelled_export_choices_leave_account_and_files_untouched(deletion_ui, cancel_at):
    state = deletion_ui
    revision = state["ledger"].get_revision()
    if cancel_at == "dialog":
        state["accept_dialog"] = False
    else:
        state["folder"] = ""
    state["window"].delete_archived_account()
    assert_account_kept(state)
    assert not state["jobs"] and not state["questions"] and not state["errors"]
    assert state["ledger"].get_revision() == revision
    if cancel_at == "dialog":
        assert not state["folder_requests"]


def test_both_exports_finish_before_final_confirmation_and_deletion(deletion_ui):
    state = deletion_ui
    state["confirm"] = True
    state["window"].delete_archived_account()
    assert_account_kept(state)
    assert not state["questions"]
    job = state["jobs"][0]
    assert job.started and state["window"]._pdf_jobs
    assert job.destination.with_suffix(".zip").is_file()
    assert not job.destination.exists()
    job.complete()
    assert len(state["questions"]) == 1
    question = state["questions"][0]
    assert question[1] == "Permanently delete archived tenant"
    assert "Saved both records" in question[2]
    assert str(job.destination) in question[2]
    assert str(job.destination.with_suffix(".zip")) in question[2]
    assert question[-1] == QMessageBox.StandardButton.No
    assert not state["window"]._pdf_jobs and not state["errors"]
    with pytest.raises(ValueError, match="not found"):
        state["ledger"].get_tenancy(state["tenant"])
    assert state["window"].account_combo.findData(state["tenant"]) == -1
    assert state["ledger"].get_tenancy(state["other"])["tenant_name"] == "Current household"
    assert job.destination.exists() and job.destination.with_suffix(".zip").exists()


def test_declining_final_confirmation_keeps_account_and_completed_exports(deletion_ui):
    state = deletion_ui
    state["window"].delete_archived_account()
    job = state["jobs"][0]
    job.complete()
    assert_account_kept(state)
    assert len(state["questions"]) == 1
    assert job.destination.exists() and job.destination.with_suffix(".zip").exists()
    assert not state["window"]._pdf_jobs and not state["errors"]


def test_explicit_no_backup_choice_still_requires_final_confirmation(deletion_ui):
    state = deletion_ui
    state["backup"] = False
    state["window"].delete_archived_account()
    assert_account_kept(state)
    assert "No CSV or PDF backup was requested" in state["questions"][0][2]
    assert not state["folder_requests"] and not state["jobs"]
    state["confirm"] = True
    state["window"].delete_archived_account()
    assert len(state["questions"]) == 2
    with pytest.raises(ValueError, match="not found"):
        state["ledger"].get_tenancy(state["tenant"])
    assert not state["errors"]


def test_pdf_failure_preserves_account_and_csv_without_showing_delete_confirmation(deletion_ui):
    state = deletion_ui
    state["confirm"] = True
    state["window"].delete_archived_account()
    job = state["jobs"][0]
    job.failed.emit("Simulated printer failure")
    assert_account_kept(state)
    assert not state["questions"] and not state["window"]._pdf_jobs
    assert job.destination.with_suffix(".zip").is_file()
    assert len(state["errors"]) == 1
    assert "No tenant data was deleted" in str(state["errors"][0])
    assert "Simulated printer failure" in str(state["errors"][0])


def test_incomplete_pdf_completion_does_not_allow_deletion(deletion_ui):
    state = deletion_ui
    state["confirm"] = True
    state["window"].delete_archived_account()
    state["jobs"][0].complete(b"%PDF-1.4\nuncompleted output")
    assert_account_kept(state)
    assert not state["questions"] and not state["window"]._pdf_jobs
    assert len(state["errors"]) == 1 and "incomplete" in str(state["errors"][0])


def test_csv_failure_stops_before_starting_pdf_or_confirming_deletion(deletion_ui, monkeypatch):
    state = deletion_ui

    def fail(*args):
        raise OSError("CSV destination is full")

    monkeypatch.setattr(state["ledger"], "export_tenancy_deletion_csv", fail)
    state["window"].delete_archived_account()
    assert_account_kept(state)
    assert not state["jobs"] and not state["questions"]
    assert len(state["errors"]) == 1 and "destination is full" in str(state["errors"][0])


def test_account_change_while_pdf_runs_requires_fresh_review(deletion_ui):
    state = deletion_ui
    state["confirm"] = True
    state["window"].delete_archived_account()
    state["ledger"].record_payment(state["other"], 100, today_ist(), "cash")
    state["jobs"][0].complete()
    assert_account_kept(state)
    assert not state["questions"] and not state["window"]._pdf_jobs
    assert len(state["errors"]) == 1 and "data changed" in str(state["errors"][0])


def test_revision_change_during_final_confirmation_prevents_deletion(deletion_ui, monkeypatch):
    state = deletion_ui

    def changed_before_yes(*args):
        state["questions"].append(args)
        state["ledger"].record_payment(state["other"], 100, today_ist(), "cash")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", changed_before_yes)
    state["window"].delete_archived_account()
    state["jobs"][0].complete()
    assert_account_kept(state)
    assert len(state["questions"]) == 1
    assert len(state["errors"]) == 1 and "data changed" in str(state["errors"][0])
    assert not state["window"]._pdf_jobs


def test_pending_pdf_blocks_a_second_deletion_flow(deletion_ui):
    state = deletion_ui
    state["window"].delete_archived_account()
    state["window"].delete_archived_account()
    assert len(state["jobs"]) == 1 and len(state["folder_requests"]) == 1
    assert_account_kept(state)
    assert len(state["errors"]) == 1 and "current PDF export" in str(state["errors"][0])


def test_changed_workspace_prevents_deletion_of_original_account(deletion_ui, tmp_path):
    state = deletion_ui
    replacement = Ledger(tmp_path / "replacement")
    state["window"].delete_archived_account()
    try:
        state["window"].ledger = replacement
        state["jobs"][0].complete()
        assert_account_kept(state)
        assert not state["questions"] and not state["window"]._pdf_jobs
        assert len(state["errors"]) == 1 and "workspace changed" in str(state["errors"][0])
    finally:
        state["window"].ledger = state["ledger"]
        replacement.close()
