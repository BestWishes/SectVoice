from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sectvoice.core.media import _condition_window_edges_pcm
from sectvoice.core.window_audio import (
    UnitFrameRange,
    WindowAudioLayout,
    balance_window_unit_levels_pcm,
    locate_window_unit_frames,
    locate_window_unit_frames_from_words,
    scale_window_layout,
)
from sectvoice.domain import PCMFormat


def test_quiet_sentence_boundaries_map_three_units_without_audio_overlap(
    tmp_path: Path,
) -> None:
    sample_rate = 1_000
    path = tmp_path / "window.pcm"
    samples = np.concatenate(
        (
            np.full(500, 0.12, dtype=np.float32),
            np.zeros(220, dtype=np.float32),
            np.full(900, -0.10, dtype=np.float32),
            np.zeros(300, dtype=np.float32),
            np.full(1_100, 0.08, dtype=np.float32),
        )
    )
    samples.astype("<f4").tofile(path)

    layout = locate_window_unit_frames(
        path,
        PCMFormat(sample_rate, 1, "f32le"),
        ("甲乙丙丁戊。", "甲乙丙丁戊己庚辛壬。", "甲乙丙丁戊己庚辛壬癸子。"),
    )

    assert [item.boundary_confidence for item in layout.unit_ranges[:2]] == [
        "quiet-run",
        "quiet-run",
    ]
    assert layout.unit_ranges[0].end_frame == pytest.approx(510, abs=20)
    assert layout.unit_ranges[1].start_frame == pytest.approx(710, abs=20)
    assert layout.unit_ranges[1].end_frame == pytest.approx(1_630, abs=20)
    assert layout.unit_ranges[2].start_frame == pytest.approx(1_910, abs=20)
    assert np.count_nonzero(samples[layout.unit_ranges[0].end_frame - 5 : layout.unit_ranges[0].end_frame]) == 0
    assert np.count_nonzero(samples[layout.unit_ranges[1].start_frame : layout.unit_ranges[1].start_frame + 5]) == 0


def test_no_quiet_boundary_partitions_but_does_not_delete_pcm(tmp_path: Path) -> None:
    path = tmp_path / "continuous.pcm"
    np.full(2_000, 0.1, dtype="<f4").tofile(path)

    layout = locate_window_unit_frames(
        path,
        PCMFormat(1_000, 1, "f32le"),
        ("甲乙丙丁", "戊己庚辛"),
    )

    first, second = layout.unit_ranges
    assert first.boundary_confidence == "estimated"
    assert first.end_frame == second.start_frame
    assert (first.end_frame - first.start_frame) + (
        second.end_frame - second.start_frame
    ) == 2_000


def test_short_phonetic_quiet_run_is_not_used_as_sentence_boundary(
    tmp_path: Path,
) -> None:
    sample_rate = 1_000
    path = tmp_path / "phonetic-closure.pcm"
    samples = np.concatenate(
        (
            np.full(3_300, 0.10, dtype=np.float32),
            np.zeros(50, dtype=np.float32),  # low-energy closure inside a word
            np.full(450, 0.10, dtype=np.float32),
            np.zeros(360, dtype=np.float32),  # actual full-stop boundary
            np.full(1_600, -0.10, dtype=np.float32),
        )
    )
    samples.astype("<f4").tofile(path)

    layout = locate_window_unit_frames(
        path,
        PCMFormat(sample_rate, 1, "f32le"),
        (
            "趁着晚霞，许川将赤血刀法的秘籍拿出来。",
            "准备先修炼一番。",
        ),
    )

    first, second = layout.unit_ranges
    assert first.boundary_confidence == "quiet-run"
    assert first.end_frame == pytest.approx(3_810, abs=20)
    assert second.start_frame == pytest.approx(4_150, abs=20)


