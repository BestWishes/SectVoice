from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


WINDOWS_APP_USER_MODEL_ID = "BestWishes.SectVoice.Reader"


def configure_windows_app_identity() -> None:
    """Give Windows one stable identity for taskbar grouping and shortcuts."""

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            WINDOWS_APP_USER_MODEL_ID
        )
    except (AttributeError, OSError):
        # Older shells can omit this API.  The executable icon still works and
        # startup must not fail merely because taskbar grouping is unavailable.
        return


def application_icon_path() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "sectvoice" / "assets" / "VoiceIcon.ico"
    return Path(__file__).resolve().parents[1] / "assets" / "VoiceIcon.ico"


def apply_application_icon(application: QApplication) -> Path:
    path = application_icon_path()
    if not path.is_file():
        raise FileNotFoundError(f"找不到SectVoice应用图标：{path}")
    icon = QIcon(str(path))
    if icon.isNull():
        raise RuntimeError(f"无法读取SectVoice应用图标：{path}")
    application.setWindowIcon(icon)
    return path
