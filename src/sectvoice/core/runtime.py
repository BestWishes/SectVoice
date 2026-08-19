from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from threading import Event, RLock, Thread
import time
from typing import Callable
from uuid import UUID, uuid4

from sectvoice.core.audio_buffer import StreamingAudioBuffer
from sectvoice.core.asr import ASRService
from sectvoice.core.cache import SynthesisCacheIdentity, VoiceCache
from sectvoice.core.engine_manager import EngineHandle, EngineManager
from sectvoice.core.generation_window import (
    GenerationWindow,
    ResolvedSpeechUnit,
    build_window_engine_text,
    estimate_unit_seconds,
    pause_milliseconds,
    plan_next_generation_window,
)
from sectvoice.core.media import FFmpegProcessor, WINDOW_EDGE_GUARD_MS
from sectvoice.core.session import SessionGate, SessionToken
from sectvoice.core.window_audio import (
    UnitFrameRange,
    WindowAudioLayout,
    balance_window_unit_levels_pcm,
    locate_window_unit_frames,
    locate_window_unit_frames_from_words,
    scale_window_layout,
)
from sectvoice.domain import (
    AudioChunk,
    PCMFormat,
    SynthesisSettings,
)
from sectvoice.reader.segmentation import SpeechUnit, segment_text


POSTPROCESS_VERSION = "sectvoice-pcm-v19-boundary-release-guards"
LOGGER = logging.getLogger(__name__)


class SessionSuperseded(RuntimeError):
    pass


class GeneratedAudioValidationError(RuntimeError):
    """The engine returned audio, but local alignment could not trust the window.

    This exception is an internal retry signal.  It must never escape the
    runtime as a user-visible reading failure because ASR is not the content
    authority and can itself make recognition mistakes.
    """

    def __init__(
        self,
        message: str,
        *,
        recognized_text: str = "",
    ) -> None:
        super().__init__(message)
        self.recognized_text = recognized_text


@dataclass(slots=True)
class RuntimeCallbacks:
    status: Callable[[str, str], None] = lambda _state, _message: None
    error: Callable[[str], None] = lambda _message: None
    completed: Callable[[], None] = lambda: None


