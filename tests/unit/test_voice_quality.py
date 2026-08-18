from sectvoice.core.voice_quality import (
    basic_cross_language_warning,
    infer_reference_language,
    to_simplified_chinese,
)


def test_reference_language_is_inferred_from_corrected_transcript() -> None:
    assert infer_reference_language("这是一段中文参考。") == "zh-CN"
    assert infer_reference_language("This is an English reference.") == "en"
    assert infer_reference_language("これは日本語です。") == "ja"
    assert infer_reference_language("한국어 음성입니다.") == "ko"


def test_basic_warns_for_cross_language_chinese_reading() -> None:
    assert basic_cross_language_warning("zh-CN") is None
    assert "漏词" in (basic_cross_language_warning("en") or "")


def test_asr_traditional_chinese_is_converted_before_display_and_storage() -> None:
    assert to_simplified_chinese("不管怎麽樣，我和湯姆還是要感謝你。") == (
        "不管怎么样，我和汤姆还是要感谢你。"
    )


def test_simplified_conversion_does_not_rewrite_latin_or_punctuation() -> None:
    assert to_simplified_chinese("OpenAI，测试 123！") == "OpenAI，测试 123！"
