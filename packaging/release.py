"""Package a verified x64 bundle, or produce release notes and checksums.

Run on Windows for ZIP/Inno Setup, Ubuntu for tar.gz/deb, and Arch for makepkg.
This script never installs packages or reads the user's account-data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from build import ROOT, project_version, require_x64


OUTPUT = ROOT / "dist" / "release"
WORK = ROOT / "build" / "release"
REPOSITORY_URL = "https://github.com/iamokish/GrihaLekha"
DEBIAN_DEPENDENCIES = (
    "libc6 (>= 2.35)", "libstdc++6 (>= 12)", "libgcc-s1", "libglib2.0-0",
    "libdbus-1-3", "libfontconfig1", "libfreetype6", "libnss3", "libnspr4",
    "libasound2", "libcups2", "libgl1", "libegl1", "libopengl0", "libdrm2",
    "libx11-6", "libx11-xcb1", "libxext6", "libxfixes3", "libxrender1",
    "libxi6", "libxrandr2", "libxdamage1", "libxcomposite1", "libxtst6",
    "libxcb1", "libxcb-cursor0", "libxcb-icccm4", "libxcb-image0",
    "libxcb-keysyms1", "libxcb-render-util0", "libxcb-shape0", "libxcb-randr0",
    "libxcb-xfixes0", "libxcb-sync1", "libxcb-shm0", "libxcb-xkb1",
    "libxkbcommon0", "libxkbcommon-x11-0", "libwayland-client0",
)


def artifact_names(version: str) -> dict[str, str]:
    return {
        "windows_portable": f"GrihaLekha-{version}-windows-x64-portable.zip",
        "windows_setup": f"GrihaLekha-{version}-windows-x64-setup.exe",
        "linux_portable": f"GrihaLekha-{version}-linux-x64-portable.tar.gz",
        "debian": f"grihalekha_{version}_amd64.deb",
        "arch": f"grihalekha-{version}-1-x86_64.pkg.tar.zst",
    }


def validate_tag(tag: str, version: str) -> None:
    if tag != f"v{version}":
        raise ValueError(f"Release tag must be v{version}, matching pyproject.toml and the application; got {tag!r}.")


def changelog_notes(changelog: Path, version: str) -> str:
    """Read one version section, retaining Markdown and excluding the link index."""
    lines = changelog.read_text(encoding="utf-8-sig").splitlines()
    sections = []
    navigation_lines = set()
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        if re.match(r"^\[(?:Unreleased|\d+\.\d+\.\d+)\]:[ \t]+", line):
            navigation_lines.add(index)
        if re.match(r"^##[ \t]+", line):
            heading = re.fullmatch(r"##[ \t]+\[([^\]]+)\](?:[ \t]+[-–—][ \t]+.*)?[ \t]*", line)
            sections.append((index, heading[1] if heading else None))
    matches = [position for position, (_, name) in enumerate(sections) if name == version]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one ## [{version}] release section.")
    position = matches[0]
    start = sections[position][0] + 1
    end = sections[position + 1][0] if position + 1 < len(sections) else len(lines)
    # Keep other reference definitions usable by the selected notes; only the
    # changelog's release-navigation index belongs outside a release body.
    notes = "\n".join(line for index, line in enumerate(lines[start:end], start)
                      if index not in navigation_lines).strip()
    if not notes:
        raise ValueError(f"The CHANGELOG.md section for {version} has no release notes.")
    return notes


def write_release_notes(destination: Path, version: str, changelog: Path) -> Path:
    """Combine the reviewed changelog entry with version-specific download help."""
    notes = changelog_notes(changelog, version)
    names = artifact_names(version)
    tag_url = f"{REPOSITORY_URL}/blob/v{version}"
    content = f"""## What's new

{notes}

## Download and install

Choose the package for your operating system. All packages target x86-64 (x64)
and include Python, the PDF renderer, and fonts.