def test_boundary_prediction_ignores_large_model_pauses_after_short_units(
    tmp_path: Path,
) -> None:
    sample_rate = 1_000
    path = tmp_path / "uneven-pauses.pcm"
    samples = np.concatenate(
        (
            np.full(1_500, 0.1, dtype=np.float32),
            np.zeros(500, dtype=np.float32),  # comma inside the long sentence
            np.full(8_000, 0.1, dtype=np.float32),
            np.zeros(1_600, dtype=np.float32),  # first SpeechUnit boundary
            np.full(500, 0.1, dtype=np.float32),
            np.zeros(2_000, dtype=np.float32),  # very short second unit boundary
            np.full(1_300, 0.1, dtype=np.float32),
            np.full(1_500, -0.1, dtype=np.float32),
        )
    )
    samples.astype("<f4").tofile(path)

    layout = locate_window_unit_frames(
        path,
        PCMFormat(sample_rate, 1, "f32le"),
        (
            "各种妖兽频出，就算是许川，也没办法保证自身安全，更别说还带着一个累赘。",
            "到了。",
            "许川点点头。",
            "窗外的雨停了。",
        ),
    )

    assert layout.unit_ranges[0].end_frame == pytest.approx(10_010, abs=40)
    assert layout.unit_ranges[1].start_frame == pytest.approx(11_590, abs=40)
    assert layout.unit_ranges[1].end_frame == pytest.approx(12_110, abs=40)
    assert layout.unit_ranges[2].start_frame == pytest.approx(14_090, abs=40)


def test_cached_layout_validates_ordered_text_fingerprints(tmp_path: Path) -> None:
    path = tmp_path / "one.pcm"
    np.ones(1_000, dtype="<f4").tofile(path)
    layout = locate_window_unit_frames(
        path, PCMFormat(1_000, 1, "f32le"), ("甲。",)
    )

    restored = WindowAudioLayout.from_metadata(
        layout.to_metadata(), expected_texts=("甲。",)
    )
    assert restored == layout
    with pytest.raises(ValueError, match="do not match"):
        WindowAudioLayout.from_metadata(layout.to_metadata(), expected_texts=("乙。",))


def test_asr_word_alignment_keeps_homophones_and_long_pause_outside_units() -> None:
    layout = locate_window_unit_frames_from_words(
        total_frames=5_000,
        sample_rate=1_000,
        unit_texts=("各种妖兽频出。", "到了。"),
        words=(
            {"text": "各种腰瘦瓶足", "start": 0.1, "end": 2.0},
            {"text": "到了", "start": 3.5, "end": 4.0},
        ),
    )

    first, second = layout.unit_ranges
    assert first.boundary_confidence == "asr-aligned"
    assert first.end_frame == 2_050
    assert second.start_frame == 3_450
    assert second.end_frame == 5_000


def test_asr_alignment_keeps_a_multi_character_word_before_the_sentence_cut() -> None:
    layout = locate_window_unit_frames_from_words(
        total_frames=6_000,
        sample_rate=1_000,
        unit_texts=(
            "趁着晚霞，许川将赤血刀法的秘籍拿出来。",
            "准备先修炼一番。",
        ),
        words=(
            {"text": "趁着晚霞许川将赤血刀法的秘籍拿", "start": 0.0, "end": 3.4},
            {"text": "出来", "start": 3.4, "end": 3.8},
            {"text": "准备先修炼一番", "start": 4.2, "end": 5.8},
        ),
    )

    first, second = layout.unit_ranges
    assert first.boundary_confidence == "asr-aligned"
    assert first.end_frame == 3_850
    assert second.start_frame == 4_150


def test_asr_word_alignment_rejects_an_incomplete_extreme_short_unit() -> None:
    with pytest.raises(ValueError, match="may be incomplete"):
        locate_window_unit_frames_from_words(
            total_frames=5_000,
            sample_rate=1_000,
            unit_texts=("各种妖兽频出。", "到了。"),
            words=(
                {"text": "各种腰瘦瓶足", "start": 0.1, "end": 2.0},
                {"text": "到", "start": 3.5, "end": 3.8},
            ),
        )


def test_asr_validates_one_complete_long_sentence_without_splitting_it() -> None:
    layout = locate_window_unit_frames_from_words(
        total_frames=8_000,
        sample_rate=1_000,
        unit_texts=("普通人几乎没有机会能够进入秘境，因为秘境不会对外开放。",),
        words=(
            {
                "text": "普通人几乎没有机会能够进入秘境因为秘境不会对外开放",
                "start": 0.1,
                "end": 7.8,
            },
        ),
    )

    assert layout.unit_ranges == (UnitFrameRange(0, 0, 8_000, "asr-validated"),)


