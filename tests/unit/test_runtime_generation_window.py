from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
from threading import Event
from types import MethodType, SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from sectvoice.core.audio_buffer import StreamingAudioBuffer
from sectvoice.core.asr import ASRError
from sectvoice.core.cache import VoiceCache
from sectvoice.core.database import Database
from sectvoice.core.generation_window import ResolvedSpeechUnit
from sectvoice.core.runtime import (
    GeneratedAudioValidationError,
    RuntimeCallbacks,
    VoiceRuntime,
)
from sectvoice.domain import (
    AudioChunk,
    EnginePayloadRef,
    PCMFormat,
    PayloadStatus,
    SynthesisSettings,
    Tier,
    utc_now_iso,
)
from sectvoice.reader.segmentation import segment_text


class _Media:
    def __init__(self) -> None:
        self.normalizations = 0
        self.stretches = 0

    def normalize_window_loudness_pcm(self, source, destination, **_kwargs) -> None:
        self.normalizations += 1
        shutil.copyfile(source, destination)

    def measure_active_speech_seconds(self, source, **kwargs) -> float:
        pcm = PCMFormat(
            int(kwargs["sample_rate"]),
            int(kwargs["channels"]),
            str(kwargs["sample_format"]),
        )
        raw_frames = source.stat().st_size // pcm.bytes_per_frame
        return raw_frames / pcm.sample_rate

    def time_stretch_pcm(self, source, destination, *, speed, **kwargs) -> None:
        self.stretches += 1
        pcm = PCMFormat(
            int(kwargs["sample_rate"]),
            int(kwargs["channels"]),
            str(kwargs["sample_format"]),
        )
        dtype = "<f4" if pcm.sample_format == "f32le" else "<i2"
        raw = np.fromfile(source, dtype=dtype)
        frames = raw.reshape(-1, pcm.channels)
        selected = np.linspace(
            0,
            frames.shape[0] - 1,
            max(1, round(frames.shape[0] / speed)),
        ).astype(int)
        frames[selected].astype(dtype).tofile(destination)

    def pcm_to_wav(self, source, destination, **_kwargs) -> None:
        shutil.copyfile(source, destination)


class _EmptyAlignmentASR:
    is_available = True

    def transcribe(self, *_args, **_kwargs):
        raise ASRError("所选片段没有识别到清晰人声，请重新选择")


class _EndClampedAlignmentASR:
    """Returns complete text whose final short unit starts at the PCM edge."""

    is_available = True

    def transcribe(self, *_args, **_kwargs):
        return SimpleNamespace(
            text="呵咦",
            words=(
                {"text": "呵", "start": 0.0, "end": 3.03},
                {"text": "咦", "start": 3.03, "end": 3.30},
            ),
        )


class _Client:
    def __init__(self, pcm_data: bytes, pcm: PCMFormat) -> None:
        self.pcm_data = pcm_data
        self.pcm = pcm
        self.calls: list[dict[str, object]] = []

    def stream_synthesis(self, **request):
        self.calls.append(request)
        yield AudioChunk(
            session_id=request["session_id"],
            generation_id=request["generation_id"],
            sequence=0,
            speech_unit_id=request["speech_unit_id"],
            pcm_format=self.pcm,
            duration_seconds=len(self.pcm_data)
            / self.pcm.bytes_per_frame
            / self.pcm.sample_rate,
            data=self.pcm_data,
        )


class _Handle:
    def __init__(
        self,
        client: _Client,
        *,
        native_speed: bool = False,
        native_speed_scale: float | None = None,
        worker_recycle_rss_mib: float | None = None,
        worker_rss_mib: float = 0.0,
        gpu_compute_pacing: dict[str, object] | None = None,
    ) -> None:
        self.client = client
        self.is_loaded = True
        self.worker_rss_mib = worker_rss_mib
        self.unload_calls = 0
        capabilities = {
            "native_speed": native_speed,
            "max_request_chars": 64,
            "pcm": {
                "sample_rate": client.pcm.sample_rate,
                "channels": client.pcm.channels,
                "sample_format": client.pcm.sample_format,
            },
        }
        if native_speed_scale is not None:
            capabilities["native_speed_scale"] = native_speed_scale
        if worker_recycle_rss_mib is not None:
            capabilities["worker_recycle_rss_mib"] = worker_recycle_rss_mib
        if gpu_compute_pacing is not None:
            capabilities["gpu_compute_pacing"] = gpu_compute_pacing
        self.manifest = SimpleNamespace(
            engine_id="engine",
            engine_version="1",
            raw={"capabilities": capabilities},
        )

    def cancel(self, *_args) -> None:
        return None

    def unload(self) -> None:
        self.unload_calls += 1
        self.is_loaded = False


