# Built-in experience voices

SectVoice Reader Core includes two small, ready-to-use VoicePackages so a new user can verify reading immediately without first finding a reference recording.

## Provenance

Both source recordings are official samples from [OpenMOSS/MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) revision `cc7bdf19c7639c0870dab22045a33b442760f6be`. That repository's root licence is Apache-2.0, Copyright 2026 OpenMOSS Team, Fudan University, SII and MOSI.

| SectVoice name | Stable VoiceId | Upstream file | Source SHA-256 | VoicePackage SHA-256 |
|---|---|---|---|---|
| 体验女声 | `69d643d7-a669-4fac-a3ef-5f483ecc2a0e` | `assets/audio/zh_4.wav` | `360724ce411ddc94e866fe98ee3161349836bba16037ed7f209a1e807f4fd7fd` | `92ed456b4c29f56c4bb41d9fef8e1f3f9c457d01b8685c3c25cd39b58b888e10` |
| 体验男声 | `a6467091-ad09-47eb-ab5d-92fc991e3e61` | `assets/audio/zh_3.wav` | `406d80c599da92573719c7236fd4a93a49c9a8c5c51872118a6444558249d70d` | `cf6ae91f2f321002b0a36df93a1ce7a60f6e74933ed1b7c9febc2a69bdfbe601` |

Each package contains the upstream sample, a normalized reference, its transcript, a test preview and engine-private Basic and Standard payloads. Package manifests contain relative paths only; no developer database, cache, document, log or machine path is included.

## Reader behavior

- A fresh data root imports both packages once before the main window opens.
- An existing profile with the same VoiceId is preserved and is not renamed or overwritten.
- The user may rename, export or delete either profile.
- A completed per-VoiceId seed marker remains after deletion, so the profile is not forced back on later launches.
- Future bundled voices must use a new catalogued VoiceId or an explicit, tested migration; they must never silently replace user data.

The Apache-2.0 record documents the upstream repository's copyright terms. Redistributors remain responsible for any separate voice, personality, privacy or local-law obligations applicable to their use and generated speech.
