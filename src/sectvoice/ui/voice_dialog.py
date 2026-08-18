from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
from typing import Any
import wave
from uuid import UUID, uuid4

import numpy as np
from PySide6.QtCore import QThreadPool, QTimer, QUrl, Signal, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sectvoice.core.asr import ASRResult, ASRService
from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.reference_analysis import (
    ReferenceQualityReport,
    analyze_reference_samples,
    recommend_reference_window,
)
from sectvoice.core.voice_compiler import VoiceCompiler
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_quality import (
    basic_cross_language_warning,
    infer_reference_language,
    to_simplified_chinese,
)
from sectvoice.domain import EnginePayloadRef, Tier, VoiceProfile
from sectvoice.paths import AppPaths
from sectvoice.ui.tasks import BackgroundTask
from sectvoice.ui.waveform import WaveformWidget


@dataclass(frozen=True, slots=True)
class VoiceCreationResult:
    profile: VoiceProfile
    payloads: tuple[EnginePayloadRef, ...]
    preview_path: Path
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VoiceBuildRequest:
    audio_path: Path
    name: str
    start_seconds: float
    end_seconds: float
    transcript: str
    language: str
    light_denoise: bool
    raw_asr_transcript: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceTranscriptionResult:
    asr: ASRResult
    quality: ReferenceQualityReport


