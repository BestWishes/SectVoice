from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import psutil
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


def _package_worker_pids(paths: AppPaths) -> set[int]:
    marker = str(paths.runtime / "engines").casefold()
    result: set[int] = set()
    for process in psutil.process_iter(("pid", "cmdline")):
        try:
            command = " ".join(process.info["cmdline"] or ()).casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if marker in command and "package\\worker.py" in command:
            result.add(int(process.info["pid"]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真实生成中立即关停后的引擎进程清理验收"
    )
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-after-ms", type=int, default=750)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    paths = AppPaths.discover()
    before = _package_worker_pids(paths)
    app = QApplication(sys.argv[:1])
    services = build_services(paths)
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError(f"{tier.value}语音包没有启用")
    voice = next(
        (
            profile
            for profile in services.voices.list_profiles()
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
        raise RuntimeError("没有READY的真实声音Payload")
    unique_text = f"关停竞争检查{uuid4().hex}，这段语音不应该生成完成后继续占用模型。"
    document = services.documents.create("关停清理验收", unique_text)
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, tier)
    errors: list[str] = []
    controller.error.connect(errors.append)

    def shutdown_while_busy() -> None:
        controller.stop()
        services.asr.shutdown()
        services.engines.shutdown()
        QTimer.singleShot(3_000, app.quit)

    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(max(1, args.stop_after_ms), shutdown_while_busy)
    QTimer.singleShot(240_000, app.quit)
    started_at = time.perf_counter()
    app.exec()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute(
            "DELETE FROM documents WHERE document_id=?", (str(document.document_id),)
        )
    remaining = sorted(_package_worker_pids(paths) - before)
    result = {
        "passed": not remaining,
        "tier": tier.value,
        "elapsed_seconds": time.perf_counter() - started_at,
        "preexisting_worker_pids": sorted(before),
        "remaining_new_worker_pids": remaining,
        "reader_errors": errors,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    print(serialized)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
