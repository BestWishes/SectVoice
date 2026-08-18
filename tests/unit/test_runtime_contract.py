from dataclasses import fields

import pytest
from uuid import uuid4

from sectvoice.core.runtime import (
    ResolvedSpeechUnit,
    _runtime_request_texts,
    _synthesis_seed,
    _pause_milliseconds,
    _stable_voice_seed,
    _target_speech_seconds,
    _timing_speed_factor,
    _uses_stable_moss_first_seed,
)


def test_runtime_resolution_does_not_put_reader_ranges_in_audio_protocol() -> None:
    # Reader ranges live in SpeechUnit; AudioChunk remains engine-neutral and range-free.
    names = {item.name for item in fields(ResolvedSpeechUnit)}
    assert {"unit", "voice_id", "tier", "payload"}.issubset(names)
    assert "text_start" not in names
    assert "text_end" not in names


def test_runtime_micro_split_preserves_text_but_not_reader_ranges() -> None:
    text = "甲" * 18 + "，" + "乙" * 18 + "。"
    parts = _runtime_request_texts(text, 24)
    assert "".join(parts) == text
    assert len(parts) == 2


def test_runtime_request_removes_layout_whitespace_before_synthesis() -> None:
    assert _runtime_request_texts("　　你好，世界。\n", 200) == ("你好，世界。",)


def test_voice_seed_is_stable_per_voice() -> None:
    voice_id = uuid4()
    assert _stable_voice_seed(voice_id) == _stable_voice_seed(voice_id)
    assert _stable_voice_seed(voice_id) != _stable_voice_seed(uuid4())


def test_cross_language_moss_uses_validated_alternate_seed_only_for_that_engine() -> None:
    from dataclasses import replace
    from pathlib import Path

    from sectvoice.domain import EnginePayloadRef, PayloadStatus, Tier, utc_now_iso
    from sectvoice.reader.segmentation import segment_text

    voice_id = uuid4()
    now = utc_now_iso()
    unit = segment_text("测试。", max_chars=120).units[0]
    payload = EnginePayloadRef(
        voice_id=voice_id,
        tier=Tier.BASIC,
        engine_id="moss-nano-onnx",
        engine_version="1",
        payload_format_version="1",
        status=PayloadStatus.READY,
        opaque_path=Path("payload"),
        sha256="abc",
        created_at=now,
        updated_at=now,
    )
    chinese = ResolvedSpeechUnit(unit, voice_id, Tier.BASIC, payload, "参考", "zh-CN")
    english = ResolvedSpeechUnit(unit, voice_id, Tier.BASIC, payload, "reference", "en")
    japanese = ResolvedSpeechUnit(unit, voice_id, Tier.BASIC, payload, "参考", "ja")
    other_engine = replace(english, payload=replace(payload, engine_id="other-engine"))
    assert _synthesis_seed(chinese) == _stable_voice_seed(voice_id)
    assert _synthesis_seed(english) == (_stable_voice_seed(voice_id) + 104729) & 0x7FFFFFFF
    assert _synthesis_seed(japanese) == _stable_voice_seed(voice_id)
    assert _synthesis_seed(other_engine) == _stable_voice_seed(voice_id)
    assert _uses_stable_moss_first_seed(chinese)
    assert not _uses_stable_moss_first_seed(english)
    assert not _uses_stable_moss_first_seed(japanese)
    assert not _uses_stable_moss_first_seed(other_engine)


def test_timing_target_corrects_short_and_long_sentence_drift() -> None:
    assert _timing_speed_factor(
        "到了。", 1.2, 1.0, active_speech_seconds=1.0
    ) == pytest.approx(1.25)
    assert _timing_speed_factor(
        "甲" * 24 + "。", 3.0, 1.0, active_speech_seconds=3.0
    ) == pytest.approx(0.90)
    assert _target_speech_seconds("甲乙丙丁。", 2.0) < _target_speech_seconds(
        "甲乙丙丁。", 1.0
    )
    assert _timing_speed_factor(
        "他不得不承认，他心动了。",
        3.46,
        1.0,
        active_speech_seconds=1.99,
    ) == pytest.approx(1.0)
    assert _timing_speed_factor(
        "怪不得怪不得。",
        2.4,
        1.0,
        active_speech_seconds=0.6,
    ) == pytest.approx(0.90)


def test_normal_default_speed_does_not_time_stretch_or_deform_words() -> None:
    assert _timing_speed_factor(
        "普通人几乎没有机会能够进入秘境。",
        3.0,
        1.0,
        active_speech_seconds=3.0,
    ) == pytest.approx(1.0)
    assert _timing_speed_factor(
        "正常语速。", 1.0, 1.5, active_speech_seconds=1.0
    ) == pytest.approx(1.5)


def test_visual_line_ending_uses_paragraph_pause_before_punctuation_pause() -> None:
    from sectvoice.domain import SynthesisSettings

    settings = SynthesisSettings(punctuation_pause_ms=120, paragraph_pause_ms=280)
    assert _pause_milliseconds("这是普通一句。\n", settings) == 280
    assert _pause_milliseconds("这是一个自然段。\n\n", settings) == 280
    assert _pause_milliseconds("标题\n", settings) == 280
    assert _pause_milliseconds("Windows换行。\r\n", settings) == 280
    assert _pause_milliseconds("同段中的一句。", settings) == 120
    assert _pause_milliseconds(
        "设置为一秒的自然段。\n",
        SynthesisSettings(punctuation_pause_ms=120, paragraph_pause_ms=1000),
    ) == 1000
