from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import UUID

import psutil
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sectvoice.app import build_services
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths


def make_text(count: int = 1200, namespace: str = "") -> str:
    templates = (
        "山风越过竹林，远处的钟声缓缓落在江面上。",
        "掌柜收好账本，又把窗边那盏旧灯轻轻拨亮。",
        "年轻人沿着石阶向前走，脚步始终平稳从容。",
        "夜色一点点沉下来，渡口仍有人耐心等船。",
        "雨后空气清澈，屋檐上的水珠接连落入青石槽。",
    )
    return "".join(
        (
            f"{namespace}连续稳定性记录第{index + 1}项，"
            f"{templates[index % len(templates)]}\n"
        )
        for index in range(count)
    )


def process_totals() -> tuple[float, float, int]:
    root = psutil.Process(os.getpid())
    processes = [root, *root.children(recursive=True)]
    rss = 0
    cpu = 0.0
    threads = 0
    for process in processes:
        try:
            rss += process.memory_info().rss
            cpu += process.cpu_percent(None)
            threads += process.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rss / 1024**3, cpu, threads


def gpu_stats() -> tuple[float, float]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return 0.0, 0.0
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
            text=True,
        )
    except Exception:
        return 0.0, 0.0
    first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    fields = [field.strip() for field in first.split(",")]
    try:
        return float(fields[0]), float(fields[1])
    except (IndexError, ValueError):
        return 0.0, 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="SectVoice真实连续朗读长测")
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice-id", type=UUID)
    parser.add_argument(
        "--text-namespace",
        default="",
        help="加入原文的唯一前缀；用于确保本次长测覆盖冷生成而不是复用旧缓存",
    )
    parser.add_argument(
        "--worker-recycle-rss-mib",
        type=float,
        help="仅用于资源保护smoke：在本次进程内覆盖Basic工作进程回收线",
    )
    parser.add_argument("--audible", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])
    services = build_services(AppPaths.discover())
    tier = Tier(args.tier)
    active = services.packages.active_for(tier)
    if active is None:
        raise RuntimeError(f"{tier.value}语音包没有启用")
    engine_handle = services.engines.for_tier(tier)
    if args.worker_recycle_rss_mib is not None:
        if tier is not Tier.BASIC or args.worker_recycle_rss_mib <= 0:
            raise ValueError("工作进程回收线smoke只支持正数Basic阈值")
        capabilities = engine_handle.manifest.raw.get("capabilities")
        if not isinstance(capabilities, dict):
            raise RuntimeError("Basic包没有可覆盖的capabilities")
        capabilities["worker_recycle_rss_mib"] = args.worker_recycle_rss_mib
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
        raise RuntimeError("没有可用于长测的真实声音Payload")
    document = services.documents.create(
        f"{tier.value}三十分钟长测",
        make_text(namespace=args.text_namespace),
    )
    controller = services.playback
    controller.set_document(document)
    controller.set_voice(voice.voice_id, tier)
    controller.set_settings(
        SynthesisSettings(
            volume=1.0 if args.audible else 0.0,
            punctuation_pause_ms=100,
            paragraph_pause_ms=150,
        )
    )
    target_seconds = args.minutes * 60
    launched_at = time.perf_counter()
    playback_started_at: float | None = None
    reached_target = False
    errors: list[str] = []
    underruns = 0
    units_started = 0
    samples: list[dict[str, object]] = []
    peak_rss = 0.0
    peak_engine_worker_rss_mib = 0.0
    peak_gpu = 0.0
    peak_gpu_utilization = 0.0
    baseline_gpu_memory, _ = gpu_stats() if tier is Tier.STANDARD else (0.0, 0.0)

    def target_complete() -> None:
        nonlocal reached_target
        reached_target = True
        app.quit()

    def unit_started(_unit_id: str, _start: int, _end: int) -> None:
        nonlocal playback_started_at, units_started
        units_started += 1
        if playback_started_at is None:
            playback_started_at = time.perf_counter()
            QTimer.singleShot(int(target_seconds * 1000), target_complete)

    def underflow() -> None:
        nonlocal underruns
        underruns += 1

    def failed(message: str) -> None:
        errors.append(message)
        app.quit()

    def ended_early() -> None:
        if not reached_target:
            errors.append("文档在目标时长前播放完毕")
            app.quit()

    def sample() -> None:
        nonlocal peak_rss, peak_engine_worker_rss_mib, peak_gpu, peak_gpu_utilization
        rss, cpu, threads = process_totals()
        engine_worker_rss_mib = engine_handle.worker_rss_mib
        gpu, gpu_utilization = gpu_stats() if tier is Tier.STANDARD else (0.0, 0.0)
        peak_rss = max(peak_rss, rss)
        peak_engine_worker_rss_mib = max(
            peak_engine_worker_rss_mib, engine_worker_rss_mib
        )
        peak_gpu = max(peak_gpu, gpu)
        peak_gpu_utilization = max(peak_gpu_utilization, gpu_utilization)
        samples.append(
            {
                "at_seconds": time.perf_counter() - launched_at,
                "rss_gib": round(rss, 3),
                "engine_worker_rss_mib": round(engine_worker_rss_mib, 1),
                "cpu_percent": round(cpu, 1),
                "threads": threads,
                "gpu_memory_mib": gpu,
                "gpu_utilization_percent": gpu_utilization,
                "buffered_seconds": round(controller.audio.buffer.buffered_seconds, 3),
                "units_started": units_started,
                "underruns": underruns,
                "state": controller.state,
                "worker_recycle_count": controller.runtime.worker_recycle_count,
            }
        )

    def progress() -> None:
        elapsed = (
            time.perf_counter() - playback_started_at
            if playback_started_at is not None
            else 0.0
        )
        print(
            f"PROGRESS tier={tier.value} playback={elapsed:.1f}/{target_seconds:.1f}s "
            f"units={units_started} underruns={underruns} rss={peak_rss:.2f}GiB "
            f"vram={peak_gpu:.0f}MiB recycles={controller.runtime.worker_recycle_count}",
            flush=True,
        )

    controller.currentUnitChanged.connect(unit_started)
    controller.audio.underrun.connect(underflow)
    controller.error.connect(failed)
    controller.finished.connect(ended_early)
    sample_timer = QTimer()
    sample_timer.setInterval(5000)
    sample_timer.timeout.connect(sample)
    sample_timer.start()
    progress_timer = QTimer()
    progress_timer.setInterval(60000)
    progress_timer.timeout.connect(progress)
    progress_timer.start()
    QTimer.singleShot(0, controller.play)
    QTimer.singleShot(int((target_seconds + 300) * 1000), lambda: failed("长测看门狗超时"))
    app.exec()
    sample()
    playback_seconds = (
        time.perf_counter() - playback_started_at
        if playback_started_at is not None
        else 0.0
    )
    controller.stop()
    services.asr.shutdown()
    services.engines.shutdown()
    with services.documents.database.connect() as connection:
        connection.execute("DELETE FROM documents WHERE document_id=?", (str(document.document_id),))
    passed = reached_target and not errors and underruns == 0 and units_started > 5
    result = {
        "passed": passed,
        "tier": tier.value,
        "voice_id": str(voice.voice_id),
        "target_playback_seconds": target_seconds,
        "actual_playback_seconds": playback_seconds,
        "launch_to_first_audio_seconds": (
            playback_started_at - launched_at if playback_started_at is not None else None
        ),
        "units_started": units_started,
        "underruns": underruns,
        "worker_recycle_count": controller.runtime.worker_recycle_count,
        "text_namespace": args.text_namespace,
        "worker_recycle_rss_mib_override": args.worker_recycle_rss_mib,
        "peak_rss_gib": peak_rss,
        "peak_engine_worker_rss_mib": peak_engine_worker_rss_mib,
        "peak_gpu_memory_mib": peak_gpu,
        "baseline_gpu_memory_mib": baseline_gpu_memory,
        "peak_gpu_utilization_percent": peak_gpu_utilization,
        "errors": errors,
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("FINAL " + json.dumps({key: value for key, value in result.items() if key != "samples"}, ensure_ascii=False), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
