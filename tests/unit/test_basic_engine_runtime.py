from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import numpy as np
import pytest


PACKAGE_DIR = Path(__file__).parents[2] / "engine_packages" / "basic_moss_nano"


def _load_runtime_module():
    dependency = types.ModuleType("onnx_tts_runtime")
    dependency.OnnxTtsRuntime = object
    previous = sys.modules.get("onnx_tts_runtime")
    sys.modules["onnx_tts_runtime"] = dependency
    try:
        spec = importlib.util.spec_from_file_location(
            "sectvoice_basic_runtime_under_test", PACKAGE_DIR / "runtime_engine.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("onnx_tts_runtime", None)
        else:
            sys.modules["onnx_tts_runtime"] = previous


def _load_worker_module(runtime_module):
    previous = sys.modules.get("runtime_engine")
    sys.modules["runtime_engine"] = runtime_module
    try:
        spec = importlib.util.spec_from_file_location(
            "sectvoice_basic_worker_under_test", PACKAGE_DIR / "worker.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("runtime_engine", None)
        else:
            sys.modules["runtime_engine"] = previous


class _FakeRuntime:
    def __init__(self, responses: list[tuple[int, float]], frame_limit: int = 10) -> None:
        self.responses = responses
        self.frame_limit = frame_limit
        self.calls = 0
        self.last_duration = 0.0
        self.manifest = {"generation_defaults": {"max_new_frames": frame_limit}}
        self.codec_meta = {"codec_config": {"sample_rate": 10, "channels": 1}}
        self.rng = np.random.default_rng(1)

    def prepare_synthesis_text(self, **kwargs):
        return {"text": kwargs["text"]}

    def split_voice_clone_text(self, text: str, max_tokens: int):
        del max_tokens
        return [text]

    def encode_text(self, text: str):
        return [ord(character) for character in text]

    def build_voice_clone_request_rows(self, prompt_audio_codes, text_token_ids):
        return {"prompt": prompt_audio_codes, "text": text_token_ids}

    def generate_audio_frames(self, request_rows, on_frame=None):
        del request_rows, on_frame
        frame_count, self.last_duration = self.responses[self.calls]
        self.calls += 1
        return [[self.calls]] * frame_count

    def decode_full_audio_safe(self, generated_frames):
        del generated_frames
        return np.ones((round(self.last_duration * 10), 1), dtype=np.float32)

    def estimate_voice_clone_inter_chunk_pause_seconds(self, text: str):
        del text
        return 0.0


def _engine_with_runtime(responses: list[tuple[int, float]], frame_limit: int = 10):
    module = _load_runtime_module()
    engine = module.MossNanoEngine(Path("model"), Path("output"), max_new_frames=frame_limit)
    runtime = _FakeRuntime(responses, frame_limit)
    engine.runtime = runtime
    engine.read_private_payload = lambda _path: [[1, 2, 3]]
    return engine, runtime


def test_first_seed_is_repeatable_and_text_specific() -> None:
    module = _load_runtime_module()

    assert module._retry_seed(123, "甲。", 0) == module._retry_seed(123, "甲。", 0)
    assert module._retry_seed(123, "甲。", 0) != module._retry_seed(123, "乙。", 0)
    assert module._retry_seed(123, "甲" * 9, 0) != module._retry_seed(123, "乙" * 9, 0)
    assert module._retry_seed(123, "甲。", 0) != module._retry_seed(123, "甲。", 1)
    assert module._retry_seed(123, "甲。", 1) != module._retry_seed(123, "乙。", 1)


def test_matching_language_mode_keeps_one_first_seed_but_text_specific_fallbacks() -> None:
    module = _load_runtime_module()

    assert module._retry_seed(123, "甲。", 0, stable_first_seed=True) == 123
    assert module._retry_seed(123, "乙。", 0, stable_first_seed=True) == 123
    assert module._retry_seed(123, "甲。", 1, stable_first_seed=True) != module._retry_seed(
        123, "乙。", 1, stable_first_seed=True
    )


@pytest.mark.parametrize("text", ("砰。", "好。", "Hello。", "任意词。"))
def test_reasonable_completed_short_text_is_accepted(text: str) -> None:
    engine, runtime = _engine_with_runtime([(4, 1.6)])
    emitted: list[bytes] = []

    result = engine.synthesize_stream(
        text=text,
        payload_dir=Path("payload"),
        emit=lambda data, *_args: emitted.append(data),
        seed=123,
    )

    assert runtime.calls == 1
    assert result["audio_seconds"] == pytest.approx(1.6)
    assert result["retry_counts"] == [0]
    assert emitted


def test_only_generation_without_eos_is_retried() -> None:
    engine, runtime = _engine_with_runtime([(10, 8.0), (4, 2.4)])

    result = engine.synthesize_stream(
        text="短。",
        payload_dir=Path("payload"),
        emit=lambda *_args: None,
        seed=123,
    )

    assert runtime.calls == 2
    assert result["audio_seconds"] == pytest.approx(2.4)
    assert result["retry_counts"] == [1]


def test_unusually_long_short_utterance_uses_stable_fallback_without_rejection() -> None:
    engine, runtime = _engine_with_runtime([(6, 14.4), (4, 1.2)])

    result = engine.synthesize_stream(
        text="砰——",
        payload_dir=Path("payload"),
        emit=lambda *_args: None,
        seed=123,
    )

    assert runtime.calls == 2
    assert result["audio_seconds"] == pytest.approx(1.2)
    assert result["retry_counts"] == [1]


def test_short_utterance_keeps_shortest_completed_path_if_all_are_long() -> None:
    engine, runtime = _engine_with_runtime(
        [(6, 14.4), (6, 12.0), (6, 10.0), (6, 9.0)]
    )

    result = engine.synthesize_stream(
        text="砰——",
        payload_dir=Path("payload"),
        emit=lambda *_args: None,
        seed=123,
    )

    assert runtime.calls == 4
    assert result["audio_seconds"] == pytest.approx(9.0)
    assert result["retry_counts"] == [3]


def test_all_attempts_without_eos_report_a_technical_failure() -> None:
    engine, runtime = _engine_with_runtime([(10, 8.0)] * 4)

    with pytest.raises(RuntimeError, match="统一安全上限10帧"):
        engine.synthesize_stream(
            text="短。",
            payload_dir=Path("payload"),
            emit=lambda *_args: None,
            seed=123,
        )

    assert runtime.calls == 4


def test_worker_persists_structured_error_context_without_private_paths(capsys) -> None:
    runtime_module = _load_runtime_module()
    worker_module = _load_worker_module(runtime_module)

    class Connection:
        def __init__(self) -> None:
            self.messages = []

        def send(self, message) -> None:
            self.messages.append(message)

    connection = Connection()
    worker = worker_module.Worker(object(), connection)
    context = {
        "command": "synthesize",
        "session_id": "session",
        "generation_id": 7,
        "speech_unit_id": "unit",
        "text": "任意短词。",
        "payload_path": "private-payload-path",
        "auth-token": "private-token",
    }
    try:
        raise RuntimeError("diagnostic failure")
    except RuntimeError as exc:
        worker._send_error("request", exc, context)

    record = json.loads(capsys.readouterr().err)
    assert record["event"] == "engine_error"
    assert record["text_preview"] == "任意短词。"
    assert "RuntimeError: diagnostic failure" in record["traceback"]
    assert "payload_path" not in record
    assert "auth-token" not in record
    assert connection.messages[0]["error"] == "diagnostic failure"
