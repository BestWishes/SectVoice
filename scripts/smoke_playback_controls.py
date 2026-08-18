from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import SynthesisSettings, Tier
from sectvoice.paths import AppPaths


TEXT = "第一句用于验证暂停以后播放时钟不会继续前进。第二句用于验证下一句和停止控制。"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])
    paths = AppPaths.discover()
    services = build_services(paths)
    voice = services.voices.list_profiles()[0]
    document = services.documents.create("播放控制验收", TEXT)
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, Tier.BASIC)
    controller.set_settings(SynthesisSettings(volume=0.0))
    states: list[str] = []
    units: list[str] = []
    errors: list[str] = []
    pause_delta_us: int | None = None
    pause_started_us = 0
    control_started = False
    started_at = time.perf_counter()

    def fail(message: str) -> None:
        errors.append(message)
        app.quit()

    def begin_pause() -> None:
        nonlocal pause_started_us
        controller.pause()
        sink = controller.audio._sink
        pause_started_us = sink.processedUSecs() if sink is not None else -1
        QTimer.singleShot(1000, finish_pause)

    def finish_pause() -> None:
        nonlocal pause_delta_us
        sink = controller.audio._sink
        current = sink.processedUSecs() if sink is not None else -1
        pause_delta_us = current - pause_started_us
        controller.resume()
        QTimer.singleShot(700, controller.next)

    def unit_started(unit_id: str, _start: int, _end: int) -> None:
        nonlocal control_started
        units.append(unit_id)
        if not control_started:
            control_started = True
            QTimer.singleShot(350, begin_pause)
        elif len(set(units)) >= 2:
            QTimer.singleShot(500, finish_test)

    def finish_test() -> None:
        controller.stop()
        app.quit()

    controller.stateChanged.connect(lambda state, _message: states.append(state))
    controller.currentUnitChanged.connect(unit_started)
    controller.error.connect(fail)
    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(60_000, lambda: fail("timeout"))
    app.exec()
    final_state = controller.state
    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute("DELETE FROM documents WHERE document_id=?", (str(document.document_id),))
    passed = (
        not errors
        and "Paused" in states
        and "Playing" in states[states.index("Paused") + 1 :]
        and len(set(units)) >= 2
        and pause_delta_us is not None
        and 0 <= pause_delta_us < 150_000
        and final_state == "Idle"
    )
    result = {
        "passed": passed,
        "states": states,
        "started_units": units,
        "pause_clock_delta_microseconds": pause_delta_us,
        "final_state": final_state,
        "errors": errors,
        "elapsed_seconds": time.perf_counter() - started_at,
    }
    report = (
        paths.artifacts
        / "smoke"
        / "playback-controls-v12-final-20260811"
        / "report.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
