from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import tempfile
import wave
from uuid import uuid4

from sectvoice.core.builtin_voices import load_builtin_voice_specs, seed_builtin_voices
from sectvoice.core.database import Database
from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.settings import SettingsStore
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageService
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在全新用户目录中验证两种内置声音的基础/中级真实生成"
    )
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument(
        "--asset-root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "src"
        / "sectvoice"
        / "assets"
        / "builtin_voices",
    )
    parser.add_argument("--text", default="欢迎使用本地声音朗读器。")
    return parser.parse_args()


def _write_wav(path: Path, chunks) -> tuple[float, float]:
    audio = [chunk for chunk in chunks if chunk.data]
    if not audio:
        raise RuntimeError("引擎没有返回音频数据")
    pcm = audio[0].pcm_format
    if any(chunk.pcm_format != pcm for chunk in audio):
        raise RuntimeError("同一次生成返回了不一致的PCM格式")
    raw = b"".join(chunk.data for chunk in audio)
    if pcm.sample_format == "f32le":
        floats = array("f")
        floats.frombytes(raw)
        samples = array(
            "h",
            (
                max(-32768, min(32767, round(value * 32767)))
                for value in floats
            ),
        )
    elif pcm.sample_format == "s16le":
        samples = array("h")
        samples.frombytes(raw)
    else:
        raise RuntimeError(f"不支持的PCM格式：{pcm.sample_format}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(pcm.channels)
        target.setsampwidth(2)
        target.setframerate(pcm.sample_rate)
        target.writeframes(samples.tobytes())
    duration = len(samples) / pcm.channels / pcm.sample_rate
    rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
    return duration, rms


def main() -> int:
    args = parse_args()
    installed = args.installed_root.resolve()
    artifacts = args.artifact_dir.resolve()
    assets = args.asset_root.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[1]
    package_records = (
        (
            installed
            / "runtime/engines/basic/moss-nano-onnx/cc7bdf19/package/package-manifest.json",
            installed / "runtime/engines/basic/moss-nano-onnx/cc7bdf19",
            installed / "models/basic/moss-nano-onnx/f52645cb",
        ),
        (
            installed
            / "runtime/engines/standard/gpt-sovits/d523079f/package/package-manifest.json",
            installed / "runtime/engines/standard/gpt-sovits/d523079f",
            installed / "models/standard/gpt-sovits-v2proplus/336b2ec4",
        ),
    )
    report: dict[str, object] = {"seed": [], "voices": []}
    with tempfile.TemporaryDirectory(
        dir=artifacts, prefix="clean-user-root-"
    ) as temporary:
        clean_root = Path(temporary)
        paths = AppPaths(
            root=clean_root,
            source=source_root,
            runtime=installed / "runtime",
            models=installed / "models",
            data=clean_root / "data",
            cache=clean_root / "cache",
            downloads=clean_root / "downloads",
            temp=clean_root / "temp",
            artifacts=clean_root / "artifacts",
        )
        paths.ensure_writable_directories()
        database = Database(paths.data / "sectvoice.db")
        database.initialize()
        voices = VoiceLibrary(database)
        settings = SettingsStore(database)
        voice_packages = VoicePackageService(voices, paths.data / "voices")
        seeded = seed_builtin_voices(voices, voice_packages, settings, assets)
        report["seed"] = [
            {"voice_id": str(item.voice_id), "action": item.action}
            for item in seeded
        ]
        packages = ModelPackageManager(database)
        for manifest, runtime, model in package_records:
            record = packages.register(manifest, runtime, model)
            packages.activate(record.package_id)
        engines = EngineManager(paths, packages)
        results: list[dict[str, object]] = []
        try:
            for tier in (Tier.BASIC, Tier.STANDARD):
                active = packages.active_for(tier)
                if active is None:
                    raise RuntimeError(f"{tier.value}引擎未启用")
                handle = engines.for_tier(tier)
                for spec in load_builtin_voice_specs(assets):
                    profile = voices.get(spec.voice_id)
                    if profile is None:
                        raise RuntimeError(f"内置声音没有导入：{spec.name}")
                    payload = next(
                        item
                        for item in voices.payloads_for(spec.voice_id)
                        if item.tier is tier
                        and item.engine_id == active.engine_id
                        and item.status is PayloadStatus.READY
                    )
                    chunks = list(
                        handle.client.stream_synthesis(
                            session_id=uuid4(),
                            generation_id=1,
                            speech_unit_id=uuid4(),
                            text=args.text,
                            payload_path=payload.opaque_path,
                            options={"seed": 20260819, "speed": 1.0},
                        )
                    )
                    output = artifacts / f"{tier.value}-{spec.package_name[:-9]}.wav"
                    duration, rms = _write_wav(output, chunks)
                    if duration <= 0.2 or rms <= 50:
                        raise RuntimeError(f"{spec.name}/{tier.value}生成了无效音频")
                    results.append(
                        {
                            "voice_id": str(spec.voice_id),
                            "name": profile.name,
                            "tier": tier.value,
                            "engine_version": active.engine_version,
                            "payload_relocated": payload.opaque_path.resolve().is_relative_to(
                                clean_root.resolve()
                            ),
                            "duration_seconds": round(duration, 3),
                            "rms_pcm16": round(rms, 1),
                            "wav": str(output),
                            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                        }
                    )
                engines.unload_tier(tier)
        finally:
            engines.shutdown()
        report["voices"] = results
    report["passed"] = len(report["voices"]) == 4 and all(
        bool(item["payload_relocated"]) for item in report["voices"]
    )
    report_path = artifacts / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
