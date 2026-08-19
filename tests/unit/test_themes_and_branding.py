from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon, QPalette

from sectvoice.app import build_services
from sectvoice.paths import AppPaths
from sectvoice.ui.branding import apply_application_icon, application_icon_path
from sectvoice.ui.main_window import MainWindow
from sectvoice.ui.themes import (
    DEFAULT_THEME_ID,
    ThemeManager,
    available_themes,
    theme_by_id,
)


def test_theme_catalog_has_stable_unique_ids_and_default() -> None:
    themes = available_themes()

    assert len(themes) == 4
    assert len({theme.theme_id for theme in themes}) == len(themes)
    assert theme_by_id(None).theme_id == DEFAULT_THEME_ID
    assert theme_by_id("removed-development-theme").theme_id == DEFAULT_THEME_ID


def test_theme_manager_applies_palette_and_stylesheet(qapp) -> None:
    manager = ThemeManager(qapp)

    theme = manager.apply("night_reading")

    assert manager.current_theme_id == "night_reading"
    assert qapp.property("sectvoiceTheme") == "night_reading"
    assert qapp.palette().color(QPalette.ColorRole.Window).name().upper() == theme.window
    assert theme.accent in qapp.styleSheet()
    assert 'QPushButton[kind="primary"]' in qapp.styleSheet()


def test_application_icon_is_multisize_and_can_be_applied(qapp) -> None:
    path = application_icon_path()
    icon = QIcon(str(path))

    assert path.is_file()
    assert not icon.isNull()
    sizes = {(size.width(), size.height()) for size in icon.availableSizes()}
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes
    assert apply_application_icon(qapp) == path
    assert not qapp.windowIcon().isNull()


def test_distribution_builds_use_the_same_windows_icon() -> None:
    root = Path(__file__).parents[2]
    spec = (root / "distribution" / "SectVoice.spec").read_text(encoding="utf-8")
    installer = (root / "distribution" / "SectVoice.iss").read_text(
        encoding="utf-8"
    )

    assert 'icon=str(icon_path)' in spec
    assert '"sectvoice/assets"' in spec
    assert "SetupIconFile=..\\src\\sectvoice\\assets\\VoiceIcon.ico" in installer
    assert "UninstallDisplayIcon={app}\\app\\SectVoiceReader.exe" in installer


def test_main_window_theme_selection_is_applied_and_persisted(
    tmp_path: Path, qtbot, qapp
) -> None:
    paths = AppPaths(
        root=tmp_path,
        source=Path(__file__).parents[2],
        runtime=tmp_path / "runtime",
        models=tmp_path / "models",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        downloads=tmp_path / "downloads",
        temp=tmp_path / "temp",
        artifacts=tmp_path / "artifacts",
    )
    services = build_services(paths)
    manager = ThemeManager(qapp)
    manager.apply(DEFAULT_THEME_ID)
    window = MainWindow(services, theme_manager=manager)
    qtbot.addWidget(window)

    window.theme_combo.setCurrentIndex(
        window.theme_combo.findData("quiet_sage")
    )

    assert manager.current_theme_id == "quiet_sage"
    assert services.settings.get("theme_id") == "quiet_sage"
    services.engines.shutdown()
