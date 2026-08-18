from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import stat
from uuid import uuid4
import zipfile

import psutil

from sectvoice.core.model_packages import EnginePackageRecord, ModelPackageManager
from sectvoice.core.package_catalog import (
    CatalogPackage,
    DownloadProgress,
    PackageCatalogError,
    download_asset,
    safe_relative_path,
    sha256_file,
)
from sectvoice.paths import AppPaths


PACKAGE_INSTALL_SAFETY_BYTES = 512 * 1024**2


class PackageInstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PackageSpaceRequirement:
    download_bytes: int
    installed_bytes: int
    peak_free_bytes: int


def package_space_requirement(
    package: CatalogPackage,
    *,
    download_root: Path | None = None,
) -> PackageSpaceRequirement:
    """Return the conservative free-space requirement for online installation."""

    retained_bytes = (
        _retained_download_bytes(package, download_root)
        if download_root is not None
        else 0
    )
    remaining_download_bytes = max(0, package.download_size - retained_bytes)
    return PackageSpaceRequirement(
        download_bytes=remaining_download_bytes,
        installed_bytes=package.installed_size,
        peak_free_bytes=(
            remaining_download_bytes
            + package.installed_size
            + PACKAGE_INSTALL_SAFETY_BYTES
        ),
    )


class PackageInstaller:
    """Promotes a tested staging package without coupling it to Reader files."""

    def __init__(self, manager: ModelPackageManager) -> None:
        self.manager = manager

    def register_tested_package(
        self,
        *,
        manifest_path: Path,
        runtime_path: Path,
        model_path: Path,
        smoke_marker: Path,
        activate: bool = True,
    ) -> EnginePackageRecord:
        if not smoke_marker.is_file():
            raise PackageInstallError(f"缺少真实语音smoke标记：{smoke_marker}")
        marker = json.loads(smoke_marker.read_text(encoding="utf-8"))
        if marker.get("passed") is not True or not marker.get("audio_path"):
            raise PackageInstallError("模型包真实语音smoke未通过")
        if not Path(str(marker["audio_path"])).is_file():
            raise PackageInstallError("smoke音频证据不存在")
        record = self.manager.register(manifest_path, runtime_path, model_path)
        return self.manager.activate(record.package_id) if activate else record

    @staticmethod
    def install_package_code(source: Path, destination: Path) -> None:
        if destination.exists():
            raise PackageInstallError(f"包目录已存在：{destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

    def install_catalog_package(
        self,
        package: CatalogPackage,
        *,
        paths: AppPaths,
        progress: DownloadProgress | None = None,
        activate: bool = True,
    ) -> EnginePackageRecord:
        """Install an immutable multi-ZIP package with rollback-safe promotion."""

        existing = self.manager.get(package.package_id)
        if (
            existing is not None
            and existing.engine_version == package.version
            and existing.runtime_path.is_dir()
            and existing.model_path.is_dir()
        ):
            return self.manager.activate(existing.package_id) if activate else existing
        download_root = paths.downloads / "engine-packages" / package.package_id / package.version
        requirement = package_space_requirement(package, download_root=download_root)
        free_bytes = shutil.disk_usage(paths.root).free
        if free_bytes < requirement.peak_free_bytes:
            raise PackageInstallError(
                "目标磁盘空间不足："
                f"{package.display_name}需要至少{_gib(requirement.peak_free_bytes):.2f}GiB"
                f"安装空闲空间，当前只有{_gib(free_bytes):.2f}GiB；"
                f"其中下载{_gib(requirement.download_bytes):.2f}GiB，"
                f"安装完成约{_gib(requirement.installed_bytes):.2f}GiB。"
            )
        archives: list[Path] = []
        completed_bytes = 0
        for asset in package.assets:
            def asset_progress(
                name: str,
                current: int,
                _total: int,
                *,
                completed: int = completed_bytes,
            ) -> None:
                if progress is not None:
                    progress(name, completed + current, package.download_size)

            archives.append(
                download_asset(
                    asset,
                    download_root / asset.name,
                    progress=asset_progress,
                )
            )
            completed_bytes += asset.size
        staging = paths.temp / "package-install" / uuid4().hex
        staging.mkdir(parents=True, exist_ok=False)
        runtime_relative = safe_relative_path(package.runtime_relative_path)
        model_relative = safe_relative_path(package.model_relative_path)
        manifest_relative = safe_relative_path(package.manifest_relative_path)
        final_runtime = (paths.root / runtime_relative).resolve()
        final_model = (paths.root / model_relative).resolve()
        self._require_child(final_runtime, (paths.runtime / "engines").resolve())
        self._require_child(final_model, paths.models.resolve())
        promoted: list[tuple[Path, Path | None]] = []
        backups = paths.data / "trash" / "package-upgrades" / uuid4().hex
        succeeded = False
        try:
            self._extract_archives(archives, staging)
            self._verify_staging(staging, package)
            staged_runtime = staging / runtime_relative
            staged_model = staging / model_relative
            if not staged_runtime.is_dir() or not staged_model.is_dir():
                raise PackageInstallError("语音包缺少运行时或模型目录")
            for staged, final in ((staged_runtime, final_runtime), (staged_model, final_model)):
                backup: Path | None = None
                if final.exists():
                    backup = backups / final.relative_to(paths.root.resolve())
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    final.rename(backup)
                final.parent.mkdir(parents=True, exist_ok=True)
                try:
                    staged.rename(final)
                except Exception:
                    if backup is not None and backup.exists() and not final.exists():
                        backup.rename(final)
                    raise
                promoted.append((final, backup))
            manifest = (paths.root / manifest_relative).resolve()
            self._require_child(manifest, final_runtime)
            record = self.manager.register(manifest, final_runtime, final_model)
            record = self.manager.activate(record.package_id) if activate else record
            succeeded = True
            return record
        except (PackageCatalogError, zipfile.BadZipFile) as exc:
            self._rollback_promoted(promoted)
            raise PackageInstallError(str(exc)) from exc
        except Exception:
            self._rollback_promoted(promoted)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if succeeded:
                # The immutable assets can always be fetched again. Keeping a
                # second compressed copy after a successful install made the
                # advertised final disk footprint misleading.
                shutil.rmtree(download_root, ignore_errors=True)

    @staticmethod
    def _extract_archives(archives: list[Path], staging: Path) -> None:
        extracted: set[Path] = set()
        for archive_path in archives:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    relative = safe_relative_path(info.filename.rstrip("/"))
                    target = staging / relative
                    file_type = (info.external_attr >> 16) & 0o170000
                    if file_type == stat.S_IFLNK:
                        raise PackageCatalogError(f"包内不允许符号链接：{info.filename}")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if relative in extracted or target.exists():
                        raise PackageCatalogError(f"包内文件重复：{info.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    extracted.add(relative)

    @staticmethod
    def _verify_staging(staging: Path, package: CatalogPackage) -> None:
        expected = {safe_relative_path(item.path): item for item in package.files}
        actual = {
            path.relative_to(staging)
            for path in staging.rglob("*")
            if path.is_file()
        }
        if actual != set(expected):
            missing = sorted(str(path) for path in set(expected) - actual)
            extra = sorted(str(path) for path in actual - set(expected))
            raise PackageInstallError(f"语音包文件清单不一致；缺少={missing[:5]}，多出={extra[:5]}")
        for relative, item in expected.items():
            path = staging / relative
            if path.stat().st_size != item.size:
                raise PackageInstallError(f"语音包文件大小不匹配：{item.path}")
            if sha256_file(path) != item.sha256:
                raise PackageInstallError(f"语音包文件SHA-256不匹配：{item.path}")

    @staticmethod
    def _rollback_promoted(promoted: list[tuple[Path, Path | None]]) -> None:
        for final, backup in reversed(promoted):
            if final.exists():
                if final.is_dir():
                    shutil.rmtree(final)
                else:
                    final.unlink()
            if backup is not None and backup.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                backup.rename(final)

    def uninstall_to_trash(
        self,
        package_id: str,
        *,
        trash_root: Path,
        allowed_runtime_root: Path,
        allowed_model_root: Path,
    ) -> Path:
        record = self.manager.get(package_id)
        if record is None:
            raise KeyError(package_id)
        runtime = record.runtime_path.resolve()
        model = record.model_path.resolve()
        self._require_child(runtime, allowed_runtime_root.resolve())
        self._require_child(model, allowed_model_root.resolve())
        trash = trash_root / f"{package_id}-{uuid4().hex}"
        runtime_trash = trash / "runtime"
        model_trash = trash / "model"
        if runtime.drive.lower() != trash.resolve().drive.lower() or model.drive.lower() != trash.resolve().drive.lower():
            raise PackageInstallError("卸载回收目录必须与引擎和模型位于同一磁盘")
        self._require_not_in_use(runtime)
        trash.mkdir(parents=True, exist_ok=False)
        moved_runtime = False
        moved_model = False
        try:
            if runtime.exists():
                runtime.rename(runtime_trash)
                moved_runtime = True
            if model.exists():
                model.rename(model_trash)
                moved_model = True
            self.manager.unregister(package_id)
            marker = trash / "uninstall.json"
            marker.write_text(
                json.dumps(
                    {
                        "package_id": record.package_id,
                        "manifest_path": str(record.manifest_path),
                        "runtime_path": str(runtime),
                        "model_path": str(model),
                        "runtime_trash": str(runtime_trash),
                        "model_trash": str(model_trash),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return marker
        except Exception:
            if moved_runtime and runtime_trash.exists() and not runtime.exists():
                runtime.parent.mkdir(parents=True, exist_ok=True)
                runtime_trash.rename(runtime)
            if moved_model and model_trash.exists() and not model.exists():
                model.parent.mkdir(parents=True, exist_ok=True)
                model_trash.rename(model)
            raise

    def restore_from_trash(
        self,
        marker: Path,
        *,
        allowed_runtime_root: Path,
        allowed_model_root: Path,
        activate: bool = True,
    ) -> EnginePackageRecord:
        data = json.loads(marker.read_text(encoding="utf-8"))
        runtime = Path(data["runtime_path"]).resolve()
        model = Path(data["model_path"]).resolve()
        runtime_trash = Path(data["runtime_trash"]).resolve()
        model_trash = Path(data["model_trash"]).resolve()
        self._require_child(runtime, allowed_runtime_root.resolve())
        self._require_child(model, allowed_model_root.resolve())
        if runtime.exists() or model.exists():
            raise PackageInstallError("目标包目录已存在，不能覆盖恢复")
        runtime.parent.mkdir(parents=True, exist_ok=True)
        model.parent.mkdir(parents=True, exist_ok=True)
        runtime_trash.rename(runtime)
        try:
            model_trash.rename(model)
            record = self.manager.register(
                Path(data["manifest_path"]), runtime, model
            )
            if activate:
                record = self.manager.activate(record.package_id)
            marker.rename(marker.with_suffix(".restored.json"))
            return record
        except Exception:
            if runtime.exists() and not runtime_trash.exists():
                runtime.rename(runtime_trash)
            if model.exists() and not model_trash.exists():
                model.rename(model_trash)
            raise

    @staticmethod
    def _require_child(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PackageInstallError(f"包路径越界，已拒绝操作：{path}") from exc

    @staticmethod
    def _require_not_in_use(runtime: Path) -> None:
        needle = str(runtime).lower()
        for process in psutil.process_iter(("pid", "cmdline", "exe")):
            try:
                command = " ".join(process.info.get("cmdline") or ()).lower()
                executable = str(process.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if needle in command or executable.startswith(needle + "\\"):
                raise PackageInstallError(
                    f"语音包仍被进程 {process.pid} 使用；请先停止朗读并释放引擎资源"
                )


def _gib(byte_count: int) -> float:
    return byte_count / 1024**3


def _retained_download_bytes(
    package: CatalogPackage,
    download_root: Path,
) -> int:
    retained = 0
    for asset in package.assets:
        destination = download_root / asset.name
        partial = destination.with_suffix(destination.suffix + ".partial")
        # A final file and its partial are alternative states for one asset.
        # Counting only the larger state avoids claiming duplicate progress.
        sizes = [
            path.stat().st_size
            for path in (destination, partial)
            if path.is_file()
        ]
        if sizes:
            retained += min(asset.size, max(sizes))
    return retained
