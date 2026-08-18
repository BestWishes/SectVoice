from __future__ import annotations

import json
from pathlib import Path

from sectvoice.core.reference_analysis import ReferenceQualityReport
from sectvoice.core.voice_compiler import VoiceCompiler


def test_reference_input_is_simplified_before_profile_storage(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"test")

    name, transcript = VoiceCompiler._validate_reference_input(
        " 测试声音 ",
        source,
        0,
        8,
        "不管怎麽樣，湯姆會繼續。",
    )

    assert name == "测试声音"
    assert transcript == "不管怎么样，汤姆会继续。"


def test_reference_diagnostics_keep_raw_and_final_transcripts(tmp_path: Path) -> None:
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

    VoiceCompiler._write_reference_diagnostics(
        tmp_path,
        report,
        raw_asr_transcript="湯姆會繼續。",
        final_transcript="汤姆会继续。",
    )

    payload = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    assert payload["raw_asr_transcript"] == "湯姆會繼續。"
    assert payload["displayed_asr_transcript"] == "汤姆会继续。"
    assert payload["final_transcript"] == "汤姆会继续。"
    assert payload["quality"]["grade"] == "good"
    assert payload["quality"]["is_usable"] is True
