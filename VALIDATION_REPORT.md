# SectVoice public validation summary

Validation date: 2026-08-18
Machine: Windows 11 Pro, Intel i5-12490F, 31.8 GiB RAM, NVIDIA RTX 2060 SUPER 8 GiB

This public summary contains measured results but omits user voices, recordings, database contents and private artifact paths.

## Stable v0.2.5 evidence

| Area | Result |
|---|---|
| Automated suite | 163 tests passed; `compileall` and `git diff --check` passed |
| Standard GPU pacing | Same voice, text and seed produced byte-identical PCM before and after pacing in 3/3 comparisons |
| Standard power profile | Mean 72.07 W → 49.94 W; P95 93.77 W → 70.42 W; peak 133.39 W → 79.57 W |
| Standard inference | Cold load 11.59 s; 3.11 s generated 3.88 s of cloned audio |
| Continuous reading | 7/7 SpeechUnits played exactly once in order; no underrun or error; 1,000 ms paragraph pause applied |
| Rapid seek | Three rapid seeks played only the final target; no stale chunk re-entry |
| Cached mid-window seek | Playback began in about 0.09–0.11 s without regeneration |
| 30-minute Standard run | 1,800.08 s, 177 units, no underrun/error; worker peak 3,848.90 MiB, VRAM peak 3,710 MiB |
| Upgrade preservation | Existing voices, cache files, SQLite user/content data and Standard weights were unchanged across upgrade |

## v0.2.4 voice/reference evidence

| Area | Result |
|---|---|
| Automated suite | 159 tests passed |
| Reference audit | Nine installed profiles checked; no corrupt audio, clipping blocker or objective tremolo blocker |
| Simplified Chinese ASR display | ASR text and final VoiceProfile transcript are converted with OpenCC `t2s`; raw ASR remains diagnostic-only |
| User-reported sentence cases | Basic and Standard each read 6/6 units exactly once and in order without underrun |
| Five-minute cold runs | Basic 29 units and Standard 33 units; no underrun/error |

## First distributable release evidence

- Clean Core-only install ran from the installation directory using the portable Python, FFmpeg and ASR components.
- Basic and Standard were independently installed, enabled, uninstalled, restored and used for real cloned speech.
- Pause/resume, previous/next, stop, multi-role playback and worker crash recovery were exercised with real audio output.
- Speed 0.75/1.0/1.5 changed duration while keeping pitch stable in the validated path.
- Public asset audits rejected audio files, VoicePackages, SQLite databases, internal voice names, absolute development paths and incomplete downloads.

## v0.3.0 release-candidate additions

- 166 automated tests pass, including display-name edits, document deletion cascades, release privacy rules and the unused-`Distance` exclusion.
- `compileall` passes for `src`, `engine_packages`, `scripts` and `tests`.
- Imported TXT files remain untouched by both operations.
- Shared cache entries remain available after a document reference is deleted.
- PyInstaller produced both Reader and package-manager executables; the package CLI help command succeeds and the frozen Reader remains running after its startup interval.
- The finalized v0.3.0 directory contains the installer, Core, Basic, nine Standard parts, catalog/update manifests, public notices and 20 matching SHA-256 entries.
- Standard release metadata and archive paths contain no `Distance 0.1.3` module or dist-info directory.
- The installer uses actual artifact sizes: Core 1,067,437,110 installed bytes; Basic 420,796,975 download / 881,108,266 installed bytes; Standard 5,434,716,749 download / 8,577,894,613 installed bytes.
- Managed-sandbox installation copied all files, then correctly rolled back when Windows denied the sandbox access to the Start Menu and HKCU uninstall registry key. This is an environment limitation, not a file-copy or installer-build failure; a final unrestricted clean-machine install remains required.

Hardware and driver combinations differ. Real engine smoke tests must be repeated when changing PyTorch, CUDA, ONNX Runtime, FFmpeg, an engine commit or model weights.
