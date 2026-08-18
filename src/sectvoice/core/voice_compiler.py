from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
from uuid import UUID, uuid4

from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.reference_analysis import (
    ReferenceQualityReport,
    analyze_reference_samples,
)
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_quality import to_simplified_chinese
from sectvoice.domain import (
    EnginePayloadRef,
    PayloadStatus,
    Tier,
    VoiceProfile,
    utc_now_iso,
)
from sectvoice.engines.process_client import EngineProcessClient


@dataclass(frozen=True, slots=True)
class CompilationTarget:
    tier: Tier
    engine_id: str
    engine_version: str
    payload_format_version: str
    client: EngineProcessClient


class VoiceCompiler:
    """One-time media preparation and private per-engine payload compilation."""

    def __init__(
        self,
        library: VoiceLibrary,
        media: FFmpegProcessor,
        voice_storage_root: Path,
    ) -> None:
        self.library = library
        self.media = media
        self.voice_storage_root = voice_storage_root

    def create_profile(
        self,
        *,
        name: str,
        source_audio: Path,
        selection_start_seconds: float,
        selection_end_seconds: float,
        transcript: str,
        language: str = "zh-CN",
        light_denoise: bool = False,
        raw_asr_transcript: str | None = None,
    ) -> VoiceProfile:
        cleaned_name, cleaned_transcript = self._validate_reference_input(
            name,
            source_audio,
            selection_start_seconds,
            selection_end_seconds,
            transcript,
        )
        voice_id = uuid4()
        voice_dir = self.voice_storage_root / str(voice_id)
        staging_dir = self.voice_storage_root / f"{voice_id}.partial"
        source_dir = staging_dir / "source"
        reference_dir = staging_dir / "reference"
        quality = self._analyze_selection(
            source_audio,
            selection_start_seconds,
            selection_end_seconds,
            cleaned_transcript,
        )
        try:
            source_dir.mkdir(parents=True, exist_ok=False)
            reference_dir.mkdir(parents=True, exist_ok=False)
            copied_source = source_dir / f"original{source_audio.suffix.lower()}"
            shutil.copy2(source_audio, copied_source)
            reference_wav = reference_dir / "reference-48k-mono-s16.wav"
            metadata = self.media.prepare_reference(
                copied_source,
                reference_wav,
                selection_start_seconds,
                selection_end_seconds,
                sample_rate=48000,
                channels=1,
                light_denoise=light_denoise,
            )
            self._write_reference_diagnostics(
                reference_dir,
                quality,
                raw_asr_transcript=raw_asr_transcript,
                final_transcript=cleaned_transcript,
            )
            staging_dir.replace(voice_dir)
        except Exception:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            raise
        copied_source = voice_dir / "source" / copied_source.name
        reference_wav = voice_dir / "reference" / reference_wav.name
        now = utc_now_iso()
        profile = VoiceProfile(
            voice_id=voice_id,
            name=cleaned_name,
            source_audio_path=copied_source,
            reference_audio_path=reference_wav,
            transcript=cleaned_transcript,
            language=language,
            reference_start_seconds=selection_start_seconds,
            reference_end_seconds=selection_end_seconds,
            reference_sample_rate=48000,
            reference_duration_seconds=metadata.duration_seconds,
            created_at=now,
            updated_at=now,
        )
        self.library.add(profile)
        return profile

    def replace_reference(
        self,
        voice_id: UUID,
        *,
        source_audio: Path,
        selection_start_seconds: float,
        selection_end_seconds: float,
        transcript: str,
        language: str = "zh-CN",
        light_denoise: bool = False,
        raw_asr_transcript: str | None = None,
    ) -> VoiceProfile:
        existing = self.library.get(voice_id)
        if existing is None:
            raise KeyError(voice_id)
        _, cleaned_transcript = self._validate_reference_input(
            existing.name,
            source_audio,
            selection_start_seconds,
            selection_end_seconds,
            transcript,
        )
        voice_dir = (self.voice_storage_root / str(voice_id)).resolve()
        if voice_dir.parent != self.voice_storage_root.resolve() or not voice_dir.is_dir():
            raise RuntimeError("声音档案目录不存在或越界")
        token = uuid4().hex
        staging = self.voice_storage_root / f".{voice_id}-reference-{token}.partial"
        source_stage = staging / "source"
        reference_stage = staging / "reference"
        source_stage.mkdir(parents=True)
        reference_stage.mkdir(parents=True)
        copied = source_stage / f"original{source_audio.suffix.lower()}"
        reference = reference_stage / "reference-48k-mono-s16.wav"
        backup_source = voice_dir / f".source-backup-{token}"
        backup_reference = voice_dir / f".reference-backup-{token}"
        old_source = voice_dir / "source"
        old_reference = voice_dir / "reference"
        quality = self._analyze_selection(
            source_audio,
            selection_start_seconds,
            selection_end_seconds,
            cleaned_transcript,
        )
        moved_old_source = False
        moved_old_reference = False
        installed_new_source = False
        installed_new_reference = False
        committed = False
        try:
            shutil.copy2(source_audio, copied)
            metadata = self.media.prepare_reference(
                copied,
                reference,
                selection_start_seconds,
                selection_end_seconds,
                sample_rate=48000,
                channels=1,
                light_denoise=light_denoise,
            )
            self._write_reference_diagnostics(
                reference_stage,
                quality,
                raw_asr_transcript=raw_asr_transcript,
                final_transcript=cleaned_transcript,
            )
            old_source.replace(backup_source)
            moved_old_source = True
            old_reference.replace(backup_reference)
            moved_old_reference = True
            source_stage.replace(old_source)
            installed_new_source = True
            reference_stage.replace(old_reference)
            installed_new_reference = True
            updated = replace(
                existing,
                source_audio_path=old_source / copied.name,
                reference_audio_path=old_reference / reference.name,
                transcript=cleaned_transcript,
                language=language,
                reference_start_seconds=selection_start_seconds,
                reference_end_seconds=selection_end_seconds,
                reference_sample_rate=48000,
                reference_duration_seconds=metadata.duration_seconds,
                updated_at=utc_now_iso(),
                is_available=True,
                last_error=None,
            )
            self.library.replace_reference(updated)
            committed = True
            shutil.rmtree(backup_source, ignore_errors=True)
            shutil.rmtree(backup_reference, ignore_errors=True)
            return updated
        except Exception:
            if not committed:
                if installed_new_source and old_source.exists():
                    shutil.rmtree(old_source)
                if installed_new_reference and old_reference.exists():
                    shutil.rmtree(old_reference)
                if moved_old_source and backup_source.exists() and not old_source.exists():
                    backup_source.replace(old_source)
                if moved_old_reference and backup_reference.exists() and not old_reference.exists():
                    backup_reference.replace(old_reference)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _validate_reference_input(
        name: str,
        source_audio: Path,
        selection_start_seconds: float,
        selection_end_seconds: float,
        transcript: str,
    ) -> tuple[str, str]:
        cleaned_name = name.strip()
        cleaned_transcript = to_simplified_chinese(transcript).strip()
        if not cleaned_name:
            raise ValueError("voice name is required")
        if not cleaned_transcript:
            raise ValueError("reference transcript is required")
        if not source_audio.is_file():
            raise FileNotFoundError(source_audio)
        if source_audio.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a"}:
            raise ValueError("supported reference formats are WAV, MP3, FLAC and M4A")
        if selection_start_seconds < 0 or not 3 <= selection_end_seconds - selection_start_seconds <= 30:
            raise ValueError("reference selection must be between 3 and 30 seconds")
        return cleaned_name, cleaned_transcript

    def _analyze_selection(
        self,
        source_audio: Path,
        start_seconds: float,
        end_seconds: float,
        transcript: str,
    ) -> ReferenceQualityReport:
        samples = self.media.decode_mono_f32(
            source_audio,
            sample_rate=16_000,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        report = analyze_reference_samples(samples, 16_000, transcript=transcript)
        if not report.is_usable:
            raise ValueError("参考片段技术质量不合格：" + "；".join(report.blocking_reasons))
        return report

    @staticmethod
    def _write_reference_diagnostics(
        reference_dir: Path,
        report: ReferenceQualityReport,
        *,
        raw_asr_transcript: str | None,
        final_transcript: str,
    ) -> None:
        payload = {
            "schema_version": 1,
            "raw_asr_transcript": raw_asr_transcript or "",
            "displayed_asr_transcript": (
                to_simplified_chinese(raw_asr_transcript or "")
            ),
            "final_transcript": final_transcript,
            "quality": report.to_dict(),
        }
        (reference_dir / "diagnostics.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def compile_payload(
        self, profile: VoiceProfile, target: CompilationTarget, timeout_seconds: float = 300.0
    ) -> EnginePayloadRef:
        destination = (
            self.voice_storage_root
            / str(profile.voice_id)
            / "payloads"
            / target.tier.value
            / target.engine_id
            / target.payload_format_version
        )
        now = utc_now_iso()
        compiling = EnginePayloadRef(
            voice_id=profile.voice_id,
            tier=target.tier,
            engine_id=target.engine_id,
            engine_version=target.engine_version,
            payload_format_version=target.payload_format_version,
            status=PayloadStatus.COMPILING,
            opaque_path=destination,
            sha256="",
            created_at=now,
            updated_at=now,
        )
        self.library.upsert_payload(compiling)
        try:
            response = target.client.request(
                "compile",
                timeout_seconds=timeout_seconds,
                reference_wav=str(profile.reference_audio_path),
                reference_transcript=profile.transcript,
                language=profile.language,
                destination=str(destination),
            )
            result = response["result"]
            ready = EnginePayloadRef(
                voice_id=profile.voice_id,
                tier=target.tier,
                engine_id=target.engine_id,
                engine_version=target.engine_version,
                payload_format_version=str(result.get("payload_format_version") or target.payload_format_version),
                status=PayloadStatus.READY,
                opaque_path=Path(result.get("opaque_path") or destination),
                sha256=str(result["sha256"]),
                created_at=now,
                updated_at=utc_now_iso(),
            )
            self.library.upsert_payload(ready)
            return ready
        except Exception as exc:
            failed = EnginePayloadRef(
                voice_id=profile.voice_id,
                tier=target.tier,
                engine_id=target.engine_id,
                engine_version=target.engine_version,
                payload_format_version=target.payload_format_version,
                status=PayloadStatus.ERROR,
                opaque_path=destination,
                sha256="",
                created_at=now,
                updated_at=utc_now_iso(),
                last_error=str(exc),
            )
            self.library.upsert_payload(failed)
            raise
