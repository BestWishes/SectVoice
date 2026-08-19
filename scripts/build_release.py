from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile


AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cmd",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".pth",
    ".py",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
SKIP_DIRECTORIES = {
    ".cache",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "tests",
    "test",
}
SKIP_SUFFIXES = {".incomplete", ".pyc", ".pyo", ".log"} | AUDIO_SUFFIXES
PORTABLE_VENV_SKIP_FILES = {"pyvenv.cfg", ".lock", ".gitignore", "cachedir.tag"}
PORTABLE_TREE_SKIP_DIRECTORIES = {".cache", ".pytest_cache", "__pycache__", "test", "tests"}
PART_UNCOMPRESSED_LIMIT = 1200 * 1024**2
GITHUB_ASSET_LIMIT = 2 * 1024**3


@dataclass(frozen=True, slots=True)
class SourceFile:
    source: Path
    archive_path: str
    size: int
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def included(path: Path, relative: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in relative.parts)
    if ".venv" in lowered_parts:
        venv_index = lowered_parts.index(".venv")
        if (
            len(lowered_parts) > venv_index + 1
            and lowered_parts[venv_index + 1] == "scripts"
        ):
            return False
        if path.name.lower() in PORTABLE_VENV_SKIP_FILES:
            return False
    return (
        not any(part.lower() in SKIP_DIRECTORIES for part in relative.parts)
        and path.suffix.lower() not in SKIP_SUFFIXES
        and path.name != ".sectvoice-write-probe"
    )


def portable_tree_ignore(_directory: str, names: list[str]) -> set[str]:
    """Drop machine-bound virtualenv launchers and transient bytecode."""

    in_venv_root = Path(_directory).name.lower() == ".venv"
    return {
        name
        for name in names
        if (in_venv_root and name.lower() in {"scripts", *PORTABLE_VENV_SKIP_FILES})
        or name.lower() in PORTABLE_TREE_SKIP_DIRECTORIES
        or Path(name).suffix.lower() in {".pyc", ".pyo", ".log", ".whl"}
    }


def python_base_ignore(directory: str, names: list[str]) -> set[str]:
    path = Path(directory)
    lowered = {name.lower(): name for name in names}
    ignored = {
        name
        for name in names
        if name.lower()
        in {
            "scripts",
            "__pycache__",
            "ensurepip",
            "idlelib",
            "test",
            "tests",
            "tkinter",
            "turtledemo",
        }
        or Path(name).suffix.lower() in ({".pyc", ".pyo", ".log"} | AUDIO_SUFFIXES)
    }
    if path.name.lower() == "lib" and "site-packages" in lowered:
        ignored.add(lowered["site-packages"])
    return ignored


def collect_tree(source: Path, archive_root: str) -> list[SourceFile]:
    result: list[SourceFile] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if not included(path, relative):
            continue
        archive_path = (Path(archive_root) / relative).as_posix()
        result.append(
            SourceFile(path, archive_path, path.stat().st_size, sha256_file(path))
        )
    return result


