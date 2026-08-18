from pathlib import Path
from uuid import uuid4

from sectvoice.core.database import Database
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import Tier, VoiceProfile
from sectvoice.reader.documents import DocumentRepository
from sectvoice.reader.roles import RoleRepository, suggest_dialogue_roles


def test_role_mapping_stays_in_reader_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    documents = DocumentRepository(database, max_speech_unit_chars=20)
    document = documents.create("对白", "张三说：“你来了。”李四问：“现在走吗？”")
    voice = VoiceProfile.create("张三", tmp_path / "a.wav", tmp_path / "b.wav", "参考文本")
    VoiceLibrary(database).add(voice)
    roles = RoleRepository(database)
    chosen = roles.set_units_role(document.document_id, document.mapping, 5, 10, "张三")
    assert chosen
    roles.assign_voice(document.document_id, "张三", voice.voice_id, Tier.BASIC)
    assert set(roles.roles_for_units(document.document_id).values()) == {"张三"}
    assert roles.voice_assignments(document.document_id)["张三"].voice_id == voice.voice_id


def test_dialogue_suggestion_is_conservative() -> None:
    from sectvoice.reader.segmentation import segment_text

    text = "张三说：“今天天气不错。”这是一句无人署名的叙述。"
    mapping = segment_text(text, max_chars=18)
    suggestions = suggest_dialogue_roles(text, mapping)
    assert "张三" in suggestions.values()
    assert all(unit_id in {unit.speech_unit_id for unit in mapping.units} for unit_id in suggestions)


def test_dialogue_suggestion_maps_long_quote_without_scanning_every_unit() -> None:
    from sectvoice.reader.segmentation import segment_text

    quote = "这是一段会跨越很多朗读单元的对白，" * 20
    text = f"掌柜说道：“{quote}”随后众人安静下来。" + ("普通叙述。" * 5_000)
    mapping = segment_text(text, max_chars=24)
    suggestions = suggest_dialogue_roles(text, mapping)

    assert suggestions
    assert set(suggestions.values()) == {"掌柜"}
