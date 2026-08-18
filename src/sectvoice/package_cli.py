from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import traceback

from sectvoice.core.database import Database
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_catalog import DEFAULT_CATALOG_URL, PackageCatalog
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.domain import Tier
from sectvoice.paths import AppPaths


LOGGER = logging.getLogger(__name__)


def services() -> tuple[AppPaths, ModelPackageManager, PackageInstaller]:
    paths = AppPaths.discover()
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    manager = ModelPackageManager(database)
    return paths, manager, PackageInstaller(manager)


def install_local(tier_name: str) -> None:
    paths, _manager, installer = services()
    config_path = paths.source / "distribution" / "installed-packages.json"
    if not config_path.is_file():
        raise RuntimeError("安装版不包含开发机本地登记清单，请使用install-catalog")
    config = json.loads(config_path.read_text(encoding="utf-8"))[tier_name]
    record = installer.register_tested_package(
        manifest_path=paths.root / config["manifest"],
        runtime_path=paths.root / config["runtime"],
        model_path=paths.root / config["model"],
        smoke_marker=paths.root / config["smoke"],
        activate=True,
    )
    print(f"installed and active: {record.package_id}")


def install_catalog(tier_name: str, catalog_source: str) -> None:
    paths, _manager, installer = services()
    catalog = PackageCatalog.load(catalog_source)
    package = catalog.for_tier(Tier(tier_name))
    if package is None:
        raise RuntimeError(f"公开目录没有可用的{tier_name}语音包")

    def progress(name: str, current: int, total: int) -> None:
        percent = int(current * 100 / max(1, total))
        print(f"download {name}: {percent}% ({current}/{total})", flush=True)

    record = installer.install_catalog_package(
        package, paths=paths, progress=progress, activate=True
    )
    print(f"downloaded, verified and active: {record.package_id}")


def list_catalog(catalog_source: str) -> None:
    catalog = PackageCatalog.load(catalog_source)
    for package in catalog.packages:
        print(
            f"{package.tier.value}\t{package.package_id}\t{package.version}\t"
            f"{package.download_size}\t{package.display_name}"
        )


def uninstall(tier_name: str) -> None:
    paths, manager, installer = services()
    record = manager.active_for(Tier(tier_name))
    if record is None:
        raise RuntimeError(f"{tier_name} package is not active")
    marker = installer.uninstall_to_trash(
        record.package_id,
        trash_root=paths.data / "trash" / "engine-packages",
        allowed_runtime_root=paths.runtime / "engines",
        allowed_model_root=paths.models,
    )
    print(f"uninstalled to recoverable storage: {marker}")


def restore(marker: Path) -> None:
    paths, _manager, installer = services()
    record = installer.restore_from_trash(
        marker,
        allowed_runtime_root=paths.runtime / "engines",
        allowed_model_root=paths.models,
        activate=True,
    )
    print(f"restored and active: {record.package_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SectVoice independent package manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("tier", choices=("basic", "standard"))
    online_parser = subparsers.add_parser("install-catalog")
    online_parser.add_argument("tier", choices=("basic", "standard"))
    online_parser.add_argument("--catalog", default=DEFAULT_CATALOG_URL)
    list_parser = subparsers.add_parser("list-catalog")
    list_parser.add_argument("--catalog", default=DEFAULT_CATALOG_URL)
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("tier", choices=("basic", "standard"))
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("marker", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "install":
        install_local(arguments.tier)
    elif arguments.command == "install-catalog":
        install_catalog(arguments.tier, arguments.catalog)
    elif arguments.command == "list-catalog":
        list_catalog(arguments.catalog)
    elif arguments.command == "uninstall":
        uninstall(arguments.tier)
    else:
        restore(arguments.marker)
    return 0


def entrypoint() -> int:
    try:
        return main()
    except Exception as exc:
        try:
            paths = AppPaths.discover()
            log_dir = paths.data / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(
                filename=log_dir / "package-installer.log",
                level=logging.INFO,
                encoding="utf-8",
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
            )
            LOGGER.error("package operation failed\n%s", traceback.format_exc())
        except Exception:
            pass
        if sys.stderr is not None:
            print(f"SectVoice语音包操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
