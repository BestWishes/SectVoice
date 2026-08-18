from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np


CASES = (
    (
        "nightfall_window",
        "趁着晚霞，许川将《赤血刀法》的秘籍拿出来。"
        "准备先修炼一番。"
        "秘境之中有机缘，也有危险。"
        "多一门攻击手段也更能保证自己和苏妙音的安全。",
    ),
    ("single_bang", "“砰——”"),
    ("single_word", "任意词。"),
    ("very_short", "到了。"),
    ("short", "真是无事一身轻。"),
    ("short_comma", "他的目的地，还是太一城。"),
    ("medium", "此时许川的心情已经没有之前那般急迫和焦躁。"),
    ("medium_commas", "回到住处，许川收拾好东西，再度走出了圣地山门。"),
    ("long", "那他就又要回到那种苦于缺少修炼资源，进境缓慢的日子中去了。"),
    ("longer", "作为一座人族巨城，太一城每日的人流量达到了一个恐怖的地步，根本不是圣地内的弟子能比得上的。"),
    ("split_tail", "而如今，他修炼玄阳决，铸就雄厚根基，武道之路不说一路平坦，至少不会在一个境界浪费几年光阴，而不得。"),
)


def _load_engine(package_dir: Path):
    sys.path.insert(0, str(package_dir))
    spec = importlib.util.spec_from_file_location(
        "sectvoice_basic_consistency_runtime", package_dir / "runtime_engine.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Basic runtime module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wav(path: Path, raw: bytes, sample_rate: int, channels: int) -> None:
    floats = np.frombuffer(raw, dtype="<f4")
    pcm = np.round(np.clip(floats, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="比较Basic采样模式的短长句稳定性")
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--modes", nargs="+", choices=("fixed", "greedy"), default=("fixed", "greedy"))
    parser.add_argument("--case-ids", nargs="+", choices=tuple(item[0] for item in CASES))
    parser.add_argument("--seeds", nargs="+", type=int, default=(20260810,))
    parser.add_argument(
        "--stable-first-seed",
        action="store_true",
        help="Use the VoiceProfile-wide first path used by Chinese Basic narration.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    module = _load_engine(args.package_dir.resolve())
    engine = module.MossNanoEngine(
        model_dir=args.model_dir,
        output_dir=args.output_dir / "engine-output",
        cpu_threads=args.cpu_threads,
    )
    loaded_at = time.perf_counter()
    engine.load()
    report: dict[str, object] = {
        "load_seconds": time.perf_counter() - loaded_at,
        "cases": [],
    }
    assert engine.runtime is not None
    report_path = args.output_dir / "report.json"
    selected_cases = tuple(
        item for item in CASES if args.case_ids is None or item[0] in args.case_ids
    )
    for mode in args.modes:
        engine.runtime.manifest["generation_defaults"]["sample_mode"] = mode
        engine.runtime.manifest["generation_defaults"]["do_sample"] = mode != "greedy"
        for seed in args.seeds:
            for index, (case_id, text) in enumerate(selected_cases):
                chunks: list[bytes] = []
                sample_rate = 0
                channels = 0

                def emit(
                    pcm: bytes,
                    rate: int,
                    channel_count: int,
                    _duration: float,
                    _is_pause: bool,
                ) -> None:
                    nonlocal sample_rate, channels
                    sample_rate = rate
                    channels = channel_count
                    chunks.append(pcm)

                started = time.perf_counter()
                result = engine.synthesize_stream(
                    text=text,
                    payload_dir=args.payload_dir,
                    emit=emit,
                    seed=seed,
                    stable_first_seed=args.stable_first_seed,
                )
                elapsed = time.perf_counter() - started
                output_path = args.output_dir / f"{mode}-{seed}-{index:02d}-{case_id}.wav"
                _write_wav(output_path, b"".join(chunks), sample_rate, channels)
                row = {
                    "mode": mode,
                    "seed": seed,
                    "case_id": case_id,
                    "text": text,
                    "characters": sum(character.isalnum() for character in text),
                    "elapsed_seconds": elapsed,
                    "audio_seconds": result["audio_seconds"],
                    "generated_frames": result["generated_frames"],
                    "retry_counts": result.get("retry_counts", []),
                    "stable_first_seed": args.stable_first_seed,
                    "output_path": str(output_path),
                }
                report["cases"].append(row)
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
