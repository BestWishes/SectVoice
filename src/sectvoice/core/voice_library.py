from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sectvoice.core.database import Database
from sectvoice.domain import (
    EnginePayloadRef,
    PayloadStatus,
    Tier,
    VoiceProfile,
    utc_now_iso,
)


class VoiceLibrary:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, profile: VoiceProfile) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_profiles (
                    voice_id, name, source_audio_path, reference_audio_path,
                    transcript, language, reference_start_seconds,
                    reference_end_seconds, reference_sample_rate,
                    reference_duration_seconds, default_settings_json, is_available,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(profile.voice_id),
                    profile.name,
                    str(profile.source_audio_path),
                    str(profile.reference_audio_path),
                    profile.transcript,
                    profile.language,
                    profile.reference_start_seconds,
                    profile.reference_end_seconds,
                    profile.reference_sample_rate,
                    profile.reference_duration_seconds,
                    json.dumps(dict(profile.default_settings), ensure_ascii=False),
                    int(profile.is_available),
                    profile.last_error,
                    profile.created_at,
                    profile.updated_at,
                ),
            )

    def get(self, voice_id: UUID) -> VoiceProfile | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_profiles WHERE voice_id=?", (str(voice_id),)
            ).fetchone()
        return self._profile_from_row(row) if row is not None else None

    def list_profiles(self) -> tuple[VoiceProfile, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM voice_profiles ORDER BY name COLLATE NOCASE, created_at"
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def rename(self, voice_id: UUID, new_name: str) -> VoiceProfile:
        profile = self.get(voice_id)
        if profile is None:
            raise KeyError(voice_id)
        cleaned = new_name.strip()
        if not cleaned:
            raise ValueError("voice name is required")
        updated = replace(profile, name=cleaned, updated_at=utc_now_iso())
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE voice_profiles SET name=?, updated_at=? WHERE voice_id=?",
                (updated.name, updated.updated_at, str(voice_id)),
            )
        return updated

    def replace_reference(self, profile: VoiceProfile) -> VoiceProfile:
        """Update identity metadata and invalidate payloads compiled from old audio."""

        if self.get(profile.voice_id) is None:
            raise KeyError(profile.voice_id)
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE voice_profiles SET
                    source_audio_path=?, reference_audio_path=?, transcript=?, language=?,
                    reference_start_seconds=?, reference_end_seconds=?,
                    reference_sample_rate=?, reference_duration_seconds=?,
                    default_settings_json=?, is_available=1, last_error=NULL, updated_at=?
                WHERE voice_id=?
                """,
                (
                    str(profile.source_audio_path),
                    str(profile.reference_audio_path),
                    profile.transcript,
                    profile.language,
                    profile.reference_start_seconds,
                    profile.reference_end_seconds,
                    profile.reference_sample_rate,
                    profile.reference_duration_seconds,
                    json.dumps(dict(profile.default_settings), ensure_ascii=False),
                    profile.updated_at,
                    str(profile.voice_id),
                ),
            )
            connection.execute(
                """
                UPDATE engine_payloads
                SET status=?, sha256='', last_error=?, updated_at=?
                WHERE voice_id=?
                """,
                (
                    PayloadStatus.MISSING.value,
                    "参考音频或转写已更换，需要重新编译",
                    profile.updated_at,
                    str(profile.voice_id),
                ),
            )
        return profile

    def delete(self, voice_id: UUID) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_profiles WHERE voice_id=?", (str(voice_id),)
            )
            if cursor.rowcount == 0:
                raise KeyError(voice_id)

    def upsert_payload(self, payload: EnginePayloadRef) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO engine_payloads (
                    voice_id, tier, engine_id, engine_version,
                    payload_format_version, status, opaque_path, sha256,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(voice_id, tier, engine_id) DO UPDATE SET
                    engine_version=excluded.engine_version,
                    payload_format_version=excluded.payload_format_version,
                    status=excluded.status,
                    opaque_path=excluded.opaque_path,
                    sha256=excluded.sha256,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    str(payload.voice_id),
                    payload.tier.value,
                    payload.engine_id,
                    payload.engine_version,
                    payload.payload_format_version,
                    payload.status.value,
                    str(payload.opaque_path),
                    payload.sha256,
                    payload.last_error,
                    payload.created_at,
                    payload.updated_at,
                ),
            )

    def payloads_for(self, voice_id: UUID) -> tuple[EnginePayloadRef, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM engine_payloads WHERE voice_id=? ORDER BY tier, engine_id",
                (str(voice_id),),
            ).fetchall()
        return tuple(
            EnginePayloadRef(
                voice_id=UUID(row["voice_id"]),
                tier=Tier(row["tier"]),
                engine_id=row["engine_id"],
                engine_version=row["engine_version"],
                payload_format_version=row["payload_format_version"],
                status=PayloadStatus(row["status"]),
                opaque_path=Path(row["opaque_path"]),
                sha256=row["sha256"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                last_error=row["last_error"],
            )
            for row in rows
        )

    @staticmethod
    def _profile_from_row(row: object) -> VoiceProfile:
        return VoiceProfile(
            voice_id=UUID(row["voice_id"]),  # type: ignore[index]
            name=row["name"],  # type: ignore[index]
            source_audio_path=Path(row["source_audio_path"]),  # type: ignore[index]
            reference_audio_path=Path(row["reference_audio_path"]),  # type: ignore[index]
            transcript=row["transcript"],  # type: ignore[index]
            language=row["language"],  # type: ignore[index]
            reference_start_seconds=float(row["reference_start_seconds"]),  # type: ignore[index]
            reference_end_seconds=float(row["reference_end_seconds"]),  # type: ignore[index]
            reference_sample_rate=int(row["reference_sample_rate"]),  # type: ignore[index]
            reference_duration_seconds=float(row["reference_duration_seconds"]),  # type: ignore[index]
            created_at=row["created_at"],  # type: ignore[index]
            updated_at=row["updated_at"],  # type: ignore[index]
            default_settings=json.loads(row["default_settings_json"]),  # type: ignore[index]
            is_available=bool(row["is_available"]),  # type: ignore[index]
            last_error=row["last_error"],  # type: ignore[index]
        )
