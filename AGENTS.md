# SectVoice development rules

- Read `PRODUCT_SPEC.md`, `ARCHITECTURE.md`, `DEVELOPMENT_PROGRESS.md` and the relevant module before changing behavior.
- Keep Reader UI, VoiceCore orchestration and engine-private runtime code separated.
- `AudioChunk` maps through `SpeechUnitId`; do not add Reader `TextStart`/`TextEnd`. Timing data may only describe positions inside a SpeechUnit.
- Keep every `EnginePayload` private to its engine. Do not promote MOSS or GPT-SoVITS fields into the shared VoiceProfile protocol.
- Model loading, inference, ASR, file import and media processing must not block the Qt GUI thread.
- Every seek creates a new session/generation identity. Old audio and completed stale jobs must never re-enter playback.
- Do not commit environments, models, user databases, voices, reference audio, generated audio, caches, logs or release staging directories.
- Public distributions must not contain internal test voices or content. Run the release privacy audit before publishing.
- Add or update tests for behavior changes. Do not delete a failing test to make a release pass.
- Record current work in `DEVELOPMENT_PROGRESS.md`; keep historical evidence in Git and `VALIDATION_REPORT.md` rather than growing a session diary.
- Third-party code and model updates require an explicit source, pinned version, license review and notice update.
- Use `apply_patch` for source and documentation edits. Preserve unrelated user changes.
