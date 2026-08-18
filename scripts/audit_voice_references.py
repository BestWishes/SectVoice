from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.reference_analysis import analyze_reference_samples
from sectvoice.core.voice_quality import to_simplified_chinese


SUPPORTED_AUDIO = {".wav", ".mp3", ".flac", ".m4a"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit source voice samples and installed VoiceProfile references"
    )
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--source-audio-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    media = FFmpegProcessor(args.ffmpeg.resolve())
    source_files = sorted(
        path
        for path in args.source_audio_dir.resolve().iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO
    )
    source_hashes = {sha256_file(path): path for path in source_files}
    sources = []
    for path in source_files:
        samples = media.decode_mono_f32(path, sample_rate=16_000)
        report = analyze_reference_samples(samples, 16_000)
        sources.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "quality": report.to_dict(),
            }
        )

    profiles = []
    if args.database is not None:
        connection = sqlite3.connect(args.database.resolve())
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM voice_profiles ORDER BY name COLLATE NOCASE"
            ).fetchall()
            for row in rows:
                source = Path(row["source_audio_path"])
                reference = Path(row["reference_audio_path"])
                transcript = str(row["transcript"])
                source_hash = sha256_file(source) if source.is_file() else None
                profile: dict[str, object] = {
                    "voice_id": row["voice_id"],
                    "name": row["name"],
                    "transcript": transcript,
                    "transcript_is_simplified": (
                        to_simplified_chinese(transcript) == transcript
                    ),
                    "source_audio_path": str(source),
                    "source_sha256": source_hash,
                    "matching_supplied_source": (
                        str(source_hashes[source_hash])
                        if source_hash is not None and source_hash in source_hashes
                        else None
                    ),
                    "reference_audio_path": str(reference),
                    "selection": [
                        float(row["reference_start_seconds"]),
                        float(row["reference_end_seconds"]),
                    ],
                }
                if reference.is_file():
                    samples = media.decode_mono_f32(reference, sample_rate=16_000)
                    profile["quality"] = analyze_reference_samples(
                        samples,
                        16_000,
                        transcript=transcript,
                    ).to_dict()
                    diagnostics = reference.parent / "diagnostics.json"
                    profile["diagnostics_path"] = (
                        str(diagnostics) if diagnostics.is_file() else None
                    )
                else:
                    profile["reference_missing"] = True
                profiles.append(profile)
        finally:
            connection.close()

    result = {
        "schema_version": 1,
        "source_audio_directory": str(args.source_audio_dir.resolve()),
        "source_files": sources,
        "database": str(args.database.resolve()) if args.database is not None else None,
        "profiles": profiles,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
