# Privacy

SectVoice is a local desktop application.

Stored locally:

- imported/pasted documents and reading positions;
- reference recordings, selected clips and corrected transcripts;
- VoiceProfiles and engine-private payloads;
- generated audio cache, settings and diagnostic logs.

On first startup, Reader Core copies two public, hash-pinned experience VoicePackages into the local voice library. They are application assets rather than user recordings. The one-time import marker prevents a voice that the user later deletes from being recreated on every launch.

The Reader does not upload these items to a SectVoice service. Network access occurs only after an explicit package/update action and is used to read the public GitHub catalog or download a selected release asset.

Deleting a document removes its Reader text and dependent metadata but does not delete the original imported TXT file. Reusable cache audio may remain until the cache is cleared. Deleting a voice removes its profile according to the UI confirmation; exported `.voicepkg` files are ordinary local files managed by the user.

Before sharing logs, remove document text, voice IDs, local paths and hardware identifiers you do not want to disclose.
