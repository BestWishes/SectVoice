from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sqlite3
import subprocess

import numpy as np


NIGHTFALL = (
    "趁着晚霞，许川将《赤血刀法》的秘籍拿出来。",
    "准备先修炼一番。",
    "秘境之中有机缘，也有危险。",
    "多一门攻击手段也更能保证自己和苏妙音的安全。",
)

FIELD_TAIL_WINDOW = (
    "各种妖兽频出，就算是许川，也没办法保证自身的安全，更别说还带着一个淬体境五重的累赘。\n",
    "到了。\n",
    "许川点点头。\n",
    "窗外的雨停了。\n",
)

FIELD_STANDARD_SHORTS = (
    "许川点点头。\n",
    "窗外的雨停了。\n",
    "晨光越过窗沿，落在安静的木桌上。",
)

FIELD_SHORT_FOUR = (
    "到了。\n",
    "许川点点头。\n",
    "窗外的雨停了。\n",
    "晨光越过窗沿，落在安静的木桌上。",
)

FIELD_STANDARD_PAIR = (
    "各种妖兽频出，就算是许川，也没办法保证自身的安全，更别说还带着一个淬体境五重的累赘。\n",
    "到了。\n",
)


def _metrics(frames: np.ndarray, sample_rate: int) -> dict[str, object]:
    mono = np.mean(frames.astype(np.float64), axis=1)
    window_frames = max(1, sample_rate // 100)
    count = mono.size // window_frames
    windows = mono[: count * window_frames].reshape(count, window_frames)
    levels = np.sqrt(np.mean(np.square(windows), axis=1)) if count else np.asarray([])
    active = levels >= 10 ** (-45.0 / 20.0)
    active_samples = windows[active].reshape(-1) if np.any(active) else mono
    active_rms = float(np.sqrt(np.mean(np.square(active_samples)))) if active_samples.size else 0.0
    active_indexes = np.flatnonzero(active)
    first_active = int(active_indexes[0]) if active_indexes.size else count
    last_active = int(active_indexes[-1]) if active_indexes.size else -1
    onset = windows[first_active : min(count, first_active + 25)].reshape(-1)
    ending = windows[max(0, last_active - 24) : last_active + 1].reshape(-1)

    def dbfs(values: np.ndarray) -> float:
        if values.size == 0:
            return -120.0
        rms = float(np.sqrt(np.mean(np.square(values))))
        return 20.0 * math.log10(max(rms, 1e-12))

    return {
        "duration_seconds": frames.shape[0] / sample_rate,
        "active_seconds": float(np.count_nonzero(active)) / 100.0,
        "active_rms_dbfs": 20.0 * math.log10(max(active_rms, 1e-12)),
        "onset_250ms_dbfs": dbfs(onset),
        "ending_250ms_dbfs": dbfs(ending),
        "leading_quiet_seconds": first_active / 100.0,
        "trailing_quiet_seconds": max(0, count - last_active - 1) / 100.0,
        "peak_dbfs": 20.0 * math.log10(max(float(np.max(np.abs(mono))), 1e-12)),
    }


def _to_wav(
    ffmpeg: Path,
    raw_path: Path,
    destination: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
) -> None:
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            sample_format,
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-i",
            str(raw_path),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="分析v12 GenerationWindow缓存与切点")
    parser.add_argument("--cache-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument(
        "--case-set",
        choices=(
            "nightfall",
            "nightfall-full",
            "field-tail",
            "field-standard-shorts",
            "field-standard-pair",
            "field-short-four",
        ),
        default="nightfall",
    )
    args = parser.parse_args()
    texts = {
        "nightfall": NIGHTFALL[:3],
        "nightfall-full": NIGHTFALL,
        "field-tail": FIELD_TAIL_WINDOW,
        "field-standard-shorts": FIELD_STANDARD_SHORTS,
        "field-standard-pair": FIELD_STANDARD_PAIR,
        "field-short-four": FIELD_SHORT_FOUR,
    }[args.case_set]
    args.output.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM cache_index WHERE cache_key=?", (args.cache_key,)
    ).fetchone()
    connection.close()
    if row is None:
        raise RuntimeError("cache key does not exist")
    metadata = json.loads(row["metadata_json"])
    layout = metadata["window_layout"]
    ranges = layout["unit_ranges"]
    if len(ranges) != len(texts):
        raise RuntimeError(
            f"selected cache has {len(ranges)} units, expected {len(texts)}"
        )
    sample_rate = int(metadata["sample_rate"])
    channels = int(metadata["channels"])
    sample_format = str(metadata["sample_format"])
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(Path(row["audio_path"]), dtype=dtype)
    frames = raw[: raw.size - raw.size % channels].reshape(-1, channels)
    normalized = frames.astype(np.float32)
    if sample_format == "s16le":
        normalized /= 32768.0

    full_wav = args.output / "window-full.wav"
    _to_wav(
        args.ffmpeg,
        Path(row["audio_path"]),
        full_wav,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
    )
    cases: list[dict[str, object]] = [
        {
            "name": "window-full",
            "text": "".join(texts),
            "output_path": str(full_wav),
        }
    ]
    units: list[dict[str, object]] = []
    for index, (text, frame_range) in enumerate(zip(texts, ranges, strict=True)):
        start = int(frame_range["start_frame"])
        end = int(frame_range["end_frame"])
        sliced = frames[start:end]
        raw_slice = args.output / f"unit-{index + 1}.pcm"
        sliced.astype(dtype).tofile(raw_slice)
        wav = args.output / f"unit-{index + 1}.wav"
        _to_wav(
            args.ffmpeg,
            raw_slice,
            wav,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )
        item = {
            "unit_index": index,
            "text": text,
            "output_path": str(wav),
            "start_frame": start,
            "end_frame": end,
            "boundary_confidence": frame_range.get("boundary_confidence"),
            **_metrics(normalized[start:end], sample_rate),
        }
        units.append(item)
        cases.append(
            {"name": f"unit-{index + 1}", "text": text, "output_path": str(wav)}
        )
    report = {
        "cache_key": args.cache_key,
        "audio_path": row["audio_path"],
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_format": sample_format,
        "window_metrics": _metrics(normalized, sample_rate),
        "units": units,
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "asr-input.json").write_text(
        json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
