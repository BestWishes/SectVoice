from dataclasses import fields
from uuid import uuid4

import pytest

from sectvoice.domain import AudioChunk, PCMFormat, TimingMark


def test_audio_chunk_has_no_reader_text_range() -> None:
    names = {item.name.lower() for item in fields(AudioChunk)}
    assert "textstart" not in names
    assert "textend" not in names
    assert "text_start" not in names
    assert "text_end" not in names


def test_timing_marks_are_speech_unit_relative() -> None:
    mark = TimingMark(2, 4, 0.1, 0.3, "你好")
    chunk = AudioChunk(
        session_id=uuid4(),
        generation_id=1,
        sequence=0,
        speech_unit_id=uuid4(),
        pcm_format=PCMFormat(24000),
        duration_seconds=0.3,
        data=b"",
        timing_info=(mark,),
    )
    assert chunk.timing_info[0].speech_unit_offset_start == 2


def test_pcm_rejects_invalid_format() -> None:
    with pytest.raises(ValueError):
        PCMFormat(0)

