from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from threading import Event
import time
import wave

import psutil


CASES = (
    "声音资料只创建一次，之后可以直接朗读任何新的中文文本。",
    "山风穿过竹林，远处的钟声一下一下落在江面上。",
    "张掌柜问：“客官，是住店，还是打尖？”少年答道：“先来一壶热茶。”",
    "连续朗读依靠几秒钟的音频缓冲，而不是提前生成整篇小说。",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--streaming", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(args.package_dir))
    from runtime_engine import GptSovitsEngine

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    engine = GptSovitsEngine(
        app_root=args.app_root,
        model_dir=args.model_dir,
        output_dir=args.artifact_dir / "engine-output",
    )
    process = psutil.Process()
    started = time.perf_counter()
    engine.load()
    load_seconds = time.perf_counter() - started
    payload_dir = args.artifact_dir / "payload"
    started = time.perf_counter()
    payload = engine.compile_voice(args.reference, args.transcript, "zh-CN", payload_dir)
    compile_seconds = time.perf_counter() - started
    case_rows = []
    for index, text in enumerate(CASES):
        parts: list[bytes] = []
        first_audio: float | None = None
        generation_started = time.perf_counter()

        def emit(pcm: bytes, _rate: int, _channels: int, _seconds: float, _pause: bool) -> None:
            nonlocal first_audio
            if first_audio is None:
                first_audio = time.perf_counter() - generation_started
            parts.append(pcm)

        result = engine.synthesize_stream(
            text=text,
            payload_dir=payload_dir,
            emit=emit,
            cancel_event=Event(),
            seed=20260810 + index,
            streaming_mode=args.streaming,
        )
        elapsed = time.perf_counter() - generation_started
        output = args.artifact_dir / f"case-{index + 1}.wav"
        with wave.open(str(output), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(int(result["sample_rate"]))
            target.writeframes(b"".join(parts))
        case_rows.append(
            {
                "text": text,
                "output": str(output),
                "first_audio_seconds": first_audio,
                "generation_seconds": elapsed,
                "audio_seconds": result["audio_seconds"],
                "rtf": elapsed / float(result["audio_seconds"]),
                "chunks": result["chunks"],
                "rss_mib": process.memory_info().rss / 1024**2,
            }
        )
    report = {
        "engine": "gpt-sovits-v2proplus",
        "load_seconds": load_seconds,
        "compile_seconds": compile_seconds,
        "payload": payload,
        "cases": case_rows,
        "rss_mib": process.memory_info().rss / 1024**2,
    }
    report_path = args.artifact_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT=" + str(report_path), flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    engine.unload()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
