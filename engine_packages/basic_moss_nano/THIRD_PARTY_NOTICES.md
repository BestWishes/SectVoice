# Third-party notices

This package contains adapted files from `OpenMOSS/MOSS-TTS-Nano`, commit
`cc7bdf19c7639c0870dab22045a33b442760f6be`. The upstream source and model
repositories declare Apache License 2.0. The unmodified license is preserved
in `LICENSE`.

SectVoice changes `onnx_tts_runtime.py` to remove the PyTorch/Torchaudio import
from the ONNX CPU path. Reference media decoding and resampling are moved to
the one-time VoiceCompiler phase; the engine receives normalized 48 kHz
signed-16-bit PCM WAV.

The engine-private prompt audio codes produced by the codec encoder remain
inside this package's payload directory and are never part of SectVoice's
common VoiceProfile or AudioChunk protocols.

