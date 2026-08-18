from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Event
from typing import Callable

import numpy as np

from onnx_tts_runtime import (
    OnnxTtsRuntime,
)


ENGINE_ID = "moss-nano-onnx"
ENGINE_VERSION = "cc7bdf19"
PAYLOAD_FORMAT_VERSION = "moss-prompt-codes-v1"


class SynthesisCancelled(RuntimeError):
    pass


def _retry_seed(
    base_seed: int,
    text: str,
    attempt: int,
    *,
    stable_first_seed: bool = False,
) -> int:
    # Matching-language Chinese narration may deliberately keep one repeatable
    # first path for a VoiceProfile; this removed a proven non-punctuation pause
    # between adjacent sentences. Cross-language voices stay text-specific: the
    # same voice-wide strategy produced 0.5-1.0 second elongations or dropped
    # words there. All fallback paths remain text-specific in both modes.
    if attempt == 0 and stable_first_seed:
        return int(base_seed) & 0x7FFFFFFF
    if attempt == 0:
        digest = hashlib.sha256(
            f"{base_seed}\0{text}\0{attempt + 1}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    digest = hashlib.sha256(
        f"{base_seed}\0{text}\0fallback-{attempt}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class MossNanoEngine:
    """SectVoice's ONNX-only adapter around the pinned upstream runtime."""

    def __init__(
        self,
        model_dir: Path,
        output_dir: Path,
        cpu_threads: int = 4,
        max_new_frames: int = 375,
    ) -> None:
        self.model_dir = model_dir.resolve()
        self.output_dir = output_dir.resolve()
        self.cpu_threads = max(1, int(cpu_threads))
        self.max_new_frames = max(1, int(max_new_frames))
        self.runtime: OnnxTtsRuntime | None = None

    def load(self) -> None:
        if self.runtime is not None:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = OnnxTtsRuntime(
            model_dir=self.model_dir,
            thread_count=self.cpu_threads,
            max_new_frames=self.max_new_frames,
            do_sample=True,
            sample_mode="fixed",
            execution_provider="cpu",
            output_dir=self.output_dir,
        )

    def unload(self) -> None:
        self.runtime = None

    def compile_voice(self, reference_wav: Path, destination: Path) -> dict[str, object]:
        runtime = self._require_runtime()
        destination.mkdir(parents=True, exist_ok=True)
        prompt_audio_codes = runtime.encode_reference_audio(reference_wav)
        private_payload = {
            "payload_format_version": PAYLOAD_FORMAT_VERSION,
            "engine_id": ENGINE_ID,
            "engine_version": ENGINE_VERSION,
            "prompt_audio_codes": prompt_audio_codes,
        }
        serialized = json.dumps(
            private_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        target = destination / "engine-payload.json"
        temporary = destination / "engine-payload.json.partial"
        temporary.write_bytes(serialized)
        os.replace(temporary, target)
        return {
            "opaque_path": str(destination),
            "sha256": hashlib.sha256(serialized).hexdigest(),
            "payload_format_version": PAYLOAD_FORMAT_VERSION,
            "prompt_frames": len(prompt_audio_codes),
            "byte_size": len(serialized),
        }

    def read_private_payload(self, payload_dir: Path) -> list[list[int]]:
        payload_path = payload_dir / "engine-payload.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if payload.get("payload_format_version") != PAYLOAD_FORMAT_VERSION:
            raise RuntimeError("Basic voice payload format is incompatible")
        if payload.get("engine_id") != ENGINE_ID:
            raise RuntimeError("Basic voice payload belongs to another engine")
        rows = payload.get("prompt_audio_codes")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Basic voice payload has no prompt audio codes")
        return [[int(value) for value in row] for row in rows]

    def synthesize_stream(
        self,
        *,
        text: str,
        payload_dir: Path,
        emit: Callable[[bytes, int, int, float, bool], None],
        cancel_event: Event | None = None,
        max_text_tokens: int = 75,
        seed: int | None = None,
        stable_first_seed: bool = False,
    ) -> dict[str, object]:
        runtime = self._require_runtime()
        if not text.strip():
            raise ValueError("text cannot be empty")
        prompt_audio_codes = self.read_private_payload(payload_dir)
        if seed is not None:
            runtime.rng = np.random.default_rng(seed)
        prepared = runtime.prepare_synthesis_text(
            text=text,
            enable_wetext=False,
            enable_normalize_tts_text=True,
        )
        chunks = runtime.split_voice_clone_text(
            str(prepared["text"]), max_tokens=max(1, int(max_text_tokens))
        )
        sample_rate = int(runtime.codec_meta["codec_config"]["sample_rate"])
        channels = int(runtime.codec_meta["codec_config"]["channels"])
        emitted_samples_total = 0
        generated_frames_total = 0
        retry_counts: list[int] = []
        original_max_frames = int(runtime.manifest["generation_defaults"]["max_new_frames"])
        base_seed = int(seed) if seed is not None else int(runtime.rng.integers(0, 2**31 - 1))
        try:
            for text_chunk_index, text_chunk in enumerate(chunks):
                self._raise_if_cancelled(cancel_event)
                text_token_ids = runtime.encode_text(text_chunk)
                request_rows = runtime.build_voice_clone_request_rows(
                    prompt_audio_codes, text_token_ids
                )
                # Text length cannot tell us whether generated speech is valid:
                # one character may be a brief word or a deliberately prolonged
                # utterance.  Only reject technical failures here.  A candidate
                # is valid when the model emitted EOS before the package-wide
                # safety cap and the codec produced finite, non-empty PCM.
                spoken_characters = sum(
                    character.isalnum() for character in text_chunk
                )
                # Short text must still accept any word, but it never needs the
                # full 30-second generation ceiling.  A bad sampling path now
                # reaches the retry point after at most about eight audio
                # seconds instead of making Reader wait through the full cap.
                frame_limit = (
                    min(original_max_frames, 100)
                    if spoken_characters <= 8
                    else original_max_frames
                )
                runtime.manifest["generation_defaults"]["max_new_frames"] = frame_limit
                accepted: tuple[np.ndarray, list[list[int]], int] | None = None
                completed_candidates: list[
                    tuple[np.ndarray, list[list[int]], int]
                ] = []
                failure_details: list[str] = []
                for attempt in range(4):
                    self._raise_if_cancelled(cancel_event)
                    runtime.rng = np.random.default_rng(
                        _retry_seed(
                            base_seed,
                            text_chunk,
                            attempt,
                            stable_first_seed=stable_first_seed,
                        )
                    )
                    generated_frames = runtime.generate_audio_frames(
                        request_rows,
                        on_frame=lambda _rows, _index, _frame: self._raise_if_cancelled(
                            cancel_event
                        ),
                    )
                    self._raise_if_cancelled(cancel_event)
                    completed = len(generated_frames) < frame_limit
                    if not completed:
                        failure_details.append(
                            f"第{attempt + 1}次达到统一安全上限{frame_limit}帧且未正常结束"
                        )
                        continue
                    waveform = np.asarray(
                        runtime.decode_full_audio_safe(generated_frames), dtype=np.float32
                    )
                    if (
                        waveform.ndim != 2
                        or waveform.shape[0] <= 0
                        or waveform.shape[1] != channels
                        or not bool(np.isfinite(waveform).all())
                    ):
                        failure_details.append(
                            f"第{attempt + 1}次已结束但解码PCM为空、格式错误或包含非有限值"
                        )
                        continue
                    candidate = (waveform, generated_frames, attempt)
                    # A completed one/few-character request can occasionally
                    # decode into many seconds of repeated or stretched sound.
                    # It is still valid PCM, so try the deterministic fallback
                    # paths rather than reporting the input as unsupported.  If
                    # every completed path is unusually long, the shortest one
                    # remains a last-resort result and arbitrary short text is
                    # never rejected solely because of its length.
                    audio_seconds = waveform.shape[0] / float(sample_rate)
                    short_request_limit = (
                        4.0
                        if spoken_characters <= 1
                        else max(1.5, 0.5 + float(spoken_characters) / 2.5)
                    )
                    if spoken_characters <= 8 and audio_seconds > short_request_limit:
                        completed_candidates.append(candidate)
                        failure_details.append(
                            f"第{attempt + 1}次已结束但{spoken_characters}字音频长达"
                            f"{audio_seconds:.2f}秒，改用稳定备用轨迹"
                        )
                        continue
                    accepted = candidate
                    break
                if accepted is None and completed_candidates:
                    accepted = min(
                        completed_candidates,
                        key=lambda candidate: candidate[0].shape[0],
                    )
                if accepted is None:
                    detail = "；".join(failure_details) or "没有可解码候选"
                    raise RuntimeError(f"Basic生成未正常完成，已重试4次：{detail}")
                waveform, generated_frames, accepted_attempt = accepted
                retry_counts.append(accepted_attempt)
                generated_frames_total += len(generated_frames)
                frames_per_emit = max(1, sample_rate // 2)
                for start in range(0, waveform.shape[0], frames_per_emit):
                    self._raise_if_cancelled(cancel_event)
                    block = np.asarray(
                        waveform[start : start + frames_per_emit], dtype="<f4"
                    )
                    duration_seconds = block.shape[0] / float(sample_rate)
                    emitted_samples_total += int(block.shape[0])
                    emit(
                        block.tobytes(order="C"),
                        sample_rate,
                        channels,
                        duration_seconds,
                        False,
                    )

                if text_chunk_index < len(chunks) - 1:
                    pause_seconds = runtime.estimate_voice_clone_inter_chunk_pause_seconds(
                        text_chunk
                    )
                    pause_frames = max(0, int(round(sample_rate * pause_seconds)))
                    if pause_frames:
                        silence = np.zeros((pause_frames, channels), dtype="<f4")
                        emitted_samples_total += pause_frames
                        emit(
                            silence.tobytes(order="C"),
                            sample_rate,
                            channels,
                            pause_seconds,
                            True,
                        )
        finally:
            runtime.manifest["generation_defaults"]["max_new_frames"] = original_max_frames

        return {
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_seconds": emitted_samples_total / float(sample_rate),
            "generated_frames": generated_frames_total,
            "text_chunks": chunks,
            "retry_counts": retry_counts,
        }

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise SynthesisCancelled("synthesis was cancelled")

    def _require_runtime(self) -> OnnxTtsRuntime:
        if self.runtime is None:
            raise RuntimeError("engine is not loaded")
        return self.runtime
