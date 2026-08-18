from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths


TEXT = (
    "掌柜说道：“今晚风很静，我们可以慢慢商量。”\n"
    "少女回答：“我已经准备好了，现在就出发吧。”\n"
    "掌柜又问：“路上的干粮和清水都带齐了吗？”\n"
    "少女笑道：“一样不少，你放心就是。”"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实多角色连续朗读smoke")
    parser.add_argument("--tier", choices=("basic", "standard"), default="basic")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError(f"{tier.value}包未安装")
    profiles = [
        profile
        for profile in services.voices.list_profiles()
        if any(
            payload.tier is tier
            and payload.engine_id == active.engine_id
            and payload.status is PayloadStatus.READY
            for payload in services.voices.payloads_for(profile.voice_id)
        )
    ]
    if len(profiles) < 2:
        raise RuntimeError("至少需要两个具有当前Tier Payload的真实声音")

    document = services.documents.create(f"多角色{tier.value}验收", TEXT)
    units = document.mapping.units
    for index, unit in enumerate(units):
        role = "掌柜" if index % 2 == 0 else "少女"
        services.roles.set_units_role(
            document.document_id,
            document.mapping,
            unit.start_char,
            unit.end_char,
            role,
        )
    services.roles.assign_voice(document.document_id, "掌柜", profiles[0].voice_id, tier)
    services.roles.assign_voice(document.document_id, "少女", profiles[1].voice_id, tier)
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(profiles[0].voice_id, tier)
    controller.set_settings(SynthesisSettings(volume=0.0))
    resolved = controller._resolve_units(units)
    resolved_voices = [str(item.voice_id) for item in resolved]
    handle = services.engines.for_tier(tier)
    worker_pid_before = handle.client.process_id
    started: list[str] = []
    errors: list[str] = []
    started_at = time.perf_counter()
    controller.currentUnitChanged.connect(lambda unit_id, _start, _end: started.append(unit_id))
    controller.error.connect(lambda message: (errors.append(message), app.quit()))
    controller.finished.connect(app.quit)
    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(int(args.timeout * 1000), lambda: (errors.append("timeout"), app.quit()))
    app.exec()
    worker_pid_after = handle.client.process_id
    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute("DELETE FROM documents WHERE document_id=?", (str(document.document_id),))
    passed = (
        not errors
        and len(started) == len(units)
        and len(set(resolved_voices)) == 2
        and worker_pid_before is not None
        and worker_pid_before == worker_pid_after
    )
    result = {
        "passed": passed,
        "tier": tier.value,
        "elapsed_seconds": time.perf_counter() - started_at,
        "speech_units": len(units),
        "started_units": started,
        "resolved_voice_ids": resolved_voices,
        "worker_pid_before": worker_pid_before,
        "worker_pid_after": worker_pid_after,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
