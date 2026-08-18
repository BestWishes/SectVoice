import numpy as np
import pytest

from sectvoice.core.reference_analysis import (
    analyze_reference_samples,
    recommend_reference_window,
)


def test_reference_window_prefers_voiced_region() -> None:
    waveform = np.zeros(1000, dtype=np.float32)
    waveform[300:700] = 0.4
    result = recommend_reference_window(waveform, 20, points_per_second=50, target_seconds=6)
    assert 2 <= result.start_seconds <= 8
    assert result.end_seconds - result.start_seconds == pytest.approx(6)


def test_reference_window_rejects_silence() -> None:
    with pytest.raises(ValueError, match="没有检测到"):
        recommend_reference_window(np.zeros(500, dtype=np.float32), 10)


def test_quality_report_blocks_silence() -> None:
    report = analyze_reference_samples(np.zeros(16_000 * 4, dtype=np.float32), 16_000)

    assert not report.is_usable
    assert "有效人声" in report.summary()


def test_quality_report_warns_about_internal_pause_and_fast_reference() -> None:
    sample_rate = 16_000
    time = np.arange(sample_rate * 6, dtype=np.float32) / sample_rate
    samples = 0.15 * np.sin(2 * np.pi * 180 * time)
    samples[sample_rate * 2 : sample_rate * 3] = 0
    report = analyze_reference_samples(
        samples,
        sample_rate,
        transcript="这是很快的参考文字" * 4,
    )

    assert report.is_usable
    assert report.longest_internal_pause_seconds >= 0.98
    assert any("长停顿" in item for item in report.warnings)
    assert any("语速偏快" in item for item in report.warnings)


def test_quality_report_accepts_stable_clean_reference() -> None:
    sample_rate = 16_000
    time = np.arange(sample_rate * 5, dtype=np.float32) / sample_rate
    samples = 0.12 * np.sin(2 * np.pi * 180 * time)
    report = analyze_reference_samples(samples, sample_rate, transcript="平稳参考文字用于测试")

    assert report.is_usable
    assert report.clipping_ratio == 0
