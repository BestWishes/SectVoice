from __future__ import annotations

import logging
from pathlib import Path
import shutil
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from sectvoice.core.asr import ASRService
from sectvoice.core.audio_buffer import StreamingAudioBuffer
from sectvoice.core.cache import VoiceCache
from sectvoice.core.database import Database
from sectvoice.core.diagnostics import run_startup_diagnostics
from sectvoice.core.engine_manager import EngineManager
from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_installer import PackageInstaller
from sectvoice.core.runtime import VoiceRuntime
from sectvoice.core.settings import SettingsStore
from sectvoice.core.voice_compiler import VoiceCompiler
from sectvoice.core.voice_library import VoiceLibrary
from sectvoice.core.voice_package import VoicePackageService
from sectvoice.paths import AppPaths
from sectvoice.reader.audio_output import AudioOutput
from sectvoice.reader.documents import DocumentRepository
from sectvoice.reader.playback_controller import PlaybackController
from sectvoice.reader.roles import RoleRepository
from sectvoice.ui.main_window import MainWindow, MainWindowServices
from sectvoice.ui.startup_wizard import StartupWizard


def build_services(paths: AppPaths) -> MainWindowServices:
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    documents = DocumentRepository(database)
    voices = VoiceLibrary(database)
    roles = RoleRepository(database)
    settings = SettingsStore(database)
    packages = ModelPackageManager(database)
    package_installer = PackageInstaller(packages)
    engines = EngineManager(paths, packages)
    media = FFmpegProcessor(paths.runtime / "ffmpeg" / "bin" / "ffmpeg.exe")
    cache = VoiceCache(database, paths.cache / "audio")
    compiler = VoiceCompiler(voices, media, paths.data / "voices")
    voice_packages = VoicePackageService(voices, paths.data / "voices")
    portable_python = paths.runtime / "common" / "python310" / "python.exe"
    asr_runtime = paths.runtime / "asr" / "faster-whisper" / "1.2.1"
    asr = ASRService(
        paths,
        (
            portable_python
            if portable_python.is_file()
            else asr_runtime / ".venv" / "Scripts" / "python.exe"
        ),
        _asr_worker_script(paths),
        paths.models / "asr" / "faster-whisper-small" / "536b0662",
        (
            (asr_runtime / ".venv" / "Lib" / "site-packages",)
            if portable_python.is_file()
            else ()
        ),
    )
    buffer = StreamingAudioBuffer()
    audio = AudioOutput(buffer)
    runtime = VoiceRuntime(
        engines=engines,
        cache=cache,
        buffer=buffer,
        temp_root=paths.temp / "runtime-pcm",
        media=media,
        alignment_asr=asr,
    )
    playback = PlaybackController(
        runtime=runtime,
        audio=audio,
        engines=engines,
        voices=voices,
        documents=documents,
        roles=roles,
    )
    return MainWindowServices(
        paths=paths,
        documents=documents,
        voices=voices,
        roles=roles,
        settings=settings,
        packages=packages,
        package_installer=package_installer,
        engines=engines,
        media=media,
        asr=asr,
        compiler=compiler,
        voice_packages=voice_packages,
        cache=cache,
        playback=playback,
    )


def _asr_worker_script(paths: AppPaths) -> Path:
    """Locate the external ASR worker in both source and installed layouts."""

    candidates = (
        paths.source / "scripts" / "asr_worker.py",
        paths.runtime / "common" / "asr_worker.py",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SectVoice Reader")
    app.setOrganizationName("SectVoice")
    paths = AppPaths.discover()
    log_dir = paths.data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "reader.log",
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # A completed installer is only a transient handoff.  Preserve .partial
    # files so an interrupted Reader update can resume after a restart, while
    # reclaiming already-launched or user-cancelled complete installers.
    reader_update_downloads = paths.downloads / "reader-updates"
    if reader_update_downloads.is_dir():
        try:
            for installer in reader_update_downloads.rglob("*.exe"):
                if not installer.name.lower().endswith(".exe.partial"):
                    installer.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).exception(
                "failed to clean completed Reader update downloads: %s",
                reader_update_downloads,
            )
    try:
        services = build_services(paths)
        diagnostics = lambda: run_startup_diagnostics(
            paths, services.media, services.packages, services.asr.is_available
        )
        if not services.settings.get("startup_completed", False):
            wizard = StartupWizard(diagnostics)
            if wizard.exec() != StartupWizard.DialogCode.Accepted:
                services.asr.shutdown()
                services.engines.shutdown()
                return 1
            services.settings.set("startup_completed", True)
        window = MainWindow(services)
        window.show()
        return app.exec()
    except Exception as exc:
        logging.exception("SectVoice startup failed")
        QMessageBox.critical(None, "SectVoice启动失败", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
