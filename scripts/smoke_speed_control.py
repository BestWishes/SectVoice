from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


TEXT = "语速调整必须真实改变朗读速度，同时尽量保持人物原来的音高。"


def main() -> int:
    parser = argparse.ArgumentParser(description="真实语速控制smoke")
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QApplication(sys.argv[:1])
    paths = AppPaths.discover()
    services = build_services(paths)
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError("语音包未启用")
    voice = next(
        profile
        for profile in services.voices.list_profiles()
        if any(
            payload.tier is tier
            and payload.engine_id == active.engine_id
            and payload.status is PayloadStatus.READY
            for payload in services.voices.payloads_for(profile.voice_id)
        )
    )
    payload = next(
        payload
        for payload in services.voices.payloads_for(voice.voice_id)
        if payload.tier is tier
        and payload.engine_id == active.engine_id
        and payload.status is PayloadStatus.READY
    )
    handle = services.engines.for_tier(tier)
    output_dir = args.output.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(speed: float) -> tuple[float, object, bytes]:
        chunks = list(
            handle.client.stream_synthesis(
                session_id=uuid4(),
                generation_id=int(speed * 100),
                speech_unit_id=uuid4(),
                text=TEXT,
                payload_path=payload.opaque_path,
                options={"speed": speed, "seed": 20260810},
            )
        )
        audible = [chunk for chunk in chunks if chunk.data]
        return sum(chunk.duration_seconds for chunk in audible), audible[0].pcm_format, b"".join(
            chunk.data for chunk in audible
        )

    if tier is Tier.STANDARD:
        slow, _pcm, _raw = synthesize(0.75)
        normal, _pcm, _raw = synthesize(1.0)
        fast, _pcm, _raw = synthesize(1.5)
        mode = "engine-native"
    else:
        normal, pcm, raw = synthesize(1.0)
        source = output_dir / "normal.pcm"
        source.write_bytes(raw)
        durations = {}
        for speed in (0.75, 1.5):
            destination = output_dir / f"speed-{speed}.pcm"
            services.media.time_stretch_pcm(
                source,
                destination,
                speed=speed,
                sample_rate=pcm.sample_rate,
                channels=pcm.channels,
                sample_format=pcm.sample_format,
            )
            durations[speed] = destination.stat().st_size / (
                pcm.sample_rate * pcm.bytes_per_frame
            )
        slow = durations[0.75]
        fast = durations[1.5]
        mode = "ffmpeg-rubberband-formant-preserving"
    services.asr.shutdown()
    services.engines.shutdown()
    passed = slow > normal * 1.15 and fast < normal * 0.9
    result = {
        "passed": passed,
        "tier": tier.value,
        "mode": mode,
        "duration_at_0.75": slow,
        "duration_at_1.0": normal,
        "duration_at_1.5": fast,
        "slow_to_normal_ratio": slow / normal,
        "fast_to_normal_ratio": fast / normal,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
