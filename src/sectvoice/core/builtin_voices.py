from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import UUID
import zipfile

from sectvoice.core.settings import SettingsStore
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageError, VoicePackageService


BUILTIN_VOICE_CATALOG_SCHEMA = 1
BUILTIN_VOICE_SEED_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class BuiltinVoiceSpec:
    voice_id: UUID
    name: str
    package_name: str
    package_sha256: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class BuiltinVoiceSeedResult:
    voice_id: UUID
    action: str


def builtin_voice_asset_root() -> Path:
    """Return the read-only package resources in source and frozen builds."""

    return Path(__file__).resolve().parents[1] / "assets" / "builtin_voices"


def load_builtin_voice_specs(asset_root: Path | None = None) -> tuple[BuiltinVoiceSpec, ...]:
    root = (asset_root or builtin_voice_asset_root()).resolve()
    catalog_path = root / "catalog.json"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"找不到内置声音目录：{catalog_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != BUILTIN_VOICE_CATALOG_SCHEMA:
        raise VoicePackageError("unsupported built-in voice catalog schema")

    specs: list[BuiltinVoiceSpec] = []
    seen_ids: set[UUID] = set()
    seen_packages: set[str] = set()
    source_revision = str(catalog.get("source_revision") or "").strip()
    if len(source_revision) != 40:
        raise VoicePackageError("built-in voice source revision is invalid")
    for item in catalog.get("voices") or ():
        voice_id = UUID(str(item["voice_id"]))
        name = str(item["name"]).strip()
        package_name = str(item["package"]).strip()
        package_sha256 = str(item["sha256"]).strip().lower()
        if not name:
            raise VoicePackageError("built-in voice name is required")
        if Path(package_name).name != package_name or not package_name.endswith(".voicepkg"):
            raise VoicePackageError(f"unsafe built-in voice package name: {package_name}")
        if len(package_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in package_sha256
        ):
            raise VoicePackageError(f"invalid built-in voice package hash: {package_name}")
        if voice_id in seen_ids or package_name in seen_packages:
            raise VoicePackageError("duplicate built-in voice catalog entry")
        seen_ids.add(voice_id)
        seen_packages.add(package_name)
        specs.append(
            BuiltinVoiceSpec(
                voice_id=voice_id,
                name=name,
                package_name=package_name,
                package_sha256=package_sha256,
                source_revision=source_revision,
            )
        )
    if not specs:
        raise VoicePackageError("built-in voice catalog is empty")
    return tuple(specs)


def seed_builtin_voices(
    library: VoiceLibrary,
    packages: VoicePackageService,
    settings: SettingsStore,
    asset_root: Path | None = None,
) -> tuple[BuiltinVoiceSeedResult, ...]:
    """Import each approved default exactly once without resurrecting deletions."""

    root = (asset_root or builtin_voice_asset_root()).resolve()
    results: list[BuiltinVoiceSeedResult] = []
    for spec in load_builtin_voice_specs(root):
        marker_key = _seed_marker_key(spec.voice_id)
        marker = settings.get(marker_key)
        if isinstance(marker, dict) and marker.get("completed") is True:
            results.append(BuiltinVoiceSeedResult(spec.voice_id, "already_seeded"))
            continue

        existing = library.get(spec.voice_id)
        if existing is None:
            package_path = root / spec.package_name
            _validate_builtin_package(package_path, spec)
            imported = packages.import_package(package_path)
            if imported.voice_id != spec.voice_id or imported.name != spec.name:
                raise VoicePackageError(
                    f"内置声音身份不匹配：{spec.package_name}"
                )
            action = "imported"
        else:
            # An upgrade may encounter the same stable VoiceId already created by
            # the project owner. Preserve all user edits and merely adopt it.
            action = "existing"

        settings.set(
            marker_key,
            {
                "completed": True,
                "schema_version": BUILTIN_VOICE_SEED_SCHEMA,
                "package": spec.package_name,
                "package_sha256": spec.package_sha256,
                "source_revision": spec.source_revision,
            },
        )
        results.append(BuiltinVoiceSeedResult(spec.voice_id, action))
    return tuple(results)


def _seed_marker_key(voice_id: UUID) -> str:
    return f"builtin_voice_seed:{BUILTIN_VOICE_SEED_SCHEMA}:{voice_id}"


def _validate_builtin_package(package_path: Path, spec: BuiltinVoiceSpec) -> None:
    if not package_path.is_file():
        raise FileNotFoundError(f"内置声音包不存在：{package_path}")
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    if digest != spec.package_sha256:
        raise VoicePackageError(f"内置声音包校验失败：{spec.package_name}")
    try:
        with zipfile.ZipFile(package_path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise VoicePackageError(f"内置声音包清单损坏：{spec.package_name}") from exc
    voice = manifest.get("voice") or {}
    if str(voice.get("voice_id")) != str(spec.voice_id) or voice.get("name") != spec.name:
        raise VoicePackageError(f"内置声音包身份不匹配：{spec.package_name}")
