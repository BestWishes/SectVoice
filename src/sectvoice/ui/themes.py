from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


DEFAULT_THEME_ID = "warm_paper"


@dataclass(frozen=True, slots=True)
class ThemeDefinition:
    """One complete, named Reader appearance.

    Keeping the colours as semantic tokens prevents dialogs and controls from
    growing their own unrelated hard-coded palettes.  Voice engines never see
    this object; themes are deliberately a UI-only concern.
    """

    theme_id: str
    display_name: str
    window: str
    panel: str
    reader: str
    elevated: str
    text: str
    muted_text: str
    border: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    selection: str
    selected_text: str
    reading_highlight: str
    danger: str
    danger_hover: str
    scrollbar: str


THEMES: tuple[ThemeDefinition, ...] = (
    ThemeDefinition(
        theme_id="warm_paper",
        display_name="暖杏纸张",
        window="#F1EBE2",
        panel="#FAF6EF",
        reader="#FFFDF8",
        elevated="#FFFFFF",
        text="#342E29",
        muted_text="#756B62",
        border="#DDD1C3",
        accent="#347FAD",
        accent_hover="#2D719B",
        accent_pressed="#275F82",
        accent_soft="#E1EFF6",
        selection="#D9EBF4",
        selected_text="#20313B",
        reading_highlight="#F6D88C",
        danger="#B85B52",
        danger_hover="#A64E47",
        scrollbar="#C5B7A8",
    ),
    ThemeDefinition(
        theme_id="soft_cream",
        display_name="柔和奶油",
        window="#F8F0DF",
        panel="#FFF9EC",
        reader="#FFFEF8",
        elevated="#FFFFFF",
        text="#3B3128",
        muted_text="#7C6C5D",
        border="#E5D3B7",
        accent="#BA7544",
        accent_hover="#A8673B",
        accent_pressed="#8F5732",
        accent_soft="#F7E5D2",
        selection="#F2DFC9",
        selected_text="#402D20",
        reading_highlight="#F5D488",
        danger="#B9534C",
        danger_hover="#A34741",
        scrollbar="#CEB99D",
    ),
    ThemeDefinition(
        theme_id="quiet_sage",
        display_name="静谧青绿",
        window="#E8EEEA",
        panel="#F3F7F4",
        reader="#FBFDFB",
        elevated="#FFFFFF",
        text="#29352F",
        muted_text="#68776F",
        border="#C9D7CF",
        accent="#4F7F6B",
        accent_hover="#426E5C",
        accent_pressed="#375C4D",
        accent_soft="#DCEAE3",
        selection="#D5E7DD",
        selected_text="#20342B",
        reading_highlight="#E8D58E",
        danger="#AD5B55",
        danger_hover="#984E49",
        scrollbar="#ACBEB4",
    ),
    ThemeDefinition(
        theme_id="night_reading",
        display_name="深夜阅读",
        window="#1E2327",
        panel="#272D31",
        reader="#20262A",
        elevated="#30373C",
        text="#ECE7DE",
        muted_text="#B4B0A8",
        border="#414A50",
        accent="#68B8E8",
        accent_hover="#7AC5F0",
        accent_pressed="#51A2D3",
        accent_soft="#2C4350",
        selection="#35566A",
        selected_text="#F5F2EA",
        reading_highlight="#705D32",
        danger="#E07A70",
        danger_hover="#ED8C83",
        scrollbar="#59636A",
    ),
)

_THEMES_BY_ID = {theme.theme_id: theme for theme in THEMES}


def theme_by_id(theme_id: str | None) -> ThemeDefinition:
    return _THEMES_BY_ID.get(str(theme_id), _THEMES_BY_ID[DEFAULT_THEME_ID])


def available_themes() -> tuple[ThemeDefinition, ...]:
    return THEMES


class ThemeManager(QObject):
    """Applies one palette to the whole process and exposes the active theme."""

    themeChanged = Signal(str)

    def __init__(self, application: QApplication | None = None) -> None:
        super().__init__()
        resolved = application or QApplication.instance()
        if not isinstance(resolved, QApplication):
            raise RuntimeError("ThemeManager需要已经创建的QApplication")
        self._application = resolved
        self._current = theme_by_id(DEFAULT_THEME_ID)

    @property
    def current(self) -> ThemeDefinition:
        return self._current

    @property
    def current_theme_id(self) -> str:
        return self._current.theme_id

    def apply(self, theme_id: str | None) -> ThemeDefinition:
        theme = theme_by_id(theme_id)
        self._application.setPalette(_palette(theme))
        self._application.setStyleSheet(_stylesheet(theme))
        self._application.setProperty("sectvoiceTheme", theme.theme_id)
        self._current = theme
        self.themeChanged.emit(theme.theme_id)
        return theme


