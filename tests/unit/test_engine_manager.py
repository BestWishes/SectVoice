from __future__ import annotations

from threading import RLock
from unittest.mock import Mock
from uuid import uuid4
from pathlib import Path

from sectvoice.core.engine_manager import EngineHandle, EnginePackageManifest
from sectvoice.core.model_packages import EnginePackageRecord
from sectvoice.domain import Tier
from sectvoice.engines.process_client import EngineProcessError
from sectvoice.paths import AppPaths


class _FailingCancelClient:
    is_running = True

    def cancel(self, *_args) -> None:
        raise EngineProcessError("closing")


def test_late_cancel_failure_never_restarts_an_unmanaged_worker() -> None:
    handle = object.__new__(EngineHandle)
    handle._lock = RLock()
    handle._client = _FailingCancelClient()
    handle.restart = Mock()

    handle.cancel(uuid4(), 3)

    handle.restart.assert_not_called()


def test_release_engine_uses_shared_relocatable_python(tmp_path: Path) -> None:
    paths = AppPaths(
        root=tmp_path,
        source=tmp_path / "app",
        runtime=tmp_path / "runtime",
        models=tmp_path / "models",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        downloads=tmp_path / "downloads",
        temp=tmp_path / "temp",
        artifacts=tmp_path / "artifacts",
    )
    runtime = paths.runtime / "engines" / "basic" / "demo"
    model = paths.models / "basic" / "demo"
    portable_python = paths.runtime / "common" / "python310" / "python.exe"
    worker = runtime / "package" / "worker.py"
    site_packages = runtime / ".venv" / "Lib" / "site-packages"
    for file in (portable_python, worker):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"test")
    site_packages.mkdir(parents=True)
    model.mkdir(parents=True)
    record = EnginePackageRecord(
        package_id="sectvoice.basic.demo",
        tier=Tier.BASIC,
        engine_id="moss-nano-onnx",
        engine_version="1",
        manifest_path=runtime / "package" / "package-manifest.json",
        runtime_path=runtime,
        model_path=model,
        status="installed",
        installed_at="now",
    )
    manifest = EnginePackageManifest(
        package_id=record.package_id,
        tier=Tier.BASIC,
        engine_id=record.engine_id,
        engine_version=record.engine_version,
        payload_format_version="demo-v1",
        raw={},
    )

    spec = EngineHandle(paths, record, manifest)._create_client().spec

    assert spec.python_executable == portable_python
    assert spec.environment["PYTHONHOME"] == str(portable_python.parent)
    assert spec.environment["PYTHONPATH"] == str(site_packages)
