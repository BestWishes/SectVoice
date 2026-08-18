from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sectvoice.core.database import Database
from sectvoice.domain import utc_now_iso
from sectvoice.reader.segmentation import (
    SEGMENTATION_VERSION,
    SegmentationMap,
    SpeechUnit,
    segment_text,
)


@dataclass(frozen=True, slots=True)
class Document:
    document_id: UUID
    title: str
    source_text: str
    current_position: int
    created_at: str
    updated_at: str
    mapping: SegmentationMap


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    document_id: UUID
    title: str
    current_position: int
    updated_at: str


class DocumentRepository:
    def __init__(self, database: Database, max_speech_unit_chars: int = 120) -> None:
        self.database = database
        self.max_speech_unit_chars = max_speech_unit_chars

    def create(self, title: str, source_text: str = "") -> Document:
        document_id = uuid4()
        now = utc_now_iso()
        mapping = segment_text(source_text, self.max_speech_unit_chars)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents(
                    document_id, title, source_text, current_position,
                    segmentation_version, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    str(document_id),
                    title.strip() or "未命名文档",
                    source_text,
                    SEGMENTATION_VERSION,
                    now,
                    now,
                ),
            )
            self._insert_units(connection, document_id, mapping)
        return Document(document_id, title.strip() or "未命名文档", source_text, 0, now, now, mapping)

    def update_text(self, document_id: UUID, source_text: str) -> Document:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT title, current_position, created_at FROM documents WHERE document_id=?",
                (str(document_id),),
            ).fetchone()
        if row is None:
            raise KeyError(document_id)
        mapping = segment_text(source_text, self.max_speech_unit_chars)
        saved_position = int(row["current_position"])
        position = min(saved_position, max(0, len(source_text) - 1)) if source_text else 0
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE documents
                SET source_text=?, current_position=?, segmentation_version=?, updated_at=?
                WHERE document_id=?
                """,
                (source_text, position, SEGMENTATION_VERSION, now, str(document_id)),
            )
            connection.execute("DELETE FROM speech_units WHERE document_id=?", (str(document_id),))
            self._insert_units(connection, document_id, mapping)
        return Document(
            document_id=document_id,
            title=row["title"],
            source_text=source_text,
            current_position=position,
            created_at=row["created_at"],
            updated_at=now,
            mapping=mapping,
        )

    def rename(self, document_id: UUID, new_title: str) -> DocumentSummary:
        """Updates only the Reader display title; imported source files are untouched."""

        cleaned = new_title.strip()
        if not cleaned:
            raise ValueError("文档名称不能为空")
        now = utc_now_iso()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE documents
                SET title=?, updated_at=?
                WHERE document_id=?
                """,
                (cleaned, now, str(document_id)),
            )
            if cursor.rowcount == 0:
                raise KeyError(document_id)
            row = connection.execute(
                "SELECT current_position FROM documents WHERE document_id=?",
                (str(document_id),),
            ).fetchone()
        return DocumentSummary(document_id, cleaned, int(row["current_position"]), now)

    def delete(self, document_id: UUID) -> None:
        """Deletes the Reader record and its dependent metadata from SQLite."""

        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM documents WHERE document_id=?", (str(document_id),)
            )
            if cursor.rowcount == 0:
                raise KeyError(document_id)

    def set_position(self, document_id: UUID, position: int) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT length(source_text) AS text_length FROM documents WHERE document_id=?",
                (str(document_id),),
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            text_length = int(row["text_length"])
            if text_length:
                if position < 0 or position >= text_length:
                    raise IndexError(position)
            elif position != 0:
                raise IndexError(position)
            connection.execute(
                "UPDATE documents SET current_position=?, updated_at=? WHERE document_id=?",
                (position, utc_now_iso(), str(document_id)),
            )

    def get(self, document_id: UUID) -> Document:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (str(document_id),)
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            if int(row["segmentation_version"]) < SEGMENTATION_VERSION:
                self._rebuild_units(connection, document_id, row["source_text"])
            unit_rows = connection.execute(
                "SELECT * FROM speech_units WHERE document_id=? ORDER BY ordinal",
                (str(document_id),),
            ).fetchall()
        units = tuple(
            SpeechUnit(
                speech_unit_id=UUID(item["speech_unit_id"]),
                ordinal=int(item["ordinal"]),
                start_char=int(item["start_char"]),
                end_char=int(item["end_char"]),
                text=item["text"],
            )
            for item in unit_rows
        )
        mapping = SegmentationMap(row["source_text"], units)
        return Document(
            document_id=UUID(row["document_id"]),
            title=row["title"],
            source_text=row["source_text"],
            current_position=int(row["current_position"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            mapping=mapping,
        )

    def list_documents(self) -> tuple[Document, ...]:
        with self.database.connect() as connection:
            ids = [
                UUID(row["document_id"])
                for row in connection.execute(
                    "SELECT document_id FROM documents ORDER BY updated_at DESC"
                ).fetchall()
            ]
        return tuple(self.get(document_id) for document_id in ids)

    def list_summaries(self) -> tuple[DocumentSummary, ...]:
        """Lists document metadata without loading full texts or SpeechUnits."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT document_id, title, current_position, updated_at
                FROM documents
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return tuple(
            DocumentSummary(
                document_id=UUID(row["document_id"]),
                title=row["title"],
                current_position=int(row["current_position"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def _rebuild_units(self, connection, document_id: UUID, source_text: str) -> None:
        """Upgrades a saved map while preserving role ranges across new unit IDs."""

        role_ranges = connection.execute(
            """
            SELECT u.start_char, u.end_char, r.role_name
            FROM speech_unit_roles r
            JOIN speech_units u ON u.speech_unit_id=r.speech_unit_id
            WHERE r.document_id=?
            ORDER BY u.start_char
            """,
            (str(document_id),),
        ).fetchall()
        mapping = segment_text(source_text, self.max_speech_unit_chars)
        connection.execute("DELETE FROM speech_units WHERE document_id=?", (str(document_id),))
        self._insert_units(connection, document_id, mapping)
        role_index = 0
        for unit in mapping.units:
            while (
                role_index < len(role_ranges)
                and int(role_ranges[role_index]["end_char"]) <= unit.start_char
            ):
                role_index += 1
            role = None
            if (
                role_index < len(role_ranges)
                and int(role_ranges[role_index]["start_char"]) < unit.end_char
            ):
                role = role_ranges[role_index]["role_name"]
            if role is not None:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO speech_unit_roles(
                        document_id, speech_unit_id, role_name
                    ) VALUES (?, ?, ?)
                    """,
                    (str(document_id), str(unit.speech_unit_id), role),
                )
        connection.execute(
            "UPDATE documents SET segmentation_version=? WHERE document_id=?",
            (SEGMENTATION_VERSION, str(document_id)),
        )

    @staticmethod
    def _insert_units(connection, document_id: UUID, mapping: SegmentationMap) -> None:
        connection.executemany(
            """
            INSERT INTO speech_units(speech_unit_id, document_id, ordinal, start_char, end_char, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(unit.speech_unit_id),
                    str(document_id),
                    unit.ordinal,
                    unit.start_char,
                    unit.end_char,
                    unit.text,
                )
                for unit in mapping.units
            ],
        )
