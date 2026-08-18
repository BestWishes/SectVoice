from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import wave
from uuid import uuid4

import numpy as np

from sectvoice.engines.process_client import EngineProcessClient, EngineProcessSpec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--reference-wav", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--sectvoice-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
    try:
        payload_dir = args.artifact_dir / "private-payload"
        compiled = client.request(
            "compile",
            timeout_seconds=120,
            reference_wav=str(args.reference_wav),
            destination=str(payload_dir),
        )
        print(f"COMPILED={compiled['result']}")
        session_id = uuid4()
        unit_id = uuid4()
        chunks = list(
            client.stream_synthesis(
                session_id=session_id,
                generation_id=1,
                speech_unit_id=unit_id,
                text="独立工作进程已经开始连续发送声音数据。",
                payload_path=payload_dir,
                options={"seed": 20260809},
            )
        )
        assert chunks
        assert all(chunk.speech_unit_id == unit_id for chunk in chunks)
        assert all(chunk.session_id == session_id for chunk in chunks)
        assert chunks[-1].is_final
        audio_chunks = [chunk for chunk in chunks if chunk.data]
        assert audio_chunks
        pcm_format = audio_chunks[0].pcm_format
        floats = np.frombuffer(b"".join(chunk.data for chunk in audio_chunks), dtype="<f4")
        pcm16 = np.round(np.clip(floats, -1, 1) * 32767).astype("<i2")
        output_path = args.artifact_dir / "ipc-smoke.wav"
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(pcm_format.channels)
            output.setsampwidth(2)
            output.setframerate(pcm_format.sample_rate)
            output.writeframes(pcm16.tobytes())
        print(f"AUDIO={output_path}")
        print(f"CHUNKS={len(audio_chunks)}")

        cancel_session = uuid4()
        cancel_stream = client.stream_synthesis(
            session_id=cancel_session,
            generation_id=2,
            speech_unit_id=uuid4(),
            text="这是取消响应测试。" * 30,
            payload_path=payload_dir,
            options={"seed": 20260810},
        )
        first_chunk = next(cancel_stream)
        assert first_chunk.data
        cancel_started = time.perf_counter()
        client.cancel(cancel_session, 2)
        trailing_chunks = list(cancel_stream)
        cancel_elapsed = time.perf_counter() - cancel_started
        trailing_count = len([item for item in trailing_chunks if item.data])
        print(f"CANCEL_SECONDS={cancel_elapsed:.4f}")
        print(f"TRAILING_CHUNKS_AFTER_CANCEL={trailing_count}")
        report = {
            "passed": trailing_count == 0,
            "engine": "moss-nano-onnx",
            "audio_path": str(output_path),
            "payload_path": str(payload_dir),
            "audio_seconds": sum(item.duration_seconds for item in audio_chunks),
            "chunks": len(audio_chunks),
            "cancel_ack_and_drain_seconds": cancel_elapsed,
            "trailing_chunks_after_cancel": trailing_count,
        }
        (args.artifact_dir / "package-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
