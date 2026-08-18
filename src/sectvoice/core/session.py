from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import UUID, uuid4

from sectvoice.domain import AudioChunk


@dataclass(frozen=True, slots=True)
class SessionToken:
    session_id: UUID
    generation_id: int


class SessionGate:
    """Atomically invalidates every producer from an earlier playback intent."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._generation_id = 0
        self._token: SessionToken | None = None

    def begin(self) -> SessionToken:
        with self._lock:
            self._generation_id += 1
            self._token = SessionToken(uuid4(), self._generation_id)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._generation_id += 1
            self._token = None

    def current(self) -> SessionToken | None:
        with self._lock:
            return self._token

    def accepts(self, chunk: AudioChunk) -> bool:
        with self._lock:
            return (
                self._token is not None
                and chunk.session_id == self._token.session_id
                and chunk.generation_id == self._token.generation_id
            )

