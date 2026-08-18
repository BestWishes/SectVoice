from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import wave
from uuid import uuid4

import numpy as np
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


SYNTHESIS_TEXT = "声音档案只创建一次，之后就可以直接朗读任何新的文字。"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean-install distribution smoke test")
    parser.add_argument("--source-audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _application = QApplication(sys.argv[:1])
    paths = AppPaths.discover()
    services = build_services(paths)
    report: dict[str, object] = {
        "root": str(paths.root),
        "portable_python": str(
            paths.runtime / "common" / "python310" / "python.exe"
        ),
        "source_audio": str(args.source_audio.resolve()),
    }
    try:
        portable_python = Path(str(report["portable_python"]))
        if not portable_python.is_file():
            raise RuntimeError("Reader Core portable Python is missing")
        if not services.asr.is_available:
            raise RuntimeError("Reader Core ASR is unavailable")
        for tier in (Tier.BASIC, Tier.STANDARD):
            if services.packages.active_for(tier) is None:
                raise RuntimeError(f"{tier.value} package is not active")

        media = services.media.probe(args.source_audio)
        end_seconds = min(8.2, media.duration_seconds)
        transcript = services.asr.transcribe(args.source_audio).text.strip()
        if not transcript:
            raise RuntimeError("ASR returned empty reference text")
        profile = services.compiler.create_profile(
            name="发行内部QA声音",
            source_audio=args.source_audio,
            selection_start_seconds=0,
            selection_end_seconds=end_seconds,
            transcript=transcript,
        )
        tier_rows: list[dict[str, object]] = []
        for tier in (Tier.BASIC, Tier.STANDARD):
            payload = services.engines.compile_voice(profile, tier, services.compiler)
            if payload.status is not PayloadStatus.READY:
                raise RuntimeError(f"{tier.value} payload did not become ready")
            chunks = list(
                services.engines.for_tier(tier).client.stream_synthesis(
                    session_id=uuid4(),
                    generation_id=1,
                    speech_unit_id=uuid4(),
                    text=SYNTHESIS_TEXT,
                    payload_path=payload.opaque_path,
                    options={"speed": 1.0, "seed": 20260811},
                )
            )
            audible = [chunk for chunk in chunks if chunk.data]
            if not audible:
                raise RuntimeError(f"{tier.value} produced no audible chunks")
            pcm_format = audible[0].pcm_format
            raw = b"".join(chunk.data for chunk in audible)
            if pcm_format.sample_format == "f32le":
                floats = np.frombuffer(raw, dtype="<f4")
                pcm16 = np.round(np.clip(floats, -1, 1) * 32767).astype("<i2")
            elif pcm_format.sample_format == "s16le":
                pcm16 = np.frombuffer(raw, dtype="<i2")
            else:
                raise RuntimeError(f"unsupported PCM: {pcm_format.sample_format}")
            rms = float(np.sqrt(np.mean(pcm16.astype(np.float64) ** 2)))
            if pcm16.size == 0 or rms < 20:
                raise RuntimeError(f"{tier.value} output is silent")
            preview = args.output.parent / f"{tier.value}-distribution-smoke.wav"
            with wave.open(str(preview), "wb") as output:
                output.setnchannels(pcm_format.channels)
                output.setsampwidth(2)
                output.setframerate(pcm_format.sample_rate)
                output.writeframes(pcm16.tobytes())
            tier_rows.append(
                {
                    "tier": tier.value,
                    "payload_status": payload.status.value,
                    "preview": str(preview),
                    "preview_bytes": preview.stat().st_size,
                    "audio_seconds": pcm16.size
                    / (pcm_format.sample_rate * pcm_format.channels),
                    "rms_pcm16": rms,
                }
            )
            services.engines.unload_tier(tier)
        report.update(
            {
                "passed": True,
                "asr_text": transcript,
                "voice_id": str(profile.voice_id),
                "tiers": tier_rows,
            }
        )
    except Exception as exc:
        report.update({"passed": False, "error": str(exc)})
    finally:
        services.asr.shutdown()
        services.engines.shutdown()

    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
