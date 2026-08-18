from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 7


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS voice_profiles (
                    voice_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_audio_path TEXT NOT NULL,
                    reference_audio_path TEXT NOT NULL,
                    transcript TEXT NOT NULL,
                    language TEXT NOT NULL,
                    reference_start_seconds REAL NOT NULL DEFAULT 0,
                    reference_end_seconds REAL NOT NULL DEFAULT 0,
                    reference_sample_rate INTEGER NOT NULL DEFAULT 0,
                    reference_duration_seconds REAL NOT NULL DEFAULT 0,
                    default_settings_json TEXT NOT NULL,
                    is_available INTEGER NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engine_payloads (
                    voice_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    payload_format_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opaque_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (voice_id, tier, engine_id),
                    FOREIGN KEY (voice_id) REFERENCES voice_profiles(voice_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    current_position INTEGER NOT NULL DEFAULT 0,
                    segmentation_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS speech_units (
                    speech_unit_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    UNIQUE(document_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cache_index (
                    cache_key TEXT PRIMARY KEY,
                    audio_path TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cache_document_refs (
                    document_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    PRIMARY KEY (document_id, cache_key),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (cache_key) REFERENCES cache_index(cache_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cache_window_units (
                    cache_key TEXT NOT NULL,
                    unit_index INTEGER NOT NULL,
                    unit_text_sha256 TEXT NOT NULL,
                    signature_key TEXT NOT NULL,
                    PRIMARY KEY (cache_key, unit_index),
                    FOREIGN KEY (cache_key) REFERENCES cache_index(cache_key) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_cache_window_unit_lookup
                ON cache_window_units(signature_key, unit_text_sha256);

                CREATE TABLE IF NOT EXISTS engine_packages (
                    package_id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    runtime_path TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    installed_at TEXT NOT NULL,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS active_engine_packages (
                    tier TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    FOREIGN KEY (package_id) REFERENCES engine_packages(package_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS speech_unit_roles (
                    document_id TEXT NOT NULL,
                    speech_unit_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    PRIMARY KEY (document_id, speech_unit_id),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (speech_unit_id) REFERENCES speech_units(speech_unit_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS document_role_voices (
                    document_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    PRIMARY KEY (document_id, role_name),
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
                    FOREIGN KEY (voice_id) REFERENCES voice_profiles(voice_id) ON DELETE CASCADE
                );
                """
            )
            row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            voice_columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(voice_profiles)").fetchall()
            }
            for column_name, column_type in (
                ("reference_start_seconds", "REAL NOT NULL DEFAULT 0"),
                ("reference_end_seconds", "REAL NOT NULL DEFAULT 0"),
                ("reference_sample_rate", "INTEGER NOT NULL DEFAULT 0"),
                ("reference_duration_seconds", "REAL NOT NULL DEFAULT 0"),
            ):
                if column_name not in voice_columns:
                    connection.execute(
                        f"ALTER TABLE voice_profiles ADD COLUMN {column_name} {column_type}"
                    )
            document_columns = {
                item["name"] for item in connection.execute("PRAGMA table_info(documents)").fetchall()
            }
            if "segmentation_version" not in document_columns:
                connection.execute(
                    "ALTER TABLE documents ADD COLUMN segmentation_version INTEGER NOT NULL DEFAULT 0"
                )
            if row is None:
                connection.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {row[0]} is not supported by {SCHEMA_VERSION}"
                )
            elif row[0] < SCHEMA_VERSION:
                connection.execute("UPDATE schema_info SET version=?", (SCHEMA_VERSION,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
