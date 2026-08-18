import json
from pathlib import Path

from sectvoice.core.database import Database
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.domain import Tier


def make_package(tmp_path: Path, tier: str) -> tuple[Path, Path, Path]:
    runtime = tmp_path / tier / "runtime"
    model = tmp_path / tier / "model"
    runtime.mkdir(parents=True)
    model.mkdir(parents=True)
    manifest = runtime / "package-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "package_id": f"sectvoice.{tier}.test",
                "tier": tier,
                "engine_id": f"{tier}.engine",
                "engine_version": "1",
                "capabilities": {},
            }
        ),
        encoding="utf-8",
    )
    return manifest, runtime, model


def test_basic_and_standard_are_independently_registered_and_active(tmp_path: Path) -> None:
    database = Database(tmp_path / "packages.db")
    database.initialize()
    manager = ModelPackageManager(database)
    for tier in ("basic", "standard"):
        manifest, runtime, model = make_package(tmp_path, tier)
        record = manager.register(manifest, runtime, model)
        manager.activate(record.package_id)

    assert manager.active_for(Tier.BASIC).package_id == "sectvoice.basic.test"  # type: ignore[union-attr]
    assert manager.active_for(Tier.STANDARD).package_id == "sectvoice.standard.test"  # type: ignore[union-attr]
    assert len(manager.list_installed()) == 2

    manager.unregister("sectvoice.basic.test")
    assert manager.active_for(Tier.BASIC) is None
    assert manager.active_for(Tier.STANDARD) is not None


def test_uninstall_is_recoverable_and_does_not_touch_other_package(tmp_path: Path) -> None:
    database = Database(tmp_path / "packages.db")
    database.initialize()
    manager = ModelPackageManager(database)
    manifest, runtime, model = make_package(tmp_path, "basic")
    record = manager.register(manifest, runtime, model)
    manager.activate(record.package_id)
    installer = PackageInstaller(manager)

    marker = installer.uninstall_to_trash(
        record.package_id,
        trash_root=tmp_path / "trash",
        allowed_runtime_root=tmp_path,
        allowed_model_root=tmp_path,
    )
    assert marker.is_file()
    assert manager.get(record.package_id) is None
    assert not runtime.exists()
    assert not model.exists()

    restored = installer.restore_from_trash(
        marker,
        allowed_runtime_root=tmp_path,
        allowed_model_root=tmp_path,
    )
    assert restored.package_id == record.package_id
    assert runtime.is_dir()
    assert model.is_dir()
    assert manager.active_for(Tier.BASIC) is not None
