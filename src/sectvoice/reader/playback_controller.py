from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import QObject, Signal, Slot

from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.runtime import ResolvedSpeechUnit, RuntimeCallbacks, VoiceRuntime
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import EnginePayloadRef, PayloadStatus, SynthesisSettings, Tier
from sectvoice.reader.audio_output import AudioOutput
from sectvoice.reader.documents import Document, DocumentRepository
from sectvoice.reader.roles import RoleRepository
from sectvoice.reader.segmentation import SpeechUnit, seek_start_within_unit


class PlaybackController(QObject):
    stateChanged = Signal(str, str)
    currentUnitChanged = Signal(str, int, int)
    currentPositionChanged = Signal(int)
    error = Signal(str)
    finished = Signal()
    _runtimeStatus = Signal(str, str)
    _runtimeError = Signal(str)
    _runtimeCompleted = Signal()

    def __init__(
        self,
        *,
        runtime: VoiceRuntime,
        audio: AudioOutput,
        engines: EngineManager,
        voices: VoiceLibrary,
        documents: DocumentRepository,
        roles: RoleRepository,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.audio = audio
        self.engines = engines
        self.voices = voices
        self.documents = documents
        self.roles = roles
        self.document: Document | None = None
        self.voice_id: UUID | None = None
        self.tier = Tier.BASIC
        self.settings = SynthesisSettings()
        self.current_unit_id: UUID | None = None
        self.current_position = 0
        self.state = "Idle"
        self._unit_by_id: dict[UUID, SpeechUnit] = {}
        self._session_positions: dict[UUID, int] = {}
        self._session_units: dict[UUID, SpeechUnit] = {}
        self._pending_runtime_error: str | None = None

        self.audio.unitStarted.connect(self._on_unit_started)
        self.audio.underrun.connect(
            lambda: self._set_state("Generating", "正在生成下一句")
        )
        self.audio.drained.connect(self._on_drained)
        self.audio.error.connect(self._on_error)
        self._runtimeStatus.connect(self._set_state)
        self._runtimeError.connect(self._on_runtime_error)
        self._runtimeCompleted.connect(self.audio.mark_producer_done)

    def set_document(self, document: Document) -> None:
        self.stop()
        self.document = document
        self.current_position = document.current_position
        self._unit_by_id = {unit.speech_unit_id: unit for unit in document.mapping.units}

    def set_voice(self, voice_id: UUID, tier: Tier) -> None:
        self.voice_id = voice_id
        self.tier = tier

    def set_settings(self, settings: SynthesisSettings) -> None:
        self.settings = settings
        self.audio.set_volume(settings.volume)

    def play(self) -> None:
        self.play_from(self.current_position)

    def play_from(self, character_position: int) -> None:
        document = self._require_document()
        if self.voice_id is None:
            raise RuntimeError("请先选择声音")
        if not document.source_text:
            raise RuntimeError("当前文档没有可朗读文字")
        position = max(0, min(character_position, len(document.source_text) - 1))

        # Audible stop happens before session invalidation and new generation.
        self.audio.stop()
        self.runtime.stop()
        self._pending_runtime_error = None
        self._set_state("Seeking", "正在跳转到新的朗读位置")
        located, offset = document.mapping.locate(position)
        start_in_unit = seek_start_within_unit(located, offset)
        actual_position = located.start_char + start_in_unit
        first = replace(
            located,
            start_char=actual_position,
            text=located.text[start_in_unit:],
        )
        units = (first,) + tuple(
            item for item in document.mapping.units if item.ordinal > located.ordinal
        )
        resolved = self._resolve_units(units)
        self._session_positions = {item.unit.speech_unit_id: item.unit.start_char for item in resolved}
        self._session_units = {item.unit.speech_unit_id: item.unit for item in resolved}
        self.current_position = actual_position
        self.documents.set_position(document.document_id, actual_position)
        self.audio.begin_session()
        self._set_state("Preparing", "正在准备朗读")
        try:
            self.runtime.start(
                resolved,
                self.settings,
                RuntimeCallbacks(
                    status=lambda state, message: self._runtimeStatus.emit(state, message),
                    error=lambda message: self._runtimeError.emit(message),
                    completed=lambda: self._runtimeCompleted.emit(),
                ),
                document_id=document.document_id,
            )
        except Exception:
            self.audio.stop()
            raise

    def pause(self) -> None:
        if self.state not in {"Playing", "Generating"}:
            return
        self.audio.pause()
        self._set_state("Paused", "已暂停")

    def resume(self) -> None:
        if self.state != "Paused":
            return
        self.audio.resume()
        # 用户主动继续必须越过“暂停时忽略后台状态”的保护条件。
        self.state = "Playing"
        self.stateChanged.emit("Playing", "继续朗读")

    def stop(self) -> None:
        self.audio.stop()
        self.runtime.stop()
        self._pending_runtime_error = None
        if self.state != "Idle":
            self._set_state("Stopping", "正在停止")
        self._set_state("Idle", "已停止")

    def previous(self) -> None:
        unit = self._current_unit()
        document = self._require_document()
        ordinal = max(0, unit.ordinal - 1 if unit else 0)
        self.play_from(document.mapping.units[ordinal].start_char)

    def next(self) -> None:
        unit = self._current_unit()
        document = self._require_document()
        ordinal = min(len(document.mapping.units) - 1, (unit.ordinal + 1) if unit else 0)
        self.play_from(document.mapping.units[ordinal].start_char)

    def _resolve_units(
        self, units: tuple[SpeechUnit, ...]
    ) -> tuple[ResolvedSpeechUnit, ...]:
        assert self.voice_id is not None
        document = self._require_document()
        role_names = self.roles.roles_for_units(document.document_id)
        role_voices = self.roles.voice_assignments(document.document_id)
        handle = self.engines.for_tier(self.tier)
        # A novel can contain tens of thousands of SpeechUnits but normally
        # only a handful of voices.  Resolve each VoiceId once: querying SQLite
        # for profile/payload inside this loop made a 55k-unit document perform
        # more than 100k synchronous queries on the UI thread.
        voice_data: dict[UUID, tuple[EnginePayloadRef, str, str]] = {}

        def resolve_voice(voice_id: UUID):
            cached = voice_data.get(voice_id)
            if cached is not None:
                return cached
            profile = self.voices.get(voice_id)
            payload = next(
                (
                    item
                    for item in self.voices.payloads_for(voice_id)
                    if item.tier is self.tier
                    and item.engine_id == handle.manifest.engine_id
                    and item.status is PayloadStatus.READY
                ),
                None,
            )
            if payload is None:
                name = profile.name if profile is not None else str(voice_id)
                raise RuntimeError(f"声音“{name}”尚未编译{self.tier.value}语音数据")
            cached = (
                payload,
                profile.transcript if profile is not None else "",
                profile.language if profile is not None else "zh-CN",
            )
            voice_data[voice_id] = cached
            return cached

        result: list[ResolvedSpeechUnit] = []
        for unit in units:
            if not any(character.isalnum() for character in unit.text):
                continue
            voice_id = self.voice_id
            role_name = role_names.get(unit.speech_unit_id)
            assignment = role_voices.get(role_name or "")
            if assignment is not None:
                if assignment.tier is not self.tier:
                    raise RuntimeError(
                        f"角色“{assignment.role_name}”使用了不同语音档次；请先统一为{self.tier.value}"
                    )
                voice_id = assignment.voice_id
            payload, transcript, reference_language = resolve_voice(voice_id)
            result.append(
                ResolvedSpeechUnit(
                    unit,
                    voice_id,
                    self.tier,
                    payload,
                    transcript,
                    reference_language,
                )
            )
        if not result:
            raise RuntimeError("所选位置之后没有可朗读文字")
        return tuple(result)

    @Slot(str)
    def _on_unit_started(self, unit_id_text: str) -> None:
        unit_id = UUID(unit_id_text)
        unit = self._session_units.get(unit_id) or self._unit_by_id.get(unit_id)
        if unit is None:
            return
        self.current_unit_id = unit_id
        self.current_position = self._session_positions.get(unit_id, unit.start_char)
        if self.document is not None:
            self.documents.set_position(self.document.document_id, self.current_position)
        self.currentUnitChanged.emit(str(unit_id), unit.start_char, unit.end_char)
        self.currentPositionChanged.emit(self.current_position)
        if self.state != "Paused":
            self._set_state("Playing", "正在朗读")

    @Slot()
    def _on_drained(self) -> None:
        if self._pending_runtime_error is not None:
            message = self._pending_runtime_error
            self._pending_runtime_error = None
            self._set_state("Error", message)
            self.error.emit(message)
            return
        self._set_state("Idle", "朗读完成")
        self.finished.emit()

    @Slot(str)
    def _on_runtime_error(self, message: str) -> None:
        # A prefetch failure must not cut off already completed SpeechUnits.
        # Mark production complete so AudioOutput drains the valid queue, then
        # surface the failure exactly when playback reaches the failed unit.
        self._pending_runtime_error = message
        self.audio.mark_producer_done()
        self._set_state("Playing", "后续语音生成失败，正在播放已准备内容")

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.audio.stop()
        self.runtime.stop()
        self._pending_runtime_error = None
        self._set_state("Error", message)
        self.error.emit(message)

    @Slot(str, str)
    def _set_state(self, state: str, message: str) -> None:
        if self.state == "Paused" and state in {"Generating", "Playing"}:
            return
        self.state = state
        self.stateChanged.emit(state, message)

    def _current_unit(self) -> SpeechUnit | None:
        return self._unit_by_id.get(self.current_unit_id) if self.current_unit_id else None

    def _require_document(self) -> Document:
        if self.document is None:
            raise RuntimeError("请先打开文档")
        return self.document
