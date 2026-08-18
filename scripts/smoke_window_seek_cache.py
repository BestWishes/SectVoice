from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths


TEXT = "清晨的风吹过庭院。年轻弟子推门而出。他准备开始今天的修炼。"


def main() -> int:
    parser = argparse.ArgumentParser(description="真实GenerationWindow中部缓存跳转测试")
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--voice-id", type=UUID)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError(f"{tier.value} 语音包未启用")
    voice = next(
        (
            profile
            for profile in services.voices.list_profiles()
            if args.voice_id is None or profile.voice_id == args.voice_id
            if any(
                payload.tier is tier
                and payload.engine_id == active.engine_id
                and payload.status is PayloadStatus.READY
                for payload in services.voices.payloads_for(profile.voice_id)
            )
        ),
        None,
    )
    if voice is None:
        raise RuntimeError(f"没有可供 {tier.value} 测试的真实声音数据")
    document = services.documents.create("v12窗口中部缓存跳转", TEXT)
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, tier)
    controller.set_settings(
        SynthesisSettings(punctuation_pause_ms=120, paragraph_pause_ms=280)
    )
    expected_units = document.mapping.units
    second_position = TEXT.index("年轻")
    expected_suffix = tuple(item.speech_unit_id for item in expected_units[1:])
    phase = 1
    first_started: list[str] = []
    seek_started: list[str] = []
    seek_states: list[dict[str, object]] = []
    errors: list[str] = []
    seek_started_at = 0.0
    seek_first_audio_seconds: float | None = None
    launched_at = time.perf_counter()

    def unit_started(unit_id: str, _start: int, _end: int) -> None:
        nonlocal seek_first_audio_seconds
        if phase == 1:
            first_started.append(unit_id)
            return
        seek_started.append(unit_id)
        if seek_first_audio_seconds is None:
            seek_first_audio_seconds = time.perf_counter() - seek_started_at

    def state_changed(state: str, message: str) -> None:
        if phase == 2:
            seek_states.append(
                {
                    "at_seconds": time.perf_counter() - seek_started_at,
                    "state": state,
                    "message": message,
                }
            )

    def finished() -> None:
        nonlocal phase, seek_started_at
        if phase == 1:
            phase = 2
            seek_started_at = time.perf_counter()
            QTimer.singleShot(30, lambda: controller.play_from(second_position))
            return
        app.quit()

    def failed(message: str) -> None:
        errors.append(message)
        app.quit()

    controller.currentUnitChanged.connect(unit_started)
    controller.stateChanged.connect(state_changed)
    controller.finished.connect(finished)
    controller.error.connect(failed)
    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(int(args.timeout * 1000), lambda: failed("测试超时"))
    app.exec()

    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute(
            "DELETE FROM documents WHERE document_id=?", (str(document.document_id),)
        )

    seek_ids = tuple(UUID(item) for item in seek_started)
    generated_during_seek = any(
        item["state"] in {"LoadingModel", "Generating"} for item in seek_states
    )
    result = {
        "tier": tier.value,
        "voice_id": str(voice.voice_id),
        "elapsed_seconds": time.perf_counter() - launched_at,
        "first_started_units": first_started,
        "seek_started_units": seek_started,
        "expected_seek_units": [str(item) for item in expected_suffix],
        "seek_first_audio_seconds": seek_first_audio_seconds,
        "generated_during_seek": generated_during_seek,
        "seek_states": seek_states,
        "errors": errors,
        "passed": (
            not errors
            and seek_ids == expected_suffix
            and seek_first_audio_seconds is not None
            and seek_first_audio_seconds < 1.0
            and not generated_during_seek
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
