from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from sectvoice.core.asr import ASRResult
from sectvoice.core.reference_analysis import ReferenceQualityReport
from sectvoice.domain import VoiceProfile
from sectvoice.ui.voice_dialog import (
    ReferenceTranscriptionResult,
    VoiceCreationDialog,
    VoiceCreationResult,
)


def test_completed_background_task_restores_voice_dialog_controls(qtbot) -> None:
    packages = Mock()
    packages.active_for.return_value = object()
    dialog = VoiceCreationDialog(
        paths=Mock(),
        media=Mock(),
        asr=Mock(),
        compiler=Mock(),
        engines=Mock(),
        packages=packages,
        voices=Mock(),
    )
    qtbot.addWidget(dialog)

    assert dialog.create_button.property("kind") is None

    dialog._start_task(lambda: "done", lambda _result: None, "正在测试后台任务")

    assert not dialog.select_button.isEnabled()
    assert not dialog.asr_button.isEnabled()
    qtbot.waitUntil(lambda: not dialog._active_tasks, timeout=3_000)
    assert dialog.select_button.isEnabled()
    assert dialog.asr_button.isEnabled()
    assert dialog.create_button.isEnabled()
    assert dialog.buttons.button(dialog.buttons.StandardButton.Cancel).isEnabled()


def test_confirm_save_persists_name_edited_after_preview(qtbot) -> None:
    packages = Mock()
    packages.active_for.return_value = object()
    voices = Mock()
    dialog = VoiceCreationDialog(
        paths=Mock(),
        media=Mock(),
        asr=Mock(),
        compiler=Mock(),
        engines=Mock(),
        packages=packages,
        voices=voices,
    )
    qtbot.addWidget(dialog)
    profile = VoiceProfile.create(
        "旧名称",
        Path("source.wav"),
        Path("reference.wav"),
        "参考文字",
    )
    updated = replace(profile, name="007")
    voices.rename.return_value = updated
    dialog.result = VoiceCreationResult(profile, (), Path("preview.wav"))
    dialog.name_edit.setText("007")

    dialog._accept_if_ready()

    voices.rename.assert_called_once_with(profile.voice_id, "007")
    assert dialog.result.profile.name == "007"


def test_asr_result_is_displayed_as_simplified_chinese(qtbot) -> None:
    packages = Mock()
    packages.active_for.return_value = object()
    dialog = VoiceCreationDialog(
        paths=Mock(),
        media=Mock(),
        asr=Mock(),
        compiler=Mock(),
        engines=Mock(),
        packages=packages,
        voices=Mock(),
    )
    qtbot.addWidget(dialog)
    report = ReferenceQualityReport(
        duration_seconds=8,
        active_speech_seconds=7,
        active_ratio=0.875,
        peak_dbfs=-3,
        clipping_ratio=0,
        active_level_spread_db=4,
        longest_internal_pause_seconds=0.2,
        rapid_modulation_score=0.1,
        pitch_jitter_semitones=0.2,
        speaking_rate_chars_per_second=4,
        blocking_reasons=(),
        warnings=(),
    )

    dialog._asr_completed(
        ReferenceTranscriptionResult(
            ASRResult("不管怎麽樣，湯姆會繼續。", "zh", 0.99, ()),
            report,
        )
    )

    assert dialog.transcript.toPlainText() == "不管怎么样，汤姆会继续。"
    assert dialog._raw_asr_transcript == "不管怎麽樣，湯姆會繼續。"
    assert "简体" in dialog.status_label.text()


def test_changing_existing_reference_range_invalidates_old_transcript(qtbot) -> None:
    packages = Mock()
    packages.active_for.return_value = object()
    dialog = VoiceCreationDialog(
        paths=Mock(),
        media=Mock(),
        asr=Mock(),
        compiler=Mock(),
        engines=Mock(),
        packages=packages,
        voices=Mock(),
    )
    qtbot.addWidget(dialog)
    dialog.transcript.setPlainText("旧片段转写")
    dialog._transcript_selection = (0.0, 8.0)

    dialog._invalidate_transcription_after_selection_change(0.0, 9.0)

    assert dialog.transcript.toPlainText() == ""
    assert dialog._transcript_selection is None
    assert "重新处理" in dialog.quality_report.text()
