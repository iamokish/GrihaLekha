"""Refresh the reviewed Qt attribution snapshot after a Qt dependency update.

This maintenance command downloads the official Qt documentation's original
third-party notices and PySide's matching source license files. Review the
resulting changes before committing them. Normal application builds only read
the committed snapshots and require no network.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html import escape
from importlib.metadata import distribution, version
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

from PySide6.QtCore import qVersion
from PySide6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineVersion


DESTINATION = Path(__file__).resolve().parent / "licenses"
PYSIDE_COMPONENTS = (
    "pyside6", "pyside6-addons", "pyside6-essentials", "pyside6-pdf",
    "pyside6-webengine", "shiboken6",
)
PYSIDE_LICENSE = "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
PYSIDE_NOTICE_FILES = {
    "GPL-2.0-only.txt": "LICENSES/GPL-2.0-only.txt",
    "GPL-3.0-only.txt": "LICENSES/GPL-3.0-only.txt",
    "LGPL-3.0-only.txt": "LICENSES/LGPL-3.0-only.txt",
    "pyside6-licensecomment.txt": "sources/pyside6/PySide6/licensecomment.txt",
    "PSF-3.7.0.txt": "sources/shiboken6/shibokenmodule/files.dir/shibokensupport/signature/PSF-3.7.0.txt",
    "shiboken6-libshiboken-attribution.json": "sources/shiboken6/libshiboken/qt_attribution.json",
    "shiboken6-signature-attribution.json": "sources/shiboken6/shibokenmodule/files.dir/shibokensupport/signature/qt_attribution.json",
}


def download_pyside_source(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not (
        parsed.netloc == "raw.githubusercontent.com" and parsed.path.startswith("/pyside/pyside-setup/")
        or parsed.netloc == "api.github.com" and parsed.path.startswith("/repos/pyside/pyside-setup/commits/")
    ):
        raise ValueError(f"Unexpected PySide source URL: {url}")
    try:
        with urlopen(url, timeout=45) as response:
            return response.read()
    except OSError as error:
        raise RuntimeError(f"Could not download {url}: {error}") from error


def pyside_notices() -> tuple[dict, dict[str, str]]:
    """Capture exact PySide wheel versions and their upstream source notices."""
    components = {}
    for name in PYSIDE_COMPONENTS:
        installed = distribution(name)
        license_expression = installed.metadata.get("License-Expression") or installed.metadata.get("License")
        if license_expression != PYSIDE_LICENSE:
            raise ValueError(f"Review the changed license for {name}: {license_expression!r}")
        components[name] = installed.version
    pyside_version = components["pyside6"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", pyside_version):
        raise ValueError(f"Review the PySide source tag for version {pyside_version!r}.")
    if any(value.split(".")[:3] != pyside_version.split(".") for value in components.values()):
        raise ValueError("PySide and Shiboken component versions do not share one source release.")
    tag = f"v{pyside_version}"
    source = json.loads(download_pyside_source(
        f"https://api.github.com/repos/pyside/pyside-setup/commits/{tag}"
    ))
    revision = source.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Could not resolve the PySide source revision for {tag}.")
    files = {}
    records = []
    for filename, path in PYSIDE_NOTICE_FILES.items():
        url = f"https://raw.githubusercontent.com/pyside/pyside-setup/{revision}/{path}"
        text = download_pyside_source(url).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ValueError(f"Empty PySide redistribution notice: {url}")
        files[filename] = text
        records.append({"path": filename, "url": url, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return {
        "components": components,
        "license_expression": PYSIDE_LICENSE,
        "source_revision": revision,
        "files": records,
    }, files


def download(url: str) -> bytes:
    if urlsplit(url).netloc != "doc.qt.io":
        raise ValueError(f"Unexpected Qt documentation host: {url}")
    try:
        with urlopen(url, timeout=45) as response:
            return response.read()
    except OSError as error:
        raise RuntimeError(f"Could not download {url}: {error}") from error


def attribution(url: str) -> tuple[str, dict]:
    data = download(url)
    source = data.decode("utf-8")
    title = re.search(r'<h1 class="title">(.*?)</h1>', source, re.DOTALL)
    description = re.search(
        r'<div class="descr" id="details">(.*?)</div>\s*<!-- @@@', source, re.DOTALL,
    )
    if not title or not description:
        raise ValueError(f"Could not find the complete attribution in {url}")
    body = description.group(1)
    if re.search(r"<script\b|<iframe\b|\bon[a-z]+\s*=", body, re.IGNORECASE):
        raise ValueError(f"Unexpected executable HTML in attribution: {url}")
    body = re.sub(r'href="([^"]+)"', lambda match: f'href="{escape(urljoin(url, match.group(1)), quote=True)}"', body)
    section = f'<section><h2>{title.group(1)}</h2><p>Source: <a href="{escape(url)}">{escape(url)}</a></p>{body}</section>'
    return section, {"url": url, "sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    qt_version = qVersion()
    series = ".".join(qt_version.split(".")[:2])
    index_url = f"https://doc.qt.io/qt-{series}/licenses-used-in-qt.html"
    index = download(index_url)
    links = {
        urljoin(index_url, link)
        for link in re.findall(r'href="([^"]*(?:3rdparty|attribution)[^"]*\.html)"', index.decode("utf-8"))
        if not link.startswith(("../qtwebengine/", "../qtpdf/"))
    }
    indexes = [{"url": index_url, "sha256": hashlib.sha256(index).hexdigest()}]
    # Qt WebEngine and PDF are separately versioned modules. Follow their own
    # complete attribution indexes; the Qt umbrella index can contain stale URLs.
    for module in ("qtwebengine", "qtpdf"):
        url = f"https://doc.qt.io/{module}/{module}-licensing.html"
        data = download(url)
        text = data.decode("utf-8")
        title = re.search(r"<title>(.*?)</title>", text)
        if not title or qWebEngineVersion() not in title.group(1):
            raise ValueError(f"The published {module} documentation does not match WebEngine {qWebEngineVersion()}.")
        links.update(urljoin(url, link) for link in re.findall(r'href="([^"]*3rdparty[^"]*\.html)"', text))
        indexes.append({"url": url, "sha256": hashlib.sha256(data).hexdigest()})
    links = sorted(links)
    if len(links) < 20:
        raise ValueError("Qt's attribution index did not contain the expected third-party notices.")
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(attribution, links))
    snapshot = {
        "qt_version": qt_version,
        "pyside6_version": version("PySide6"),
        "qt_webengine_version": qWebEngineVersion(),
        "chromium_version": qWebEngineChromiumVersion(),
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "indexes": indexes,
        "pages": [item[1] for item in pages],
    }
    heading = f"Qt {escape(qt_version)} third-party notices"
    text = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><title>{heading}</title>
<style>body{{max-width:90ch;margin:2em auto;padding:0 1em;font-family:sans-serif}}pre{{white-space:pre-wrap}}section{{border-top:1px solid #888;margin-top:2em}}</style>
<h1>{heading}</h1>
<p>This snapshot retains the third-party attribution sections published by The Qt Company.
Source pages and their SHA256 hashes are recorded in qt-attributions.json.
It includes notices for Qt modules beyond those used by GrihaLekha; inclusion here does
not imply a component is bundled. Review the build inventory to identify shipped components.</p>
<p>Qt documentation excerpts: Copyright 2026 The Qt Company Ltd. and their respective
contributors, under the GNU Free Documentation License version 1.3, with no Invariant
Sections, no Front-Cover Texts, and no Back-Cover Texts. See
<a href="GFDL-1.3-no-invariants-only.txt">the included GFDL text</a>. Original third-party
license texts retain their own terms. The compilation and source links are the only
editorial changes to the attribution sections.</p>
'''
    text += "\n".join(page[0] for page in pages) + "\n</html>\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    snapshot["notice_file_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pyside_snapshot, pyside_files = pyside_notices()
    DESTINATION.mkdir(exist_ok=True)
    for filename, content in pyside_files.items():
        (DESTINATION / filename).write_text(content, encoding="utf-8", newline="\n")
    (DESTINATION / "pyside6-notices.json").write_text(
        json.dumps(pyside_snapshot, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    (DESTINATION / "qt-third-party-notices.html").write_text(text, encoding="utf-8", newline="\n")
    (DESTINATION / "qt-attributions.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(pages)} official Qt attribution pages and {len(pyside_files)} PySide source notices; review the snapshots before committing.")


if __name__ == "__main__":
    main()
