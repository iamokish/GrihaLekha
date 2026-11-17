# Agreed maintenance follow-ups

These changes were deferred during the November 2026 CI and code audit so they
can be reviewed separately from the failure fixes.

1. **Detect edits during standalone compilation.** Capture the application code
   and runtime asset hashes before Nuitka starts, then compare them before
   accepting the compiled bundle. Fail clearly if an input changed during the
   build. Cover changed and removed files, and ensure a rejected build cannot
   receive a successful verification manifest. The existing release check
   already rejects bundles whose inputs changed after compilation.

2. **Share account-reference validation between startup and backup.** Review
   ownership and chronology requirements for allocations, refund sources,
   deposit sources, and electricity coverage links. Define a shared validator
   for opening accounts and validating backup snapshots, retaining support for
   older schemas and valid historical records. Test invalid cross-account links
   and ensure rejection preserves the live database and existing backups. The
   current audit added narrowly scoped deposit-source checks; the broader
   validation change remains separate.
