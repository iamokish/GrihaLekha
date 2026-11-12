"""Collect redistribution notices from the actual build environment.

The collector uses installed runtime distributions and a reviewed Qt snapshot.
It never opens a GrihaLekha account or downloads files during a build. This is an
inventory and notice collector, not an automated legal-compliance determination.
"""

from __future__ import annotations

import csv
from importlib.metadata import distribution
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import sysconfig

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
PYSIDE_COMPONENTS = frozenset({
    "pyside6", "pyside6-addons", "pyside6-essentials", "pyside6-pdf",
    "pyside6-webengine", "shiboken6",
})


def runtime_distributions(project: str = "grihalekha") -> list:
    """Resolve active base requirements, excluding development/optional extras."""
    pending = [(project, frozenset())]
    seen = set()
    result = {}
    while pending:
        name, extras = pending.pop()
        key = (canonicalize_name(name), extras)
        if key in seen:
            continue
        seen.add(key)
        installed = distribution(name)
        if canonicalize_name(name) != canonicalize_name(project):
            result[canonicalize_name(name)] = installed
        for raw in installed.requires or []:
            requirement = Requirement(raw)
            contexts = extras or {""}
            if requirement.marker and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in contexts
            ):
                continue
            pending.append((requirement.name, frozenset(requirement.extras)))
    return [result[name] for name in sorted(result)]


