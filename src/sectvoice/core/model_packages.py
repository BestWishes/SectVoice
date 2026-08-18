from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from sectvoice.core.database import Database
from sectvoice.domain import Tier, utc_now_iso


class ModelPackageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EnginePackageRecord:
    package_id: str
    tier: Tier
    engine_id: str
    engine_version: str
    manifest_path: Path
    runtime_path: Path
    model_path: Path
    status: str
    installed_at: str
    last_error: str | None = None


class ModelPackageManager:
    """Tracks independently installed tiers without importing their engines."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def register(
        self,
        manifest_path: Path,
        runtime_path: Path,
        model_path: Path,
        status: str = "installed",
    ) -> EnginePackageRecord:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = {"package_id", "tier", "engine_id", "engine_version", "capabilities"}
        missing = required - manifest.keys()
        if missing:
            raise ModelPackageError(f"package manifest is missing: {sorted(missing)}")
        tier = Tier(manifest["tier"])
        if tier is Tier.ADVANCED:
            raise ModelPackageError("Advanced is reserved and cannot be installed in version 1")
        if not runtime_path.is_dir():
            raise ModelPackageError(f"runtime directory is missing: {runtime_path}")
        if not model_path.is_dir():
            raise ModelPackageError(f"model directory is missing: {model_path}")
        record = EnginePackageRecord(
            package_id=str(manifest["package_id"]),
            tier=tier,
            engine_id=str(manifest["engine_id"]),
            engine_version=str(manifest["engine_version"]),
            manifest_path=manifest_path.resolve(),
            runtime_path=runtime_path.resolve(),
            model_path=model_path.resolve(),
            status=status,
            installed_at=utc_now_iso(),
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO engine_packages(
                    package_id, tier, engine_id, engine_version, manifest_path,
                    runtime_path, model_path, status, installed_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                    tier=excluded.tier,
                    engine_id=excluded.engine_id,
                    engine_version=excluded.engine_version,
                    manifest_path=excluded.manifest_path,
                    runtime_path=excluded.runtime_path,
                    model_path=excluded.model_path,
                    status=excluded.status,
                    last_error=excluded.last_error
                """,
                (
                    record.package_id,
                    record.tier.value,
                    record.engine_id,
                    record.engine_version,
                    str(record.manifest_path),
                    str(record.runtime_path),
                    str(record.model_path),
                    record.status,
                    record.installed_at,
                    record.last_error,
                ),
            )
        return record

    def activate(self, package_id: str) -> EnginePackageRecord:
        record = self.get(package_id)
        if record is None:
            raise KeyError(package_id)
        if record.status != "installed":
            raise ModelPackageError(f"package is not ready: {record.status}")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO active_engine_packages(tier, package_id) VALUES (?, ?)
                ON CONFLICT(tier) DO UPDATE SET package_id=excluded.package_id
                """,
                (record.tier.value, record.package_id),
            )
        return record

    def active_for(self, tier: Tier) -> EnginePackageRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT package_id FROM active_engine_packages WHERE tier=?
                """,
                (tier.value,),
            ).fetchone()
        return self.get(row["package_id"]) if row is not None else None

    def get(self, package_id: str) -> EnginePackageRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM engine_packages WHERE package_id=?", (package_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_installed(self) -> tuple[EnginePackageRecord, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM engine_packages ORDER BY tier, engine_id, engine_version"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def unregister(self, package_id: str) -> EnginePackageRecord:
        """Removes registration only; the installer owns physical deletion."""

        record = self.get(package_id)
        if record is None:
            raise KeyError(package_id)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM engine_packages WHERE package_id=?", (package_id,))
        return record

    @staticmethod
    def _from_row(row) -> EnginePackageRecord:
        return EnginePackageRecord(
            package_id=row["package_id"],
            tier=Tier(row["tier"]),
            engine_id=row["engine_id"],
            engine_version=row["engine_version"],
            manifest_path=Path(row["manifest_path"]),
            runtime_path=Path(row["runtime_path"]),
            model_path=Path(row["model_path"]),
            status=row["status"],
            installed_at=row["installed_at"],
            last_error=row["last_error"],
        )

