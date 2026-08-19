# Changelog

## 0.3.3 - 2026-08-19

- Added attributed Experience Female and Experience Male VoiceProfiles derived from the Apache-2.0 OpenMOSS/MOSS-TTS-Nano sample recordings.
- Added one-time, hash-verified first-start seeding that preserves an existing matching VoiceId and does not resurrect a profile after the user deletes it.
- Extended PyInstaller/wheel packaging and the release privacy audit to admit only exact catalogue-approved built-in VoicePackages while continuing to reject all other voice and user-data assets.

## 0.3.2 - 2026-08-19

- Unified the Play, Create Voice and package-install controls with the ordinary button appearance instead of leaving them permanently highlighted.
- Prevented extreme-short final SpeechUnits from receiving an empty PCM range during continuous-window alignment.
- Added automatic complete-unit retry/splitting and invalid-cache eviction so this boundary condition no longer stops long reading or silently skips source text.
- Added full runtime traceback logging for unexpected generation failures.

## 0.3.1 - 2026-08-19

- Added a unified SectVoice Windows icon for the taskbar, frozen executable, installer, shortcuts and uninstaller.
- Added four persistent Reader themes: Warm Paper, Soft Cream, Quiet Sage and Night Reading.
- Reworked the main window and shared Qt controls with warmer panels, reading surfaces, rounded controls and consistent dialog styling without changing voice or playback behavior.

## 0.3.0 - 2026-08-18

- Open-sourced SectVoice-owned code under Apache License 2.0.
- Added document display-name editing and document deletion inside Reader.
- Added public screenshots, build/contribution/security/privacy/responsible-use documents and corrected third-party notices.
- Kept the v0.2.5 real-audio Reader/Basic/Standard behavior and Standard GPU pacing baseline.

## 0.2.5 - 2026-08-18

- Smoothed Standard GPU generation power/clock behavior while keeping generated PCM byte-identical.
- Completed real A/B, continuous playback, rapid-seek, cached-seek, 30-minute and installed-upgrade validation.

Earlier test-release details are summarized in `VALIDATION_REPORT.md` and preserved in the private archive history.
