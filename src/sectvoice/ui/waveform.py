from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


class WaveformWidget(QWidget):
    selectionChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self._waveform = np.zeros(0, dtype=np.float32)
        self._duration = 0.0
        self._start = 0.0
        self._end = 0.0
        self._drag_anchor = 0.0

    def set_waveform(self, waveform: np.ndarray, duration: float) -> None:
        self._waveform = np.asarray(waveform, dtype=np.float32)
        self._duration = max(0.0, float(duration))
        self._start = 0.0
        self._end = min(self._duration, 8.0)
        self.update()

    def set_selection(self, start: float, end: float, emit: bool = False) -> None:
        self._start = max(0.0, min(float(start), self._duration))
        self._end = max(self._start, min(float(end), self._duration))
        self.update()
        if emit:
            self.selectionChanged.emit(self._start, self._end)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#edf3f8"))
        if self._waveform.size:
            width = max(1, self.width())
            center = self.height() / 2
            values = self._waveform
            if values.size > width:
                indices = np.linspace(0, values.size - 1, width).astype(int)
                values = values[indices]
            painter.setPen(QPen(QColor("#277cb7"), 1))
            scale = self.height() * 0.46 / max(0.001, float(values.max()))
            for x, value in enumerate(values):
                xx = x * width / max(1, values.size - 1)
                amplitude = float(value) * scale
                painter.drawLine(QPointF(xx, center - amplitude), QPointF(xx, center + amplitude))
        if self._duration:
            left = self._start / self._duration * self.width()
            right = self._end / self._duration * self.width()
            painter.fillRect(
                QRectF(0, 0, left, self.height()), QColor(40, 50, 60, 70)
            )
            painter.fillRect(
                QRectF(right, 0, self.width() - right, self.height()), QColor(40, 50, 60, 70)
            )
            painter.setPen(QPen(QColor("#e67e22"), 2))
            painter.drawLine(QPointF(left, 0), QPointF(left, self.height()))
            painter.drawLine(QPointF(right, 0), QPointF(right, self.height()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._duration:
            return
        self._drag_anchor = self._seconds_at(event.position().x())
        self.set_selection(self._drag_anchor, self._drag_anchor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not event.buttons() & Qt.MouseButton.LeftButton or not self._duration:
            return
        current = self._seconds_at(event.position().x())
        self.set_selection(min(self._drag_anchor, current), max(self._drag_anchor, current))
        self.selectionChanged.emit(self._start, self._end)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selectionChanged.emit(self._start, self._end)

    def _seconds_at(self, x: float) -> float:
        return max(0.0, min(self._duration, x / max(1, self.width()) * self._duration))

