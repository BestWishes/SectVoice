from __future__ import annotations

from collections.abc import Callable
import traceback
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(object)
    finished = Signal()


class BackgroundTask(QRunnable):
    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self.function())
        except Exception:
            self.signals.failed.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()