def _palette(theme: ThemeDefinition) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.reader,
        QPalette.ColorRole.AlternateBase: theme.panel,
        QPalette.ColorRole.ToolTipBase: theme.elevated,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.elevated,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.BrightText: theme.selected_text,
        QPalette.ColorRole.Highlight: theme.selection,
        QPalette.ColorRole.HighlightedText: theme.selected_text,
        QPalette.ColorRole.Link: theme.accent,
        QPalette.ColorRole.LinkVisited: theme.accent_pressed,
        QPalette.ColorRole.PlaceholderText: theme.muted_text,
    }
    for role, colour in roles.items():
        palette.setColor(role, QColor(colour))
    disabled_text = QColor(theme.muted_text)
    disabled_text.setAlpha(150)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    return palette


def _stylesheet(theme: ThemeDefinition) -> str:
    # Qt Style Sheets intentionally use only supported, cross-version
    # properties.  The same rules cover the main window, first-run wizard,
    # voice creation, package manager and standard message/input dialogs.
    return f"""
QWidget {{
    color: {theme.text};
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}}
QMainWindow, QDialog, QMessageBox {{
    background-color: {theme.window};
}}
QWidget#appRoot {{
    background-color: {theme.window};
}}
QWidget#sidePanel, QWidget#readerPanel, QWidget#settingsPanel,
QWidget#playbackBar {{
    background-color: {theme.panel};
    border: 1px solid {theme.border};
    border-radius: 12px;
}}
QLabel#sectionTitle {{
    color: {theme.text};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#documentTitle {{
    color: {theme.text};
    font-size: 18px;
    font-weight: 600;
}}
QLabel#subtleText {{
    color: {theme.muted_text};
}}
QPlainTextEdit, QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox,
QComboBox, QListWidget, QTableWidget {{
    background-color: {theme.reader};
    color: {theme.text};
    border: 1px solid {theme.border};
    border-radius: 8px;
    selection-background-color: {theme.selection};
    selection-color: {theme.selected_text};
}}
QPlainTextEdit#readerText {{
    background-color: {theme.reader};
    border-radius: 10px;
    padding: 16px;
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    min-height: 30px;
    padding-left: 8px;
    padding-right: 6px;
}}
QTextEdit {{
    padding: 7px;
}}
QListWidget {{
    padding: 4px;
}}
QListWidget::item {{
    border-radius: 7px;
    padding: 7px 8px;
    margin: 1px 0;
}}
QListWidget::item:hover {{
    background-color: {theme.accent_soft};
}}
QListWidget::item:selected {{
    background-color: {theme.selection};
    color: {theme.selected_text};
}}
QTableWidget {{
    gridline-color: {theme.border};
    alternate-background-color: {theme.panel};
}}
QTableWidget::item {{
    padding: 6px;
}}
QHeaderView::section {{
    background-color: {theme.accent_soft};
    color: {theme.text};
    border: none;
    border-right: 1px solid {theme.border};
    border-bottom: 1px solid {theme.border};
    padding: 8px;
    font-weight: 600;
}}
QPushButton, QToolButton {{
    background-color: {theme.elevated};
    color: {theme.text};
    border: 1px solid {theme.border};
    border-radius: 8px;
    min-height: 30px;
    padding: 4px 11px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {theme.accent_soft};
    border-color: {theme.accent};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: {theme.selection};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {theme.panel};
    color: {theme.muted_text};
    border-color: {theme.border};
}}
QPushButton[kind="primary"] {{
    background-color: {theme.accent};
    color: #FFFFFF;
    border-color: {theme.accent};
    font-weight: 600;
}}
QPushButton[kind="primary"]:hover {{
    background-color: {theme.accent_hover};
    border-color: {theme.accent_hover};
}}
QPushButton[kind="primary"]:pressed {{
    background-color: {theme.accent_pressed};
    border-color: {theme.accent_pressed};
}}
QPushButton[kind="danger"] {{
    color: {theme.danger};
}}
QPushButton[kind="danger"]:hover {{
    color: #FFFFFF;
    background-color: {theme.danger_hover};
    border-color: {theme.danger_hover};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {theme.elevated};
    border: 1px solid {theme.border};
    selection-background-color: {theme.selection};
    selection-color: {theme.selected_text};
}}
QCheckBox {{
    spacing: 7px;
}}
QProgressBar {{
    min-height: 12px;
    background-color: {theme.accent_soft};
    border: 1px solid {theme.border};
    border-radius: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {theme.accent};
    border-radius: 5px;
}}
QStatusBar {{
    color: {theme.muted_text};
    background-color: {theme.window};
    border: none;
}}
QStatusBar::item {{
    border: none;
}}
QSplitter::handle {{
    background-color: transparent;
    width: 8px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 3px;
}}
QScrollBar::handle:vertical {{
    background: {theme.scrollbar};
    min-height: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {theme.scrollbar};
    min-width: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0;
}}
QToolTip {{
    color: {theme.text};
    background-color: {theme.elevated};
    border: 1px solid {theme.border};
    padding: 5px;
}}
"""
