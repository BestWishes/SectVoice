# FFmpeg binary and corresponding source information

The official SectVoice Windows Core currently carries this unmodified binary:

- Banner: `ffmpeg version 7.1-essentials_build-www.gyan.dev`
- Size: `87,638,016` bytes
- SHA-256: `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`
- Toolchain: GCC 14.2.0 (MSYS2)
- Licence mode reported by the binary: `--enable-gpl --enable-version3`
- Relevant external filter: `--enable-librubberband`

The original binary archive is mirrored by Gyan at:

`https://github.com/GyanD/codexffmpeg/releases/tag/7.1`

The matching FFmpeg release source is the FFmpeg 7.1 source line. Gyan's 7.1
release metadata identifies FFmpeg commit `b08d7969c5`:

`https://github.com/FFmpeg/FFmpeg/commit/b08d7969c5`

Gyan's build index and mirror provide the build configuration, binary archive,
hashes and source-revision links:

- `https://www.gyan.dev/ffmpeg/builds/`
- `https://github.com/GyanD/codexffmpeg`

The complete configure line is reproducible by running the distributed
`ffmpeg.exe -version`; it is also recorded in `THIRD_PARTY_NOTICES.md` release
materials. The included `licenses/GPL-3.0.txt` applies to this GPLv3 build.

Rubber Band is available under GPL-2.0-or-later or a separate commercial
licence from its copyright holder:

- Source: `https://github.com/breakfastquay/rubberband`
- Licence: `https://breakfastquay.com/rubberband/license.html`

SectVoice does not modify FFmpeg or Rubber Band. If a future release changes
the binary or build configuration, this file and the shipped source references
must be updated before publication.
