from __future__ import annotations

from functools import lru_cache

from opencc import OpenCC


@lru_cache(maxsize=1)
def _traditional_to_simplified() -> OpenCC:
    return OpenCC("t2s")


def to_simplified_chinese(text: str) -> str:
    """Return user-facing Chinese in Simplified form without touching other text.

    Faster-Whisper can emit Traditional Chinese for a Mandarin recording.  The
    conversion belongs at the VoiceProfile input boundary: Reader documents
    are never rewritten, while the editable/saved reference transcript has a
    stable script regardless of the ASR model's choice.
    """

    return _traditional_to_simplified().convert(text)


def infer_reference_language(transcript: str) -> str:
    """Infers the prompt language after the user has corrected its transcript."""

    if any("\uac00" <= character <= "\ud7af" for character in transcript):
        return "ko"
    if any(
        "\u3040" <= character <= "\u30ff" or "\u31f0" <= character <= "\u31ff"
        for character in transcript
    ):
        return "ja"
    cjk = sum("\u3400" <= character <= "\u9fff" for character in transcript)
    latin = sum(character.isascii() and character.isalpha() for character in transcript)
    if cjk:
        return "zh-CN"
    if latin:
        return "en"
    return "zh-CN"


def basic_cross_language_warning(reference_language: str) -> str | None:
    if reference_language.lower().startswith("zh"):
        return None
    return (
        "参考音频不是中文。基础语音包进行跨语种中文朗读时，可能出现漏词、"
        "异常停顿或语气漂移；优先使用同一人物的清晰中文参考，或改用中级语音包。"
    )
