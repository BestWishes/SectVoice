from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from sectvoice.core.media import (
    FFmpegProcessor,
    SENTENCE_EDGE_GUARD_MS,
    _cap_internal_silence_pcm,
    _condition_sentence_edges_pcm,
    _measure_active_speech_seconds,
    _stabilize_active_speech_level,
)


def test_reference_duration_validation() -> None:
    processor = FFmpegProcessor(Path("missing-ffmpeg"))
    with pytest.raises(ValueError, match="between 3 and 30"):
        processor.prepare_reference(
            Path("input.mp3"),
            Path("output.wav"),
            0,
            1.5,
            sample_rate=48000,
            channels=2,
        )


def test_decode_selection_for_quality_analysis_uses_requested_range() -> None:
    processor = FFmpegProcessor(Path("ffmpeg.exe"))
    processor._run = Mock(  # type: ignore[method-assign]
        return_value=Mock(stdout=np.zeros(160, dtype="<f4").tobytes())
    )

    result = processor.decode_mono_f32(
        Path("input.mp3"),
        sample_rate=16_000,
        start_seconds=1.25,
        end_seconds=5.75,
    )

    arguments = processor._run.call_args.args[0]  # type: ignore[union-attr]
    assert arguments[arguments.index("-ss") + 1] == "1.250000"
    assert arguments[arguments.index("-t") + 1] == "4.500000"
    assert result.dtype == np.float32


def test_sentence_pcm_loudness_normalization_uses_raw_format(tmp_path: Path) -> None:
    processor = FFmpegProcessor(Path("ffmpeg.exe"))
    processor._run = Mock()  # type: ignore[method-assign]
    source = tmp_path / "source.pcm"
    destination = tmp_path / "normalized.pcm"

    processor.normalize_loudness_pcm(
        source,
        destination,
        sample_rate=48_000,
        channels=2,
        sample_format="f32le",
        target_lufs=-20.0,
    )

    arguments = processor._run.call_args.args[0]  # type: ignore[union-attr]
    filter_chain = next(item for item in arguments if "loudnorm=" in item)
    assert "loudnorm=I=-20.0:TP=-2.0:LRA=7.0" in filter_chain
    assert filter_chain == "loudnorm=I=-20.0:TP=-2.0:LRA=7.0"
    assert "silenceremove=" not in filter_chain
    assert arguments.count("f32le") == 2


def test_speech_speed_uses_formant_preserving_rubberband(tmp_path: Path) -> None:
    processor = FFmpegProcessor(Path("ffmpeg.exe"))
    processor._run = Mock()  # type: ignore[method-assign]

    processor.time_stretch_pcm(
        tmp_path / "source.pcm",
        tmp_path / "stretched.pcm",
        speed=0.92,
        sample_rate=48_000,
        channels=2,
        sample_format="f32le",
    )

    arguments = processor._run.call_args.args[0]  # type: ignore[union-attr]
    filter_chain = next(item for item in arguments if "rubberband=" in item)
    assert "tempo=0.920000" in filter_chain
    assert "pitch=1.0" in filter_chain
    assert "formant=preserved" in filter_chain
    assert "pitchq=quality" in filter_chain
    assert not any("atempo=" in item for item in arguments)


@pytest.mark.parametrize("sample_format", ["f32le", "s16le"])
def test_active_speech_level_stabilizes_short_pcm(
    tmp_path: Path, sample_format: str
) -> None:
    path = tmp_path / f"short-{sample_format}.pcm"
    source = np.full(4_800, 0.35, dtype=np.float32)
    if sample_format == "f32le":
        source.astype("<f4").tofile(path)
    else:
        np.rint(source * 32767).astype("<i2").tofile(path)

    _stabilize_active_speech_level(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format=sample_format,
    )

    dtype = "<f4" if sample_format == "f32le" else "<i2"
    result = np.fromfile(path, dtype=dtype).astype(np.float32)
    if sample_format == "s16le":
        result /= 32768.0
    rms_dbfs = 20 * np.log10(np.sqrt(np.mean(np.square(result))))
    assert rms_dbfs == pytest.approx(-22.0, abs=0.1)
    assert float(np.max(np.abs(result))) <= 10 ** (-2.0 / 20.0) + 1e-4


