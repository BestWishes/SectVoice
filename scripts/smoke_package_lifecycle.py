from __future__ import annotations

import json

from sectvoice.core.database import Database
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.domain import Tier
from sectvoice.paths import AppPaths


def main() -> int:
    paths = AppPaths.discover()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    manager = ModelPackageManager(database)
    installer = PackageInstaller(manager)
    results = []
    for tier in (Tier.BASIC, Tier.STANDARD):
        record = manager.active_for(tier)
        if record is None:
            raise RuntimeError(f"{tier.value}包未启用")
        other_tier = Tier.STANDARD if tier is Tier.BASIC else Tier.BASIC
        other_before = manager.active_for(other_tier)
        marker = installer.uninstall_to_trash(
            record.package_id,
            trash_root=paths.data / "trash" / "engine-packages",
            allowed_runtime_root=paths.runtime / "engines",
            allowed_model_root=paths.models,
        )
        independently_removed = (
            manager.active_for(tier) is None
            and manager.active_for(other_tier) is not None
            and not record.runtime_path.exists()
            and not record.model_path.exists()
        )
        restored = installer.restore_from_trash(
            marker,
            allowed_runtime_root=paths.runtime / "engines",
            allowed_model_root=paths.models,
        )
        results.append(
            {
                "tier": tier.value,
                "package_id": record.package_id,
                "independently_removed": independently_removed,
                "other_package_unchanged": other_before == manager.active_for(other_tier),
                "restored": restored.package_id == record.package_id
                and record.runtime_path.is_dir()
                and record.model_path.is_dir(),
                "marker": str(marker.with_suffix(".restored.json")),
            }
        )
    passed = all(
        item["independently_removed"]
        and item["other_package_unchanged"]
        and item["restored"]
        for item in results
    )
    report = paths.artifacts / "smoke" / "package-lifecycle-real-20260810" / "report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = {"passed": passed, "packages": results}
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
