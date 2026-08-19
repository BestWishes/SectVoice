from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from sectvoice.core.builtin_voices import (
    builtin_voice_asset_root,
    load_builtin_voice_specs,
    seed_builtin_voices,
)
from sectvoice.core.database import Database
from sectvoice.core.settings import SettingsStore
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageError, VoicePackageService, sha256_file
from sectvoice.domain import PayloadStatus, Tier


def _services(tmp_path: Path):
    database = Database(tmp_path / "sectvoice.db")
    database.initialize()
    library = VoiceLibrary(database)
    settings = SettingsStore(database)
    packages = VoicePackageService(library, tmp_path / "voices")
    return database, library, settings, packages


def test_bundled_voice_catalog_matches_approved_packages() -> None:
    root = builtin_voice_asset_root()
    specs = load_builtin_voice_specs(root)

    assert [spec.name for spec in specs] == ["体验女声", "体验男声"]
    assert len({spec.voice_id for spec in specs}) == 2
    for spec in specs:
        assert sha256_file(root / spec.package_name) == spec.package_sha256


def test_first_run_seeds_both_voices_with_basic_and_standard_payloads(
    tmp_path: Path,
) -> None:
    _, library, settings, packages = _services(tmp_path)

    results = seed_builtin_voices(library, packages, settings)

    assert [result.action for result in results] == ["imported", "imported"]
    assert {profile.name for profile in library.list_profiles()} == {
        "体验女声",
        "体验男声",
    }
    for profile in library.list_profiles():
        assert profile.source_audio_path.is_file()
        assert profile.reference_audio_path.is_file()
        assert profile.source_audio_path.resolve().is_relative_to(
            (tmp_path / "voices").resolve()
        )
        payloads = library.payloads_for(profile.voice_id)
        assert {payload.tier for payload in payloads} == {Tier.BASIC, Tier.STANDARD}
        assert all(payload.status is PayloadStatus.READY for payload in payloads)
        assert all(payload.opaque_path.is_dir() for payload in payloads)


def test_existing_voice_is_adopted_without_overwriting_user_name(tmp_path: Path) -> None:
    _, library, settings, packages = _services(tmp_path)
    female = load_builtin_voice_specs()[0]
    packages.import_package(builtin_voice_asset_root() / female.package_name)
    library.rename(female.voice_id, "我改过的名称")

    results = seed_builtin_voices(library, packages, settings)

    by_id = {result.voice_id: result.action for result in results}
    assert by_id[female.voice_id] == "existing"
    assert library.get(female.voice_id).name == "我改过的名称"  # type: ignore[union-attr]


def test_deleted_builtin_voice_is_not_resurrected_on_later_start(tmp_path: Path) -> None:
    _, library, settings, packages = _services(tmp_path)
    seed_builtin_voices(library, packages, settings)
    female = load_builtin_voice_specs()[0]
    library.delete(female.voice_id)
    shutil.rmtree(tmp_path / "voices" / str(female.voice_id))

    results = seed_builtin_voices(library, packages, settings)

    assert library.get(female.voice_id) is None
    assert {result.action for result in results} == {"already_seeded"}


def test_seed_rejects_a_package_that_does_not_match_catalog_hash(tmp_path: Path) -> None:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    (asset_root / "bad.voicepkg").write_bytes(b"not the approved package")
    (asset_root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": "a" * 40,
                "voices": [
                    {
                        "voice_id": "00000000-0000-0000-0000-000000000001",
                        "name": "坏包",
                        "package": "bad.voicepkg",
                        "sha256": "0" * 64,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _, library, settings, packages = _services(tmp_path / "data")

    with pytest.raises(VoicePackageError, match="校验失败"):
        seed_builtin_voices(library, packages, settings, asset_root)