def test_active_speech_measurement_ignores_silence(tmp_path: Path) -> None:
    path = tmp_path / "timing.pcm"
    samples = np.concatenate(
        [np.zeros(4_800, dtype=np.float32), np.full(9_600, 0.1, dtype=np.float32)]
    )
    samples.astype("<f4").tofile(path)

    assert _measure_active_speech_seconds(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format="f32le",
    ) == pytest.approx(0.2)


def test_active_leveling_does_not_rewrite_sentence_onset_prosody(tmp_path: Path) -> None:
    path = tmp_path / "heavy-onset.pcm"
    source = np.concatenate(
        [
            np.full(12_000, 0.18, dtype=np.float32),
            np.full(36_000, 0.10, dtype=np.float32),
        ]
    )
    source.astype("<f4").tofile(path)
    before_ratio = float(np.sqrt(np.mean(np.square(source[:12_000])))) / float(
        np.sqrt(np.mean(np.square(source[12_000:])))
    )

    _stabilize_active_speech_level(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format="f32le",
    )

    result = np.fromfile(path, dtype="<f4")
    after_ratio = float(np.sqrt(np.mean(np.square(result[:12_000])))) / float(
        np.sqrt(np.mean(np.square(result[12_000:])))
    )
    assert after_ratio == pytest.approx(before_ratio, rel=0.001)


@pytest.mark.parametrize("sample_format", ["f32le", "s16le"])
def test_internal_long_silence_is_capped_without_removing_speech(
    tmp_path: Path, sample_format: str
) -> None:
    path = tmp_path / f"internal-gap-{sample_format}.pcm"
    source = np.concatenate(
        [
            np.full(9_600, 0.10, dtype=np.float32),
            np.zeros(33_600, dtype=np.float32),
            np.full(9_600, -0.10, dtype=np.float32),
        ]
    )
    if sample_format == "f32le":
        source.astype("<f4").tofile(path)
    else:
        np.rint(source * 32767.0).astype("<i2").tofile(path)

    removed = _cap_internal_silence_pcm(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format=sample_format,
    )

    dtype = "<f4" if sample_format == "f32le" else "<i2"
    result = np.fromfile(path, dtype=dtype).astype(np.float32)
    if sample_format == "s16le":
        result /= 32768.0
    assert removed == pytest.approx(0.52, abs=0.011)
    assert result.size / 48_000 == pytest.approx(0.58, abs=0.011)
    assert np.count_nonzero(result > 0.05) == 9_600
    assert np.count_nonzero(result < -0.05) == 9_600


@pytest.mark.parametrize("sample_format", ["f32le", "s16le"])
def test_sentence_edges_get_exact_zero_guards_and_fades(
    tmp_path: Path, sample_format: str
) -> None:
    path = tmp_path / f"edges-{sample_format}.pcm"
    source = np.concatenate(
        [
            np.full(4_800, 0.12, dtype=np.float32),
            np.full(4_800, -0.18, dtype=np.float32),
        ]
    )
    if sample_format == "f32le":
        source.astype("<f4").tofile(path)
    else:
        np.rint(source * 32767.0).astype("<i2").tofile(path)

    _condition_sentence_edges_pcm(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format=sample_format,
    )
    first_size = path.stat().st_size
    _condition_sentence_edges_pcm(
        path,
        sample_rate=48_000,
        channels=1,
        sample_format=sample_format,
    )

    dtype = "<f4" if sample_format == "f32le" else "<i2"
    result = np.fromfile(path, dtype=dtype).astype(np.float32)
    if sample_format == "s16le":
        result /= 32768.0
    guard_frames = round(48_000 * SENTENCE_EDGE_GUARD_MS / 1000)
    bytes_per_sample = 4 if sample_format == "f32le" else 2
    assert abs(path.stat().st_size - first_size) <= 10 * bytes_per_sample
    assert np.count_nonzero(result[:guard_frames]) == 0
    assert np.count_nonzero(result[-guard_frames:]) == 0
    assert result[guard_frames] == pytest.approx(0.0, abs=1e-6)
    assert result[-guard_frames - 1] == pytest.approx(0.0, abs=1e-6)
    assert float(np.max(np.abs(result))) > 0.10
