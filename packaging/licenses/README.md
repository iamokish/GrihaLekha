# Qt and PySide redistribution notices

These are verbatim license documents from the Qt project's `qtbase` source at
tag `v6.12.0`. They supplement the notices supplied by the Python wheels.

- `GPL-3.0-only.txt`: https://github.com/qt/qtbase/blob/v6.12.0/LICENSES/GPL-3.0-only.txt
- `LGPL-3.0-only.txt`: https://github.com/qt/qtbase/blob/v6.12.0/LICENSES/LGPL-3.0-only.txt
- `GFDL-1.3-no-invariants-only.txt`: https://github.com/qt/qtbase/blob/v6.12.0/LICENSES/GFDL-1.3-no-invariants-only.txt

Qt and third-party components retain their own licenses. These license documents
alone are not the complete third-party notices for Qt.

## PySide and Shiboken source notices

The PySide6 and Shiboken 6.12.0 Linux wheels contain no separate license files;
their Windows wheels contain only the commercial-license notice. The installed
metadata on both platforms declares `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`.
`pyside6-notices.json` records the six reviewed wheel versions, their license
expression, the matching `pyside/pyside-setup` source revision, and the source URL
and SHA-256 checksum for every supplemental notice. Checksums use UTF-8 text with
LF line endings so a checkout with CRLF line endings has the same content hash.

The supplements are copied from upstream tag `v6.12.0`:

- `GPL-2.0-only.txt`, `GPL-3.0-only.txt`, and `LGPL-3.0-only.txt` retain the three
  open-source license alternatives. The GPLv3 and LGPLv3 texts are identical to
  the copies from `qtbase` listed above.
- `pyside6-licensecomment.txt` retains the original generated binding copyright
  and license notice. PySide and Shiboken runtime source headers also credit
  The Qt Company Ltd. for the years 2016 through 2026 under the same license
  alternatives.
- `PSF-3.7.0.txt` retains the Python Software Foundation's license and copyright
  for the Python 3.7 code incorporated in Shiboken.
- `shiboken6-libshiboken-attribution.json` and
  `shiboken6-signature-attribution.json` preserve upstream's descriptions of that
  incorporated Python code. Their `LicenseFile` fields refer to paths in the
  upstream source tree; the full PSF license is supplied here as `PSF-3.7.0.txt`.

Standalone builds associate these shared notices with every reviewed PySide and
Shiboken component on both platforms, while retaining any installed wheel
notices. A missing notice from any other dependency still stops the build.
Changing a reviewed version, license expression, or notice file requires a
reviewed snapshot update.

## Qt third-party attributions

`qt-third-party-notices.html` collects the original attribution sections from
Qt's documentation. `qt-attributions.json` records the reviewed Qt/PySide6/
Chromium versions and SHA256 of every original page. The excerpts retain their
GFDLv1.3 documentation notice; the original third-party licenses remain unchanged.
They include notices for additional Qt modules beyond those used by GrihaLekha.

After updating Qt dependencies, run `uv run python packaging/update_qt_notices.py`
to fetch notices from the official versioned Qt index and separately versioned
WebEngine/PDF indexes, and refresh the PySide source supplements from the matching
source tag. Review the resulting notices, source revision, wheel versions, and
license expressions before committing. If upstream adds another runtime notice
or changes its location, update the maintenance script's source list as part of
that review. The normal build copies these committed files offline and rejects
a version mismatch, so a dependency upgrade cannot silently reuse old notices.
Also review native libraries listed by the compilation report when preparing a
release; this snapshot is not a substitute for the exact binary/source audit.
