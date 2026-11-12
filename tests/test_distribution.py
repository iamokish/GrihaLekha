"""Publishing checks must catch incomplete source and accidental private data."""

import importlib.util
import io
from pathlib import Path
import tarfile
import zipfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "check_distribution", Path(__file__).resolve().parents[1] / "scripts/check_distribution.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

METADATA = (
    "Metadata-Version: 2.4\nName: grihalekha\nVersion: 0.1.0\n"
    "License-Expression: AGPL-3.0-or-later\nLicense-File: LICENSE\n"
    "License-File: THIRD_PARTY_NOTICES.md\n\n"
).encode()
DIST_INFO = "grihalekha-0.1.0.dist-info"


@pytest.fixture
def distribution(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    source = {name: b"Project documentation\n" for name in checker.PROJECT_FILES}
    source.update({
        "pyproject.toml": b'[project]\nversion = "0.1.0"\nlicense = "AGPL-3.0-or-later"\n',
        "src/grihalekha/__init__.py": b'__version__ = "0.1.0"\n',
        "src/grihalekha/__main__.py": b"def main(): return 0\n",
        "src/grihalekha/ui/window.py": b"class MainWindow: pass\n",
        "src/grihalekha/assets/ui/check.svg": b"<svg/>\n",
        "icons/grihalekha.ico": b"Icon bytes",
        "icons/grihalekha-icon-512.png": b"PNG bytes",
        "packaging/build.py": b"# Build recipe\n",
        "packaging/PKGBUILD.in": b"# Arch installation recipe\n",
        "packaging/licenses/GPL-3.0.txt": b"Dependency license\n",
        "scripts/check_distribution.py": b"# Distribution checker\n",
        "tests/test_startup.py": b"# Regression test\n",
        "docs/ARCHITECTURE.md": b"# Architecture\n",
        "docs/images/overview.png": b"Documentation screenshot PNG bytes",
        ".github/workflows/check.yml": b"name: Checks\n",
    })
    for family in ("NotoSans", "NotoSansDevanagari"):
        for style in ("Regular", "Bold"):
            source[f"src/grihalekha/assets/fonts/{family}-{style}.ttf"] = b"Font bytes"
    for name in ("OFL-NotoSans.txt", "OFL-Devanagari.txt"):
        source[f"src/grihalekha/assets/fonts/{name}"] = b"Font license\n"
    for name, content in source.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    wheel = {name.removeprefix("src/"): content for name, content in source.items()
             if name.startswith("src/")}
    for name in ("grihalekha.ico", "grihalekha-icon-512.png"):
        wheel[f"grihalekha/assets/icons/{name}"] = source[f"icons/{name}"]
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md",
                 "src/grihalekha/assets/fonts/OFL-NotoSans.txt",
                 "src/grihalekha/assets/fonts/OFL-Devanagari.txt"):
        wheel[f"{DIST_INFO}/licenses/{name}"] = source[name]
    wheel[f"{DIST_INFO}/METADATA"] = METADATA
    source["PKG-INFO"] = METADATA
    # setuptools' normal generated source metadata is permitted.
    source["src/grihalekha.egg-info/PKG-INFO"] = METADATA
    output = tmp_path / "distributions"
    output.mkdir()
    return root, output, wheel, source


