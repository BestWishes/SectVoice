from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from threading import Event
import time
from typing import Any, Callable

import numpy as np


ENGINE_ID = "gpt-sovits-v2proplus"
ENGINE_VERSION = "d523079f-sv3"
PAYLOAD_FORMAT_VERSION = "gpt-sovits-prompt-cache-v1"
DEFAULT_GPU_COMPUTE_PACING = {
    "active_fraction": 0.58,
    "token_interval": 8,
    "maximum_token_pause_seconds": 0.08,
    "maximum_chunk_pause_seconds": 0.12,
    "maximum_realtime_factor": 0.72,
}
_TRAILING_CLOSERS = frozenset("”’\"'）)]】〕］》〉」』")
_TERMINAL_PUNCTUATION = frozenset(".!?。！？")


class SynthesisCancelled(RuntimeError):
    pass


class GptSovitsEngine:
    """Pinned GPT-SoVITS adapter whose prompt representation stays private."""

    def __init__(
        self,
        *,
        app_root: Path,
        model_dir: Path,
        output_dir: Path,
        device: str = "cuda",
        half: bool = True,
    ) -> None:
        self.app_root = app_root.resolve()
        self.model_dir = model_dir.resolve()
        self.output_dir = output_dir.resolve()
        self.device = device
        self.half = bool(half)
        self.tts: Any = None
        self._loaded_payload: Path | None = None

    def load(self) -> None:
        if self.tts is not None:
            return
        self._check_required_files()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(self.app_root)
        os.environ["bert_path"] = str(self.model_dir / "chinese-roberta-wwm-ext-large")
        package_dir = Path(__file__).resolve().parent
        for item in (package_dir / "compat", self.app_root, self.app_root / "GPT_SoVITS"):
            value = str(item)
            if value not in sys.path:
                sys.path.insert(0, value)

        # Upstream SV uses a process-relative default. Override it inside this
        # private adapter so the weight remains in the independently managed
        # model package instead of being copied into Reader or common protocols.
        import sv

        sv.sv_path = str(self.model_dir / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt")
        from TTS_infer_pack.TTS import TTS, TTS_Config
        import fast_langdetect

        fast_langdetect.infer._default_detector = fast_langdetect.infer.LangDetector(
            fast_langdetect.infer.LangDetectConfig(
                cache_dir=self.model_dir / "fast_langdetect"
            )
        )

        custom = {
            "device": self.device,
            "is_half": self.half,
            "version": "v2ProPlus",
            "t2s_weights_path": str(self.model_dir / "s1v3.ckpt"),
            "vits_weights_path": str(self.model_dir / "v2Pro" / "s2Gv2ProPlus.pth"),
            "cnhuhbert_base_path": str(self.model_dir / "chinese-hubert-base"),
            "bert_base_path": str(self.model_dir / "chinese-roberta-wwm-ext-large"),
        }
        self.tts = TTS(TTS_Config({"custom": custom}))

    def unload(self) -> None:
        if self.tts is None:
            return
        import gc
        import torch

        self.tts = None
        self._loaded_payload = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def compile_voice(
        self,
        reference_wav: Path,
        reference_transcript: str,
        language: str,
        destination: Path,
    ) -> dict[str, object]:
        tts = self._require_tts()
        transcript = reference_transcript.strip()
        if not transcript:
            raise ValueError("reference transcript is required")
        prompt_language = self._map_language(language)
        transcript = _place_terminal_punctuation_after_closers(transcript)
        if transcript[-1] not in _TERMINAL_PUNCTUATION:
            transcript += "。" if prompt_language != "en" else "."

        tts.set_ref_audio(str(reference_wav.resolve()))
        phones, bert_features, normalized = (
            tts.text_preprocessor.segment_and_extract_feature_for_text(
                transcript, prompt_language, tts.configs.version
            )
        )
        tts.prompt_cache["prompt_text"] = transcript
        tts.prompt_cache["prompt_lang"] = prompt_language
        tts.prompt_cache["phones"] = phones
        tts.prompt_cache["bert_features"] = bert_features
        tts.prompt_cache["norm_text"] = normalized

        # These names and tensors are deliberately confined to this engine package.
        private_payload = {
            "payload_format_version": PAYLOAD_FORMAT_VERSION,
            "engine_id": ENGINE_ID,
            "engine_version": ENGINE_VERSION,
            "prompt_cache": self._to_cpu(tts.prompt_cache),
        }
        private_payload["prompt_cache"]["ref_audio_path"] = None
        destination.mkdir(parents=True, exist_ok=True)
        temporary = destination / "engine-payload.pt.partial"
        target = destination / "engine-payload.pt"
        import torch

        torch.save(private_payload, temporary)
        os.replace(temporary, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self._loaded_payload = None
        return {
            "opaque_path": str(destination),
            "sha256": digest,
            "payload_format_version": PAYLOAD_FORMAT_VERSION,
            "byte_size": target.stat().st_size,
        }

    def synthesize_stream(
        self,
        *,
        text: str,
        payload_dir: Path,
        emit: Callable[[bytes, int, int, float, bool], None],
        cancel_event: Event | None = None,
        speed: float = 1.0,
        seed: int = -1,
        min_chunk_length: int = 16,
        streaming_mode: bool = True,
        compute_pacing: dict[str, object] | None = None,
    ) -> dict[str, object]:
        tts = self._require_tts()
        if not text.strip():
            raise ValueError("text cannot be empty")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be in [0.5, 2.0]")
        self._load_private_payload(payload_dir)
        cache = tts.prompt_cache
        engine_text = _place_terminal_punctuation_after_closers(text)
        generator = tts.run(
            {
                "text": engine_text,
                "text_lang": "zh",
                "ref_audio_path": None,
                "prompt_text": cache["prompt_text"],
                "prompt_lang": cache["prompt_lang"],
                "top_k": 15,
                "top_p": 1.0,
                "temperature": 1.0,
                # Reader already plans one coherent GenerationWindow.  cut1
                # groups/splits punctuation again and can create a different
                # rhythm inside the same Reader window, especially around
                # several short quoted questions.  cut0 keeps this adapter's
                # request as the single acoustic take Reader asked for.
                "text_split_method": "cut0",
                "batch_size": 1,
                "split_bucket": False,
                "speed_factor": speed,
                "fragment_interval": 0.0,
                "seed": seed,
                "parallel_infer": not streaming_mode,
                "streaming_mode": streaming_mode,
                "return_fragment": False,
                "min_chunk_length": max(8, int(min_chunk_length)),
                "overlap_length": 2,
            }
        )
        pacer = _ComputePacer(compute_pacing, cancel_event)
        samples = 0
        chunks = 0
        sample_rate = int(tts.configs.sampling_rate)
        pacer.install_token_hook(tts)
        try:
            iterator = iter(generator)
            while True:
                compute_started = time.perf_counter()
                try:
                    rate, audio = next(iterator)
                except StopIteration:
                    break
                compute_seconds = max(0.0, time.perf_counter() - compute_started)
                self._raise_if_cancelled(cancel_event)
                array = np.asarray(audio, dtype="<i2").reshape(-1)
                if array.size == 0:
                    continue
                sample_rate = int(rate)
                samples += int(array.size)
                chunk_seconds = array.size / sample_rate
                emit(array.tobytes(order="C"), sample_rate, 1, chunk_seconds, False)
                chunks += 1
                pacer.after_chunk(compute_seconds, chunk_seconds)
        finally:
            pacer.remove_token_hook(tts)
        return {
            "sample_rate": sample_rate,
            "channels": 1,
            "audio_seconds": samples / float(sample_rate),
            "chunks": chunks,
            "compute_pacing": pacer.report(),
        }

    def request_cancel(self) -> None:
        if self.tts is not None:
            self.tts.stop()
    def _load_private_payload(self, payload_dir: Path) -> None:
        tts = self._require_tts()
        resolved = payload_dir.resolve()
        if self._loaded_payload == resolved:
            return
        import torch

        payload_path = resolved / "engine-payload.pt"
        payload = torch.load(payload_path, map_location="cpu", weights_only=False)
        if payload.get("payload_format_version") != PAYLOAD_FORMAT_VERSION:
            raise RuntimeError("Standard voice payload format is incompatible")
        if payload.get("engine_id") != ENGINE_ID:
            raise RuntimeError("Standard voice payload belongs to another engine")
        tts.prompt_cache = self._to_device(payload["prompt_cache"], tts.configs.device)
        self._loaded_payload = resolved

    def _check_required_files(self) -> None:
        required = (
            self.model_dir / "s1v3.ckpt",
            self.model_dir / "v2Pro" / "s2Gv2ProPlus.pth",
            self.model_dir / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
            self.model_dir / "chinese-hubert-base" / "pytorch_model.bin",
            self.model_dir / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin",
            self.model_dir / "fast_langdetect" / "lid.176.bin",
            self.app_root / "GPT_SoVITS" / "text" / "G2PWModel",
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError("GPT-SoVITS model package is incomplete: " + "; ".join(missing))

    @staticmethod
    def _map_language(language: str) -> str:
        lowered = language.lower()
        if lowered.startswith("zh"):
            return "zh"
        if lowered.startswith("en"):
            return "en"
        raise ValueError(f"unsupported prompt language: {language}")

    @classmethod
    def _to_cpu(cls, value: Any) -> Any:
        import torch

        if isinstance(value, torch.Tensor):
            return value.detach().cpu()
        if isinstance(value, dict):
            return {key: cls._to_cpu(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._to_cpu(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._to_cpu(item) for item in value)
        return value

    @classmethod
    def _to_device(cls, value: Any, device: Any) -> Any:
        import torch

        if isinstance(value, torch.Tensor):
            return value.to(device)
        if isinstance(value, dict):
            return {key: cls._to_device(item, device) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._to_device(item, device) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._to_device(item, device) for item in value)
        return value

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise SynthesisCancelled("synthesis was cancelled")

    def _require_tts(self) -> Any:
        if self.tts is None:
            raise RuntimeError("engine is not loaded")
        return self.tts


def _place_terminal_punctuation_after_closers(text: str) -> str:
    """Avoid GPT-SoVITS appending a second full stop after a closing quote."""

    stripped = text.rstrip()
    suffix_space = text[len(stripped) :]
    closer_start = len(stripped)
    while closer_start > 0 and stripped[closer_start - 1] in _TRAILING_CLOSERS:
        closer_start -= 1
    if closer_start == len(stripped) or closer_start == 0:
        return text
    punctuation_index = closer_start - 1
    if stripped[punctuation_index] not in _TERMINAL_PUNCTUATION:
        return text
    return (
        stripped[:punctuation_index]
        + stripped[closer_start:]
        + stripped[punctuation_index]
        + suffix_space
    )


class _ComputePacer:
    """Spread existing streaming kernels without changing synthesis audio.

    GPT-SoVITS does not expose a safe per-process GPU-utilization limit on
    Windows.  The adapter therefore yields briefly only between the model's
    existing streaming chunks.  It never changes text, seeds, sampling,
    semantic chunk length, vocoder settings or PCM.  The realtime-factor
    ceiling is a fail-open guard: slow voices or machines automatically receive
    less (or no) pacing so Reader's audible buffer is not starved.
    """

    def __init__(
        self,
        config: dict[str, object] | None,
        cancel_event: Event | None,
    ) -> None:
        raw = dict(config or {})
        self.active_fraction = float(raw.get("active_fraction", 1.0))
        self.token_interval = max(1, int(raw.get("token_interval", 8)))
        self.maximum_token_pause_seconds = float(
            raw.get("maximum_token_pause_seconds", 0.0)
        )
        self.maximum_chunk_pause_seconds = float(
            raw.get("maximum_chunk_pause_seconds", 0.0)
        )
        self.maximum_realtime_factor = float(
            raw.get("maximum_realtime_factor", 0.0)
        )
        self.enabled = (
            0.05 <= self.active_fraction < 1.0
            and self.maximum_token_pause_seconds > 0.0
            and self.maximum_realtime_factor > 0.0
        )
        self.cancel_event = cancel_event
        self.started_at = time.perf_counter()
        self.audio_seconds = 0.0
        self.compute_seconds = 0.0
        self.sleep_seconds = 0.0
        self.pause_count = 0
        self.token_pause_count = 0
        self._token_counter = 0
        self._token_compute_started = time.perf_counter()
        self._token_hook = self.after_token

    def install_token_hook(self, tts: Any) -> None:
        if not self.enabled:
            return
        model = tts.t2s_model.model
        if not hasattr(model, "_sectvoice_compute_pacing_hook"):
            model._sectvoice_compute_pacing_hook = self._token_hook

    def remove_token_hook(self, tts: Any) -> None:
        model = getattr(getattr(tts, "t2s_model", None), "model", None)
        if model is not None and getattr(
            model, "_sectvoice_compute_pacing_hook", None
        ) is self._token_hook:
            delattr(model, "_sectvoice_compute_pacing_hook")

    def after_token(self) -> None:
        self._token_counter += 1
        if not self.enabled or self._token_counter % self.token_interval:
            return
        now = time.perf_counter()
        compute_seconds = max(0.0, now - self._token_compute_started)
        self.compute_seconds += compute_seconds
        pause = min(
            compute_seconds * (1.0 / self.active_fraction - 1.0),
            self.maximum_token_pause_seconds,
        )
        cancelled = self._sleep(pause)
        self.token_pause_count += int(pause > 0.001 and not cancelled)
        self._token_compute_started = time.perf_counter()

    def after_chunk(self, compute_seconds: float, audio_seconds: float) -> None:
        self.audio_seconds += max(0.0, audio_seconds)
        self.compute_seconds += max(0.0, compute_seconds)
        if not self.enabled or compute_seconds <= 0.0:
            return
        elapsed = max(0.0, time.perf_counter() - self.started_at)
        realtime_budget = max(
            0.0,
            self.audio_seconds * self.maximum_realtime_factor - elapsed,
        )
        duty_pause = compute_seconds * (1.0 / self.active_fraction - 1.0)
        pause = min(
            duty_pause,
            self.maximum_chunk_pause_seconds,
            realtime_budget,
        )
        cancelled = self._sleep(pause)
        if cancelled:
            raise SynthesisCancelled("synthesis was cancelled")
        self.pause_count += int(pause > 0.001)

    def _sleep(self, pause: float) -> bool:
        if pause <= 0.001:
            return False
        if self.cancel_event is not None:
            if self.cancel_event.wait(pause):
                # The token hook executes inside upstream's generator. Raising
                # there makes upstream treat an ordinary Reader seek as a model
                # fault and reload all weights.  Return promptly instead;
                # request_cancel already sets upstream's stop flag, and the
                # adapter raises at its own safe boundary.
                return True
        else:
            time.sleep(pause)
        self.sleep_seconds += pause
        return False

    def report(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "active_fraction": self.active_fraction,
            "token_interval": self.token_interval,
            "maximum_token_pause_seconds": self.maximum_token_pause_seconds,
            "maximum_chunk_pause_seconds": self.maximum_chunk_pause_seconds,
            "maximum_realtime_factor": self.maximum_realtime_factor,
            "pause_count": self.pause_count,
            "token_pause_count": self.token_pause_count,
            "sleep_seconds": self.sleep_seconds,
            "compute_seconds": self.compute_seconds,
        }