class _Engines:
    def __init__(self, handle: _Handle) -> None:
        self.handle = handle

    def for_tier(self, _tier):
        return self.handle


def _resolved_units(texts: tuple[str, ...]) -> tuple[ResolvedSpeechUnit, ...]:
    voice_id = uuid4()
    now = utc_now_iso()
    payload = EnginePayloadRef(
        voice_id=voice_id,
        tier=Tier.BASIC,
        engine_id="engine",
        engine_version="1",
        payload_format_version="1",
        status=PayloadStatus.READY,
        opaque_path=Path("payload"),
        sha256="payload-hash",
        created_at=now,
        updated_at=now,
    )
    units = []
    cursor = 0
    for ordinal, text in enumerate(texts):
        unit = segment_text(text, max_chars=120).units[0]
        unit = replace(
            unit,
            ordinal=ordinal,
            start_char=cursor,
            end_char=cursor + len(text),
        )
        cursor += len(text)
        units.append(
            ResolvedSpeechUnit(unit, voice_id, Tier.BASIC, payload, "参考", "zh-CN")
        )
    return tuple(units)


def _runtime(tmp_path: Path, handle: _Handle, media: _Media, *, alignment_asr=None):
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    buffer = StreamingAudioBuffer()
    runtime = VoiceRuntime(
        engines=_Engines(handle),
        cache=VoiceCache(database, tmp_path / "cache"),
        buffer=buffer,
        temp_root=tmp_path / "temp",
        media=media,
        alignment_asr=alignment_asr,
    )
    return runtime, buffer


def _continuous_three_unit_pcm() -> tuple[PCMFormat, bytes]:
    pcm = PCMFormat(1_000, 1, "f32le")
    samples = np.concatenate(
        (
            np.full(800, 0.10, dtype=np.float32),
            np.zeros(150, dtype=np.float32),
            np.full(900, -0.10, dtype=np.float32),
            np.zeros(180, dtype=np.float32),
            np.full(1_000, 0.08, dtype=np.float32),
        )
    )
    return pcm, samples.astype("<f4").tobytes()


def _run(runtime: VoiceRuntime, units, settings) -> None:
    done = Event()
    errors: list[str] = []
    runtime.start(
        units,
        settings,
        RuntimeCallbacks(error=errors.append, completed=done.set),
    )
    assert done.wait(5), errors
    assert not errors


