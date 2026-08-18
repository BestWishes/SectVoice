from __future__ import annotations

import json
from typing import Any

from sectvoice.core.database import Database


class SettingsStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: Any = None) -> Any:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set(self, key: str, value: Any) -> None:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
                """,
                (key, serialized),
            )