def write_parts(
    files: list[SourceFile], output: Path, name_prefix: str
) -> list[dict[str, object]]:
    batches: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    current_size = 0
    for item in files:
        if item.size >= GITHUB_ASSET_LIMIT:
            raise RuntimeError(f"单文件超过GitHub Release限制：{item.source}")
        if current and current_size + item.size > PART_UNCOMPRESSED_LIMIT:
            batches.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += item.size
    if current:
        batches.append(current)
    assets: list[dict[str, object]] = []
    for index, batch in enumerate(batches, 1):
        name = f"{name_prefix}-part{index:02d}.zip"
        path = output / name
        with zipfile.ZipFile(
            path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            for item in batch:
                archive.write(item.source, item.archive_path)
        size = path.stat().st_size
        if size >= GITHUB_ASSET_LIMIT:
            raise RuntimeError(f"Release资产超过2GiB：{path}")
        assets.append(
            {"name": name, "url": name, "size": size, "sha256": sha256_file(path)}
        )
        print(f"built {name}: {size / 1024**3:.3f} GiB", flush=True)
    return assets


def replace_standard_portable_config(
    files: list[SourceFile], portable_config: Path
) -> list[SourceFile]:
    """Keep upstream's mutable local config out of portable public packages."""

    portable_archive_suffix = "app/GPT_SoVITS/configs/tts_infer.yaml"
    portable_size = portable_config.stat().st_size
    portable_sha256 = sha256_file(portable_config)
    return [
        SourceFile(
            portable_config,
            item.archive_path,
            portable_size,
            portable_sha256,
        )
        if item.archive_path.endswith(portable_archive_suffix)
        else item
        for item in files
    ]


def remove_unused_gpl_distance(files: list[SourceFile]) -> list[SourceFile]:
    """Exclude g2p_en's unused GPL `Distance` dependency from Standard.

    g2p_en 2.1.0 declares Distance>=0.1.3 in package metadata, but its shipped
    runtime code does not import it.  Keeping that uncalled package would add
    GPL-2.0-or-later code to the Standard Python environment for no behavior.
    The g2p_en package itself and every dependency it actually imports remain.
    """

    blocked_parts = {"distance", "distance-0.1.3.dist-info"}
    return [
        item
        for item in files
        if not blocked_parts.intersection(
            part.lower() for part in Path(item.archive_path).parts
        )
    ]


def replace_standard_gpu_pacing_hook(
    files: list[SourceFile], temporary_root: Path
) -> list[SourceFile]:
    """Inject the pinned, no-audio-change pacing hook into upstream GPT-SoVITS.

    The public package keeps upstream code under its original private engine
    runtime.  Only a callback lookup is inserted into the autoregressive token
    loop; runtime_engine.py owns the policy and removes the callback after each
    request.  Exact matching deliberately stops a release when the pinned
    upstream implementation changes instead of silently shipping no hook.
    """

    archive_suffix = "app/GPT_SoVITS/AR/models/t2s_model.py"
    marker = "_sectvoice_compute_pacing_hook"
    needle = """            y = torch.concat([y, samples], dim=1)\n\n            if early_stop_num != -1"""
    replacement = """            y = torch.concat([y, samples], dim=1)\n\n            # SECTVOICE_GPU_PACING_HOOK: scheduling only; tensors and RNG stay unchanged.\n            pacing_hook = getattr(self, \"_sectvoice_compute_pacing_hook\", None)\n            if pacing_hook is not None:\n                pacing_hook()\n\n            if early_stop_num != -1"""
    result: list[SourceFile] = []
    replaced = False
    for item in files:
        if not item.archive_path.endswith(archive_suffix):
            result.append(item)
            continue
        source = item.source.read_text(encoding="utf-8")
        if marker not in source:
            if needle not in source:
                raise RuntimeError("GPT-SoVITS固定版本的token循环已变化，拒绝遗漏GPU平滑钩子")
            source = source.replace(needle, replacement, 1)
        patched = temporary_root / "t2s_model.py"
        patched.parent.mkdir(parents=True, exist_ok=True)
        patched.write_text(source, encoding="utf-8", newline="\n")
        result.append(
            SourceFile(
                patched,
                item.archive_path,
                patched.stat().st_size,
                sha256_file(patched),
            )
        )
        replaced = True
    if not replaced:
        raise RuntimeError("Standard运行时缺少GPT-SoVITS token模型文件")
    return result


def build_engine_package(
    root: Path,
    source_root: Path,
    output: Path,
    tier: str,
    release_version: str,
) -> dict[str, object]:
    installed = json.loads(
        (source_root / "distribution" / "installed-packages.json").read_text(
            encoding="utf-8"
        )
    )[tier]
    runtime_relative = Path(installed["runtime"])
    model_relative = Path(installed["model"])
    manifest_relative = Path(installed["manifest"])
    runtime_source = root / runtime_relative
    model_source = root / model_relative
    manifest = json.loads((root / manifest_relative).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(dir=output, prefix="engine-patch-") as temporary:
        files = collect_tree(runtime_source, runtime_relative.as_posix())
        if tier == "standard":
            files = remove_unused_gpl_distance(files)
            portable_config = source_root / "distribution" / "tts_infer.portable.yaml"
            files = replace_standard_portable_config(files, portable_config)
            files = replace_standard_gpu_pacing_hook(files, Path(temporary))
        files += collect_tree(model_source, model_relative.as_posix())
        if tier == "standard":
            nltk_source = root / "cache" / "nltk_data"
            if nltk_source.is_dir():
                nltk_target = runtime_relative / ".nltk_data"
                files += collect_tree(nltk_source, nltk_target.as_posix())
        archive_label = "Basic" if tier == "basic" else "Standard"
        assets = write_parts(
            files,
            output,
            f"SectVoice-{archive_label}-{release_version}",
        )
    return {
        "package_id": manifest["package_id"],
        "tier": tier,
        "display_name": manifest.get("display_name", f"{archive_label}语音包"),
        "version": str(manifest["engine_version"]),
        "manifest_relative_path": manifest_relative.as_posix(),
        "runtime_relative_path": runtime_relative.as_posix(),
        "model_relative_path": model_relative.as_posix(),
        "download_size": sum(int(item["size"]) for item in assets),
        "installed_size": sum(item.size for item in files),
        "requirements": (
            {"device": "cpu", "minimum_memory_gib": 8}
            if tier == "basic"
            else {"device": "cuda", "minimum_vram_gib": 4, "nvidia_required": True}
        ),
        "upstream": manifest.get("upstream", {}),
        "assets": assets,
        "files": [
            {"path": item.archive_path, "size": item.size, "sha256": item.sha256}
            for item in files
        ],
    }


def copy_core(root: Path, source_root: Path, output: Path, app_source: Path) -> Path:
    core = output / "core-root"
    if core.exists():
        raise RuntimeError(f"Core staging已存在，拒绝覆盖：{core}")
    shutil.copytree(app_source, core / "app")
    shutil.copytree(root / "runtime" / "ffmpeg", core / "runtime" / "ffmpeg")
    shutil.copytree(
        root / "runtime" / "asr",
        core / "runtime" / "asr",
        ignore=portable_tree_ignore,
    )
    shutil.copytree(
        root / "models" / "asr",
        core / "models" / "asr",
        ignore=portable_tree_ignore,
    )
    common = core / "runtime" / "common"
    common.mkdir(parents=True)
    python_base = Path(sys.base_prefix).resolve()
    if not (python_base / "python.exe").is_file():
        raise RuntimeError(f"找不到用于发行的CPython基础运行时：{python_base}")
    shutil.copytree(
        python_base,
        common / "python310",
        ignore=python_base_ignore,
    )
    shutil.copy2(source_root / "scripts" / "asr_worker.py", common / "asr_worker.py")
    shutil.copy2(source_root / "LICENSE", core / "LICENSE.txt")
    shutil.copy2(source_root / "NOTICE", core / "NOTICE.txt")
    shutil.copy2(
        source_root / "THIRD_PARTY_NOTICES.md",
        core / "THIRD_PARTY_NOTICES.md",
    )
    shutil.copy2(
        source_root / "distribution" / "FFMPEG_SOURCE.md",
        core / "FFMPEG_SOURCE.md",
    )
    shutil.copytree(
        source_root / "distribution" / "licenses",
        core / "licenses",
    )
    for name in (
        "README-INSTALL.txt",
        "Start-SectVoice.cmd",
    ):
        shutil.copy2(source_root / "distribution" / name, core / name)
    return core


def zip_core(core: Path, output: Path, version: str) -> dict[str, object]:
    name = f"SectVoice-Reader-Core-{version}-x64.zip"
    path = output / name
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as archive:
        for file in sorted(core.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(core).as_posix())
    if path.stat().st_size >= GITHUB_ASSET_LIMIT:
        raise RuntimeError("Reader Core ZIP超过GitHub 2GiB资产限制")
    return {
        "name": name,
        "size": path.stat().st_size,
        "installed_size": sum(
            file.stat().st_size for file in core.rglob("*") if file.is_file()
        ),
        "sha256": sha256_file(path),
    }


def audit_no_voice_assets(output: Path, private_roots: tuple[Path, ...] = ()) -> None:
    bad: list[str] = []
    markers: list[bytes] = []
    for root in private_roots:
        resolved = str(root.resolve())
        markers.extend(
            (
                resolved.encode("utf-8"),
                resolved.replace("\\", "\\\\").encode("utf-8"),
                resolved.replace("\\", "/").encode("utf-8"),
            )
        )
    for archive_path in output.glob("*.zip"):
        with zipfile.ZipFile(archive_path) as archive:
            approved_voices = _approved_builtin_voices(archive, bad)
            for info in archive.infolist():
                name = info.filename
                if Path(name).suffix.lower() in AUDIO_SUFFIXES:
                    bad.append(f"{archive_path.name}:{name}")
                lowered = name.lower()
                if ".cache" in tuple(part.lower() for part in Path(name).parts) or Path(
                    name
                ).suffix.lower() == ".incomplete":
                    bad.append(f"{archive_path.name}:{name}:transient-cache")
                if "voicepkg" in lowered:
                    approved = approved_voices.get(PurePosixPath(name).name)
                    if approved is None or not _is_builtin_voice_member(name):
                        bad.append(f"{archive_path.name}:{name}")
                    else:
                        _audit_builtin_voice_package(
                            archive_path.name,
                            name,
                            archive.read(info),
                            approved,
                            markers,
                            bad,
                        )
                elif "sectvoice.db" in lowered:
                    bad.append(f"{archive_path.name}:{name}")
                if (
                    Path(name).suffix.lower() in TEXT_SUFFIXES
                    and info.file_size <= 16 * 1024**2
                ):
                    content = archive.read(info)
                    if any(marker in content for marker in markers):
                        bad.append(f"{archive_path.name}:{name}:private-text")
    for path in output.iterdir():
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            content = path.read_bytes()
            if any(marker in content for marker in markers):
                bad.append(f"{path.name}:private-text")
    if bad:
        raise RuntimeError("公开资产包含禁止的声音/数据文件：" + ", ".join(bad[:10]))


def _approved_builtin_voices(
    archive: zipfile.ZipFile, bad: list[str]
) -> dict[str, dict[str, object]]:
    catalogs = [
        info
        for info in archive.infolist()
        if tuple(PurePosixPath(info.filename).parts[-4:])
        == ("sectvoice", "assets", "builtin_voices", "catalog.json")
    ]
    if not catalogs:
        return {}
    if len(catalogs) != 1:
        bad.append(f"{Path(archive.filename or 'archive').name}:duplicate-builtin-catalog")
        return {}
    try:
        catalog = json.loads(archive.read(catalogs[0]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        bad.append(f"{Path(archive.filename or 'archive').name}:invalid-builtin-catalog")
        return {}
    if catalog.get("schema_version") != 1:
        bad.append(f"{Path(archive.filename or 'archive').name}:unsupported-builtin-catalog")
        return {}
    approved: dict[str, dict[str, object]] = {}
    for item in catalog.get("voices") or ():
        package_name = str(item.get("package") or "")
        digest = str(item.get("sha256") or "").lower()
        if (
            PurePosixPath(package_name).name != package_name
            or not package_name.endswith(".voicepkg")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or package_name in approved
        ):
            bad.append(
                f"{Path(archive.filename or 'archive').name}:invalid-builtin-entry"
            )
            continue
        approved[package_name] = dict(item)
    return approved


def _is_builtin_voice_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return len(parts) >= 4 and tuple(parts[-4:-1]) == (
        "sectvoice",
        "assets",
        "builtin_voices",
    )


def _audit_builtin_voice_package(
    outer_name: str,
    member_name: str,
    payload: bytes,
    approved: dict[str, object],
    private_markers: list[bytes],
    bad: list[str],
) -> None:
    label = f"{outer_name}:{member_name}"
    if hashlib.sha256(payload).hexdigest() != str(approved["sha256"]):
        bad.append(f"{label}:builtin-hash")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            names = set(package.namelist())
            if "manifest.json" not in names:
                bad.append(f"{label}:missing-manifest")
                return
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
            voice = manifest.get("voice") or {}
            if (
                str(voice.get("voice_id")) != str(approved.get("voice_id"))
                or voice.get("name") != approved.get("name")
            ):
                bad.append(f"{label}:identity")
            listed: set[str] = set()
            for entry in manifest.get("files") or ():
                nested_name = str(entry.get("path") or "")
                nested_path = PurePosixPath(nested_name)
                if nested_path.is_absolute() or ".." in nested_path.parts:
                    bad.append(f"{label}:unsafe-path")
                    continue
                listed.add(nested_name)
                try:
                    content = package.read(nested_name)
                except KeyError:
                    bad.append(f"{label}:missing-file")
                    continue
                if len(content) != int(entry.get("size") or -1):
                    bad.append(f"{label}:size")
                if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                    bad.append(f"{label}:nested-hash")
                if any(marker in content for marker in private_markers):
                    bad.append(f"{label}:private-content")
            if listed != names - {"manifest.json"}:
                bad.append(f"{label}:unlisted-content")
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile):
        bad.append(f"{label}:invalid-package")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build immutable SectVoice release assets")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="源码目录；默认<SectVoiceRoot>/source，便于从只读模型根构建公开快照",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        help="发布工作目录；默认<SectVoiceRoot>/release，磁盘不足时可显式改到其它盘",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--app-source", type=Path)
    parser.add_argument("--skip-core", action="store_true")
    parser.add_argument("--skip-engines", action="store_true")
    parser.add_argument("--reuse-engine-catalog", type=Path)
    parser.add_argument("--reuse-engine-base-url")
    args = parser.parse_args()
    root = args.root.resolve()
    source_root = (
        args.source_root.resolve()
        if args.source_root is not None
        else (root / "source").resolve()
    )
    if not (source_root / "pyproject.toml").is_file():
        raise RuntimeError(f"无效源码目录：{source_root}")
    output = args.output.resolve()
    release_root = (
        args.release_root.resolve()
        if args.release_root is not None
        else (root / "release").resolve()
    )
    try:
        output.relative_to(release_root)
    except ValueError as exc:
        raise RuntimeError("构建输出必须位于<SectVoiceRoot>/release内") from exc
    output.mkdir(parents=True, exist_ok=False)
    packages: list[dict[str, object]] = []
    core_asset: dict[str, object] | None = None
    if args.reuse_engine_catalog is not None:
        prior = json.loads(
            args.reuse_engine_catalog.resolve().read_text(encoding="utf-8")
        )
        packages = list(prior.get("packages") or [])
        if not packages:
            raise RuntimeError("复用目录中没有语音包")
        if args.reuse_engine_base_url:
            base = args.reuse_engine_base_url.rstrip("/") + "/"
            for package in packages:
                for asset in package["assets"]:
                    if "://" not in str(asset["url"]):
                        asset["url"] = base + str(asset["url"])
    elif not args.skip_engines:
        for tier in ("basic", "standard"):
            packages.append(
                build_engine_package(root, source_root, output, tier, args.version)
            )
    if not args.skip_core:
        if args.app_source is None or not args.app_source.is_dir():
            raise RuntimeError("构建Reader Core必须提供有效--app-source PyInstaller目录")
        core = copy_core(root, source_root, output, args.app_source.resolve())
        core_asset = zip_core(core, output, args.version)
    catalog = {
        "schema_version": 1,
        "reader_min_version": args.version,
        "release_version": args.version,
        "packages": packages,
    }
    (output / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {"version": args.version, "core": core_asset, "packages": packages}
    (output / "release-manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    audit_no_voice_assets(output, (root, Path.home()))
    checksum_lines = []
    for path in sorted(output.glob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksum_lines.append(f"{sha256_file(path)}  {path.name}")
    (output / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
