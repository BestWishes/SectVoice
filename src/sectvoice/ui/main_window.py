from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import os
import shutil
from uuid import UUID, uuid4

from PySide6.QtCore import QByteArray, QProcess, QThreadPool, QTimer, Qt, QUrl, Slot
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sectvoice.core.asr import ASRService
from sectvoice.core.cache import VoiceCache
from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.core.reader_update import (
    DEFAULT_READER_UPDATE_URL,
    ReaderUpdate,
    download_reader_update,
    is_version_newer,
    load_reader_update,
)
from sectvoice.core.settings import SettingsStore
from sectvoice.core.voice_compiler import VoiceCompiler
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageService
from sectvoice.core.voice_quality import basic_cross_language_warning
from sectvoice.domain import PayloadStatus, SynthesisSettings, Tier
from sectvoice.paths import AppPaths
from sectvoice.reader.documents import Document, DocumentRepository
from sectvoice.reader.playback_controller import PlaybackController
from sectvoice.reader.roles import RoleRepository, suggest_dialogue_roles
from sectvoice.ui.package_manager_dialog import PackageManagerDialog
from sectvoice.ui.tasks import BackgroundTask
from sectvoice.ui.text_editor import ReaderTextEdit
from sectvoice.ui.themes import DEFAULT_THEME_ID, ThemeManager, available_themes
from sectvoice.ui.voice_dialog import VoiceCreationDialog
from sectvoice import __version__


@dataclass(slots=True)
class MainWindowServices:
    paths: AppPaths
    documents: DocumentRepository
    voices: VoiceLibrary
    roles: RoleRepository
    settings: SettingsStore
    packages: ModelPackageManager
    package_installer: PackageInstaller
    engines: EngineManager
    media: FFmpegProcessor
    asr: ASRService
    compiler: VoiceCompiler
    voice_packages: VoicePackageService
    cache: VoiceCache
    playback: PlaybackController


@dataclass(frozen=True, slots=True)
class DocumentSaveSnapshot:
    document_id: UUID
    source_text: str
    revision: int


