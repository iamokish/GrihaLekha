"""Release notices must cover runtime dependencies without pulling in dev tools."""

from importlib.metadata import Distribution
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from packaging.utils import canonicalize_name
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("license_notices", ROOT / "packaging" / "license_notices.py")
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


@pytest.fixture
def installed_distribution(tmp_path):
    """Create real installed metadata/RECORD files without platform binaries."""
    def create(name, *, version="1.0", expression="MIT", files=None, requires=()):
        package_dir = tmp_path / "site-packages" / f"{name.replace('-', '_')}-{version}.dist-info"
        package_dir.mkdir(parents=True, exist_ok=True)
        metadata = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
        if expression is not None:
            metadata.append(f"License-Expression: {expression}")
        metadata.extend(f"Requires-Dist: {requirement}" for requirement in requires)
        payload = {"METADATA": "\n".join(metadata) + "\n", **(files or {})}
        for relative, content in payload.items():
            if content is not None:
                target = package_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        (package_dir / "RECORD").write_text(
            "".join(f"{package_dir.name}/{relative},,\n" for relative in [*payload, "RECORD"]),
            encoding="utf-8",
        )
        return Distribution.at(package_dir)

    return create


@pytest.fixture
def linux_pyside_distributions(installed_distribution, monkeypatch):
    """Reproduce the six Linux wheels that declare terms but omit license files."""
    original_distribution = notices.distribution
    replacements = {}
    for name in notices.PYSIDE_COMPONENTS:
        original = original_distribution(name)
        replacements[name] = installed_distribution(
            original.metadata["Name"], version=original.version,
            expression=original.metadata.get("License-Expression") or original.metadata.get("License"),
            requires=original.requires or [],
        )

    def lookup(name):
        return replacements.get(canonicalize_name(name)) or original_distribution(name)

    monkeypatch.setattr(notices, "distribution", lookup)
    return replacements


@pytest.fixture
def notice_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "packaging" / "licenses", source / "packaging" / "licenses")
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / filename, source / filename)
    monkeypatch.setattr(notices, "ROOT", source)
    return source / "packaging" / "licenses"


def test_runtime_dependency_walk_respects_optional_extras_and_transitive_dependencies(monkeypatch):
    def dependency(name, requires):
        return SimpleNamespace(metadata={"Name": name}, requires=requires)

    packages = {
        "app": dependency("app", ["runtime[feature]", 'test-only; extra == "dev"']),
        "runtime": dependency("runtime", ["base", 'feature-only; extra == "feature"', 'unused; extra == "other"']),
        "base": dependency("base", ["runtime"]),
        "feature-only": dependency("feature-only", []),
    }
    monkeypatch.setattr(notices, "distribution", packages.__getitem__)
    result = notices.runtime_distributions("app")
    assert [item.metadata["Name"] for item in result] == ["base", "feature-only", "runtime"]


def test_missing_installed_license_stops_distribution(tmp_path, installed_distribution):
    package = installed_distribution("UnlicensedDependency")
    with pytest.raises(RuntimeError, match="No redistribution notices"):
        notices._distribution_notices(package, tmp_path)