def _distribution_notices(
    installed, destination: Path, *, supplemental_notices: tuple[Path, ...] = (),
) -> dict:
    name = installed.metadata["Name"]
    component_dir = destination / canonicalize_name(name)
    component_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    # importlib.metadata may filter missing files out of .files. Read the wheel's
    # inventory directly so a supplement cannot hide a damaged installation.
    record = installed.read_text("RECORD")
    entries = ((row[0] for row in csv.reader(io.StringIO(record)) if row)
               if record is not None else installed.files or [])
    for entry in entries:
        relative = PurePosixPath(str(entry).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if not any(part.endswith(".dist-info") for part in relative.parts):
            continue
        filename = relative.name.lower()
        is_notice = "licenses" in relative.parts or filename.startswith(
            ("license", "copying", "notice", "authors")
        )
        if not is_notice:
            continue
        source = Path(installed.locate_file(entry))
        if not source.is_file():
            raise RuntimeError(f"Installed redistribution notice is missing: {name}/{relative.name}")
        # Preserve directory structure below .dist-info to avoid basename clashes.
        index = next(i for i, part in enumerate(relative.parts) if part.endswith(".dist-info"))
        target = component_dir.joinpath(*relative.parts[index + 1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(destination.parent).as_posix())
    for path in supplemental_notices:
        if not path.is_file():
            raise RuntimeError(f"Supplemental redistribution notice is missing: {name}/{path.name}")
        copied.append(path.relative_to(destination.parent).as_posix())
    if not copied:
        raise RuntimeError(f"No redistribution notices found for installed runtime component {name}.")
    metadata = installed.read_text("METADATA")
    if metadata:
        (component_dir / "METADATA").write_text(metadata, encoding="utf-8")
    return {
        "name": name,
        "version": installed.version,
        "license": installed.metadata.get("License-Expression") or installed.metadata.get("License"),
        "notices": sorted(copied),
        "upstream": installed.metadata.get_all("Project-URL") or [],
    }


def _pyside_notice_snapshot(qt_dir: Path) -> tuple[dict, tuple[Path, ...]]:
    """Verify the reviewed source notices omitted from some PySide wheels."""
    manifest = qt_dir / "pyside6-notices.json"
    snapshot = json.loads(manifest.read_text(encoding="utf-8"))
    if set(snapshot.get("components", {})) != PYSIDE_COMPONENTS:
        raise RuntimeError("PySide notice snapshot must cover the six reviewed runtime components.")
    if not snapshot.get("license_expression") or not snapshot.get("files"):
        raise RuntimeError("PySide notice snapshot is missing its license expression or files.")
    paths = [manifest]
    for entry in snapshot["files"]:
        filename = entry["path"]
        if (not filename or filename in {".", ".."}
                or any(character in filename for character in "/\\:\0")):
            raise RuntimeError(f"Invalid PySide notice snapshot filename: {filename!r}")
        path = qt_dir / filename
        if not path.is_file():
            raise RuntimeError(f"PySide notice snapshot file is missing: {filename}")
        # Keep verification independent of Git's platform-specific line endings.
        data = path.read_text(encoding="utf-8").encode("utf-8")
        if not data or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise RuntimeError(f"PySide notice snapshot checksum does not match: {filename}")
        paths.append(path)
    return snapshot, tuple(paths)


def _pyside_supplement(installed, snapshot: dict, paths: tuple[Path, ...]) -> tuple[Path, ...]:
    name = canonicalize_name(installed.metadata["Name"])
    if name not in PYSIDE_COMPONENTS:
        return ()
    expected = snapshot["components"][name]
    if installed.version != expected:
        raise RuntimeError(
            f"PySide notice snapshot has {name}={expected!r}, but this build uses {installed.version!r}. "
            "Run packaging/update_qt_notices.py and review the updated notices first."
        )
    expression = installed.metadata.get("License-Expression") or installed.metadata.get("License")
    if expression != snapshot["license_expression"]:
        raise RuntimeError(
            f"PySide notice snapshot license does not match installed runtime component {name}. "
            "Review the dependency's license and update the notices first."
        )
    return paths


def _python_license() -> Path:
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "share" / "doc" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE",
        Path(sys.base_prefix) / "share" / "licenses" / "python" / "LICENSE",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("The build Python's LICENSE.txt could not be located; retain the interpreter's license files.")


def _source_document(components: list[dict], qt_version: str, webengine_version: str) -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    revision = os.environ.get("GITHUB_SHA", "")
    if repository or revision:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            raise RuntimeError("GITHUB_REPOSITORY and GITHUB_SHA must identify the exact source checkout.")
        application_source = (
            f"Repository: https://github.com/{repository}\n\n"
            f"Revision: `{revision}`\n\n"
            f"Matching source: https://github.com/{repository}/archive/{revision}.tar.gz\n"
        )
    else:
        application_source = (
            "Project repository: https://github.com/iamokish/GrihaLekha\n\n"
            "This is a local build. Its matching application source is the checkout used to\n"
            "build it. Before redistributing this build, provide that complete source and\n"
            "the build/installation scripts alongside the binaries. GitHub release builds\n"
            "record the repository and exact revision here automatically.\n"
        )
    python_version = sys.version.split()[0]
    qt_series = ".".join(qt_version.split(".")[:2])
    rows = "\n".join(
        f"| {component['name']} | {component['version']} | https://pypi.org/project/{component['name']}/{component['version']}/ |"
        for component in components
    )
    return f"""# Source availability for this build

{application_source}
Application license: **AGPL-3.0-or-later**. See the bundled `LICENSE` file.

The application source includes `uv.lock`, `pyproject.toml`, `packaging/`, and
`.github/workflows/` for rebuilding and installing the application. Font source
provenance is recorded in `src/grihalekha/assets/fonts/README.md` in that source.

## Bundled upstream components

| Python component | Exact version | Upstream release information |
| --- | --- | --- |
{rows}

- CPython {python_version}: https://www.python.org/ftp/python/{python_version}/Python-{python_version}.tgz
- Qt {qt_version}: https://download.qt.io/official_releases/qt/{qt_series}/{qt_version}/submodules/
- Qt for Python / Shiboken source releases: https://download.qt.io/official_releases/QtForPython/pyside6/
- Qt WebEngine reports version {webengine_version}; use the Qt release's matching
  WebEngine source, including its Chromium sources and patches. Its internal
  version can differ from the PySide6 WebEngine wheel version shown above.
- Qt source repositories and tags: https://code.qt.io/cgit/qt/
- Qt for Python source repository: https://code.qt.io/cgit/pyside/pyside-setup.git/
- Nuitka source releases: https://github.com/Nuitka/Nuitka/releases

These are upstream source locations and version records, not a written source
offer. Before publishing, ensure recipients can obtain the complete corresponding
source for the exact binaries, including applicable Qt/Chromium sources, patches,
and build scripts. Keep that source available for as long as required by the
applicable licenses; mirror it if an upstream source disappears. A link to a
binary-only wheel does not provide its corresponding source.

`THIRD_PARTY_COMPONENTS.json` identifies the installed dependency versions;
`licenses/` contains their supplied notices and the reviewed Qt attributions.
Review native libraries in the Nuitka compilation report as well: Python package
metadata alone does not inventory every operating-system or compiler library.
"""


def collect_notices(bundle: Path) -> dict:
    """Add project/dependency notices to a staged standalone bundle; fail on gaps."""
    bundle = Path(bundle)
    if not bundle.is_dir():
        raise ValueError("License collection requires an existing staged bundle directory.")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, bundle / name)
    destination = bundle / "licenses"
    destination.mkdir(exist_ok=True)
    qt_dir = destination / "qt"
    qt_dir.mkdir(exist_ok=True)
    from PySide6.QtCore import qVersion
    from PySide6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineVersion

    snapshot_path = ROOT / "packaging" / "licenses" / "qt-attributions.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_html = snapshot_path.with_name("qt-third-party-notices.html")
    # Git and source archives may normalize text line endings across platforms.
    snapshot_bytes = snapshot_html.read_text(encoding="utf-8").encode("utf-8")
    if hashlib.sha256(snapshot_bytes).hexdigest() != snapshot.get("notice_file_sha256"):
        raise RuntimeError("Qt notice snapshot checksum does not match; restore or regenerate and review the notices.")
    qt_versions = {
        "qt_version": qVersion(),
        "pyside6_version": distribution("PySide6").version,
        "qt_webengine_version": qWebEngineVersion(),
        "chromium_version": qWebEngineChromiumVersion(),
    }
    for key, actual in qt_versions.items():
        if snapshot.get(key) != actual:
            raise RuntimeError(
                f"Qt notice snapshot has {key}={snapshot.get(key)!r}, but this build uses {actual!r}. "
                "Run packaging/update_qt_notices.py and review the updated notices first."
            )
    for path in sorted((ROOT / "packaging" / "licenses").iterdir()):
        if path.is_file():
            shutil.copyfile(path, qt_dir / path.name)
    pyside_snapshot, pyside_paths = _pyside_notice_snapshot(qt_dir)
    components = [
        _distribution_notices(
            item, destination,
            supplemental_notices=_pyside_supplement(item, pyside_snapshot, pyside_paths),
        )
        for item in runtime_distributions()
    ]
    # Nuitka's compiled runtime has its own notice and Section 7 exception.
    components.append(_distribution_notices(distribution("Nuitka"), destination))
    python_dir = destination / "python"
    python_dir.mkdir(exist_ok=True)
    shutil.copyfile(_python_license(), python_dir / "LICENSE.txt")

    result = {
        "application_license": "AGPL-3.0-or-later",
        "python_version": sys.version.split()[0],
        **qt_versions,
        "components": components,
        "qt_attributions": "licenses/qt/qt-third-party-notices.html",
    }
    (bundle / "THIRD_PARTY_COMPONENTS.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (bundle / "SOURCE_CODE.md").write_text(
        _source_document(components, result["qt_version"], result["qt_webengine_version"]), encoding="utf-8",
    )
    return result
