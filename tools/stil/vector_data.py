"""Bounded decoding of the supported STIL WaveformChar data format.

WaveformChar names are case-sensitive selectors, not timing event codes.
Only the next whitespace-delimited group is affected by ``\\rN``; this is
character repetition within one Vector, never repetition of timed Vectors.
"""

from __future__ import annotations

import re

from .errors import StilError


WAVEFORM_CHAR_ALPHABET = "01ZXRFPNHLTUDhludpn"
WAVEFORM_CHAR_CLASS = "[" + WAVEFORM_CHAR_ALPHABET + "]"
_CHARS = re.compile(WAVEFORM_CHAR_CLASS + "+")
_GROUPS = re.compile(r"\S+")
_REPEAT = re.compile(r"\\r([0-9]+)")
# A malformed/unbound target must not let a short input allocate gigabytes.
# Known group widths impose a tighter bound before any repetition is expanded.
MAX_VECTOR_CHARACTERS = 1_100_000


def decode_vector_data(data: str, *, max_width: int | None = None) -> str:
    """Decode WaveformChars, optional ``\\w``, and positive ``\\rN group``.

    Callers add their source location to errors. The cap also applies to
    literal data; normal target-width validation remains in the resolver.
    """

    limit = MAX_VECTOR_CHARACTERS if max_width is None else min(max_width, MAX_VECTOR_CHARACTERS)
    if _CHARS.fullmatch(data):
        if len(data) > MAX_VECTOR_CHARACTERS:
            raise StilError("ERR_VECTOR_DATA_LIMIT", "vector data exceeds the character expansion limit")
        return data
    groups = iter(_GROUPS.finditer(data))
    parts: list[str] = []
    size = 0
    for match in groups:
        group = match.group()
        if group.startswith(r"\w"):
            group = group[2:]
            if not group:
                continue
        repeat = 1
        if group.startswith(r"\r"):
            count = _REPEAT.fullmatch(group)
            if count is None:
                raise StilError("ERR_UNSUPPORTED_PATTERN_DATA", "expected \\rN followed by a WaveformChar group")
            digits = count.group(1).lstrip("0") or "0"
            # Bound the decimal input before int(), including Python's own
            # large-decimal conversion guard and arbitrary-precision growth.
            if len(digits) > len(str(limit)):
                raise StilError("ERR_VECTOR_DATA_LIMIT", "vector repetition exceeds the target width or expansion limit")
            repeat = int(digits)
            if repeat < 1:
                raise StilError("ERR_UNSUPPORTED_PATTERN_DATA", "vector repetition count must be positive")
            next_group = next(groups, None)
            if next_group is None:
                raise StilError("ERR_UNSUPPORTED_PATTERN_DATA", "vector repetition is missing its next character group")
            group = next_group.group()
        if not _CHARS.fullmatch(group):
            raise StilError("ERR_UNSUPPORTED_PATTERN_DATA", f"unsupported WaveformChar group {group!r}")
        expanded_size = len(group) * repeat
        if size + expanded_size > limit:
            raise StilError("ERR_VECTOR_DATA_LIMIT", "vector data exceeds the target width or expansion limit")
        parts.append(group * repeat)
        size += expanded_size
    if not parts:
        raise StilError("ERR_UNSUPPORTED_PATTERN_DATA", "empty vector data")
    return "".join(parts)
