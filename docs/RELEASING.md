# Releasing GrihaLekha

Releases for [iamokish/GrihaLekha](https://github.com/iamokish/GrihaLekha/releases)
are built from `vMAJOR.MINOR.PATCH` tags. The Release workflow tests and builds
the tagged source, then creates a draft for review. Publication is manual.

Each release contains five x64 packages and `SHA256SUMS.txt`:

| Platform | Packages |
| --- | --- |
| Windows 11 | Per-user installer and portable ZIP |
| Debian 12+ / Ubuntu 22.04+ | `.deb` package and Linux portable archive |
| Arch Linux | `.pkg.tar.zst` package |

GitHub also provides source archive links for the tag. Those are separate from
the six uploaded release assets.

## Version and changelog

1. Set the same version in `pyproject.toml` and `src/grihalekha/__init__.py`, then
   run `uv lock`. Versions use three numeric components, such as `0.1.0`.
2. Move completed changes from `## [Unreleased]` into one dated entry in
   [CHANGELOG.md](../CHANGELOG.md), for example `## [0.1.0] - 2026-11-12`.
   Describe the resulting behavior, compatibility changes, and known limitations.
3. Update user and build documentation, and review dependency/source notices
   when dependencies change.
4. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md). Preview the exact
   release body before tagging:

   ```sh
   uv run --frozen python packaging/release.py --release-notes build/release-notes.md --tag v0.1.0
   ```

The notes command extracts only the matching version's changelog entry and
adds package names, installation guidance, checksum instructions, and links to
the tagged documentation. Missing, empty, or duplicate entries fail validation.
The same check runs in CI before compilation; GitHub release notes come from
this file rather than commit messages.

## Tag and build

After the release commit passes CI, create and push its annotated tag:

```sh
git tag -a v0.1.0 -m "GrihaLekha 0.1.0"
git push origin v0.1.0
```

Use the actual release version. A tag must match the project metadata, runtime,
and lockfile. Published tags must not be moved to different source.

The workflow runs the regression suite on Windows and Linux with Python
3.12–3.14, then compiles on Windows and Ubuntu using Python 3.12. It verifies
the relocated application, Chromium and native PDF rendering, embedded fonts,
payment proofs, and complete backups. Portable archives and installed packages
are checked again. The Arch job verifies the Ubuntu-built runtime on Arch
before packaging and installing it.

Release packaging checks the x64 executable header, source hashes, commit,
license documents, and successful compiled verification. A failed check stops
draft creation. After every package succeeds, the final job requires the exact
five package names, writes their SHA-256 checksums, and attaches the six files
to a draft with the reviewed changelog notes.

Use **Actions → Release → Run workflow** on a branch to rehearse compilation.
Manual runs upload the complete packages as workflow artifacts without creating
a GitHub release. See the [build guide](../packaging/README.md) for local builds.

## Review and publish

- Open the draft linked in the final job's summary. Check its version, release
  notes, five platform packages, and checksum file.
- Install on clean Windows 11 and supported Linux desktops. Check startup,
  display scaling, dialogs, English/Hindi PDFs, and desktop shortcuts. Test
  X11/Wayland where supported by the target distribution.
- Use sample accounts to check backup/restore, supported database upgrades,
  an existing installation update, and data retention after uninstalling.
- Review `LICENSE`, `THIRD_PARTY_NOTICES.md`, and the generated source and
  dependency notices. Make sure matching source remains available.
- Record tested platforms and any remaining limitations, then select
  **Publish release**.

Packages are currently unsigned. Checksums detect changed or damaged downloads;
they do not establish publisher identity.

## Repository configuration

Enable Actions and private vulnerability reporting. Protect the default branch
with pull-request review and required checks appropriate to the project.
The workflows use read-only repository permissions except for the final
draft-creation job's `contents: write`. They use `GITHUB_TOKEN`; no personal
access token or signing secret is required. Action dependencies are pinned to
commits and reviewed through Dependabot updates.

## Failed or interrupted releases

Logs, test results, and build-verification diagnostics are uploaded as workflow
artifacts. Fix the source or environment issue before rerunning the build.

The workflow refuses to overwrite an existing release. If draft creation or an
asset upload fails, inspect and remove that unpublished draft before rerunning
the final job. Keep already published releases intact; corrections to published
code or packages require a new version.