def test_short_units_use_one_engine_request_and_keep_original_unit_ids(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, buffer = _runtime(tmp_path, _Handle(client), media)
    units = _resolved_units(("好。", "到了。", "继续。"))

    _run(runtime, units, SynthesisSettings(punctuation_pause_ms=120))

    assert len(client.calls) == 1
    assert client.calls[0]["text"] == "好。到了。继续。"
    assert media.normalizations == 1
    chunks = []
    while (chunk := buffer.pop()) is not None:
        chunks.append(chunk)
    assert [chunk.speech_unit_id for chunk in chunks if chunk.is_final] == [
        item.unit.speech_unit_id for item in units
    ]
    assert sum(chunk.duration_seconds for chunk in chunks if chunk.data) == pytest.approx(
        3.06, abs=0.05
    )


def test_expanded_worker_is_recycled_only_after_full_window_is_buffered(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    handle = _Handle(
        _Client(data, pcm),
        worker_recycle_rss_mib=4_608,
        worker_rss_mib=4_900,
    )
    runtime, buffer = _runtime(tmp_path, handle, _Media())

    _run(runtime, _resolved_units(("好。", "到了。", "继续。")), SynthesisSettings())

    assert handle.unload_calls == 1
    chunks = []
    while (chunk := buffer.pop()) is not None:
        chunks.append(chunk)
    assert [chunk.speech_unit_id for chunk in chunks if chunk.is_final]


def test_worker_below_package_rss_limit_stays_warm(tmp_path: Path) -> None:
    pcm, data = _continuous_three_unit_pcm()
    handle = _Handle(
        _Client(data, pcm),
        worker_recycle_rss_mib=4_608,
        worker_rss_mib=4_500,
    )
    runtime, _buffer = _runtime(tmp_path, handle, _Media())

    _run(runtime, _resolved_units(("好。", "到了。", "继续。")), SynthesisSettings())

    assert handle.unload_calls == 0


def test_window_cache_reuses_pcm_for_new_document_unit_ids(tmp_path: Path) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, buffer = _runtime(tmp_path, _Handle(client), media)
    first = _resolved_units(("好。", "到了。", "继续。"))
    second = tuple(
        replace(item, unit=replace(item.unit, speech_unit_id=uuid4())) for item in first
    )

    _run(runtime, first, SynthesisSettings())
    while buffer.pop() is not None:
        pass
    _run(runtime, second, SynthesisSettings())

    assert len(client.calls) == 1
    assert media.normalizations == 1
    final_ids = []
    while (chunk := buffer.pop()) is not None:
        if chunk.is_final:
            final_ids.append(chunk.speech_unit_id)
    assert final_ids == [item.unit.speech_unit_id for item in second]


def test_seek_into_middle_of_cached_window_reuses_only_matching_suffix(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, buffer = _runtime(tmp_path, _Handle(client), media)
    original = _resolved_units(("好。", "到了。", "继续。"))

    _run(runtime, original, SynthesisSettings())
    while buffer.pop() is not None:
        pass
    suffix = tuple(
        replace(item, unit=replace(item.unit, speech_unit_id=uuid4()))
        for item in original[1:]
    )
    _run(runtime, suffix, SynthesisSettings())

    assert len(client.calls) == 1
    assert media.normalizations == 1
    final_ids = []
    while (chunk := buffer.pop()) is not None:
        if chunk.is_final:
            final_ids.append(chunk.speech_unit_id)
    assert final_ids == [item.unit.speech_unit_id for item in suffix]


def test_shorter_cached_fragments_do_not_replace_one_planned_acoustic_window(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    runtime, buffer = _runtime(tmp_path, _Handle(client), _Media())
    combined = _resolved_units(("好。", "到了。", "继续。", "出发。"))

    _run(runtime, combined[:3], SynthesisSettings())
    while buffer.pop() is not None:
        pass
    _run(runtime, combined[3:], SynthesisSettings())
    while buffer.pop() is not None:
        pass
    _run(runtime, combined, SynthesisSettings())

    assert len(client.calls) == 3
    assert client.calls[-1]["text"] == "好。到了。继续。出发。"


def test_basic_user_speed_is_applied_once_to_whole_window(tmp_path: Path) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, _buffer = _runtime(tmp_path, _Handle(client), media)

    _run(
        runtime,
        _resolved_units(("好。", "到了。", "继续。")),
        SynthesisSettings(speed=1.5),
    )

    assert len(client.calls) == 1
    assert media.stretches == 1


def test_native_speed_is_sent_once_and_not_postprocessed(tmp_path: Path) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, _buffer = _runtime(tmp_path, _Handle(client, native_speed=True), media)

    _run(
        runtime,
        _resolved_units(("好。", "到了。", "继续。")),
        SynthesisSettings(speed=1.5),
    )

    assert client.calls[0]["options"]["speed"] == pytest.approx(1.5)
    assert media.stretches == 0


def test_native_engine_package_can_apply_a_calibrated_speed_scale(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    media = _Media()
    runtime, _buffer = _runtime(
        tmp_path,
        _Handle(client, native_speed=True, native_speed_scale=0.96),
        media,
    )

    _run(
        runtime,
        _resolved_units(("好。", "到了。", "继续。")),
        SynthesisSettings(speed=1.5),
    )

    assert client.calls[0]["options"]["speed"] == pytest.approx(1.44)
    assert media.stretches == 0


def test_gpu_pacing_is_passed_but_fails_open_at_low_audible_buffer(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    config = {
        "active_fraction": 0.58,
        "token_interval": 8,
        "maximum_token_pause_seconds": 0.08,
        "maximum_chunk_pause_seconds": 0.12,
        "maximum_realtime_factor": 0.72,
    }
    runtime, buffer = _runtime(
        tmp_path,
        _Handle(client, gpu_compute_pacing=config),
        _Media(),
    )

    def seed_low_buffer(self, _token, _cancel_event) -> None:
        buffer.push(
            AudioChunk(
                session_id=uuid4(),
                generation_id=0,
                sequence=0,
                speech_unit_id=uuid4(),
                pcm_format=pcm,
                duration_seconds=1.0,
                data=b"",
            )
        )

    runtime._wait_for_buffer_room = MethodType(seed_low_buffer, runtime)  # type: ignore[method-assign]
    _run(runtime, _resolved_units(("低缓冲时必须优先保证连续播放。",)), SynthesisSettings())

    assert client.calls[0]["options"]["compute_pacing"] is False

    normal_client = _Client(data, pcm)
    normal_runtime, _normal_buffer = _runtime(
        tmp_path / "normal",
        _Handle(normal_client, gpu_compute_pacing=config),
        _Media(),
    )
    _run(
        normal_runtime,
        _resolved_units(("缓冲充足或首窗时使用平滑计算。",)),
        SynthesisSettings(),
    )
    assert normal_client.calls[0]["options"]["compute_pacing"] == config


def test_incomplete_aligned_window_uses_one_alternate_generation_path(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    runtime, _buffer = _runtime(tmp_path, _Handle(_Client(data, pcm)), _Media())
    attempts: list[int] = []
    statuses: list[tuple[str, str]] = []
    done = Event()
    errors: list[str] = []

    def fake_produce_window(
        self,
        *_args,
        synthesis_attempt: int = 0,
        **_kwargs,
    ) -> None:
        attempts.append(synthesis_attempt)
        if synthesis_attempt == 0:
            raise GeneratedAudioValidationError("incomplete")

    runtime._produce_window = MethodType(fake_produce_window, runtime)  # type: ignore[method-assign]
    runtime.start(
        _resolved_units(("第一句。", "第二句。")),
        SynthesisSettings(),
        RuntimeCallbacks(
            status=lambda state, message: statuses.append((state, message)),
            error=errors.append,
            completed=done.set,
        ),
    )

    assert done.wait(5), errors
    assert not errors
    assert attempts == [0, 1]
    assert any("自动重试" in message for _state, message in statuses)


def test_repeated_alignment_rejection_splits_at_complete_units_without_error(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    runtime, _buffer = _runtime(tmp_path, _Handle(_Client(data, pcm)), _Media())
    calls: list[tuple[tuple[str, ...], int, bool]] = []
    done = Event()
    errors: list[str] = []

    def fake_produce_window(
        self,
        _token,
        window,
        *_args,
        synthesis_attempt: int = 0,
        skip_alignment_validation: bool = False,
        **_kwargs,
    ) -> None:
        texts = tuple(item.unit.text for item in window.units)
        calls.append((texts, synthesis_attempt, skip_alignment_validation))
        if len(texts) > 1:
            raise GeneratedAudioValidationError(
                "ASR itself was uncertain",
                recognized_text="识别结果可能有误",
            )

    runtime._produce_window = MethodType(fake_produce_window, runtime)  # type: ignore[method-assign]
    units = _resolved_units(
        (
            "收到罗同的命令，外面走近来几个执法堂弟子，拖着跪在地上的几人就要前往地牢。",
            "大人饶命！",
            "不关我们的事啊！",
        )
    )
    runtime.start(
        units,
        SynthesisSettings(),
        RuntimeCallbacks(error=errors.append, completed=done.set),
    )

    assert done.wait(5), errors
    assert not errors
    assert [texts for texts, _attempt, _skip in calls if len(texts) == 1] == [
        (item.unit.text,) for item in units
    ]
    assert all(
        texts == tuple(item.unit.text for item in units[:1])
        or all(text in {item.unit.text for item in units} for text in texts)
        for texts, _attempt, _skip in calls
    )


def test_single_complete_speech_unit_is_never_rejected_by_alignment(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    handle = _Handle(client)
    handle.manifest.raw["capabilities"]["window_boundary_timing"] = "asr"
    runtime, buffer = _runtime(tmp_path, handle, _Media())
    text = "等到这里彻底安静下来之后，罗同对着空无一人的宅院说道。"

    _run(runtime, _resolved_units((text,)), SynthesisSettings())

    assert client.calls[0]["text"] == text
    final_chunks = []
    while (chunk := buffer.pop()) is not None:
        if chunk.is_final:
            final_chunks.append(chunk)
    assert len(final_chunks) == 1


def test_arbitrary_literal_text_is_forwarded_without_content_validation_error(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    handle = _Handle(client)
    handle.manifest.raw["capabilities"]["window_boundary_timing"] = "asr"
    runtime, _buffer = _runtime(tmp_path, handle, _Media())
    text = "xyz乱序词@@甲乙123不通，也照着文字读。"

    _run(runtime, _resolved_units((text,)), SynthesisSettings())

    assert len(client.calls) == 1
    assert client.calls[0]["text"] == text


def test_empty_alignment_asr_result_never_escapes_as_reference_audio_error(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    handle = _Handle(client)
    handle.manifest.raw["capabilities"]["window_boundary_timing"] = "asr"
    runtime, buffer = _runtime(
        tmp_path,
        handle,
        _Media(),
        alignment_asr=_EmptyAlignmentASR(),
    )
    texts = (
        "> 你又走不了、反击成本高 → 我的攻击成本很低。\n",
        "那么**地位高的人欺负地位低的人，不只是权力使人变坏。**",
    )
    units = _resolved_units(texts)

    _run(runtime, units, SynthesisSettings())

    assert len(client.calls) == 4
    assert all(
        "没有识别到清晰人声" not in str(call)
        for call in client.calls
    )
    final_ids = []
    while (chunk := buffer.pop()) is not None:
        if chunk.is_final:
            final_ids.append(chunk.speech_unit_id)
    assert final_ids == [unit.unit.speech_unit_id for unit in units]


def test_end_clamped_short_unit_is_split_and_read_instead_of_failing(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    handle = _Handle(client)
    handle.manifest.raw["capabilities"]["window_boundary_timing"] = "asr"
    runtime, buffer = _runtime(
        tmp_path,
        handle,
        _Media(),
        alignment_asr=_EndClampedAlignmentASR(),
    )
    units = _resolved_units(("呵——。", "咦？"))

    _run(runtime, units, SynthesisSettings())

    assert len(client.calls) == 4
    final_ids = []
    while (chunk := buffer.pop()) is not None:
        if chunk.is_final:
            final_ids.append(chunk.speech_unit_id)
    assert final_ids == [unit.unit.speech_unit_id for unit in units]


def test_invalid_cached_empty_range_is_discarded_and_regenerated(
    tmp_path: Path,
) -> None:
    pcm, data = _continuous_three_unit_pcm()
    client = _Client(data, pcm)
    runtime, _buffer = _runtime(tmp_path, _Handle(client), _Media())
    units = _resolved_units(("第一句。", "第二句。", "第三句。"))

    _run(runtime, units, SynthesisSettings())
    with runtime.cache.database.connect() as connection:
        row = connection.execute(
            "SELECT cache_key, metadata_json FROM cache_index"
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        final_range = metadata["window_layout"]["unit_ranges"][-1]
        final_range["start_frame"] = final_range["end_frame"]
        connection.execute(
            "UPDATE cache_index SET metadata_json=? WHERE cache_key=?",
            (json.dumps(metadata), row["cache_key"]),
        )

    _run(runtime, units, SynthesisSettings())

    assert len(client.calls) == 2
    with runtime.cache.database.connect() as connection:
        refreshed = json.loads(
            connection.execute(
                "SELECT metadata_json FROM cache_index"
            ).fetchone()["metadata_json"]
        )
    final_range = refreshed["window_layout"]["unit_ranges"][-1]
    assert final_range["end_frame"] > final_range["start_frame"]
