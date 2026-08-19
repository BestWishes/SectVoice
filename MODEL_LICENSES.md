# Engine and model licenses

This document describes the license boundary; it is not legal advice and does not replace upstream license texts.

| Component | Role | Upstream terms recorded by this project |
|---|---|---|
| MOSS-TTS-Nano code and pinned ONNX model | Basic CPU engine | Apache-2.0 |
| MOSS-TTS-Nano `zh_3.wav` and `zh_4.wav` samples plus bundled derived VoicePackages | Built-in experience voices | Apache-2.0 |
| GPT-SoVITS pinned source and v2ProPlus model repository | Standard CUDA engine | MIT as declared by upstream |
| G2PW resources | Chinese text frontend | MIT as declared by upstream |
| fastText `lid.176.bin` | Language identification | CC BY-SA 3.0 |
| faster-whisper / CTranslate2 | Local ASR | MIT |
| Whisper model files | Local ASR model | Upstream MIT release terms |

`g2p_en` 2.1.0 declares the GPL `Distance` package as a dependency even though
its installed runtime code does not import it. SectVoice's Standard release
builder excludes that unused module and its metadata; the package audit fails
if it reappears in a public archive.

Model packages are downloaded separately and remain under their upstream terms. User-created SectVoice VoiceProfiles and `.voicepkg` files remain user data. The only included VoicePackages are the two hash-pinned OpenMOSS-derived experience voices documented in `BUILTIN_VOICES.md`.

Before adding or upgrading an engine/model:

1. pin a source revision and model revision;
2. preserve upstream license/notice files inside the package;
3. inventory transitive runtime dependencies;
4. verify whether model data has separate acceptable-use or attribution terms;
5. update this file and `THIRD_PARTY_NOTICES.md`;
6. repeat real quality, completeness, latency, cancellation and long-run tests.

If an upstream license cannot be confirmed, the component is not eligible for a public package.
