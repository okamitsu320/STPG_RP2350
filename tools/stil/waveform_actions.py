"""Common interpretation of STIL waveform actions.

The waveform character (for example ``P`` or ``N``) identifies the
WaveformChar selected by a vector.  The action attached to each event is the
authority for the value written at that event.  Keeping that distinction in a
single helper prevents the backends from silently replacing a sequence such
as ``Drive(0), Drive(1)`` with the final character value.
"""

from __future__ import annotations

from stpg.constants import STPG_DRIVE_ONE, STPG_DRIVE_ZERO

from .errors import StilError


# Internal compiler sentinel.  Drive(Z) is an output-enable update, not a
# firmware rate-slot drive opcode.
STIL_DRIVE_RELEASE = -1
ADDED_WAVEFORM_CHARS = frozenset("UDhludpn")
_CHAR_INTENT = {"U": "1", "D": "0", "u": "1", "d": "0",
                "h": "H", "l": "L", "p": "R", "n": "F"}


def event_char(event, *, char: str) -> str:  # type: ignore[no-untyped-def]
    """Interpret new selectors only *after* their exact WFT lookup.

    U and u, for example, can select different edge times. Do not uppercase
    either selector during resolution. Explicit actions remain authoritative.
    Legacy selectors retain their existing semantics and binary encoding.
    """

    if char not in ADDED_WAVEFORM_CHARS:
        return char
    if event.action_name == "Compare":
        argument = str(event.action_arg).strip()
        if argument == "1":
            return "H"
        if argument == "0":
            return "L"
        if argument.upper() == "X":
            return "X"
        raise StilError("ERR_UNSUPPORTED_COMPARE_ACTION", f"unsupported Compare({argument})")
    if event.action_name in {"DirZ", "DirInput"}:
        return "Z"
    if event.action_name in {"DirDrive", "DirOutput"} and _CHAR_INTENT[char] in {"H", "L"}:
        raise StilError("ERR_UNSUPPORTED_DIRECTION_ACTION",
                        "direction-only output on a compare selector is outside the supported subset")
    if event.action_name in {"Drive", "DirDrive", "DirOutput"}:
        return _CHAR_INTENT[char]
    raise StilError("ERR_UNSUPPORTED_WAVEFORM_ACTION", f"unsupported waveform action {event.action_name!r}")


def vector_compare_char(char: str, events) -> str:  # type: ignore[no-untyped-def]
    """Rate/Macro records have only one expected level per pin per vector."""

    if char not in ADDED_WAVEFORM_CHARS:
        return char
    levels = {event_char(event, char=char) for event in events if event.action_name == "Compare"}
    levels.discard("X")
    if len(levels) > 1:
        raise StilError("ERR_VECTOR_COMPARE_LEVEL_UNSUPPORTED",
                        "different expected levels for one pin in one vector require raw-event mode")
    return next(iter(levels), "X")


def drive_level_for_char(char: str) -> int | None:
    """Return the level implied by a vector character, if it is a drive."""

    char = _CHAR_INTENT.get(char, char)
    if char in ("1", "R", "P"):
        return 1
    if char in ("0", "F", "N"):
        return 0
    return None


def drive_op_for_event(event, *, char: str) -> int | None:  # type: ignore[no-untyped-def]
    """Resolve one ``Drive`` event to a firmware rate-slot drive operation.

    Explicit ``Drive(0)``/``Drive(1)``/``Drive(Z)`` always wins over the
    WaveformChar spelling.  The spelling fallback is retained for synthetic
    IR entries which do not carry an argument, and for compatibility with
    marker injection.  ``STIL_DRIVE_RELEASE`` is an internal sentinel; it is
    never serialized as a firmware drive opcode.
    """

    if event.action_name != "Drive":
        return None
    if event.action_arg is not None:
        argument = str(event.action_arg).strip()
        if argument == "0":
            return STPG_DRIVE_ZERO
        if argument == "1":
            return STPG_DRIVE_ONE
        if argument.upper() == "Z":
            return STIL_DRIVE_RELEASE
        raise StilError(
            "ERR_UNSUPPORTED_DRIVE_ACTION",
            f"Drive({argument}) is unsupported; expected Drive(0), Drive(1), or Drive(Z)",
        )
    level = drive_level_for_char(char)
    if level == 0:
        return STPG_DRIVE_ZERO
    if level == 1:
        return STPG_DRIVE_ONE
    return None


def final_drive_enable(char: str, events) -> bool | None:  # type: ignore[no-untyped-def]
    """Return the final output-enable intent, or ``None`` to preserve it."""

    level = drive_level_for_char(char)
    enabled: bool | None = True if level is not None else (False if char == "Z" else None)
    for event in events:
        if char in ADDED_WAVEFORM_CHARS and event.action_name in {"DirZ", "DirInput"}:
            enabled = False
            continue
        operation = drive_op_for_event(event, char=char)
        if operation in (STPG_DRIVE_ZERO, STPG_DRIVE_ONE):
            enabled = True
        elif operation == STIL_DRIVE_RELEASE:
            enabled = False
    return enabled


def final_drive_level(char: str, events) -> int | None:  # type: ignore[no-untyped-def]
    """Return the value left by the drive actions in one vector.

    P/N are often normalized to R/F for classification, but their event
    list can end in the opposite level (a pulse).  The last explicit Drive
    action therefore takes precedence over the character spelling.
    """

    level = drive_level_for_char(char)
    for event in events:
        operation = drive_op_for_event(event, char=char)
        if operation == STPG_DRIVE_ZERO:
            level = 0
        elif operation == STPG_DRIVE_ONE:
            level = 1
    return level