class MainWindow(QMainWindow):
    def __init__(
        self,
        services: MainWindowServices,
        *,
        theme_manager: ThemeManager | None = None,
    ) -> None:
        super().__init__()
        self.services = services
        self.theme_manager = theme_manager or ThemeManager()
        if theme_manager is None:
            active_theme = self.theme_manager.apply(
                str(services.settings.get("theme_id", DEFAULT_THEME_ID))
            )
            services.settings.set("theme_id", active_theme.theme_id)
        self.current_document: Document | None = None
        self._loading_text = False
        self._dirty = False
        self._document_revisions: dict[UUID, int] = {}
        self._saved_revisions: dict[UUID, int] = {}
        self._save_queue: list[DocumentSaveSnapshot] = []
        self._active_save: DocumentSaveSnapshot | None = None
        self._save_task: BackgroundTask | None = None
        self._background_tasks: set[BackgroundTask] = set()
        self._after_save_actions: dict[UUID, list[Callable[[], None]]] = {}
        self._closing_after_save = False
        self._last_highlight: tuple[int, int] | None = None
        self._preview_player = QMediaPlayer(self)
        self._preview_audio = QAudioOutput(self)
        self._preview_player.setAudioOutput(self._preview_audio)
        self.setWindowTitle("SectVoice 可定位的多角色本地朗读器")
        self.resize(1500, 920)
        self._build_ui()
        self._wire_events()
        self._restore_settings()
        self._refresh_documents()
        self._refresh_voices()
        self._open_initial_document()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 7)
        root_layout.setSpacing(10)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(self.splitter, 1)

        left = QWidget()
        left.setObjectName("sidePanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(8)
        document_section_title = QLabel("文档")
        document_section_title.setObjectName("sectionTitle")
        left_layout.addWidget(document_section_title)
        self.document_list = QListWidget()
        self.document_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        left_layout.addWidget(self.document_list, 2)
        doc_buttons = QHBoxLayout()
        self.new_document_button = QPushButton("新建")
        self.import_document_button = QPushButton("导入TXT")
        doc_buttons.addWidget(self.new_document_button)
        doc_buttons.addWidget(self.import_document_button)
        left_layout.addLayout(doc_buttons)
        document_manage_buttons = QHBoxLayout()
        self.rename_document_button = QPushButton("修改名称")
        self.delete_document_button = QPushButton("删除")
        self.delete_document_button.setProperty("kind", "danger")
        document_manage_buttons.addWidget(self.rename_document_button)
        document_manage_buttons.addWidget(self.delete_document_button)
        left_layout.addLayout(document_manage_buttons)
        voice_section_title = QLabel("声音档案")
        voice_section_title.setObjectName("sectionTitle")
        left_layout.addWidget(voice_section_title)
        self.voice_list = QListWidget()
        left_layout.addWidget(self.voice_list, 2)
        voice_buttons_1 = QHBoxLayout()
        self.new_voice_button = QPushButton("新建声音")
        self.rename_voice_button = QPushButton("改名")
        self.delete_voice_button = QPushButton("删除")
        self.delete_voice_button.setProperty("kind", "danger")
        for button in (self.new_voice_button, self.rename_voice_button, self.delete_voice_button):
            voice_buttons_1.addWidget(button)
        left_layout.addLayout(voice_buttons_1)
        voice_buttons_2 = QHBoxLayout()
        self.preview_voice_button = QPushButton("试听")
        self.replace_voice_button = QPushButton("更换参考")
        self.recompile_voice_button = QPushButton("重编译")
        for button in (
            self.preview_voice_button,
            self.replace_voice_button,
            self.recompile_voice_button,
        ):
            voice_buttons_2.addWidget(button)
        left_layout.addLayout(voice_buttons_2)
        voice_buttons_3 = QHBoxLayout()
        self.import_voice_button = QPushButton("导入.voicepkg")
        self.export_voice_button = QPushButton("导出.voicepkg")
        voice_buttons_3.addWidget(self.import_voice_button)
        voice_buttons_3.addWidget(self.export_voice_button)
        left_layout.addLayout(voice_buttons_3)
        self.splitter.addWidget(left)

        center = QWidget()
        center.setObjectName("readerPanel")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(14, 12, 14, 14)
        center_layout.setSpacing(10)
        center_header = QHBoxLayout()
        self.document_title = QLabel("未打开文档")
        self.document_title.setObjectName("documentTitle")
        self.font_down = QToolButton()
        self.font_down.setText("A−")
        self.font_up = QToolButton()
        self.font_up.setText("A+")
        self.line_spacing = QDoubleSpinBox()
        self.line_spacing.setRange(1.0, 2.5)
        self.line_spacing.setSingleStep(0.1)
        self.line_spacing.setPrefix("行距 ")
        center_header.addWidget(self.document_title, 1)
        center_header.addWidget(self.font_down)
        center_header.addWidget(self.font_up)
        center_header.addWidget(self.line_spacing)
        center_layout.addLayout(center_header)
        self.editor = ReaderTextEdit()
        self.editor.setObjectName("readerText")
        self.editor.setPlaceholderText("在此粘贴文字，或从左侧导入 UTF-8 TXT。双击任意字符即可跳转朗读。")
        self.editor.setLineWrapMode(ReaderTextEdit.LineWrapMode.WidgetWidth)
        center_layout.addWidget(self.editor, 1)
        self.splitter.addWidget(center)

        right = QWidget()
        right.setObjectName("settingsPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.theme_combo = QComboBox()
        for theme in available_themes():
            self.theme_combo.addItem(theme.display_name, theme.theme_id)
        theme_index = self.theme_combo.findData(self.theme_manager.current_theme_id)
        self.theme_combo.setCurrentIndex(max(0, theme_index))
        self.tier_combo = QComboBox()
        self.tier_combo.addItem("基础（CPU轻量）", Tier.BASIC.value)
        self.tier_combo.addItem("中级（GPU高质量）", Tier.STANDARD.value)
        self.tier_combo.addItem("高级（第一版未提供）", Tier.ADVANCED.value)
        self.voice_combo = QComboBox()
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setValue(1.0)
        self.volume_spin = QDoubleSpinBox()
        self.volume_spin.setRange(0.0, 2.0)
        self.volume_spin.setSingleStep(0.05)
        self.volume_spin.setValue(1.0)
        self.emotion_combo = QComboBox()
        self.emotion_combo.addItem("自然（当前引擎无独立情绪控制）", "natural")
        self.emotion_combo.setEnabled(False)
        self.punctuation_pause = QSpinBox()
        self.punctuation_pause.setRange(0, 2000)
        self.punctuation_pause.setValue(120)
        self.paragraph_pause = QSpinBox()
        self.paragraph_pause.setRange(0, 5000)
        self.paragraph_pause.setValue(280)
        form.addRow("界面主题", self.theme_combo)
        form.addRow("语音档次", self.tier_combo)
        form.addRow("当前声音", self.voice_combo)
        form.addRow("语速", self.speed_spin)
        form.addRow("音量", self.volume_spin)
        form.addRow("情绪", self.emotion_combo)
        form.addRow("标点停顿(ms)", self.punctuation_pause)
        form.addRow("段落停顿(ms)", self.paragraph_pause)
        right_layout.addLayout(form)

        role_section_title = QLabel("角色声音（按SpeechUnit分配）")
        role_section_title.setObjectName("sectionTitle")
        right_layout.addWidget(role_section_title)
        self.role_list = QListWidget()
        right_layout.addWidget(self.role_list, 1)
        role_buttons = QVBoxLayout()
        self.mark_role_button = QPushButton("将选中文字标记为角色")
        self.analyze_roles_button = QPushButton("根据“某某说/问”分析对话（需确认）")
        self.assign_role_voice_button = QPushButton("把当前声音分配给所选角色")
        for button in (self.mark_role_button, self.analyze_roles_button, self.assign_role_voice_button):
            role_buttons.addWidget(button)
        right_layout.addLayout(role_buttons)
        self.cache_label = QLabel("缓存占用：0 MiB")
        self.max_cache_gb = QSpinBox()
        self.max_cache_gb.setRange(1, 200)
        self.max_cache_gb.setSuffix(" GB")
        self.clear_document_cache_button = QPushButton("清理当前文档缓存")
        self.clear_cache_button = QPushButton("清理全部语音缓存")
        self.release_engines_button = QPushButton("释放引擎内存/显存")
        self.package_manager_button = QPushButton("管理语音引擎/模型包")
        self.reader_update_button = QPushButton(f"检查Reader更新（当前 {__version__}）")
        right_layout.addWidget(self.cache_label)
        cache_limit_label = QLabel("最大缓存空间")
        cache_limit_label.setObjectName("subtleText")
        right_layout.addWidget(cache_limit_label)
        right_layout.addWidget(self.max_cache_gb)
        right_layout.addWidget(self.clear_document_cache_button)
        right_layout.addWidget(self.clear_cache_button)
        right_layout.addWidget(self.release_engines_button)
        right_layout.addWidget(self.package_manager_button)
        right_layout.addWidget(self.reader_update_button)
        right_layout.addStretch(1)
        self.splitter.addWidget(right)
        self.splitter.setSizes([300, 900, 300])

        playback_bar = QWidget()
        playback_bar.setObjectName("playbackBar")
        controls = QHBoxLayout(playback_bar)
        controls.setContentsMargins(10, 7, 10, 7)
        controls.setSpacing(8)
        self.previous_button = QPushButton("上一句")
        self.play_button = QPushButton("播放")
        self.play_button.setProperty("kind", "primary")
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("停止")
        self.next_button = QPushButton("下一句")
        self.retry_button = QPushButton("重试当前句")
        for button in (
            self.previous_button,
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.next_button,
            self.retry_button,
        ):
            controls.addWidget(button)
        self.playback_position_label = QLabel("位置 0")
        controls.addStretch(1)
        controls.addWidget(self.playback_position_label)
        root_layout.addWidget(playback_bar)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Idle")

        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(700)
        self.restart_timer = QTimer(self)
        self.restart_timer.setSingleShot(True)
        self.restart_timer.setInterval(350)

    def _wire_events(self) -> None:
        self.new_document_button.clicked.connect(self._new_document)
        self.import_document_button.clicked.connect(self._import_document)
        self.rename_document_button.clicked.connect(self._rename_document)
        self.delete_document_button.clicked.connect(self._delete_document)
        self.document_list.currentItemChanged.connect(self._document_selected)
        self.editor.textChanged.connect(self._text_changed)
        self.editor.characterDoubleClicked.connect(self._double_click_play)
        self.save_timer.timeout.connect(self._save_current_text)
        self.new_voice_button.clicked.connect(self._new_voice)
        self.rename_voice_button.clicked.connect(self._rename_voice)
        self.delete_voice_button.clicked.connect(self._delete_voice)
        self.preview_voice_button.clicked.connect(self._preview_selected_voice)
        self.replace_voice_button.clicked.connect(self._replace_voice_reference)
        self.recompile_voice_button.clicked.connect(self._recompile_voice)
        self.import_voice_button.clicked.connect(self._guard(self._import_voice_package))
        self.export_voice_button.clicked.connect(self._guard(self._export_voice_package))
        self.voice_combo.currentIndexChanged.connect(self._voice_settings_changed)
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        self.tier_combo.currentIndexChanged.connect(self._tier_changed)
        self.speed_spin.valueChanged.connect(self._synthesis_setting_changed)
        self.volume_spin.valueChanged.connect(self._volume_changed)
        self.punctuation_pause.valueChanged.connect(self._synthesis_setting_changed)
        self.paragraph_pause.valueChanged.connect(self._synthesis_setting_changed)
        self.restart_timer.timeout.connect(self._restart_if_playing)
        self.previous_button.clicked.connect(self._guard(self.services.playback.previous))
        self.play_button.clicked.connect(self._guard(self._play_or_resume))
        self.pause_button.clicked.connect(self.services.playback.pause)
        self.stop_button.clicked.connect(self.services.playback.stop)
        self.next_button.clicked.connect(self._guard(self.services.playback.next))
        self.retry_button.clicked.connect(self._guard(self.services.playback.play))
        self.services.playback.stateChanged.connect(self._playback_state)
        self.services.playback.currentUnitChanged.connect(self._highlight_unit)
        self.services.playback.currentPositionChanged.connect(
            lambda position: self.playback_position_label.setText(f"位置 {position}")
        )
        self.services.playback.error.connect(
            lambda message: QMessageBox.critical(self, "朗读失败", message)
        )
        self.font_up.clicked.connect(lambda: self._change_font(1))
        self.font_down.clicked.connect(lambda: self._change_font(-1))
        self.line_spacing.valueChanged.connect(self._set_line_spacing)
        self.mark_role_button.clicked.connect(self._mark_selected_role)
        self.analyze_roles_button.clicked.connect(self._analyze_roles)
        self.assign_role_voice_button.clicked.connect(self._assign_role_voice)
        self.clear_cache_button.clicked.connect(self._clear_cache)
        self.clear_document_cache_button.clicked.connect(self._clear_document_cache)
        self.max_cache_gb.valueChanged.connect(self._cache_limit_changed)
        self.release_engines_button.clicked.connect(self._release_engines)
        self.package_manager_button.clicked.connect(self._manage_packages)
        self.reader_update_button.clicked.connect(self._check_reader_update)

    def _restore_settings(self) -> None:
        store = self.services.settings
        geometry = store.get("window_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        sizes = store.get("splitter_sizes")
        if sizes:
            self.splitter.setSizes([int(item) for item in sizes])
        self.speed_spin.setValue(float(store.get("speed", 1.0)))
        self.volume_spin.setValue(float(store.get("volume", 1.0)))
        timing_profile_version = int(store.get("reader_timing_profile_version", 1))
        punctuation_pause = int(store.get("punctuation_pause_ms", 180))
        paragraph_pause = int(store.get("paragraph_pause_ms", 500))
        if timing_profile_version < 2:
            if punctuation_pause == 180:
                punctuation_pause = 120
            if paragraph_pause == 500:
                paragraph_pause = 280
            store.set("punctuation_pause_ms", punctuation_pause)
            store.set("paragraph_pause_ms", paragraph_pause)
            store.set("reader_timing_profile_version", 2)
        self.punctuation_pause.setValue(punctuation_pause)
        self.paragraph_pause.setValue(paragraph_pause)
        tier = str(store.get("tier", Tier.BASIC.value))
        index = self.tier_combo.findData(tier)
        self.tier_combo.setCurrentIndex(max(0, index))
        self._set_font_size(int(store.get("font_size", 18)))
        self.line_spacing.setValue(float(store.get("line_spacing", 1.4)))
        self.max_cache_gb.setValue(int(store.get("max_cache_gb", 20)))

    def _theme_changed(self) -> None:
        theme_id = str(self.theme_combo.currentData() or DEFAULT_THEME_ID)
        theme = self.theme_manager.apply(theme_id)
        self.services.settings.set("theme_id", theme.theme_id)
        if self._last_highlight is not None:
            self._highlight_unit("", *self._last_highlight)

    def _refresh_documents(self, select_id: UUID | None = None) -> None:
        selected = select_id or (self.current_document.document_id if self.current_document else None)
        self.document_list.blockSignals(True)
        self.document_list.clear()
        target_row = -1
        for row, document in enumerate(self.services.documents.list_summaries()):
            item = QListWidgetItem(document.title)
            item.setData(Qt.ItemDataRole.UserRole, str(document.document_id))
            self.document_list.addItem(item)
            if document.document_id == selected:
                target_row = row
        self.document_list.blockSignals(False)
        if target_row >= 0:
            self.document_list.setCurrentRow(target_row)

    def _refresh_voices(self, select_id: UUID | None = None) -> None:
        selected_text = (
            str(select_id)
            if select_id
            else self.voice_combo.currentData()
            or self.services.settings.get("selected_voice_id")
        )
        profiles = self.services.voices.list_profiles()
        self.voice_list.clear()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        selected_index = -1
        for index, profile in enumerate(profiles):
            payloads = self.services.voices.payloads_for(profile.voice_id)
            status = {item.tier: item.status for item in payloads}
            cross_language_warning = (
                basic_cross_language_warning(profile.language)
                if status.get(Tier.BASIC) is PayloadStatus.READY
                else None
            )
            badges = (
                f"基础{'✓' if status.get(Tier.BASIC) is PayloadStatus.READY else '—'}  "
                f"中级{'✓' if status.get(Tier.STANDARD) is PayloadStatus.READY else '—'}"
            )
            if cross_language_warning is not None:
                badges += "  跨语种参考⚠"
            item = QListWidgetItem(f"{profile.name}    {badges}")
            errors = [payload.last_error for payload in payloads if payload.last_error]
            notices = [*errors]
            if cross_language_warning is not None:
                notices.append(cross_language_warning)
            item.setToolTip("\n".join(notices) if notices else "声音档案可用")
            item.setData(Qt.ItemDataRole.UserRole, str(profile.voice_id))
            self.voice_list.addItem(item)
            self.voice_combo.addItem(profile.name, str(profile.voice_id))
            if str(profile.voice_id) == selected_text:
                selected_index = index
        self.voice_combo.setCurrentIndex(selected_index if selected_index >= 0 else (0 if profiles else -1))
        if profiles:
            self.voice_list.setCurrentRow(
                selected_index if selected_index >= 0 else 0
            )
        self.voice_combo.blockSignals(False)
        self._voice_settings_changed()
        self._refresh_roles()
        self._update_cache_label()

    def _open_initial_document(self) -> None:
        summaries = self.services.documents.list_summaries()
        if not summaries:
            document = self.services.documents.create("新文档")
            self._refresh_documents(document.document_id)
            self._load_document(document)
            return
        saved = self.services.settings.get("last_document_id")
        summary = next(
            (item for item in summaries if str(item.document_id) == saved),
            summaries[0],
        )
        self._refresh_documents(summary.document_id)
        self._load_document(self.services.documents.get(summary.document_id))

    def _load_document(self, document: Document) -> None:
        self.current_document = document
        self._document_revisions.setdefault(document.document_id, 0)
        self._saved_revisions.setdefault(document.document_id, 0)
        self._loading_text = True
        self.editor.setPlainText(document.source_text)
        self._loading_text = False
        self._dirty = False
        self.document_title.setText(document.title)
        self.services.playback.set_document(document)
        self._set_line_spacing(self.line_spacing.value())
        cursor = self.editor.textCursor()
        cursor.setPosition(min(document.current_position, len(document.source_text)))
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self._refresh_roles()

    @Slot(QListWidgetItem, QListWidgetItem)
    def _document_selected(self, current, _previous) -> None:
        if current is None:
            return
        self._save_current_text()
        try:
            self._load_document(self.services.documents.get(UUID(current.data(Qt.ItemDataRole.UserRole))))
        except Exception as exc:
            QMessageBox.critical(self, "打开文档失败", str(exc))

    def _new_document(self) -> None:
        title, ok = QInputDialog.getText(self, "新建文档", "文档名称")
        if ok:
            document = self.services.documents.create(title or "新文档")
            self._refresh_documents(document.document_id)
            self._load_document(document)

    def _rename_document(self) -> None:
        item = self.document_list.currentItem()
        if item is None:
            return
        document_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        title, ok = QInputDialog.getText(
            self,
            "修改文档名称",
            "只修改朗读器中显示的名称，不会修改原始TXT文件名：",
            text=item.text(),
        )
        if not ok:
            return

        def rename_saved_document() -> None:
            summary = self.services.documents.rename(document_id, title)
            if self.current_document is not None and self.current_document.document_id == document_id:
                self.current_document = replace(
                    self.current_document,
                    title=summary.title,
                    updated_at=summary.updated_at,
                )
                self.document_title.setText(summary.title)
            self._refresh_documents(document_id)
            self.statusBar().showMessage("文档名称已修改", 2500)

        if self.current_document is not None and self.current_document.document_id == document_id:
            self._after_current_text_saved(self._guard(rename_saved_document))
        else:
            self._guard(rename_saved_document)()

    def _delete_document(self) -> None:
        item = self.document_list.currentItem()
        if item is None:
            return
        document_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        title = item.text()
        selected_row = self.document_list.currentRow()
        answer = QMessageBox.question(
            self,
            "删除文档",
            (
                f"确定从朗读器中删除文档“{title}”？\n\n"
                "这会删除朗读器内保存的文本、阅读位置和角色设置，"
                "但不会删除导入时的原始TXT文件。"
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def delete_saved_document() -> None:
            self.services.playback.stop()
            self.services.documents.delete(document_id)
            self._document_revisions.pop(document_id, None)
            self._saved_revisions.pop(document_id, None)
            self._after_save_actions.pop(document_id, None)
            if self.current_document is not None and self.current_document.document_id == document_id:
                self.current_document = None
                self._dirty = False
                self._last_highlight = None

            summaries = self.services.documents.list_summaries()
            if summaries:
                next_row = min(max(0, selected_row), len(summaries) - 1)
                next_document = self.services.documents.get(summaries[next_row].document_id)
            else:
                next_document = self.services.documents.create("新文档")
            self._refresh_documents(next_document.document_id)
            self._load_document(next_document)
            self.services.settings.set("last_document_id", str(next_document.document_id))
            self.statusBar().showMessage(f"已删除文档“{title}”", 2500)

        if self.current_document is not None and self.current_document.document_id == document_id:
            self._after_save_actions.pop(document_id, None)
            self._after_current_text_saved(self._guard(delete_saved_document))
        else:
            self._guard(delete_saved_document)()

    def _import_document(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "导入UTF-8 TXT", "", "文本 (*.txt)")
        if not filename:
            return
        self.import_document_button.setEnabled(False)
        self.statusBar().showMessage("正在后台导入并建立朗读位置映射……")

        def load_document_file() -> Document:
            try:
                text = Path(filename).read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("第一版只支持UTF-8编码TXT。") from exc
            return self.services.documents.create(Path(filename).stem, text)

        task = BackgroundTask(load_document_file)

        def completed(document: Document) -> None:
            self.import_document_button.setEnabled(True)
            self._refresh_documents(document.document_id)
            self._load_document(document)
            self.statusBar().showMessage("文档导入完成", 2500)

        def failed(detail: str) -> None:
            self.import_document_button.setEnabled(True)
            QMessageBox.critical(self, "导入失败", detail[-5000:])

        task.signals.succeeded.connect(completed)
        task.signals.failed.connect(failed)
        self._start_retained_task(task)

    def _text_changed(self) -> None:
        if self._loading_text:
            return
        if self.current_document is None:
            return
        if not self._dirty:
            # The existing SpeechUnit map no longer matches the editor. Stop
            # immediately; a later Play/Seek waits for the background rebuild.
            self.services.playback.stop()
        document_id = self.current_document.document_id
        self._document_revisions[document_id] = (
            self._document_revisions.get(document_id, 0) + 1
        )
        self._dirty = True
        self.save_timer.start()

    def _save_current_text(self) -> None:
        if not self._dirty or self.current_document is None:
            return
        document_id = self.current_document.document_id
        snapshot = DocumentSaveSnapshot(
            document_id,
            self.editor.toPlainText(),
            self._document_revisions.get(document_id, 0),
        )
        self._dirty = False
        self._enqueue_document_save(snapshot)

    def _enqueue_document_save(self, snapshot: DocumentSaveSnapshot) -> None:
        # Keep only the newest not-yet-started snapshot for one document. The
        # active snapshot must finish first so an older write can never land
        # after a newer one.
        for index, queued in enumerate(self._save_queue):
            if queued.document_id == snapshot.document_id:
                self._save_queue[index] = snapshot
                break
        else:
            self._save_queue.append(snapshot)
        self.statusBar().showMessage("正在后台保存并建立朗读位置映射……")
        self._start_next_document_save()

    def _start_next_document_save(self) -> None:
        if self._active_save is not None or not self._save_queue:
            return
        snapshot = self._save_queue.pop(0)
        self._active_save = snapshot
        task = BackgroundTask(
            lambda: self.services.documents.update_text(
                snapshot.document_id, snapshot.source_text
            )
        )
        self._save_task = task
        task.signals.succeeded.connect(
            lambda document, snapshot=snapshot: self._document_save_succeeded(
                snapshot, document
            )
        )
        task.signals.failed.connect(
            lambda detail, snapshot=snapshot: self._document_save_failed(snapshot, detail)
        )
        self._start_retained_task(task)

    def _start_retained_task(self, task: BackgroundTask) -> None:
        """Keeps QRunnable wrappers alive until all queued Qt signals arrive."""

        self._background_tasks.add(task)
        task.signals.finished.connect(
            lambda task=task: self._background_tasks.discard(task)
        )
        QThreadPool.globalInstance().start(task)

    def _document_save_succeeded(
        self, snapshot: DocumentSaveSnapshot, document: Document
    ) -> None:
        if self._active_save != snapshot:
            return
        self._saved_revisions[snapshot.document_id] = max(
            snapshot.revision,
            self._saved_revisions.get(snapshot.document_id, 0),
        )
        current_id = self.current_document.document_id if self.current_document else None
        if (
            current_id == snapshot.document_id
            and snapshot.revision
            == self._document_revisions.get(snapshot.document_id, 0)
            and not self._dirty
        ):
            self.current_document = document
            self.services.playback.set_document(document)
            self.statusBar().showMessage("文档已自动保存", 1500)
        self._active_save = None
        self._save_task = None
        self._start_next_document_save()
        self._run_ready_after_save_actions(snapshot.document_id)
        self._finish_deferred_close_if_ready()

    def _document_save_failed(
        self, snapshot: DocumentSaveSnapshot, detail: str
    ) -> None:
        if self._active_save != snapshot:
            return
        current_id = self.current_document.document_id if self.current_document else None
        if current_id == snapshot.document_id:
            self._dirty = True
        self._after_save_actions.pop(snapshot.document_id, None)
        self._active_save = None
        self._save_task = None
        self._closing_after_save = False
        self._start_next_document_save()
        QMessageBox.critical(self, "文档保存失败", detail[-5000:])

    def _has_pending_save(self, document_id: UUID) -> bool:
        return bool(
            (self._active_save and self._active_save.document_id == document_id)
            or any(item.document_id == document_id for item in self._save_queue)
        )

    def _after_current_text_saved(self, action: Callable[[], None]) -> None:
        if self.current_document is None:
            return
        document_id = self.current_document.document_id
        self._save_current_text()
        if self._has_pending_save(document_id):
            self._after_save_actions.setdefault(document_id, []).append(action)
            self.statusBar().showMessage("正在后台准备大文档，完成后自动继续……")
            return
        action()

    def _run_ready_after_save_actions(self, document_id: UUID) -> None:
        if self._has_pending_save(document_id):
            return
        current_id = self.current_document.document_id if self.current_document else None
        if current_id != document_id:
            self._after_save_actions.pop(document_id, None)
            return
        current_revision = self._document_revisions.get(document_id, 0)
        if self._dirty or self._saved_revisions.get(document_id, -1) < current_revision:
            return
        actions = self._after_save_actions.pop(document_id, [])
        for action in actions:
            QTimer.singleShot(0, action)

    def _finish_deferred_close_if_ready(self) -> None:
        if (
            self._closing_after_save
            and not self._dirty
            and self._active_save is None
            and not self._save_queue
        ):
            self._closing_after_save = False
            QTimer.singleShot(0, self.close)

    def _double_click_play(self, position: int) -> None:
        def play_from_saved_position() -> None:
            self._update_playback_settings()
            self._guard(lambda: self.services.playback.play_from(position))()

        self._after_current_text_saved(play_from_saved_position)

    def _play_or_resume(self) -> None:
        def play_saved_document() -> None:
            self._update_playback_settings()
            if self.services.playback.state == "Paused":
                self.services.playback.resume()
            else:
                self.services.playback.play()

        self._after_current_text_saved(play_saved_document)

    def _tier_changed(self) -> None:
        tier = Tier(self.tier_combo.currentData())
        if tier is Tier.ADVANCED:
            QMessageBox.information(self, "高级语音包", "高级档次只保留扩展位置，第一版不安装。")
            self.tier_combo.setCurrentIndex(0)
            return
        if self.services.packages.active_for(tier) is None:
            self.statusBar().showMessage(f"{tier.value}语音包尚未安装或启用")
        self.services.playback.stop()
        self.services.engines.unload_except(tier)
        self._voice_settings_changed()

    def _voice_settings_changed(self) -> None:
        value = self.voice_combo.currentData()
        if not value:
            return
        tier = Tier(self.tier_combo.currentData())
        if tier is Tier.ADVANCED:
            return
        voice_id = UUID(value)
        self.services.playback.set_voice(voice_id, tier)
        profile = self.services.voices.get(voice_id)
        warning = (
            basic_cross_language_warning(profile.language)
            if tier is Tier.BASIC and profile is not None
            else None
        )
        if warning is not None:
            self.statusBar().showMessage(warning, 12000)

    def _update_playback_settings(self) -> None:
        self._voice_settings_changed()
        self.services.playback.set_settings(
            SynthesisSettings(
                speed=self.speed_spin.value(),
                volume=self.volume_spin.value(),
                emotion="natural",
                punctuation_pause_ms=self.punctuation_pause.value(),
                paragraph_pause_ms=self.paragraph_pause.value(),
            )
        )

    def _synthesis_setting_changed(self) -> None:
        self._update_playback_settings()
        self.services.settings.set("speed", self.speed_spin.value())
        self.services.settings.set(
            "punctuation_pause_ms", self.punctuation_pause.value()
        )
        self.services.settings.set("paragraph_pause_ms", self.paragraph_pause.value())
        if self.services.playback.state in {"Playing", "Generating"}:
            self.restart_timer.start()

    def _volume_changed(self) -> None:
        self._update_playback_settings()
        self.services.settings.set("volume", self.volume_spin.value())

    def _restart_if_playing(self) -> None:
        if self.services.playback.state in {"Playing", "Generating"}:
            self._guard(
                lambda: self.services.playback.play_from(self.services.playback.current_position)
            )()

    def _new_voice(self) -> None:
        dialog = VoiceCreationDialog(
            paths=self.services.paths,
            media=self.services.media,
            asr=self.services.asr,
            compiler=self.services.compiler,
            engines=self.services.engines,
            packages=self.services.packages,
            voices=self.services.voices,
            parent=self,
        )
        dialog.voiceCreated.connect(lambda value: self._refresh_voices(UUID(value)))
        dialog.exec()

    def _replace_voice_reference(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        profile = self.services.voices.get(voice_id)
        if profile is None:
            return
        self.services.playback.stop()
        self.services.engines.shutdown()
        dialog = VoiceCreationDialog(
            paths=self.services.paths,
            media=self.services.media,
            asr=self.services.asr,
            compiler=self.services.compiler,
            engines=self.services.engines,
            packages=self.services.packages,
            voices=self.services.voices,
            existing_profile=profile,
            parent=self,
        )
        dialog.voiceCreated.connect(lambda value: self._refresh_voices(UUID(value)))
        dialog.exec()

    def _preview_selected_voice(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        preview = (
            self.services.paths.data
            / "voices"
            / str(voice_id)
            / "previews"
            / "creation-test.wav"
        )
        if not preview.is_file():
            QMessageBox.information(
                self,
                "没有试听文件",
                "这个声音包没有保存试听音频；可以在正文中选择声音后直接播放。",
            )
            return
        self._preview_player.setSource(QUrl.fromLocalFile(str(preview)))
        self._preview_player.play()

    def _recompile_voice(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        profile = self.services.voices.get(voice_id)
        tier = Tier(self.tier_combo.currentData())
        if profile is None or tier is Tier.ADVANCED:
            return
        self.services.playback.stop()
        self.statusBar().showMessage(f"正在重新编译{tier.value}声音数据……")
        task = BackgroundTask(
            lambda: self.services.engines.compile_voice(
                profile, tier, self.services.compiler
            )
        )

        def completed(_value) -> None:
            self._refresh_voices(voice_id)
            self.statusBar().showMessage("声音数据编译完成", 3000)

        task.signals.succeeded.connect(completed)
        task.signals.failed.connect(
            lambda detail: QMessageBox.critical(self, "声音编译失败", detail[-5000:])
        )
        self._start_retained_task(task)

    def _import_voice_package(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "导入声音包", "", "SectVoice声音包 (*.voicepkg)"
        )
        if not filename:
            return
        profile = self.services.voice_packages.import_package(Path(filename))
        self._refresh_voices(profile.voice_id)

    def _export_voice_package(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        profile = self.services.voices.get(voice_id)
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出声音包",
            f"{profile.name if profile else voice_id}.voicepkg",
            "SectVoice声音包 (*.voicepkg)",
        )
        if not filename:
            return
        output = self.services.voice_packages.export(voice_id, Path(filename))
        self.statusBar().showMessage(f"声音包已导出：{output}", 4000)

    def _selected_voice_id(self) -> UUID | None:
        item = self.voice_list.currentItem()
        if item is not None:
            return UUID(item.data(Qt.ItemDataRole.UserRole))
        value = self.voice_combo.currentData()
        return UUID(value) if value else None

    def _rename_voice(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        profile = self.services.voices.get(voice_id)
        name, ok = QInputDialog.getText(self, "声音改名", "新名称", text=profile.name if profile else "")
        if ok:
            self.services.voices.rename(voice_id, name)
            self._refresh_voices(voice_id)

    def _delete_voice(self) -> None:
        voice_id = self._selected_voice_id()
        if voice_id is None:
            return
        profile = self.services.voices.get(voice_id)
        answer = QMessageBox.question(
            self,
            "删除声音",
            f"删除声音“{profile.name if profile else voice_id}”？文件会移到SectVoice回收目录。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        voice_root = (self.services.paths.data / "voices").resolve()
        source = (voice_root / str(voice_id)).resolve()
        if source.parent != voice_root:
            raise RuntimeError("声音路径越界，已拒绝删除")
        destination: Path | None = None
        if source.exists():
            destination = (
                self.services.paths.data
                / "trash"
                / "voices"
                / f"{voice_id}-{uuid4().hex}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        try:
            self.services.voices.delete(voice_id)
        except Exception:
            if destination is not None and destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
            raise
        self._refresh_voices()

    def _mark_selected_role(self) -> None:
        self._after_current_text_saved(self._mark_selected_role_on_saved_document)

    def _mark_selected_role_on_saved_document(self) -> None:
        if self.current_document is None:
            return
        cursor = self.editor.textCursor()
        role, ok = QInputDialog.getText(self, "角色名称", "把选中SpeechUnit标记为角色")
        if not ok:
            return
        unit_ids = self.services.roles.set_units_role(
            self.current_document.document_id,
            self.current_document.mapping,
            cursor.selectionStart(),
            cursor.selectionEnd(),
            role,
        )
        self.statusBar().showMessage(f"已标记 {len(unit_ids)} 个朗读单元", 2500)
        self._refresh_roles()

    def _analyze_roles(self) -> None:
        self._after_current_text_saved(self._analyze_roles_on_saved_document)

    def _analyze_roles_on_saved_document(self) -> None:
        if self.current_document is None:
            return
        suggestions = suggest_dialogue_roles(
            self.current_document.source_text, self.current_document.mapping
        )
        if not suggestions:
            QMessageBox.information(self, "对话分析", "没有找到足够明确的“某某说/问”结构。")
            return
        answer = QMessageBox.question(
            self,
            "确认对话分析",
            f"找到 {len(suggestions)} 个可明确归属的对白朗读单元。只会写入这些明确结果，是否应用？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        by_role: dict[str, list[UUID]] = {}
        for unit_id, role in suggestions.items():
            by_role.setdefault(role, []).append(unit_id)
        with self.services.roles.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO speech_unit_roles(document_id, speech_unit_id, role_name)
                VALUES (?, ?, ?)
                ON CONFLICT(document_id, speech_unit_id) DO UPDATE SET role_name=excluded.role_name
                """,
                [
                    (str(self.current_document.document_id), str(unit_id), role)
                    for unit_id, role in suggestions.items()
                ],
            )
        self._refresh_roles()

    def _refresh_roles(self) -> None:
        self.role_list.clear()
        if self.current_document is None:
            return
        roles = sorted(set(self.services.roles.roles_for_units(self.current_document.document_id).values()))
        assignments = self.services.roles.voice_assignments(self.current_document.document_id)
        for role in roles:
            assignment = assignments.get(role)
            profile = self.services.voices.get(assignment.voice_id) if assignment else None
            text = f"{role}  →  {profile.name if profile else '未分配声音'}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, role)
            self.role_list.addItem(item)

    def _assign_role_voice(self) -> None:
        if self.current_document is None or self.role_list.currentItem() is None:
            return
        voice_value = self.voice_combo.currentData()
        if not voice_value:
            return
        role = self.role_list.currentItem().data(Qt.ItemDataRole.UserRole)
        tier = Tier(self.tier_combo.currentData())
        self.services.roles.assign_voice(
            self.current_document.document_id, role, UUID(voice_value), tier
        )
        self._refresh_roles()

    def _clear_cache(self) -> None:
        answer = QMessageBox.question(self, "清理缓存", "确定清理全部已生成语音缓存？声音档案不会删除。")
        if answer == QMessageBox.StandardButton.Yes:
            removed = self.services.cache.clear_all()
            self.statusBar().showMessage(f"已清理 {removed / 1024 / 1024:.1f} MiB", 2500)
            self._update_cache_label()

    def _clear_document_cache(self) -> None:
        if self.current_document is None:
            return
        self.services.playback.stop()
        removed = self.services.cache.clear_document(self.current_document.document_id)
        self.statusBar().showMessage(
            f"已清理当前文档缓存 {removed / 1024 / 1024:.1f} MiB", 2500
        )
        self._update_cache_label()

    def _cache_limit_changed(self, value: int) -> None:
        self.services.settings.set("max_cache_gb", value)
        self.services.cache.prune_to_limit(value * 1024**3)
        self._update_cache_label()

    def _release_engines(self) -> None:
        self.services.playback.stop()
        self.services.asr.shutdown()
        self.services.engines.shutdown()
        self.statusBar().showMessage(
            "语音引擎和词语核对资源已释放；下次播放时会重新加载", 3500
        )

    def _manage_packages(self) -> None:
        self.services.playback.stop()
        dialog = PackageManagerDialog(
            paths=self.services.paths,
            packages=self.services.packages,
            installer=self.services.package_installer,
            engines=self.services.engines,
            parent=self,
        )
        dialog.exec()
        self._refresh_voices()

    def _check_reader_update(self) -> None:
        source = os.environ.get(
            "SECTVOICE_READER_UPDATE", DEFAULT_READER_UPDATE_URL
        )
        self.reader_update_button.setEnabled(False)
        self.statusBar().showMessage("正在检查Reader更新……")
        task = BackgroundTask(lambda: load_reader_update(source))
        task.signals.succeeded.connect(self._reader_update_checked)
        task.signals.failed.connect(self._reader_update_check_failed)
        task.signals.finished.connect(
            lambda: self.reader_update_button.setEnabled(True)
        )
        self._start_retained_task(task)

    def _reader_update_checked(self, value: object) -> None:
        assert isinstance(value, ReaderUpdate)
        if not is_version_newer(value.version, __version__):
            QMessageBox.information(
                self,
                "Reader已是最新",
                f"当前Reader {__version__}，公开版本 {value.version}。",
            )
            self.statusBar().showMessage("Reader已是最新版本", 3000)
            return
        signature_note = (
            "安装器已有代码签名。"
            if value.signed
            else "当前仍是未代码签名测试版，但会严格核对SHA-256。"
        )
        answer = QMessageBox.question(
            self,
            "发现Reader更新",
            f"发现Reader {value.version}（当前 {__version__}），"
            f"下载约{value.installer_size / 1024**2:.1f}MiB。\n"
            f"{signature_note}\n\n"
            "是否下载、校验并启动升级安装？Reader会先保存当前文档再退出。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._download_reader_update(value)

    def _reader_update_check_failed(self, detail: str) -> None:
        QMessageBox.critical(self, "Reader更新检查失败", detail[-5000:])
        self.statusBar().showMessage("Reader更新检查失败", 3000)

    def _download_reader_update(self, update: ReaderUpdate) -> None:
        self.reader_update_button.setEnabled(False)
        holder: dict[str, BackgroundTask] = {}

        def download() -> Path:
            task = holder["task"]
            return download_reader_update(
                update,
                self.services.paths.downloads / "reader-updates" / update.version,
                progress=lambda current, total: task.signals.progress.emit(
                    (current, total)
                ),
            )

        task = BackgroundTask(download)
        holder["task"] = task
        task.signals.progress.connect(
            lambda value: self.statusBar().showMessage(
                "正在下载Reader升级程序："
                f"{int(value[0]) / 1024**2:.1f}/{int(value[1]) / 1024**2:.1f}MiB"
            )
        )
        task.signals.succeeded.connect(
            lambda path: self._after_current_text_saved(
                lambda: self._launch_reader_installer(Path(path))
            )
        )
        task.signals.failed.connect(
            lambda detail: QMessageBox.critical(
                self, "Reader升级下载失败", detail[-5000:]
            )
        )
        task.signals.finished.connect(
            lambda: self.reader_update_button.setEnabled(True)
        )
        self._start_retained_task(task)

    def _launch_reader_installer(self, installer: Path) -> None:
        self.services.playback.stop()
        self.services.asr.shutdown()
        self.services.engines.shutdown()
        started, _process_id = QProcess.startDetached(
            str(installer), [], str(installer.parent)
        )
        if not started:
            QMessageBox.critical(
                self,
                "Reader升级启动失败",
                f"升级程序已经校验，但无法启动：{installer}",
            )
            return
        self.close()

    @Slot(str, str)
    def _playback_state(self, state: str, message: str) -> None:
        self.statusBar().showMessage(f"{state} · {message}")
        self.pause_button.setText("继续" if state == "Paused" else "暂停")

    @Slot(str, int, int)
    def _highlight_unit(self, _unit_id: str, start: int, end: int) -> None:
        selection = QTextEdit.ExtraSelection()
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        selection.cursor = cursor
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self.theme_manager.current.reading_highlight))
        selection.format = fmt
        self.editor.setExtraSelections([selection])
        visible = self.editor.textCursor()
        visible.setPosition(start)
        self.editor.setTextCursor(visible)
        self.editor.ensureCursorVisible()
        self._last_highlight = (start, end)

    def _change_font(self, delta: int) -> None:
        self._set_font_size(max(10, min(40, self.editor.font().pointSize() + delta)))

    def _set_font_size(self, size: int) -> None:
        font = self.editor.font()
        font.setPointSize(size)
        self.editor.setFont(font)

    def _set_line_spacing(self, value: float) -> None:
        cursor = self.editor.textCursor()
        position = cursor.position()
        self.editor.blockSignals(True)
        cursor.select(QTextCursor.SelectionType.Document)
        block_format = QTextBlockFormat()
        block_format.setLineHeight(
            int(value * 100),
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
        )
        cursor.mergeBlockFormat(block_format)
        cursor.clearSelection()
        cursor.setPosition(min(position, len(self.editor.toPlainText())))
        self.editor.setTextCursor(cursor)
        self.editor.blockSignals(False)

    def _update_cache_label(self) -> None:
        self.cache_label.setText(
            f"缓存占用：{self.services.cache.total_bytes() / 1024 / 1024:.1f} MiB"
        )

    def _guard(self, function):
        def guarded(*_args):
            try:
                function()
            except Exception as exc:
                QMessageBox.critical(self, "操作失败", str(exc))

        return guarded

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_current_text()
        if self._active_save is not None or self._save_queue:
            self._closing_after_save = True
            self.services.playback.stop()
            self.statusBar().showMessage("正在完成文档保存，随后自动关闭……")
            event.ignore()
            return
        self.services.playback.stop()
        self.services.asr.shutdown()
        self.services.engines.shutdown()
        store = self.services.settings
        store.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))
        store.set("splitter_sizes", self.splitter.sizes())
        store.set("font_size", self.editor.font().pointSize())
        store.set("line_spacing", self.line_spacing.value())
        store.set("speed", self.speed_spin.value())
        store.set("volume", self.volume_spin.value())
        store.set("punctuation_pause_ms", self.punctuation_pause.value())
        store.set("paragraph_pause_ms", self.paragraph_pause.value())
        store.set("tier", self.tier_combo.currentData())
        store.set("last_document_id", str(self.current_document.document_id) if self.current_document else None)
        store.set("selected_voice_id", self.voice_combo.currentData())
        event.accept()
