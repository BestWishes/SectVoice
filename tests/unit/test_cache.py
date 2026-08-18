from pathlib import Path
from uuid import uuid4

from sectvoice.core.cache import SynthesisCacheIdentity, VoiceCache
from sectvoice.core.database import Database
from sectvoice.domain import Tier
from sectvoice.reader.documents import DocumentRepository


def identity(*, emotion: str = "natural", synthesis_speed: float | None = None):
    return SynthesisCacheIdentity(
        text="同一段文字。",
        voice_id=uuid4(),
        tier=Tier.BASIC,
        engine_id="engine",
        engine_version="1",
        payload_version="1",
        payload_sha256="payload-hash",
        reference_transcript_sha256="transcript-hash",
        language="zh",
        style="",
        emotion=emotion,
        emotion_strength=0.5,
        speed_mode="postprocess" if synthesis_speed is None else "native",
        synthesis_speed=synthesis_speed,
        punctuation_pause_ms=180,
        paragraph_pause_ms=500,
        pcm_format="48000-2-f32le",
        postprocess_version="1",
    )


def test_cache_key_changes_for_real_synthesis_settings() -> None:
    voice_id = uuid4()
    base = identity()
    from dataclasses import replace

    first = replace(base, voice_id=voice_id)
    assert first.key == replace(first).key
    assert first.key != replace(first, emotion="sad").key
    assert first.key != replace(first, synthesis_speed=1.2, speed_mode="native").key
    assert first.key != replace(first, payload_sha256="new-payload")
    assert first.key != replace(first, reference_transcript_sha256="new-transcript")
    assert first.content_signature == replace(first, text="另一种组窗").content_signature
    assert first.content_signature == replace(first, punctuation_pause_ms=999).content_signature
    assert first.content_signature != replace(first, payload_sha256="new-payload").content_signature


def test_cache_round_trip_and_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "cache.db")
    database.initialize()
    cache = VoiceCache(database, tmp_path / "audio-cache")
    source = tmp_path / "audio.wav"
    source.write_bytes(b"a" * 100)
    cache_identity = identity()
    entry = cache.put(cache_identity, source, 1.0, {"unit": "test"})
    assert cache.get(cache_identity.key).audio_path == entry.audio_path  # type: ignore[union-attr]
    assert cache.total_bytes() == 100
    assert cache.prune_to_limit(0) == 100
    assert cache.get(cache_identity.key) is None


def test_cache_can_clear_only_entries_used_by_one_document(tmp_path: Path) -> None:
    database = Database(tmp_path / "cache.db")
    database.initialize()
    documents = DocumentRepository(database)
    first_document = documents.create("第一篇", "第一段。")
    second_document = documents.create("第二篇", "第二段。")
    cache = VoiceCache(database, tmp_path / "audio-cache")
    source = tmp_path / "audio.pcm"
    source.write_bytes(b"a" * 80)
    first = identity()
    second = identity(emotion="calm")
    cache.put(first, source, 1.0)
    cache.put(second, source, 1.0)
    cache.note_document_use(first_document.document_id, first.key)
    cache.note_document_use(second_document.document_id, second.key)

    assert cache.clear_document(first_document.document_id) == 80
    assert cache.get(first.key) is None
    assert cache.get(second.key) is not None
