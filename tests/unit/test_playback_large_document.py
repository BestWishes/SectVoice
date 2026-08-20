from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from sectvoice.domain import EnginePayloadRef, PayloadStatus, Tier, VoiceProfile, utc_now_iso
from sectvoice.reader.documents import Document
from sectvoice.reader.playback_controller import PlaybackController
from sectvoice.reader.segmentation import segment_text


def test_large_document_resolves_voice_payload_once_per_voice() -> None:
    voice = VoiceProfile.create(
        "旁白",
        Path("source.wav"),
        Path("reference.wav"),
        "参考文字",
    )
    now = utc_now_iso()
    payload = EnginePayloadRef(
        voice_id=voice.voice_id,
        tier=Tier.BASIC,
        engine_id="test-engine",
        engine_version="1",
        payload_format_version="1",
        status=PayloadStatus.READY,
        opaque_path=Path("payload"),
        sha256="abc",
        created_at=now,
        updated_at=now,
    )
    mapping = segment_text("很长的小说正文。\n" * 5_000, max_chars=16)
    document = Document(uuid4(), "长篇小说", mapping.source_text, 0, now, now, mapping)
    audio = Mock()
    runtime = Mock()
    engines = Mock()
    engines.for_tier.return_value.manifest.engine_id = "test-engine"
    voices = Mock()
    voices.get.return_value = voice
    voices.payloads_for.return_value = (payload,)
    roles = Mock()
    roles.roles_for_units.return_value = {}
    roles.voice_assignments.return_value = {}
    controller = PlaybackController(
        runtime=runtime,
        audio=audio,
        engines=engines,
        voices=voices,
        documents=Mock(),
        roles=roles,
    )
    controller.document = document
    controller.voice_id = voice.voice_id
    controller.tier = Tier.BASIC

    resolved = controller._resolve_units(mapping.units)

    assert len(resolved) == 5_000
    assert voices.get.call_count == 1
    assert voices.payloads_for.call_count == 1


def test_prefetch_failure_drains_completed_audio_before_reporting(qtbot) -> None:
    audio = Mock()
    controller = PlaybackController(
        runtime=Mock(),
        audio=audio,
        engines=Mock(),
        voices=Mock(),
        documents=Mock(),
        roles=Mock(),
    )
    errors: list[str] = []
    completed: list[bool] = []
    controller.error.connect(errors.append)
    controller.finished.connect(lambda: completed.append(True))

    controller._on_runtime_error("下一句生成失败")

    audio.stop.assert_not_called()
    audio.mark_producer_done.assert_called_once_with()
    assert controller.state == "Playing"
    assert errors == []

    controller._on_drained()

    assert controller.state == "Error"
    assert errors == ["下一句生成失败"]
    assert completed == []


def test_pause_is_transport_state_and_survives_background_status(qtbot) -> None:
    audio = Mock()
    controller = PlaybackController(
        runtime=Mock(),
        audio=audio,
        engines=Mock(),
        voices=Mock(),
        documents=Mock(),
        roles=Mock(),
    )
    states: list[tuple[str, str]] = []
    controller.stateChanged.connect(lambda state, message: states.append((state, message)))
    controller.state = "Preparing"

    controller.pause()

    assert controller.is_paused
    assert controller.state == "Paused"
    audio.pause.assert_called_once_with()

    for runtime_state in ("LoadingModel", "Preparing", "Generating", "Playing"):
        controller._set_state(runtime_state, "后台仍在准备")
        assert controller.state == "Paused"

    controller.resume()

    assert not controller.is_paused
    assert controller.state == "Playing"
    audio.resume.assert_called_once_with()
    assert states == [("Paused", "已暂停"), ("Playing", "继续朗读")]
