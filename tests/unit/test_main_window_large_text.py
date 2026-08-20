from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QTimer

from sectvoice.app import build_services
from sectvoice.paths import AppPaths
from sectvoice.ui.main_window import MainWindow


def test_large_text_save_runs_without_blocking_gui_thread(
    tmp_path: Path, qtbot, monkeypatch
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
    window = MainWindow(services)
    qtbot.addWidget(window)
    started = Event()
    release = Event()
    original_update = services.documents.update_text

    def delayed_update(document_id, source_text):
        started.set()
        assert release.wait(3)
        return original_update(document_id, source_text)

    monkeypatch.setattr(services.documents, "update_text", delayed_update)
    text = "长篇正文不会阻塞界面。\n" * 20_000
    window.editor.setPlainText(text)
    window.save_timer.stop()
    window._save_current_text()
    qtbot.waitUntil(started.is_set, timeout=3_000)

    gui_event_processed: list[bool] = []
    QTimer.singleShot(0, lambda: gui_event_processed.append(True))
    qtbot.waitUntil(lambda: bool(gui_event_processed), timeout=1_000)
    assert window._active_save is not None
    continued_after_save: list[bool] = []
    window._after_current_text_saved(lambda: continued_after_save.append(True))
    assert not continued_after_save

    release.set()
    qtbot.waitUntil(lambda: window._active_save is None, timeout=5_000)
    qtbot.waitUntil(lambda: bool(continued_after_save), timeout=1_000)
    assert services.documents.get(window.current_document.document_id).source_text == text
    services.engines.shutdown()


def test_pause_button_really_toggles_pause_and_resume(tmp_path: Path, qtbot) -> None:
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
    window = MainWindow(services)
    qtbot.addWidget(window)
    services.playback.state = "Preparing"

    window.pause_button.click()

    assert services.playback.is_paused
    assert services.playback.state == "Paused"
    assert window.pause_button.text() == "继续"

    services.playback._set_state("Preparing", "正在核对片段是否完整及词语边界")
    assert services.playback.state == "Paused"
    assert window.pause_button.text() == "继续"

    window.pause_button.click()

    assert not services.playback.is_paused
    assert services.playback.state == "Playing"
    assert window.pause_button.text() == "暂停"
    services.engines.shutdown()
