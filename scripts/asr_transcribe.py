from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--cpu-threads", default=4, type=int)
    parser.add_argument("--word-timestamps", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(args.model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, args.cpu_threads),
        local_files_only=True,
    )
    segments, info = model.transcribe(
        str(args.audio),
        language=args.language,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=args.word_timestamps,
    )
    segments = tuple(segments)
    rows = [
        {"start": item.start, "end": item.end, "text": item.text.strip()}
        for item in segments
        if item.text.strip()
    ]
    words = [
        {"start": word.start, "end": word.end, "text": word.word.strip()}
        for segment in segments
        for word in (segment.words or ())
        if word.word.strip()
    ]
    result = {
        "text": "".join(item["text"] for item in rows),
        "segments": rows,
        "words": words,
        "language": info.language,
        "language_probability": info.language_probability,
    }
    print("SECTVOICE_ASR_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
