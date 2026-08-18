from __future__ import annotations

import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np


PACKAGE_DIR = Path(__file__).parents[2] / "engine_packages" / "standard_gpt_sovits"


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location(
        "sectvoice_standard_runtime_under_test", PACKAGE_DIR / "runtime_engine.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTTS:
    def __init__(self) -> None:
        self.configs = SimpleNamespace(sampling_rate=32_000)
        self.t2s_model = SimpleNamespace(model=SimpleNamespace())
        self.prompt_cache = {"prompt_text": "参考。", "prompt_lang": "zh"}
        self.options: dict[str, object] | None = None

    def run(self, options):
        self.options = dict(options)
        yield 32_000, np.full(320, 100, dtype="<i2")


class _TwoChunkTTS(_FakeTTS):
    def run(self, options):
        self.options = dict(options)
        for value in (100, 200):
            time.sleep(0.003)
            yield 32_000, np.full(32_000, value, dtype="<i2")


def test_reader_window_is_not_split_a_second_time_by_standard_engine(tmp_path: Path) -> None:
    module = _load_runtime_module()
    payload = tmp_path / "payload"
    payload.mkdir()
    fake = _FakeTTS()
    engine = module.GptSovitsEngine(
        app_root=tmp_path,
        model_dir=tmp_path,
        output_dir=tmp_path,
    )
    engine.tts = fake
    engine._loaded_payload = payload.resolve()

    emitted: list[bytes] = []
    engine.synthesize_stream(
        text="“嗯？杨晟？火灵体？真传弟子？”正在路上的许川并不知道。",
        payload_dir=payload,
        emit=lambda data, *_args: emitted.append(data),
    )

    assert emitted
    assert fake.options is not None
    assert fake.options["text_split_method"] == "cut0"
    assert fake.options["text"].endswith("不知道。")
    assert module.ENGINE_VERSION == "d523079f-sv3"


def test_terminal_punctuation_is_moved_after_closing_quote_without_duplication() -> None:
    module = _load_runtime_module()

    assert module._place_terminal_punctuation_after_closers("他说：“知道了。”") == (
        "他说：“知道了”。"
    )
    assert module._place_terminal_punctuation_after_closers("他说：“为什么？”") == (
        "他说：“为什么”？"
    )
    assert module._place_terminal_punctuation_after_closers("《标题》") == "《标题》"


def test_compute_pacing_keeps_pcm_identical_and_reports_yields(tmp_path: Path) -> None:
    module = _load_runtime_module()
    payload = tmp_path / "payload"
    payload.mkdir()

    def generate(pacing):
        fake = _TwoChunkTTS()
        engine = module.GptSovitsEngine(
            app_root=tmp_path,
            model_dir=tmp_path,
            output_dir=tmp_path,
        )
        engine.tts = fake
        engine._loaded_payload = payload.resolve()
        emitted: list[bytes] = []
        result = engine.synthesize_stream(
            text="同一段文字应当生成完全相同的音频。",
            payload_dir=payload,
            emit=lambda data, *_args: emitted.append(data),
            compute_pacing=pacing,
        )
        return b"".join(emitted), result

    unpaced_audio, unpaced = generate(None)
    paced_audio, paced = generate(
        {
            "active_fraction": 0.5,
            "token_interval": 8,
            "maximum_token_pause_seconds": 0.02,
            "maximum_chunk_pause_seconds": 0.02,
            "maximum_realtime_factor": 0.9,
        }
    )

    assert paced_audio == unpaced_audio
    assert unpaced["compute_pacing"]["enabled"] is False
    assert paced["compute_pacing"]["enabled"] is True
    assert paced["compute_pacing"]["pause_count"] >= 1
    assert paced["compute_pacing"]["sleep_seconds"] > 0.0
