from __future__ import annotations

import argparse
from pathlib import Path
import wave
from uuid import uuid4

import numpy as np

from sectvoice.core.database import Database
from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.voice_compiler import CompilationTarget, VoiceCompiler
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import Tier
from sectvoice.engines.process_client import EngineProcessClient, EngineProcessSpec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-audio", type=Path, required=True)
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--sectvoice-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    sectvoice_root = (args.sectvoice_root or args.artifact_dir / "runtime-root").resolve()
    temp_dir = sectvoice_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    database = Database(args.artifact_dir / "compiler-smoke.db")
    database.initialize()
    library = VoiceLibrary(database)
    compiler = VoiceCompiler(
        library,
        FFmpegProcessor(args.ffmpeg),
        args.artifact_dir / "voice-library",
    )
    client = EngineProcessClient(
        EngineProcessSpec(
            python_executable=args.engine_python,
            worker_script=args.package_dir / "worker.py",
            working_directory=args.package_dir,
            environment={
                "SECTVOICE_ROOT": str(sectvoice_root),
                "TEMP": str(temp_dir),
                "TMP": str(temp_dir),
                "PYTHONPYCACHEPREFIX": str(sectvoice_root / "cache" / "pycache" / "moss-basic-worker"),
            },
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
        profile = compiler.create_profile(
            name="真实编译测试声音",
            source_audio=args.source_audio,
            selection_start_seconds=0,
            selection_end_seconds=7.8,
            transcript="欢迎关注模思智能、上海创智学院与复旦大学自然语言处理实验室。",
        )
        payload = compiler.compile_payload(
            profile,
            CompilationTarget(
                tier=Tier.BASIC,
                engine_id="moss-nano-onnx",
                engine_version="cc7bdf19",
                payload_format_version="moss-prompt-codes-v1",
                client=client,
            ),
        )
        print(f"VOICE_ID={profile.voice_id}")
        print(f"REFERENCE_SECONDS={profile.reference_duration_seconds}")
        print(f"PAYLOAD={payload.opaque_path}")
        chunks = list(
            client.stream_synthesis(
                session_id=uuid4(),
                generation_id=1,
                speech_unit_id=uuid4(),
                text="声音创建完成以后，可以直接朗读任何新的文字。",
                payload_path=payload.opaque_path,
                options={"seed": 20260810},
            )
        )
        audio = [item for item in chunks if item.data]
        floats = np.frombuffer(b"".join(item.data for item in audio), dtype="<f4")
        pcm16 = np.round(np.clip(floats, -1, 1) * 32767).astype("<i2")
        output_path = args.artifact_dir / "voice-compiler-smoke.wav"
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(audio[0].pcm_format.channels)
            output.setsampwidth(2)
            output.setframerate(audio[0].pcm_format.sample_rate)
            output.writeframes(pcm16.tobytes())
        print(f"AUDIO={output_path}")
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

