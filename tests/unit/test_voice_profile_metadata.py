from pathlib import Path

from sectvoice.core.database import Database
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import VoiceProfile


def test_reference_selection_metadata_survives_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "voices.db")
    database.initialize()
    library = VoiceLibrary(database)
    profile = VoiceProfile.create(
        "角色",
        tmp_path / "source.mp3",
        tmp_path / "reference.wav",
        "参考文字。",
        reference_start_seconds=2.5,
        reference_end_seconds=9.0,
        reference_sample_rate=48000,
        reference_duration_seconds=6.3,
    )
    library.add(profile)
    restored = library.get(profile.voice_id)
    assert restored is not None
    assert restored.reference_start_seconds == 2.5
    assert restored.reference_end_seconds == 9.0
    assert restored.reference_sample_rate == 48000
    assert restored.reference_duration_seconds == 6.3

