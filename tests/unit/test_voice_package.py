import json
from pathlib import Path
from uuid import uuid4
import zipfile

from sectvoice.core.database import Database
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageService, sha256_file
from sectvoice.domain import EnginePayloadRef, PayloadStatus, Tier, VoiceProfile, utc_now_iso


def test_voice_package_round_trip_keeps_private_payload_opaque(tmp_path: Path) -> None:
    source_db = Database(tmp_path / "source.db")
    source_db.initialize()
    source_library = VoiceLibrary(source_db)
    source_audio = tmp_path / "source.wav"
    reference_audio = tmp_path / "reference.wav"
    source_audio.write_bytes(b"RIFF-source")
    reference_audio.write_bytes(b"RIFF-reference")
    profile = VoiceProfile.create("人物甲", source_audio, reference_audio, "参考文本。")
    source_library.add(profile)

    payload_dir = tmp_path / "opaque-basic"
    payload_dir.mkdir()
    private_file = payload_dir / "engine-payload.json"
    private_file.write_text('{"prompt_audio_codes":[[1,2,3]]}', encoding="utf-8")
    now = utc_now_iso()
    source_library.upsert_payload(
        EnginePayloadRef(
            voice_id=profile.voice_id,
            tier=Tier.BASIC,
            engine_id="private.engine",
            engine_version="1",
            payload_format_version="private-v1",
            status=PayloadStatus.READY,
            opaque_path=payload_dir,
            sha256=sha256_file(private_file),
            created_at=now,
            updated_at=now,
        )
    )

    package_path = VoicePackageService(source_library, tmp_path / "unused").export(
        profile.voice_id, tmp_path / "voice.voicepkg"
    )
    with zipfile.ZipFile(package_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "prompt_audio_codes" not in json.dumps(manifest)
        assert any(name.endswith("engine-payload.json") for name in archive.namelist())

    target_db = Database(tmp_path / "target.db")
    target_db.initialize()
    target_library = VoiceLibrary(target_db)
    imported = VoicePackageService(target_library, tmp_path / "imported").import_package(package_path)
    assert imported.voice_id == profile.voice_id
    payloads = target_library.payloads_for(imported.voice_id)
    assert len(payloads) == 1
    assert payloads[0].opaque_path.joinpath("engine-payload.json").is_file()