def test_linux_wheels_use_reviewed_notices_and_keep_each_components_metadata(
    tmp_path, linux_pyside_distributions,
):
    result = notices.collect_notices(tmp_path)
    components = {canonicalize_name(item["name"]): item for item in result["components"]}
    assert notices.PYSIDE_COMPONENTS <= components.keys()
    for name, installed in linux_pyside_distributions.items():
        item = components[name]
        assert item["version"] == installed.version
        assert item["license"] == installed.metadata["License-Expression"]
        assert (tmp_path / "licenses" / name / "METADATA").read_text(encoding="utf-8") == installed.read_text("METADATA")
        paths = {Path(path).name: tmp_path / path for path in item["notices"]}
        assert "GNU GENERAL PUBLIC LICENSE" in paths["GPL-2.0-only.txt"].read_text(encoding="utf-8")
        assert "GNU GENERAL PUBLIC LICENSE" in paths["GPL-3.0-only.txt"].read_text(encoding="utf-8")
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in paths["LGPL-3.0-only.txt"].read_text(encoding="utf-8")
        assert "PSF LICENSE AGREEMENT FOR PYTHON 3.7.0" in paths["PSF-3.7.0.txt"].read_text(encoding="utf-8")
        assert "The Qt Company Ltd." in paths["pyside6-licensecomment.txt"].read_text(encoding="utf-8")
        snapshot = json.loads(paths["pyside6-notices.json"].read_text(encoding="utf-8"))
        assert snapshot["components"][name] == installed.version
        assert len(snapshot["source_revision"]) == 40


def test_supplement_preserves_installed_notices_and_same_named_files(
    tmp_path, installed_distribution,
):
    destination = tmp_path / "licenses"
    qt_dir = destination / "qt"
    qt_dir.mkdir(parents=True)
    supplement = qt_dir / "LICENSE"
    supplement.write_text("Reviewed source terms", encoding="utf-8")
    expected = {"licenses/qt/LICENSE": "Reviewed source terms"}
    for name in ("FirstDependency", "SecondDependency"):
        package = installed_distribution(name, files={
            "LICENSE": f"{name} original terms",
            "licenses/LICENSE": f"{name} additional terms",
        })
        item = notices._distribution_notices(package, destination, supplemental_notices=(supplement,))
        component_dir = f"licenses/{canonicalize_name(name)}"
        expected.update({
            f"{component_dir}/LICENSE": f"{name} original terms",
            f"{component_dir}/licenses/LICENSE": f"{name} additional terms",
        })
        assert set(item["notices"]) == {
            f"{component_dir}/LICENSE", f"{component_dir}/licenses/LICENSE", "licenses/qt/LICENSE",
        }
    for relative, content in expected.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("name", ["UnlicensedDependency", "PySide6_Future"])
