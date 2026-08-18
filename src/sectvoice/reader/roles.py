from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

from sectvoice.core.database import Database
from sectvoice.domain import Tier
from sectvoice.reader.segmentation import SegmentationMap


@dataclass(frozen=True, slots=True)
class RoleVoiceAssignment:
    document_id: UUID
    role_name: str
    voice_id: UUID
    tier: Tier


class RoleRepository:
    """Reader-owned role mapping; engines only ever receive the resolved VoiceId."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def set_units_role(
        self,
        document_id: UUID,
        mapping: SegmentationMap,
        selection_start: int,
        selection_end: int,
        role_name: str,
    ) -> tuple[UUID, ...]:
        cleaned = role_name.strip()
        if not cleaned:
            raise ValueError("role name is required")
        start, end = sorted((selection_start, selection_end))
        if start == end:
            end = min(len(mapping.source_text), end + 1)
        selected = tuple(
            unit.speech_unit_id
            for unit in mapping.units
            if unit.start_char < end and unit.end_char > start
        )
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO speech_unit_roles(document_id, speech_unit_id, role_name)
                VALUES (?, ?, ?)
                ON CONFLICT(document_id, speech_unit_id)
                DO UPDATE SET role_name=excluded.role_name
                """,
                [(str(document_id), str(unit_id), cleaned) for unit_id in selected],
            )
        return selected

    def clear_units_role(self, document_id: UUID, unit_ids: tuple[UUID, ...]) -> None:
        with self.database.connect() as connection:
            connection.executemany(
                "DELETE FROM speech_unit_roles WHERE document_id=? AND speech_unit_id=?",
                [(str(document_id), str(unit_id)) for unit_id in unit_ids],
            )

    def roles_for_units(self, document_id: UUID) -> dict[UUID, str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT speech_unit_id, role_name FROM speech_unit_roles WHERE document_id=?",
                (str(document_id),),
            ).fetchall()
        return {UUID(row["speech_unit_id"]): row["role_name"] for row in rows}

    def assign_voice(
        self, document_id: UUID, role_name: str, voice_id: UUID, tier: Tier
    ) -> RoleVoiceAssignment:
        cleaned = role_name.strip()
        if not cleaned:
            raise ValueError("role name is required")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO document_role_voices(document_id, role_name, voice_id, tier)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id, role_name) DO UPDATE SET
                    voice_id=excluded.voice_id, tier=excluded.tier
                """,
                (str(document_id), cleaned, str(voice_id), tier.value),
            )
        return RoleVoiceAssignment(document_id, cleaned, voice_id, tier)

    def voice_assignments(self, document_id: UUID) -> dict[str, RoleVoiceAssignment]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM document_role_voices WHERE document_id=? ORDER BY role_name",
                (str(document_id),),
            ).fetchall()
        return {
            row["role_name"]: RoleVoiceAssignment(
                document_id=UUID(row["document_id"]),
                role_name=row["role_name"],
                voice_id=UUID(row["voice_id"]),
                tier=Tier(row["tier"]),
            )
            for row in rows
        }


_SPEAKER_BEFORE = re.compile(r"([\u4e00-\u9fff]{1,6}?)(?:低声|大声|轻声)?(?:说道|问道|答道|喊道|说|问|喊|道|答|叫)[：:,，]?\s*$")
_SPEAKER_AFTER = re.compile(r"^\s*[，,]?([\u4e00-\u9fff]{1,6}?)(?:低声|大声|轻声)?(?:说道|问道|答道|喊道|说|问|喊|道|答|叫)")
_QUOTED = re.compile(r"[“\"]([^”\"]+)[”\"]")


def suggest_dialogue_roles(source_text: str, mapping: SegmentationMap) -> dict[UUID, str]:
    """Conservative hints only; ambiguous narration remains unassigned."""

    result: dict[UUID, str] = {}
    for match in _QUOTED.finditer(source_text):
        before = source_text[max(0, match.start() - 24) : match.start()]
        after = source_text[match.end() : min(len(source_text), match.end() + 24)]
        speaker_match = _SPEAKER_BEFORE.search(before) or _SPEAKER_AFTER.search(after)
        if speaker_match is None:
            continue
        speaker = speaker_match.group(1)
        first, _ = mapping.locate(match.start(1))
        last, _ = mapping.locate(match.end(1) - 1)
        for ordinal in range(first.ordinal, last.ordinal + 1):
            result[mapping.units[ordinal].speech_unit_id] = speaker
    return result