def test_asr_rejects_one_long_sentence_with_two_consecutive_missing_characters() -> None:
    with pytest.raises(ValueError, match="consecutive words may be incomplete"):
        locate_window_unit_frames_from_words(
            total_frames=8_000,
            sample_rate=1_000,
            unit_texts=("普通人几乎没有机会能够进入秘境因为秘境不会对外开放",),
            words=(
                {
                    "text": "普通人几乎没有机会能够进入因为秘境不会对外开放",
                    "start": 0.1,
                    "end": 7.8,
                },
            ),
        )


def test_layout_scales_once_with_whole_window_time_stretch(tmp_path: Path) -> None:
    path = tmp_path / "two.pcm"
    samples = np.concatenate(
        (np.ones(800, dtype=np.float32), np.zeros(200), -np.ones(1_000, dtype=np.float32))
    )
    samples.astype("<f4").tofile(path)
    layout = locate_window_unit_frames(
        path, PCMFormat(1_000, 1, "f32le"), ("第一句。", "第二句。")
    )

    scaled = scale_window_layout(layout, new_total_frames=4_000)
    assert scaled.total_frames == 4_000
    assert scaled.unit_ranges[0].end_frame == layout.unit_ranges[0].end_frame * 2
    assert scaled.unit_ranges[-1].end_frame == 4_000


def test_window_edge_conditioning_adds_one_small_outer_guard_only(tmp_path: Path) -> None:
    path = tmp_path / "edges.pcm"
    np.full(1_000, 0.2, dtype="<f4").tofile(path)

    _condition_window_edges_pcm(
        path, sample_rate=1_000, channels=1, sample_format="f32le"
    )
    result = np.fromfile(path, dtype="<f4")

    assert result.size == 1_020
    assert np.count_nonzero(result[:10]) == 0
    assert np.count_nonzero(result[-10:]) == 0
    assert np.count_nonzero(result[20:-20]) > 900


def test_window_level_balance_removes_near_far_drift_without_touching_gaps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "levels.pcm"
    samples = np.concatenate(
        (
            np.full(1_000, 0.04, dtype=np.float32),
            np.zeros(200, dtype=np.float32),
            np.full(1_000, 0.10, dtype=np.float32),
        )
    )
    samples.astype("<f4").tofile(path)
    layout = locate_window_unit_frames(
        path,
        PCMFormat(1_000, 1, "f32le"),
        ("第一句。", "第二句。"),
    )

    result = balance_window_unit_levels_pcm(
        path, PCMFormat(1_000, 1, "f32le"), layout, maximum_gain_db=8.0
    )
    balanced = np.fromfile(path, dtype="<f4")

    assert len(result) == 2
    assert result[0]["after_active_rms_dbfs"] == pytest.approx(-22.0, abs=0.05)
    assert result[1]["after_active_rms_dbfs"] == pytest.approx(-22.0, abs=0.05)
    assert np.count_nonzero(balanced[1_000:1_200]) == 0


def test_window_level_balance_can_attenuate_loud_short_unit_without_boosting_noise(
    tmp_path: Path,
) -> None:
    path = tmp_path / "asymmetric-levels.pcm"
    np.concatenate(
        (
            np.full(1_000, 0.04, dtype=np.float32),
            np.zeros(200, dtype=np.float32),
            np.full(300, 0.18, dtype=np.float32),
        )
    ).astype("<f4").tofile(path)
    layout = locate_window_unit_frames(
        path,
        PCMFormat(1_000, 1, "f32le"),
        ("平稳的第一句。", "到了。"),
    )

    result = balance_window_unit_levels_pcm(
        path,
        PCMFormat(1_000, 1, "f32le"),
        layout,
        maximum_gain_db=2.5,
        maximum_attenuation_db=6.0,
    )

    assert result[0]["gain_db"] == pytest.approx(2.5)
    assert result[1]["gain_db"] == pytest.approx(-6.0)
