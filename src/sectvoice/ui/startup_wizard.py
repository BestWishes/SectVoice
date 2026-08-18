from __future__ import annotations

from collections.abc import Callable
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from sectvoice.core.diagnostics import DiagnosticItem


class StartupWizard(QDialog):
    def __init__(
        self,
        diagnostics: Callable[[], tuple[DiagnosticItem, ...]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.diagnostics = diagnostics
        self.setWindowTitle("SectVoice 首次启动配置")
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        title = QLabel("首次启动检查")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(title)
        description = QLabel(
            "Reader不会静默下载大型模型。核心环境通过后可以进入；Basic和Standard可在主界面的语音包管理器中任选下载，实际创建声音时会生成测试语音。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["检查项", "结果", "详情"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.refresh_button = QPushButton("重新检查")
        layout.addWidget(self.refresh_button)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("进入Reader")
        layout.addWidget(self.buttons)
        self.refresh_button.clicked.connect(self.refresh)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.refresh()

    def refresh(self) -> None:
        items = self.diagnostics()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(item.name))
            result = QTableWidgetItem("通过" if item.passed else ("可选未通过" if not item.required else "未通过"))
            result.setForeground(Qt.GlobalColor.darkGreen if item.passed else Qt.GlobalColor.darkRed)
            self.table.setItem(row, 1, result)
            self.table.setItem(row, 2, QTableWidgetItem(item.detail))
        ready = all(item.passed for item in items if item.required)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ready)
        self.note.setText(
            "检查通过。"
            if ready
            else "必要检查尚未通过。请打开语音引擎/模型包管理器完成安装，随后点“重新检查”。"
        )
