from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tier(str, Enum):
    BASIC = "basic"
    STANDARD = "standard"
    ADVANCED = "advanced"


class PayloadStatus(str, Enum):
    MISSING = "missing"
    COMPILING = "compiling"
    READY = "ready"
    ERROR = "error"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class PCMFormat:
    sample_rate: int
    channels: int = 1
    sample_format: str = "s16le"

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels not in (1, 2):
            raise ValueError("only mono and stereo PCM are supported")
        if self.sample_format not in {"s16le", "f32le"}:
            raise ValueError(f"unsupported PCM format: {self.sample_format}")

    @property
    def bytes_per_frame(self) -> int:
        bytes_per_sample = 2 if self.sample_format == "s16le" else 4
        return bytes_per_sample * self.channels


@dataclass(frozen=True, slots=True)
class TimingMark:
    """Optional timing relative to one SpeechUnit, never to a Reader document."""

    speech_unit_offset_start: int
    speech_unit_offset_end: int
    start_seconds: float
    end_seconds: float
    label: str | None = None

    def __post_init__(self) -> None:
        if self.speech_unit_offset_start < 0:
            raise ValueError("speech-unit offset cannot be negative")
        if self.speech_unit_offset_end < self.speech_unit_offset_start:
            raise ValueError("speech-unit offsets are reversed")
        if self.start_seconds < 0 or self.end_seconds < self.start_seconds:
            raise ValueError("timing range is invalid")


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Engine-neutral PCM packet keyed only by a SpeechUnit identity."""

    session_id: UUID
    generation_id: int
    sequence: int
    speech_unit_id: UUID
    pcm_format: PCMFormat
    duration_seconds: float
    data: bytes
    is_final: bool = False
    timing_info: tuple[TimingMark, ...] = ()

    def __post_init__(self) -> None:
        if self.generation_id < 0 or self.sequence < 0:
            raise ValueError("generation_id and sequence must be non-negative")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        if not isinstance(self.data, bytes):
            raise TypeError("AudioChunk.data must be bytes")


@dataclass(frozen=True, slots=True)
class EnginePayloadRef:
    voice_id: UUID
    tier: Tier
    engine_id: str
    engine_version: str
    payload_format_version: str
    status: PayloadStatus
    opaque_path: Path
    sha256: str
    created_at: str
    updated_at: str
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    voice_id: UUID
    name: str
    source_audio_path: Path
    reference_audio_path: Path
    transcript: str
    language: str = "zh-CN"
    reference_start_seconds: float = 0.0
    reference_end_seconds: float = 0.0
    reference_sample_rate: int = 0
    reference_duration_seconds: float = 0.0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    default_settings: Mapping[str, Any] = field(default_factory=dict)
    is_available: bool = True
    last_error: str | None = None

    @classmethod
    def create(
        cls,
        name: str,
        source_audio_path: Path,
        reference_audio_path: Path,
        transcript: str,
        language: str = "zh-CN",
        reference_start_seconds: float = 0.0,
        reference_end_seconds: float = 0.0,
        reference_sample_rate: int = 0,
        reference_duration_seconds: float = 0.0,
    ) -> "VoiceProfile":
        cleaned_name = name.strip()
        cleaned_transcript = transcript.strip()
        if not cleaned_name:
            raise ValueError("voice name is required")
        if not cleaned_transcript:
            raise ValueError("reference transcript is required")
        return cls(
            voice_id=uuid4(),
            name=cleaned_name,
            source_audio_path=source_audio_path,
            reference_audio_path=reference_audio_path,
            transcript=cleaned_transcript,
            language=language,
            reference_start_seconds=reference_start_seconds,
            reference_end_seconds=reference_end_seconds,
            reference_sample_rate=reference_sample_rate,
            reference_duration_seconds=reference_duration_seconds,
        )


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    streaming_audio: bool
    cancellable_generation: bool
    native_speed: bool
    emotions: tuple[str, ...]
    styles: tuple[str, ...]
    languages: tuple[str, ...]
    preferred_pcm_format: PCMFormat
    min_reference_seconds: float
    max_reference_seconds: float
    execution_devices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SynthesisSettings:
    speed: float = 1.0
    volume: float = 1.0
    emotion: str = "natural"
    emotion_strength: float = 0.5
    punctuation_pause_ms: int = 120
    paragraph_pause_ms: int = 280
    custom_style: str | None = None

    def __post_init__(self) -> None:
        if not 0.5 <= self.speed <= 2.0:
            raise ValueError("speed must be in [0.5, 2.0]")
        if not 0.0 <= self.volume <= 2.0:
            raise ValueError("volume must be in [0.0, 2.0]")
        if not 0.0 <= self.emotion_strength <= 1.0:
            raise ValueError("emotion_strength must be in [0.0, 1.0]")
        if self.punctuation_pause_ms < 0 or self.paragraph_pause_ms < 0:
            raise ValueError("pause values cannot be negative")


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    session_id: UUID
    generation_id: int
    speech_unit_id: UUID
    text: str
    voice_id: UUID
    tier: Tier
    settings: SynthesisSettings

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("synthesis text cannot be empty")
