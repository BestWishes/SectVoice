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
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


TEST_TEXT = (
    "第一段声音会立即被后来的跳转取消。"
    "第二段也不能在过期以后重新进入播放队列。"
    "最终只应该听到这一段：声音资料创建一次，就能直接朗读任何新的文字。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 Reader 声卡输出与快速跳转冒烟测试")
    parser.add_argument("--tier", choices=("Basic", "Standard"), default="Basic")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--voice-id", type=UUID)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
    tier = Tier(args.tier.lower())
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
    document = services.documents.create("Reader声卡输出验收", TEST_TEXT)
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, tier)

    states: list[dict[str, object]] = []
    starts: list[dict[str, object]] = []
    errors: list[str] = []
    started_at = time.perf_counter()
    expected_position = TEST_TEXT.index("最终")
    expected_unit, _ = document.mapping.locate(expected_position)

    def state_changed(state: str, message: str) -> None:
        states.append({"at": time.perf_counter() - started_at, "state": state, "message": message})

    def unit_changed(unit_id: str, start: int, end: int) -> None:
        starts.append(
            {
                "at": time.perf_counter() - started_at,
                "speech_unit_id": unit_id,
                "start": start,
                "end": end,
            }
        )

    def finish() -> None:
        app.quit()

    def fail(message: str) -> None:
        errors.append(message)
        app.quit()

    controller.stateChanged.connect(state_changed)
    controller.currentUnitChanged.connect(unit_changed)
    controller.error.connect(fail)
    controller.finished.connect(finish)

    first_position = 0
    second_position = TEST_TEXT.index("第二段")
    QTimer.singleShot(0, lambda: controller.play_from(first_position))
    QTimer.singleShot(25, lambda: controller.play_from(second_position))
    QTimer.singleShot(50, lambda: controller.play_from(expected_position))
    QTimer.singleShot(int(args.timeout * 1000), lambda: fail("测试超时"))
    app.exec()

    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute("DELETE FROM documents WHERE document_id=?", (str(document.document_id),))

    result = {
        "tier": tier.value,
        "voice_id": str(voice.voice_id),
        "elapsed_seconds": time.perf_counter() - started_at,
        "expected_position": expected_position,
        "expected_speech_unit_id": str(expected_unit.speech_unit_id),
        "states": states,
        "started_units": starts,
        "errors": errors,
        "passed": not errors
        and bool(starts)
        and all(UUID(item["speech_unit_id"]) == expected_unit.speech_unit_id for item in starts),
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
