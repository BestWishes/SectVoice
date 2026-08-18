from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import UUID

from sectvoice.core.database import Database
from sectvoice.domain import Tier, utc_now_iso


@dataclass(frozen=True, slots=True)
class SynthesisCacheIdentity:
    text: str
    voice_id: UUID
    tier: Tier
    engine_id: str
    engine_version: str
    payload_version: str
    payload_sha256: str
    reference_transcript_sha256: str
    language: str
    style: str
    emotion: str
    emotion_strength: float
    speed_mode: str
    synthesis_speed: float | None
    punctuation_pause_ms: int
    paragraph_pause_ms: int
    pcm_format: str
    postprocess_version: str

    @property
    def key(self) -> str:
        payload = asdict(self)
        payload["voice_id"] = str(self.voice_id)
        payload["tier"] = self.tier.value
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def content_signature(self) -> str:
        """Identity shared by differently grouped windows of the same voice."""

        payload = asdict(self)
        for key in ("text", "punctuation_pause_ms", "paragraph_pause_ms"):
            payload.pop(key, None)
        payload["voice_id"] = str(self.voice_id)
        payload["tier"] = self.tier.value
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    cache_key: str
    audio_path: Path
    metadata: dict[str, Any]
    byte_size: int
    duration_seconds: float
    created_at: str
    last_accessed_at: str


@dataclass(frozen=True, slots=True)
class WindowUnitCacheHit:
    entry: CacheEntry
    unit_index: int


class VoiceCache:
    """Cross-document synthesis cache owned by VoiceCore."""

    def __init__(self, database: Database, root: Path) -> None:
        self.database = database
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        identity: SynthesisCacheIdentity,
        source_audio: Path,
        duration_seconds: float,
        metadata: dict[str, Any] | None = None,
    ) -> CacheEntry:
        cache_key = identity.key
        extension = source_audio.suffix.lower() or ".bin"
        destination = self.root / cache_key[:2] / f"{cache_key}{extension}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".partial")
        shutil.copyfile(source_audio, temporary)
        temporary.replace(destination)
        now = utc_now_iso()
        serialized_metadata = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_index(
                    cache_key, audio_path, metadata_json, byte_size,
                    duration_seconds, created_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    audio_path=excluded.audio_path,
                    metadata_json=excluded.metadata_json,
                    byte_size=excluded.byte_size,
                    duration_seconds=excluded.duration_seconds,
                    last_accessed_at=excluded.last_accessed_at
                """,
                (
                    cache_key,
                    str(destination),
                    serialized_metadata,
                    destination.stat().st_size,
                    duration_seconds,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM cache_window_units WHERE cache_key=?", (cache_key,)
            )
            window_layout = dict((metadata or {}).get("window_layout") or {})
            fingerprints = list(window_layout.get("unit_text_sha256") or [])
            for unit_index, fingerprint in enumerate(fingerprints):
                connection.execute(
                    """
                    INSERT INTO cache_window_units(
                        cache_key, unit_index, unit_text_sha256, signature_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        unit_index,
                        str(fingerprint),
                        identity.content_signature,
                    ),
                )
        self.prune_to_limit(self.maximum_bytes())
        return self.get(cache_key)  # type: ignore[return-value]

    def note_document_use(self, document_id: UUID, cache_key: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO cache_document_refs(document_id, cache_key)
                VALUES (?, ?)
                """,
                (str(document_id), cache_key),
            )

    def index_window(
        self,
        identity: SynthesisCacheIdentity,
        metadata: dict[str, Any],
    ) -> None:
        """Backfills the reusable unit index for an existing exact cache hit."""

        window_layout = dict(metadata.get("window_layout") or {})
        fingerprints = list(window_layout.get("unit_text_sha256") or [])
        if not fingerprints:
            return
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM cache_window_units WHERE cache_key=?", (identity.key,)
            )
            for unit_index, fingerprint in enumerate(fingerprints):
                connection.execute(
                    """
                    INSERT INTO cache_window_units(
                        cache_key, unit_index, unit_text_sha256, signature_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        identity.key,
                        unit_index,
                        str(fingerprint),
                        identity.content_signature,
                    ),
                )

    def clear_document(self, document_id: UUID) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.cache_key, c.audio_path, c.byte_size
                FROM cache_index c
                JOIN cache_document_refs r ON r.cache_key=c.cache_key
                WHERE r.document_id=?
                """,
                (str(document_id),),
            ).fetchall()
            removed = 0
            for row in rows:
                path = Path(row["audio_path"])
                if path.is_file():
                    path.unlink()
                removed += int(row["byte_size"])
                connection.execute(
                    "DELETE FROM cache_index WHERE cache_key=?", (row["cache_key"],)
                )
        return removed

    def maximum_bytes(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key='max_cache_gb'"
            ).fetchone()
        if row is None:
            return 20 * 1024**3
        try:
            gigabytes = float(json.loads(row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            gigabytes = 20.0
        return max(0, int(gigabytes * 1024**3))

    def get(self, cache_key: str) -> CacheEntry | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cache_index WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            path = Path(row["audio_path"])
            if not path.is_file():
                connection.execute("DELETE FROM cache_index WHERE cache_key=?", (cache_key,))
                return None
            now = utc_now_iso()
            connection.execute(
                "UPDATE cache_index SET last_accessed_at=? WHERE cache_key=?", (now, cache_key)
            )
        return CacheEntry(
            cache_key=row["cache_key"],
            audio_path=path,
            metadata=json.loads(row["metadata_json"]),
            byte_size=int(row["byte_size"]),
            duration_seconds=float(row["duration_seconds"]),
            created_at=row["created_at"],
            last_accessed_at=now,
        )

    def find_window_units(
        self, *, signature_key: str, unit_text_sha256: str
    ) -> tuple[WindowUnitCacheHit, ...]:
        """Finds cached continuous windows containing a requested first unit."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT w.cache_key, w.unit_index
                FROM cache_window_units w
                JOIN cache_index c ON c.cache_key=w.cache_key
                WHERE w.signature_key=? AND w.unit_text_sha256=?
                ORDER BY c.last_accessed_at DESC
                LIMIT 32
                """,
                (signature_key, unit_text_sha256),
            ).fetchall()
        hits: list[WindowUnitCacheHit] = []
        for row in rows:
            entry = self.get(str(row["cache_key"]))
            if entry is not None:
                hits.append(WindowUnitCacheHit(entry, int(row["unit_index"])))
        return tuple(hits)

    def total_bytes(self) -> int:
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COALESCE(SUM(byte_size), 0) FROM cache_index").fetchone()[0])

    def prune_to_limit(self, maximum_bytes: int) -> int:
        if maximum_bytes < 0:
            raise ValueError("maximum_bytes cannot be negative")
        removed = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT cache_key, audio_path, byte_size FROM cache_index ORDER BY last_accessed_at ASC"
            ).fetchall()
            total = sum(int(row["byte_size"]) for row in rows)
            for row in rows:
                if total <= maximum_bytes:
                    break
                path = Path(row["audio_path"])
                if path.is_file():
                    path.unlink()
                connection.execute("DELETE FROM cache_index WHERE cache_key=?", (row["cache_key"],))
                size = int(row["byte_size"])
                total -= size
                removed += size
        return removed

    def clear_all(self) -> int:
        return self.prune_to_limit(0)
