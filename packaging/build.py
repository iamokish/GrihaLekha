"""Build, verify, and publish a self-contained Nuitka desktop application.

Run from any directory with the project's Python environment. Compiler output
and caches stay inside this workspace. A failed compile or verification never
replaces the current distribution. User account data is never a build input.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import struct
import tomllib
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "nuitka"
DIST = ROOT / "dist"
FONT_NAMES = (
    "NotoSans-Regular.ttf", "NotoSans-Bold.ttf",
    "NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf",
    "OFL-NotoSans.txt", "OFL-Devanagari.txt",
)
ICON_NAMES = ("grihalekha.ico", "grihalekha-icon-512.png")
DOCUMENT_NAMES = ("README.md", "USER_GUIDE.md", "CHANGELOG.md", "SECURITY.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
DOCUMENT_ASSETS = (
    "icons/grihalekha-icon-512.png",
    "docs/images/overview.png",
    "docs/images/tenant-account.png",
    "docs/images/statement.png",
)


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        result = tomllib.load(stream)["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", result):
        raise ValueError("Release versions must have the form MAJOR.MINOR.PATCH.")
    module = ast.parse((ROOT / "src" / "grihalekha" / "__init__.py").read_text(encoding="utf-8"))
    app_versions = [ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if app_versions != [result]:
        raise ValueError("pyproject.toml and grihalekha.__version__ must have the same version.")
    with (ROOT / "uv.lock").open("rb") as stream:
        locked = tomllib.load(stream)
    locked_versions = [package.get("version") for package in locked.get("package", []) if package.get("name") == "grihalekha"]
    if locked_versions != [result]:
        raise ValueError("uv.lock must match the project version. Run uv lock before releasing.")
    return result


def require_x64() -> None:
    if struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Release builds require an x86-64 operating system and 64-bit Python.")


def build_command(*, jobs: int, compiler: str, lto: str) -> list[str]:
    require_x64()
    project_version()
    windows = sys.platform == "win32"
    if sys.platform not in {"win32", "linux"}:
        raise ValueError("Build on Windows or Linux for the corresponding target platform.")
    if not 1 <= jobs <= 64:
        raise ValueError("Compiler jobs must be between 1 and 64.")
    if not windows and compiler != "auto":
        raise ValueError("MSVC/MinGW compiler selection applies only to Windows builds.")
    if compiler == "mingw64" and sys.version_info >= (3, 13):
        raise ValueError("Use MSVC for Python 3.13+; the supported MinGW build uses Python 3.12.")
    command = [
        sys.executable, "-m", "nuitka", "--mode=standalone",
        "--update-check=never", "--enable-plugin=pyside6",
        "--include-package=grihalekha", "--include-package-data=grihalekha",
        f"--include-data-dir={ROOT / 'icons'}=grihalekha/assets/icons",
        "--include-module=sqlalchemy.dialects.sqlite.pysqlite",
        "--nofollow-import-to=pytest,pymupdf,fitz,numpy,onnxruntime",
        "--nofollow-import-to=sqlalchemy.dialects.mssql,sqlalchemy.dialects.mysql,sqlalchemy.dialects.oracle,sqlalchemy.dialects.postgresql",
        "--noinclude-data-files=*.debug.pak",
        "--python-flag=isolated", "--python-flag=no_docstrings",
        f"--output-dir={WORK}", "--output-folder-name=GrihaLekha",
        "--output-filename=" + ("GrihaLekha.exe" if windows else "GrihaLekha"),
        f"--jobs={jobs}", f"--lto={lto}",
        f"--report={WORK / 'compilation-report.xml'}",
        "--assume-yes-for-downloads",
    ]
    if windows:
        command += [
            "--windows-console-mode=disable", "--company-name=GrihaLekha",
            f"--windows-icon-from-ico={ROOT / 'icons' / 'grihalekha.ico'}",
            "--product-name=GrihaLekha", f"--file-version={project_version()}",
            f"--product-version={project_version()}",
            "--file-description=GrihaLekha offline rental accounts",
        ]
        if compiler == "msvc":
            command.append("--msvc=latest")
        elif compiler == "mingw64":
            command.append("--mingw64")
    command.append(str(ROOT / "scripts" / "launcher.py"))
    return command


def _copy_documentation(staged: Path) -> None:
    """Keep root documents and their relative image links usable offline."""
    for relative in (*DOCUMENT_NAMES, *DOCUMENT_ASSETS):
        destination = staged / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


def _managed_path(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or path.is_symlink():
        raise ValueError(f"Refusing to change an unmanaged build path: {path}")
    return resolved


def _remove_build_directory(path: Path, parent: Path) -> None:
    # Windows recursive cleanup is restricted to a checked, immediate child of
    # the explicitly managed build/output directory.
    checked = _managed_path(path, parent)
    if checked.exists():
        shutil.rmtree(checked)


@contextmanager
def build_lock():
    WORK.mkdir(parents=True, exist_ok=True)
    lock = WORK / "build.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError(f"A build is already running, or a previous build was interrupted. Check {lock} before starting another build.") from error
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def _publish(staged: Path) -> Path:
    target = _managed_path(DIST / "GrihaLekha", DIST)
    _managed_path(staged, DIST)
    previous = _managed_path(DIST / (".previous-" + uuid4().hex), DIST)
    moved_previous = False
    try:
        if target.exists():
            os.replace(target, previous)
            moved_previous = True
        os.replace(staged, target)
    except OSError:
        if moved_previous and previous.exists() and not target.exists():
            os.replace(previous, target)
        raise
    if moved_previous:
        try:
            _remove_build_directory(previous, DIST)
        except OSError:
            print(f"The new build is ready. A previous build remains at {previous} because Windows still has one of its files open.", flush=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="Parallel compiler jobs (default: up to 4)")
    parser.add_argument("--compiler", choices=("auto", "msvc", "mingw64"), default="msvc" if sys.platform == "win32" else "auto")
    parser.add_argument("--lto", choices=("auto", "yes", "no"), default="auto", help="Nuitka link-time optimization policy")
    parser.add_argument("--dry-run", action="store_true", help="Print build configuration without compiling or replacing files")
    args = parser.parse_args(argv)
    try:
        command = build_command(jobs=args.jobs, compiler=args.compiler, lto=args.lto)
        print(json.dumps({"command": command, "output": str(DIST / "GrihaLekha"), "cache": str(ROOT / ".cache" / "nuitka")}, indent=2), flush=True)
        if args.dry_run:
            return 0
        nuitka_version = version("Nuitka")
        if sys.version_info < (3, 12) or sys.version_info >= (3, 15):
            raise ValueError("This project requires Python 3.12–3.14.")
        for name in FONT_NAMES:
            if not (ROOT / "src" / "grihalekha" / "assets" / "fonts" / name).is_file():
                raise ValueError(f"Required bundled font/license is missing: {name}")
        for name in ICON_NAMES:
            if not (ROOT / "icons" / name).is_file():
                raise ValueError(f"Required application icon is missing: {name}")
        env = os.environ.copy()
        env["NUITKA_CACHE_DIR"] = str(ROOT / ".cache" / "nuitka")
        env["NUITKA_UPDATE_CHECK"] = "never"
        env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        DIST.mkdir(exist_ok=True)
        with build_lock():
            subprocess.run(command, cwd=ROOT, env=env, check=True)
            compiled = _managed_path(WORK / "GrihaLekha.dist", WORK)
            executable = compiled / ("GrihaLekha.exe" if sys.platform == "win32" else "GrihaLekha")
            if not executable.is_file():
                raise RuntimeError(f"Nuitka did not produce the expected executable: {executable}")
            staged = DIST / (".nuitka-staging-" + uuid4().hex)
            try:
                shutil.copytree(compiled, staged)
                _copy_documentation(staged)
                from license_notices import collect_notices
                from verify_build import verify_bundle

                notices = collect_notices(staged)
                verification = verify_bundle(staged, WORK / "verification")
                manifest = {
                    "application": "GrihaLekha", "version": project_version(),
                    "builder": "Nuitka", "nuitka_version": nuitka_version,
                    "python_version": sys.version.split()[0], "platform": sys.platform,
                    "architecture": "x64", "source_revision": env.get("GITHUB_SHA"),
                    "pyside6_version": version("PySide6"), "sqlalchemy_version": version("SQLAlchemy"),
                    "source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT / "src" / "grihalekha").rglob("*.py"))},
                    "verification": verification,
                    "licenses": notices,
                }
                (staged / "build-info.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                published = _publish(staged)
            finally:
                if staged.exists():
                    _remove_build_directory(staged, DIST)
            print(f"Verified Nuitka application: {published}", flush=True)
        return 0
    except PackageNotFoundError:
        parser.exit(1, "Install the project build dependencies first: uv sync --frozen --extra dev\n")
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Build failed: {error}\nThe previous published application has not been replaced unless the verified publish step completed.\n")


if __name__ == "__main__":
    raise SystemExit(main())
