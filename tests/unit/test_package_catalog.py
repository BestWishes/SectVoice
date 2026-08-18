from __future__ import annotations

import hashlib
from http.client import IncompleteRead
from io import BytesIO
import json
from pathlib import Path
import shutil
from email.message import Message
import zipfile

import pytest

from sectvoice.core.database import Database
from sectvoice.core.model_packages import ModelPackageManager
from sectvoice.core.package_catalog import (
    CatalogAsset,
    PackageCatalog,
    PackageCatalogError,
    download_asset,
)
from sectvoice.core.package_installer import PackageInstallError, PackageInstaller
from sectvoice.core.package_installer import (
    PACKAGE_INSTALL_SAFETY_BYTES,
    package_space_requirement,
)
from sectvoice.domain import Tier
from sectvoice.paths import AppPaths


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        source=root / "app",
        runtime=root / "runtime",
        models=root / "models",
        data=root / "data",
        cache=root / "cache",
        downloads=root / "downloads",
        temp=root / "temp",
        artifacts=root / "artifacts",
    )


class _DownloadResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int,
        content_range: str | None = None,
    ) -> None:
        self.stream = BytesIO(payload)
        self.status = status
        self.headers = Message()
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _CatalogResponse(_DownloadResponse):
    def __init__(self, payload: bytes, *, fail_early: bool) -> None:
        super().__init__(payload, status=200)
        self.headers["Content-Length"] = str(len(payload))
        self.fail_early = fail_early

    def read(self, size: int = -1) -> bytes:
        if self.fail_early:
            self.fail_early = False
            partial = self.stream.read(max(1, len(self.stream.getvalue()) // 2))
            raise IncompleteRead(partial, len(self.stream.getvalue()) - len(partial))
        return super().read(size)


def _asset(payload: bytes) -> CatalogAsset:
    return CatalogAsset(
        name="large-part01.zip",
        url="https://downloads.example.test/large-part01.zip",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _make_catalog(root: Path, *, malicious: bool = False, bad_asset_hash: bool = False) -> Path:
    source = root / "asset-source"
    runtime_file = source / "runtime/engines/basic/demo/1/package/package-manifest.json"
    worker_file = source / "runtime/engines/basic/demo/1/package/worker.py"
    model_file = source / "models/basic/demo/1/model.bin"
    runtime_file.parent.mkdir(parents=True)
    model_file.parent.mkdir(parents=True)
    runtime_file.write_text(
        json.dumps(
            {
                "package_id": "sectvoice.basic.demo",
                "tier": "basic",
                "engine_id": "demo",
                "engine_version": "1",
                "capabilities": {},
            }
        ),
        encoding="utf-8",
    )
    worker_file.write_text("print('worker')\n", encoding="utf-8")
    model_file.write_bytes(b"real model bytes")
    asset = root / "basic-1-part01.zip"
    files = (runtime_file, worker_file, model_file)
    with zipfile.ZipFile(asset, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(source).as_posix())
        if malicious:
            archive.writestr("../escape.txt", "blocked")
    rows = [
        {
            "path": path.relative_to(source).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha(path),
        }
        for path in files
    ]
    if malicious:
        rows.append(
            {
                "path": "runtime/ignored.txt",
                "size": 7,
                "sha256": hashlib.sha256(b"blocked").hexdigest(),
            }
        )
    asset_sha = "0" * 64 if bad_asset_hash else _sha(asset)
    catalog = root / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reader_min_version": "0.2.0",
                "packages": [
                    {
                        "package_id": "sectvoice.basic.demo",
                        "tier": "basic",
                        "display_name": "基础测试包",
                        "version": "1",
                        "manifest_relative_path": runtime_file.relative_to(source).as_posix(),
                        "runtime_relative_path": "runtime/engines/basic/demo/1",
                        "model_relative_path": "models/basic/demo/1",
                        "assets": [
                            {
                                "name": asset.name,
                                "url": asset.name,
                                "size": asset.stat().st_size,
                                "sha256": asset_sha,
                            }
                        ],
                        "files": rows,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return catalog


def test_catalog_package_downloads_verifies_and_activates(tmp_path: Path) -> None:
    catalog = PackageCatalog.load(_make_catalog(tmp_path))
    package = catalog.for_tier(Tier.BASIC)
    assert package is not None
    paths = _paths(tmp_path / "install")
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    manager = ModelPackageManager(database)
    progress: list[tuple[str, int, int]] = []

    record = PackageInstaller(manager).install_catalog_package(
        package,
        paths=paths,
        progress=lambda name, current, total: progress.append((name, current, total)),
    )

    assert record.package_id == "sectvoice.basic.demo"
    assert manager.active_for(Tier.BASIC) == record
    assert (record.runtime_path / "package/worker.py").is_file()
    assert (record.model_path / "model.bin").read_bytes() == b"real model bytes"
    assert progress[-1][1] == progress[-1][2]
    assert not (
        paths.downloads / "engine-packages" / package.package_id / package.version
    ).exists()


def test_catalog_short_http_stream_retries_the_manifest_request(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _make_catalog(tmp_path).read_bytes()
    responses = iter(
        (_CatalogResponse(payload, fail_early=True), _CatalogResponse(payload, fail_early=False))
    )
    requests = []
    monkeypatch.setattr(
        "sectvoice.core.package_catalog.urlopen",
        lambda request, **_kwargs: (requests.append(request), next(responses))[1],
    )
    monkeypatch.setattr("sectvoice.core.package_catalog.time.sleep", lambda _delay: None)

    catalog = PackageCatalog.load("https://downloads.example.test/catalog.json")

    assert catalog.for_tier(Tier.BASIC) is not None
    assert len(requests) == 2


def test_asset_short_stream_resumes_with_range_in_the_same_install(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"0123456789abcdef"
    asset = _asset(payload)
    requests = []
    responses = iter(
        (
            _DownloadResponse(payload[:6], status=200),
            _DownloadResponse(
                payload[6:],
                status=206,
                content_range=f"bytes 6-{len(payload) - 1}/{len(payload)}",
            ),
        )
    )

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr("sectvoice.core.package_catalog.urlopen", fake_urlopen)

    destination = download_asset(
        asset,
        tmp_path / asset.name,
        retry_delays_seconds=(0,),
    )

    assert destination.read_bytes() == payload
    assert requests[0].get_header("Range") is None
    assert requests[1].get_header("Range") == "bytes=6-"
    assert not destination.with_suffix(destination.suffix + ".partial").exists()


def test_asset_retry_exhaustion_keeps_partial_for_next_install_click(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"complete immutable package bytes"
    asset = _asset(payload)
    destination = tmp_path / asset.name
    requests = []
    first = iter((_DownloadResponse(payload[:9], status=200),))

    def first_urlopen(request, **_kwargs):
        requests.append(request)
        return next(first)

    monkeypatch.setattr("sectvoice.core.package_catalog.urlopen", first_urlopen)
    with pytest.raises(PackageCatalogError, match="已保留9字节断点"):
        download_asset(asset, destination, retry_delays_seconds=())

    partial = destination.with_suffix(destination.suffix + ".partial")
    assert partial.read_bytes() == payload[:9]
    second = iter(
        (
            _DownloadResponse(
                payload[9:],
                status=206,
                content_range=f"bytes 9-{len(payload) - 1}/{len(payload)}",
            ),
        )
    )
    monkeypatch.setattr(
        "sectvoice.core.package_catalog.urlopen",
        lambda request, **_kwargs: (requests.append(request), next(second))[1],
    )

    result = download_asset(asset, destination, retry_delays_seconds=())

    assert result.read_bytes() == payload
    assert requests[-1].get_header("Range") == "bytes=9-"


def test_asset_server_ignoring_range_overwrites_instead_of_appending(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"server returned the complete object"
    asset = _asset(payload)
    destination = tmp_path / asset.name
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.write_bytes(payload[:7])
    monkeypatch.setattr(
        "sectvoice.core.package_catalog.urlopen",
        lambda *_args, **_kwargs: _DownloadResponse(payload, status=200),
    )

    result = download_asset(asset, destination, retry_delays_seconds=())

    assert result.read_bytes() == payload


def test_package_space_requirement_includes_download_extract_and_safety(
    tmp_path: Path,
) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path)).packages[0]

    requirement = package_space_requirement(package)

    assert requirement.download_bytes == package.download_size
    assert requirement.installed_bytes == package.installed_size
    assert requirement.peak_free_bytes == (
        package.download_size + package.installed_size + PACKAGE_INSTALL_SAFETY_BYTES
    )


def test_package_space_requirement_subtracts_retained_completed_and_partial_bytes(
    tmp_path: Path,
) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path)).packages[0]
    download_root = tmp_path / "downloads"
    destination = download_root / package.assets[0].name
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.parent.mkdir(parents=True)
    retained = max(1, package.download_size // 3)
    partial.write_bytes(b"x" * retained)

    requirement = package_space_requirement(
        package, download_root=download_root
    )

    assert requirement.download_bytes == package.download_size - retained
    assert requirement.peak_free_bytes == (
        package.download_size
        - retained
        + package.installed_size
        + PACKAGE_INSTALL_SAFETY_BYTES
    )


def test_catalog_install_fails_before_download_when_disk_space_is_insufficient(
    tmp_path: Path, monkeypatch
) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path)).packages[0]
    paths = _paths(tmp_path / "install")
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    monkeypatch.setattr(
        "sectvoice.core.package_installer.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=100, used=99, free=1),
    )

    with pytest.raises(PackageInstallError, match="空间不足"):
        PackageInstaller(ModelPackageManager(database)).install_catalog_package(
            package, paths=paths
        )

    assert not (paths.downloads / "engine-packages").exists()


def test_catalog_install_of_current_version_is_a_no_download_noop(
    tmp_path: Path, monkeypatch
) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path)).packages[0]
    paths = _paths(tmp_path / "install")
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    manager = ModelPackageManager(database)
    installer = PackageInstaller(manager)
    first = installer.install_catalog_package(package, paths=paths)
    monkeypatch.setattr(
        "sectvoice.core.package_installer.download_asset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("downloaded")),
    )

    second = installer.install_catalog_package(package, paths=paths)

    assert second == first


def test_catalog_rejects_wrong_download_hash_without_installing(tmp_path: Path) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path, bad_asset_hash=True)).packages[0]
    paths = _paths(tmp_path / "install")
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()
    manager = ModelPackageManager(database)

    with pytest.raises(PackageCatalogError, match="SHA-256"):
        PackageInstaller(manager).install_catalog_package(package, paths=paths)

    assert manager.list_installed() == ()


def test_catalog_rejects_zip_path_traversal(tmp_path: Path) -> None:
    package = PackageCatalog.load(_make_catalog(tmp_path, malicious=True)).packages[0]
    paths = _paths(tmp_path / "install")
    paths.ensure_writable_directories()
    database = Database(paths.data / "sectvoice.db")
    database.initialize()

    with pytest.raises(PackageInstallError, match="不安全"):
        PackageInstaller(ModelPackageManager(database)).install_catalog_package(
            package, paths=paths
        )

    assert not (tmp_path / "escape.txt").exists()
