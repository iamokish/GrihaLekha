"""Check wheel/sdist contents against this checkout without installing either."""

from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LICENSE_EXPRESSION = "AGPL-3.0-or-later"
PROJECT_FILES = (
    "pyproject.toml", "uv.lock", "setup.py", "MANIFEST.in", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "README.md", "USER_GUIDE.md", "CONTRIBUTING.md",
    "SECURITY.md", "CHANGELOG.md", ".gitignore",
)
FORBIDDEN_PARTS = {
    ".git", ".venv", "venv", "build", "dist", ".audit", ".cache",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__", ".aws",
    ".codex", ".agents", ".data", "data", "backups", "attachments",
    "documents", "proofs", "migration-backups", "logs",
}
FORBIDDEN_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib", ".exe", ".msi",
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm",
    ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log", ".zip", ".tar.gz",
    ".tar.zst", ".whl", ".deb", ".rpm", ".pem", ".key",
)


def project_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    tree = ast.parse((root / "src/grihalekha/__init__.py").read_text(encoding="utf-8"))
    runtime = [ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "__version__"
                       for target in node.targets)]
    if runtime != [version]:
        raise ValueError("The project and application must declare the same version.")
    if project.get("license") != LICENSE_EXPRESSION:
        raise ValueError(f"Project license must be {LICENSE_EXPRESSION}.")
    return version


