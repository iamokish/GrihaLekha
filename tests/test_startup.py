"""Exercise the actual entry point in its own Qt application process."""
import os
from pathlib import Path
import subprocess
import sys


def test_smoke_launch_creates_separate_empty_account_folder(tmp_path):
    data = tmp_path / "empty-account"
    screenshot = tmp_path / "startup.png"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", GRIHALEKHA_PDF_RENDERER="native")
    result = subprocess.run([sys.executable, "-m", "grihalekha", "--smoke-test", "--data-dir", str(data), "--screenshot", str(screenshot)], capture_output=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (data / "grihalekha.sqlite3").is_file()
    assert screenshot.is_file()
    assert not (data / "application.lock").exists()


def test_corrupt_database_is_never_silently_replaced_on_startup(tmp_path):
    data = tmp_path / "damaged-account"
    data.mkdir()
    database = data / "grihalekha.sqlite3"
    original = b"Existing damaged database; must remain available for recovery."
    database.write_bytes(original)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", GRIHALEKHA_PDF_RENDERER="native")
    result = subprocess.run([sys.executable, "-m", "grihalekha", "--smoke-test", "--data-dir", str(data)], capture_output=True, timeout=30, env=env)
    assert result.returncode == 1
    assert database.read_bytes() == original
    assert not (data / "application.lock").exists()
