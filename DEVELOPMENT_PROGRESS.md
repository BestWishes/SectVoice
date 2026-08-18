# SectVoice development status

Last updated: 2026-08-18

## Current release line

- Stable downloadable release: v0.2.5.
- Locally finalized open-source release: v0.3.0; remote publication is pending GitHub access.
- Reader, Basic, Standard, reusable voice profiles, continuous windows, multi-role reading, rapid seek, cache reuse, package management and in-app updates are implemented.

## v0.3.0 changes

- Added Reader document display-name editing without renaming the imported TXT file.
- Added Reader document deletion with an explicit confirmation. Dependent SQLite metadata is removed; the imported TXT file and reusable shared audio-cache files are not deleted.
- Added Apache-2.0 project licensing, public contribution/security/privacy/responsible-use documents and sanitized interface previews.
- Corrected the binary dependency notice: the bundled Gyan FFmpeg Essentials build is GPLv3 and includes Rubber Band; it is not an LGPL-only build.
- Prepared a clean public source history that excludes private Git history, user data, models, environments and internal voice assets.
- Rebuilt the frozen Reader, Core ZIP, Basic package, Standard package, installer, catalog, update manifest and SHA-256 file from the reviewed v0.3.0 source.
- Verified 166 automated tests, `compileall`, frozen package CLI startup, frozen Reader startup and all 20 release checksums.
- Recalculated installer download/installed-size constants from the actual Core/Basic/Standard artifacts.
- Excluded the unused GPL `Distance` package declared by `g2p_en` from the Standard release runtime.

## Stable rollback references

| Reference | Purpose |
|---|---|
| `v0.2.5` | Stable release with Standard GPU pacing |
| `v0.2.4-stable-baseline` | Stable state before GPU pacing work |
| `v15-stable-baseline` | Stable state before Windows distribution work |

## Release gate

- Automated tests, compile check and diff check pass.
- The v0.3.0 voice path is unchanged from the real-audio v0.2.5 baseline; Basic/Standard real synthesis, continuous playback and rapid seek evidence remains recorded in `VALIDATION_REPORT.md`.
- Installer build, component-size metadata and update manifests are complete.
- Release privacy audit and SHA-256 manifest pass.
- The frozen Reader and package CLI start successfully from the finalized Core tree.
- A true Inno install cannot finish inside the current managed sandbox because registry and Start Menu writes are denied; the same installer must receive one final clean-machine install check after remote publication.
- Public repository and Release publication remain incomplete until remote GitHub writes succeed.

## Known boundaries

- Standard cold start normally takes about 10–15 seconds on the validated machine.
- Basic and Standard may sound different for the same VoiceId because their private payloads and models differ.
- No independently validated emotion-control interface is exposed for the current engines.
- The installer is not code-signed.
- The official Windows bundle includes GPL components; see `THIRD_PARTY_NOTICES.md` before redistribution.
