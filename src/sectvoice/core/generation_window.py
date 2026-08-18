from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Sequence
from uuid import UUID

from sectvoice.domain import EnginePayloadRef, SynthesisSettings, Tier
from sectvoice.reader.segmentation import SpeechUnit


DEFAULT_CHARS_PER_SECOND = 4.8
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKDOWN_LINE_PREFIX = re.compile(
    r"^(?:[ \t]{0,3}(?:>+[ \t]*|#{1,6}[ \t]+|(?:[-+*]|\d+[.)])[ \t]+))+"
)
_DIRECTION_ARROW = re.compile(r"[ \t]*(?:→|←|⇒|⇐|↔|⇔|->|<-|=>)[ \t]*")


@dataclass(frozen=True, slots=True)
class ResolvedSpeechUnit:
    """Reader unit plus the already compiled voice selected for this session."""

    unit: SpeechUnit
    voice_id: UUID
    tier: Tier
    payload: EnginePayloadRef
    reference_transcript: str = ""
    reference_language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class WindowPlanningPolicy:
    # The first completed window must be long enough to sound like one
    # continuous reading passage, not merely satisfy the buffer by splitting
    # a nearby fourth sentence into a fresh acoustic take.  A 10-second target
    # still bounds cold-start work, while the shared 15-second ceiling lets a
    # short/short/medium/medium passage remain one inference when it fits the
    # engine's request limit.
    first_target_seconds: float = 10.0
    first_minimum_seconds: float = 6.0
    first_maximum_seconds: float = 15.0
    steady_target_seconds: float = 12.0
    steady_minimum_seconds: float = 8.0
    steady_maximum_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not (
            0 < self.first_minimum_seconds
            <= self.first_target_seconds
            <= self.first_maximum_seconds
        ):
            raise ValueError("invalid first-window duration policy")
        if not (
            0 < self.steady_minimum_seconds
            <= self.steady_target_seconds
            <= self.steady_maximum_seconds
        ):
            raise ValueError("invalid steady-window duration policy")

    def durations(self, *, initial: bool) -> tuple[float, float, float]:
        if initial:
            return (
                self.first_minimum_seconds,
                self.first_target_seconds,
                self.first_maximum_seconds,
            )
        return (
            self.steady_minimum_seconds,
            self.steady_target_seconds,
            self.steady_maximum_seconds,
        )


@dataclass(frozen=True, slots=True)
class GenerationWindow:
    """Transient inference unit; it never owns Reader character ranges."""

    units: tuple[ResolvedSpeechUnit, ...]
    engine_text: str
    estimated_seconds: float
    maximum_request_chars: int
    initial: bool

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("a generation window must contain at least one unit")
        if not self.engine_text.strip():
            raise ValueError("a generation window must contain audible text")
        if self.estimated_seconds <= 0:
            raise ValueError("estimated duration must be positive")

    @property
    def cache_text(self) -> str:
        """Canonical ordered text/boundary identity, safe across documents."""

        return json.dumps(
            [unit.unit.text for unit in self.units],
            ensure_ascii=False,
            separators=(",", ":"),
        )


def plan_next_generation_window(
    units: Sequence[ResolvedSpeechUnit],
    *,
    start_index: int,
    settings: SynthesisSettings,
    maximum_request_chars: int,
    chars_per_second: float = DEFAULT_CHARS_PER_SECOND,
    initial: bool = False,
    policy: WindowPlanningPolicy | None = None,
) -> GenerationWindow:
    """Accumulates compatible units by predicted duration, never sentence count."""

    if start_index < 0 or start_index >= len(units):
        raise IndexError(start_index)
    if chars_per_second <= 0:
        raise ValueError("chars_per_second must be positive")
    policy = policy or WindowPlanningPolicy()
    minimum_seconds, target_seconds, maximum_seconds = policy.durations(
        initial=initial
    )
    first = units[start_index]
    compatibility = _compatibility_key(first)
    selected: list[ResolvedSpeechUnit] = []
    estimated_seconds = 0.0

    for candidate in units[start_index:]:
        if selected and _compatibility_key(candidate) != compatibility:
            break

        candidate_seconds = estimate_unit_seconds(
            candidate.unit.text,
            settings,
            chars_per_second=chars_per_second,
        )
        if selected:
            if candidate_seconds >= maximum_seconds:
                break
            proposed = selected + [candidate]
            proposed_text = build_window_engine_text(proposed)
            if maximum_request_chars > 0 and len(proposed_text) > maximum_request_chars:
                break
            if (
                estimated_seconds >= minimum_seconds
                and estimated_seconds + candidate_seconds > maximum_seconds
            ):
                break
            if estimated_seconds >= target_seconds:
                break

        selected.append(candidate)
        estimated_seconds += candidate_seconds

        # A genuinely long unit stands alone. Engine-private splitting may be
        # required, but Reader still sees one SpeechUnit and one window.
        if len(selected) == 1 and candidate_seconds >= maximum_seconds:
            break

    engine_text = build_window_engine_text(selected)
    return GenerationWindow(
        units=tuple(selected),
        engine_text=engine_text,
        estimated_seconds=estimated_seconds,
        maximum_request_chars=maximum_request_chars,
        initial=initial,
    )


