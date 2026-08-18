from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Sequence

import numpy as np


DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
ACTIVE_SPEECH_TARGET_DBFS = -22.0
PEAK_CEILING_DBFS = -2.0
SENTENCE_EDGE_GUARD_MS = 50
WINDOW_EDGE_GUARD_MS = 10


class MediaProcessingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioMetadata:
    duration_seconds: float


class FFmpegProcessor:
    def __init__(self, ffmpeg_executable: Path) -> None:
        self.ffmpeg_executable = ffmpeg_executable

    def health_check(self) -> str:
        result = self._run(["-version"])
        first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
        return first_line

    def probe(self, source: Path) -> AudioMetadata:
        result = subprocess.run(
            [str(self.ffmpeg_executable), "-hide_banner", "-i", str(source)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        diagnostic = result.stderr.decode("utf-8", errors="replace")
        match = DURATION_PATTERN.search(diagnostic)
        if match is None:
            raise MediaProcessingError(f"cannot determine audio duration: {diagnostic[-2000:]}")
        hours, minutes, seconds = match.groups()
        return AudioMetadata(int(hours) * 3600 + int(minutes) * 60 + float(seconds))

    def waveform(self, source: Path, points_per_second: int = 100) -> np.ndarray:
        sample_rate = max(100, int(points_per_second * 8))
        result = self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "f32le",
                "pipe:1",
            ]
        )
        samples = np.frombuffer(result.stdout, dtype="<f4")
        bucket = max(1, sample_rate // points_per_second)
        usable = samples[: samples.size - (samples.size % bucket)]
        if not usable.size:
            return np.zeros((0,), dtype=np.float32)
        return np.max(np.abs(usable.reshape(-1, bucket)), axis=1).astype(np.float32)

    def decode_mono_f32(
        self,
        source: Path,
        *,
        sample_rate: int = 16000,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> np.ndarray:
        """Decode a selection for local signal-quality analysis."""

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if start_seconds is not None and start_seconds < 0:
            raise ValueError("start_seconds cannot be negative")
        if end_seconds is not None:
            start = start_seconds or 0.0
            if end_seconds <= start:
                raise ValueError("end_seconds must be after start_seconds")
        arguments = ["-hide_banner", "-loglevel", "error"]
        if start_seconds is not None:
            arguments.extend(("-ss", f"{start_seconds:.6f}"))
        if end_seconds is not None:
            arguments.extend(("-t", f"{end_seconds - (start_seconds or 0.0):.6f}"))
        arguments.extend(
            (
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "f32le",
                "pipe:1",
            )
        )
        result = self._run(arguments)
        return np.frombuffer(result.stdout, dtype="<f4").copy()

    def prepare_reference(
        self,
        source: Path,
        destination: Path,
        start_seconds: float,
        end_seconds: float,
        *,
        sample_rate: int,
        channels: int,
        light_denoise: bool = False,
    ) -> AudioMetadata:
        if start_seconds < 0 or end_seconds <= start_seconds:
            raise ValueError("invalid audio selection")
        duration = end_seconds - start_seconds
        if not 3.0 <= duration <= 30.0:
            raise ValueError("reference selection must be between 3 and 30 seconds")
        destination.parent.mkdir(parents=True, exist_ok=True)
        filters = [
            "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-48dB",
            "areverse",
            "silenceremove=start_periods=1:start_duration=0.12:start_threshold=-48dB",
            "areverse",
        ]
        if light_denoise:
            filters.append("afftdn=nf=-30:tn=1")
        filters.append("loudnorm=I=-23:TP=-2:LRA=11")
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start_seconds:.6f}",
                "-t",
                f"{duration:.6f}",
                "-i",
                str(source),
                "-vn",
                "-af",
                ",".join(filters),
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-c:a",
                "pcm_s16le",
                str(destination),
            ]
        )
        return self.probe(destination)

    def time_stretch(
        self, source: Path, destination: Path, speed: float
    ) -> None:
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be in [0.5, 2.0]")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-filter:a",
                _rubberband_speech_filter(speed),
                str(destination),
            ]
        )

    def time_stretch_pcm(
        self,
        source: Path,
        destination: Path,
        *,
        speed: float,
        sample_rate: int,
        channels: int,
        sample_format: str,
    ) -> None:
        """High-quality, formant-preserving stretch for generated speech PCM."""

        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be in [0.5, 2.0]")
        if sample_format not in {"f32le", "s16le"}:
            raise ValueError(f"unsupported PCM format: {sample_format}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                sample_format,
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                str(source),
                "-filter:a",
                _rubberband_speech_filter(speed),
                "-f",
                sample_format,
                str(destination),
            ]
        )

    def pcm_to_wav(
        self,
        source: Path,
        destination: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
    ) -> None:
        """Wraps raw generated PCM in a WAV container for local alignment."""

        if sample_format not in {"f32le", "s16le"}:
            raise ValueError(f"unsupported PCM format: {sample_format}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                sample_format,
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                str(source),
                "-c:a",
                "pcm_s16le",
                str(destination),
            ]
        )

    def normalize_loudness_pcm(
        self,
        source: Path,
        destination: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
        target_lufs: float = -20.0,
    ) -> None:
        """Normalizes one fully generated SpeechUnit before it enters playback."""

        if sample_format not in {"f32le", "s16le"}:
            raise ValueError(f"unsupported PCM format: {sample_format}")
        if not -30.0 <= target_lufs <= -14.0:
            raise ValueError("target_lufs must be in [-30, -14]")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Do not let FFmpeg cut directly into the first or final phoneme.  The
        # old silenceremove chain left many real sentences with zero boundary
        # guard and a final sample around -24 dBFS, which is an audible click
        # when Reader switches to digital silence.  Boundary trimming, fades
        # and exact zero guards are performed conservatively on the completed
        # PCM below; internal long pauses are handled separately as well.
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                sample_format,
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                str(source),
                "-filter:a",
                f"loudnorm=I={target_lufs:.1f}:TP=-2.0:LRA=7.0",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-f",
                sample_format,
                str(destination),
            ]
        )
        _stabilize_active_speech_level(
            destination,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )
        _cap_internal_silence_pcm(
            destination,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )
        _condition_sentence_edges_pcm(
            destination,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    def normalize_window_loudness_pcm(
        self,
        source: Path,
        destination: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
        target_lufs: float = -20.0,
    ) -> None:
        """Normalizes one continuous GenerationWindow exactly once."""

        if sample_format not in {"f32le", "s16le"}:
            raise ValueError(f"unsupported PCM format: {sample_format}")
        if not -30.0 <= target_lufs <= -14.0:
            raise ValueError("target_lufs must be in [-30, -14]")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                sample_format,
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                str(source),
                "-filter:a",
                f"loudnorm=I={target_lufs:.1f}:TP=-2.0:LRA=7.0",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-f",
                sample_format,
                str(destination),
            ]
        )
        _stabilize_active_speech_level(
            destination,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )
        _condition_window_edges_pcm(
            destination,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    def measure_active_speech_seconds(
        self,
        source: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
    ) -> float:
        """Measures voiced 10 ms windows so timing ignores model-made pauses."""

        return _measure_active_speech_seconds(
            source,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    def cap_internal_silence_pcm(
        self,
        source: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
    ) -> float:
        """Caps long internal gaps in final playback PCM after time stretching."""

        return _cap_internal_silence_pcm(
            source,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    def condition_sentence_edges_pcm(
        self,
        source: Path,
        *,
        sample_rate: int,
        channels: int,
        sample_format: str,
    ) -> None:
        """Restores click-free guards after an optional time stretch."""

        _condition_sentence_edges_pcm(
            source,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=sample_format,
        )

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        if not self.ffmpeg_executable.is_file():
            raise MediaProcessingError(f"FFmpeg is missing: {self.ffmpeg_executable}")
        result = subprocess.run(
            [str(self.ffmpeg_executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise MediaProcessingError(result.stderr.decode("utf-8", errors="replace")[-4000:])
        return result


def _rubberband_speech_filter(speed: float) -> str:
    """Uses the bundled Rubber Band filter with conservative speech settings."""

    return (
        f"rubberband=tempo={speed:.6f}:pitch=1.0:transients=smooth:"
        "detector=soft:phase=laminar:window=long:smoothing=on:"
        "formant=preserved:pitchq=quality:channels=together"
    )


def _stabilize_active_speech_level(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
    target_dbfs: float = ACTIVE_SPEECH_TARGET_DBFS,
) -> None:
    """Makes very short and normal sentences equally near without compression."""

    if not path.is_file() or path.stat().st_size == 0:
        return
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    if raw.size < channels:
        return
    samples = raw.astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    usable = samples[: samples.size - samples.size % channels]
    frames = usable.reshape(-1, channels)
    window_frames = max(1, sample_rate // 100)
    window_count = frames.shape[0] // window_frames
    if window_count:
        windows = frames[: window_count * window_frames].reshape(
            window_count, window_frames, channels
        )
        levels = np.sqrt(
            np.mean(np.square(windows, dtype=np.float64), axis=(1, 2))
        )
        active = windows[levels >= 10 ** (-45.0 / 20.0)].reshape(-1)
    else:
        active = frames.reshape(-1)
    if active.size == 0:
        return
    active_rms = float(np.sqrt(np.mean(np.square(active, dtype=np.float64))))
    peak = float(np.max(np.abs(samples)))
    if active_rms <= 1e-9 or peak <= 1e-9:
        return
    gain = 10 ** (target_dbfs / 20.0) / active_rms
    peak_ceiling = 10 ** (PEAK_CEILING_DBFS / 20.0)
    gain = min(gain, peak_ceiling / peak)
    adjusted = np.clip(samples * gain, -1.0, 1.0)
    if sample_format == "f32le":
        adjusted.astype("<f4").tofile(path)
    else:
        np.rint(adjusted * 32767.0).astype("<i2").tofile(path)


def _measure_active_speech_seconds(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
) -> float:
    if not path.is_file() or path.stat().st_size == 0:
        return 0.0
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    if raw.size < channels:
        return 0.0
    samples = raw.astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    usable = samples[: samples.size - samples.size % channels]
    frames = usable.reshape(-1, channels)
    window_frames = max(1, sample_rate // 100)
    window_count = frames.shape[0] // window_frames
    if not window_count:
        return frames.shape[0] / sample_rate
    windows = frames[: window_count * window_frames].reshape(
        window_count, window_frames, channels
    )
    levels = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=(1, 2)))
    return float(np.count_nonzero(levels >= 10 ** (-45.0 / 20.0))) / 100.0


def _cap_internal_silence_pcm(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
    trigger_seconds: float = 0.36,
    keep_seconds: float = 0.18,
) -> float:
    """Removes only the middle of objectively silent, internal long gaps."""

    if not path.is_file() or path.stat().st_size == 0:
        return 0.0
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    if raw.size < channels:
        return 0.0
    samples = raw.astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    usable_sample_count = samples.size - samples.size % channels
    frames = samples[:usable_sample_count].reshape(-1, channels)
    window_frames = max(1, sample_rate // 100)
    window_count = frames.shape[0] // window_frames
    if window_count < 3:
        return 0.0
    analyzed = frames[: window_count * window_frames].reshape(
        window_count, window_frames, channels
    )
    levels = np.sqrt(np.mean(np.square(analyzed, dtype=np.float64), axis=(1, 2)))
    silent = levels < 10 ** (-45.0 / 20.0)
    active_indexes = np.flatnonzero(~silent)
    if active_indexes.size < 2:
        return 0.0
    padded = np.pad(silent.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    runs = zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1))
    trigger_windows = max(1, int(round(trigger_seconds * 100)))
    keep_windows = max(1, int(round(keep_seconds * 100)))
    first_active = int(active_indexes[0])
    last_active = int(active_indexes[-1])
    keep_mask = np.ones(frames.shape[0], dtype=bool)
    removed_frames = 0
    for start_window, end_window in runs:
        run_windows = int(end_window - start_window)
        if (
            start_window <= first_active
            or end_window > last_active
            or run_windows < trigger_windows
            or run_windows <= keep_windows
        ):
            continue
        keep_before = keep_windows // 2
        keep_after = keep_windows - keep_before
        remove_start = (start_window + keep_before) * window_frames
        remove_end = (end_window - keep_after) * window_frames
        keep_mask[remove_start:remove_end] = False
        removed_frames += remove_end - remove_start
    if removed_frames <= 0:
        return 0.0
    compacted = frames[keep_mask].reshape(-1)
    if sample_format == "f32le":
        compacted.astype("<f4").tofile(path)
    else:
        np.rint(np.clip(compacted, -1.0, 1.0) * 32767.0).astype("<i2").tofile(path)
    return removed_frames / sample_rate


def _condition_sentence_edges_pcm(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
    guard_ms: int = SENTENCE_EDGE_GUARD_MS,
) -> None:
    """Trims only outer quiet regions, then adds zero guards and short fades.

    The active-window threshold is intentionally lower than the internal-pause
    threshold and two 10 ms windows are retained around detected speech.  This
    protects low-energy initial consonants while preventing codec padding from
    becoming part of every Reader pause.
    """

    if not path.is_file() or path.stat().st_size == 0:
        return
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    if raw.size < channels:
        return
    samples = raw.astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    usable_sample_count = samples.size - samples.size % channels
    frames = samples[:usable_sample_count].reshape(-1, channels)
    nonzero_frames = np.flatnonzero(np.max(np.abs(frames), axis=1) > 1e-8)
    if nonzero_frames.size:
        frames = frames[int(nonzero_frames[0]) : int(nonzero_frames[-1]) + 1]
    window_frames = max(1, sample_rate // 100)
    window_count = frames.shape[0] // window_frames
    if window_count:
        windows = frames[: window_count * window_frames].reshape(
            window_count, window_frames, channels
        )
        levels = np.sqrt(
            np.mean(np.square(windows, dtype=np.float64), axis=(1, 2))
        )
        active = np.flatnonzero(levels >= 10 ** (-55.0 / 20.0))
    else:
        active = np.asarray([], dtype=np.int64)
    if active.size:
        margin_windows = 2
        start_window = max(0, int(active[0]) - margin_windows)
        end_window = min(window_count, int(active[-1]) + 1 + margin_windows)
        start_frame = start_window * window_frames
        end_frame = (
            frames.shape[0]
            if end_window >= window_count
            else end_window * window_frames
        )
        body = np.asarray(frames[start_frame:end_frame], dtype=np.float32).copy()
    else:
        body = np.asarray(frames, dtype=np.float32).copy()
    if body.size == 0:
        return
    fade_frames = min(max(1, int(round(sample_rate * 0.006))), body.shape[0] // 2)
    if fade_frames:
        phase = np.linspace(0.0, np.pi / 2.0, fade_frames, dtype=np.float32)
        fade_in = np.square(np.sin(phase), dtype=np.float32)
        body[:fade_frames] *= fade_in[:, None]
        body[-fade_frames:] *= fade_in[::-1, None]
    guard_frames = max(1, int(round(sample_rate * guard_ms / 1000.0)))
    guard = np.zeros((guard_frames, channels), dtype=np.float32)
    conditioned = np.concatenate((guard, body, guard), axis=0).reshape(-1)
    if sample_format == "f32le":
        conditioned.astype("<f4").tofile(path)
    else:
        np.rint(np.clip(conditioned, -1.0, 1.0) * 32767.0).astype("<i2").tofile(path)


def _condition_window_edges_pcm(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
    guard_ms: int = WINDOW_EDGE_GUARD_MS,
) -> None:
    """Protects only the outer edge of a whole window, never every sentence."""

    if not path.is_file() or path.stat().st_size == 0:
        return
    dtype = "<f4" if sample_format == "f32le" else "<i2"
    raw = np.fromfile(path, dtype=dtype)
    if raw.size < channels:
        return
    samples = raw.astype(np.float32)
    if sample_format == "s16le":
        samples /= 32768.0
    usable = samples[: samples.size - samples.size % channels]
    frames = usable.reshape(-1, channels)
    active = np.flatnonzero(np.max(np.abs(frames), axis=1) > 1e-8)
    if active.size == 0:
        return
    fade_frames = max(1, int(round(sample_rate * 0.003)))
    first_active = int(active[0])
    last_active = int(active[-1])
    fade_in_count = min(fade_frames, frames.shape[0] - first_active)
    fade_out_count = min(fade_frames, last_active + 1)
    if fade_in_count:
        phase = np.linspace(0.0, np.pi / 2.0, fade_in_count, dtype=np.float32)
        frames[first_active : first_active + fade_in_count] *= np.square(
            np.sin(phase), dtype=np.float32
        )[:, None]
    if fade_out_count:
        phase = np.linspace(np.pi / 2.0, 0.0, fade_out_count, dtype=np.float32)
        frames[last_active - fade_out_count + 1 : last_active + 1] *= np.square(
            np.sin(phase), dtype=np.float32
        )[:, None]
    guard_frames = max(1, int(round(sample_rate * guard_ms / 1000.0)))
    leading = first_active
    trailing = frames.shape[0] - 1 - last_active
    prefix = np.zeros((max(0, guard_frames - leading), channels), dtype=np.float32)
    suffix = np.zeros((max(0, guard_frames - trailing), channels), dtype=np.float32)
    conditioned = np.concatenate((prefix, frames, suffix), axis=0).reshape(-1)
    if sample_format == "f32le":
        conditioned.astype("<f4").tofile(path)
    else:
        np.rint(np.clip(conditioned, -1.0, 1.0) * 32767.0).astype("<i2").tofile(path)
