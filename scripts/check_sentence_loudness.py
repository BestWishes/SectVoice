from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

from sectvoice.paths import AppPaths


def _sample_array(path: Path, sample_format: str) -> np.ndarray:
    if sample_format == "s16le":
        return np.fromfile(path, dtype="<i2").astype(np.float32) / 32768.0
    if sample_format == "f32le":
        return np.fromfile(path, dtype="<f4").astype(np.float32)
    raise ValueError(f"unsupported PCM sample format: {sample_format}")


def _longest_internal_silence_seconds(
    samples: np.ndarray, *, sample_rate: int, channels: int
) -> float:
    usable = samples[: samples.size - samples.size % channels].reshape(-1, channels)
    window_frames = max(1, sample_rate // 100)
    window_count = usable.shape[0] // window_frames
    if window_count < 3:
        return 0.0
    windows = usable[: window_count * window_frames].reshape(
        window_count, window_frames, channels
    )
    levels = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=(1, 2)))
    active_indexes = np.flatnonzero(levels >= 10 ** (-45.0 / 20.0))
    if active_indexes.size < 2:
        return 0.0
    silent = levels < 10 ** (-45.0 / 20.0)
    changes = np.diff(np.pad(silent.astype(np.int8), (1, 1)))
    first_active = int(active_indexes[0])
    last_active = int(active_indexes[-1])
    internal_runs = [
        int(end - start)
        for start, end in zip(
            np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)
        )
        if start > first_active and end <= last_active
    ]
    return max(internal_runs, default=0) / 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Reader 完整句子缓存的实际电平一致性")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--created-after", help="ISO-8601 UTC 时间下限")
    parser.add_argument("--created-before", help="ISO-8601 UTC 时间上限")
    parser.add_argument("--max-rms-spread-db", type=float, default=4.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    database_path = AppPaths.discover().data / "sectvoice.db"
    clauses = ["metadata_json LIKE '%sentence_atomic%'"]
    parameters: list[object] = []
    if args.created_after:
        clauses.append("created_at >= ?")
        parameters.append(datetime.fromisoformat(args.created_after).isoformat())
    if args.created_before:
        clauses.append("created_at <= ?")
        parameters.append(datetime.fromisoformat(args.created_before).isoformat())
    parameters.append(args.limit)
    query = f"""
        SELECT audio_path, duration_seconds, metadata_json, created_at
        FROM cache_index
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT ?
    """
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()

    measurements: list[dict[str, object]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        audio_path = Path(row["audio_path"])
        samples = _sample_array(audio_path, str(metadata["sample_format"]))
        if samples.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        peak = float(np.max(np.abs(samples)))
        sample_rate = int(metadata["sample_rate"])
        channels = int(metadata["channels"])
        measurements.append(
            {
                "created_at": row["created_at"],
                "audio_path": str(audio_path),
                "duration_seconds": row["duration_seconds"],
                "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
                "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
                "longest_internal_silence_seconds": _longest_internal_silence_seconds(
                    samples,
                    sample_rate=sample_rate,
                    channels=channels,
                ),
                "metadata": metadata,
            }
        )

    rms_values = [float(item["rms_dbfs"]) for item in measurements]
    spread = max(rms_values) - min(rms_values) if rms_values else math.inf
    result = {
        "passed": len(measurements) >= 2 and spread <= args.max_rms_spread_db,
        "sentence_count": len(measurements),
        "rms_spread_db": spread,
        "rms_standard_deviation_db": float(np.std(rms_values)) if rms_values else None,
        "maximum_peak_dbfs": (
            max(float(item["peak_dbfs"]) for item in measurements)
            if measurements
            else None
        ),
        "threshold_db": args.max_rms_spread_db,
        "measurements": measurements,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
