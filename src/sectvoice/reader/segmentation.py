from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


PRIMARY_BOUNDARIES = frozenset("。！？!?；;\n")
SECONDARY_BOUNDARIES = frozenset("，,、：:")
CLOSING_PUNCTUATION = frozenset("”’\"'）》】」』〕〉")
SEGMENTATION_VERSION = 3


@dataclass(frozen=True, slots=True)
class SpeechUnit:
    """Reader-owned exact slice of a document."""

    speech_unit_id: UUID
    ordinal: int
    start_char: int
    end_char: int
    text: str

    def contains(self, position: int) -> bool:
        return self.start_char <= position < self.end_char


class SegmentationMap:
    def __init__(self, source_text: str, units: tuple[SpeechUnit, ...]) -> None:
        self.source_text = source_text
        self.units = units
        self._validate()

    def _validate(self) -> None:
        cursor = 0
        rebuilt: list[str] = []
        for expected_ordinal, unit in enumerate(self.units):
            if unit.ordinal != expected_ordinal:
                raise ValueError("SpeechUnit ordinals are not contiguous")
            if unit.start_char != cursor or unit.end_char <= unit.start_char:
                raise ValueError("SpeechUnit ranges are not a contiguous cover")
            if self.source_text[unit.start_char : unit.end_char] != unit.text:
                raise ValueError("SpeechUnit text does not match source slice")
            cursor = unit.end_char
            rebuilt.append(unit.text)
        if cursor != len(self.source_text) or "".join(rebuilt) != self.source_text:
            raise ValueError("SpeechUnits do not losslessly cover the document")

    def locate(self, position: int) -> tuple[SpeechUnit, int]:
        if not self.source_text:
            raise IndexError("cannot locate a position in an empty document")
        if position < 0 or position >= len(self.source_text):
            raise IndexError(position)
        low = 0
        high = len(self.units) - 1
        while low <= high:
            middle = (low + high) // 2
            unit = self.units[middle]
            if position < unit.start_char:
                high = middle - 1
            elif position >= unit.end_char:
                low = middle + 1
            else:
                return unit, position - unit.start_char
        raise RuntimeError("validated segmentation map has an uncovered character")


def _consume_closing_marks(text: str, cursor: int, end: int) -> int:
    while cursor < end and text[cursor] in CLOSING_PUNCTUATION:
        cursor += 1
    return cursor


def _preferred_cut(
    text: str, start: int, end: int, max_chars: int, min_tail_chars: int
) -> int:
    target = min(start + max_chars, end)
    if target == end:
        return end

    search_floor = start + max(1, max_chars // 2)
    for index in range(target - 1, search_floor - 1, -1):
        if text[index] in SECONDARY_BOUNDARIES:
            candidate = _consume_closing_marks(text, index + 1, end)
            if end - candidate >= min_tail_chars:
                return candidate

    for index in range(target - 1, search_floor - 1, -1):
        if text[index].isspace():
            candidate = index + 1
            if end - candidate >= min_tail_chars:
                return candidate
    if end - target < min_tail_chars:
        return max(start + 1, end - min_tail_chars)
    return target


def _split_long_range(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = start
    min_tail_chars = max(8, min(16, max_chars // 3))
    while end - cursor > max_chars:
        cut = _preferred_cut(text, cursor, end, max_chars, min_tail_chars)
        if cut <= cursor:
            cut = min(cursor + max_chars, end)
        ranges.append((cursor, cut))
        cursor = cut
    if cursor < end:
        ranges.append((cursor, end))
    return ranges


def segment_text(text: str, max_chars: int = 120) -> SegmentationMap:
    """Losslessly segments text while preferring sentence and newline boundaries."""

    if max_chars < 16:
        raise ValueError("max_chars must be at least 16")
    if not text:
        return SegmentationMap(text, ())

    natural_ranges: list[tuple[int, int]] = []
    start = 0
    cursor = 0
    while cursor < len(text):
        char = text[cursor]
        cursor += 1
        if char in PRIMARY_BOUNDARIES:
            cursor = _consume_closing_marks(text, cursor, len(text))
            natural_ranges.append((start, cursor))
            start = cursor
    if start < len(text):
        natural_ranges.append((start, len(text)))

    # Newlines, ellipses and isolated punctuation must stay in the lossless map,
    # but must not become engine prompts by themselves. Attach them to a nearby
    # spoken unit so Reader cannot appear to skip or pause on punctuation units.
    merged_ranges: list[tuple[int, int]] = []
    leading_whitespace_start: int | None = None
    for range_start, range_end in natural_ranges:
        if not any(character.isalnum() for character in text[range_start:range_end]):
            if merged_ranges:
                previous_start, _ = merged_ranges[-1]
                merged_ranges[-1] = (previous_start, range_end)
            elif leading_whitespace_start is None:
                leading_whitespace_start = range_start
            continue
        if leading_whitespace_start is not None:
            range_start = leading_whitespace_start
            leading_whitespace_start = None
        merged_ranges.append((range_start, range_end))
    if leading_whitespace_start is not None:
        merged_ranges.append((leading_whitespace_start, len(text)))

    final_ranges: list[tuple[int, int]] = []
    for range_start, range_end in merged_ranges:
        final_ranges.extend(_split_long_range(text, range_start, range_end, max_chars))

    units = tuple(
        SpeechUnit(
            speech_unit_id=uuid4(),
            ordinal=ordinal,
            start_char=range_start,
            end_char=range_end,
            text=text[range_start:range_end],
        )
        for ordinal, (range_start, range_end) in enumerate(final_ranges)
    )
    return SegmentationMap(text, units)


def seek_start_within_unit(unit: SpeechUnit, offset: int, max_backtrack: int = 24) -> int:
    """Finds a nearby natural start inside a long unit for double-click seeking."""

    if offset < 0 or offset >= len(unit.text):
        raise IndexError(offset)
    floor = max(0, offset - max_backtrack)
    for index in range(offset - 1, floor - 1, -1):
        if unit.text[index] in PRIMARY_BOUNDARIES | SECONDARY_BOUNDARIES:
            return index + 1
    return offset
