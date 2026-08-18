from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback


RESULT_PREFIX = "SECTVOICE_ASR_WORKER_RESULT="


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--cpu-threads", default=4, type=int)
    return parser.parse_args()


def _transcribe(model, request: dict[str, object]) -> dict[str, object]:
    segments, info = model.transcribe(
        str(request["audio"]),
        language=str(request.get("language") or "zh"),
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=bool(request.get("word_timestamps")),
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
    return {
        "text": "".join(item["text"] for item in rows),
        "segments": rows,
        "words": words,
        "language": info.language,
        "language_probability": info.language_probability,
    }


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
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            if request.get("command") == "shutdown":
                return 0
            request_id = str(request.get("request_id") or "")
            if not request_id:
                raise ValueError("missing request_id")
            response = {
                "request_id": request_id,
                "ok": True,
                "result": _transcribe(model, request),
            }
        except Exception as exc:
            request_id = str(locals().get("request", {}).get("request_id") or "")
            traceback.print_exc(file=sys.stderr)
            response = {
                "request_id": request_id,
                "ok": False,
                "error": str(exc),
            }
        print(RESULT_PREFIX + json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
