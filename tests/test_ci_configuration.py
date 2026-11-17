"""Linux setup files must survive Windows edits and Git checkouts."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELL_FILES = [*sorted((ROOT / "scripts").glob("*.sh")), ROOT / "packaging/PKGBUILD.in"]
BASH = shutil.which("bash") if os.name != "nt" else None
if os.name == "nt" and (git := shutil.which("git")):
    # Avoid Windows' WSL launcher when a Linux distribution is not installed.
    git_directory = Path(git).resolve().parent
    BASH = next((str(path) for path in (
        git_directory / "bash.exe",
        git_directory.parent / "bin/bash.exe",
        git_directory.parent / "usr/bin/bash.exe",
    ) if path.is_file()), None)


@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda path: path.name)
def test_linux_setup_files_use_lf(path):
    content = path.read_bytes()
    assert b"\r" not in content, f"{path.name}: CRLF breaks Bash options and continuations"
    assert content.endswith(b"\n")


@pytest.mark.skipif(BASH is None, reason="Bash is required for shell syntax checks")
@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda path: path.name)
def test_linux_setup_files_have_valid_bash_syntax(path):
    result = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