| System | Package | Install or run |
| --- | --- | --- |
| Windows 11 | `{names['windows_setup']}` | Run the per-user installer. |
| Windows 11, portable | `{names['windows_portable']}` | Extract, then open `GrihaLekha/GrihaLekha.exe`. |
| Debian 12+ / Ubuntu 22.04+ | `{names['debian']}` | `sudo apt install ./{names['debian']}` |
| Arch Linux | `{names['arch']}` | `sudo pacman -U ./{names['arch']}` |
| Linux, portable | `{names['linux_portable']}` | Extract, then run `GrihaLekha/GrihaLekha`. |

Keep portable folders intact. Linux needs the desktop libraries listed in the
[build guide]({tag_url}/packaging/README.md). The [user guide]({tag_url}/USER_GUIDE.md)
covers setup, billing, backups, and moving accounts between computers.

### Verify a download

`SHA256SUMS.txt` contains the SHA-256 digest of each of the five packages.
On Linux, save it beside the downloaded packages and run:

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
```

On Windows, run `Get-FileHash .\\FILENAME -Algorithm SHA256` in PowerShell and
compare the result with the corresponding line in `SHA256SUMS.txt`.

### Existing accounts

Export a complete backup before updating. Account data is stored separately
from the application; updates and uninstalling retain that data. Opening a
supported older database creates a verified snapshot before upgrading it.

[Full changelog]({tag_url}/CHANGELOG.md) · [Source code]({REPOSITORY_URL}/tree/v{version})
"""
    destination = destination.resolve()
    if destination == changelog.resolve():
        raise ValueError("Release notes must not overwrite CHANGELOG.md.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=destination.parent, prefix=".release-notes-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_executable(path: Path, target: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if target == "windows":
            if len(header) < 64 or header[:2] != b"MZ":
                raise ValueError("The Windows executable has no valid PE header.")
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            pe = stream.read(6)
            if pe != b"PE\0\0\x64\x86":
                raise ValueError("The Windows executable is not x86-64.")
        elif len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 62:
            raise ValueError("The Linux executable is not little-endian ELF x86-64.")


def validate_bundle(bundle: Path, target: str, version: str) -> dict:
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
    platform_name = "win32" if target == "windows" else "linux"
    if manifest.get("version") != version or manifest.get("platform") != platform_name or manifest.get("architecture") != "x64":
        raise ValueError("Bundle version, platform or architecture does not match the requested release.")
    verification = manifest.get("verification", {})
    if verification.get("ok") is not True:
        raise ValueError("The bundle has not passed the compiled application verifier.")
    if verification.get("chromium_runtime_verified") is not True:
        raise ValueError("Release packaging requires a successful Chromium PDF runtime check, including the native fallback check.")
    expected = {path.relative_to(ROOT).as_posix(): sha256(path) for path in sorted((ROOT / "src" / "grihalekha").rglob("*.py"))}
    if manifest.get("source_sha256") != expected:
        raise ValueError("The bundle does not match the current application source. Rebuild before packaging.")
    if os.environ.get("GITHUB_SHA") and manifest.get("source_revision") != os.environ["GITHUB_SHA"]:
        raise ValueError("The bundle was built from a different GitHub commit.")
    for name in (
        "LICENSE", "THIRD_PARTY_NOTICES.md", "SOURCE_CODE.md", "THIRD_PARTY_COMPONENTS.json",
        "README.md", "USER_GUIDE.md", "CHANGELOG.md", "icons/grihalekha-icon-512.png",
        "docs/images/overview.png", "docs/images/tenant-account.png", "docs/images/statement.png",
    ):
        if not (bundle / name).is_file():
            raise ValueError(f"Required distribution document is missing: {name}")
    if not (bundle / "licenses").is_dir() or not any((bundle / "licenses").rglob("*")):
        raise ValueError("The third-party license directory is missing or empty.")
    for path in bundle.rglob("*"):
        if path.is_symlink() and (target == "windows" or path.readlink().is_absolute() or not path.resolve().is_relative_to(bundle)):
            raise ValueError(f"Unsupported or external bundle symlink: {path}")
        if path.name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
            raise ValueError(f"An account database must never be packaged: {path}")
    executable = bundle / ("GrihaLekha.exe" if target == "windows" else "GrihaLekha")
    _verify_executable(executable, target)
    if target == "linux" and not os.access(executable, os.X_OK):
        raise ValueError("The Linux executable has lost its executable permission.")
    return manifest


def make_zip(bundle: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Windows ZIP cannot contain symlinks: {path}")
                if path.is_file():
                    archive.write(path, (Path("GrihaLekha") / path.relative_to(bundle)).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            invalid = archive.testzip()
            if invalid:
                raise ValueError(f"Corrupt portable ZIP entry: {invalid}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def make_tar(bundle: Path, destination: Path, *, compressed: bool = True) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with tarfile.open(temporary, "w:gz" if compressed else "w", dereference=False) as archive:
            def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                return info

            archive.add(bundle, arcname="GrihaLekha", filter=normalize)
        with tarfile.open(temporary, "r:*") as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_layout(bundle: Path, directory: Path) -> None:
    shutil.copytree(bundle, directory / "opt" / "grihalekha", symlinks=True)
    launcher = directory / "usr" / "bin" / "grihalekha"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexec /opt/grihalekha/GrihaLekha "$@"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    for source, relative in (
        (ROOT / "packaging" / "grihalekha.desktop", "usr/share/applications/grihalekha.desktop"),
        (ROOT / "icons" / "grihalekha-icon-512.png", "usr/share/icons/hicolor/512x512/apps/grihalekha.png"),
        (bundle / "LICENSE", "usr/share/doc/grihalekha/copyright"),
    ):
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)


def make_deb(bundle: Path, destination: Path, version: str, workspace: Path) -> None:
    package = workspace / "debian"
    _install_layout(bundle, package)
    control_dir = package / "DEBIAN"
    control_dir.mkdir()
    size = sum(path.stat().st_size for path in package.rglob("*") if path.is_file())
    control = (
        f"Package: grihalekha\nVersion: {version}\nArchitecture: amd64\n"
        "Maintainer: GrihaLekha contributors\nSection: utils\nPriority: optional\n"
        f"Homepage: {REPOSITORY_URL}\n"
        f"Installed-Size: {(size + 1023) // 1024}\nDepends: {', '.join(DEBIAN_DEPENDENCIES)}\n"
        "Description: Offline rent and tenant accounts\n"
        " Desktop rental accounts with exact balances, selectable currencies,\n"
        " Hindi and English statements, payment proofs, and local backups.\n"
        " Account data is stored in the user's data directory and is retained\n"
        " when uninstalling.\n"
    )
    (control_dir / "control").write_text(control, encoding="utf-8", newline="\n")
    (control_dir / "control").chmod(0o644)
    temporary = workspace / destination.name
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(package), str(temporary)], check=True)
    subprocess.run(["dpkg-deb", "--info", str(temporary)], check=True)
    os.replace(temporary, destination)


def make_arch(bundle: Path, destination: Path, version: str, workspace: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Run Arch packaging as an unprivileged user; makepkg must not run as root.")
    source = workspace / "bundle.tar"
    make_tar(bundle, source, compressed=False)
    template = (ROOT / "packaging" / "PKGBUILD.in").read_text(encoding="utf-8")
    content = template.replace("@VERSION@", version).replace("@SHA256@", sha256(source))
    (workspace / "PKGBUILD").write_text(content, encoding="utf-8", newline="\n")
    shutil.copyfile(ROOT / "packaging" / "grihalekha.desktop", workspace / "grihalekha.desktop")
    shutil.copyfile(ROOT / "icons" / "grihalekha-icon-512.png", workspace / "grihalekha.png")
    env = os.environ.copy()
    env["PKGDEST"] = str(workspace)
    # This packages an already compiled/verified bundle; no dependency install or
    # privileged command belongs here. Runtime dependencies remain in .PKGINFO.
    subprocess.run(["makepkg", "--nodeps", "--noconfirm"], cwd=workspace, env=env, check=True)
    built = workspace / destination.name
    if not built.is_file():
        raise ValueError(f"makepkg did not create the expected .pkg.tar.zst: {built}")
    subprocess.run(["bsdtar", "-xOf", str(built), ".PKGINFO"], check=True)
    os.replace(built, destination)


def make_windows_installer(destination: Path, version: str) -> None:
    compiler = shutil.which("ISCC.exe") or shutil.which("iscc")
    if compiler is None:
        candidate = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe"
        if candidate.is_file():
            compiler = str(candidate)
    if compiler is None:
        raise ValueError("Install Inno Setup 6 before building the Windows installer.")
    subprocess.run([compiler, f"/DAppVersion={version}", str(ROOT / "packaging" / "windows.iss")], check=True)
    if not destination.is_file():
        raise ValueError(f"Inno Setup did not produce {destination}")
    # Inno Setup's bootstrap may itself be 32-bit while installing an x64 app.
    # The application's x64 PE machine type is checked in validate_bundle.
    with destination.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Inno Setup did not produce a Windows executable.")


def write_checksums(directory: Path, version: str) -> Path:
    expected = set(artifact_names(version).values())
    found = {path.name for path in directory.iterdir() if path.name != "SHA256SUMS.txt"}
    if found != expected:
        raise ValueError(f"Expected exactly five release packages. Missing: {sorted(expected - found)}; unexpected: {sorted(found - expected)}")
    lines = []
    for name in sorted(expected):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Release artifact is missing, empty or a symlink: {name}")
        lines.append(f"{sha256(path)}  {name}\n")
    destination = directory / "SHA256SUMS.txt"
    temporary = directory / ".SHA256SUMS.partial"
    try:
        temporary.write_text("".join(lines), encoding="ascii", newline="\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--platform", choices=("windows", "linux"))
    action.add_argument("--arch-package", action="store_true")
    action.add_argument("--checksums", type=Path, metavar="DIRECTORY")
    action.add_argument("--validate-tag", metavar="TAG")
    action.add_argument("--print-version", action="store_true")
    action.add_argument("--release-notes", type=Path, metavar="FILE")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    parser.add_argument("--tag", help="Require the release tag to match the canonical version")
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist" / "GrihaLekha")
    args = parser.parse_args(argv)
    try:
        version = project_version()
        for tag in (args.tag, args.validate_tag):
            if tag:
                validate_tag(tag, version)
        if args.print_version or args.validate_tag:
            print(version)
            return 0
        if args.release_notes:
            print(write_release_notes(args.release_notes, version, args.changelog))
            return 0
        if args.checksums:
            print(write_checksums(args.checksums.resolve(), version))
            return 0
        require_x64()
        target = "linux" if args.arch_package else args.platform
        if (target == "windows" and sys.platform != "win32") or (target == "linux" and sys.platform != "linux"):
            raise ValueError("Build each installer on its target operating system.")
        bundle = args.bundle.resolve()
        if target == "windows" and bundle != (ROOT / "dist" / "GrihaLekha").resolve():
            raise ValueError("The Windows installer expects the verified bundle at dist/GrihaLekha.")
        validate_bundle(bundle, target, version)
        if not all(path.resolve().is_relative_to(ROOT.resolve()) for path in (OUTPUT, WORK)):
            raise ValueError("Release output and staging must remain inside the project workspace.")
        OUTPUT.mkdir(parents=True, exist_ok=True)
        WORK.mkdir(parents=True, exist_ok=True)
        names = artifact_names(version)
        # TemporaryDirectory removes only the fresh directory created under this
        # resolved, workspace-owned build path. Existing builds are never erased.
        with tempfile.TemporaryDirectory(prefix="stage-", dir=WORK) as temporary:
            workspace = Path(temporary)
            if args.arch_package:
                make_arch(bundle, OUTPUT / names["arch"], version, workspace)
            elif target == "windows":
                make_zip(bundle, OUTPUT / names["windows_portable"])
                make_windows_installer(OUTPUT / names["windows_setup"], version)
            else:
                make_tar(bundle, OUTPUT / names["linux_portable"])
                make_deb(bundle, OUTPUT / names["debian"], version, workspace)
        print(f"Release packages written to {OUTPUT}")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release packaging failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
