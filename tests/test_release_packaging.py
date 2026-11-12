"""Release safeguards that can run without compiling or installing the app."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("grihalekha_release", ROOT / "packaging" / "release.py")
release = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT / "packaging"))
try:
    SPEC.loader.exec_module(release)
    INSTALLER_SPEC = importlib.util.spec_from_file_location("grihalekha_installer_check", ROOT / "packaging" / "verify_windows_installer.py")
    installer_check = importlib.util.module_from_spec(INSTALLER_SPEC)
    INSTALLER_SPEC.loader.exec_module(installer_check)
finally:
    sys.path.pop(0)


def _bundle(tmp_path, monkeypatch):
    root = tmp_path / "source"
    source = root / "src" / "grihalekha" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", root)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "version": "0.1.0", "platform": "win32", "architecture": "x64",
        "source_sha256": {"src/grihalekha/__init__.py": release.sha256(source)},
        "verification": {"ok": True, "chromium_runtime_verified": True},
    }
    (bundle / "build-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in (
        "LICENSE", "THIRD_PARTY_NOTICES.md", "SOURCE_CODE.md", "THIRD_PARTY_COMPONENTS.json",
        "README.md", "USER_GUIDE.md", "CHANGELOG.md", "icons/grihalekha-icon-512.png",
        "docs/images/overview.png", "docs/images/tenant-account.png", "docs/images/statement.png",
    ):
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Bundled document", encoding="utf-8")
    (bundle / "licenses").mkdir()
    (bundle / "licenses" / "dependency.txt").write_text("License", encoding="utf-8")
    executable = bytearray(70)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 60, 64)
    executable[64:70] = b"PE\0\0\x64\x86"
    (bundle / "GrihaLekha.exe").write_bytes(executable)
    return bundle, manifest, source


def test_release_refuses_stale_sources_and_unverified_chromium(tmp_path, monkeypatch):
    bundle, manifest, source = _bundle(tmp_path, monkeypatch)
    assert release.validate_bundle(bundle, "windows", "0.1.0") == manifest
    source.write_text("changed source", encoding="utf-8")
    with pytest.raises(ValueError, match="current application source"):
        release.validate_bundle(bundle, "windows", "0.1.0")
    manifest["source_sha256"]["src/grihalekha/__init__.py"] = release.sha256(source)
    manifest["verification"]["chromium_runtime_verified"] = False
    (bundle / "build-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Chromium"):
        release.validate_bundle(bundle, "windows", "0.1.0")


def test_release_refuses_database_and_wrong_executable_architecture(tmp_path, monkeypatch):
    bundle, _, _ = _bundle(tmp_path, monkeypatch)
    database = bundle / "grihalekha.sqlite3"
    database.write_bytes(b"private account")
    with pytest.raises(ValueError, match="account database"):
        release.validate_bundle(bundle, "windows", "0.1.0")
    database.unlink()
    path = bundle / "GrihaLekha.exe"
    executable = bytearray(path.read_bytes())
    executable[68:70] = b"\x4c\x01"  # IMAGE_FILE_MACHINE_I386
    path.write_bytes(executable)
    with pytest.raises(ValueError, match="not x86-64"):
        release.validate_bundle(bundle, "windows", "0.1.0")


@pytest.mark.parametrize("name", [
    "CHANGELOG.md", "icons/grihalekha-icon-512.png", "docs/images/overview.png",
    "docs/images/tenant-account.png", "docs/images/statement.png",
])
def test_release_requires_complete_readme_assets_and_changelog(tmp_path, monkeypatch, name):
    bundle, _, _ = _bundle(tmp_path, monkeypatch)
    (bundle / name).unlink()
    with pytest.raises(ValueError, match="Required distribution document"):
        release.validate_bundle(bundle, "windows", "0.1.0")


def test_release_refuses_wrong_tag_or_commit(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Release tag"):
        release.validate_tag("v0.2.0", "0.1.0")
    bundle, _, _ = _bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "another-commit")
    with pytest.raises(ValueError, match="different GitHub commit"):
        release.validate_bundle(bundle, "windows", "0.1.0")


def test_release_notes_select_exact_version_and_preserve_markdown(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    notes = "### Added\n\n- Currency display: ₹, €, ¥.\n\n```md\n## [9.9.9]\n[0.1.0]: example-in-code\n```\n\nSee [details].\n\n[details]: https://example.org/details"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Future work.\n\n## [0.10.0] - 2026-12-01\n\n- Later release.\n\n"
        "## [0.1.0] - 2026-11-12\n\n" + notes + "\n\n## [0.0.1]\n\n- Older release.\n",
        encoding="utf-8-sig",
    )
    assert release.changelog_notes(changelog, "0.1.0") == notes


def test_release_notes_exclude_navigation_links_at_end_of_changelog(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [Unreleased]\n\n## [0.1.0]\n\n- Initial release.\n\n"
        "[Unreleased]: https://example.org/compare\n[0.1.0]: https://example.org/release\n",
        encoding="utf-8",
    )
    assert release.changelog_notes(changelog, "0.1.0") == "- Initial release."


@pytest.mark.parametrize("content", [
    "## [Unreleased]\n\n- Future work.\n",
    "## [0.10.0]\n\n- Different version.\n",
    "## [0.1.0]\n\n## [0.0.1]\n\n- Earlier version.\n",
    "## [0.1.0]\n\n- First.\n\n## [0.1.0] - 2026-11-12\n\n- Duplicate.\n",
    "## [0.1.0]\n\n[0.1.0]: https://example.org/release\n",
])
def test_release_notes_reject_missing_empty_or_duplicate_version_sections(tmp_path, content):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="CHANGELOG.md"):
        release.changelog_notes(changelog, "0.1.0")


def test_release_notes_cli_writes_exact_changes_and_versioned_download_help(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [0.1.0] - 2026-11-12\n\n### Added\n\n- Initial ₹ display.\n", encoding="utf-8")
    destination = tmp_path / "output" / "release-notes.md"
    monkeypatch.setattr(release, "project_version", lambda: "0.1.0")
    assert release.main(["--release-notes", str(destination), "--changelog", str(changelog), "--tag", "v0.1.0"]) == 0
    content = destination.read_text(encoding="utf-8")
    assert "### Added\n\n- Initial ₹ display." in content
    assert all(name in content for name in release.artifact_names("0.1.0").values())
    assert "SHA256SUMS.txt" in content
    assert "https://github.com/iamokish/GrihaLekha/blob/v0.1.0/CHANGELOG.md" in content
    assert "Get-FileHash .\\FILENAME" in content
    assert b"\r\n" not in destination.read_bytes()
    assert not list(destination.parent.glob(".release-notes-*"))
    changelog.write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        release.main(["--release-notes", str(destination), "--changelog", str(changelog)])
    assert error.value.code == 1
    assert destination.read_text(encoding="utf-8") == content


def test_release_notes_refuse_to_overwrite_the_changelog(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    content = "## [0.1.0]\n\n- Initial release.\n"
    changelog.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        release.write_release_notes(changelog, "0.1.0", changelog)
    assert changelog.read_text(encoding="utf-8") == content


def test_version_validation_catches_runtime_metadata_drift(tmp_path, monkeypatch):
    _, _, source = _bundle(tmp_path, monkeypatch)
    (release.ROOT / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    lock = release.ROOT / "uv.lock"
    lock.write_text('[[package]]\nname = "grihalekha"\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setitem(release.project_version.__globals__, "ROOT", release.ROOT)
    assert release.project_version() == "0.1.0"
    source.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="same version"):
        release.project_version()
    source.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    lock.write_text('[[package]]\nname = "grihalekha"\nversion = "0.2.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="uv.lock"):
        release.project_version()


def test_linux_executable_header_rejects_arm64(tmp_path):
    executable = bytearray(64)
    executable[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", executable, 18, 62)
    path = tmp_path / "GrihaLekha"
    path.write_bytes(executable)
    release._verify_executable(path, "linux")
    struct.pack_into("<H", executable, 18, 183)  # EM_AARCH64
    path.write_bytes(executable)
    with pytest.raises(ValueError, match="ELF x86-64"):
        release._verify_executable(path, "linux")


def test_checksums_require_exact_complete_release_and_hash_actual_bytes(tmp_path):
    names = release.artifact_names("0.1.0")
    for name in names.values():
        (tmp_path / name).write_bytes(name.encode("ascii"))
    checksum = release.write_checksums(tmp_path, "0.1.0")
    expected = "".join(f"{hashlib.sha256(name.encode('ascii')).hexdigest()}  {name}\n" for name in sorted(names.values()))
    assert checksum.read_bytes() == expected.encode("ascii")
    assert len(list(tmp_path.iterdir())) == 6
    stale = tmp_path / "old-release.zip"
    stale.write_bytes(b"stale")
    with pytest.raises(ValueError, match="unexpected"):
        release.write_checksums(tmp_path, "0.1.0")
    stale.unlink()
    (tmp_path / names["arch"]).unlink()
    with pytest.raises(ValueError, match="Missing"):
        release.write_checksums(tmp_path, "0.1.0")


def test_portable_archives_keep_one_folder_and_complete_file_contents(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    payload = bytes(range(256)) * 300
    (bundle / "assets" / "font.ttf").write_bytes(payload)
    destination = tmp_path / "portable.zip"
    release.make_zip(bundle, destination)
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["GrihaLekha/assets/font.ttf"]
        assert archive.read(archive.namelist()[0]) == payload
    destination = tmp_path / "portable.tar.gz"
    release.make_tar(bundle, destination)
    with tarfile.open(destination) as archive:
        assert archive.extractfile("GrihaLekha/assets/font.ttf").read() == payload
        assert {item.uid for item in archive} == {0}


@pytest.mark.skipif(os.name == "nt", reason="Unix executable modes and symlinks")
def test_linux_portable_preserves_executable_modes_and_library_symlinks(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "GrihaLekha").write_bytes(b"executable")
    (bundle / "GrihaLekha").chmod(0o755)
    (bundle / "libexample.so.1").write_bytes(b"library")
    (bundle / "libexample.so").symlink_to("libexample.so.1")
    destination = tmp_path / "portable.tar.gz"
    release.make_tar(bundle, destination)
    with tarfile.open(destination) as archive:
        assert archive.getmember("GrihaLekha/GrihaLekha").mode & 0o111 == 0o111
        link = archive.getmember("GrihaLekha/libexample.so")
        assert link.issym()
        assert link.linkname == "libexample.so.1"


def test_linux_layout_launches_fixed_bundle_and_keeps_account_data_separate(tmp_path, monkeypatch):
    bundle, _, _ = _bundle(tmp_path, monkeypatch)
    root = release.ROOT
    (root / "packaging").mkdir()
    (root / "packaging" / "grihalekha.desktop").write_text("[Desktop Entry]\nExec=grihalekha\n", encoding="utf-8")
    (root / "icons").mkdir()
    (root / "icons" / "grihalekha-icon-512.png").write_bytes(b"icon")
    layout = tmp_path / "package"
    release._install_layout(bundle, layout)
    assert (layout / "usr" / "bin" / "grihalekha").read_bytes() == b'#!/bin/sh\nexec /opt/grihalekha/GrihaLekha "$@"\n'
    assert (layout / "opt" / "grihalekha" / "LICENSE").is_file()
    assert not (layout / "home").exists()


def test_installer_check_refuses_local_execution_and_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError, match="CI-only"):
        installer_check._require_fresh_ci_runner()
    monkeypatch.setattr(installer_check, "ROOT", tmp_path / "workspace")
    with pytest.raises(ValueError, match="workspace's build directory"):
        installer_check._managed(tmp_path / "outside")


@pytest.mark.parametrize("name, mode", [("../outside.txt", 0), ("GrihaLekha/link", 0o120777)])
def test_installer_check_rejects_unsafe_zip_entries(tmp_path, monkeypatch, name, mode):
    monkeypatch.setattr(installer_check, "ROOT", tmp_path)
    archive_path = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = mode << 16
        archive.writestr(entry, "target")
    (tmp_path / "build").mkdir()
    with pytest.raises(ValueError, match="Unsafe"):
        installer_check._extract_portable(archive_path, tmp_path / "build" / "extracted")
    assert not (tmp_path / "outside.txt").exists()


def test_installer_check_extracts_the_portable_files(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_check, "ROOT", tmp_path)
    archive_path = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("GrihaLekha/GrihaLekha.exe", b"application")
        archive.writestr("GrihaLekha/assets/font.ttf", b"font")
    (tmp_path / "build").mkdir()
    extracted = installer_check._extract_portable(archive_path, tmp_path / "build" / "extracted")
    assert (extracted / "GrihaLekha.exe").read_bytes() == b"application"
    assert (extracted / "assets" / "font.ttf").read_bytes() == b"font"
