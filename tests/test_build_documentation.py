"""Standalone packages retain usable local documentation and release notes."""

import importlib.util
from pathlib import Path
import re

import pytest


SPEC = importlib.util.spec_from_file_location(
    "grihalekha_builder", Path(__file__).resolve().parents[1] / "packaging/build.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_staged_readme_images_and_release_notes_match_the_source(tmp_path):
    staged = tmp_path / "bundle"
    builder._copy_documentation(staged)
    readme = (staged / "README.md").read_text(encoding="utf-8")
    references = re.findall(r'!\[[^\]]*\]\(([^)]+)\)|<img\b[^>]*\bsrc="([^"]+)"', readme)
    local_images = [markdown or html for markdown, html in references
                    if not (markdown or html).startswith(("https://", "http://"))]
    assert local_images
    for relative in (*builder.DOCUMENT_NAMES, *local_images):
        assert (staged / relative).read_bytes() == (builder.ROOT / relative).read_bytes()
    for relative in re.findall(r'\]\(([^\s)]+)\)', readme):
        if not relative.startswith(("https://", "http://", "#")):
            assert (staged / relative.split("#", 1)[0]).exists(), relative
    assert (staged / "CHANGELOG.md").is_file()


def test_missing_documentation_asset_stops_staging(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in builder.DOCUMENT_NAMES:
        (source / name).write_text("Sample document", encoding="utf-8")
    monkeypatch.setattr(builder, "ROOT", source)
    with pytest.raises(FileNotFoundError):
        builder._copy_documentation(tmp_path / "bundle")
