import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest

from grihalekha.backups import BackupManager, _safe_name
from grihalekha.ledger import Ledger


def sample_data(tmp_path):
    data = tmp_path / "accounts"
    ledger = Ledger(data)
    building = ledger.add_building("Backup House", "Example address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenancy = ledger.add_tenancy(flat, "Tenant", "2020-01-01", 1, 0)
    ledger.record_payment(tenancy, 123456, "2020-01-01", "cash", allocations={})
    ledger.close()
    db = data / "grihalekha.sqlite3"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE payments SET proof_path='attachments/proof.pdf'")
    (data / "attachments").mkdir(exist_ok=True)
    (data / "attachments" / "proof.pdf").write_bytes(b"%PDF-1.4 payment proof fixture")
    (data / "documents").mkdir(exist_ok=True)
    (data / "documents" / "statement.pdf").write_bytes(b"%PDF-1.4 issued statement fixture")
    return data, db, BackupManager(data, db)


def test_complete_backup_restore_and_two_copy_retention(tmp_path):
    data, db, manager = sample_data(tmp_path)
    first = manager.create()
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE payments SET amount_paise=500000")
    second = manager.create()
    third = manager.create()
    assert len(manager.list_backups()) == 2
    assert not first.exists() and second.exists() and third.exists()
    external = manager.create(tmp_path / "external.zip")
    assert external.exists() and len(manager.list_backups()) == 2
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DELETE FROM payments")
    (data / "attachments" / "proof.pdf").unlink()
    manager.restore(external)
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT amount_paise FROM payments").fetchone()[0] == 500000
    assert (data / "attachments" / "proof.pdf").read_bytes().startswith(b"%PDF")
    assert (data / "documents" / "statement.pdf").exists()
    assert len(manager.list_backups()) == 2


def test_missing_proof_rejects_backup_without_rotating_successes(tmp_path):
    data, _, manager = sample_data(tmp_path)
    kept = [manager.create(), manager.create()]
    (data / "attachments" / "proof.pdf").unlink()
    with pytest.raises(ValueError, match="proof"):
        manager.create()
    assert set(manager.list_backups()) == set(kept)
    assert not list(manager.backup_dir.glob("*.partial"))


def test_restore_rejects_tampering_before_touching_live_files(tmp_path):
    data, db, manager = sample_data(tmp_path)
    valid = manager.create()
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(tampered, "w") as output:
        for name in source.namelist():
            content = source.read(name)
            output.writestr(name, b"X" * len(content) if name.endswith("proof.pdf") else content)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        manager.restore(tampered)
    assert db.read_bytes() == before


def test_restore_rejects_archive_traversal(tmp_path):
    _, _, manager = sample_data(tmp_path)
    bad = tmp_path / "unsafe.zip"
    content = b"not allowed"
    manifest = {"application": "GrihaLekha", "format": 1, "schema": 1, "files": {
        "grihalekha.sqlite3": {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
        "attachments/../../escaped": {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()},
    }}
    with zipfile.ZipFile(bad, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("grihalekha.sqlite3", b"")
        bundle.writestr("attachments/../../escaped", content)
    with pytest.raises(ValueError, match="unsafe"):
        manager.validate(bad)
    assert not (tmp_path / "escaped").exists()


def test_import_rejects_incomplete_database_even_with_valid_checksums(tmp_path):
    _, db, manager = sample_data(tmp_path)
    invalid = tmp_path / "incomplete.sqlite3"
    with closing(sqlite3.connect(invalid)) as connection:
        connection.execute("PRAGMA user_version=1")
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    content = invalid.read_bytes()
    manifest = {"application": "GrihaLekha", "format": 1, "schema": 1, "files": {
        "grihalekha.sqlite3": {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()},
    }}
    archive = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("grihalekha.sqlite3", content)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="required account tables"):
        manager.restore(archive)
    assert db.read_bytes() == before


def test_restore_from_corrupt_live_database_keeps_recovery_files(tmp_path):
    data, db, manager = sample_data(tmp_path)
    valid = manager.create(tmp_path / "good.zip")
    db.write_bytes(b"not a sqlite database")
    manager.restore(valid)
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT amount_paise FROM payments").fetchone()[0] == 123456
    recovery = list(data.glob("recovery-*"))
    assert len(recovery) == 1
    assert (recovery[0] / "grihalekha.sqlite3").read_bytes() == b"not a sqlite database"


def test_interrupted_restore_is_recovered_before_ledger_opens(tmp_path):
    data, db, manager = sample_data(tmp_path)
    recovery = data / "recovery-interrupted"
    recovery.mkdir()
    db.replace(recovery / db.name)
    db.write_bytes(b"part of a failed new database")
    (data / "restore-state.json").write_text(json.dumps({"recovery": recovery.name, "originals": [db.name, "attachments", "documents"]}))
    assert manager.recover_interrupted_restore()
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT amount_paise FROM payments").fetchone()[0] == 123456
    assert not manager.recover_interrupted_restore()


def test_backup_restores_on_another_data_path_without_original_files(tmp_path):
    source_data, _, source = sample_data(tmp_path)
    exported = source.create(tmp_path / "transfer.zip")
    target = tmp_path / "second-computer"
    target.mkdir()
    manager = BackupManager(target, target / "grihalekha.sqlite3")
    # A first-time restore has no database yet. It must install the snapshot.
    manager.restore(exported)
    assert (target / "attachments" / "proof.pdf").read_bytes() == (source_data / "attachments" / "proof.pdf").read_bytes()
    with closing(sqlite3.connect(target / "grihalekha.sqlite3")) as connection:
        assert connection.execute("SELECT amount_paise FROM payments").fetchone()[0] == 123456
    assert manager.create().is_file()


def test_online_backup_captures_committed_wal_data(tmp_path):
    _, db, manager = sample_data(tmp_path)
    with closing(sqlite3.connect(db)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("UPDATE payments SET amount_paise=98765")
        live.commit()
        exported = manager.create(tmp_path / "wal.zip")
    with zipfile.ZipFile(exported) as bundle:
        snapshot = tmp_path / "snapshot.sqlite3"
        snapshot.write_bytes(bundle.read("grihalekha.sqlite3"))
    with closing(sqlite3.connect(snapshot)) as connection:
        assert connection.execute("SELECT amount_paise FROM payments").fetchone()[0] == 98765


@pytest.mark.parametrize("path", ["attachments/CON.pdf", "documents/file.", "attachments/file ", "documents/a:b", "attachments/../outside", "documents/bad?.pdf"])
def test_backup_paths_are_portable_and_cannot_target_windows_devices(path):
    assert not _safe_name(path)


@pytest.mark.parametrize("corruption", ["other_account", "wrong_total", "missing_sources", "wrong_kind"])
def test_restore_rejects_invalid_deposit_sources_before_replacing_live_data(tmp_path, corruption):
    data, db, manager = sample_data(tmp_path)
    ledger = Ledger(data)
    building = ledger.list_buildings()[0]["id"]
    flat = ledger.add_flat(building, "2", "Ground", 1)
    other_tenancy = ledger.add_tenancy(flat, "Other tenant", "2020-01-01", 1, 0)
    other_payment = ledger.record_payment(other_tenancy, 10000, "2020-01-01", "cash", allocations={})
    transfer = ledger.transfer_balance_to_deposit(1, 10000, "2020-01-01", "Retain as security")
    ledger.close()
    valid = manager.create(tmp_path / "valid-transfer.zip")
    snapshot = tmp_path / "invalid-transfer.sqlite3"
    with zipfile.ZipFile(valid) as bundle:
        snapshot.write_bytes(bundle.read("grihalekha.sqlite3"))
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        if corruption == "other_account":
            connection.execute("UPDATE deposit_sources SET payment_id=?", (other_payment,))
        elif corruption == "wrong_total":
            connection.execute("UPDATE deposit_sources SET amount_paise=9999")
        elif corruption == "missing_sources":
            connection.execute("DELETE FROM deposit_sources")
        else:
            connection.execute("UPDATE deposits SET kind='opening' WHERE id=?", (transfer,))
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    corrupt = tmp_path / "invalid-transfer.zip"
    content = snapshot.read_bytes()
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(corrupt, "w") as output:
        manifest = json.loads(source.read("manifest.json"))
        manifest["files"]["grihalekha.sqlite3"] = {
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name in source.namelist():
            if name == "manifest.json":
                output.writestr(name, json.dumps(manifest))
            elif name == "grihalekha.sqlite3":
                output.writestr(name, content)
            else:
                output.writestr(name, source.read(name))
    before = db.read_bytes()
    proof_before = (data / "attachments" / "proof.pdf").read_bytes()
    with pytest.raises(ValueError, match="transfer sources"):
        manager.restore(corrupt)
    assert db.read_bytes() == before
    assert (data / "attachments" / "proof.pdf").read_bytes() == proof_before


def test_complete_backup_preserves_balance_to_deposit_and_reversal(tmp_path):
    data, _, manager = sample_data(tmp_path)
    ledger = Ledger(data)
    transfer = ledger.transfer_balance_to_deposit(1, 10000, "2020-01-01", "Retain as security")
    ledger.reverse_deposit(transfer, "Release security", "2020-01-02")
    ledger.close()
    exported = manager.create(tmp_path / "transfer-history.zip")
    manager.restore(exported)
    restored = Ledger(data)
    try:
        assert restored.balance(1, "2020-01-01")["credit_paise"] == 113456
        assert restored.balance(1, "2020-01-01")["deposit_paise"] == 10000
        assert restored.balance(1, "2020-01-02")["credit_paise"] == 123456
        assert restored.balance(1, "2020-01-02")["deposit_paise"] == 0
    finally:
        restored.close()


@pytest.mark.parametrize("folder_name", ["attachments", "documents"])
@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
def test_backup_rejects_linked_storage_without_copying_external_files(tmp_path, folder_name, nested):
    data, _, manager = sample_data(tmp_path)
    kept = {manager.create(), manager.create()}
    external = tmp_path / "external-files"
    external.mkdir()
    private_file = external / "private.txt"
    private_file.write_text("External files must stay outside the backup.", encoding="utf-8")
    folder = data / folder_name
    if nested:
        link = folder / "linked-folder"
    else:
        folder.rename(data / f"original-{folder_name}")
        link = folder
    if sys.platform == "win32":
        # Directory junctions need no symlink privilege and reproduce the
        # Windows case where Path.is_symlink() alone misses a linked root.
        import _winapi

        try:
            _winapi.CreateJunction(str(external), str(link))
        except OSError as exc:
            pytest.skip(f"The test filesystem cannot create directory junctions: {exc}")
        assert link.is_junction() and not link.is_symlink()
    else:
        link.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="application storage"):
            manager.create()
        assert set(manager.list_backups()) == kept
        assert not list(manager.backup_dir.glob("*.partial"))
        assert private_file.read_text(encoding="utf-8") == "External files must stay outside the backup."
    finally:
        if sys.platform == "win32":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("folder_name", ["attachments", "documents"])
def test_backup_rejects_managed_root_resolving_outside_data_dir(tmp_path, monkeypatch, folder_name):
    data, _, manager = sample_data(tmp_path)
    kept = {manager.create(), manager.create()}
    folder = data / folder_name
    external = tmp_path / "external-files"
    external.mkdir()
    original_resolve = Path.resolve

    def escaped_resolve(path, *args, **kwargs):
        # Model a filesystem redirect without needing Windows reparse-point
        # privileges. Descendants resolve relative to the redirected root too.
        if path.is_relative_to(folder):
            return external / path.relative_to(folder)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped_resolve)
    with pytest.raises(ValueError, match="application storage"):
        manager.create()
    assert set(manager.list_backups()) == kept
    assert not list(manager.backup_dir.glob("*.partial"))
