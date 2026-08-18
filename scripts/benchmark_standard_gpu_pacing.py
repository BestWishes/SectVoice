from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
from threading import Event, Lock, Thread
import time
import wave


TEXT = (
    "趁着晚霞，许川将《赤血刀法》的秘籍拿出来。"
    "准备先修炼一番。"
    "秘境之中有机缘，也有危险。"
    "多一门攻击手段也更能保证自己和苏妙音的安全。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standard GPU计算平滑真实A/B")
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--rest-seconds", type=float, default=15.0)
    return parser.parse_args()


class NvidiaSampler:
    FIELDS = (
        "utilization.gpu",
        "power.draw",
        "temperature.gpu",
        "fan.speed",
        "clocks.sm",
    )

    def __init__(self) -> None:
        self._samples: list[dict[str, float]] = []
        self._lock = Lock()
        self._stop = Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        command = [
            "nvidia-smi",
            "--query-gpu=" + ",".join(self.FIELDS),
            "--format=csv,noheader,nounits",
            "--loop-ms=100",
        ]
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._thread = Thread(target=self._read, name="nvidia-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process is not None:
            self._process.terminate()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def between(self, started: float, ended: float) -> list[dict[str, float]]:
        with self._lock:
            return [row for row in self._samples if started <= row["at"] <= ended]

    def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if self._stop.is_set():
                break
            values = [self._number(item) for item in line.strip().split(",")]
            if len(values) != len(self.FIELDS):
                continue
            row = {"at": time.perf_counter()}
            row.update(dict(zip(self.FIELDS, values, strict=True)))
            with self._lock:
                self._samples.append(row)

    @staticmethod
    def _number(value: str) -> float:
        try:
            return float(value.strip())
        except ValueError:
            return 0.0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def maximum_rolling_average(rows: list[dict[str, float]], field: str) -> float:
    best = 0.0
    left = 0
    total = 0.0
    for right, row in enumerate(rows):
        total += row[field]
        while row["at"] - rows[left]["at"] > 1.0:
            total -= rows[left][field]
            left += 1
        best = max(best, total / (right - left + 1))
    return best


def summarize(rows: list[dict[str, float]]) -> dict[str, object]:
    result: dict[str, object] = {"sample_count": len(rows)}
    for field in NvidiaSampler.FIELDS:
        values = [row[field] for row in rows]
        result[field] = {
            "mean": statistics.fmean(values) if values else 0.0,
            "p95": percentile(values, 0.95),
            "peak": max(values, default=0.0),
            "rolling_1s_peak_mean": maximum_rolling_average(rows, field),
        }
    result["samples"] = [
        {key: value for key, value in row.items() if key != "at"} for row in rows
    ]
    return result


def main() -> int:
    args = parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.package_dir))
    from runtime_engine import DEFAULT_GPU_COMPUTE_PACING, GptSovitsEngine

    engine = GptSovitsEngine(
        app_root=args.app_root,
        model_dir=args.model_dir,
        output_dir=args.artifact_dir / "engine-output",
    )
    loaded_at = time.perf_counter()
    engine.load()
    load_seconds = time.perf_counter() - loaded_at
    warmup_parts: list[bytes] = []
    engine.synthesize_stream(
        # Warm the same long-window path so one-time CUDA kernel/allocation
        # effects are not incorrectly attributed to either side of the A/B.
        text=TEXT,
        payload_dir=args.payload_dir,
        emit=lambda data, *_args: warmup_parts.append(data),
        seed=20260817,
        streaming_mode=True,
        compute_pacing=None,
    )
    time.sleep(max(0.0, args.rest_seconds))
    sampler = NvidiaSampler()
    sampler.start()
    modes = (
        ("unpaced", None),
        ("balanced", dict(DEFAULT_GPU_COMPUTE_PACING)),
    )
    report: dict[str, object] = {
        "engine_version": engine.__class__.__module__,
        "text": TEXT,
        "load_seconds": load_seconds,
        "modes": {},
    }
    try:
        for mode_index, (mode, pacing) in enumerate(modes):
            if mode_index:
                time.sleep(max(0.0, args.rest_seconds))
            rows: list[dict[str, object]] = []
            mode_started = time.perf_counter()
            for repeat in range(args.repeats):
                parts: list[bytes] = []
                started = time.perf_counter()
                result = engine.synthesize_stream(
                    text=TEXT,
                    payload_dir=args.payload_dir,
                    emit=lambda data, *_args: parts.append(data),
                    seed=20260818 + repeat,
                    streaming_mode=True,
                    compute_pacing=pacing,
                )
                elapsed = time.perf_counter() - started
                pcm = b"".join(parts)
                wav_path = args.artifact_dir / f"{mode}-{repeat + 1}.wav"
                with wave.open(str(wav_path), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(int(result["sample_rate"]))
                    target.writeframes(pcm)
                rows.append(
                    {
                        "repeat": repeat + 1,
                        "elapsed_seconds": elapsed,
                        "audio_seconds": result["audio_seconds"],
                        "realtime_factor": elapsed / float(result["audio_seconds"]),
                        "chunks": result["chunks"],
                        "pcm_bytes": len(pcm),
                        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                        "compute_pacing": result["compute_pacing"],
                        "wav_path": str(wav_path),
                    }
                )
            mode_ended = time.perf_counter()
            gpu = summarize(sampler.between(mode_started, mode_ended))
            report["modes"][mode] = {
                "runs": rows,
                "gpu": gpu,
            }
    finally:
        engine.unload()
        sampler.stop()

    unpaced = report["modes"]["unpaced"]["runs"]
    balanced = report["modes"]["balanced"]["runs"]
    report["audio_identical"] = all(
        before["pcm_sha256"] == after["pcm_sha256"]
        and before["pcm_bytes"] == after["pcm_bytes"]
        for before, after in zip(unpaced, balanced, strict=True)
    )
    report_path = args.artifact_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("REPORT=" + str(report_path), flush=True)
    print(
        json.dumps(
            {
                "audio_identical": report["audio_identical"],
                "load_seconds": load_seconds,
                "unpaced": {
                    key: value
                    for key, value in report["modes"]["unpaced"]["gpu"].items()
                    if key != "samples"
                },
                "balanced": {
                    key: value
                    for key, value in report["modes"]["balanced"]["gpu"].items()
                    if key != "samples"
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0 if report["audio_identical"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
