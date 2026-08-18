from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import math
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class ReferenceSuggestion:
    start_seconds: float
    end_seconds: float
    message: str


@dataclass(frozen=True, slots=True)
class ReferenceQualityReport:
    """Engine-neutral evidence about one selected voice reference.

    The report deliberately separates technical blockers from style warnings.
    Expressive or fast speech is still usable, but users should know that a
    cloning engine can copy those traits into unrelated narration.
    """

    duration_seconds: float
    active_speech_seconds: float
    active_ratio: float
    peak_dbfs: float
    clipping_ratio: float
    active_level_spread_db: float
    longest_internal_pause_seconds: float
    rapid_modulation_score: float
    pitch_jitter_semitones: float
    speaking_rate_chars_per_second: float | None
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def is_usable(self) -> bool:
        return not self.blocking_reasons

    @property
    def grade(self) -> str:
        if self.blocking_reasons:
            return "invalid"
        if self.warnings:
            return "caution"
        return "good"

    def summary(self) -> str:
        if self.blocking_reasons:
            heading = "参考片段不可用"
            details = self.blocking_reasons
        elif self.warnings:
            heading = "参考片段可用，但有质量风险"
            details = self.warnings
        else:
            heading = "参考片段技术质量良好"
            details = ("未发现明显削波、长空白、快速周期抖动或语速风险。",)
        metrics = (
            f"有效人声 {self.active_speech_seconds:.1f}/{self.duration_seconds:.1f} 秒，"
            f"最长内部停顿 {self.longest_internal_pause_seconds:.2f} 秒，"
            f"峰值 {self.peak_dbfs:.1f} dBFS"
        )
        return heading + "：" + "；".join(details) + "\n" + metrics

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["grade"] = self.grade
        result["is_usable"] = self.is_usable
        return result


def recommend_reference_window(
    waveform: np.ndarray,
    duration_seconds: float,
    *,
    points_per_second: int = 50,
    target_seconds: float = 10.0,
) -> ReferenceSuggestion:
    if duration_seconds < 3 or waveform.size < points_per_second * 3:
        raise ValueError("参考音频太短，至少需要3秒清晰人声")
    if float(np.max(waveform)) < 0.008:
        raise ValueError("音频中没有检测到明显人声")
    window = min(int(target_seconds * points_per_second), waveform.size)
    squared = np.square(waveform.astype(np.float64))
    energy = np.convolve(squared, np.ones(window), mode="valid") / window
    # Penalize windows with too much silence; music/overlap remains an explicit user warning.
    voiced = np.convolve((waveform > 0.015).astype(np.float64), np.ones(window), mode="valid") / window
    score = energy * np.clip(voiced, 0.05, 1.0)
    start_index = int(np.argmax(score))
    start = start_index / points_per_second
    end = min(duration_seconds, start + window / points_per_second)
    if end - start < 3:
        start = max(0.0, end - 3.0)
    return ReferenceSuggestion(
        start_seconds=start,
        end_seconds=end,
        message="已选择能量较稳定的片段；仍请人工确认只有目标人物、无重叠说话和明显背景音乐。",
    )


