from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from sectvoice.core.generation_window import (
    ResolvedSpeechUnit,
    WindowPlanningPolicy,
    build_window_engine_text,
    plan_next_generation_window,
    pause_milliseconds,
    speech_text_for_engine,
)
from sectvoice.domain import (
    EnginePayloadRef,
    PayloadStatus,
    SynthesisSettings,
    Tier,
    utc_now_iso,
)
from sectvoice.reader.segmentation import segment_text


def _resolved(text: str, *, voice_id=None, ordinal: int = 0) -> ResolvedSpeechUnit:
    voice_id = voice_id or uuid4()
    now = utc_now_iso()
    unit = segment_text(text, max_chars=120).units[0]
    unit = replace(unit, ordinal=ordinal)
    payload = EnginePayloadRef(
        voice_id=voice_id,
        tier=Tier.BASIC,
        engine_id="engine",
        engine_version="1",
        payload_format_version="1",
        status=PayloadStatus.READY,
        opaque_path=Path("payload") / str(voice_id),
        sha256=str(voice_id),
        created_at=now,
        updated_at=now,
    )
    return ResolvedSpeechUnit(unit, voice_id, Tier.BASIC, payload, "参考", "zh-CN")


def test_many_extremely_short_units_fill_duration_instead_of_fixed_count() -> None:
    voice_id = uuid4()
    units = tuple(_resolved("好。", voice_id=voice_id, ordinal=index) for index in range(12))
    window = plan_next_generation_window(
        units,
        start_index=0,
        settings=SynthesisSettings(punctuation_pause_ms=100),
        maximum_request_chars=64,
        chars_per_second=4.8,
        initial=True,
    )

    assert len(window.units) > 3
    assert window.estimated_seconds >= 4.0
    assert len(window.engine_text) <= 64


def test_initial_nightfall_passage_stays_in_one_acoustic_window() -> None:
    voice_id = uuid4()
    texts = (
        "趁着晚霞，许川将《赤血刀法》的秘籍拿出来。",
        "准备先修炼一番。",
        "秘境之中有机缘，也有危险。",
        "多一门攻击手段也更能保证自己和苏妙音的安全。",
    )
    units = tuple(
        _resolved(text, voice_id=voice_id, ordinal=index)
        for index, text in enumerate(texts)
    )

    window = plan_next_generation_window(
        units,
        start_index=0,
        settings=SynthesisSettings(
            punctuation_pause_ms=120,
            paragraph_pause_ms=560,
        ),
        maximum_request_chars=64,
        chars_per_second=4.8,
        initial=True,
    )

    assert window.units == units
    assert window.engine_text == "".join(texts)


def test_one_extremely_long_unit_stands_alone_even_over_engine_soft_limit() -> None:
    voice_id = uuid4()
    long_unit = _resolved("甲" * 100 + "。", voice_id=voice_id)
    following = _resolved("下一句。", voice_id=voice_id, ordinal=1)
    window = plan_next_generation_window(
        (long_unit, following),
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=48,
        chars_per_second=4.8,
        initial=False,
    )

    assert window.units == (long_unit,)
    assert len(window.engine_text) > 48


def test_long_next_unit_is_not_forced_into_a_short_existing_window() -> None:
    voice_id = uuid4()
    short = _resolved("短句。", voice_id=voice_id)
    long_unit = _resolved("乙" * 90 + "。", voice_id=voice_id, ordinal=1)
    window = plan_next_generation_window(
        (short, long_unit),
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=200,
        chars_per_second=4.8,
        initial=False,
    )

    assert window.units == (short,)


def test_voice_or_payload_change_is_a_hard_window_boundary() -> None:
    first = _resolved("第一句。")
    second = _resolved("第二句。")
    window = plan_next_generation_window(
        (first, second),
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=100,
        initial=True,
    )

    assert window.units == (first,)


def test_engine_char_limit_closes_multi_unit_window_without_splitting_reader_unit() -> None:
    voice_id = uuid4()
    units = tuple(
        _resolved("甲乙丙丁戊己庚辛。", voice_id=voice_id, ordinal=index)
        for index in range(4)
    )
    window = plan_next_generation_window(
        units,
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=20,
        initial=True,
    )

    assert len(window.units) == 2
    assert len(window.engine_text) <= 20


def test_window_cache_identity_preserves_unit_boundaries() -> None:
    voice_id = uuid4()
    first = _resolved("甲。", voice_id=voice_id)
    second = _resolved("乙。", voice_id=voice_id, ordinal=1)
    window = plan_next_generation_window(
        (first, second),
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=100,
        initial=True,
        policy=WindowPlanningPolicy(first_target_seconds=1, first_minimum_seconds=1, first_maximum_seconds=2),
    )

    assert window.cache_text == '["甲。","乙。"]'


def test_layout_newline_gets_engine_only_boundary_punctuation() -> None:
    voice_id = uuid4()
    first = _resolved("标题\n", voice_id=voice_id)
    second = _resolved("正文。", voice_id=voice_id, ordinal=1)
    window = plan_next_generation_window(
        (first, second),
        start_index=0,
        settings=SynthesisSettings(),
        maximum_request_chars=100,
        initial=True,
        policy=WindowPlanningPolicy(first_target_seconds=1, first_minimum_seconds=1, first_maximum_seconds=3),
    )

    assert window.engine_text.startswith("标题。正文。")
    assert first.unit.text == "标题\n"


def test_closing_quote_after_terminal_punctuation_does_not_gain_extra_period() -> None:
    voice_id = uuid4()
    first = _resolved("“真传弟子啊！”", voice_id=voice_id)
    second = _resolved("许川止不住地叹息。", voice_id=voice_id, ordinal=1)

    assert build_window_engine_text((first, second)) == (
        "“真传弟子啊！”许川止不住地叹息。"
    )


def test_title_closer_without_punctuation_still_gets_engine_boundary() -> None:
    voice_id = uuid4()
    first = _resolved("《赤血刀法》", voice_id=voice_id)
    second = _resolved("准备修炼。", voice_id=voice_id, ordinal=1)

    assert build_window_engine_text((first, second)) == "《赤血刀法》，准备修炼。"


def test_punctuation_pause_recognizes_period_before_closing_quote() -> None:
    settings = SynthesisSettings(punctuation_pause_ms=345)

    assert pause_milliseconds("他说：“知道了。”", settings) == 345


def test_markdown_and_arrow_become_audible_text_without_mutating_reader_units() -> None:
    source = (
        "> 你又走不了、反击成本高 → 我的攻击成本很低。\n\n"
        "那么**地位高的人欺负地位低的人，不只是“权力使人变坏”，"
        "而是某些原本限制攻击路径的成本消失了。**"
    )
    mapping = segment_text(source)
    voice_id = uuid4()
    units = tuple(
        replace(
            _resolved(unit.text, voice_id=voice_id, ordinal=unit.ordinal),
            unit=unit,
        )
        for unit in mapping.units
    )

    engine_text = build_window_engine_text(units)

    assert ">" not in engine_text
    assert "**" not in engine_text
    assert "→" not in engine_text
    assert "反击成本高，我的攻击成本很低" in engine_text
    assert "地位高的人欺负地位低的人" in engine_text
    assert "".join(item.unit.text for item in units) == source


def test_inline_markdown_link_keeps_only_its_spoken_label() -> None:
    assert speech_text_for_engine("请看[说明](https://example.test)，不要念网址。") == (
        "请看说明，不要念网址。"
    )


def test_invalid_duration_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        WindowPlanningPolicy(first_minimum_seconds=7, first_target_seconds=6)
