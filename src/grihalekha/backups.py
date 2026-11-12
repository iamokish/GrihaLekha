"""Complete, validated backup bundles with two rotating restore points.

The live SQLite file is read using SQLite's online backup API. ZIP files are
constructed in staging, validated, and atomically renamed before retention.
Restore stages every file before swapping any application data.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime, timezone
from uuid import uuid4
import zipfile

from . import __version__
from . import models

FORMAT_VERSION = 1
MAX_SCHEMA_VERSION = models.SCHEMA_VERSION
MAX_FILES = 50000
MAX_UNPACKED_BYTES = 10 * 1024**3


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return (
        bool(name)
        and "\\" not in name
        and ":" not in name
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in name.split("/"))
        and all(not any(char in '<>"|?*' or ord(char) < 32 for char in part) and not part.endswith((".", " ")) and part.split(".")[0].upper() not in reserved for part in path.parts)
        and (name == "grihalekha.sqlite3" or path.parts[0] in {"attachments", "documents"})
    )


def _database_check(path: Path, included: set[str] | None = None) -> int:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("The database integrity check failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("The database has broken record references.")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > MAX_SCHEMA_VERSION:
            raise ValueError("This backup needs a newer version of GrihaLekha.")
        required = models.required_schema(version)
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if set(required) - set(tables):
            raise ValueError("The backup database is missing required account tables.")
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
            if table in required and required[table] - columns:
                raise ValueError("The backup database is missing required account columns.")
            if "proof_path" in columns and included is not None:
                for (proof,) in connection.execute(f"SELECT proof_path FROM {quoted} WHERE proof_path IS NOT NULL"):
                    if proof and (not _safe_name(proof) or proof not in included):
                        raise ValueError("A payment proof referenced by the database is missing from the backup.")
        return version


class BackupManager:
    def __init__(self, data_dir: Path, db_path: Path):
        self.data_dir = Path(data_dir).resolve()
        self.db_path = Path(db_path).resolve()
        if self.db_path.parent != self.data_dir or self.db_path.name != "grihalekha.sqlite3":
            raise ValueError("The database must be in the GrihaLekha data folder.")
        self.backup_dir = self.data_dir / "backups"
        self._lock = threading.RLock()

    def recover_interrupted_restore(self) -> bool:
        """Rollback an interrupted multi-file restore before opening a Ledger."""
        journal = self.data_dir / "restore-state.json"
        if not journal.exists():
            return False
        state = json.loads(journal.read_text(encoding="utf-8"))
        recovery = self.data_dir / state["recovery"]
        allowed = {"grihalekha.sqlite3", "grihalekha.sqlite3-wal", "grihalekha.sqlite3-shm", "attachments", "documents"}
        originals = set(state["originals"])
        if recovery.parent.resolve() != self.data_dir or not recovery.name.startswith("recovery-") or not originals <= allowed:
            raise ValueError("An interrupted restore needs manual recovery; its recovery record is invalid.")
        if not recovery.is_dir() or recovery.is_symlink():
            raise ValueError("The interrupted restore's recovery folder is missing.")
        for name in allowed:
            current = self.data_dir / name
            previous = recovery / name
            if name in originals and previous.exists():
                if current.exists():
                    os.replace(current, recovery / ("interrupted-new-" + name))
                os.replace(previous, current)
            elif name not in originals and current.exists():
                os.replace(current, recovery / ("interrupted-new-" + name))
        journal.unlink()
        (recovery / "RECOVERY.txt").write_text("An interrupted restore was rolled back. These are unverified intermediate files.\n", encoding="utf-8")
        return True

    def list_backups(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("GrihaLekha-*.zip"), key=lambda p: p.name, reverse=True)

    def latest_status(self) -> str:
        backups = self.list_backups()
        if not backups:
            return "No backup yet"
        date = datetime.fromtimestamp(backups[0].stat().st_mtime).astimezone()
        return f"Last backup: {date:%d %b %Y, %H:%M}"

    def create(self, destination: Path | None = None) -> Path:
        with self._lock:
            if not self.db_path.is_file():
                raise ValueError("No database exists to back up.")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            managed = destination is None
            target = self.backup_dir / f"GrihaLekha-{stamp}-{uuid4().hex[:6]}.zip" if managed else Path(destination).resolve()
            if target.is_dir():
                target = target / f"GrihaLekha-{stamp}.zip"
            if target.suffix.lower() != ".zip":
                target = target.with_suffix(".zip")
            if target == self.db_path or _under(target, self.data_dir / "attachments") or _under(target, self.data_dir / "documents"):
                raise ValueError("Choose a backup destination outside the managed attachments and documents folders.")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + f".{uuid4().hex}.partial")
            try:
                with tempfile.TemporaryDirectory(prefix=".backup-", dir=self.data_dir) as temp:
                    staging = Path(temp)
                    snapshot = staging / "grihalekha.sqlite3"
                    source = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30)
                    try:
                        with closing(sqlite3.connect(snapshot)) as copy:
                            source.backup(copy)
                    finally:
                        source.close()
                    files = {"grihalekha.sqlite3": snapshot}
                    for folder_name in ("attachments", "documents"):
                        folder = self.data_dir / folder_name
                        if folder.is_symlink():
                            raise ValueError("Managed backup folders must not be symbolic links.")
                        if folder.exists():
                            for path in sorted(folder.rglob("*")):
                                if path.is_symlink() or not _under(path, folder):
                                    raise ValueError("A managed file points outside application storage.")
                                if path.is_file() and not path.name.endswith(".partial"):
                                    files[path.relative_to(self.data_dir).as_posix()] = path
                    schema = _database_check(snapshot, set(files))
                    manifest = {
                        "format": FORMAT_VERSION, "application": "GrihaLekha", "version": __version__,
                        "schema": schema, "created_at": datetime.now(timezone.utc).isoformat(), "files": {},
                    }
                    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                        for name, path in files.items():
                            manifest["files"][name] = {"sha256": _digest(path), "size": path.stat().st_size}
                            bundle.write(path, name)
                        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    self.validate(partial)
                    with partial.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    os.replace(partial, target)
                if managed:
                    for expired in self.list_backups()[2:]:
                        if expired.parent.resolve() != self.backup_dir.resolve():
                            raise ValueError("Unexpected backup retention path.")
                        expired.unlink()
                return target
            finally:
                partial.unlink(missing_ok=True)

    def _stage(self, path: Path, staging: Path) -> dict:
        try:
            with zipfile.ZipFile(path) as bundle:
                entries = bundle.infolist()
                names = [item.filename for item in entries]
                if len(names) != len({name.casefold() for name in names}) or len(names) > MAX_FILES:
                    raise ValueError("The backup has duplicate files or too many entries.")
                if "manifest.json" not in names:
                    raise ValueError("This is not a complete GrihaLekha backup.")
                if sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES:
                    raise ValueError("The backup is larger than the supported restoration limit.")
                manifest_info = bundle.getinfo("manifest.json")
                if manifest_info.file_size > 16 * 1024**2:
                    raise ValueError("Invalid backup manifest size.")
                manifest = json.loads(bundle.read("manifest.json"))
                if manifest.get("application") != "GrihaLekha" or manifest.get("format") != FORMAT_VERSION:
                    raise ValueError("Unsupported backup format.")
                if not isinstance(manifest.get("schema"), int) or manifest["schema"] > MAX_SCHEMA_VERSION:
                    raise ValueError("This backup needs a newer version of GrihaLekha.")
                files = manifest.get("files")
                if not isinstance(files, dict) or "grihalekha.sqlite3" not in files:
                    raise ValueError("The backup is missing its database.")
                if set(names) != set(files) | {"manifest.json"}:
                    raise ValueError("The backup file list does not match its manifest.")
                for name, details in files.items():
                    if not _safe_name(name):
                        raise ValueError("The backup contains an unsafe file path.")
                    info = bundle.getinfo(name)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or info.is_dir():
                        raise ValueError("Links and directory entries are not valid backup files.")
                    if info.file_size != details.get("size"):
                        raise ValueError("A backup file has an unexpected size.")
                    target = staging.joinpath(*PurePosixPath(name).parts)
                    if not _under(target, staging):
                        raise ValueError("Unsafe restoration path.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(name) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    if _digest(target) != details.get("sha256"):
                        raise ValueError("A backup checksum failed. This backup may be damaged.")
                actual_schema = _database_check(staging / "grihalekha.sqlite3", set(files))
                if actual_schema != manifest["schema"]:
                    raise ValueError("The backup database version does not match its manifest.")
                return manifest
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, AttributeError, sqlite3.DatabaseError) as error:
            raise ValueError(f"The backup could not be verified: {error}") from error

    def validate(self, path: Path) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, tempfile.TemporaryDirectory(prefix=".verify-", dir=self.data_dir) as temp:
            return self._stage(Path(path), Path(temp))

    def restore(self, path: Path) -> Path:
        """Restore with closed Ledger connections; return the prior-data copy."""
        with self._lock, tempfile.TemporaryDirectory(prefix=".restore-", dir=self.data_dir) as temp:
            staging = Path(temp)
            self._stage(Path(path), staging)
            safety_copy = None
            try:
                safety_copy = self.create()
            except (sqlite3.DatabaseError, ValueError):
                # A corrupt live database is precisely a reason to restore. Keep
                # its raw files in the rollback folder, never label them verified.
                pass
            recovery = self.data_dir / ("recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            recovery.mkdir()
            replaced: list[str] = []
            moved: list[str] = []
            names = ("grihalekha.sqlite3", "grihalekha.sqlite3-wal", "grihalekha.sqlite3-shm", "attachments", "documents")
            journal = self.data_dir / "restore-state.json"
            journal.write_text(json.dumps({"recovery": recovery.name, "originals": [name for name in names if (self.data_dir / name).exists()]}), encoding="utf-8")
            with journal.open("r+b") as stream:
                os.fsync(stream.fileno())
            try:
                for name in names:
                    current = self.data_dir / name
                    if current.exists():
                        os.replace(current, recovery / name)
                        moved.append(name)
                for name in ("grihalekha.sqlite3", "attachments", "documents"):
                    source = staging / name
                    if source.exists():
                        os.replace(source, self.data_dir / name)
                        replaced.append(name)
                _database_check(self.db_path)
            except BaseException:
                # Move newly installed data back into staging, then restore the
                # old files. Never recursively delete any unverified target.
                for name in reversed(replaced):
                    current = self.data_dir / name
                    if current.exists():
                        os.replace(current, staging / name)
                for name in moved:
                    os.replace(recovery / name, self.data_dir / name)
                journal.unlink(missing_ok=True)
                raise
            journal.unlink()
            if safety_copy is not None:
                if recovery.resolve().parent != self.data_dir or not recovery.name.startswith("recovery-"):
                    raise ValueError("Unexpected recovery cleanup path.")
                shutil.rmtree(recovery)
            else:
                (recovery / "RECOVERY.txt").write_text(
                    "Original files preserved before restoration. This folder is not a verified backup.\n",
                    encoding="utf-8",
                )
            return safety_copy if safety_copy is not None else recovery
