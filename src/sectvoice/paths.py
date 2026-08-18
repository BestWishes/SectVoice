from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import sys


def _running_application_dir() -> Path:
    """Return the Reader resource directory in source and frozen builds."""

    override = os.environ.get("SECTVOICE_APP_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _default_root() -> Path:
    """Choose a relocatable root without baking the developer's H: path."""

    application_dir = _running_application_dir()
    if application_dir.name.lower() in {"app", "source"}:
        return application_dir.parent
    return application_dir


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Centralizes every writable path so engines cannot spill data to C:."""

    root: Path
    source: Path
    runtime: Path
    models: Path
    data: Path
    cache: Path
    downloads: Path
    temp: Path
    artifacts: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        root_value = os.environ.get("SECTVOICE_ROOT")
        root = Path(root_value).resolve() if root_value else _default_root().resolve()
        application_override = os.environ.get("SECTVOICE_APP_DIR")
        if application_override:
            source = Path(application_override).resolve()
        elif (root / "source" / "src" / "sectvoice").is_dir():
            source = (root / "source").resolve()
        elif (root / "app").is_dir():
            source = (root / "app").resolve()
        else:
            source = _running_application_dir()
        return cls(
            root=root,
            source=source,
            runtime=root / "runtime",
            models=root / "models",
            data=root / "data",
            cache=root / "cache",
            downloads=root / "downloads",
            temp=root / "temp",
            artifacts=root / "artifacts",
        )

    def ensure_writable_directories(self) -> None:
        for path in (
            self.runtime,
            self.models,
            self.data,
            self.cache,
            self.downloads,
            self.temp,
            self.artifacts,
        ):
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".sectvoice-write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()

    def process_environment(self) -> dict[str, str]:
        """Returns project-local cache variables for child engine processes."""

        return {
            "SECTVOICE_ROOT": str(self.root),
            "UV_CACHE_DIR": str(self.cache / "uv"),
            "HF_HOME": str(self.cache / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(self.cache / "huggingface" / "hub"),
            "TORCH_HOME": str(self.cache / "torch"),
            "NLTK_DATA": str(self.cache / "nltk_data"),
            "XDG_CACHE_HOME": str(self.cache / "xdg"),
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
