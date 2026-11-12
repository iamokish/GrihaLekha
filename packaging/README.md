# Build and package GrihaLekha

[`build.py`](build.py) compiles GrihaLekha into a standalone application directory
using Nuitka. The folder contains Python, Qt, WebEngine PDF support, fonts,
artwork, and license notices. Users do not need Python installed. Build scripts
use a separate workspace from the application's account folder; account data
is never a build input.

This guide covers the build tools for
[GrihaLekha 0.1.0](https://github.com/iamokish/GrihaLekha).
For tags and GitHub releases, see the [release checklist](../docs/RELEASING.md).

## Targets and prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
Release builds use **64-bit Python 3.12** and the checked-in `uv.lock`. The
application supports Python 3.12–3.14; release workflows use 3.12.

| Target | Build environment | Output |
| --- | --- | --- |
| Windows 11 x64 | Windows x64, Visual Studio 2022 C++ tools/Windows SDK, Inno Setup 6 | Portable ZIP and per-user installer EXE |
| Debian 12+ / Ubuntu 22.04+ x64 | Ubuntu 22.04 x64, C/C++ toolchain, patchelf, dpkg-deb | Portable tar.gz and .deb |
| Arch Linux x64 | Current Arch with base-devel/makepkg | .pkg.tar.zst assembled from the verified Linux portable bundle |

Ubuntu 22.04 supplies the release's glibc 2.35 baseline. A bundle compiled on a
newer Linux distribution may require newer system libraries; do not substitute
a rolling distribution as the generic portable build host. Arch packaging reuses
the Ubuntu-built bundle and verifies it again on current Arch before publication.
These are workflow targets; successful CI and manual checks for each tag establish
that release's actual validation status. No ARM or macOS binaries are produced.

### Linux

On Ubuntu 22.04 build machines, install the compiler and Qt system dependencies:

```sh
sudo bash scripts/install_linux_ci_dependencies.sh
```

The `.deb` declares its required system libraries in `release.py` and `apt`
installs them. Arch dependencies are in `PKGBUILD.in` and installed by `pacman`.
Portable Linux users need the same runtime libraries: glibc, libstdc++, GLib,
D-Bus, Fontconfig/FreeType, NSS/NSPR, ALSA, CUPS, OpenGL/EGL, X11/XCB,
libxkbcommon, and Wayland client libraries. A supported desktop/display session
is required for normal interactive use. Running Chromium as root is unsupported;
the workflows run the app and makepkg as an unprivileged user.

### Windows

Install Visual Studio 2022 Build Tools with **Desktop development with C++** and
a Windows SDK. Run from an x64 Visual Studio developer terminal if MSVC is not
detected automatically. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
to create the EXE installer; compiling the standalone folder does not need it.
`release.py` searches its standard installation locations and `PATH`.

The installer runs without administrator privileges and installs under the
user's Programs folder. The application runs on Windows 11; CI uses a Windows
Server runner.

## Get the source

```sh
git clone https://github.com/iamokish/GrihaLekha.git
cd GrihaLekha
uv sync --locked --extra dev --python 3.12
```

Run the commands below from the repository root. Build scripts resolve paths
from their own locations and can also be called by absolute path from another
directory using the project's Python environment.

## Compile the application

From the repository root on the target build machine:

```sh
uv run --frozen python packaging/build.py --dry-run
uv run --frozen python packaging/build.py --jobs 2
```

The final bundle is `dist/GrihaLekha`. It includes the README, user guide,
changelog, license notices, and the README's local screenshots and logo.
Compiler work and verification evidence are
under `build/nuitka`; Nuitka helper/compiler caches are under `.cache/nuitka`.
The build stages a new bundle and verifies it before replacing the previous
output. Builds are serialized with `build/nuitka/build.lock`. After a hard
interruption, check that no build is running before removing a stale lock.

Open `dist/GrihaLekha/GrihaLekha.exe` on Windows or
`dist/GrihaLekha/GrihaLekha` on Linux. Keep the complete folder together. For a
manual test, pass `--data-dir` with a separate sample account folder.

### build.py options

| Option | Values and behavior |
| --- | --- |
| `--jobs N` | Parallel compiler jobs, from 1 to 64. Defaults to the CPU count, capped at 4. |
| `--compiler auto`, `--compiler msvc`, `--compiler mingw64` | Defaults to `msvc` on Windows and `auto` on Linux. Explicit compiler selection is Windows-only; MinGW requires Python 3.12. |
| `--lto auto`, `--lto yes`, `--lto no` | Link-time optimization policy; defaults to `auto`. |
| `--dry-run` | Validate the configuration and print the Nuitka command, output directory, and cache path without compiling. |
| `--help` | Show the command-line reference. |

For example, a Windows build with fewer jobs and no link-time optimization is:

```powershell
uv run --frozen python packaging/build.py --compiler msvc --jobs 2 --lto no
```

The first build may download Nuitka helper tools. Dependency locks do not make
compiler and operating-system outputs byte-for-byte reproducible.

## Create release packages

Package the verified directory:

```powershell
# Windows: both ZIP and installer
uv run --frozen python packaging/release.py --platform windows --tag v0.1.0
```

```sh
# Ubuntu: both tar.gz and .deb
uv run --frozen python packaging/release.py --platform linux --tag v0.1.0

# Arch, as an unprivileged user: place the extracted portable GrihaLekha/
# directory under dist/, then verify it and build the native package.
uv run --frozen python packaging/verify_build.py dist/GrihaLekha --output-dir build/arch-verification --require-chromium
uv run --frozen python packaging/release.py --arch-package --tag v0.1.0
```

`release.py` checks the version, actual PE/ELF x64 executable header, matching
application source hashes, required documents, and successful compiled verification.
It also checks the source commit in CI. Rebuild old local bundles before packaging.
The Windows installer uses the version passed from project metadata and a stable
AppId for upgrades. Linux packages install under `/opt/grihalekha`, with a
`grihalekha` launcher, desktop entry, and icon. Uninstall leaves account data intact.

The final filenames for version `0.1.0` are:

```text
GrihaLekha-0.1.0-windows-x64-portable.zip
GrihaLekha-0.1.0-windows-x64-setup.exe
GrihaLekha-0.1.0-linux-x64-portable.tar.gz
grihalekha_0.1.0_amd64.deb
grihalekha-0.1.0-1-x86_64.pkg.tar.zst
SHA256SUMS.txt
```

Outputs go to `dist/release`. After collecting exactly the five platform packages
in one directory, generate the sixth file:

```sh
uv run --frozen python packaging/release.py --checksums dist/release --tag v0.1.0
```

Unexpected, missing, or empty artifacts fail this step. `release.py` owns all
portable archives and installer packages; Linux archives preserve executable
modes and symlinks. For later releases, use the corresponding version and tag.

The [release workflow](../.github/workflows/release.yml) runs native builds and
installation checks, assembles all six files, and creates a draft release with
notes from [CHANGELOG.md](../CHANGELOG.md).

## Verification and tests

Every full build runs `verify_build.py` against the staged executable using
isolated synthetic accounts, a different working directory, and a restricted
environment. It checks empty startup, bundled icons/fonts/WebEngine resources,
populated-account PDFs, exact money, font embedding, payment proofs, and complete
backups. It exercises both the default renderer and explicit native fallback.

Run it separately with:

```sh
uv run --frozen python packaging/verify_build.py dist/GrihaLekha --output-dir build/verification --require-chromium
```

`--require-chromium` fails when default PDF generation falls back to native Qt.
Local build verification records fallback as a limitation, but `release.py`
always requires successful Chromium execution. The workflow rechecks extracted
portable archives and installed packages. Windows installer lifecycle checks
are restricted to a fresh GitHub Actions runner to protect existing installations.
Linux/Arch jobs install and remove packages in disposable runners and verify
that separate synthetic account databases survive uninstall.

Run the regression suite before packaging. On Windows PowerShell:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:GRIHALEKHA_PDF_RENDERER = 'native'
uv run --frozen pytest
Remove-Item Env:GRIHALEKHA_PDF_RENDERER
```

On Linux:

```sh
TZ=Asia/Kolkata QT_QPA_PLATFORM=offscreen GRIHALEKHA_PDF_RENDERER=native uv run --frozen pytest
```

Use the default PDF renderer for release verification, without forcing
`GRIHALEKHA_PDF_RENDERER=native`. The native renderer is selected above only for
headless regression tests.

Python source and wheel packages are separate from the standalone application:

```sh
uv build --out-dir dist/python
uv run --frozen python scripts/check_distribution.py dist/python
```

Logs, screenshots, PDFs, and JSON reports remain under each verification output
directory and are uploaded as workflow diagnostics. No real account folder is
read. Automated checks do not replace manual desktop, display-scaling, upgrade,
and X11/Wayland checks in the [release checklist](../docs/RELEASING.md).

## Assets, licenses, and source

Original GrihaLekha code and artwork use **AGPL-3.0-or-later**. Dependency and font
licenses remain separate; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Canonical artwork lives in `icons/`. `setup.py` copies it into installed Python
packages; Nuitka includes it under `grihalekha/assets/icons`. Fonts and their OFL
notices live in `src/grihalekha/assets/fonts`. Keep all four fonts and both notices.

`license_notices.py` copies the application's AGPLv3 license, installed runtime
dependency notices, Python's license, and reviewed Qt attributions. It generates
`THIRD_PARTY_COMPONENTS.json` and `SOURCE_CODE.md`. Reviewed upstream source
notices supplement PySide6/Shiboken wheels that omit license files. The Qt and
PySide snapshots are checked against installed versions and notice checksums;
changes require a reviewed refresh using `update_qt_notices.py`.
See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for source availability
and redistribution obligations.

The application source archive includes build scripts, tests, icons, notices,
documentation, workflows, and `uv.lock`. `scripts/check_distribution.py` checks
the wheel and source archive for required content and accidental private/generated
files. Source archives are available from GitHub alongside the six release assets.

See [docs/RELEASING.md](../docs/RELEASING.md) for versioning, tags, draft review,
and repository settings.

## Troubleshooting

| Problem | Action |
| --- | --- |
| `build/nuitka/build.lock` already exists | Check that no build is running. If a previous process was interrupted, remove only the stale lock before retrying. |
| Compiler not found | Use an x64 Visual Studio developer shell on Windows or install the Ubuntu build dependencies. |
| Compilation or linking runs out of memory | Lower `--jobs` and try `--lto no`. |
| Version mismatch | Update `pyproject.toml` and `src/grihalekha/__init__.py` together, then run `uv lock`. |
| Inno Setup not found | Install Inno Setup 6 and make `ISCC.exe` discoverable. |
| Stale bundle or source-hash mismatch | Run `build.py` again after source changes before packaging. |
| PDF verification uses the native fallback | Inspect the verification logs, install the required desktop libraries, and run as a non-root user with Chromium's sandbox enabled. |
| Qt attribution version mismatch | Review the dependency change and update the notices using `update_qt_notices.py`; see [licenses/README.md](licenses/README.md). |
| PySide notice snapshot mismatch or missing file | Restore the committed notices, or review the dependency change and refresh the snapshot with `update_qt_notices.py`; see [licenses/README.md](licenses/README.md). |

Compiler diagnostics are in `build/nuitka/compilation-report.xml`; runtime logs,
screenshots, and sample PDFs are under `build/nuitka/verification/`.
`dist/GrihaLekha/build-info.json` records dependency versions, source hashes,
and the verification result for the completed bundle. Temporary installer
staging uses `build/release/`.
