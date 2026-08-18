from __future__ import annotations

import argparse
import json
from pathlib import Path
import wave
from uuid import UUID, uuid4

import numpy as np

from sectvoice.core.runtime import _runtime_request_texts, _stable_voice_seed
from sectvoice.engines.process_client import EngineProcessClient, EngineProcessSpec


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为同一Basic声音和原文生成多个确定性种子候选"
    )
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--voice-id", type=UUID, required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--candidate-count", type=int, default=4)
    parser.add_argument("--max-request-chars", type=int, default=48)
    parser.add_argument("--sectvoice-root", type=Path)
    parser.add_argument(
        "--stable-first-seed",
        action="store_true",
        help="Use each candidate seed directly for the first synthesis path.",
    )
    args = parser.parse_args()

    text = args.text_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("诊断原文不能为空")
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    sectvoice_root = (args.sectvoice_root or args.artifact_dir / "runtime-root").resolve()
    temp_dir = sectvoice_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    environment = {
        "SECTVOICE_ROOT": str(sectvoice_root),
        "TEMP": str(temp_dir),
        "TMP": str(temp_dir),
        "PYTHONPYCACHEPREFIX": str(sectvoice_root / "cache" / "pycache" / "moss-basic-worker"),
    }
    client = EngineProcessClient(
        EngineProcessSpec(
            python_executable=args.engine_python,
            worker_script=args.package_dir / "worker.py",
            working_directory=args.package_dir,
            environment=environment,
            log_path=args.artifact_dir / "worker.log",
            startup_timeout_seconds=180.0,
        )
    )
    client.start(
        [
            "--model-dir",
            str(args.model_dir),
            "--output-dir",
            str(args.artifact_dir / "worker-output"),
            "--cpu-threads",
            "2",
        ]
    )
    rows: list[dict[str, object]] = []
    base_seed = _stable_voice_seed(args.voice_id)
    try:
        for candidate in range(max(1, args.candidate_count)):
            seed = (base_seed + candidate * 104729) & 0x7FFFFFFF
            data_parts: list[bytes] = []
            pcm_format = None
            request_texts = _runtime_request_texts(text, args.max_request_chars)
            for request_text in request_texts:
                chunks = client.stream_synthesis(
                    session_id=uuid4(),
                    generation_id=candidate + 1,
                    speech_unit_id=uuid4(),
                    text=request_text,
                    payload_path=args.payload_dir,
                    options={
                        "seed": seed,
                        "stable_first_seed": args.stable_first_seed,
                    },
                )
                for chunk in chunks:
                    if not chunk.data:
                        continue
                    pcm_format = pcm_format or chunk.pcm_format
                    if chunk.pcm_format != pcm_format:
                        raise RuntimeError("候选生成期间PCM格式改变")
                    data_parts.append(chunk.data)
            if pcm_format is None or not data_parts:
                raise RuntimeError("候选没有生成可听音频")
            samples = np.frombuffer(b"".join(data_parts), dtype="<f4")
            pcm16 = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
            output_path = args.artifact_dir / f"candidate-{candidate + 1}.wav"
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(pcm_format.channels)
                output.setsampwidth(2)
                output.setframerate(pcm_format.sample_rate)
                output.writeframes(pcm16.tobytes())
            row = {
                "candidate": candidate + 1,
                "seed": seed,
                "target_text": text,
                "output_path": str(output_path),
                "request_texts": request_texts,
                "stable_first_seed": args.stable_first_seed,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        client.stop()
    report = {"cases": rows}
    (args.artifact_dir / "candidates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
