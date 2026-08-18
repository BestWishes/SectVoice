from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from sectvoice.core.reader_update import (
    ReaderUpdateError,
    download_reader_update,
    is_version_newer,
    load_reader_update,
)


def _manifest(tmp_path: Path, *, bad_hash: bool = False) -> Path:
    installer = tmp_path / "SectVoice-Setup-0.2.1-x64.exe"
    installer.write_bytes(b"real installer bytes")
    manifest = tmp_path / "reader-update.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "0.2.1",
                "release_notes_url": "https://example.test/release",
                "installer": {
                    "name": installer.name,
                    "url": installer.name,
                    "size": installer.stat().st_size,
                    "sha256": "0" * 64
                    if bad_hash
                    else hashlib.sha256(installer.read_bytes()).hexdigest(),
                    "signed": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_reader_update_manifest_downloads_and_verifies(tmp_path: Path) -> None:
    update = load_reader_update(_manifest(tmp_path))
    progress: list[tuple[int, int]] = []

    downloaded = download_reader_update(
        update,
        tmp_path / "downloads",
        progress=lambda current, total: progress.append((current, total)),
    )

    assert downloaded.read_bytes() == b"real installer bytes"
    assert progress[-1] == (update.installer_size, update.installer_size)


def test_reader_update_rejects_wrong_hash_and_removes_full_corrupt_partial(
    tmp_path: Path,
) -> None:
    update = load_reader_update(_manifest(tmp_path, bad_hash=True))
    destination = tmp_path / "downloads"

    with pytest.raises(ReaderUpdateError, match="SHA-256"):
        download_reader_update(update, destination)

    assert not list(destination.glob("*.partial"))


def test_semantic_reader_version_comparison() -> None:
    assert is_version_newer("0.2.1", "0.2.0")
    assert is_version_newer("1.0", "0.9.9")
    assert not is_version_newer("0.2.1", "0.2.1")
    assert not is_version_newer("0.2.0", "0.2.1")


def test_reader_update_rejects_an_invalid_version(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["version"] = "latest; run this"
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ReaderUpdateError, match="版本号"):
        load_reader_update(manifest)


def test_reader_update_space_check_subtracts_retained_partial(
    tmp_path: Path, monkeypatch
) -> None:
    update = load_reader_update(_manifest(tmp_path))
    destination_root = tmp_path / "downloads"
    destination_root.mkdir()
    partial = destination_root / f"{update.installer_name}.partial"
    retained = max(1, update.installer_size // 2)
    partial.write_bytes(b"x" * retained)
    required = update.installer_size - retained + 128 * 1024**2
    monkeypatch.setattr(
        "sectvoice.core.reader_update.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(
            total=required, used=0, free=required
        ),
    )
    monkeypatch.setattr(
        "sectvoice.core.reader_update.download_asset",
        lambda *_args, **_kwargs: destination_root / update.installer_name,
    )

    assert download_reader_update(update, destination_root) == (
        destination_root / update.installer_name
    )
