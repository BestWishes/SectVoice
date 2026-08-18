from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Condition

from sectvoice.domain import AudioChunk


@dataclass(frozen=True, slots=True)
class BufferWatermarks:
    low_seconds: float = 3.0
    target_seconds: float = 6.0
    high_seconds: float = 18.0

    def __post_init__(self) -> None:
        if not 0 <= self.low_seconds < self.target_seconds < self.high_seconds:
            raise ValueError("watermarks must be strictly increasing")


class StreamingAudioBuffer:
    """Thread-safe PCM queue measured in playable seconds, not sentence count."""

    def __init__(self, watermarks: BufferWatermarks | None = None) -> None:
        self.watermarks = watermarks or BufferWatermarks()
        self._chunks: deque[AudioChunk] = deque()
        self._seconds = 0.0
        self._condition = Condition()

    @property
    def buffered_seconds(self) -> float:
        with self._condition:
            return self._seconds

    @property
    def needs_audio(self) -> bool:
        return self.buffered_seconds <= self.watermarks.low_seconds

    @property
    def at_high_watermark(self) -> bool:
        return self.buffered_seconds >= self.watermarks.high_seconds

    @property
    def completed_units(self) -> int:
        with self._condition:
            return sum(1 for chunk in self._chunks if chunk.is_final)

    @property
    def ready_to_start(self) -> bool:
        """Starts only after complete units provide a real minimum time cushion."""

        with self._condition:
            completed = sum(1 for chunk in self._chunks if chunk.is_final)
            return completed >= 1 and self._seconds >= self.watermarks.target_seconds

    def push(self, chunk: AudioChunk) -> None:
        with self._condition:
            self._chunks.append(chunk)
            self._seconds += chunk.duration_seconds
            self._condition.notify()

    def pop(self) -> AudioChunk | None:
        with self._condition:
            if not self._chunks:
                return None
            chunk = self._chunks.popleft()
            self._seconds = max(0.0, self._seconds - chunk.duration_seconds)
            return chunk

    def clear(self) -> tuple[AudioChunk, ...]:
        with self._condition:
            discarded = tuple(self._chunks)
            self._chunks.clear()
            self._seconds = 0.0
            self._condition.notify_all()
            return discarded

    def __len__(self) -> int:
        with self._condition:
            return len(self._chunks)