class VoiceRuntime:
    """Produces a few playable seconds ahead and rejects all superseded chunks."""

    def __init__(
        self,
        *,
        engines: EngineManager,
        cache: VoiceCache,
        buffer: StreamingAudioBuffer,
        temp_root: Path,
        media: FFmpegProcessor,
        alignment_asr: ASRService | None = None,
    ) -> None:
        self.engines = engines
        self.cache = cache
        self.buffer = buffer
        self.temp_root = temp_root
        self.media = media
        self.alignment_asr = alignment_asr
        self.gate = SessionGate()
        self._lock = RLock()
        self._cancel_event: Event | None = None
        self._active_handle: EngineHandle | None = None
        self._thread: Thread | None = None
        self._pace_ema: dict[tuple[object, ...], float] = {}
        self._worker_recycle_count = 0

    @property
    def worker_recycle_count(self) -> int:
        with self._lock:
            return self._worker_recycle_count

    def start(
        self,
        units: tuple[ResolvedSpeechUnit, ...],
        settings: SynthesisSettings,
        callbacks: RuntimeCallbacks | None = None,
        document_id: UUID | None = None,
    ) -> SessionToken:
        if not units:
            raise ValueError("no speech units to play")
        self.stop()
        token = self.gate.begin()
        cancel_event = Event()
        with self._lock:
            self._cancel_event = cancel_event
            self._thread = Thread(
                target=self._produce,
                args=(
                    token,
                    units,
                    settings,
                    callbacks or RuntimeCallbacks(),
                    cancel_event,
                    document_id,
                ),
                name=f"sectvoice-session-{token.generation_id}",
                daemon=True,
            )
            self._thread.start()
        return token

    def stop(self) -> None:
        token = self.gate.current()
        with self._lock:
            cancel_event = self._cancel_event
            handle = self._active_handle
            self._cancel_event = None
            self._active_handle = None
        self.gate.invalidate()
        self.buffer.clear()
        if cancel_event is not None:
            cancel_event.set()
        if token is not None and handle is not None:
            Thread(
                target=handle.cancel,
                args=(token.session_id, token.generation_id),
                name="sectvoice-cancel",
                daemon=True,
            ).start()

    def _produce(
        self,
        token: SessionToken,
        units: tuple[ResolvedSpeechUnit, ...],
        settings: SynthesisSettings,
        callbacks: RuntimeCallbacks,
        cancel_event: Event,
        document_id: UUID | None,
    ) -> None:
        try:
            self._raise_if_superseded(token, cancel_event)
            callbacks.status("Preparing", "正在准备语音引擎")
            unit_index = 0
            window_index = 0
            while unit_index < len(units):
                self._raise_if_superseded(token, cancel_event)
                resolved = units[unit_index]
                handle = self.engines.for_tier(resolved.tier)
                with self._lock:
                    self._active_handle = handle
                capabilities = dict(handle.manifest.raw.get("capabilities") or {})
                maximum_request_chars = int(
                    capabilities.get("max_request_chars") or 0
                )
                window = plan_next_generation_window(
                    units,
                    start_index=unit_index,
                    settings=settings,
                    maximum_request_chars=maximum_request_chars,
                    chars_per_second=self._pace_for(resolved),
                    initial=(
                        self.buffer.buffered_seconds
                        < self.buffer.watermarks.target_seconds
                    ),
                )
                cached_count = self._emit_best_cached_unit_run(
                    token,
                    units,
                    start_index=unit_index,
                    minimum_units=len(window.units),
                    settings=settings,
                    handle=handle,
                    capabilities=capabilities,
                    cancel_event=cancel_event,
                    document_id=document_id,
                    callbacks=callbacks,
                )
                if cached_count:
                    unit_index += cached_count
                    window_index += 1
                    continue
                callbacks.status(
                    "Preparing",
                    (
                        f"正在准备连续片段 {window_index + 1}："
                        f"{len(window.units)} 个朗读单元，预计 {window.estimated_seconds:.1f} 秒"
                    ),
                )
                self._produce_window_resilient(
                    token,
                    window,
                    settings,
                    handle,
                    cancel_event,
                    document_id,
                    callbacks,
                )
                unit_index += len(window.units)
                window_index += 1
            self._raise_if_superseded(token, cancel_event)
            callbacks.status("Playing", "后续内容已生成")
            callbacks.completed()
        except SessionSuperseded:
            return
        except Exception as exc:
            if self.gate.current() == token:
                LOGGER.exception(
                    "voice_runtime_generation_failed session_id=%s generation_id=%s",
                    token.session_id,
                    token.generation_id,
                )
                callbacks.error(str(exc))

    def _produce_window_resilient(
        self,
        token: SessionToken,
        window: GenerationWindow,
        settings: SynthesisSettings,
        handle: EngineHandle,
        cancel_event: Event,
        document_id: UUID | None,
        callbacks: RuntimeCallbacks,
    ) -> None:
        """Generate a window without ever aborting merely because ASR disagrees.

        Normal windows retain the continuous two-candidate path.  If both
        candidates cannot be aligned, the window is recursively divided only
        at complete SpeechUnit boundaries.  A single SpeechUnit is generated
        literally and does not ask ASR for permission to be played.
        """

        last_error: GeneratedAudioValidationError | None = None
        for synthesis_attempt in range(2):
            try:
                self._produce_window(
                    token,
                    window,
                    settings,
                    handle,
                    cancel_event,
                    document_id,
                    callbacks,
                    synthesis_attempt=synthesis_attempt,
                )
                return
            except GeneratedAudioValidationError as exc:
                last_error = exc
                self._raise_if_superseded(token, cancel_event)
                action = (
                    "retry-alternate-seed"
                    if synthesis_attempt == 0
                    else "split-at-speech-unit-boundary"
                )
                self._log_alignment_fallback(
                    token,
                    window,
                    exc,
                    synthesis_attempt=synthesis_attempt,
                    action=action,
                )
                if synthesis_attempt == 0:
                    callbacks.status(
                        "Generating",
                        "片段核对不确定，正在换一条生成路径自动重试（朗读不会中断）",
                    )

        # A real single-unit window never enters ASR validation.  Keeping this
        # guard makes the invariant explicit and protects future validators.
        if len(window.units) <= 1:
            LOGGER.warning(
                "single SpeechUnit unexpectedly requested validation fallback; "
                "retrying literal synthesis without a validator: %s",
                last_error,
            )
            self._produce_window(
                token,
                window,
                settings,
                handle,
                cancel_event,
                document_id,
                callbacks,
                synthesis_attempt=2,
                skip_alignment_validation=True,
            )
            return

        callbacks.status(
            "Generating",
            "片段核对仍不确定，正在按完整朗读单元自动拆分并继续",
        )
        first, second = _split_generation_window(window, settings)
        self._produce_window_resilient(
            token,
            first,
            settings,
            handle,
            cancel_event,
            document_id,
            callbacks,
        )
        self._produce_window_resilient(
            token,
            second,
            settings,
            handle,
            cancel_event,
            document_id,
            callbacks,
        )

    @staticmethod
    def _log_alignment_fallback(
        token: SessionToken,
        window: GenerationWindow,
        error: GeneratedAudioValidationError,
        *,
        synthesis_attempt: int,
        action: str,
    ) -> None:
        LOGGER.warning(
            "generation_window_alignment_fallback %s",
            json.dumps(
                {
                    "session_id": str(token.session_id),
                    "generation_id": token.generation_id,
                    "speech_unit_ids": [
                        str(item.unit.speech_unit_id) for item in window.units
                    ],
                    "source_text": window.engine_text,
                    "recognized_text": error.recognized_text,
                    "attempt": synthesis_attempt + 1,
                    "reason": str(error),
                    "action": action,
                },
                ensure_ascii=False,
            ),
        )

    def _produce_window(
        self,
        token: SessionToken,
        window: GenerationWindow,
        settings: SynthesisSettings,
        handle: EngineHandle,
        cancel_event: Event,
        document_id: UUID | None,
        callbacks: RuntimeCallbacks,
        *,
        synthesis_attempt: int = 0,
        skip_alignment_validation: bool = False,
    ) -> None:
        resolved = window.units[0]
        manifest = handle.manifest
        capabilities = dict(manifest.raw.get("capabilities") or {})
        native_speed = bool(capabilities.get("native_speed"))
        identity = _window_cache_identity(
            window.cache_text,
            resolved,
            manifest,
            capabilities,
            settings,
        )
        entry = self.cache.get(identity.key)
        if entry is not None:
            try:
                pcm = PCMFormat(
                    sample_rate=int(entry.metadata["sample_rate"]),
                    channels=int(entry.metadata["channels"]),
                    sample_format=str(entry.metadata["sample_format"]),
                )
                layout = WindowAudioLayout.from_metadata(
                    dict(entry.metadata["window_layout"]),
                    expected_texts=tuple(item.unit.text for item in window.units),
                )
            except (KeyError, TypeError, ValueError) as exc:
                LOGGER.warning(
                    "discarding_invalid_window_cache cache_key=%s reason=%s",
                    entry.cache_key,
                    exc,
                )
                self.cache.remove(entry.cache_key)
            else:
                self.cache.index_window(identity, entry.metadata)
                if document_id is not None:
                    self.cache.note_document_use(document_id, identity.key)
                self._emit_window_with_optional_speed(
                    token,
                    window,
                    pcm,
                    entry.audio_path,
                    layout,
                    settings,
                    native_speed=native_speed,
                    cancel_event=cancel_event,
                )
                return

        self.temp_root.mkdir(parents=True, exist_ok=True)
        if not handle.is_loaded:
            self._raise_if_superseded(token, cancel_event)
            callbacks.status(
                "LoadingModel",
                f"正在加载{resolved.tier.value}语音包（本会话只加载一次）",
            )
            _ = handle.client
            self._raise_if_superseded(token, cancel_event)
        self._raise_if_superseded(token, cancel_event)
        callbacks.status(
            "Generating",
            f"正在一次生成连续片段（{len(window.units)} 个朗读单元）",
        )
        temporary = self.temp_root / f"{uuid4().hex}-raw.pcm"
        normalized_path = self.temp_root / f"{uuid4().hex}-normalized.pcm"
        alignment_wav = self.temp_root / f"{uuid4().hex}-alignment.wav"
        pcm_format: PCMFormat | None = None
        try:
            self._wait_for_buffer_room(token, cancel_event)
            with temporary.open("wb") as output:
                options = {
                    "seed": (
                        _synthesis_seed(resolved)
                        + synthesis_attempt * 32_452_843
                    )
                    & 0x7FFFFFFF
                }
                if _uses_stable_moss_first_seed(resolved):
                    options["stable_first_seed"] = True
                if native_speed:
                    options["speed"] = _native_synthesis_speed(settings, capabilities)
                compute_pacing = capabilities.get("gpu_compute_pacing")
                if isinstance(compute_pacing, dict):
                    buffered_seconds = self.buffer.buffered_seconds
                    options["compute_pacing"] = (
                        False
                        if 0.0 < buffered_seconds <= self.buffer.watermarks.low_seconds
                        else dict(compute_pacing)
                    )
                for chunk in handle.client.stream_synthesis(
                    session_id=token.session_id,
                    generation_id=token.generation_id,
                    speech_unit_id=resolved.unit.speech_unit_id,
                    text=window.engine_text,
                    payload_path=resolved.payload.opaque_path,
                    options=options,
                ):
                    self._raise_if_superseded(token, cancel_event)
                    if chunk.data:
                        if pcm_format is None:
                            pcm_format = chunk.pcm_format
                        elif chunk.pcm_format != pcm_format:
                            raise RuntimeError(
                                "engine changed PCM format inside one GenerationWindow"
                            )
                        output.write(chunk.data)
            if pcm_format is None:
                raise RuntimeError("engine completed without audible PCM")
            self._raise_if_superseded(token, cancel_event)
            callbacks.status("Preparing", "正在完成连续片段并统一处理")
            self.media.normalize_window_loudness_pcm(
                temporary,
                normalized_path,
                sample_rate=pcm_format.sample_rate,
                channels=pcm_format.channels,
                sample_format=pcm_format.sample_format,
                target_lufs=-20.0,
            )
            normalized_duration = (
                normalized_path.stat().st_size
                / pcm_format.sample_rate
                / pcm_format.bytes_per_frame
            )
            unit_texts = tuple(item.unit.text for item in window.units)
            boundary_timing = str(
                capabilities.get("window_boundary_timing") or "voiced"
            )
            if (
                boundary_timing == "asr"
                and len(window.units) > 1
                and not skip_alignment_validation
            ):
                if self.alignment_asr is None or not self.alignment_asr.is_available:
                    raise GeneratedAudioValidationError(
                        "本地词语核对组件暂时不可用；已进入完整朗读单元降级"
                    )
                callbacks.status("Preparing", "正在核对片段是否完整及词语边界")
                self.media.pcm_to_wav(
                    normalized_path,
                    alignment_wav,
                    sample_rate=pcm_format.sample_rate,
                    channels=pcm_format.channels,
                    sample_format=pcm_format.sample_format,
                )
                try:
                    alignment = self.alignment_asr.transcribe(
                        alignment_wav,
                        timeout_seconds=120.0,
                        word_timestamps=True,
                    )
                except Exception as exc:
                    # The same ASR service is also used when creating a voice,
                    # where an empty result correctly means the reference clip
                    # needs attention.  During reading it is only a fallible
                    # timing validator, so its wording must never escape as a
                    # fatal reference-audio error.
                    raise GeneratedAudioValidationError(
                        "本地词语核对暂时没有得到可靠结果；"
                        "已进入自动重试和完整朗读单元降级"
                    ) from exc
                try:
                    layout = locate_window_unit_frames_from_words(
                        total_frames=(
                            normalized_path.stat().st_size // pcm_format.bytes_per_frame
                        ),
                        sample_rate=pcm_format.sample_rate,
                        unit_texts=unit_texts,
                        words=alignment.words,
                    )
                except ValueError as exc:
                    raise GeneratedAudioValidationError(
                        "本地核对发现生成片段可能存在漏词或不完整；"
                        f"已拒绝当前候选并进入自动降级（{exc}）",
                        recognized_text=alignment.text,
                    ) from exc
            else:
                try:
                    layout = locate_window_unit_frames(
                        normalized_path,
                        pcm_format,
                        unit_texts,
                        timing_mode=(
                            boundary_timing
                            if boundary_timing in {"voiced", "hybrid"}
                            else "voiced"
                        ),
                    )
                except ValueError as exc:
                    if len(window.units) <= 1:
                        raise
                    raise GeneratedAudioValidationError(
                        "连续片段无法为每个朗读单元建立可靠音频边界；"
                        "已进入自动重试和完整朗读单元降级"
                    ) from exc
            level_balance = balance_window_unit_levels_pcm(
                normalized_path,
                pcm_format,
                layout,
                # A very short generated phrase may arrive substantially
                # louder than the rest of the same acoustic take.  Constant
                # attenuation is transparent and safe; amplification stays
                # tightly bounded so quiet noise is never pulled forward.
                maximum_gain_db=2.5,
                maximum_attenuation_db=6.0,
            )
            active_speech_seconds = self.media.measure_active_speech_seconds(
                normalized_path,
                sample_rate=pcm_format.sample_rate,
                channels=pcm_format.channels,
                sample_format=pcm_format.sample_format,
            )
            self.cache.put(
                identity,
                normalized_path,
                normalized_duration,
                {
                    "sample_rate": pcm_format.sample_rate,
                    "channels": pcm_format.channels,
                    "sample_format": pcm_format.sample_format,
                    "loudness_target_lufs": -20.0,
                    "generation_window": True,
                    "generation_window_unit_count": len(window.units),
                    "window_layout": layout.to_metadata(),
                    "model_boundary_silence_replaced_at_playback": True,
                    "active_speech_target_dbfs": -22.0,
                    "bounded_window_range_level_balance": list(level_balance),
                    "maximum_window_range_gain_db": 2.5,
                    "maximum_window_range_attenuation_db": 6.0,
                    "active_speech_seconds": active_speech_seconds,
                    "window_edge_guard_ms": WINDOW_EDGE_GUARD_MS,
                    "per_sentence_loudness": False,
                    "per_sentence_edge_conditioning": False,
                    "automatic_sentence_time_stretch": False,
                },
            )
            if document_id is not None:
                self.cache.note_document_use(document_id, identity.key)
            self._update_pace(
                resolved,
                window,
                active_speech_seconds,
                native_speed=native_speed,
                requested_speed=_native_synthesis_speed(settings, capabilities),
            )
            self._emit_window_with_optional_speed(
                token,
                window,
                pcm_format,
                normalized_path,
                layout,
                settings,
                native_speed=native_speed,
                cancel_event=cancel_event,
            )
            self._recycle_worker_after_safe_window_boundary(
                token,
                handle,
                capabilities,
                cancel_event,
                callbacks,
            )
        finally:
            temporary.unlink(missing_ok=True)
            normalized_path.unlink(missing_ok=True)
            alignment_wav.unlink(missing_ok=True)

    def _recycle_worker_after_safe_window_boundary(
        self,
        token: SessionToken,
        handle: EngineHandle,
        capabilities: dict[str, object],
        cancel_event: Event,
        callbacks: RuntimeCallbacks,
    ) -> None:
        """Release an expanded allocator only after its full window is buffered.

        ONNX Runtime deliberately retains arena allocations.  A process restart is
        the only reliable way to return that memory to Windows, so the package may
        declare a conservative RSS ceiling.  This method is never called while an
        engine stream is active and never touches the already-buffered PCM.
        """

        raw_limit = capabilities.get("worker_recycle_rss_mib")
        if raw_limit is None or not handle.is_loaded:
            return
        try:
            limit_mib = float(raw_limit)
        except (TypeError, ValueError):
            return
        if limit_mib <= 0:
            return
        resident_mib = handle.worker_rss_mib
        if resident_mib < limit_mib:
            return
        self._raise_if_superseded(token, cancel_event)
        callbacks.status(
            "Preparing",
            (
                "基础语音进程已到达内存回收边界，"
                f"正在安全释放（{resident_mib / 1024:.1f} GiB）"
            ),
        )
        handle.unload()
        with self._lock:
            self._worker_recycle_count += 1

    def _emit_best_cached_unit_run(
        self,
        token: SessionToken,
        units: tuple[ResolvedSpeechUnit, ...],
        *,
        start_index: int,
        minimum_units: int,
        settings: SynthesisSettings,
        handle: EngineHandle,
        capabilities: dict[str, object],
        cancel_event: Event,
        document_id: UUID | None,
        callbacks: RuntimeCallbacks,
    ) -> int:
        """Reuses one cached acoustic run only when it covers the planned window.

        A shorter cached fragment would force the following text to come from
        another inference and recreate the sentence-to-sentence voice reset we
        are trying to avoid.  A seek into a larger cached window remains hot
        because its suffix covers the complete newly planned run.
        """

        if minimum_units <= 0:
            raise ValueError("minimum_units must be positive")

        first = units[start_index]
        identity = _window_cache_identity(
            "",
            first,
            handle.manifest,
            capabilities,
            settings,
        )
        fingerprint = hashlib.sha256(first.unit.text.encode("utf-8")).hexdigest()
        hits = self.cache.find_window_units(
            signature_key=identity.content_signature,
            unit_text_sha256=fingerprint,
        )
        best: tuple[int, object, WindowAudioLayout] | None = None
        invalid_cache_keys: set[str] = set()
        for hit in hits:
            metadata = hit.entry.metadata
            try:
                stored_layout = WindowAudioLayout.from_metadata(
                    dict(metadata["window_layout"]),
                    expected_fingerprints=tuple(
                        str(item)
                        for item in metadata["window_layout"]["unit_text_sha256"]
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                if hit.entry.cache_key not in invalid_cache_keys:
                    invalid_cache_keys.add(hit.entry.cache_key)
                    LOGGER.warning(
                        "discarding_invalid_window_cache cache_key=%s reason=%s",
                        hit.entry.cache_key,
                        exc,
                    )
                    self.cache.remove(hit.entry.cache_key)
                continue
            matched = 0
            stored_index = hit.unit_index
            while (
                start_index + matched < len(units)
                and stored_index + matched < len(stored_layout.unit_ranges)
            ):
                current = units[start_index + matched]
                current_identity = _window_cache_identity(
                    "",
                    current,
                    handle.manifest,
                    capabilities,
                    settings,
                )
                current_hash = hashlib.sha256(
                    current.unit.text.encode("utf-8")
                ).hexdigest()
                if (
                    current_identity.content_signature != identity.content_signature
                    or current_hash
                    != stored_layout.unit_text_sha256[stored_index + matched]
                ):
                    break
                matched += 1
            if matched < minimum_units or (best is not None and matched <= best[0]):
                continue
            selected_ranges = tuple(
                UnitFrameRange(
                    unit_index=index,
                    start_frame=item.start_frame,
                    end_frame=item.end_frame,
                    boundary_confidence=item.boundary_confidence,
                )
                for index, item in enumerate(
                    stored_layout.unit_ranges[stored_index : stored_index + matched]
                )
            )
            selected_hashes = stored_layout.unit_text_sha256[
                stored_index : stored_index + matched
            ]
            best = (
                matched,
                hit,
                WindowAudioLayout(
                    stored_layout.total_frames,
                    selected_ranges,
                    selected_hashes,
                ),
            )
        if best is None:
            return 0
        matched, raw_hit, layout = best
        hit = raw_hit
        entry = hit.entry
        selected_units = units[start_index : start_index + matched]
        cached_window = GenerationWindow(
            units=selected_units,
            engine_text=build_window_engine_text(selected_units),
            estimated_seconds=max(0.01, entry.duration_seconds),
            maximum_request_chars=int(capabilities.get("max_request_chars") or 0),
            initial=self.buffer.buffered_seconds < self.buffer.watermarks.target_seconds,
        )
        pcm = PCMFormat(
            sample_rate=int(entry.metadata["sample_rate"]),
            channels=int(entry.metadata["channels"]),
            sample_format=str(entry.metadata["sample_format"]),
        )
        if document_id is not None:
            self.cache.note_document_use(document_id, entry.cache_key)
        callbacks.status(
            "Preparing",
            f"正在使用已准备的连续语音（{matched} 个朗读单元）",
        )
        self._emit_window_with_optional_speed(
            token,
            cached_window,
            pcm,
            entry.audio_path,
            layout,
            settings,
            native_speed=bool(capabilities.get("native_speed")),
            cancel_event=cancel_event,
        )
        return matched

    def _emit_window_with_optional_speed(
        self,
        token: SessionToken,
        window: GenerationWindow,
        pcm: PCMFormat,
        path: Path,
        layout: WindowAudioLayout,
        settings: SynthesisSettings,
        *,
        native_speed: bool,
        cancel_event: Event,
    ) -> None:
        source_path = path
        playback_layout = layout
        stretched: Path | None = None
        if not native_speed and abs(settings.speed - 1.0) >= 0.01:
            self.temp_root.mkdir(parents=True, exist_ok=True)
            stretched = self.temp_root / f"{uuid4().hex}-window-stretched.pcm"
            self.media.time_stretch_pcm(
                path,
                stretched,
                speed=settings.speed,
                sample_rate=pcm.sample_rate,
                channels=pcm.channels,
                sample_format=pcm.sample_format,
            )
            new_total_frames = stretched.stat().st_size // pcm.bytes_per_frame
            playback_layout = scale_window_layout(
                layout, new_total_frames=new_total_frames
            )
            source_path = stretched
        try:
            self._emit_window(
                token,
                window,
                pcm,
                source_path,
                playback_layout,
                settings,
                cancel_event,
            )
        finally:
            if stretched is not None:
                stretched.unlink(missing_ok=True)

    def _emit_window(
        self,
        token: SessionToken,
        window: GenerationWindow,
        pcm: PCMFormat,
        path: Path,
        layout: WindowAudioLayout,
        settings: SynthesisSettings,
        cancel_event: Event,
    ) -> None:
        frames_per_chunk = max(1, int(pcm.sample_rate * 0.25))
        bytes_per_chunk = frames_per_chunk * pcm.bytes_per_frame
        with path.open("rb") as source:
            for resolved, frame_range in zip(
                window.units, layout.unit_ranges, strict=True
            ):
                self._wait_for_buffer_room(token, cancel_event)
                remaining_frames = frame_range.end_frame - frame_range.start_frame
                if remaining_frames <= 0:
                    raise RuntimeError("GenerationWindow produced an empty SpeechUnit range")
                source.seek(frame_range.start_frame * pcm.bytes_per_frame)
                sequence = 0
                while remaining_frames > 0:
                    self._raise_if_superseded(token, cancel_event)
                    requested_frames = min(frames_per_chunk, remaining_frames)
                    data = source.read(requested_frames * pcm.bytes_per_frame)
                    frames = len(data) // pcm.bytes_per_frame
                    if frames <= 0:
                        raise RuntimeError("GenerationWindow PCM ended before its unit map")
                    self._push(
                        AudioChunk(
                            session_id=token.session_id,
                            generation_id=token.generation_id,
                            sequence=sequence,
                            speech_unit_id=resolved.unit.speech_unit_id,
                            pcm_format=pcm,
                            duration_seconds=frames / pcm.sample_rate,
                            data=data,
                        )
                    )
                    sequence += 1
                    remaining_frames -= frames
                self._emit_pause(
                    token,
                    resolved.unit,
                    pcm,
                    settings,
                    cancel_event,
                    sequence,
                )
                self._push(
                    AudioChunk(
                        session_id=token.session_id,
                        generation_id=token.generation_id,
                        sequence=sequence + 1,
                        speech_unit_id=resolved.unit.speech_unit_id,
                        pcm_format=pcm,
                        duration_seconds=0,
                        data=b"",
                        is_final=True,
                    )
                )

    def _emit_pause(
        self,
        token: SessionToken,
        unit: SpeechUnit,
        pcm: PCMFormat,
        settings: SynthesisSettings,
        cancel_event: Event,
        sequence: int = 1000000,
    ) -> None:
        milliseconds = pause_milliseconds(unit.text, settings)
        if milliseconds <= 0:
            return
        self._raise_if_superseded(token, cancel_event)
        frames = int(pcm.sample_rate * milliseconds / 1000)
        silence = bytes(frames * pcm.bytes_per_frame)
        self._push(
            AudioChunk(
                session_id=token.session_id,
                generation_id=token.generation_id,
                sequence=sequence,
                speech_unit_id=unit.speech_unit_id,
                pcm_format=pcm,
                duration_seconds=frames / pcm.sample_rate,
                data=silence,
            )
        )

    def _pace_for(self, resolved: ResolvedSpeechUnit) -> float:
        with self._lock:
            return self._pace_ema.get(_pace_key(resolved), 4.8)

    def _update_pace(
        self,
        resolved: ResolvedSpeechUnit,
        window: GenerationWindow,
        active_speech_seconds: float,
        *,
        native_speed: bool,
        requested_speed: float,
    ) -> None:
        spoken = sum(
            character.isalnum()
            for item in window.units
            for character in item.unit.text
        )
        if spoken <= 0 or active_speech_seconds <= 0:
            return
        measured = spoken / active_speech_seconds
        if native_speed:
            measured /= requested_speed
        if not 2.0 <= measured <= 8.0:
            return
        key = _pace_key(resolved)
        with self._lock:
            previous = self._pace_ema.get(key)
            self._pace_ema[key] = measured if previous is None else previous * 0.8 + measured * 0.2

    def _push(self, chunk: AudioChunk) -> None:
        if self.gate.accepts(chunk):
            self.buffer.push(chunk)

    def _wait_for_buffer_room(self, token: SessionToken, cancel_event: Event) -> None:
        while self.buffer.at_high_watermark:
            self._raise_if_superseded(token, cancel_event)
            time.sleep(0.015)

    def _raise_if_superseded(self, token: SessionToken, cancel_event: Event) -> None:
        if cancel_event.is_set() or self.gate.current() != token:
            raise SessionSuperseded()


def json_pcm(capabilities: dict[str, object]) -> str:
    pcm = dict(capabilities.get("pcm") or {})
    return f"{pcm.get('sample_rate', 0)}:{pcm.get('channels', 1)}:{pcm.get('sample_format', '')}"


def _split_generation_window(
    window: GenerationWindow,
    settings: SynthesisSettings,
) -> tuple[GenerationWindow, GenerationWindow]:
    """Bisect a failed window by audible duration, never inside a SpeechUnit."""

    if len(window.units) < 2:
        raise ValueError("a single-unit generation window cannot be split")
    estimates = [
        estimate_unit_seconds(item.unit.text, settings)
        for item in window.units
    ]
    target = sum(estimates) / 2
    running = 0.0
    cut = 1
    best_distance = float("inf")
    for candidate in range(1, len(window.units)):
        running += estimates[candidate - 1]
        distance = abs(running - target)
        if distance < best_distance:
            best_distance = distance
            cut = candidate
    groups = (window.units[:cut], window.units[cut:])
    result: list[GenerationWindow] = []
    for index, group in enumerate(groups):
        result.append(
            GenerationWindow(
                units=group,
                engine_text=build_window_engine_text(group),
                estimated_seconds=sum(
                    estimate_unit_seconds(item.unit.text, settings)
                    for item in group
                ),
                maximum_request_chars=window.maximum_request_chars,
                initial=window.initial and index == 0,
            )
        )
    return result[0], result[1]


def _window_cache_identity(
    text: str,
    resolved: ResolvedSpeechUnit,
    manifest,
    capabilities: dict[str, object],
    settings: SynthesisSettings,
) -> SynthesisCacheIdentity:
    native_speed = bool(capabilities.get("native_speed"))
    return SynthesisCacheIdentity(
        text=text,
        voice_id=resolved.voice_id,
        tier=resolved.tier,
        engine_id=manifest.engine_id,
        engine_version=manifest.engine_version,
        payload_version=resolved.payload.payload_format_version,
        payload_sha256=resolved.payload.sha256,
        reference_transcript_sha256=hashlib.sha256(
            resolved.reference_transcript.encode("utf-8")
        ).hexdigest(),
        language="zh-CN",
        style=settings.custom_style or "",
        emotion=settings.emotion,
        emotion_strength=settings.emotion_strength,
        speed_mode="native" if native_speed else "base",
        synthesis_speed=(
            _native_synthesis_speed(settings, capabilities) if native_speed else None
        ),
        # Reader pauses are rebuilt from frame ranges during playback.
        punctuation_pause_ms=0,
        paragraph_pause_ms=0,
        pcm_format=json_pcm(capabilities),
        postprocess_version=POSTPROCESS_VERSION,
    )


def _native_synthesis_speed(
    settings: SynthesisSettings, capabilities: dict[str, object]
) -> float:
    scale = float(capabilities.get("native_speed_scale") or 1.0)
    return min(2.0, max(0.5, settings.speed * scale))


def _runtime_request_texts(text: str, maximum_chars: int) -> tuple[str, ...]:
    """Micro-split audible text without sending layout whitespace to an engine."""

    audible_text = text.strip()
    if not audible_text:
        return (text,)
    if maximum_chars < 16 or len(audible_text) <= maximum_chars:
        return (audible_text,)
    parts = tuple(
        unit.text.strip()
        for unit in segment_text(audible_text, max_chars=maximum_chars).units
        if any(character.isalnum() for character in unit.text)
    )
    return parts or (audible_text,)


def _stable_voice_seed(voice_id: UUID) -> int:
    """Keeps one voice's sentence-to-sentence sampling character stable."""

    return int.from_bytes(hashlib.sha256(voice_id.bytes).digest()[:4], "big") & 0x7FFFFFFF


def _synthesis_seed(resolved: ResolvedSpeechUnit) -> int:
    """Uses the validated MOSS cross-language candidate without affecting other engines."""

    seed = _stable_voice_seed(resolved.voice_id)
    language = resolved.reference_language.lower()
    if resolved.payload.engine_id == "moss-nano-onnx" and language.startswith("en"):
        # The first deterministic path of an English prompt dropped Chinese
        # content in field feedback. The next prime-spaced path preserved the
        # complete sentence. Limit that choice to MOSS cross-language payloads.
        seed = (seed + 104729) & 0x7FFFFFFF
    return seed


def _uses_stable_moss_first_seed(resolved: ResolvedSpeechUnit) -> bool:
    """Keeps matching-language MOSS narration on one acoustic sampling path.

    English reference audio reading Chinese remains text-specific because the
    voice-wide path was proven to elongate or drop words for those payloads.
    """

    return (
        resolved.payload.engine_id == "moss-nano-onnx"
        and resolved.reference_language.lower().startswith("zh")
    )


def _pause_milliseconds(text: str, settings: SynthesisSettings) -> int:
    """Backward-compatible entry point for the Reader pause rule."""

    return pause_milliseconds(text, settings)


def _pace_key(resolved: ResolvedSpeechUnit) -> tuple[object, ...]:
    return (
        resolved.voice_id,
        resolved.tier,
        resolved.payload.engine_id,
        resolved.payload.engine_version,
        resolved.payload.payload_format_version,
        resolved.payload.sha256,
        resolved.reference_language.lower(),
    )


def _target_speech_seconds(text: str, requested_speed: float) -> float:
    """Returns a restrained timing target without flattening natural punctuation."""

    spoken = sum(character.isalnum() for character in text)
    last_spoken = max(
        (index for index, character in enumerate(text) if character.isalnum()),
        default=-1,
    )
    internal_pause = sum(
        0.16 if character in "；;" else 0.10
        for index, character in enumerate(text)
        if index < last_spoken and character in "，,、：:；;"
    )
    base_seconds = max(0.38, spoken / 4.8 + internal_pause)
    return base_seconds / requested_speed


def _timing_speed_factor(
    text: str,
    generated_duration_seconds: float,
    requested_speed: float,
    *,
    active_speech_seconds: float | None = None,
) -> float:
    """Applies requested speed and only restrains objectively extreme model pace."""

    if generated_duration_seconds <= 0:
        return 1.0
    if active_speech_seconds is not None and active_speech_seconds > 0:
        spoken = sum(character.isalnum() for character in text)
        measured_rate = spoken / active_speech_seconds if spoken else 0.0
        automatic_correction = 1.0
        if 0 < measured_rate < 3.6:
            maximum_slow_correction = 1.25 if spoken <= 8 else 1.10
            automatic_correction = min(
                maximum_slow_correction, 3.6 / measured_rate
            )
        elif measured_rate > 6.0:
            automatic_correction = max(0.90, 6.0 / measured_rate)
        return min(2.0, max(0.5, requested_speed * automatic_correction))
    return min(2.0, max(0.5, requested_speed))
