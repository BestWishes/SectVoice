# SectVoice development status

Last updated: 2026-08-20

## Current release line

- Stable public source and downloadable release: v0.3.4.
- v0.3.4 fixes pause/resume without changing the accepted v0.3.3 voice, synthesis or model-package paths.
- The release continues to ship the two attributed, hash-pinned OpenMOSS-derived experience voices introduced in v0.3.3 and imports them once for a fresh data root.
- Basic and Standard model assets remain the existing immutable downloads and are not uploaded again.
- Reader, Basic, Standard, reusable voice profiles, continuous windows, multi-role reading, rapid seek, cache reuse, package management and in-app updates are implemented.

## Next release staging (not published)

- The installed Start Menu and optional desktop shortcuts now declare the same `BestWishes.SectVoice.Reader` AppUserModelID already used by the Reader process.
- Both shortcuts explicitly select icon index 0 from `SectVoiceReader.exe` instead of leaving Windows Shell to infer the icon source.
- This addresses intermittent generic taskbar icons after first launch or upgrade without changing the current v0.3.4 executable, installer or public release. Existing taskbar pins may need to be unpinned and pinned again after the future installer upgrade so Windows discards their old cached shortcut metadata.

## v0.3.4 pause/resume fix

- Corrected the Pause/Continue button: its Continue state now invokes the real resume path instead of calling pause a second time.
- Added an explicit transport-pause flag independent from model loading, preparation, alignment validation and pre-generation. Background work may continue filling the queue while paused, but its status cannot replace `Paused` or disable Continue.
- Play and Pause/Continue now both use the controller's actual transport state, avoiding a UI label/state race.
- Added controller- and UI-level regressions covering pause during background preparation, ignored background status updates and successful resume.
- Full regression status: 184 passed; `compileall` and repository diff checks passed.
- The frozen package CLI and frozen Reader startup smoke checks passed without loading a speech model.
- The finalized release contains a 663,480,099-byte Core ZIP and a 581,386,169-byte unsigned installer. All ten files match `SHA256SUMS.txt`, and the privacy audit passed.
- Basic and Standard continue to reference the existing immutable engine/model assets; this Reader-only fix does not duplicate their multi-gigabyte downloads.

## v0.3.3 completed acceptance

- The product owner accepted v0.3.3 as complete on 2026-08-20. No additional clean-machine installation follow-up remains open for this release.
- The project intentionally does not use GitHub Actions or other hosted CI. Local automated checks, real-engine smoke tests, package audits and recorded manual evidence are the release authority.

- Identified Experience Female and Experience Male as exact copies of OpenMOSS/MOSS-TTS-Nano `zh_4.wav` and `zh_3.wav` at pinned revision `cc7bdf19c7639c0870dab22045a33b442760f6be`; both source SHA-256 values match upstream.
- Exported clean VoicePackages containing ready Basic and Standard payloads, totalling about 5.4 MB compressed, with no absolute developer path in any text member.
- Added one-time per-VoiceId seeding: fresh roots import both, upgrades preserve an existing same-ID profile, and a deleted built-in voice stays deleted.
- Extended source/frozen packaging, provenance notices and release auditing so only exact catalogued built-in VoicePackages are allowed; all other voice or user-data files remain release blockers.
- Full regression status: 182 passed.
- Fresh-root real-engine validation passed for Experience Female and Experience Male on
  both Basic `cc7bdf19` and Standard `d523079f-sv3`; all four imported payloads were
  relocated under the clean user root and produced non-empty audible WAV output.
- Frozen Reader cold-start validation imported exactly two built-ins and four ready payloads;
  the v0.3.3 Core, installer, checksums and privacy audit completed successfully.

## v0.3.2 release

- Replaced the public interface preview set with four v0.3.2 screenshots in filename order: Warm Paper Reader, Night Reading Reader, voice creation and engine/package management.
- Removed the persistent primary-button marker from Play, Create Voice, Install Basic and Install Standard so these controls use the same ordinary button appearance as their neighbors; transient hover, press, disabled and list-selection feedback remain intact.
- Confirmed the long-reading failure `GenerationWindow produced an empty SpeechUnit range` was a Reader boundary-mapping defect: an ASR timestamp for an extreme-short final unit could clamp to the end of the generated PCM and leave that unit with zero frames.
- Window layouts now reject zero-frame SpeechUnits before caching or playback. A rejected multi-unit window automatically retries and then splits only at complete SpeechUnit boundaries, so no source text is silently skipped.
- Whole-window speed scaling and proportional fallback reserve at least one PCM frame for every SpeechUnit, preventing rounding from reintroducing an empty range.
- Old cache entries containing an invalid empty range are discarded and regenerated automatically instead of stopping playback.
- Unexpected generation failures now write their traceback with session and generation identifiers to the Reader log before the user-facing error callback.
- Regression status for the v0.3.2 source checkout: 175 passed.
- PyInstaller Reader/Package executables, the 658,077,355-byte Core ZIP and the 575,899,750-byte installer were rebuilt from the reviewed v0.3.2 source; the frozen package CLI and hidden Reader startup smoke checks passed.
- All 10 local v0.3.2 release assets match `SHA256SUMS.txt`; every Basic/Standard catalog asset remains an immutable remote reference, so no model archive is duplicated in this Reader-only release.

## v0.3.1 release

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
- Published the source tag and Windows release on 2026-08-19; the remote `latest` update manifest resolves to v0.3.1 and the installer asset size matches the local signed-off manifest.

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
| `v0.3.4` | Current pause/resume bug-fix release; reuses unchanged v0.3.3 voices and engine packages |
| `v0.3.3` | Accepted release with built-in experience voices |
| `v0.3.2` | Persistent-button and empty-SpeechUnit boundary recovery release; reuses unchanged engine packages |
| `v0.3.1` | Reader theme and Windows branding release; reuses the unchanged v0.3.0 engine packages |
| `v0.3.0` | Public open-source/release baseline before theme and branding work |
| `v0.2.5` | Stable release with Standard GPU pacing |
| `v0.2.4-stable-baseline` | Stable state before GPU pacing work |
| `v15-stable-baseline` | Stable state before Windows distribution work |

## Release gate

- Local automated tests (184), compile check and diff check pass; the project does not use GitHub Actions.
- The v0.3.0 voice path is unchanged from the real-audio v0.2.5 baseline; Basic/Standard real synthesis, continuous playback and rapid seek evidence remains recorded in `VALIDATION_REPORT.md`.
- Installer build, component-size metadata and update manifests are complete.
- Release privacy audit and SHA-256 manifest pass.
- The frozen Reader and package CLI start successfully from the finalized Core tree.
- The managed-sandbox Inno run could not finish because registry and Start Menu writes were denied. This remains historical environment evidence, not an open v0.3.3 blocker; the product owner accepted v0.3.3 as complete on 2026-08-20.
- The v0.3.4 Reader release reuses the unchanged Basic and Standard assets instead of uploading several gigabytes of model data again.

## Known boundaries

- Standard cold start normally takes about 10–15 seconds on the validated machine.
- Basic and Standard may sound different for the same VoiceId because their private payloads and models differ.
- No independently validated emotion-control interface is exposed for the current engines.
- The installer is intentionally distributed unsigned for now. Windows may display “Unknown publisher”; this is a documented release choice, not an unfinished release task.
- GitHub Actions and other hosted CI are intentionally outside the project plan.
- The official Windows bundle includes GPL components; see `THIRD_PARTY_NOTICES.md` before redistribution.
