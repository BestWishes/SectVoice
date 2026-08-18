# Third-party notices

SectVoice-owned source code is Apache-2.0. The following components retain separate terms. Complete license texts shipped with an installed component control over this summary.

## Reader Core

- CPython: Python Software Foundation License.
- PySide6 / Qt 6: LGPLv3/GPLv3/commercial, using dynamically loadable Qt DLLs.
- NumPy: BSD-3-Clause.
- psutil: BSD-3-Clause.
- platformdirs: MIT.
- opencc-python-reimplemented: Apache-2.0, including its OpenCC-derived data notices.
- PyInstaller bootloader: GPLv2-or-later with the PyInstaller bootloader exception.
- faster-whisper and CTranslate2: MIT.
- Whisper model files: upstream MIT model/code release terms.

## FFmpeg and Rubber Band in the official Windows bundle

The included `ffmpeg.exe` identifies itself as Gyan's static Essentials build and reports `--enable-gpl`, `--enable-version3` and `--enable-librubberband`. Gyan documents its builds as GPLv3. Rubber Band is GPL-2.0-or-later unless a publisher obtains a commercial licence.

SectVoice invokes FFmpeg as a separate process; no FFmpeg library is linked into the Python/Qt executable. The official open-source bundle nevertheless preserves the GPL notices and corresponding-source references. Do not describe this binary as LGPL-only. A closed-source downstream bundle must replace this component with a compatible implementation or obtain the necessary commercial rights.

- FFmpeg legal information: https://ffmpeg.org/legal.html
- Gyan build and matching source revision index: https://www.gyan.dev/ffmpeg/builds/
- Rubber Band licence: https://breakfastquay.com/rubberband/license.html

## Basic package

MOSS-TTS-Nano code and the pinned ONNX model are recorded as Apache-2.0. The package carries the upstream Apache licence and its dependency metadata.

## Standard package

GPT-SoVITS source/model are recorded as MIT. G2PW resources retain their upstream licence. The included fastText `lid.176.bin` model is CC BY-SA 3.0 and requires attribution. Python dependencies retain their installed metadata and licence files. The unused GPL `Distance` dependency declared by `g2p_en` is explicitly excluded from the public Standard runtime.

## User content

No reference recording, cloned voice, VoiceProfile, EnginePayload, generated speech, document, user database or internal test sample is included in the public source or release assets.
