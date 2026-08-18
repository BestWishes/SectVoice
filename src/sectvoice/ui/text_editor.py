from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QPlainTextEdit


class ReaderTextEdit(QPlainTextEdit):
    characterDoubleClicked = Signal(int)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        position = self.cursorForPosition(event.position().toPoint()).position()
        super().mouseDoubleClickEvent(event)
        self.characterDoubleClicked.emit(position)

