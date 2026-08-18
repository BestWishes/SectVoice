from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from sectvoice.core.audio_buffer import StreamingAudioBuffer
from sectvoice.domain import AudioChunk, PCMFormat


@dataclass(slots=True)
class _PendingChunk:
    chunk: AudioChunk
    offset: int = 0
    scheduled: bool = False


class AudioOutput(QObject):
    unitStarted = Signal(str)
    positionChanged = Signal(float)
    underrun = Signal()
    drained = Signal()
    error = Signal(str)

    def __init__(self, buffer: StreamingAudioBuffer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.buffer = buffer
        self._sink: QAudioSink | None = None
        self._device = None
        self._format: PCMFormat | None = None
        self._pending: _PendingChunk | None = None
        self._timeline: deque[tuple[int, UUID]] = deque()
        self._submitted_microseconds = 0
        self._last_unit: UUID | None = None
        self._producer_done = False
        self._ever_started = False
        self._underrun_reported = False
        self._paused = False
        self._volume = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(12)
        self._timer.timeout.connect(self._pump)

    @staticmethod
    def output_device_name() -> str:
        return QMediaDevices.defaultAudioOutput().description()

    def begin_session(self) -> None:
        self.stop()
        self._producer_done = False
        self._ever_started = False
        self._underrun_reported = False
        self._timer.start()

    def mark_producer_done(self) -> None:
        self._producer_done = True

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(2.0, float(value)))
        if self._sink is not None:
            self._sink.setVolume(min(1.0, self._volume))

    def pause(self) -> None:
        self._paused = True
        if self._sink is not None:
            self._sink.suspend()

    def resume(self) -> None:
        self._paused = False
        if self._sink is not None:
            self._sink.resume()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        # QAudioSink.stop() 会同步触发 stateChanged。先摘除对象并断开
        # 回调，避免错误处理在 stop() 内再次进入本方法并重复释放。
        sink = self._sink
        self._sink = None
        if sink is not None:
            try:
                sink.stateChanged.disconnect(self._on_state_changed)
            except (TypeError, RuntimeError):
                pass
            sink.stop()
            sink.deleteLater()
        self._device = None
        self._format = None
        self._pending = None
        self._timeline.clear()
        self._submitted_microseconds = 0
        self._last_unit = None
        self._paused = False

    def _pump(self) -> None:
        if self._paused:
            return
        try:
            self._update_timeline()
            if (
                not self._ever_started
                and not self._producer_done
                and not self.buffer.ready_to_start
            ):
                return
            if self._pending is None:
                chunk = self.buffer.pop()
                while chunk is not None and not chunk.data:
                    chunk = self.buffer.pop()
                if chunk is not None:
                    self._pending = _PendingChunk(chunk)
                    self._ensure_sink(chunk.pcm_format)

            if self._pending is not None and self._sink is not None and self._device is not None:
                available = max(0, self._sink.bytesFree())
                if available:
                    pending = self._pending
                    data = pending.chunk.data[pending.offset : pending.offset + available]
                    if data:
                        written = int(self._device.write(data))
                        if written > 0:
                            if not pending.scheduled:
                                self._schedule_unit(pending.chunk)
                                pending.scheduled = True
                            pending.offset += written
                            self._ever_started = True
                            self._underrun_reported = False
                    if pending.offset >= len(pending.chunk.data):
                        self._pending = None

            empty = self._pending is None and len(self.buffer) == 0
            if empty and not self._producer_done and self._ever_started:
                if self._sink is not None and self._sink.state() == QAudio.State.IdleState:
                    if not self._underrun_reported:
                        self._underrun_reported = True
                        self.underrun.emit()
            if empty and self._producer_done and self._audio_has_drained():
                self._timer.stop()
                self._update_timeline(force=True)
                self.drained.emit()
        except Exception as exc:
            self._timer.stop()
            self.error.emit(str(exc))

    def _ensure_sink(self, pcm: PCMFormat) -> None:
        if self._sink is not None and self._format == pcm:
            return
        if self._sink is not None and self._ever_started:
            raise RuntimeError("朗读会话中音频格式发生变化；请保持同一语音档次")
        qt_format = QAudioFormat()
        qt_format.setSampleRate(pcm.sample_rate)
        qt_format.setChannelCount(pcm.channels)
        qt_format.setSampleFormat(
            QAudioFormat.SampleFormat.Float
            if pcm.sample_format == "f32le"
            else QAudioFormat.SampleFormat.Int16
        )
        device_info = QMediaDevices.defaultAudioOutput()
        if device_info.isNull():
            raise RuntimeError("没有可用的音频输出设备")
        if not device_info.isFormatSupported(qt_format):
            raise RuntimeError(
                f"输出设备不支持 {pcm.sample_rate}Hz/{pcm.channels}ch/{pcm.sample_format}"
            )
        self._sink = QAudioSink(device_info, qt_format, self)
        self._sink.setBufferSize(max(4096, qt_format.bytesForDuration(800_000)))
        self._sink.setVolume(min(1.0, self._volume))
        self._sink.stateChanged.connect(self._on_state_changed)
        self._device = self._sink.start()
        self._format = pcm

    def _schedule_unit(self, chunk: AudioChunk) -> None:
        if chunk.speech_unit_id != self._last_unit:
            self._timeline.append((self._submitted_microseconds, chunk.speech_unit_id))
            self._last_unit = chunk.speech_unit_id
        self._submitted_microseconds += int(chunk.duration_seconds * 1_000_000)

    def _update_timeline(self, force: bool = False) -> None:
        if self._sink is None:
            return
        played = self._sink.processedUSecs()
        while self._timeline and (force or self._timeline[0][0] <= played + 25_000):
            _, unit_id = self._timeline.popleft()
            self.unitStarted.emit(str(unit_id))
        self.positionChanged.emit(played / 1_000_000.0)

    def _audio_has_drained(self) -> bool:
        if self._sink is None:
            return True
        if not self._ever_started:
            return True
        remaining_us = self._submitted_microseconds - self._sink.processedUSecs()
        return remaining_us <= 35_000 or self._sink.state() == QAudio.State.IdleState

    def _on_state_changed(self, _state: QAudio.State) -> None:
        # error 信号的槽可能立即调用 stop()，所以只访问一次局部引用。
        sink = self._sink
        if sink is None:
            return
        error = sink.error()
        # PySide6 6.8 在部分 Windows 音频后端中会返回名称为 NoError、
        # 但与 QAudio.Error.NoError 不相等的包装对象；按 Qt 枚举值判断。
        if int(error.value) != 0:
            self.error.emit(f"音频输出失败：{error.name}")
