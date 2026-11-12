# Third-party notices

GrihaLekha's original application code is licensed under the GNU Affero General
Public License, version 3 or any later version (`AGPL-3.0-or-later`).
See [LICENSE](LICENSE).
Third-party software and artwork retain their own licenses; GrihaLekha's license
does not relicense them.

| Component | License / notice |
| --- | --- |
| CPython and its standard library | Python Software Foundation license and the additional notices in Python's `LICENSE.txt` |
| PySide6 and Shiboken | LGPLv3 / GPL alternatives as specified by each upstream component |
| Qt libraries | Module-specific LGPL/GPL terms and third-party notices; see [Qt licensing](https://doc.qt.io/qt-6/licensing.html) |
| Qt WebEngine / Chromium | Qt terms plus bundled Chromium and third-party notices; see [Qt WebEngine licensing](https://doc.qt.io/qt-6/qtwebengine-licensing.html) |
| SQLAlchemy | MIT; retain its `LICENSE` and `AUTHORS` |
| typing_extensions | PSF-2.0 |
| Noto Sans and Noto Sans Devanagari | SIL Open Font License 1.1; full copyright/license notices accompany the four bundled fonts |
| Nuitka compiled runtime | The installed compiler's license and runtime exception; Nuitka 4.3.4 supplies AGPLv3 with the Nuitka Runtime Library Exception 1.0 |

Font source URLs, checksums, and the process used to produce the static font
instances are recorded in
[`src/grihalekha/assets/fonts/README.md`](https://github.com/iamokish/GrihaLekha/blob/main/src/grihalekha/assets/fonts/README.md).
The fonts' licenses are distributed beside the fonts in source and binary builds.

## Notices in standalone distributions

The build copies the installed runtime packages' original license/notice files
and metadata to `licenses/`, together with the build Python's license, Nuitka's
runtime license/exception, and Qt's GPLv3/LGPLv3 license texts. A reviewed snapshot
of Qt's third-party attributions, including WebEngine/Chromium and PDF notices,
is supplied as `licenses/qt/qt-third-party-notices.html`. Its source URLs and
SHA256 hashes are recorded in `licenses/qt/qt-attributions.json`; the associated
Qt documentation excerpts retain their GFDLv1.3 terms, included beside them.
PySide6's Linux wheels omit separate license files. The build supplements all
six PySide6/Shiboken packages with matching upstream source notices, including
their open-source license texts and Shiboken's incorporated Python notices.
`licenses/qt/pyside6-notices.json` records their exact versions, license
expression, source revision, and notice checksums. Installed wheel notices are
retained on every platform. Builds reject a version, license, or checksum
mismatch until the affected snapshot is updated and reviewed. Notice collection
during a build requires no network.

`THIRD_PARTY_COMPONENTS.json` records actual package, Qt, and Chromium versions.
`SOURCE_CODE.md` records the application's exact GitHub source revision for CI
builds and upstream source locations. `build-info.json` records source hashes
and bundle verification results.

PyMuPDF, pytest, and the other development tools are not application runtime
dependencies. They are used for testing and build verification and are excluded
from the application bundle.

## Preparing or redistributing a release

Retain these notices and the fonts' OFL files. Keep the standalone directory
intact, including replaceable Qt shared libraries. Do not treat a wheel's
commercial-license notice as a commercial license grant; use the applicable
open-source terms for the components being distributed.

Review the exact Qt modules' [third-party attributions](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
and native libraries in the Nuitka compilation report, including any copied
compiler/operating-system runtime libraries. The generated Python inventory and
Qt notice snapshot do not by themselves prove that every native library notice or
source obligation has been satisfied.

Make the complete corresponding source for the exact distributed binaries
available as required by their licenses, including applicable dependencies,
patches, and build/installation scripts. Check source availability before
publishing and preserve or mirror sources when needed. The release's matching
application source archive does not itself contain Qt or Chromium source.
