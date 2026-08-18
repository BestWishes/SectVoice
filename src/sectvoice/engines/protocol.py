from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from sectvoice.domain import (
    AudioChunk,
    EngineCapabilities,
    EnginePayloadRef,
    SynthesisRequest,
    Tier,
    VoiceProfile,
)


class VoiceEngine(ABC):
    """Implemented by an isolated engine worker, never by the Reader window."""

    engine_id: str
    engine_version: str
    tier: Tier

    @abstractmethod
    def capabilities(self) -> EngineCapabilities:
        raise NotImplementedError

    @abstractmethod
    def load(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def warmup(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def compile_voice(
        self, profile: VoiceProfile, destination: Path
    ) -> EnginePayloadRef:
        raise NotImplementedError

    @abstractmethod
    def synthesize(
        self, request: SynthesisRequest, payload: EnginePayloadRef
    ) -> Iterator[AudioChunk]:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, session_id: UUID, generation_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> None:
        raise NotImplementedError