def _write(distribution, extra_tar=None):
    _, output, wheel, source = distribution
    with zipfile.ZipFile(output / "grihalekha-0.1.0-py3-none-any.whl", "w") as archive:
        for name, content in wheel.items():
            archive.writestr(name, content)
    with tarfile.open(output / "grihalekha-0.1.0.tar.gz", "w:gz") as archive:
        for name, content in source.items():
            entry = tarfile.TarInfo("grihalekha-0.1.0/" + name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        if extra_tar is not None:
            archive.addfile(extra_tar)


def test_complete_distributions_preserve_current_source_and_metadata(distribution):
    root, output, _, _ = distribution
    _write(distribution)
    assert checker.check_distribution(output, root)["version"] == "0.1.0"
    (root / "src/grihalekha/ui/window.py").write_bytes(b"Changed after build\n")
    with pytest.raises(ValueError, match="stale or changed.*window.py"):
        checker.check_distribution(output, root)


@pytest.mark.parametrize("kind,name", [
    ("wheel", "grihalekha/ui/obsolete_dialog.py"),
    ("source", "src/grihalekha/ui/obsolete_dialog.py"),
    ("source", "docs/retired-plan.md"),
    ("source", "scripts/unused_preview.py"),
])
def test_distribution_rejects_removed_source_and_docs(distribution, kind, name):
    root, output, wheel, source = distribution
    (wheel if kind == "wheel" else source)[name] = b"Obsolete content\n"
    _write(distribution)
    with pytest.raises(ValueError, match="outside the current source"):
        checker.check_distribution(output, root)


@pytest.mark.parametrize(("kind", "name"), [
    ("wheel", "grihalekha/assets/icons/grihalekha.ico"),
    ("wheel", "grihalekha/assets/fonts/NotoSansDevanagari-Bold.ttf"),
    ("wheel", "grihalekha/assets/ui/check.svg"),
    ("wheel", f"{DIST_INFO}/licenses/LICENSE"),
    ("wheel", f"{DIST_INFO}/licenses/THIRD_PARTY_NOTICES.md"),
    ("source", "uv.lock"),
    ("source", ".github/workflows/check.yml"),
    ("source", "packaging/PKGBUILD.in"),
    ("source", "packaging/licenses/GPL-3.0.txt"),
    ("source", "tests/test_startup.py"),
    ("source", "docs/images/overview.png"),
])
def test_missing_assets_licenses_and_rebuild_files_fail(distribution, kind, name):
    root, output, wheel, source = distribution
    del (wheel if kind == "wheel" else source)[name]
    _write(distribution)
    with pytest.raises(ValueError, match="missing required files"):
        checker.check_distribution(output, root)


@pytest.mark.parametrize("name", [
    "src/grihalekha/private.sqlite3", "backup.sqlite3-wal", "build/app.exe",
    ".audit/preview.png", "src/grihalekha/__pycache__/ledger.pyc", ".env",
    "documents/tenant-statement.pdf", "../outside.txt", "C:/private.txt",
])
def test_private_data_generated_outputs_and_unsafe_paths_fail(distribution, name):
    root, output, _, source = distribution
    source[name] = b"Must not be published"
    _write(distribution)
    with pytest.raises(ValueError, match="Private data|Unsafe archive path"):
        checker.check_distribution(output, root)


@pytest.mark.parametrize(("before", "after"), [
    (b"Version: 0.1.0", b"Version: 0.2.0"),
    (b"License-Expression: AGPL-3.0-or-later", b"License-Expression: MIT"),
    (b"License-File: LICENSE\n", b""),
])
def test_incorrect_wheel_metadata_fails(distribution, before, after):
    root, output, wheel, _ = distribution
    wheel[f"{DIST_INFO}/METADATA"] = METADATA.replace(before, after)
    _write(distribution)
    with pytest.raises(ValueError, match="metadata"):
        checker.check_distribution(output, root)


def test_tar_links_and_case_collisions_are_rejected(distribution):
    root, output, wheel, _ = distribution
    link = tarfile.TarInfo("grihalekha-0.1.0/external-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../private"
    _write(distribution, link)
    with pytest.raises(ValueError, match="link or special file"):
        checker.check_distribution(output, root)
    wheel["grihalekha/__INIT__.py"] = b"Case collision"
    _write(distribution)
    with pytest.raises(ValueError, match="Duplicate archive path"):
        checker.check_distribution(output, root)


def test_stale_extra_distribution_is_rejected(distribution):
    root, output, _, _ = distribution
    _write(distribution)
    (output / "grihalekha-0.0.1-py3-none-any.whl").write_bytes(b"Old build")
    with pytest.raises(ValueError, match="exactly one"):
        checker.check_distribution(output, root)
