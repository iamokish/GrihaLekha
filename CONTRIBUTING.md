# Contributing

Read the [architecture](docs/ARCHITECTURE.md) and [service contract](docs/API_CONTRACT.md)
before changing billing, dates, storage, or account lifecycle behavior. Discuss
large behavior changes in an [issue](https://github.com/iamokish/GrihaLekha/issues)
with a concrete example and expected result.

## Development environment

Use Python 3.12–3.14 and uv. From the repository root:

```sh
uv sync --locked --extra dev --python 3.12
uv run --frozen python -m grihalekha --data-dir .data/development
```

Use a separate data folder for development. Do not test migrations or destructive
operations against real tenant accounts. `.data/` is ignored by Git.

## Checks

On Linux:

```sh
TZ=Asia/Kolkata QT_QPA_PLATFORM=offscreen GRIHALEKHA_PDF_RENDERER=native uv run --frozen pytest
```

On Windows PowerShell:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:GRIHALEKHA_PDF_RENDERER = 'native'
uv run --frozen pytest
```

The application uses IST. Several date-sensitive tests use the machine's current
date, so CI sets its time zone to India Standard Time. For local runs, use that
time zone or avoid the interval when local and IST dates differ.

Run `uv build` to check source/wheel packaging and
`uv run --frozen python scripts/check_distribution.py dist` to inspect the result.
Run `uv run --frozen python packaging/build.py --dry-run` to inspect compiler
configuration. Full build instructions are in [packaging/README.md](packaging/README.md).

## Changes and review

- Keep amounts as integer hundredths (`*_paise`) and dates as ISO strings. Preserve dated audit
  history, transaction boundaries, cycle locks, and backup safety.
- Add regression coverage for behavior changes, especially boundary dates,
  corrections, rollback, and restoration of older databases. Keep UI changes
  accessible by keyboard and test affected Qt behavior offscreen.
- Update the user guide and internal contract when behavior or APIs change.
  Record user-visible changes under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).
- Keep dependency changes deliberate. Update `pyproject.toml`, run `uv lock`,
  review `uv.lock`, and test. Do not broadly upgrade dependencies as part of an
  unrelated fix.
- Include the problem, resulting behavior, relevant screenshots when useful,
  and checks run in the pull request. Use synthetic data in examples.

Never commit account databases, payment proofs, backups, issued tenant PDFs,
credentials, virtual environments, compiler caches, or release binaries. Check
the staged file list before committing; `.gitignore` is a convenience, not a
complete guarantee that a new filename is safe to share.

Contributions to the original project are under AGPL-3.0-or-later. Preserve copyright
and third-party license notices. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
