from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from sectvoice.domain import PCMFormat


@dataclass(frozen=True, slots=True)
class UnitFrameRange:
    unit_index: int
    start_frame: int
    end_frame: int
    boundary_confidence: str = "exact"

    def __post_init__(self) -> None:
        if self.unit_index < 0:
            raise ValueError("unit_index cannot be negative")
        if self.start_frame < 0 or self.end_frame < self.start_frame:
            raise ValueError("invalid PCM frame range")


@dataclass(frozen=True, slots=True)
class WindowAudioLayout:
    total_frames: int
    unit_ranges: tuple[UnitFrameRange, ...]
    unit_text_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.total_frames <= 0:
            raise ValueError("window PCM must contain frames")
        if not self.unit_ranges:
            raise ValueError("window layout needs at least one unit")
        if len(self.unit_ranges) != len(self.unit_text_sha256):
            raise ValueError("text fingerprints do not match unit ranges")
        previous_end = 0
        for index, item in enumerate(self.unit_ranges):
            if item.unit_index != index:
                raise ValueError("window unit indexes must be contiguous")
            if item.start_frame < previous_end or item.end_frame > self.total_frames:
                raise ValueError("window PCM ranges overlap or exceed the source")
            previous_end = item.end_frame

    def to_metadata(self) -> dict[str, Any]:
        return {
            "total_frames": self.total_frames,
            "unit_text_sha256": list(self.unit_text_sha256),
            "unit_ranges": [
                {
                    "unit_index": item.unit_index,
                    "start_frame": item.start_frame,
                    "end_frame": item.end_frame,
                    "boundary_confidence": item.boundary_confidence,
                }
                for item in self.unit_ranges
            ],
        }

    @classmethod
    def from_metadata(
        cls,
        metadata: dict[str, Any],
        *,
        expected_texts: Sequence[str] | None = None,
        expected_fingerprints: Sequence[str] | None = None,
    ) -> "WindowAudioLayout":
        layout = cls(
            total_frames=int(metadata["total_frames"]),
            unit_text_sha256=tuple(str(item) for item in metadata["unit_text_sha256"]),
            unit_ranges=tuple(
                UnitFrameRange(
                    unit_index=int(item["unit_index"]),
                    start_frame=int(item["start_frame"]),
                    end_frame=int(item["end_frame"]),
                    boundary_confidence=str(item.get("boundary_confidence") or "unknown"),
                )
                for item in metadata["unit_ranges"]
            ),
        )
        if expected_texts is not None and expected_fingerprints is not None:
            raise ValueError("provide expected_texts or expected_fingerprints, not both")
        expected = (
            _text_fingerprints(expected_texts)
            if expected_texts is not None
            else tuple(expected_fingerprints or ())
        )
        if expected and layout.unit_text_sha256 != expected:
            raise ValueError("cached window unit boundaries do not match current texts")
        return layout


