"""CI-only install, compiled-app verification, and uninstall on a fresh runner.

Refuses local execution and any existing GrihaLekha installation registration.
Only a unique installation directory under this repository's build directory is
used. The fixed product AppId must never be exercised against a real install.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

from build import ROOT, project_version, require_x64
from release import artifact_names, sha256
from verify_build import _stop_process_tree, verify_bundle


APP_ID = "{FD8D6BDC-77E6-45DB-BFE9-6FC644DE7B92}_is1"


def _require_fresh_ci_runner() -> None:
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("The installer lifecycle check is CI-only and requires a fresh Windows GitHub Actions runner.")
    require_x64()
    import winreg

    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view):
                    pass
            except FileNotFoundError:
                continue
            raise ValueError("An existing GrihaLekha installer registration was found. Use a fresh disposable runner.")


def _managed(path: Path) -> Path:
    build_root = ROOT.resolve() / "build"
    resolved = path.resolve()
    if not resolved.is_relative_to(build_root) or path.is_symlink():
        raise ValueError(f"Installer verification path must stay in this workspace's build directory: {path}")
    return resolved


def _run_hidden(command: list[str], *, directory: Path, label: str, timeout: int = 300) -> None:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b"Installer output did not close after terminating the process tree."
    (directory / f"{label}.stdout.log").write_bytes(stdout)
    (directory / f"{label}.stderr.log").write_bytes(stderr)
    if timed_out or process.returncode != 0:
        raise RuntimeError(f"{label} {'timed out' if timed_out else f'failed with exit code {process.returncode}'}. See {directory}.")


def _snapshot(directory: Path) -> dict[str, str]:
    return {path.relative_to(directory).as_posix(): sha256(path) for path in directory.rglob("*") if path.is_file()}


def _extract_portable(archive_path: Path, destination: Path) -> Path:
    destination = _managed(destination)
    destination.mkdir()
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (
                not name.parts or name.parts[0] != "GrihaLekha" or name.is_absolute()
                or ".." in name.parts or "\\" in member.filename or ":" in member.filename
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or member.filename.casefold() in seen
            ):
                raise ValueError(f"Unsafe or duplicate portable ZIP entry: {member.filename!r}")
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError(f"Portable ZIP entry escapes the verification directory: {member.filename!r}")
            seen.add(member.filename.casefold())
        if not seen:
            raise ValueError("The portable ZIP is empty.")
        archive.extractall(destination)
    return destination / "GrihaLekha"


def verify_installer() -> dict:
    _require_fresh_ci_runner()
    version = project_version()
    installer = ROOT / "dist" / "release" / artifact_names(version)["windows_setup"]
    if not installer.is_file() or installer.is_symlink():
        raise ValueError(f"Build the exact versioned installer first: {installer}")
    work = _managed(ROOT / "build" / "installer-check")
    work.mkdir(parents=True, exist_ok=True)
    run_dir = _managed(Path(tempfile.mkdtemp(prefix="run-", dir=work)))
    app_dir = _managed(run_dir / "app")
    verification_dir = _managed(ROOT / "build" / "installer-verification")
    result = {"ok": False, "installer": str(installer), "installation": str(app_dir), "run_dir": str(run_dir)}
    error = None
    fixture_dir = None
    snapshot = None
    try:
        portable = ROOT / "dist" / "release" / artifact_names(version)["windows_portable"]
        if not portable.is_file() or portable.is_symlink():
            raise ValueError(f"Build the exact versioned portable ZIP first: {portable}")
        portable_work = _managed(ROOT / "build" / "portable-check")
        portable_work.mkdir(parents=True, exist_ok=True)
        portable_run = _managed(Path(tempfile.mkdtemp(prefix="run-", dir=portable_work)))
        extracted = _extract_portable(portable, portable_run / "extracted")
        portable_report = verify_bundle(extracted, _managed(ROOT / "build" / "portable-verification"), require_chromium=True)
        result["portable_verification_report"] = portable_report["report_path"]
        _run_hidden([
            str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
            f"/DIR={app_dir}", "/GROUP=GrihaLekha CI check", "/TASKS=",
            f"/LOG={run_dir / 'install.log'}",
        ], directory=run_dir, label="install")
        report = verify_bundle(app_dir, verification_dir, require_chromium=True)
        result["verification_report"] = report["report_path"]
        if report.get("chromium_runtime_verified") is not True:
            raise RuntimeError("The installed application did not pass Chromium PDF runtime verification.")
        fixture_dir = _managed(Path(report["run_dir"]))
        snapshot = _snapshot(fixture_dir)
        if not snapshot:
            raise RuntimeError("The installed-application check produced no isolated account fixtures.")
    except Exception as caught:
        error = caught
        result["error"] = f"{type(caught).__name__}: {caught}"
    finally:
        uninstaller = _managed(app_dir / "unins000.exe")
        if uninstaller.is_file():
            try:
                _run_hidden([
                    str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                    f"/LOG={run_dir / 'uninstall.log'}",
                ], directory=run_dir, label="uninstall")
                deadline = time.monotonic() + 15
                while (app_dir / "GrihaLekha.exe").exists() and time.monotonic() < deadline:
                    time.sleep(0.25)
                if (app_dir / "GrihaLekha.exe").exists():
                    raise RuntimeError("Uninstall did not remove the installed application executable.")
                if snapshot is not None and _snapshot(fixture_dir) != snapshot:
                    raise RuntimeError("Uninstall changed the separate isolated account fixtures.")
                _require_fresh_ci_runner()  # The product registration must also be gone.
                result["uninstall_verified"] = True
                result["account_fixtures_preserved"] = snapshot is not None
            except Exception as caught:
                result["uninstall_error"] = f"{type(caught).__name__}: {caught}"
                if error is None:
                    error = caught
        elif error is None:
            error = RuntimeError("The installed application has no uninstaller.")
            result["error"] = str(error)
        result["ok"] = error is None
        (run_dir / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if error is not None:
        raise RuntimeError(f"Installer verification failed. See {run_dir / 'report.json'}: {error}") from error
    return result


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        result = verify_installer()
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
