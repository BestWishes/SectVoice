from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="批量回转写TTS一致性样本")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    report = json.loads(args.report.read_text(encoding="utf-8"))
    model = WhisperModel(
        str(args.model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, args.cpu_threads),
        local_files_only=True,
    )
    rows: list[dict[str, object]] = []
    for item in report["cases"]:
        audio_path = item.get("output_path") or item.get("output")
        if not audio_path:
            raise RuntimeError("case has no output_path or output audio")
        segments, _info = model.transcribe(
            str(audio_path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        transcript = "".join(segment.text.strip() for segment in segments)
        row = {**item, "output_path": str(audio_path), "asr_transcript": transcript}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    result = {"cases": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
