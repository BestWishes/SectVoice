from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import psutil
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, Tier
from sectvoice.paths import AppPaths


def main() -> int:
    parser = argparse.ArgumentParser(description="真实引擎崩溃后自动重启smoke")
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
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
    first_client = handle.client
    first_pid = first_client.process_id
    if first_pid is None:
        raise RuntimeError("工作进程没有启动")
    process = psutil.Process(first_pid)
    process.kill()
    process.wait(timeout=10)
    deadline = time.monotonic() + 5
    while first_client.is_running and time.monotonic() < deadline:
        time.sleep(0.02)
    restarted_at = time.perf_counter()
    second_client = handle.client
    restart_seconds = time.perf_counter() - restarted_at
    second_pid = second_client.process_id
    chunks = list(
        second_client.stream_synthesis(
            session_id=uuid4(),
            generation_id=2,
            speech_unit_id=uuid4(),
            text="工作进程重新启动以后，Reader仍然可以继续朗读。",
            payload_path=payload.opaque_path,
            options={"speed": 1.0, "seed": 20260810},
        )
    )
    audible_bytes = sum(len(chunk.data) for chunk in chunks)
    services.asr.shutdown()
    services.engines.shutdown()
    passed = first_pid != second_pid and audible_bytes > 0
    result = {
        "passed": passed,
        "tier": tier.value,
        "first_pid": first_pid,
        "restarted_pid": second_pid,
        "restart_and_load_seconds": restart_seconds,
        "audible_bytes": audible_bytes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
