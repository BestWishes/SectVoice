from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event, Thread
import time
import wave
from uuid import uuid4

from sectvoice.engines.process_client import EngineProcessClient, EngineProcessSpec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-python", required=True, type=Path)
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--sectvoice-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    if args.sectvoice_root is None:
        args.sectvoice_root = (args.artifact_dir / "runtime-root").resolve()
    engine_runtime = args.package_dir.parent
    environment = {
        "SECTVOICE_ROOT": str(args.sectvoice_root),
        "HF_HOME": str(args.sectvoice_root / "cache" / "huggingface"),
        "TORCH_HOME": str(args.sectvoice_root / "cache" / "torch"),
        "XDG_CACHE_HOME": str(args.sectvoice_root / "cache" / "xdg"),
        "TEMP": str(args.sectvoice_root / "temp"),
        "TMP": str(args.sectvoice_root / "temp"),
        "PYTHONPYCACHEPREFIX": str(
            args.sectvoice_root / "cache" / "pycache" / "standard-ipc"
        ),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    portable_python = (
        args.sectvoice_root / "runtime" / "common" / "python310" / "python.exe"
    )
    if args.engine_python.resolve() == portable_python.resolve():
        environment.update(
            {
                "PYTHONHOME": str(portable_python.parent),
                "PYTHONPATH": str(
                    engine_runtime / ".venv" / "Lib" / "site-packages"
                ),
                "PYTHONNOUSERSITE": "1",
            }
        )
    package_nltk = engine_runtime / ".nltk_data"
    if package_nltk.is_dir():
        environment["NLTK_DATA"] = str(package_nltk)
    client = EngineProcessClient(
        EngineProcessSpec(
            python_executable=args.engine_python,
            worker_script=args.package_dir / "worker.py",
            working_directory=args.package_dir,
            environment=environment,
            log_path=args.artifact_dir / "worker.log",
            startup_timeout_seconds=180,
        )
    )
    client.start(
        [
            "--app-root",
            str(args.app_root),
            "--model-dir",
            str(args.model_dir),
            "--output-dir",
            str(args.artifact_dir / "engine-output"),
        ]
    )
    session = uuid4()
    generation = 1
    try:
        started = time.perf_counter()
        client.request("load", timeout_seconds=180)
        load_seconds = time.perf_counter() - started
        compiled = client.request(
            "compile",
            timeout_seconds=180,
            reference_wav=str(args.reference),
            reference_transcript=args.transcript,
            language="zh-CN",
            destination=str(args.artifact_dir / "payload"),
        )["result"]
        started = time.perf_counter()
        chunks = list(
            client.stream_synthesis(
                session_id=session,
                generation_id=generation,
                speech_unit_id=uuid4(),
                text="独立工作进程已经生成了真正的中级克隆语音。",
                payload_path=Path(compiled["opaque_path"]),
                options={"seed": 20260810, "speed": 1.0},
            )
        )
        elapsed = time.perf_counter() - started
        audio = [chunk for chunk in chunks if chunk.data]
        output_path = args.artifact_dir / "standard-ipc-smoke.wav"
        with wave.open(str(output_path), "wb") as target:
            target.setnchannels(audio[0].pcm_format.channels)
            target.setsampwidth(2)
            target.setframerate(audio[0].pcm_format.sample_rate)
            target.writeframes(b"".join(chunk.data for chunk in audio))

        cancel_session = uuid4()
        cancel_generation = 2
        first_chunk = Event()
        cancel_done = Event()
        trailing = []

        def consume() -> None:
            try:
                for chunk in client.stream_synthesis(
                    session_id=cancel_session,
                    generation_id=cancel_generation,
                    speech_unit_id=uuid4(),
                    text="这是一段用于验证快速跳转取消的较长文字。" * 10,
                    payload_path=Path(compiled["opaque_path"]),
                    options={"seed": 20260811, "speed": 1.0},
                ):
                    if chunk.data and not first_chunk.is_set():
                        first_chunk.set()
                    elif cancel_done.is_set() and chunk.data:
                        trailing.append(chunk)
            finally:
                cancel_done.set()

        thread = Thread(target=consume, daemon=True)
        thread.start()
        if not first_chunk.wait(15):
            raise RuntimeError("cancel smoke did not receive first audio")
        cancel_started = time.perf_counter()
        client.cancel(cancel_session, cancel_generation)
        cancel_seconds = time.perf_counter() - cancel_started
        cancel_done.set()
        thread.join(timeout=15)
        if thread.is_alive():
            raise RuntimeError("cancelled Standard generation did not stop")

        report = {
            "passed": True,
            "engine": "gpt-sovits-v2proplus",
            "load_seconds": load_seconds,
            "generation_seconds": elapsed,
            "audio_seconds": sum(chunk.duration_seconds for chunk in audio),
            "audio_path": str(output_path),
            "payload_path": compiled["opaque_path"],
            "cancel_ack_seconds": cancel_seconds,
            "trailing_chunks_after_cancel": len(trailing),
        }
        marker = args.artifact_dir / "package-smoke.json"
        marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
