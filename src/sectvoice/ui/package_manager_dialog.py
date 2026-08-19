from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_catalog import (
    DEFAULT_CATALOG_URL,
    CatalogPackage,
    PackageCatalog,
)
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.core.package_installer import package_space_requirement
from sectvoice.core.reader_update import is_version_newer
from sectvoice.domain import Tier
from sectvoice.paths import AppPaths
from sectvoice.ui.tasks import BackgroundTask
from sectvoice import __version__


LOGGER = logging.getLogger(__name__)


class PackageManagerDialog(QDialog):
    """Manages Basic/Standard packages without creating a second Reader."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        packages: ModelPackageManager,
        installer: PackageInstaller,
        engines: EngineManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.packages = packages
        self.installer = installer
        self.engines = engines
        self.catalog_source = os.environ.get(
            "SECTVOICE_PACKAGE_CATALOG", DEFAULT_CATALOG_URL
        )
        self.catalog: PackageCatalog | None = None
        self._catalog_reader_compatible = True
        self._active_tasks: set[BackgroundTask] = set()
        self.setWindowTitle("语音引擎/模型包管理")
        self.resize(980, 480)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Reader始终只有一个。基础和中级属于不同档次，可以同时启用；"
            "只有同一档安装了多个版本或引擎时才需要切换启用项。"
            "卸载只移动到SectVoice回收目录，不删除声音档案。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        online = QHBoxLayout()
        self.refresh_catalog_button = QPushButton("刷新可下载包")
        self.install_basic_button = QPushButton("下载安装基础包")
        self.install_standard_button = QPushButton("下载安装中级包")
        self.install_basic_button.setEnabled(False)
        self.install_standard_button.setEnabled(False)
        online.addWidget(self.refresh_catalog_button)
        online.addWidget(self.install_basic_button)
        online.addWidget(self.install_standard_button)
        online.addStretch(1)
        layout.addLayout(online)
        self.download_status = QLabel(
            "不会自动联网；点击“检查模型包更新”后才读取公开目录。高级包尚未发布。"
        )
        self.download_status.setObjectName("subtleText")
        self.download_status.setWordWrap(True)
        layout.addWidget(self.download_status)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setVisible(False)
        layout.addWidget(self.download_progress)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["档次", "包ID", "引擎", "版本", "状态", "路径"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_selection_actions)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.activate_button = QPushButton("启用所选包")
        self.uninstall_button = QPushButton("卸载到回收目录")
        self.restore_button = QPushButton("恢复已卸载包")
        self.refresh_button = QPushButton("刷新")
        self.close_button = QPushButton("关闭")
        for button in (
            self.activate_button,
            self.uninstall_button,
            self.restore_button,
            self.refresh_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.activate_button.clicked.connect(self._activate)
        self.uninstall_button.clicked.connect(self._uninstall)
        self.restore_button.clicked.connect(self._restore)
        self.refresh_button.clicked.connect(self.refresh)
        self.close_button.clicked.connect(self.accept)
        self.refresh_catalog_button.clicked.connect(self._refresh_catalog)
        self.install_basic_button.clicked.connect(
            lambda: self._install_catalog_tier(Tier.BASIC)
        )
        self.install_standard_button.clicked.connect(
            lambda: self._install_catalog_tier(Tier.STANDARD)
        )
        self.refresh()
        self.refresh_catalog_button.setText("检查模型包更新")

    def refresh(self) -> None:
        records = self.packages.list_installed()
        active = {
            record.package_id
            for record in (
                self.packages.active_for(record.tier) for record in records
            )
            if record is not None
        }
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (
                {"basic": "基础", "standard": "中级"}.get(
                    record.tier.value, record.tier.value
                ),
                record.package_id,
                record.engine_id,
                record.engine_version,
                "已启用" if record.package_id in active else record.status,
                str(record.model_path),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, record.package_id)
                item.setData(
                    Qt.ItemDataRole.UserRole + 1,
                    record.package_id in active,
                )
                self.table.setItem(row, column, item)
        if records:
            self.table.selectRow(0)
        self._update_selection_actions()

    def _selected_package_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _update_selection_actions(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        is_active = bool(
            item.data(Qt.ItemDataRole.UserRole + 1)
            if item is not None
            else False
        )
        self.activate_button.setText(
            "所选包已启用" if is_active else "启用所选包"
        )
        self.activate_button.setEnabled(item is not None and not is_active)
        self.uninstall_button.setEnabled(item is not None)

    def _activate(self) -> None:
        package_id = self._selected_package_id()
        if package_id is None:
            return
        try:
            self.engines.shutdown()
            self.packages.activate(package_id)
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "启用失败", str(exc))

    def _uninstall(self) -> None:
        package_id = self._selected_package_id()
        if package_id is None:
            return
        answer = QMessageBox.question(
            self,
            "卸载语音包",
            "确定卸载所选引擎/模型包？声音档案和私有Payload保留，包文件移动到回收目录。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engines.shutdown()
            marker = self.installer.uninstall_to_trash(
                package_id,
                trash_root=self.paths.data / "trash" / "engine-packages",
                allowed_runtime_root=self.paths.runtime / "engines",
                allowed_model_root=self.paths.models,
            )
            QMessageBox.information(self, "卸载完成", f"可恢复记录：{marker}")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "卸载失败", str(exc))

    def _restore(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择卸载恢复记录",
            str(self.paths.data / "trash" / "engine-packages"),
            "卸载记录 (uninstall.json)",
        )
        if not filename:
            return
        try:
            self.engines.shutdown()
            self.installer.restore_from_trash(
                Path(filename),
                allowed_runtime_root=self.paths.runtime / "engines",
                allowed_model_root=self.paths.models,
            )
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "恢复失败", str(exc))

    def _refresh_catalog(self) -> None:
        self._set_online_busy(True, "正在读取公开语音包目录……")
        task = BackgroundTask(lambda: PackageCatalog.load(self.catalog_source))
        task.signals.succeeded.connect(self._catalog_loaded)
        task.signals.failed.connect(self._catalog_refresh_failed)
        task.signals.finished.connect(lambda: self._task_finished(task))
        self._active_tasks.add(task)
        QThreadPool.globalInstance().start(task)

    def _catalog_refresh_failed(self, detail: str) -> None:
        LOGGER.error("model package catalog refresh failed\n%s", detail)
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        message = lines[-1] if lines else "未知错误"
        if ": " in message:
            message = message.split(": ", 1)[1]
        QMessageBox.critical(self, "目录读取失败", message)

    def _catalog_loaded(self, catalog: object) -> None:
        assert isinstance(catalog, PackageCatalog)
        self.catalog = catalog
        self._catalog_reader_compatible = not is_version_newer(
            catalog.reader_min_version, __version__
        )
        if not self._catalog_reader_compatible:
            self.install_basic_button.setEnabled(False)
            self.install_standard_button.setEnabled(False)
            self.download_status.setText(
                f"该模型包目录要求Reader {catalog.reader_min_version}或更高版本；"
                f"当前为{__version__}，请先返回主界面升级Reader。"
            )
            return
        basic = catalog.for_tier(Tier.BASIC)
        standard = catalog.for_tier(Tier.STANDARD)
        self._update_catalog_button(self.install_basic_button, basic, "基础")
        self._update_catalog_button(self.install_standard_button, standard, "中级")
        labels = []
        for package in (basic, standard):
            if package is not None:
                labels.append(
                    f"{package.display_name} {package.version}（{self._package_state(package)}；"
                    f"下载{_gib(package.download_size):.2f}GiB，"
                    f"安装{_gib(package.installed_size):.2f}GiB）"
                )
        self.download_status.setText("可下载：" + "；".join(labels))

    def _package_state(self, package: CatalogPackage) -> str:
        installed = self.packages.get(package.package_id)
        if installed is None:
            return "未安装"
        return "已是最新" if installed.engine_version == package.version else "可升级"

    def _update_catalog_button(
        self,
        button: QPushButton,
        package: CatalogPackage | None,
        tier_label: str,
    ) -> None:
        if package is None:
            button.setText(f"{tier_label}包尚未发布")
            button.setEnabled(False)
            return
        if not self._catalog_reader_compatible:
            QMessageBox.warning(
                self,
                "需要先升级Reader",
                f"当前Reader {__version__}不满足模型包目录要求。",
            )
            return
        installed = self.packages.get(package.package_id)
        if installed is None:
            button.setText(f"下载安装{tier_label}包")
            button.setEnabled(True)
        elif installed.engine_version == package.version:
            button.setText(f"{tier_label}包已是最新")
            button.setEnabled(False)
        else:
            button.setText(f"升级{tier_label}包")
            button.setEnabled(True)

    def _install_catalog_tier(self, tier: Tier) -> None:
        package = self.catalog.for_tier(tier) if self.catalog is not None else None
        if package is None:
            QMessageBox.warning(self, "尚未读取目录", "请先刷新可下载包目录。")
            return
        installed = self.packages.get(package.package_id)
        if installed is not None and installed.engine_version == package.version:
            QMessageBox.information(
                self,
                "已经是最新版本",
                f"{package.display_name} {package.version} 已安装并启用。",
            )
            return
        download_root = (
            self.paths.downloads
            / "engine-packages"
            / package.package_id
            / package.version
        )
        requirement = package_space_requirement(
            package, download_root=download_root
        )
        action = "升级" if installed is not None else "安装"
        answer = QMessageBox.question(
            self,
            f"{action}{package.display_name}",
            f"本次还需下载约{_gib(requirement.download_bytes):.2f}GiB，"
            f"安装完成约{_gib(requirement.installed_bytes):.2f}GiB，"
            f"安装过程至少需要{_gib(requirement.peak_free_bytes):.2f}GiB空闲空间。\n"
            f"是否继续{action}？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        device = str(package.requirements.get("device") or "").lower()
        if device == "cuda" and tier is Tier.STANDARD:
            answer = QMessageBox.question(
                self,
                "安装中级包",
                "中级包需要受支持的NVIDIA GPU和CUDA环境。继续下载安装？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.engines.shutdown()
        self._set_online_busy(True, f"正在安装{package.display_name}……")
        holder: dict[str, BackgroundTask] = {}

        def install() -> object:
            task = holder["task"]
            return self.installer.install_catalog_package(
                package,
                paths=self.paths,
                progress=lambda name, current, total: task.signals.progress.emit(
                    (name, current, total)
                ),
            )

        task = BackgroundTask(install)
        holder["task"] = task
        task.signals.progress.connect(self._download_progressed)
        task.signals.succeeded.connect(
            lambda _record: self._catalog_install_succeeded(package)
        )
        task.signals.failed.connect(self._catalog_install_failed)
        task.signals.finished.connect(lambda: self._task_finished(task))
        self._active_tasks.add(task)
        QThreadPool.globalInstance().start(task)

    def _download_progressed(self, value: object) -> None:
        name, current, total = value  # type: ignore[misc]
        percent = min(100, int(int(current) * 100 / max(1, int(total))))
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(percent)
        self.download_status.setText(
            f"当前分卷 {name}；整体进度 "
            f"{_gib(int(current)):.2f}/{_gib(int(total)):.2f}GiB"
        )

    def _catalog_install_failed(self, detail: str) -> None:
        LOGGER.error("model package installation failed\n%s", detail)
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        message = lines[-1] if lines else "未知错误"
        if ": " in message:
            message = message.split(": ", 1)[1]
        QMessageBox.critical(self, "语音包安装失败", message)

    def _catalog_install_succeeded(self, package: CatalogPackage) -> None:
        self.download_status.setText(
            f"{package.display_name} {package.version} 已校验并启用，下载分卷已清理。"
        )
        self.refresh()
        if self.catalog is not None:
            self._catalog_loaded(self.catalog)

    def _set_online_busy(self, busy: bool, message: str) -> None:
        self.refresh_catalog_button.setEnabled(not busy)
        if (
            not busy
            and self.catalog is not None
            and self._catalog_reader_compatible
        ):
            self._update_catalog_button(
                self.install_basic_button, self.catalog.for_tier(Tier.BASIC), "基础"
            )
            self._update_catalog_button(
                self.install_standard_button,
                self.catalog.for_tier(Tier.STANDARD),
                "中级",
            )
        else:
            self.install_basic_button.setEnabled(False)
            self.install_standard_button.setEnabled(False)
        self.download_progress.setVisible(busy)
        if busy:
            self.download_progress.setRange(0, 0)
        self.download_status.setText(message)

    def _task_finished(self, task: BackgroundTask) -> None:
        self._active_tasks.discard(task)
        self.download_progress.setVisible(False)
        self.refresh_catalog_button.setEnabled(True)
        if self.catalog is not None and self._catalog_reader_compatible:
            self._update_catalog_button(
                self.install_basic_button, self.catalog.for_tier(Tier.BASIC), "基础"
            )
            self._update_catalog_button(
                self.install_standard_button,
                self.catalog.for_tier(Tier.STANDARD),
                "中级",
            )


def _gib(byte_count: int) -> float:
    return byte_count / 1024**3
