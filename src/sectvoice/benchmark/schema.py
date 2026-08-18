from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ResourceSample:
    elapsed_seconds: float
    cpu_percent: float
    ram_mib: float
    gpu_percent: float | None = None
    vram_mib: float | None = None


@dataclass(frozen=True, slots=True)
class SynthesisMeasurement:
    case_id: str
    text_length: int
    audio_seconds: float
    elapsed_seconds: float
    first_chunk_seconds: float | None
    success: bool
    error: str | None = None

    @property
    def rtf(self) -> float | None:
        if not self.success or self.audio_seconds <= 0:
            return None
        return self.elapsed_seconds / self.audio_seconds


@dataclass(slots=True)
class BenchmarkReport:
    engine_id: str
    engine_version: str
    model_id: str
    model_revision: str
    device: str
    machine: dict[str, Any]
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    cold_load_seconds: float | None = None
    warmup_seconds: float | None = None
    measurements: list[SynthesisMeasurement] = field(default_factory=list)
    resources: list[ResourceSample] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        for item, measurement in zip(payload["measurements"], self.measurements):
            item["rtf"] = measurement.rtf
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