class VoiceCreationDialog(QDialog):
    voiceCreated = Signal(str)

    def __init__(
        self,
        *,
        paths: AppPaths,
        media: FFmpegProcessor,
        asr: ASRService,
        compiler: VoiceCompiler,
        engines: EngineManager,
        packages: ModelPackageManager,
        voices: VoiceLibrary,
        existing_profile: VoiceProfile | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.media = media
        self.asr = asr
        self.compiler = compiler
        self.engines = engines
        self.packages = packages
        self.voices = voices
        self.existing_profile = existing_profile
        self._preset_selection: tuple[float, float] | None = None
        self.audio_path: Path | None = None
        self.duration = 0.0
        self.result: VoiceCreationResult | None = None
        self._pending_new_voice_id: UUID | None = None
        self._confirmed = False
        self._busy = False
        self._raw_asr_transcript: str | None = None
        self._quality_report: ReferenceQualityReport | None = None
        self._transcript_selection: tuple[float, float] | None = None
        # QThreadPool owns the native QRunnable while it runs, but keeping the
        # Python wrappers alive here is also required.  Without that reference,
        # a queued ``finished`` signal can be lost after a very fast task and
        # leave the dialog permanently disabled even though ``succeeded`` ran.
        self._active_tasks: set[BackgroundTask] = set()
        self._media_player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._media_player.setAudioOutput(self._audio_output)
        self._preview_stop = QTimer(self)
        self._preview_stop.setSingleShot(True)
        self._preview_stop.timeout.connect(self._media_player.stop)

        self.setWindowTitle("更换声音参考片段" if existing_profile else "新建声音档案")
        self.resize(880, 720)
        layout = QVBoxLayout(self)
        select_row = QHBoxLayout()
        self.select_button = QPushButton("选择 WAV / MP3 / FLAC / M4A")
        self.path_label = QLabel("尚未选择音频")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        select_row.addWidget(self.select_button)
        select_row.addWidget(self.path_label, 1)
        layout.addLayout(select_row)

        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform)
        form = QFormLayout()
        range_row = QHBoxLayout()
        self.start_spin = QDoubleSpinBox()
        self.end_spin = QDoubleSpinBox()
        for spin in (self.start_spin, self.end_spin):
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            range_row.addWidget(spin)
        form.addRow("片段起止（秒）", range_row)
        self.light_denoise = QCheckBox("轻度降噪（不会启用强降噪）")
        form.addRow("处理", self.light_denoise)
        self.quality_confirm = QCheckBox(
            "我已试听并确认：只有目标人物，无重叠说话、明显背景音乐或严重噪声"
        )
        form.addRow("质量确认", self.quality_confirm)
        self.name_edit = QLineEdit()
        form.addRow("声音名称", self.name_edit)
        self.transcript = QTextEdit()
        self.transcript.setPlaceholderText("自动转写后请对照音频修正；准确参考文字会影响声音质量。")
        self.transcript.setMaximumHeight(130)
        form.addRow("参考转写", self.transcript)
        self.quality_report = QLabel("尚未分析所选片段的技术质量。")
        self.quality_report.setWordWrap(True)
        self.quality_report.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("自动质量报告", self.quality_report)
        package_row = QHBoxLayout()
        self.basic_check = QCheckBox("编译基础语音数据")
        self.standard_check = QCheckBox("编译中级语音数据")
        self.basic_check.setChecked(self.packages.active_for(Tier.BASIC) is not None)
        self.standard_check.setChecked(self.packages.active_for(Tier.STANDARD) is not None)
        self.basic_check.setEnabled(self.packages.active_for(Tier.BASIC) is not None)
        self.standard_check.setEnabled(self.packages.active_for(Tier.STANDARD) is not None)
        package_row.addWidget(self.basic_check)
        package_row.addWidget(self.standard_check)
        form.addRow("声音数据", package_row)
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.play_selection = QPushButton("试听所选原音")
        self.asr_button = QPushButton("处理片段并自动转写")
        self.create_button = QPushButton("创建声音并生成测试语音")
        self.play_result = QPushButton("试听克隆测试语音")
        self.play_result.setEnabled(False)
        for button in (self.play_selection, self.asr_button, self.create_button, self.play_result):
            action_row.addWidget(button)
        layout.addLayout(action_row)
        self.status_label = QLabel("建议选择3～10秒清晰、单人、无重叠说话和背景音乐的人声。")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("确认保存声音档案")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        layout.addWidget(self.buttons)

        self.select_button.clicked.connect(self._choose_audio)
        self.waveform.selectionChanged.connect(self._waveform_selection)
        self.start_spin.valueChanged.connect(self._spin_selection)
        self.end_spin.valueChanged.connect(self._spin_selection)
        self.play_selection.clicked.connect(self._play_selected_source)
        self.asr_button.clicked.connect(self._transcribe)
        self.create_button.clicked.connect(self._create)
        self.play_result.clicked.connect(self._play_preview)
        self.buttons.accepted.connect(self._accept_if_ready)
        self.buttons.rejected.connect(self.reject)
        if existing_profile is not None:
            self.name_edit.setText(existing_profile.name)
            self.name_edit.setEnabled(False)
            self.transcript.setPlainText(existing_profile.transcript)
            self.audio_path = existing_profile.source_audio_path
            self.path_label.setText(str(existing_profile.source_audio_path))
            self._preset_selection = (
                existing_profile.reference_start_seconds,
                existing_profile.reference_end_seconds,
            )
            self._transcript_selection = self._preset_selection
            QTimer.singleShot(0, self._load_existing_audio)

    def _load_existing_audio(self) -> None:
        self._start_task(
            self._analyze_audio,
            self._audio_analyzed,
            "正在读取现有原音频和波形……",
        )

    def _choose_audio(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择参考音频",
            "",
            "音频 (*.wav *.mp3 *.flac *.m4a)",
        )
        if not filename:
            return
        self.audio_path = Path(filename)
        self.path_label.setText(filename)
        self.name_edit.setText(self.audio_path.stem)
        self._raw_asr_transcript = None
        self._quality_report = None
        self._transcript_selection = None
        self.transcript.clear()
        self.quality_report.setText("尚未分析所选片段的技术质量。")
        self._start_task(
            self._analyze_audio,
            self._audio_analyzed,
            "正在读取音频并生成波形……",
        )

    def _analyze_audio(self):
        assert self.audio_path is not None
        metadata = self.media.probe(self.audio_path)
        waveform = self.media.waveform(self.audio_path, points_per_second=50)
        suggestion = recommend_reference_window(waveform, metadata.duration_seconds)
        return metadata.duration_seconds, waveform, suggestion

    def _audio_analyzed(self, value) -> None:
        duration, waveform, suggestion = value
        self.duration = duration
        for spin in (self.start_spin, self.end_spin):
            spin.setMaximum(duration)
        self.waveform.set_waveform(waveform, duration)
        selected = self._preset_selection or (
            suggestion.start_seconds,
            suggestion.end_seconds,
        )
        self._preset_selection = None
        self.start_spin.setValue(selected[0])
        self.end_spin.setValue(selected[1])
        self.waveform.set_selection(*selected)
        self.status_label.setText(
            suggestion.message + " 下一步：试听后点击“处理片段并自动转写”。"
        )

    def _waveform_selection(self, start: float, end: float) -> None:
        self._invalidate_transcription_after_selection_change(start, end)
        self.start_spin.blockSignals(True)
        self.end_spin.blockSignals(True)
        self.start_spin.setValue(start)
        self.end_spin.setValue(end)
        self.start_spin.blockSignals(False)
        self.end_spin.blockSignals(False)

    def _spin_selection(self) -> None:
        self._invalidate_transcription_after_selection_change(
            self.start_spin.value(), self.end_spin.value()
        )
        self.waveform.set_selection(self.start_spin.value(), self.end_spin.value())

    def _invalidate_transcription_after_selection_change(
        self, start: float, end: float
    ) -> None:
        if self._transcript_selection is not None and all(
            abs(current - previous) <= 0.02
            for current, previous in zip(
                (start, end), self._transcript_selection, strict=True
            )
        ):
            return
        if (
            self._raw_asr_transcript is None
            and self._quality_report is None
            and self._transcript_selection is None
        ):
            return
        self._raw_asr_transcript = None
        self._quality_report = None
        self._transcript_selection = None
        self.transcript.clear()
        self.quality_report.setText("片段范围已变化，请重新处理并自动转写。")

    def _play_selected_source(self) -> None:
        if self.audio_path is None:
            return
        self._media_player.setSource(QUrl.fromLocalFile(str(self.audio_path)))
        self._media_player.setPosition(int(self.start_spin.value() * 1000))
        self._media_player.play()
        self._preview_stop.start(
            max(1, int((self.end_spin.value() - self.start_spin.value()) * 1000))
        )

    def _transcribe(self) -> None:
        if self.audio_path is None:
            QMessageBox.warning(self, "缺少音频", "请先选择参考音频。")
            return
        if self.end_spin.value() - self.start_spin.value() < 3:
            QMessageBox.warning(self, "片段太短", "参考片段至少需要3秒。")
            return
        audio_path = self.audio_path
        start = self.start_spin.value()
        end = self.end_spin.value()
        denoise = self.light_denoise.isChecked()
        self._start_task(
            lambda: self._prepare_and_transcribe(audio_path, start, end, denoise),
            self._asr_completed,
            "正在规范化片段并进行本地自动转写……",
        )

    def _prepare_and_transcribe(
        self, audio_path: Path, start: float, end: float, denoise: bool
    ) -> ReferenceTranscriptionResult:
        raw_samples = self.media.decode_mono_f32(
            audio_path,
            sample_rate=16_000,
            start_seconds=start,
            end_seconds=end,
        )
        target = self.paths.temp / "asr-selections" / f"{uuid4().hex}.wav"
        self.media.prepare_reference(
            audio_path,
            target,
            start,
            end,
            sample_rate=16000,
            channels=1,
            light_denoise=denoise,
        )
        try:
            asr_result = self.asr.transcribe(target)
            quality = analyze_reference_samples(
                raw_samples,
                16_000,
                transcript=to_simplified_chinese(asr_result.text),
            )
            return ReferenceTranscriptionResult(asr_result, quality)
        finally:
            target.unlink(missing_ok=True)

    def _asr_completed(self, result: ReferenceTranscriptionResult) -> None:
        simplified = to_simplified_chinese(result.asr.text)
        self._raw_asr_transcript = result.asr.text
        self._quality_report = result.quality
        self._transcript_selection = (
            self.start_spin.value(),
            self.end_spin.value(),
        )
        self.transcript.setPlainText(simplified)
        self.quality_report.setText(result.quality.summary())
        self.status_label.setText(
            f"自动转写完成（语言置信度 {result.asr.language_probability:.0%}），已统一显示为简体。"
            "请对照原音人工修正；自动质量报告不会代替试听。"
        )
        if not result.quality.is_usable:
            self.create_button.setEnabled(False)

    def _create(self) -> None:
        if self.audio_path is None:
            QMessageBox.warning(self, "缺少音频", "请先选择参考音频。")
            return
        if not self.transcript.toPlainText().strip():
            QMessageBox.warning(self, "缺少转写", "参考转写不能为空。")
            return
        if not self.quality_confirm.isChecked():
            QMessageBox.warning(
                self,
                "需要人工确认",
                "多人重叠、明显背景音乐或严重噪声不能可靠创建目标声音。请先试听并勾选质量确认。",
            )
            return
        if self._quality_report is not None and not self._quality_report.is_usable:
            QMessageBox.warning(
                self,
                "参考片段不可用",
                self._quality_report.summary(),
            )
            return
        tiers = tuple(
            tier
            for tier, checked in (
                (Tier.BASIC, self.basic_check.isChecked()),
                (Tier.STANDARD, self.standard_check.isChecked()),
            )
            if checked
        )
        if not tiers:
            QMessageBox.warning(self, "未选语音包", "至少选择一个已安装语音包。")
            return
        if self.existing_profile is not None:
            answer = QMessageBox.question(
                self,
                "确认更换参考",
                "更换后旧的Basic/Standard声音数据会失效，并按勾选档次重新编译。是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        language = infer_reference_language(self.transcript.toPlainText())
        language_warning = (
            basic_cross_language_warning(language)
            if self.basic_check.isChecked()
            else None
        )
        if language_warning is not None:
            answer = QMessageBox.question(
                self,
                "基础语音包跨语种提示",
                language_warning + "\n\n仍要继续创建吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        request = VoiceBuildRequest(
            audio_path=self.audio_path,
            name=self.name_edit.text(),
            start_seconds=self.start_spin.value(),
            end_seconds=self.end_spin.value(),
            transcript=self.transcript.toPlainText(),
            language=language,
            light_denoise=self.light_denoise.isChecked(),
            raw_asr_transcript=self._raw_asr_transcript,
        )
        self._start_task(
            lambda: self._compile_and_preview(request, tiers),
            self._creation_completed,
            "正在创建永久VoiceId、编译声音数据并生成测试语音……",
        )

    def _compile_and_preview(
        self, request: VoiceBuildRequest, tiers: tuple[Tier, ...]
    ) -> VoiceCreationResult:
        if self.existing_profile is None:
            profile = self.compiler.create_profile(
                name=request.name,
                source_audio=request.audio_path,
                selection_start_seconds=request.start_seconds,
                selection_end_seconds=request.end_seconds,
                transcript=request.transcript,
                language=request.language,
                light_denoise=request.light_denoise,
                raw_asr_transcript=request.raw_asr_transcript,
            )
            self._pending_new_voice_id = profile.voice_id
        else:
            profile = self.compiler.replace_reference(
                self.existing_profile.voice_id,
                source_audio=request.audio_path,
                selection_start_seconds=request.start_seconds,
                selection_end_seconds=request.end_seconds,
                transcript=request.transcript,
                language=request.language,
                light_denoise=request.light_denoise,
                raw_asr_transcript=request.raw_asr_transcript,
            )
        payload_rows: list[EnginePayloadRef] = []
        warnings: list[str] = []
        for tier in tiers:
            try:
                payload_rows.append(self.engines.compile_voice(profile, tier, self.compiler))
            except Exception as exc:
                warnings.append(f"{tier.value}编译失败：{exc}")
        payloads = tuple(payload_rows)
        if not payloads:
            raise RuntimeError("所有所选语音包编译都失败；声音档案和错误状态已保留，可稍后重试。")
        preview_payload = payloads[-1]
        handle = self.engines.for_tier(preview_payload.tier)
        chunks = list(
            handle.client.stream_synthesis(
                session_id=uuid4(),
                generation_id=1,
                speech_unit_id=uuid4(),
                text="声音创建成功。以后可以直接用这个声音朗读新的文字。",
                payload_path=preview_payload.opaque_path,
                options={"speed": 1.0, "seed": 20260810},
            )
        )
        audible = [chunk for chunk in chunks if chunk.data]
        if not audible:
            raise RuntimeError("引擎没有生成可听见的测试音频")
        pcm = audible[0].pcm_format
        raw = b"".join(chunk.data for chunk in audible)
        if pcm.sample_format == "f32le":
            floats = np.frombuffer(raw, dtype="<f4")
            raw = np.round(np.clip(floats, -1, 1) * 32767).astype("<i2").tobytes()
        preview = self.paths.data / "voices" / str(profile.voice_id) / "previews" / "creation-test.wav"
        preview.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(preview), "wb") as output:
            output.setnchannels(pcm.channels)
            output.setsampwidth(2)
            output.setframerate(pcm.sample_rate)
            output.writeframes(raw)
        return VoiceCreationResult(profile, payloads, preview, tuple(warnings))

    def _creation_completed(self, result: VoiceCreationResult) -> None:
        self.result = result
        self.play_result.setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
        self.status_label.setText(
            "真实克隆测试语音已经生成。请试听；确认声音正确后再保存。"
            + (("\n" + "\n".join(result.warnings)) if result.warnings else "")
        )
        self.create_button.setEnabled(False)
        self._play_preview()

    def _play_preview(self) -> None:
        if self.result is None:
            return
        self._media_player.setSource(QUrl.fromLocalFile(str(self.result.preview_path)))
        self._media_player.play()

    def _accept_if_ready(self) -> None:
        if self.result is None:
            return
        final_name = self.name_edit.text().strip()
        if not final_name:
            QMessageBox.warning(self, "缺少名称", "声音名称不能为空。")
            return
        if final_name != self.result.profile.name:
            try:
                updated = self.voices.rename(
                    self.result.profile.voice_id,
                    final_name,
                )
            except Exception as exc:
                QMessageBox.critical(self, "保存名称失败", str(exc))
                return
            self.result = replace(self.result, profile=updated)
        self._confirmed = True
        self.voiceCreated.emit(str(self.result.profile.voice_id))
        self.accept()

    def reject(self) -> None:
        if self._busy:
            QMessageBox.information(self, "任务仍在运行", "请等待当前声音处理任务结束。")
            return
        if (
            not self._confirmed
            and self.existing_profile is None
            and self._pending_new_voice_id is not None
        ):
            voice_id = self._pending_new_voice_id
            source = self.paths.data / "voices" / str(voice_id)
            destination = (
                self.paths.data
                / "trash"
                / "unconfirmed-voices"
                / f"{voice_id}-{uuid4().hex}"
            )
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            try:
                self.voices.delete(voice_id)
            except Exception:
                if destination.exists() and not source.exists():
                    shutil.move(str(destination), str(source))
                raise
            self._pending_new_voice_id = None
        super().reject()

    def _task_failed(self, detail: str) -> None:
        self.status_label.setText("操作失败；详细错误已显示。")
        QMessageBox.critical(self, "声音创建失败", detail[-5000:])

    def _start_task(
        self,
        function: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_text: str,
    ) -> None:
        self._set_busy(True, busy_text)
        task = BackgroundTask(function)
        self._active_tasks.add(task)
        task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(self._task_failed)
        task.signals.finished.connect(lambda task=task: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _task_finished(self, task: BackgroundTask) -> None:
        self._active_tasks.discard(task)
        self._set_busy(False)

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self._busy = busy
        for widget in (
            self.select_button,
            self.asr_button,
            self.create_button,
            self.basic_check,
            self.standard_check,
        ):
            widget.setEnabled(not busy)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(not busy)
        if not busy:
            self.basic_check.setEnabled(self.packages.active_for(Tier.BASIC) is not None)
            self.standard_check.setEnabled(self.packages.active_for(Tier.STANDARD) is not None)
            if self.result is not None:
                self.create_button.setEnabled(False)
            elif self._quality_report is not None and not self._quality_report.is_usable:
                self.create_button.setEnabled(False)
        if text:
            self.status_label.setText(text)
