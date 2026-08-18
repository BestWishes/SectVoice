from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from uuid import UUID

import numpy as np

from sectvoice.app import build_services
from sectvoice.core.cache import SynthesisCacheIdentity
from sectvoice.core.engine_manager import EnginePackageManifest
from sectvoice.core.runtime import POSTPROCESS_VERSION, _timing_speed_factor, json_pcm
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths


def _runs(mask: np.ndarray, value: bool) -> list[tuple[int, int]]:
    matches = np.asarray(mask == value, dtype=np.int8)
    padded = np.pad(matches, (1, 1))
    changes = np.diff(padded)
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


def _measure(path: Path, metadata: dict[str, object]) -> dict[str, object]:
    sample_format = str(metadata["sample_format"])
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    samples = np.fromfile(path, dtype=dtype).astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    channels = int(metadata["channels"])
    sample_rate = int(metadata["sample_rate"])
    frames = samples[: samples.size - samples.size % channels].reshape(-1, channels)
    frame_size = max(1, sample_rate // 100)
    usable = frames[: frames.shape[0] - frames.shape[0] % frame_size]
    windows = usable.reshape(-1, frame_size, channels)
    window_rms = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=(1, 2)))
    active = window_rms >= 10 ** (-45.0 / 20.0)
    active_indexes = np.flatnonzero(active)
    first_active = int(active_indexes[0]) if active_indexes.size else len(active)
    last_active = int(active_indexes[-1]) if active_indexes.size else -1
    internal_silences = [
        (start / 100.0, end / 100.0, (end - start) / 100.0)
        for start, end in _runs(active, False)
        if start > first_active and end <= last_active and end - start >= 18
    ]
    active_samples = windows[active].reshape(-1) if np.any(active) else samples
    active_rms = float(np.sqrt(np.mean(np.square(active_samples, dtype=np.float64))))
    onset_window_count = max(1, int(round(0.25 * 100)))
    onset_windows = windows[
        first_active : min(len(windows), first_active + onset_window_count)
    ]
    onset_rms = (
        float(np.sqrt(np.mean(np.square(onset_windows, dtype=np.float64))))
        if onset_windows.size
        else 0.0
    )
    return {
        "duration_seconds": frames.shape[0] / sample_rate,
        "leading_silence_seconds": first_active / 100.0,
        "trailing_silence_seconds": max(0.0, (len(active) - last_active - 1) / 100.0),
        "active_seconds": float(np.count_nonzero(active)) / 100.0,
        "active_rms_dbfs": 20.0 * math.log10(max(active_rms, 1e-12)),
        "onset_250ms_rms_dbfs": 20.0 * math.log10(max(onset_rms, 1e-12)),
        "onset_vs_active_db": 20.0
        * math.log10(max(onset_rms, 1e-12) / max(active_rms, 1e-12)),
        "internal_silences": internal_silences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="分析当前阅读位置附近的最终句子缓存")
    parser.add_argument("--before", type=int, default=8)
    parser.add_argument("--after", type=int, default=20)
    parser.add_argument("--voice-id", type=UUID)
    parser.add_argument("--ordinal", type=int)
    parser.add_argument("--punctuation-pause-ms", type=int)
    parser.add_argument("--paragraph-pause-ms", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    services = build_services(AppPaths.discover())
    store = services.settings
    document_id = UUID(str(store.get("last_document_id")))
    voice_id = args.voice_id or UUID(str(store.get("selected_voice_id")))
    tier = Tier(str(store.get("tier", Tier.BASIC.value)))
    document = services.documents.get(document_id)
    profile = services.voices.get(voice_id)
    active_package = services.packages.active_for(tier)
    if document is None or profile is None or active_package is None:
        raise RuntimeError("当前文档、声音或引擎包不可用")
    manifest = EnginePackageManifest.read(active_package.manifest_path)
    payload = next(
        item
        for item in services.voices.payloads_for(voice_id)
        if item.tier is tier
        and item.engine_id == manifest.engine_id
        and item.status is PayloadStatus.READY
    )
    settings = SynthesisSettings(
        speed=float(store.get("speed", 1.0)),
        volume=float(store.get("volume", 1.0)),
        punctuation_pause_ms=(
            args.punctuation_pause_ms
            if args.punctuation_pause_ms is not None
            else int(store.get("punctuation_pause_ms", 120))
        ),
        paragraph_pause_ms=(
            args.paragraph_pause_ms
            if args.paragraph_pause_ms is not None
            else int(store.get("paragraph_pause_ms", 280))
        ),
    )
    current_unit = (
        document.mapping.units[args.ordinal]
        if args.ordinal is not None
        else document.mapping.locate(document.current_position)[0]
    )
    first = max(0, current_unit.ordinal - args.before)
    units = document.mapping.units[first : current_unit.ordinal + args.after + 1]
    capabilities = dict(manifest.raw.get("capabilities") or {})
    native_speed = bool(capabilities.get("native_speed"))
    rows: list[dict[str, object]] = []
    with services.documents.database.connect() as connection:
        for unit in units:
            identity = SynthesisCacheIdentity(
                text=unit.text,
                voice_id=voice_id,
                tier=tier,
                engine_id=manifest.engine_id,
                engine_version=manifest.engine_version,
                payload_version=payload.payload_format_version,
                payload_sha256=payload.sha256,
                reference_transcript_sha256=hashlib.sha256(
                    profile.transcript.encode("utf-8")
                ).hexdigest(),
                language="zh-CN",
                style="",
                emotion=settings.emotion,
                emotion_strength=settings.emotion_strength,
                speed_mode="native" if native_speed else "base",
                synthesis_speed=settings.speed if native_speed else None,
                punctuation_pause_ms=settings.punctuation_pause_ms,
                paragraph_pause_ms=settings.paragraph_pause_ms,
                pcm_format=json_pcm(capabilities),
                postprocess_version=POSTPROCESS_VERSION,
            )
            cache_row = connection.execute(
                "SELECT * FROM cache_index WHERE cache_key=?", (identity.key,)
            ).fetchone()
            item: dict[str, object] = {
                "ordinal": unit.ordinal,
                "text": unit.text,
                "characters": sum(character.isalnum() for character in unit.text),
                "cached": cache_row is not None,
            }
            if cache_row is not None:
                metadata = json.loads(cache_row["metadata_json"])
                item["audio_path"] = str(cache_row["audio_path"])
                item.update(_measure(Path(cache_row["audio_path"]), metadata))
                active_seconds = float(item["active_seconds"])
                speed_factor = _timing_speed_factor(
                    unit.text,
                    float(item["duration_seconds"]),
                    settings.speed,
                    active_speech_seconds=active_seconds,
                )
                item["playback_speed_factor"] = speed_factor
                item["post_stretch_active_seconds"] = active_seconds / speed_factor
                item["characters_per_active_second"] = (
                    float(item["characters"]) / active_seconds if active_seconds else None
                )
                item["post_stretch_characters_per_active_second"] = (
                    float(item["characters"]) * speed_factor / active_seconds
                    if active_seconds
                    else None
                )
                item["post_stretch_internal_silences"] = [
                    [start / speed_factor, end / speed_factor, duration / speed_factor]
                    for start, end, duration in item["internal_silences"]
                ]
            rows.append(item)

    cached = [item for item in rows if item["cached"]]
    result = {
        "document_id": str(document_id),
        "current_position": document.current_position,
        "current_ordinal": current_unit.ordinal,
        "voice_id": str(voice_id),
        "tier": tier.value,
        "settings": {
            "speed": settings.speed,
            "punctuation_pause_ms": settings.punctuation_pause_ms,
            "paragraph_pause_ms": settings.paragraph_pause_ms,
        },
        "cached_units": len(cached),
        "units": rows,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
