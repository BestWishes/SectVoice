from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from uuid import UUID, uuid4
import zipfile

from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.domain import (
    EnginePayloadRef,
    PayloadStatus,
    Tier,
    VoiceProfile,
    utc_now_iso,
)


VOICE_PACKAGE_SCHEMA = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VoicePackageError(RuntimeError):
    pass


class VoicePackageService:
    """Moves VoiceProfiles without knowing any engine-private payload field."""

    def __init__(self, library: VoiceLibrary, voice_storage_root: Path) -> None:
        self.library = library
        self.voice_storage_root = voice_storage_root

    def export(self, voice_id: UUID, destination: Path) -> Path:
        profile = self.library.get(voice_id)
        if profile is None:
            raise KeyError(voice_id)
        payloads = self.library.payloads_for(voice_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix.lower() != ".voicepkg":
            destination = destination.with_suffix(".voicepkg")

        files: list[dict[str, object]] = []
        payload_entries: list[dict[str, object]] = []
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            source_member = self._add_file(
                archive,
                profile.source_audio_path,
                f"source/original{profile.source_audio_path.suffix.lower()}",
                files,
            )
            reference_member = self._add_file(
                archive,
                profile.reference_audio_path,
                f"reference/reference{profile.reference_audio_path.suffix.lower()}",
                files,
            )
            transcript_member = "transcript/reference.txt"
            transcript_bytes = profile.transcript.encode("utf-8")
            archive.writestr(transcript_member, transcript_bytes)
            files.append(
                {
                    "path": transcript_member,
                    "sha256": hashlib.sha256(transcript_bytes).hexdigest(),
                    "size": len(transcript_bytes),
                }
            )

            preview_root = profile.reference_audio_path.parent.parent / "previews"
            if preview_root.is_dir():
                for path in sorted(item for item in preview_root.rglob("*") if item.is_file()):
                    member = str(
                        PurePosixPath("previews", path.relative_to(preview_root).as_posix())
                    )
                    self._add_file(archive, path, member, files)

            for payload in payloads:
                prefix = PurePosixPath(
                    "payloads",
                    payload.tier.value,
                    payload.engine_id,
                    payload.payload_format_version,
                )
                payload_files: list[str] = []
                if not payload.opaque_path.is_dir():
                    raise VoicePackageError(f"payload directory is missing: {payload.opaque_path}")
                for path in sorted(item for item in payload.opaque_path.rglob("*") if item.is_file()):
                    relative = path.relative_to(payload.opaque_path)
                    member = str(prefix / PurePosixPath(relative.as_posix()))
                    self._add_file(archive, path, member, files)
                    payload_files.append(member)
                payload_entries.append(
                    {
                        "tier": payload.tier.value,
                        "engine_id": payload.engine_id,
                        "engine_version": payload.engine_version,
                        "payload_format_version": payload.payload_format_version,
                        "status": payload.status.value,
                        "sha256": payload.sha256,
                        "files": payload_files,
                    }
                )

            manifest = {
                "schema_version": VOICE_PACKAGE_SCHEMA,
                "voice": {
                    "voice_id": str(profile.voice_id),
                    "name": profile.name,
                    "language": profile.language,
                    "reference_start_seconds": profile.reference_start_seconds,
                    "reference_end_seconds": profile.reference_end_seconds,
                    "reference_sample_rate": profile.reference_sample_rate,
                    "reference_duration_seconds": profile.reference_duration_seconds,
                    "created_at": profile.created_at,
                    "updated_at": profile.updated_at,
                    "default_settings": dict(profile.default_settings),
                    "is_available": profile.is_available,
                    "source_member": source_member,
                    "reference_member": reference_member,
                    "transcript_member": transcript_member,
                },
                "payloads": payload_entries,
                "files": files,
                "exported_at": utc_now_iso(),
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        return destination

    def import_package(self, package_path: Path) -> VoiceProfile:
        self.voice_storage_root.mkdir(parents=True, exist_ok=True)
        staging = self.voice_storage_root / f".import-{uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(package_path, "r") as archive:
                for info in archive.infolist():
                    self._validate_member(info.filename)
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if manifest.get("schema_version") != VOICE_PACKAGE_SCHEMA:
                    raise VoicePackageError("unsupported voice package schema")
                archive.extractall(staging)
            self._verify_files(staging, manifest.get("files", []))
            voice = manifest["voice"]
            voice_id = UUID(voice["voice_id"])
            if self.library.get(voice_id) is not None:
                raise VoicePackageError(f"VoiceId already exists: {voice_id}")
            destination = self.voice_storage_root / str(voice_id)
            if destination.exists():
                raise VoicePackageError(f"voice storage already exists: {destination}")
            staging.rename(destination)
            staging = destination
            transcript = (destination / voice["transcript_member"]).read_text(encoding="utf-8")
            profile = VoiceProfile(
                voice_id=voice_id,
                name=voice["name"],
                source_audio_path=destination / voice["source_member"],
                reference_audio_path=destination / voice["reference_member"],
                transcript=transcript,
                language=voice["language"],
                reference_start_seconds=float(voice.get("reference_start_seconds", 0)),
                reference_end_seconds=float(voice.get("reference_end_seconds", 0)),
                reference_sample_rate=int(voice.get("reference_sample_rate", 0)),
                reference_duration_seconds=float(voice.get("reference_duration_seconds", 0)),
                created_at=voice["created_at"],
                updated_at=voice["updated_at"],
                default_settings=voice.get("default_settings", {}),
                is_available=bool(voice.get("is_available", True)),
            )
            self.library.add(profile)
            now = utc_now_iso()
            for payload in manifest.get("payloads", []):
                opaque_path = destination / "payloads" / payload["tier"] / payload["engine_id"] / payload["payload_format_version"]
                self.library.upsert_payload(
                    EnginePayloadRef(
                        voice_id=voice_id,
                        tier=Tier(payload["tier"]),
                        engine_id=payload["engine_id"],
                        engine_version=payload["engine_version"],
                        payload_format_version=payload["payload_format_version"],
                        status=PayloadStatus(payload["status"]),
                        opaque_path=opaque_path,
                        sha256=payload["sha256"],
                        created_at=now,
                        updated_at=now,
                    )
                )
            return profile
        finally:
            if staging.exists() and staging.name.startswith(".import-"):
                shutil.rmtree(staging)

    @staticmethod
    def _add_file(
        archive: zipfile.ZipFile,
        source: Path,
        member: str,
        files: list[dict[str, object]],
    ) -> str:
        if not source.is_file():
            raise VoicePackageError(f"voice asset is missing: {source}")
        archive.write(source, member)
        files.append({"path": member, "sha256": sha256_file(source), "size": source.stat().st_size})
        return member

    @staticmethod
    def _validate_member(member: str) -> None:
        path = PurePosixPath(member)
        if path.is_absolute() or ".." in path.parts or not member:
            raise VoicePackageError(f"unsafe voice package path: {member}")

    @staticmethod
    def _verify_files(root: Path, entries: list[dict[str, object]]) -> None:
        for entry in entries:
            relative = str(entry["path"])
            VoicePackageService._validate_member(relative)
            path = root / Path(PurePosixPath(relative))
            if not path.is_file():
                raise VoicePackageError(f"voice package file is missing: {relative}")
            if path.stat().st_size != int(entry["size"]):
                raise VoicePackageError(f"voice package size mismatch: {relative}")
            if sha256_file(path) != entry["sha256"]:
                raise VoicePackageError(f"voice package hash mismatch: {relative}")
