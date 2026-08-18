# Security policy

## Supported versions

Security fixes target the latest release. Older unsigned test releases may not receive updates.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose local files, execute code, bypass package verification or load an untrusted VoicePackage. Use GitHub's private vulnerability reporting for this repository. Include the affected version, reproduction steps and impact; remove reference audio, voices, document text, access tokens and local user paths.

Package catalogs and Reader updates are verified by declared byte size and SHA-256, but SectVoice releases are not currently code-signed. Download only from the official Release page and verify the published hash.