def test_reviewed_pyside_notices_do_not_cover_unknown_packages(
    tmp_path, monkeypatch, installed_distribution, name,
):
    package = installed_distribution(
        name, version="6.12.0", expression="LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    )
    monkeypatch.setattr(notices, "runtime_distributions", lambda: [package])
    with pytest.raises(RuntimeError, match=f"No redistribution notices.*{name}"):
        notices.collect_notices(tmp_path)


@pytest.mark.parametrize("name,version", [
    ("pyside6-addons", "6.12.0.1"),
    ("pyside6-pdf", "6.12.0.141"),
    ("shiboken6", "6.12.0.1"),
])
def test_supplement_requires_the_exact_component_version(
    tmp_path, installed_distribution, linux_pyside_distributions, name, version,
):
    installed = linux_pyside_distributions[name]
    linux_pyside_distributions[name] = installed_distribution(
        installed.metadata["Name"], version=version,
        expression=installed.metadata["License-Expression"], requires=installed.requires or [],
    )
    with pytest.raises(RuntimeError, match=f"PySide notice snapshot has {name}="):
        notices.collect_notices(tmp_path)


@pytest.mark.parametrize("expression", ["MIT", None])
def test_supplement_rejects_changed_or_absent_license_terms(
    tmp_path, installed_distribution, linux_pyside_distributions, expression,
):
    installed = linux_pyside_distributions["shiboken6"]
    linux_pyside_distributions["shiboken6"] = installed_distribution(
        installed.metadata["Name"], version=installed.version,
        expression=expression, requires=installed.requires or [],
    )
    with pytest.raises(RuntimeError, match="PySide notice snapshot license does not match"):
        notices.collect_notices(tmp_path)


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_snapshot_requires_the_reviewed_component_set(tmp_path, notice_source, change):
    manifest = notice_source / "pyside6-notices.json"
    snapshot = json.loads(manifest.read_text(encoding="utf-8"))
    if change == "missing":
        del snapshot["components"]["shiboken6"]
    else:
        snapshot["components"]["pyside6-future"] = "6.12.0"
    manifest.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(RuntimeError, match="six reviewed runtime components"):
        notices.collect_notices(tmp_path)


@pytest.mark.parametrize("change,error", [
    ("corrupted", "PySide notice snapshot checksum does not match"),
    ("missing", "PySide notice snapshot file is missing"),
])
def test_snapshot_rejects_corrupted_or_missing_source_notices(tmp_path, notice_source, change, error):
    path = notice_source / "PSF-3.7.0.txt"
    if change == "corrupted":
        path.write_text("Incomplete source notice", encoding="utf-8")
    else:
        path.unlink()
    with pytest.raises(RuntimeError, match=error):
        notices.collect_notices(tmp_path)


def test_snapshot_checksums_accept_platform_line_endings(tmp_path, notice_source):
    path = notice_source / "PSF-3.7.0.txt"
    path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    result = notices.collect_notices(tmp_path)
    assert "shiboken6" in {canonicalize_name(item["name"]) for item in result["components"]}


def test_supplement_does_not_hide_a_missing_declared_wheel_license(
    tmp_path, installed_distribution, linux_pyside_distributions,
):
    installed = linux_pyside_distributions["shiboken6"]
    linux_pyside_distributions["shiboken6"] = installed_distribution(
        installed.metadata["Name"], version=installed.version,
        expression=installed.metadata["License-Expression"], requires=installed.requires or [],
        files={"licenses/LICENSE": None},
    )
    with pytest.raises(RuntimeError, match="Installed redistribution notice is missing"):
        notices.collect_notices(tmp_path)


def test_qt_upgrade_requires_refreshed_notices(tmp_path, monkeypatch):
    from PySide6 import QtCore

    monkeypatch.setattr(QtCore, "qVersion", lambda: "99.0.0")
    with pytest.raises(RuntimeError, match="review the updated notices"):
        notices.collect_notices(tmp_path)


def test_notice_collection_records_runtime_licenses_and_source_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "iamokish/GrihaLekha")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    result = notices.collect_notices(tmp_path)
    assert result["application_license"] == "AGPL-3.0-or-later"
    names = {item["name"].lower() for item in result["components"]}
    assert {"pyside6", "sqlalchemy", "nuitka"} <= names
    assert not {"pytest", "pymupdf", "onnxruntime", "numpy"} & names
    for item in result["components"]:
        assert item["notices"]
        assert all((tmp_path / path).is_file() for path in item["notices"])
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (tmp_path / "LICENSE").read_text(encoding="utf-8")
    assert (tmp_path / result["qt_attributions"]).stat().st_size > 10000
    snapshot = json.loads((tmp_path / "licenses/qt/qt-attributions.json").read_text(encoding="utf-8"))
    assert any("qtcore-attribution" in page["url"] for page in snapshot["pages"])
    assert any("qtwebengine-3rdparty-chromium-global" in page["url"] for page in snapshot["pages"])
    assert (tmp_path / "licenses/python/LICENSE.txt").is_file()
    source = (tmp_path / "SOURCE_CODE.md").read_text(encoding="utf-8")
    assert f"https://github.com/iamokish/GrihaLekha/archive/{'a' * 40}.tar.gz" in source
    assert "AGPL-3.0-or-later" in source
    assert json.loads((tmp_path / "THIRD_PARTY_COMPONENTS.json").read_text(encoding="utf-8")) == result


def test_local_source_notice_links_project_without_inventing_a_revision(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    source = notices._source_document([], "6.12.0", "6.12.0")
    assert "https://github.com/iamokish/GrihaLekha" in source
    assert "This is a local build" in source
    assert "Revision:" not in source and "/archive/" not in source
    assert "AGPL-3.0-or-later" in source
