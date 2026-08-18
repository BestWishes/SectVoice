from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Callable
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from sectvoice.core.package_catalog import (
    CatalogAsset,
    PackageCatalogError,
    download_asset,
)


DEFAULT_READER_UPDATE_URL = (
    "https://github.com/BestWishes/SectVoice-Downloads/"
    "releases/latest/download/reader-update.json"
)
READER_UPDATE_SCHEMA_VERSION = 1
UPDATE_DOWNLOAD_SAFETY_BYTES = 128 * 1024**2
UpdateProgress = Callable[[int, int], None]


class ReaderUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReaderUpdate:
    version: str
    installer_name: str
    installer_url: str
    installer_size: int
    installer_sha256: str
    release_notes_url: str
    signed: bool


def load_reader_update(
    source: str | Path = DEFAULT_READER_UPDATE_URL,
    *,
    timeout_seconds: float = 30.0,
) -> ReaderUpdate:
    text, base_url = _read_source(source, timeout_seconds)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReaderUpdateError(f"Reader更新清单JSON无效：{exc}") from exc
    if raw.get("schema_version") != READER_UPDATE_SCHEMA_VERSION:
        raise ReaderUpdateError("不支持的Reader更新清单版本")
    try:
        installer = dict(raw["installer"])
        name = str(installer["name"])
        url = urljoin(base_url, str(installer.get("url") or name))
        size = int(installer["size"])
        sha256 = str(installer["sha256"]).lower()
        version = str(raw["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReaderUpdateError(f"Reader更新清单字段无效：{exc}") from exc
    if Path(name).name != name or not name.lower().endswith(".exe"):
        raise ReaderUpdateError("Reader更新安装器名称不安全")
    if urlparse(url).scheme not in {"https", "file"}:
        raise ReaderUpdateError("Reader更新安装器只允许HTTPS或本地file来源")
    if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ReaderUpdateError("Reader更新安装器大小或SHA-256无效")
    _version_parts(version)
    return ReaderUpdate(
        version=version,
        installer_name=name,
        installer_url=url,
        installer_size=size,
        installer_sha256=sha256,
        release_notes_url=str(raw.get("release_notes_url") or ""),
        signed=bool(installer.get("signed", False)),
    )


def is_version_newer(candidate: str, current: str) -> bool:
    return _version_parts(candidate) > _version_parts(current)


def download_reader_update(
    update: ReaderUpdate,
    destination_root: Path,
    *,
    progress: UpdateProgress | None = None,
    timeout_seconds: float = 60.0,
) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / update.installer_name
    partial = destination.with_suffix(destination.suffix + ".partial")
    retained_sizes = [
        path.stat().st_size
        for path in (destination, partial)
        if path.is_file()
    ]
    retained_bytes = (
        min(update.installer_size, max(retained_sizes))
        if retained_sizes
        else 0
    )
    free_bytes = shutil.disk_usage(destination_root).free
    required = (
        update.installer_size - retained_bytes + UPDATE_DOWNLOAD_SAFETY_BYTES
    )
    if free_bytes < required:
        raise ReaderUpdateError(
            "下载Reader升级程序的空间不足："
            f"至少需要{_gib(required):.2f}GiB，当前只有{_gib(free_bytes):.2f}GiB。"
        )
    if destination.is_file() and _matches(destination, update):
        if progress is not None:
            progress(update.installer_size, update.installer_size)
        return destination
    try:
        return download_asset(
            CatalogAsset(
                name=update.installer_name,
                url=update.installer_url,
                size=update.installer_size,
                sha256=update.installer_sha256,
            ),
            destination,
            timeout_seconds=timeout_seconds,
            progress=(
                (lambda _name, current, total: progress(current, total))
                if progress is not None
                else None
            ),
        )
    except PackageCatalogError as exc:
        raise ReaderUpdateError(str(exc)) from exc


def _read_source(source: str | Path, timeout_seconds: float) -> tuple[str, str]:
    if isinstance(source, Path):
        path = source.resolve()
        return path.read_text(encoding="utf-8"), path.as_uri()
    parsed = urlparse(source)
    if parsed.scheme in {"https", "file"}:
        with urlopen(source, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8"), source
    path = Path(source).resolve()
    return path.read_text(encoding="utf-8"), path.as_uri()


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip())
    if match is None:
        raise ReaderUpdateError(f"不支持的版本号：{value}")
    parts = tuple(int(item) for item in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def _matches(path: Path, update: ReaderUpdate) -> bool:
    if path.stat().st_size != update.installer_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == update.installer_sha256


def _gib(byte_count: int) -> float:
    return byte_count / 1024**3
