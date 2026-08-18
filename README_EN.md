# SectVoice Reader

SectVoice is a Windows-first, fully local continuous text reader with reusable cloned voice profiles. A reference recording is compiled when a voice is created or replaced; everyday reading reuses that profile for arbitrary new text.

The Reader plays the current generation window while preparing the next one. Double-clicking any character immediately invalidates the old session and continues near the selected position. Basic (CPU) and Standard (NVIDIA CUDA) are independently installable engine/model packages for the same Reader.

See the Chinese [README](README.md) for screenshots and usage, [BUILDING.md](BUILDING.md) for development setup, and [MODEL_LICENSES.md](MODEL_LICENSES.md) for the engine/model licensing boundary.

SectVoice-owned source code is licensed under the [Apache License 2.0](LICENSE). Third-party engines, models and binary components retain their own licenses. The official Windows bundle currently includes a GPLv3 FFmpeg build with Rubber Band; redistributors must comply with those licenses. Apache-2.0 permits closed-source derivatives of SectVoice-owned code, but a proprietary downstream bundle must replace that GPL component or obtain an appropriate commercial licence.
