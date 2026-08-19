from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from sectvoice.core.model_packages import EnginePackageRecord
from sectvoice.domain import Tier
from sectvoice.ui.package_manager_dialog import PackageManagerDialog


def _record(package_id: str, tier: Tier) -> EnginePackageRecord:
    return EnginePackageRecord(
        package_id=package_id,
        tier=tier,
        engine_id="engine",
        engine_version="1",
        manifest_path=Path("manifest.json"),
        runtime_path=Path("runtime"),
        model_path=Path("models"),
        status="installed",
        installed_at="2026-08-13T00:00:00Z",
    )


def test_activation_is_only_available_for_inactive_package_in_same_tier(
    qtbot, tmp_path
) -> None:
    active = _record("basic.active", Tier.BASIC)
    alternate = _record("basic.alternate", Tier.BASIC)
    packages = Mock()
    packages.list_installed.return_value = (active, alternate)
    packages.active_for.return_value = active
    paths = Mock()
    paths.data = tmp_path / "data"
    paths.downloads = tmp_path / "downloads"
    paths.runtime = tmp_path / "runtime"
    paths.models = tmp_path / "models"
    dialog = PackageManagerDialog(
        paths=paths,
        packages=packages,
        installer=Mock(),
        engines=Mock(),
    )
    qtbot.addWidget(dialog)

    assert dialog.install_basic_button.property("kind") is None
    assert dialog.install_standard_button.property("kind") is None

    dialog.table.selectRow(0)
    assert dialog.activate_button.text() == "所选包已启用"
    assert not dialog.activate_button.isEnabled()

    dialog.table.selectRow(1)
    assert dialog.activate_button.text() == "启用所选包"
    assert dialog.activate_button.isEnabled()
