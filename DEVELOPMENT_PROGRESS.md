# SectVoice development status

Last updated: 2026-08-19

## Current release line

- Stable public source and downloadable release: v0.3.0.
- v0.3.1 release candidate: unified Windows branding and persistent Reader themes; Basic and Standard model assets remain the immutable v0.3.0 downloads.
- Reader, Basic, Standard, reusable voice profiles, continuous windows, multi-role reading, rapid seek, cache reuse, package management and in-app updates are implemented.

## v0.3.1 release candidate

- Uses the supplied blue book/speaker artwork as one normalized multi-resolution Windows ICO (16 through 256 px) and a 512 px source PNG.
- Applies a stable Windows AppUserModelID and the same icon to QApplication/windows, Reader and package-helper EXEs, installer, shortcuts and uninstall entry.
- Adds a UI-only `ThemeManager` with semantic palette tokens; VoiceCore, engine requests, audio cache identity and playback behavior are unchanged.
- Adds four immediate, SQLite-persisted themes: Warm Paper (default), Soft Cream, Quiet Sage and Night Reading.
- Restyles the main three-panel layout, reading surface, playback bar, lists, tables, forms, buttons, scrollbars, progress/status elements and inherited dialogs.
- Adds automated theme/catalog/persistence/icon/package-wiring coverage and actual offscreen visual captures for the warm and night themes.
- PyInstaller one-folder build completed successfully; the embedded EXE icon plus bundled runtime ICO were inspected, and the frozen Reader stayed alive through a startup smoke check.
- Inno Setup 6.7.3 successfully compiled the final installer script against a minimal Core fixture, and the resulting installer icon was extracted and visually checked.
- Corrects one legacy test that assumed every source checkout folder must literally be named `source`; it now verifies both the installed `source` layout and arbitrarily named public clones without changing runtime path behavior.
- Regression status in the public checkout: 171 passed.

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
| `v0.3.1` | Reader theme and Windows branding release; reuses the unchanged v0.3.0 engine packages |
| `v0.3.0` | Public open-source/release baseline before theme and branding work |
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
- The v0.3.1 Reader release reuses the unchanged v0.3.0 Basic and Standard assets instead of uploading several gigabytes of model data again.

## Known boundaries

- Standard cold start normally takes about 10–15 seconds on the validated machine.
- Basic and Standard may sound different for the same VoiceId because their private payloads and models differ.
- No independently validated emotion-control interface is exposed for the current engines.
- The installer is not code-signed.
- The official Windows bundle includes GPL components; see `THIRD_PARTY_NOTICES.md` before redistribution.
