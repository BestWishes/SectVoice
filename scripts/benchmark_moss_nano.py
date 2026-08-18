from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
import time
import wave

import numpy as np
import psutil


def load_engine_module(package_dir: Path):
    sys.path.insert(0, str(package_dir))
    module_path = package_dir / "runtime_engine.py"
    spec = importlib.util.spec_from_file_location("sectvoice_moss_runtime", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_wav(path: Path, pcm_chunks: list[bytes], sample_rate: int, channels: int) -> None:
    floats = np.frombuffer(b"".join(pcm_chunks), dtype="<f4")
    pcm16 = np.round(np.clip(floats, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm16.tobytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--reference-wav", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--cpu-threads", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module = load_engine_module(args.package_dir.resolve())
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    payload_dir = args.artifact_dir / "private-payload"
    engine = module.MossNanoEngine(
        model_dir=args.model_dir,
        output_dir=args.artifact_dir / "engine-output",
        cpu_threads=args.cpu_threads,
    )
    process = psutil.Process(os.getpid())
    report: dict[str, object] = {
        "engine_id": module.ENGINE_ID,
        "engine_version": module.ENGINE_VERSION,
        "device": "cpu",
        "cpu_threads": args.cpu_threads,
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpu_count": psutil.cpu_count(),
            "ram_gib": round(psutil.virtual_memory().total / (1024**3), 2),
        },
        "cases": [],
    }
    started = time.perf_counter()
    engine.load()
    report["cold_load_seconds"] = time.perf_counter() - started
    report["loaded_rss_mib"] = process.memory_info().rss / (1024**2)

    compile_started = time.perf_counter()
    compile_result = engine.compile_voice(args.reference_wav, payload_dir)
    report["compile_seconds"] = time.perf_counter() - compile_started
    report["compile_result"] = compile_result

    cases = [
        ("short", "夜色渐深，远处传来几声清脆的风铃。"),
        ("dialogue", "掌柜抬起头说道：“客官，您要的茶已经备好了。”"),
        ("mixed", "系统将在2026年8月9日保存到H盘，版本是Reader 1.0。"),
        ("long", "山路在细雨中显得格外安静，年轻人放慢脚步，望着远处灯火微明的小镇，心里忽然想起许多年前那个同样潮湿的黄昏。"),
    ]
    for index, (case_id, text) in enumerate(cases):
        pcm_chunks: list[bytes] = []
        first_audio_seconds: float | None = None
        sample_rate = 0
        channels = 0
        audio_seconds = 0.0
        synthesis_started = time.perf_counter()

        def emit(pcm: bytes, rate: int, channel_count: int, duration: float, is_pause: bool) -> None:
            del is_pause
            nonlocal first_audio_seconds, sample_rate, channels, audio_seconds
            if first_audio_seconds is None and pcm:
                first_audio_seconds = time.perf_counter() - synthesis_started
            sample_rate = rate
            channels = channel_count
            audio_seconds += duration
            pcm_chunks.append(pcm)

        result = engine.synthesize_stream(
            text=text,
            payload_dir=payload_dir,
            emit=emit,
            seed=20260809 + index,
        )
        elapsed = time.perf_counter() - synthesis_started
        output_path = args.artifact_dir / f"{index:02d}-{case_id}.wav"
        write_wav(output_path, pcm_chunks, sample_rate, channels)
        case = {
            "case_id": case_id,
            "text": text,
            "elapsed_seconds": elapsed,
            "first_audio_seconds": first_audio_seconds,
            "audio_seconds": audio_seconds,
            "rtf": elapsed / audio_seconds if audio_seconds else None,
            "rss_mib": process.memory_info().rss / (1024**2),
            "output_path": str(output_path),
            "engine_result": result,
        }
        report["cases"].append(case)
        print(json.dumps(case, ensure_ascii=False), flush=True)

    report_path = args.artifact_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