def analyze_reference_samples(
    samples: np.ndarray,
    sample_rate: int,
    *,
    transcript: str = "",
) -> ReferenceQualityReport:
    """Analyze decoded mono float samples without importing an ML model.

    These metrics are intentionally conservative: only objectively unusable
    audio is blocked.  Pitch/envelope measurements are advisory because an
    expressive performance can look similar to tremor in signal statistics.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    array = np.asarray(samples, dtype=np.float32)
    if array.ndim == 2:
        array = np.mean(array, axis=1, dtype=np.float32)
    elif array.ndim != 1:
        raise ValueError("reference samples must be mono or frame/channel audio")
    array = np.nan_to_num(array, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    duration = array.size / float(sample_rate)
    peak = float(np.max(np.abs(array))) if array.size else 0.0
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-9))
    clipping_ratio = float(np.mean(np.abs(array) >= 0.995)) if array.size else 0.0

    frame_size = max(1, int(round(sample_rate * 0.02)))
    frame_count = array.size // frame_size
    if frame_count:
        framed = array[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(framed, dtype=np.float64), axis=1) + 1e-12)
    else:
        frame_rms = np.zeros((0,), dtype=np.float64)
    active = frame_rms >= 10 ** (-45.0 / 20.0)
    active_seconds = float(np.count_nonzero(active)) * 0.02
    active_ratio = active_seconds / duration if duration > 0 else 0.0
    longest_pause = _longest_internal_false_run(active) * 0.02

    broad_size = max(1, int(round(sample_rate * 0.10)))
    broad_count = array.size // broad_size
    if broad_count:
        broad = array[: broad_count * broad_size].reshape(broad_count, broad_size)
        broad_rms = np.sqrt(np.mean(np.square(broad, dtype=np.float64), axis=1) + 1e-12)
        broad_active = broad_rms[broad_rms >= 10 ** (-45.0 / 20.0)]
    else:
        broad_active = np.zeros((0,), dtype=np.float64)
    if broad_active.size >= 4:
        levels = 20.0 * np.log10(np.maximum(broad_active, 1e-9))
        level_spread = float(np.percentile(levels, 90) - np.percentile(levels, 10))
    else:
        level_spread = 0.0

    modulation_score = _rapid_modulation_score(frame_rms, active)
    pitch_jitter = _pitch_jitter_semitones(array, sample_rate, frame_rms)
    spoken_characters = sum(
        character.isalnum() and not character.isspace() for character in transcript
    )
    speaking_rate = (
        spoken_characters / active_seconds
        if transcript.strip() and spoken_characters and active_seconds > 0
        else None
    )

    blockers: list[str] = []
    warnings: list[str] = []
    if duration < 3.0:
        blockers.append("不足3秒")
    if peak < 0.002 or active_seconds < 2.0 or active_ratio < 0.20:
        blockers.append("没有足够清晰的有效人声")
    if clipping_ratio >= 0.03:
        blockers.append("严重削波失真")
    elif clipping_ratio >= 0.001:
        warnings.append(f"检测到约{clipping_ratio:.2%}削波采样")
    if active_ratio < 0.50 and active_seconds >= 2.0:
        warnings.append("空白比例偏高，可能复制断续节奏")
    if longest_pause >= 0.80:
        warnings.append(f"片段内部有{longest_pause:.2f}秒长停顿")
    if level_spread >= 18.0:
        warnings.append("人声强弱或录音距离变化较大")
    if speaking_rate is not None:
        if speaking_rate > 5.6:
            warnings.append(f"参考语速偏快（约{speaking_rate:.1f}字/秒）")
        elif speaking_rate < 2.2:
            warnings.append(f"参考语速偏慢（约{speaking_rate:.1f}字/秒）")
    if modulation_score >= 0.72 and pitch_jitter >= 0.85:
        warnings.append("检测到较强的快速周期波动，可能带来颤音或卡顿感")

    return ReferenceQualityReport(
        duration_seconds=duration,
        active_speech_seconds=active_seconds,
        active_ratio=active_ratio,
        peak_dbfs=peak_dbfs,
        clipping_ratio=clipping_ratio,
        active_level_spread_db=level_spread,
        longest_internal_pause_seconds=longest_pause,
        rapid_modulation_score=modulation_score,
        pitch_jitter_semitones=pitch_jitter,
        speaking_rate_chars_per_second=speaking_rate,
        blocking_reasons=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _longest_internal_false_run(active: np.ndarray) -> int:
    indexes = np.flatnonzero(active)
    if indexes.size < 2:
        return 0
    middle = active[int(indexes[0]) : int(indexes[-1]) + 1]
    padded = np.pad((~middle).astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return int(np.max(ends - starts)) if starts.size else 0


def _rapid_modulation_score(frame_rms: np.ndarray, active: np.ndarray) -> float:
    if frame_rms.size < 100 or np.count_nonzero(active) < 50:
        return 0.0
    envelope = 20.0 * np.log10(np.maximum(frame_rms, 1e-9))
    active_levels = envelope[active]
    floor = float(np.percentile(active_levels, 15))
    envelope = np.maximum(envelope, floor)
    trend_width = min(51, envelope.size if envelope.size % 2 else envelope.size - 1)
    if trend_width < 5:
        return 0.0
    trend = np.convolve(envelope, np.ones(trend_width) / trend_width, mode="same")
    residual = (envelope - trend) * active.astype(np.float64)
    spectrum = np.square(np.abs(np.fft.rfft(residual)))
    frequencies = np.fft.rfftfreq(residual.size, d=0.02)
    total = float(np.sum(spectrum[(frequencies >= 0.5) & (frequencies <= 15.0)]))
    rapid = float(np.sum(spectrum[(frequencies >= 3.0) & (frequencies <= 12.0)]))
    return rapid / total if total > 1e-9 else 0.0


def _pitch_jitter_semitones(
    samples: np.ndarray,
    sample_rate: int,
    frame_rms: np.ndarray,
) -> float:
    window_size = max(128, int(round(sample_rate * 0.05)))
    hop = max(1, int(round(sample_rate * 0.02)))
    if samples.size < window_size:
        return 0.0
    minimum_lag = max(1, int(sample_rate / 420.0))
    maximum_lag = min(window_size - 2, int(sample_rate / 65.0))
    if maximum_lag <= minimum_lag:
        return 0.0
    nfft = 1 << (2 * window_size - 1).bit_length()
    taper = np.hanning(window_size).astype(np.float32)
    pitches: list[float] = []
    for index, start in enumerate(range(0, samples.size - window_size + 1, hop)):
        if index < frame_rms.size and frame_rms[index] < 10 ** (-42.0 / 20.0):
            pitches.append(float("nan"))
            continue
        frame = samples[start : start + window_size].astype(np.float64)
        frame = (frame - np.mean(frame)) * taper
        spectrum = np.fft.rfft(frame, n=nfft)
        correlation = np.fft.irfft(spectrum * np.conjugate(spectrum), n=nfft)[:window_size]
        if correlation[0] <= 1e-10:
            pitches.append(float("nan"))
            continue
        segment = correlation[minimum_lag : maximum_lag + 1]
        lag = int(np.argmax(segment)) + minimum_lag
        confidence = float(correlation[lag] / correlation[0])
        pitches.append(sample_rate / lag if confidence >= 0.32 else float("nan"))
    values = np.asarray(pitches, dtype=np.float64)
    if values.size < 5:
        return 0.0
    valid_pairs = np.isfinite(values[1:]) & np.isfinite(values[:-1])
    if np.count_nonzero(valid_pairs) < 4:
        return 0.0
    deltas = np.abs(12.0 * np.log2(values[1:][valid_pairs] / values[:-1][valid_pairs]))
    deltas = deltas[deltas <= 6.0]
    return float(np.median(deltas)) if deltas.size else 0.0
