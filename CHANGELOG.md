# Changelog

Notable changes to GrihaLekha are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); release tags use `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [0.1.0] - 2026-11-12

The first release of GrihaLekha: offline rent and tenant accounts for Windows
and Linux, with English, Hindi, and bilingual statements.

### Added

- Buildings, flats, households, and dated tenancy accounts with individual
  monthly rent, billing days, grace periods, and opening balances.
- Reviewed monthly rent and electricity bills, optional meter readings, and
  electricity deferrals that combine several cycles into one bill.
- Payment receipts with optional proofs, available balances, full bill payments,
  payment reversals, refunds, and cycle payment locks.
- Separate refundable security deposits and dated transfers between deposits
  and available balance, with receipt source tracking.
- Bill amount corrections, linked replacements, and retained transaction history.
- Rent changes, flat transfers and exchanges, move-out corrections, cancellation
  of upcoming tenancies, and archival of settled accounts.
- Guarded deletion of archived accounts with optional CSV and PDF records.
- English, Hindi, and bilingual PDF statements and receipts, plus exact CSV
  exports with billing status, meter readings, and electricity coverage.
- Display currencies: INR (default), USD, EUR, JPY, GBP, CAD, AUD, SGD, and NZD.
  Switching changes symbols and labels only; amounts and decimal precision are
  preserved without currency conversion.
- Complete backup and restore for the database, proofs, and issued documents,
  including automatic managed backups and verified upgrades of supported older
  account databases.
- Windows installer and portable packages, Debian/Ubuntu and Arch Linux
  packages, a Linux portable archive, and SHA-256 checksums.

### Technical notes

- All money uses exact integer arithmetic. Every display currency retains two
  decimal places; no exchange rates or automatic rounding are applied.
- The app runs locally without an account or server. SQLite stores account
  records; PySide6 provides the desktop interface. Release packages include
  their Python runtime, PDF renderer, and fonts.
- Accounting dates and statement issue times use India Standard Time.
- Account files and backups are not encrypted. Keep live databases on a local
  disk and use backup exports to transfer records between computers.
- Chromium is the default PDF renderer. Native Qt printing is available as a
  fallback, with limited Hindi text search and copy fidelity.
- Release packages target x86-64 Windows and Linux. Packages are unsigned;
  checksums verify file integrity, not publisher identity.

### License

- Original code and artwork: **AGPL-3.0-or-later**. Third-party dependencies and
  fonts retain their own licenses.

[Unreleased]: https://github.com/iamokish/GrihaLekha/compare/v0.1.0...master
[0.1.0]: https://github.com/iamokish/GrihaLekha/releases/tag/v0.1.0