def locate_window_unit_frames(
    path: Path,
    pcm: PCMFormat,
    unit_texts: Sequence[str],
    *,
    timing_mode: str = "voiced",
) -> WindowAudioLayout:
    """Partitions one continuous PCM window without deleting audible frames.

    Natural sentence punctuation normally creates a low-energy run.  The
    detector chooses the run nearest the text-duration prediction.  If an
    engine produces no trustworthy quiet run, it falls back to a local
    low-energy cut but removes no model audio at that boundary.
    """

    if not unit_texts:
        raise ValueError("unit_texts cannot be empty")
    if timing_mode not in {"voiced", "hybrid"}:
        raise ValueError(f"unsupported window timing mode: {timing_mode}")
    frames = _read_frames(path, pcm)
    total_frames = int(frames.shape[0])
    if total_frames <= 0:
        raise ValueError("window PCM is empty")
    if len(unit_texts) == 1:
        return WindowAudioLayout(
            total_frames,
            (UnitFrameRange(0, 0, total_frames),),
            _text_fingerprints(unit_texts),
        )

    window_frames = max(1, pcm.sample_rate // 100)
    window_count = total_frames // window_frames
    if window_count <= len(unit_texts):
        return _proportional_layout(total_frames, unit_texts)
    analyzed = frames[: window_count * window_frames].reshape(
        window_count, window_frames, pcm.channels
    )
    levels = np.sqrt(np.mean(np.square(analyzed, dtype=np.float64), axis=(1, 2)))
    quiet = levels <= 10 ** (-40.0 / 20.0)
    # A stricter threshold is used only for proportional timing.  Low-level
    # breath and room noise inside a long model pause must not count as spoken
    # progress, while the more permissive threshold above still locates the
    # actual quiet edges precisely.
    voiced_for_timing = levels >= 10 ** (-36.0 / 20.0)
    # A voiced syllable can contain 30-80 ms of very low energy.  Treating such
    # a phonetic closure as a sentence boundary can insert Reader's pause in
    # the middle of a word (for example, between "拿" and "出来").  Basic's
    # generated full-stop gaps are materially longer, so only accept a
    # sustained quiet region here.  Merge only tiny analysis flicker; joining
    # across 100 ms of audible speech can manufacture a false long boundary.
    minimum_boundary_windows = max(3, int(np.ceil(0.12 * 100)))
    runs = _merge_quiet_runs(
        _quiet_runs(quiet, minimum_windows=minimum_boundary_windows),
        maximum_gap_windows=2,
    )
    weights = np.asarray([_speech_weight(text) for text in unit_texts], dtype=np.float64)
    total_weight = float(np.sum(weights))

    boundaries: list[tuple[int, int, str]] = []
    previous_end = 0
    for boundary_index in range(len(unit_texts) - 1):
        expected_ratio = float(np.sum(weights[: boundary_index + 1])) / total_weight
        # Sentence boundaries must be estimated on audible time, not file
        # time.  Some engines insert multi-second pauses after short clauses;
        # counting those pauses shifts every later proportional boundary and
        # can assign the next spoken sentence to the preceding SpeechUnit.
        voiced_expected_frame = _frame_for_active_ratio(
            voiced_for_timing,
            expected_ratio=expected_ratio,
            window_frames=window_frames,
            total_frames=total_frames,
        )
        if timing_mode == "hybrid":
            file_time_expected_frame = int(round(total_frames * expected_ratio))
            expected_frame = int(
                round((voiced_expected_frame + file_time_expected_frame) / 2)
            )
        else:
            expected_frame = voiced_expected_frame
        minimum_frame = previous_end + max(1, int(pcm.sample_rate * 0.05))
        remaining_units = len(unit_texts) - boundary_index - 1
        maximum_frame = total_frames - remaining_units * max(
            1, int(pcm.sample_rate * 0.05)
        )
        candidate = _nearest_quiet_run(
            runs,
            expected_frame=expected_frame,
            minimum_frame=minimum_frame,
            maximum_frame=maximum_frame,
            window_frames=window_frames,
            sample_rate=pcm.sample_rate,
            total_frames=total_frames,
        )
        if candidate is None:
            cut = _local_low_energy_cut(
                levels,
                expected_frame=expected_frame,
                minimum_frame=minimum_frame,
                maximum_frame=maximum_frame,
                window_frames=window_frames,
                sample_rate=pcm.sample_rate,
            )
            boundaries.append((cut, cut, "estimated"))
            previous_end = cut
        else:
            start_window, end_window = candidate
            gap_start = start_window * window_frames
            gap_end = min(total_frames, end_window * window_frames)
            boundaries.append((gap_start, gap_end, "quiet-run"))
            previous_end = gap_end

    # Retain a tiny piece of the model's own quiet run on both sides.  The
    # middle of a long model pause is still replaced by Reader's configured
    # pause, but the retained quiet protects the preceding release and the
    # following onset from hard PCM cuts.  Nothing synthetic is inserted.
    boundary_guard_frames = max(1, int(round(pcm.sample_rate * 0.01)))
    ranges: list[UnitFrameRange] = []
    start_frame = 0
    for index, (gap_start, gap_end, confidence) in enumerate(boundaries):
        quiet_run_frames = max(0, gap_end - gap_start)
        retained_guard = min(boundary_guard_frames, quiet_run_frames // 2)
        end_frame = max(start_frame, gap_start + retained_guard)
        ranges.append(UnitFrameRange(index, start_frame, end_frame, confidence))
        start_frame = max(end_frame, gap_end - retained_guard)
    ranges.append(UnitFrameRange(len(unit_texts) - 1, start_frame, total_frames))
    return WindowAudioLayout(
        total_frames,
        tuple(ranges),
        _text_fingerprints(unit_texts),
    )


def locate_window_unit_frames_from_words(
    *,
    total_frames: int,
    sample_rate: int,
    unit_texts: Sequence[str],
    words: Sequence[dict[str, object]],
) -> WindowAudioLayout:
    """Builds loss-safe SpeechUnit ranges from local ASR word timestamps.

    The transcription text is used only for temporal alignment.  Reader text
    remains authoritative and is never replaced by ASR output.
    """

    if total_frames <= 0 or sample_rate <= 0:
        raise ValueError("invalid aligned window PCM format")
    if not unit_texts:
        raise ValueError("unit_texts cannot be empty")

    source_units = tuple(_spoken_characters(text) for text in unit_texts)
    source_text = "".join(source_units)
    timed_characters: list[tuple[str, float, float]] = []
    for word in words:
        characters = _spoken_characters(str(word.get("text") or ""))
        if not characters:
            continue
        start = max(0.0, float(word.get("start") or 0.0))
        end = max(start, float(word.get("end") or start))
        step = (end - start) / len(characters)
        for index, character in enumerate(characters):
            timed_characters.append(
                (character, start + step * index, start + step * (index + 1))
            )
    recognized_text = "".join(item[0] for item in timed_characters)
    if not recognized_text:
        raise ValueError("window alignment returned no timed speech")
    length_ratio = len(recognized_text) / max(1, len(source_text))
    if not 0.8 <= length_ratio <= 1.25:
        raise ValueError("window alignment transcript length is not trustworthy")
    mapping = _align_character_indexes(source_text, recognized_text)
    mapped_count = len(mapping)
    if mapped_count < max(1, int(round(min(len(source_text), len(recognized_text)) * 0.85))):
        raise ValueError("window alignment coverage is too low")
    mapped_source_indexes = set(mapping)
    if source_text and (
        0 not in mapped_source_indexes or len(source_text) - 1 not in mapped_source_indexes
    ):
        raise ValueError("window alignment indicates the start or ending may be incomplete")
    if _longest_missing_run(mapped_source_indexes, len(source_text)) >= 2:
        raise ValueError("window alignment indicates consecutive words may be incomplete")

    unit_source_start = 0
    for source_unit in source_units:
        unit_source_end = unit_source_start + len(source_unit)
        mapped_in_unit = sum(
            unit_source_start <= source_index < unit_source_end
            for source_index in mapping
        )
        required_ratio = 0.75 if len(source_unit) <= 4 else 0.6
        required = max(1, int(np.ceil(len(source_unit) * required_ratio)))
        if mapped_in_unit < required:
            raise ValueError(
                "window alignment indicates a SpeechUnit may be incomplete"
            )
        unit_source_start = unit_source_end

    if len(unit_texts) == 1:
        return WindowAudioLayout(
            total_frames,
            (UnitFrameRange(0, 0, total_frames, "asr-validated"),),
            _text_fingerprints(unit_texts),
        )

    boundaries: list[tuple[int, int, str]] = []
    source_boundary = 0
    for unit_index in range(len(source_units) - 1):
        source_boundary += len(source_units[unit_index])
        previous_indexes = [
            recognized_index
            for source_index, recognized_index in mapping.items()
            if source_index < source_boundary
        ]
        following_indexes = [
            recognized_index
            for source_index, recognized_index in mapping.items()
            if source_index >= source_boundary
        ]
        if not previous_indexes or not following_indexes:
            raise ValueError("window alignment cannot locate every SpeechUnit boundary")
        previous_recognized = max(previous_indexes)
        following_recognized = min(following_indexes)
        if following_recognized <= previous_recognized:
            raise ValueError("window alignment produced an unordered boundary")
        previous_end = timed_characters[previous_recognized][2]
        following_start = timed_characters[following_recognized][1]
        if following_start < previous_end:
            cut_seconds = max(0.0, (previous_end + following_start) / 2)
            gap_start = gap_end = int(round(cut_seconds * sample_rate))
        else:
            gap_start = int(round(previous_end * sample_rate))
            gap_end = int(round(following_start * sample_rate))
        gap_start = max(0, min(total_frames, gap_start))
        gap_end = max(gap_start, min(total_frames, gap_end))
        boundaries.append((gap_start, gap_end, "asr-aligned"))

    return _layout_from_boundaries(
        total_frames=total_frames,
        sample_rate=sample_rate,
        unit_texts=unit_texts,
        boundaries=boundaries,
    )


def _layout_from_boundaries(
    *,
    total_frames: int,
    sample_rate: int,
    unit_texts: Sequence[str],
    boundaries: Sequence[tuple[int, int, str]],
) -> WindowAudioLayout:
    if len(boundaries) != len(unit_texts) - 1:
        raise ValueError("boundary count does not match SpeechUnits")
    # ASR timestamps normally stop at the lexical end and can exclude the
    # low-energy release of the final phoneme.  Keep 50 ms from each side of
    # an available model pause; this does not overlap units or synthesize a
    # fade, but prevents a short final character from sounding clipped.
    boundary_guard_frames = max(1, int(round(sample_rate * 0.05)))
    ranges: list[UnitFrameRange] = []
    start_frame = 0
    for index, (raw_gap_start, raw_gap_end, confidence) in enumerate(boundaries):
        gap_start = max(start_frame, min(total_frames, raw_gap_start))
        gap_end = max(gap_start, min(total_frames, raw_gap_end))
        quiet_run_frames = gap_end - gap_start
        retained_guard = min(boundary_guard_frames, quiet_run_frames // 2)
        end_frame = max(start_frame, gap_start + retained_guard)
        ranges.append(UnitFrameRange(index, start_frame, end_frame, confidence))
        start_frame = max(end_frame, gap_end - retained_guard)
    ranges.append(
        UnitFrameRange(len(unit_texts) - 1, start_frame, total_frames, "exact")
    )
    return WindowAudioLayout(
        total_frames,
        tuple(ranges),
        _text_fingerprints(unit_texts),
    )


def _spoken_characters(text: str) -> str:
    return "".join(character.casefold() for character in text if character.isalnum())


def _align_character_indexes(source: str, recognized: str) -> dict[int, int]:
    """Levenshtein alignment that keeps substitutions as temporal matches."""

    source_count = len(source)
    recognized_count = len(recognized)
    costs = np.empty((source_count + 1, recognized_count + 1), dtype=np.int16)
    parents = np.zeros((source_count + 1, recognized_count + 1), dtype=np.uint8)
    costs[:, 0] = np.arange(source_count + 1, dtype=np.int16)
    costs[0, :] = np.arange(recognized_count + 1, dtype=np.int16)
    parents[1:, 0] = 1  # delete source character
    parents[0, 1:] = 2  # insert recognized character
    for source_index in range(1, source_count + 1):
        for recognized_index in range(1, recognized_count + 1):
            diagonal = costs[source_index - 1, recognized_index - 1] + (
                source[source_index - 1] != recognized[recognized_index - 1]
            )
            deletion = costs[source_index - 1, recognized_index] + 1
            insertion = costs[source_index, recognized_index - 1] + 1
            best = min(diagonal, deletion, insertion)
            costs[source_index, recognized_index] = best
            # Prefer a diagonal substitution on ties because Chinese ASR
            # homophones normally preserve character timing and length.
            parents[source_index, recognized_index] = (
                0 if diagonal == best else 1 if deletion == best else 2
            )
    mapping: dict[int, int] = {}
    source_index = source_count
    recognized_index = recognized_count
    while source_index > 0 or recognized_index > 0:
        direction = int(parents[source_index, recognized_index])
        if source_index > 0 and recognized_index > 0 and direction == 0:
            source_index -= 1
            recognized_index -= 1
            mapping[source_index] = recognized_index
        elif source_index > 0 and (recognized_index == 0 or direction == 1):
            source_index -= 1
        else:
            recognized_index -= 1
    return mapping


def _longest_missing_run(mapped_indexes: set[int], source_count: int) -> int:
    longest = 0
    current = 0
    for index in range(source_count):
        if index in mapped_indexes:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def scale_window_layout(
    layout: WindowAudioLayout,
    *,
    new_total_frames: int,
) -> WindowAudioLayout:
    """Scales relative ranges after one uniform whole-window time stretch."""

    if new_total_frames <= 0:
        raise ValueError("new_total_frames must be positive")
    ratio = new_total_frames / layout.total_frames
    ranges: list[UnitFrameRange] = []
    previous_end = 0
    for index, item in enumerate(layout.unit_ranges):
        start = max(previous_end, int(round(item.start_frame * ratio)))
        end = max(start, int(round(item.end_frame * ratio)))
        if (
            index == len(layout.unit_ranges) - 1
            and item.end_frame == layout.total_frames
        ):
            end = new_total_frames
        end = min(new_total_frames, end)
        ranges.append(
            UnitFrameRange(item.unit_index, start, end, item.boundary_confidence)
        )
        previous_end = end
    return WindowAudioLayout(new_total_frames, tuple(ranges), layout.unit_text_sha256)


def balance_window_unit_levels_pcm(
    path: Path,
    pcm: PCMFormat,
    layout: WindowAudioLayout,
    *,
    target_dbfs: float = -22.0,
    maximum_gain_db: float = 2.5,
    maximum_attenuation_db: float | None = None,
    peak_ceiling_dbfs: float = -2.0,
) -> tuple[dict[str, float], ...]:
    """Applies bounded constant gains inside one globally normalized window.

    This is not a second LUFS pass, compression, onset shaping, or a separate
    sentence effect. It removes only gross near/far level drift between the
    already mapped ranges while preserving every range's internal dynamics.
    """

    if maximum_gain_db < 0:
        raise ValueError("maximum_gain_db cannot be negative")
    attenuation_limit = (
        maximum_gain_db
        if maximum_attenuation_db is None
        else maximum_attenuation_db
    )
    if attenuation_limit < 0:
        raise ValueError("maximum_attenuation_db cannot be negative")
    dtype = "<f4" if pcm.sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    usable_count = raw.size - raw.size % pcm.channels
    if usable_count <= 0:
        return ()
    samples = raw[:usable_count].reshape(-1, pcm.channels).astype(np.float32)
    if pcm.sample_format == "s16le":
        samples /= 32768.0
    results: list[dict[str, float]] = []
    target_rms = 10 ** (target_dbfs / 20.0)
    peak_ceiling = 10 ** (peak_ceiling_dbfs / 20.0)
    for frame_range in layout.unit_ranges:
        segment = samples[frame_range.start_frame : frame_range.end_frame]
        active_rms = _active_rms(segment, pcm.sample_rate)
        peak = float(np.max(np.abs(segment))) if segment.size else 0.0
        if active_rms <= 1e-9 or peak <= 1e-9:
            gain_db = 0.0
            gain = 1.0
        else:
            gain_db = 20.0 * np.log10(target_rms / active_rms)
            gain_db = float(
                np.clip(gain_db, -attenuation_limit, maximum_gain_db)
            )
            gain = 10 ** (gain_db / 20.0)
            if peak * gain > peak_ceiling:
                gain = peak_ceiling / peak
                gain_db = 20.0 * np.log10(max(gain, 1e-12))
            segment *= gain
        results.append(
            {
                "before_active_rms_dbfs": float(
                    20.0 * np.log10(max(active_rms, 1e-12))
                ),
                "gain_db": float(gain_db),
                "after_active_rms_dbfs": float(
                    20.0 * np.log10(max(active_rms * gain, 1e-12))
                ),
            }
        )
    flattened = np.clip(samples, -1.0, 1.0).reshape(-1)
    if pcm.sample_format == "f32le":
        flattened.astype("<f4").tofile(path)
    else:
        np.rint(flattened * 32767.0).astype("<i2").tofile(path)
    return tuple(results)


def _read_frames(path: Path, pcm: PCMFormat) -> np.ndarray:
    dtype = "<f4" if pcm.sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    usable = raw[: raw.size - raw.size % pcm.channels]
    frames = usable.reshape(-1, pcm.channels).astype(np.float32)
    if pcm.sample_format == "s16le":
        frames /= 32768.0
    return frames


def _active_rms(frames: np.ndarray, sample_rate: int) -> float:
    if frames.size == 0:
        return 0.0
    window_frames = max(1, sample_rate // 100)
    window_count = frames.shape[0] // window_frames
    if window_count:
        windows = frames[: window_count * window_frames].reshape(
            window_count, window_frames, frames.shape[1]
        )
        levels = np.sqrt(
            np.mean(np.square(windows, dtype=np.float64), axis=(1, 2))
        )
        active = windows[levels >= 10 ** (-45.0 / 20.0)].reshape(-1)
    else:
        active = frames.reshape(-1)
    if active.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(active, dtype=np.float64))))


def _quiet_runs(quiet: np.ndarray, *, minimum_windows: int) -> list[tuple[int, int]]:
    padded = np.pad(quiet.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    return [
        (int(start), int(end))
        for start, end in zip(
            np.flatnonzero(changes == 1),
            np.flatnonzero(changes == -1),
            strict=True,
        )
        if int(end - start) >= minimum_windows
    ]


def _merge_quiet_runs(
    runs: Sequence[tuple[int, int]], *, maximum_gap_windows: int
) -> list[tuple[int, int]]:
    if not runs:
        return []
    merged = [runs[0]]
    for start, end in runs[1:]:
        previous_start, previous_end = merged[-1]
        if start - previous_end <= maximum_gap_windows:
            merged[-1] = (previous_start, end)
        else:
            merged.append((start, end))
    return merged


def _frame_for_active_ratio(
    active_windows: np.ndarray,
    *,
    expected_ratio: float,
    window_frames: int,
    total_frames: int,
) -> int:
    active_indexes = np.flatnonzero(active_windows)
    if active_indexes.size < 2:
        return int(round(total_frames * expected_ratio))
    target = int(round((active_indexes.size - 1) * expected_ratio))
    target = max(0, min(active_indexes.size - 1, target))
    return min(
        total_frames,
        int(active_indexes[target]) * window_frames + window_frames // 2,
    )


def _nearest_quiet_run(
    runs: Sequence[tuple[int, int]],
    *,
    expected_frame: int,
    minimum_frame: int,
    maximum_frame: int,
    window_frames: int,
    sample_rate: int,
    total_frames: int,
) -> tuple[int, int] | None:
    search_radius = max(int(sample_rate * 1.25), int(total_frames * 0.16))
    maximum_boundary_lead = int(sample_rate * 0.12)
    candidates: list[tuple[float, tuple[int, int]]] = []
    for start, end in runs:
        start_frame = start * window_frames
        end_frame = end * window_frames
        center = (start_frame + end_frame) // 2
        if center < minimum_frame or center > maximum_frame:
            continue
        # Voiceless consonants can resemble a short quiet run.  A run that
        # begins materially after the voiced-time prediction is therefore not
        # allowed to erase a later word onset; the non-destructive local cut
        # is safer in that case.
        duration = end_frame - start_frame
        if (
            duration < int(sample_rate * 0.5)
            and start_frame > expected_frame + maximum_boundary_lead
        ):
            continue
        if expected_frame < start_frame:
            distance = start_frame - expected_frame
        elif expected_frame > end_frame:
            distance = expected_frame - end_frame
        else:
            distance = 0
        if distance > search_radius:
            continue
        score = float(distance) - min(duration, sample_rate // 2) * 0.15
        candidates.append((score, (start, end)))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _local_low_energy_cut(
    levels: np.ndarray,
    *,
    expected_frame: int,
    minimum_frame: int,
    maximum_frame: int,
    window_frames: int,
    sample_rate: int,
) -> int:
    radius_frames = int(sample_rate * 0.45)
    lower = max(minimum_frame, expected_frame - radius_frames)
    upper = min(maximum_frame, expected_frame + radius_frames)
    lower_window = max(0, lower // window_frames)
    upper_window = min(levels.size, max(lower_window + 1, upper // window_frames + 1))
    local = levels[lower_window:upper_window]
    if local.size == 0:
        return max(minimum_frame, min(maximum_frame, expected_frame))
    # Small distance penalty prevents a remote quiet phoneme from beating the
    # natural boundary merely by a fraction of a decibel.
    indexes = np.arange(lower_window, upper_window)
    distance_penalty = np.abs(indexes * window_frames - expected_frame) / max(
        1, radius_frames
    )
    score = np.log10(np.maximum(local, 1e-8)) + distance_penalty * 0.12
    selected_window = int(indexes[int(np.argmin(score))])
    return max(
        minimum_frame,
        min(maximum_frame, selected_window * window_frames + window_frames // 2),
    )


def _speech_weight(text: str) -> float:
    spoken = sum(character.isalnum() for character in text)
    punctuation = sum(0.8 if character in "；;" else 0.5 for character in text if character in "，,、：:；;")
    # Very short utterances still have a syllable onset and release; treating
    # one character as one fifth of a five-character phrase biases the first
    # boundary too early.  The small floor affects only extreme-short units.
    return max(2.2, float(spoken) + punctuation)


def _text_fingerprints(texts: Sequence[str]) -> tuple[str, ...]:
    return tuple(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)


def _proportional_layout(
    total_frames: int, unit_texts: Sequence[str]
) -> WindowAudioLayout:
    weights = np.asarray([_speech_weight(text) for text in unit_texts], dtype=np.float64)
    cumulative = np.cumsum(weights) / float(np.sum(weights))
    ranges: list[UnitFrameRange] = []
    start = 0
    for index in range(len(unit_texts)):
        end = total_frames if index == len(unit_texts) - 1 else int(round(total_frames * cumulative[index]))
        end = max(start, min(total_frames, end))
        ranges.append(UnitFrameRange(index, start, end, "proportional"))
        start = end
    return WindowAudioLayout(total_frames, tuple(ranges), _text_fingerprints(unit_texts))
