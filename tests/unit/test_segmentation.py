import pytest

from sectvoice.reader.segmentation import seek_start_within_unit, segment_text


@pytest.mark.parametrize(
    "text",
    [
        "他说：“你好！”然后转身。\n\n第二段来了？",
        "Hello, world! 'Quoted sentence?' Next; yes.",
        "第一行\n\n\n第四行",
        "没有任何标点" * 80,
        "中英mixed，数字123.45；路径H:\\SectVoice。",
        "“外层『内层』结束。”下一句！",
    ],
)
def test_segmentation_is_lossless_and_contiguous(text: str) -> None:
    mapping = segment_text(text, max_chars=32)
    assert "".join(unit.text for unit in mapping.units) == text
    assert mapping.units[0].start_char == 0
    assert mapping.units[-1].end_char == len(text)
    for index, unit in enumerate(mapping.units):
        assert unit.ordinal == index
        assert text[unit.start_char : unit.end_char] == unit.text
        if index:
            assert mapping.units[index - 1].end_char == unit.start_char


def test_every_character_maps_to_unit_and_internal_offset() -> None:
    text = "甲。\n乙！ English?\n\n丙"
    mapping = segment_text(text, max_chars=20)
    for position, character in enumerate(text):
        unit, offset = mapping.locate(position)
        assert unit.text[offset] == character
        assert unit.start_char + offset == position


def test_long_sentence_prefers_comma_boundary() -> None:
    text = "甲" * 20 + "，" + "乙" * 20 + "，" + "丙" * 20 + "。"
    mapping = segment_text(text, max_chars=32)
    assert len(mapping.units) >= 2
    assert mapping.units[0].text.endswith("，")


def test_long_sentence_does_not_leave_an_orphan_tail() -> None:
    text = "而如今，他修炼玄阳决，铸就雄厚根基，武道之路不说一路平坦，至少不会在一个境界浪费几年光阴，而不得。\n"
    mapping = segment_text(text, max_chars=48)
    assert "".join(unit.text for unit in mapping.units) == text
    assert all(sum(character.isalnum() for character in unit.text) >= 8 for unit in mapping.units)
    assert all(unit.text.strip() != "。" for unit in mapping.units)


def test_punctuation_only_range_attaches_to_a_spoken_unit() -> None:
    text = "第一句。\n　　……\n　　第二句。"
    mapping = segment_text(text, max_chars=32)
    assert "".join(unit.text for unit in mapping.units) == text
    assert all(any(character.isalnum() for character in unit.text) for unit in mapping.units)


def test_seek_uses_nearby_internal_boundary() -> None:
    mapping = segment_text("开头很长的一段，后面还有很多内容没有句号", max_chars=80)
    unit = mapping.units[0]
    offset = unit.text.index("后")
    assert seek_start_within_unit(unit, offset) == unit.text.index("后")


def test_empty_text_has_no_units() -> None:
    assert segment_text("").units == ()


def test_newlines_after_sentence_marks_are_not_empty_speech_units() -> None:
    mapping = segment_text("甲说完了。\n乙回答了！\n\n丙点头。", max_chars=32)
    assert "".join(unit.text for unit in mapping.units) == mapping.source_text
    assert all(unit.text.strip() for unit in mapping.units)
    assert mapping.units[0].text.endswith("\n")
