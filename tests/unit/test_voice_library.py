from pathlib import Path
from dataclasses import replace

from sectvoice.core.database import Database
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import (
    EnginePayloadRef,
    PayloadStatus,
    Tier,
    VoiceProfile,
    utc_now_iso,
)


def test_voice_id_survives_rename_and_has_two_private_payloads(tmp_path: Path) -> None:
    database = Database(tmp_path / "sectvoice.db")
    database.initialize()
    library = VoiceLibrary(database)
    profile = VoiceProfile.create(
        "老掌柜",
        tmp_path / "source.wav",
        tmp_path / "reference.wav",
        "今天天气很好。",
    )
    library.add(profile)

    renamed = library.rename(profile.voice_id, "掌柜")
    assert renamed.voice_id == profile.voice_id

    for tier, engine in ((Tier.BASIC, "basic.test"), (Tier.STANDARD, "standard.test")):
        now = utc_now_iso()
        library.upsert_payload(
            EnginePayloadRef(
                voice_id=profile.voice_id,
                tier=tier,
                engine_id=engine,
                engine_version="1.0",
                payload_format_version="private-1",
                status=PayloadStatus.READY,
                opaque_path=tmp_path / tier.value / "opaque",
                sha256="a" * 64,
                created_at=now,
                updated_at=now,
            )
        )

    payloads = library.payloads_for(profile.voice_id)
    assert {item.tier for item in payloads} == {Tier.BASIC, Tier.STANDARD}
    assert all(item.voice_id == profile.voice_id for item in payloads)

    updated = library.replace_reference(
        replace(
            renamed,
            transcript="新的准确转写。",
            reference_audio_path=tmp_path / "new-reference.wav",
            updated_at=utc_now_iso(),
        )
    )
    assert updated.voice_id == profile.voice_id
    assert library.get(profile.voice_id).transcript == "新的准确转写。"  # type: ignore[union-attr]
    assert all(
        item.status is PayloadStatus.MISSING
        for item in library.payloads_for(profile.voice_id)
    )