def required_payloads(root: Path, version: str) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Map published paths to original bytes, including installed icon copies."""
    package = root / "src/grihalekha"
    application = [path for path in package.rglob("*") if path.is_file()
                   and "__pycache__" not in path.parts
                   and (path.suffix == ".py" or ("assets" in path.relative_to(package).parts
                        and path.suffix in {".ttf", ".txt", ".md", ".svg", ".ico", ".png"}))]
    wheel = {path.relative_to(root / "src").as_posix(): path.read_bytes() for path in application}
    source = {path.relative_to(root).as_posix(): path.read_bytes() for path in application}
    for name in ("grihalekha.ico", "grihalekha-icon-512.png"):
        content = (root / "icons" / name).read_bytes()
        wheel[f"grihalekha/assets/icons/{name}"] = content
        source[f"icons/{name}"] = content
    for name in PROJECT_FILES:
        source[name] = (root / name).read_bytes()
    for directory, suffixes in (
        ("docs", {".md", ".png"}),
        ("packaging", {".py", ".md", ".iss", ".desktop", ".in", ".txt", ".json", ".html"}),
        ("scripts", {".py", ".sh"}),
        ("tests", {".py"}),
        (".github", {".yml", ".yaml", ".md"}),
    ):
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                source[path.relative_to(root).as_posix()] = path.read_bytes()
    license_names = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    license_names.extend(path.relative_to(root).as_posix()
                         for path in sorted((package / "assets/fonts").glob("OFL-*.txt")))
    for name in license_names:
        wheel[f"grihalekha-{version}.dist-info/licenses/{name}"] = (root / name).read_bytes()
    return wheel, source


def _check_name(name: str) -> str:
    normalized = name.rstrip("/")
    parts = normalized.split("/")
    if (not normalized or "\\" in name or ":" in name or "\0" in name
            or PurePosixPath(normalized).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)):
        raise ValueError(f"Unsafe archive path: {name!r}")
    lowered = [part.casefold() for part in parts]
    if (any(part in FORBIDDEN_PARTS or part.startswith("recovery-") for part in lowered)
            or lowered[-1].endswith(FORBIDDEN_SUFFIXES)
            or lowered[-1] in {"application.lock", "restore-state.json", ".coverage", ".env"}
            or lowered[-1].startswith(".env.") and lowered[-1] != ".env.example"):
        raise ValueError(f"Private data or generated output in archive: {name}")
    return normalized


def _zip_contents(path: Path) -> dict[str, bytes]:
    files = {}
    seen = set()
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = _check_name(item.filename)
            if name.casefold() in seen:
                raise ValueError(f"Duplicate archive path: {name}")
            seen.add(name.casefold())
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"Unsupported archive link or special file: {name}")
            if not item.is_dir():
                files[name] = archive.read(item)
    return files


def _tar_contents(path: Path, version: str) -> dict[str, bytes]:
    files = {}
    seen = set()
    prefix = f"grihalekha-{version}"
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            name = _check_name(item.name)
            if name.casefold() in seen:
                raise ValueError(f"Duplicate archive path: {name}")
            seen.add(name.casefold())
            if name != prefix and not name.startswith(prefix + "/"):
                raise ValueError(f"Source archive must have one {prefix}/ root: {name}")
            if item.isdir():
                continue
            if not item.isfile():
                raise ValueError(f"Unsupported archive link or special file: {name}")
            relative = name[len(prefix) + 1:]
            with archive.extractfile(item) as stream:
                files[relative] = stream.read()
    return files


def _require_contents(actual: dict[str, bytes], expected: dict[str, bytes], kind: str) -> None:
    missing = expected.keys() - actual.keys()
    if missing:
        raise ValueError(f"{kind} is missing required files: {', '.join(sorted(missing))}")
    changed = [name for name, content in expected.items() if actual[name] != content]
    if changed:
        raise ValueError(f"{kind} contains stale or changed files: {', '.join(sorted(changed))}")


def _check_metadata(data: bytes, version: str, kind: str) -> None:
    metadata = BytesParser().parsebytes(data)
    for field, expected in (("Name", "grihalekha"), ("Version", version),
                            ("License-Expression", LICENSE_EXPRESSION)):
        if metadata.get_all(field) != [expected]:
            raise ValueError(f"{kind} metadata {field} must be {expected!r}.")
    licenses = set(metadata.get_all("License-File", []))
    if not {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= licenses:
        raise ValueError(f"{kind} metadata must declare the project and third-party license files.")


def check_distribution(directory: Path, root: Path = ROOT) -> dict[str, str | int]:
    version = project_version(root)
    wheels = sorted(directory.glob("*.whl"))
    sources = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or not wheels[0].name.startswith(f"grihalekha-{version}-"):
        raise ValueError(f"Expected exactly one GrihaLekha {version} wheel in {directory}.")
    if len(sources) != 1 or sources[0].name != f"grihalekha-{version}.tar.gz":
        raise ValueError(f"Expected exactly one grihalekha-{version}.tar.gz in {directory}.")
    expected_wheel, expected_source = required_payloads(root, version)
    wheel = _zip_contents(wheels[0])
    source = _tar_contents(sources[0], version)
    _require_contents(wheel, expected_wheel, "Wheel")
    _require_contents(source, expected_source, "Source archive")
    for contents, expected, prefixes, kind in (
        (wheel, expected_wheel, ("grihalekha/",), "Wheel"),
        (source, expected_source, ("src/grihalekha/", "docs/", "scripts/", "packaging/", "tests/", ".github/"), "Source archive"),
    ):
        obsolete = sorted(name for name in contents
                          if name.startswith(prefixes) and name not in expected)
        if obsolete:
            raise ValueError(f"{kind} contains files outside the current source: {', '.join(obsolete)}")
    dist_info = f"grihalekha-{version}.dist-info"
    if any(name.split("/")[0] not in {"grihalekha", dist_info} for name in wheel):
        raise ValueError("Wheel contains unexpected top-level files or packages.")
    for files, metadata_path, kind in (
        (wheel, f"{dist_info}/METADATA", "Wheel"),
        (source, "PKG-INFO", "Source archive"),
    ):
        if metadata_path not in files:
            raise ValueError(f"{kind} is missing {metadata_path}.")
        _check_metadata(files[metadata_path], version, kind)
    return {"version": version, "wheel": wheels[0].name, "sdist": sources[0].name,
            "wheel_files": len(wheel), "sdist_files": len(source)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Folder containing one wheel and one source .tar.gz")
    args = parser.parse_args(argv)
    try:
        result = check_distribution(args.directory)
    except (OSError, ValueError, KeyError, SyntaxError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Distribution check failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified {result['wheel']} ({result['wheel_files']} files) and "
          f"{result['sdist']} ({result['sdist_files']} files), {LICENSE_EXPRESSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
