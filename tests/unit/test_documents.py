from pathlib import Path
from uuid import uuid4

import pytest

from sectvoice.core.database import Database
from sectvoice.reader.documents import DocumentRepository
from sectvoice.reader.segmentation import SEGMENTATION_VERSION


def test_document_persists_lossless_units_and_position(tmp_path: Path) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database, max_speech_unit_chars=32)
    document = repository.create("小说", "第一句。\n\n第二句很长，" + "甲" * 60 + "！")
    assert "".join(item.text for item in document.mapping.units) == document.source_text
    repository.set_position(document.document_id, 4)
    restored = repository.get(document.document_id)
    assert restored.current_position == 4

    edited = repository.update_text(document.document_id, restored.source_text + "尾声。")
    assert edited.document_id == document.document_id
    assert "".join(item.text for item in edited.mapping.units) == edited.source_text
    assert edited.current_position == 4


def test_default_repository_keeps_a_normal_full_sentence_as_one_reader_unit(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database)
    sentence = (
        "而如今，他修炼玄阳决，铸就雄厚根基，武道之路不说一路平坦，"
        "至少不会在一个境界浪费几年光阴，而不得。"
    )
    document = repository.create("完整句", sentence)
    assert len(document.mapping.units) == 1
    assert document.mapping.units[0].text == sentence


def test_document_metadata_operations_do_not_reload_all_speech_units(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database, max_speech_unit_chars=32)
    document = repository.create("长文档", ("这是一个朗读单元。\n" * 2_000))

    # Position updates and text replacement have all metadata they need from a
    # lightweight row query; loading every SpeechUnit here would stall playback.
    monkeypatch.setattr(
        repository,
        "get",
        lambda _document_id: (_ for _ in ()).throw(AssertionError("full document reload")),
    )
    repository.set_position(document.document_id, 10)
    updated = repository.update_text(document.document_id, document.source_text + "尾声。")
    assert updated.current_position == 10
    assert repository.list_summaries()[0].document_id == document.document_id


def test_document_can_be_renamed_without_touching_imported_source(tmp_path: Path) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database)
    imported = tmp_path / "原始名称.txt"
    imported.write_text("原文保持不变。", encoding="utf-8")
    document = repository.create(imported.stem, imported.read_text(encoding="utf-8"))

    summary = repository.rename(document.document_id, "  阅读器显示名称  ")

    assert summary.title == "阅读器显示名称"
    assert repository.get(document.document_id).title == "阅读器显示名称"
    assert imported.name == "原始名称.txt"
    assert imported.read_text(encoding="utf-8") == "原文保持不变。"
    with pytest.raises(ValueError, match="不能为空"):
        repository.rename(document.document_id, "   ")


def test_document_delete_cascades_metadata_but_keeps_shared_cache(tmp_path: Path) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database)
    document = repository.create("待删除", "第一句。第二句。")
    cache_key = "shared-cache"
    voice_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO cache_index(
                cache_key,audio_path,metadata_json,byte_size,duration_seconds,
                created_at,last_accessed_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (cache_key, "shared.wav", "{}", 1, 0.1, "now", "now"),
        )
        connection.execute(
            "INSERT INTO cache_document_refs(document_id,cache_key) VALUES(?,?)",
            (str(document.document_id), cache_key),
        )
        connection.execute(
            """
            INSERT INTO voice_profiles(
                voice_id,name,source_audio_path,reference_audio_path,transcript,language,
                default_settings_json,is_available,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (voice_id, "示例", "source.wav", "reference.wav", "示例", "zh", "{}", 1, "now", "now"),
        )
        connection.execute(
            "INSERT INTO document_role_voices(document_id,role_name,voice_id,tier) VALUES(?,?,?,?)",
            (str(document.document_id), "旁白", voice_id, "basic"),
        )

    repository.delete(document.document_id)

    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM documents WHERE document_id=?", (str(document.document_id),)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM speech_units WHERE document_id=?", (str(document.document_id),)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM cache_document_refs WHERE document_id=?",
            (str(document.document_id),),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM document_role_voices WHERE document_id=?",
            (str(document.document_id),),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM cache_index WHERE cache_key=?", (cache_key,)
        ).fetchone() is not None
    with pytest.raises(KeyError):
        repository.delete(document.document_id)


def test_old_segmentation_is_rebuilt_and_role_range_is_preserved(tmp_path: Path) -> None:
    database = Database(tmp_path / "reader.db")
    database.initialize()
    repository = DocumentRepository(database, max_speech_unit_chars=32)
    document = repository.create("旧映射", "张三说完了。\n　　……\n　　李四回答了。")
    first_unit = document.mapping.units[0]
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO speech_unit_roles(document_id,speech_unit_id,role_name) VALUES(?,?,?)",
            (str(document.document_id), str(first_unit.speech_unit_id), "张三"),
        )
        connection.execute(
            "UPDATE documents SET segmentation_version=0 WHERE document_id=?",
            (str(document.document_id),),
        )
    restored = repository.get(document.document_id)
    assert all(any(character.isalnum() for character in unit.text) for unit in restored.mapping.units)
    with database.connect() as connection:
        version = connection.execute(
            "SELECT segmentation_version FROM documents WHERE document_id=?",
            (str(document.document_id),),
        ).fetchone()[0]
        roles = connection.execute(
            "SELECT role_name FROM speech_unit_roles WHERE document_id=?",
            (str(document.document_id),),
        ).fetchall()
    assert version == SEGMENTATION_VERSION
    assert [row[0] for row in roles] == ["张三"]
