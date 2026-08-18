from uuid import uuid4

import pytest

from sectvoice.core.audio_buffer import BufferWatermarks, StreamingAudioBuffer
from sectvoice.core.session import SessionGate
from sectvoice.domain import AudioChunk, PCMFormat


def make_chunk(session_id, generation_id, sequence, duration=0.5):
    return AudioChunk(
        session_id=session_id,
        generation_id=generation_id,
        sequence=sequence,
        speech_unit_id=uuid4(),
        pcm_format=PCMFormat(24000),
        duration_seconds=duration,
        data=b"\0\0",
    )


def make_final_chunk(session_id, generation_id, sequence):
    return AudioChunk(
        session_id=session_id,
        generation_id=generation_id,
        sequence=sequence,
        speech_unit_id=uuid4(),
        pcm_format=PCMFormat(24000),
        duration_seconds=0,
        data=b"",
        is_final=True,
    )


def test_latest_session_is_the_only_accepted_session() -> None:
    gate = SessionGate()
    first = gate.begin()
    old_chunk = make_chunk(first.session_id, first.generation_id, 0)
    assert gate.accepts(old_chunk)

    second = gate.begin()
    new_chunk = make_chunk(second.session_id, second.generation_id, 0)
    assert not gate.accepts(old_chunk)
    assert gate.accepts(new_chunk)


def test_buffer_uses_seconds_and_clear_is_atomic() -> None:
    gate = SessionGate()
    token = gate.begin()
    buffer = StreamingAudioBuffer(BufferWatermarks(0.5, 1.0, 1.5))
    buffer.push(make_chunk(token.session_id, token.generation_id, 0, 0.8))
    buffer.push(make_chunk(token.session_id, token.generation_id, 1, 0.9))
    assert buffer.buffered_seconds == pytest.approx(1.7)
    assert buffer.at_high_watermark
    discarded = buffer.clear()
    assert len(discarded) == 2
    assert buffer.buffered_seconds == 0
    assert buffer.needs_audio


def test_startup_buffer_waits_for_complete_units_and_minimum_seconds() -> None:
    token = SessionGate().begin()
    buffer = StreamingAudioBuffer(BufferWatermarks(3.0, 6.0, 18.0))
    buffer.push(make_chunk(token.session_id, token.generation_id, 0, 2.5))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 1))
    assert buffer.completed_units == 1
    assert not buffer.ready_to_start

    buffer.push(make_chunk(token.session_id, token.generation_id, 2, 3.6))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 3))
    assert buffer.completed_units == 2
    assert buffer.ready_to_start


def test_two_tiny_sentences_do_not_start_with_an_unsafe_time_buffer() -> None:
    token = SessionGate().begin()
    buffer = StreamingAudioBuffer(BufferWatermarks(3.0, 6.0, 18.0))
    buffer.push(make_chunk(token.session_id, token.generation_id, 0, 1.2))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 1))
    buffer.push(make_chunk(token.session_id, token.generation_id, 2, 1.3))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 3))
    assert buffer.completed_units == 2
    assert not buffer.ready_to_start


def test_one_complete_sentence_waits_until_target_cushion_is_reached() -> None:
    token = SessionGate().begin()
    buffer = StreamingAudioBuffer(BufferWatermarks(3.0, 6.0, 18.0))
    buffer.push(make_chunk(token.session_id, token.generation_id, 0, 3.2))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 1))
    assert not buffer.ready_to_start
    buffer.push(make_chunk(token.session_id, token.generation_id, 2, 3.0))
    buffer.push(make_final_chunk(token.session_id, token.generation_id, 3))
    assert buffer.ready_to_start
