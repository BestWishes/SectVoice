from pathlib import Path
import zipfile

from scripts.build_release import (
    SourceFile,
    audit_no_voice_assets,
    included,
    portable_tree_ignore,
    remove_unused_gpl_distance,
    replace_standard_gpu_pacing_hook,
    replace_standard_portable_config,
    write_parts,
)
from scripts.reuse_unchanged_release_assets import reuse_matching_assets


def test_installer_hides_package_helper_and_rechecks_live_space() -> None:
    source = (Path(__file__).parents[2] / "distribution" / "SectVoice.iss").read_text(
        encoding="utf-8"
    )

    assert "GetDiskFreeSpaceExW@kernel32.dll" in source
    assert "ExecAndLogOutput(" in source
    assert "ewWaitUntilTerminated, ResultCode, nil" in source
    assert "RaiseException(TierName" not in source
    assert "CurPageID = wpReady" in source


def test_release_filter_excludes_audio_cache_and_logs(tmp_path: Path) -> None:
    assert included(tmp_path / "worker.py", Path("package/worker.py"))
    assert not included(tmp_path / "voice.wav", Path("assets/voice.wav"))
    assert not included(tmp_path / "download.bin", Path(".cache/download.bin"))
    assert not included(
        tmp_path / "weights.incomplete", Path("models/weights.incomplete")
    )
    assert not included(tmp_path / "worker.pyc", Path("__pycache__/worker.pyc"))
    assert not included(tmp_path / "reader.log", Path("logs/reader.log"))
    assert not included(
        tmp_path / "python.exe", Path(".venv/Scripts/python.exe")
    )
    assert not included(tmp_path / "pyvenv.cfg", Path(".venv/pyvenv.cfg"))
    assert included(
        tmp_path / "numpy.py", Path(".venv/Lib/site-packages/numpy.py")
    )
    ignored = portable_tree_ignore(
        str(tmp_path / ".venv" / "Lib" / "site-packages" / "numpy"),
        ["tests", "__pycache__", "wheel.whl", "core.py"],
    )
    assert ignored == {"tests", "__pycache__", "wheel.whl"}


def test_release_parts_are_independently_extractable_and_hashed(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"a" * 100)
    second.write_bytes(b"b" * 100)
    files = [
        SourceFile(first, "runtime/demo/first.bin", 100, "unused"),
        SourceFile(second, "models/demo/second.bin", 100, "unused"),
    ]

    assets = write_parts(files, tmp_path, "package")

    assert len(assets) == 1
    with zipfile.ZipFile(tmp_path / str(assets[0]["name"])) as archive:
        assert set(archive.namelist()) == {
            "runtime/demo/first.bin",
            "models/demo/second.bin",
        }
    audit_no_voice_assets(tmp_path)


def test_standard_package_replaces_machine_bound_upstream_config(
    tmp_path: Path,
) -> None:
    mutable = tmp_path / "runtime.yaml"
    portable = tmp_path / "portable.yaml"
    mutable.write_text(r"custom:\n  model: H:\\SectVoice\\models", encoding="utf-8")
    portable.write_text("custom:\n  model: portable-default\n", encoding="utf-8")
    archive_path = (
        "runtime/engines/standard/demo/app/GPT_SoVITS/configs/tts_infer.yaml"
    )

    replaced = replace_standard_portable_config(
        [SourceFile(mutable, archive_path, mutable.stat().st_size, "old")],
        portable,
    )

    assert replaced[0].source == portable
    assert replaced[0].archive_path == archive_path
    assert replaced[0].size == portable.stat().st_size


def test_standard_package_excludes_unused_gpl_distance_dependency(tmp_path: Path) -> None:
    keep = tmp_path / "g2p.py"
    blocked_module = tmp_path / "distance.py"
    blocked_metadata = tmp_path / "METADATA"
    for path in (keep, blocked_module, blocked_metadata):
        path.write_text("x", encoding="utf-8")
    files = [
        SourceFile(
            keep,
            "runtime/.venv/Lib/site-packages/g2p_en/g2p.py",
            1,
            "keep",
        ),
        SourceFile(
            blocked_module,
            "runtime/.venv/Lib/site-packages/distance/__init__.py",
            1,
            "blocked",
        ),
        SourceFile(
            blocked_metadata,
            "runtime/.venv/Lib/site-packages/Distance-0.1.3.dist-info/METADATA",
            1,
            "blocked",
        ),
    ]

    filtered = remove_unused_gpl_distance(files)

    assert [item.archive_path for item in filtered] == [files[0].archive_path]


def test_standard_package_injects_pacing_hook_without_changing_model_math(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "t2s_model.py"
    upstream.write_text(
        "class Model:\n"
        "    def infer(self):\n"
        "            y = torch.concat([y, samples], dim=1)\n\n"
        "            if early_stop_num != -1:\n"
        "                pass\n",
        encoding="utf-8",
    )
    archive_path = (
        "runtime/engines/standard/demo/app/GPT_SoVITS/AR/models/t2s_model.py"
    )

    replaced = replace_standard_gpu_pacing_hook(
        [SourceFile(upstream, archive_path, upstream.stat().st_size, "old")],
        tmp_path / "patched",
    )
    patched = replaced[0].source.read_text(encoding="utf-8")

    assert "SECTVOICE_GPU_PACING_HOOK" in patched
    assert "pacing_hook()" in patched
    assert patched.count("y = torch.concat([y, samples], dim=1)") == 1
    assert replaced[0].archive_path == archive_path


def test_reused_relative_asset_can_be_pinned_to_an_old_release_url() -> None:
    current = {
        "assets": [
            {"name": "new.zip", "url": "new.zip", "size": 7, "sha256": "a" * 64}
        ]
    }
    previous = {
        "assets": [
            {"name": "old.zip", "url": "old.zip", "size": 7, "sha256": "a" * 64}
        ]
    }

    reused = reuse_matching_assets(
        current,
        previous,
        previous_base_url="https://example.invalid/releases/download/v1",
    )

    assert reused == 1
    assert current["assets"][0]["name"] == "old.zip"
    assert current["assets"][0]["url"] == (
        "https://example.invalid/releases/download/v1/old.zip"
    )


def test_release_audit_rejects_built_in_voice_audio(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("data/voices/private-test-voice.wav", b"not allowed")

    try:
        audit_no_voice_assets(tmp_path)
    except RuntimeError as exc:
        assert "禁止" in str(exc)
    else:
        raise AssertionError("voice audio must never enter a public asset")


def test_release_audit_rejects_transient_download_cache(tmp_path: Path) -> None:
    archive = tmp_path / "bad-cache.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("models/demo/.cache/weights.incomplete", b"partial")

    try:
        audit_no_voice_assets(tmp_path)
    except RuntimeError as exc:
        assert "禁止" in str(exc)
    else:
        raise AssertionError("download cache must never enter a public asset")


def test_release_audit_rejects_developer_absolute_path(tmp_path: Path) -> None:
    developer_root = tmp_path / "developer-root"
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "runtime/package/package-manifest.json",
            ('{"path": "' + str(developer_root).replace("\\", "\\\\") + '"}').encode(),
        )

    try:
        audit_no_voice_assets(tmp_path, (developer_root,))
    except RuntimeError as exc:
        assert "禁止" in str(exc)
    else:
        raise AssertionError("developer absolute path must not enter a public asset")
