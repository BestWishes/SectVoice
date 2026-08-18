from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock

from sectvoice.core.model_packages import EnginePackageRecord, ModelPackageManager
from sectvoice.core.voice_compiler import CompilationTarget
from sectvoice.domain import EnginePayloadRef, Tier, VoiceProfile
from sectvoice.engines.process_client import (
    EngineProcessClient,
    EngineProcessError,
    EngineProcessSpec,
)
from sectvoice.paths import AppPaths


class EngineUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EnginePackageManifest:
    package_id: str
    tier: Tier
    engine_id: str
    engine_version: str
    payload_format_version: str
    raw: dict[str, object]

    @classmethod
    def read(cls, path: Path) -> "EnginePackageManifest":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            package_id=str(raw["package_id"]),
            tier=Tier(raw["tier"]),
            engine_id=str(raw["engine_id"]),
            engine_version=str(raw["engine_version"]),
            payload_format_version=str(raw["payload_format_version"]),
            raw=raw,
        )


class EngineHandle:
    def __init__(
        self,
        paths: AppPaths,
        record: EnginePackageRecord,
        manifest: EnginePackageManifest,
    ) -> None:
        self.paths = paths
        self.record = record
        self.manifest = manifest
        self._client: EngineProcessClient | None = None
        self._lock = RLock()

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._client is not None and self._client.is_running

    @property
    def worker_rss_mib(self) -> float:
        """Measure an already-loaded worker without accidentally loading it."""

        with self._lock:
            client = self._client
        if client is None or not client.is_running:
            return 0.0
        return client.process_tree_rss_mib

    @property
    def client(self) -> EngineProcessClient:
        with self._lock:
            if self._client is not None and self._client.is_running:
                return self._client
            stale = self._client
            self._client = None
            if stale is not None:
                stale.stop(force=True)
            self._client = self._create_client()
            try:
                self._client.start(self._launch_args())
                self._client.request("load", timeout_seconds=180.0)
            except Exception:
                self._client.stop(force=True)
                self._client = None
                raise
            return self._client

    def restart(self) -> EngineProcessClient:
        with self._lock:
            if self._client is not None:
                self._client.stop(force=True)
                self._client = None
        return self.client

    def cancel(self, session_id, generation_id: int) -> None:
        with self._lock:
            client = self._client
        if client is None or not client.is_running:
            return
        try:
            client.cancel(session_id, generation_id)
        except EngineProcessError:
            # Cancellation is best-effort.  Playback stop deliberately runs it
            # on a helper thread so Qt never blocks.  Restarting here races
            # with EngineManager.shutdown(): the manager can clear/unload the
            # handle and this late cancel error would then launch a brand-new,
            # unmanaged model process while the application is closing.  A
            # later real synthesis request already recreates a stale client
            # through the client property, so cancel must never restart it.
            return

    def unload(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.stop()

    def compilation_target(self) -> CompilationTarget:
        return CompilationTarget(
            tier=self.manifest.tier,
            engine_id=self.manifest.engine_id,
            engine_version=self.manifest.engine_version,
            payload_format_version=self.manifest.payload_format_version,
            client=self.client,
        )

    def _create_client(self) -> EngineProcessClient:
        worker = self.record.runtime_path / "package" / "worker.py"
        portable_python = self.paths.runtime / "common" / "python310" / "python.exe"
        python = (
            portable_python
            if portable_python.is_file()
            else self.record.runtime_path / ".venv" / "Scripts" / "python.exe"
        )
        if not python.is_file():
            raise EngineUnavailableError(f"引擎Python环境不存在：{python}")
        if not worker.is_file():
            raise EngineUnavailableError(f"引擎工作进程不存在：{worker}")
        environment = self.paths.process_environment()
        environment["PYTHONPYCACHEPREFIX"] = str(
            self.paths.cache / "pycache" / self.manifest.engine_id
        )
        if python == portable_python:
            # Release packages carry only the engine's site-packages.  A shared,
            # relocatable CPython runtime in Reader Core replaces the absolute
            # developer-machine paths stored by a normal Windows virtualenv.
            environment["PYTHONHOME"] = str(portable_python.parent)
            environment["PYTHONPATH"] = str(
                self.record.runtime_path / ".venv" / "Lib" / "site-packages"
            )
            environment["PYTHONNOUSERSITE"] = "1"
        package_nltk = self.record.runtime_path / ".nltk_data"
        if package_nltk.is_dir():
            environment["NLTK_DATA"] = str(package_nltk)
        return EngineProcessClient(
            EngineProcessSpec(
                python_executable=python,
                worker_script=worker,
                working_directory=worker.parent,
                environment=environment,
                log_path=self.paths.data / "logs" / f"{self.manifest.engine_id}-worker.log",
                startup_timeout_seconds=180.0,
            )
        )

    def _launch_args(self) -> list[str]:
        output = self.paths.temp / "engine-output" / self.manifest.engine_id
        if self.manifest.engine_id == "moss-nano-onnx":
            return [
                "--model-dir",
                str(self.record.model_path),
                "--output-dir",
                str(output),
                "--cpu-threads",
                "2",
            ]
        if self.manifest.engine_id == "gpt-sovits-v2proplus":
            return [
                "--app-root",
                str(self.record.runtime_path / "app"),
                "--model-dir",
                str(self.record.model_path),
                "--output-dir",
                str(output),
                "--device",
                "cuda",
            ]
        raise EngineUnavailableError(f"没有引擎启动适配器：{self.manifest.engine_id}")


class EngineManager:
    """Keeps one isolated process per installed tier and restarts failures."""

    def __init__(self, paths: AppPaths, packages: ModelPackageManager) -> None:
        self.paths = paths
        self.packages = packages
        self._handles: dict[Tier, EngineHandle] = {}
        self._lock = RLock()

    def for_tier(self, tier: Tier) -> EngineHandle:
        if tier is Tier.ADVANCED:
            raise EngineUnavailableError("高级语音包第一版尚未实现")
        record = self.packages.active_for(tier)
        if record is None:
            raise EngineUnavailableError(f"尚未安装或启用{self._display_tier(tier)}语音包")
        with self._lock:
            current = self._handles.get(tier)
            if current is not None and current.record.package_id == record.package_id:
                return current
            if current is not None:
                current.unload()
            manifest = EnginePackageManifest.read(record.manifest_path)
            handle = EngineHandle(self.paths, record, manifest)
            self._handles[tier] = handle
            return handle

    def compile_voice(self, profile: VoiceProfile, tier: Tier, compiler) -> EnginePayloadRef:
        return compiler.compile_payload(profile, self.for_tier(tier).compilation_target())

    def shutdown(self) -> None:
        with self._lock:
            handles = tuple(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle.unload()

    def unload_tier(self, tier: Tier) -> None:
        """Release one package process without affecting the other installed tier."""

        with self._lock:
            handle = self._handles.pop(tier, None)
        if handle is not None:
            handle.unload()

    def unload_except(self, tier: Tier) -> None:
        """Keep only the tier the Reader is about to use to limit RAM/VRAM use."""

        with self._lock:
            unwanted = [key for key in self._handles if key is not tier]
        for key in unwanted:
            self.unload_tier(key)

    @staticmethod
    def _display_tier(tier: Tier) -> str:
        return {Tier.BASIC: "基础", Tier.STANDARD: "中级", Tier.ADVANCED: "高级"}[tier]
