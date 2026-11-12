# Security and data safety

Use the latest published release. Security fixes are maintained on the main
development line; older releases do not have a separate support branch.

Report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/iamokish/GrihaLekha/security/advisories/new).
If private reporting is unavailable, open an
[issue](https://github.com/iamokish/GrihaLekha/issues) asking for a private contact
channel without including exploit details, credentials, or tenant data.

For a private report, include the application version, operating system,
reproduction steps using synthetic data, and expected versus actual behavior.
Do not upload a live database, backup, payment proof, or tenant statement to a
public issue. Logs may contain local paths and account details; redact them.

GrihaLekha is an offline application, but account files, exported documents, and
backups are not encrypted by the app. Protect the operating-system account and
storage. Use the built-in backup/restore workflow, and retain an independent
backup before updates or recovery. See [USER_GUIDE.md](USER_GUIDE.md).