def estimate_unit_seconds(
    text: str,
    settings: SynthesisSettings,
    *,
    chars_per_second: float = DEFAULT_CHARS_PER_SECOND,
) -> float:
    """Estimates audible speech plus the Reader-controlled following pause."""

    if chars_per_second <= 0:
        raise ValueError("chars_per_second must be positive")
    spoken = sum(character.isalnum() for character in text)
    internal_pause = sum(
        0.16 if character in "；;" else 0.10
        for character in text
        if character in "，,、：:；;"
    )
    speech_seconds = max(0.30, spoken / chars_per_second + internal_pause)
    pause_seconds = pause_milliseconds(text, settings) / 1000.0
    return speech_seconds / settings.speed + pause_seconds


def build_window_engine_text(units: Sequence[ResolvedSpeechUnit]) -> str:
    """Builds one continuous prompt while keeping Reader text untouched."""

    audible_units = [
        (resolved, speech_text_for_engine(resolved.unit.text))
        for resolved in units
    ]
    audible_units = [(resolved, text) for resolved, text in audible_units if text]
    parts: list[str] = []
    for index, (resolved, text) in enumerate(audible_units):
        if index < len(audible_units) - 1 and not _has_terminal_boundary(text):
            # Layout-only newlines otherwise disappear in engine normalization.
            # A non-spoken punctuation mark supplies a stable acoustic boundary
            # without modifying the Reader document or AudioChunk contract.
            text += "。" if resolved.unit.text.rstrip(" \t\u3000").endswith(("\n", "\r")) else "，"
        parts.append(text)
    return "".join(parts)


def speech_text_for_engine(text: str) -> str:
    """Convert document markup to audible text without changing Reader text.

    Markdown quote/list/heading markers and emphasis are visual syntax rather
    than words.  Direction arrows act as a short acoustic boundary.  The
    original SpeechUnit remains untouched, so character seeking, highlighting
    and cache ownership continue to use the exact document.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_MARKDOWN_LINE_PREFIX.sub("", line) for line in normalized.split("\n")]
    audible = "\n".join(lines)
    audible = _MARKDOWN_IMAGE.sub(lambda match: match.group(1), audible)
    audible = _MARKDOWN_LINK.sub(lambda match: match.group(1), audible)
    audible = _DIRECTION_ARROW.sub("，", audible)
    for marker in ("**", "__", "~~", "```"):
        audible = audible.replace(marker, "")
    audible = audible.replace("`", "")
    return audible.strip()


def pause_milliseconds(text: str, settings: SynthesisSettings) -> int:
    """Uses a visual line ending as the Reader's paragraph boundary."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    without_horizontal_space = normalized.rstrip(" \t\u3000")
    stripped = normalized.rstrip()
    if without_horizontal_space.endswith("\n"):
        return settings.paragraph_pause_ms
    if _has_terminal_boundary(stripped, include_minor=False):
        return settings.punctuation_pause_ms
    return 0


_TRAILING_CLOSERS = frozenset("”’\"'）)]】〕］》〉」』")
_MAJOR_BOUNDARIES = frozenset("。！？!?；;.")
_MINOR_BOUNDARIES = frozenset("，,、：:")


def _has_terminal_boundary(text: str, *, include_minor: bool = True) -> bool:
    """Recognize punctuation that appears immediately before closing marks."""

    remaining = text.rstrip()
    while remaining and remaining[-1] in _TRAILING_CLOSERS:
        remaining = remaining[:-1].rstrip()
    boundaries = _MAJOR_BOUNDARIES | (_MINOR_BOUNDARIES if include_minor else frozenset())
    return bool(remaining and remaining[-1] in boundaries)


def _compatibility_key(resolved: ResolvedSpeechUnit) -> tuple[object, ...]:
    payload = resolved.payload
    return (
        resolved.voice_id,
        resolved.tier,
        payload.engine_id,
        payload.engine_version,
        payload.payload_format_version,
        payload.sha256,
        str(payload.opaque_path),
        resolved.reference_language.lower(),
        resolved.reference_transcript,
    )
