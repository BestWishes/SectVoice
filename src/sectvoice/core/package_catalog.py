from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import json
import re
from pathlib import Path, PurePosixPath
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from sectvoice.domain import Tier


CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_URL = (
    "https://github.com/BestWishes/SectVoice-Downloads/"
    "releases/latest/download/catalog.json"
)
DownloadProgress = Callable[[str, int, int], None]
DOWNLOAD_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0)
CATALOG_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)


class PackageCatalogError(RuntimeError):
    pass


class _RetryableDownloadError(RuntimeError):
    pass


class _RestartDownload(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogAsset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CatalogFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CatalogPackage:
    package_id: str
    tier: Tier
    display_name: str
    version: str
    manifest_relative_path: str
    runtime_relative_path: str
    model_relative_path: str
    download_size: int
    installed_size: int
    assets: tuple[CatalogAsset, ...]
    files: tuple[CatalogFile, ...]
    requirements: dict[str, object]
    upstream: dict[str, object]


@dataclass(frozen=True, slots=True)
class PackageCatalog:
    source: str
    reader_min_version: str
    packages: tuple[CatalogPackage, ...]

    @classmethod
    def load(cls, source: str | Path, *, timeout_seconds: float = 30.0) -> "PackageCatalog":
        source_text, base_url = _read_catalog_source(source, timeout_seconds)
        try:
            raw = json.loads(source_text)
        except json.JSONDecodeError as exc:
            raise PackageCatalogError(f"语音包目录JSON无效：{exc}") from exc
        if raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise PackageCatalogError("不支持的语音包目录版本")
        package_rows = raw.get("packages")
        if not isinstance(package_rows, list):
            raise PackageCatalogError("语音包目录缺少packages数组")
        packages = tuple(_parse_package(item, base_url) for item in package_rows)
        package_ids = [item.package_id for item in packages]
        if len(package_ids) != len(set(package_ids)):
            raise PackageCatalogError("语音包目录包含重复package_id")
        return cls(
            source=str(source),
            reader_min_version=str(raw.get("reader_min_version") or "0"),
            packages=packages,
        )

    def for_tier(self, tier: Tier) -> CatalogPackage | None:
        return next((item for item in self.packages if item.tier is tier), None)


def download_asset(
    asset: CatalogAsset,
    destination: Path,
    *,
    timeout_seconds: float = 60.0,
    progress: DownloadProgress | None = None,
    retry_delays_seconds: tuple[float, ...] = DOWNLOAD_RETRY_DELAYS_SECONDS,
) -> Path:
    """Download one immutable asset with persistent, validated HTTP Range resume."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if destination.is_file() and _file_matches(destination, asset.size, asset.sha256):
        partial.unlink(missing_ok=True)
        if progress is not None:
            progress(asset.name, asset.size, asset.size)
        return destination
    if destination.is_file():
        destination.unlink()
    last_error: Exception | None = None
    hash_failures = 0
    attempts = len(retry_delays_seconds) + 1
    for attempt in range(attempts):
        try:
            _download_asset_once(
                asset,
                partial,
                timeout_seconds=timeout_seconds,
                progress=progress,
            )
            if partial.stat().st_size != asset.size:
                raise _RetryableDownloadError(
                    f"连接提前结束，收到{partial.stat().st_size}，预期{asset.size}"
                )
            if sha256_file(partial) != asset.sha256:
                # A full-sized corrupt file is never a valid resume base.
                partial.unlink(missing_ok=True)
                hash_failures += 1
                if hash_failures > 1:
                    raise PackageCatalogError(
                        f"下载SHA-256不匹配：{asset.name}；已完整重下仍不匹配"
                    )
                raise _RetryableDownloadError(
                    "完整分卷SHA-256不匹配，已清空并准备重下"
                )
            partial.replace(destination)
            if progress is not None:
                progress(asset.name, asset.size, asset.size)
            return destination
        except _RestartDownload as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
        except HTTPError as exc:
            last_error = exc
            if exc.code == 416:
                if partial.is_file() and _file_matches(
                    partial, asset.size, asset.sha256
                ):
                    partial.replace(destination)
                    if progress is not None:
                        progress(asset.name, asset.size, asset.size)
                    return destination
                partial.unlink(missing_ok=True)
            elif exc.code not in {403, 408, 425, 429, 500, 502, 503, 504}:
                raise PackageCatalogError(
                    f"下载请求失败：{asset.name}，HTTP {exc.code}"
                ) from exc
        except (
            _RetryableDownloadError,
            IncompleteRead,
            RemoteDisconnected,
            TimeoutError,
            socket.timeout,
            URLError,
            ConnectionError,
        ) as exc:
            last_error = exc
        if attempt < len(retry_delays_seconds):
            delay = retry_delays_seconds[attempt]
            if delay > 0:
                time.sleep(delay)
    received = partial.stat().st_size if partial.is_file() else 0
    retained = (
        f"已保留{received}字节断点，再次点击安装会从断点继续"
        if received
        else "没有可保留的有效断点，再次点击安装会重新下载"
    )
    raise PackageCatalogError(
        f"下载多次中断：{asset.name}；{retained}；"
        f"预期{asset.size}字节。最后错误：{last_error}"
    ) from last_error


def _download_asset_once(
    asset: CatalogAsset,
    partial: Path,
    *,
    timeout_seconds: float,
    progress: DownloadProgress | None,
) -> None:
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > asset.size:
        partial.unlink(missing_ok=True)
        offset = 0
    if offset == asset.size:
        if progress is not None:
            progress(asset.name, offset, asset.size)
        return
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(asset.url, headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:
        status = getattr(response, "status", None) or response.getcode()
        mode = "wb"
        received = 0
        if offset and status == 206:
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
            if match is None or int(match.group(1)) != offset:
                raise _RestartDownload(
                    f"服务器返回了无效续传范围：{content_range or '<empty>'}"
                )
            if match.group(3) != "*" and int(match.group(3)) != asset.size:
                raise _RestartDownload(
                    f"服务器续传总大小变化：{match.group(3)}，预期{asset.size}"
                )
            mode = "ab"
            received = offset
        elif offset and status == 200:
            # Some CDNs ignore Range.  Overwrite instead of corruptly appending
            # a second complete response to the existing prefix.
            mode = "wb"
        elif offset:
            raise _RestartDownload(f"服务器不支持可靠续传，状态码{status}")
        if progress is not None:
            progress(asset.name, received, asset.size)
        with partial.open(mode) as output:
            while True:
                try:
                    block = response.read(1024 * 1024)
                except IncompleteRead as exc:
                    # http.client may carry the final received bytes on the
                    # exception instead of returning them from read().  Keep
                    # those bytes so the next Range request starts exactly at
                    # the true durable offset.
                    block = exc.partial
                    if block:
                        output.write(block)
                        received += len(block)
                        if progress is not None:
                            progress(asset.name, received, asset.size)
                    raise _RetryableDownloadError(
                        f"连接提前结束，收到{received}，预期{asset.size}"
                    ) from exc
                if not block:
                    break
                output.write(block)
                received += len(block)
                if received > asset.size:
                    raise _RestartDownload(
                        f"服务器返回数据超过目录大小：{received}>{asset.size}"
                    )
                if progress is not None:
                    progress(asset.name, received, asset.size)
    if received < asset.size:
        raise _RetryableDownloadError(
            f"连接提前结束，收到{received}，预期{asset.size}"
        )


def safe_relative_path(value: str) -> Path:
    """Convert a catalog/ZIP POSIX path while rejecting traversal and drives."""

    if not value or "\\" in value:
        raise PackageCatalogError(f"不安全的包内路径：{value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise PackageCatalogError(f"不安全的包内路径：{value!r}")
    if ":" in parsed.parts[0]:
        raise PackageCatalogError(f"不安全的包内路径：{value!r}")
    return Path(*parsed.parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_catalog_source(source: str | Path, timeout_seconds: float) -> tuple[str, str]:
    if isinstance(source, Path):
        path = source.resolve()
        return path.read_text(encoding="utf-8"), path.as_uri()
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        last_error: Exception | None = None
        for attempt in range(len(CATALOG_RETRY_DELAYS_SECONDS) + 1):
            try:
                request = Request(source, headers={"Accept-Encoding": "identity"})
                with urlopen(request, timeout=timeout_seconds) as response:
                    payload = _read_response_bytes(response)
                return payload.decode("utf-8"), source
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise PackageCatalogError(
                        f"读取语音包目录失败：HTTP {exc.code}"
                    ) from exc
            except (
                _RetryableDownloadError,
                IncompleteRead,
                RemoteDisconnected,
                TimeoutError,
                socket.timeout,
                URLError,
                ConnectionError,
            ) as exc:
                last_error = exc
            if attempt < len(CATALOG_RETRY_DELAYS_SECONDS):
                time.sleep(CATALOG_RETRY_DELAYS_SECONDS[attempt])
        raise PackageCatalogError(
            "语音包目录连接多次提前中断，请检查网络后重试；"
            f"最后错误：{last_error}"
        ) from last_error
    if parsed.scheme == "file":
        with urlopen(source, timeout=timeout_seconds) as response:
            return _read_response_bytes(response).decode("utf-8"), source
    path = Path(source).resolve()
    return path.read_text(encoding="utf-8"), path.as_uri()


def _read_response_bytes(response) -> bytes:
    """Read a small manifest while detecting HTTP responses that end early."""

    expected_text = response.headers.get("Content-Length", "")
    expected = int(expected_text) if expected_text.isdigit() else None
    received = bytearray()
    while True:
        try:
            block = response.read(1024 * 1024)
        except IncompleteRead as exc:
            if exc.partial:
                received.extend(exc.partial)
            raise _RetryableDownloadError(
                f"目录连接提前结束，收到{len(received)}字节"
            ) from exc
        if not block:
            break
        received.extend(block)
    if expected is not None and len(received) != expected:
        raise _RetryableDownloadError(
            f"目录连接提前结束，收到{len(received)}，预期{expected}"
        )
    return bytes(received)


def _parse_package(raw: object, base_url: str) -> CatalogPackage:
    if not isinstance(raw, dict):
        raise PackageCatalogError("语音包条目必须是对象")
    try:
        tier = Tier(str(raw["tier"]))
        if tier is Tier.ADVANCED:
            raise PackageCatalogError("高级语音包尚未发布")
        assets = tuple(_parse_asset(item, base_url) for item in raw["assets"])
        files = tuple(_parse_file(item) for item in raw["files"])
        manifest = str(raw["manifest_relative_path"])
        runtime = str(raw["runtime_relative_path"])
        model = str(raw["model_relative_path"])
        for value in (manifest, runtime, model):
            safe_relative_path(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise PackageCatalogError(f"语音包条目字段无效：{exc}") from exc
    if not assets or not files:
        raise PackageCatalogError("语音包条目缺少资产或文件清单")
    return CatalogPackage(
        package_id=str(raw["package_id"]),
        tier=tier,
        display_name=str(raw.get("display_name") or raw["package_id"]),
        version=str(raw["version"]),
        manifest_relative_path=manifest,
        runtime_relative_path=runtime,
        model_relative_path=model,
        download_size=int(raw.get("download_size") or sum(item.size for item in assets)),
        installed_size=int(raw.get("installed_size") or sum(item.size for item in files)),
        assets=assets,
        files=files,
        requirements=dict(raw.get("requirements") or {}),
        upstream=dict(raw.get("upstream") or {}),
    )


def _parse_asset(raw: object, base_url: str) -> CatalogAsset:
    if not isinstance(raw, dict):
        raise PackageCatalogError("资产条目必须是对象")
    name = str(raw.get("name") or "")
    if Path(name).name != name or not name.lower().endswith(".zip"):
        raise PackageCatalogError(f"资产名称无效：{name!r}")
    url = urljoin(base_url, str(raw.get("url") or name))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "file"}:
        raise PackageCatalogError(f"资产URL协议无效：{url}")
    sha = str(raw.get("sha256") or "").lower()
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise PackageCatalogError(f"资产SHA-256无效：{name}")
    return CatalogAsset(name=name, url=url, size=int(raw["size"]), sha256=sha)


def _parse_file(raw: object) -> CatalogFile:
    if not isinstance(raw, dict):
        raise PackageCatalogError("文件条目必须是对象")
    path = str(raw.get("path") or "")
    safe_relative_path(path)
    sha = str(raw.get("sha256") or "").lower()
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise PackageCatalogError(f"文件SHA-256无效：{path}")
    return CatalogFile(path=path, size=int(raw["size"]), sha256=sha)


def _file_matches(path: Path, size: int, sha256: str) -> bool:
    return path.stat().st_size == size and sha256_file(path) == sha256
