"""Private synchronous invocation ownership, not a public prepared-plan input.

Nested accessory/capture/core calls share one sealed snapshot. Context reset
on every exit prevents reuse by later invocations. Thread ownership prevents
copied contexts from accidentally sharing mutable preparation state.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import get_ident
from typing import Any


@dataclass
class _Invocation:
    thread: int = field(default_factory=get_ident)
    plans: dict[tuple, Any] = field(default_factory=dict)


_invocation: ContextVar[_Invocation | None] = ContextVar("stpg_runner_invocation", default=None)


def _current_invocation() -> _Invocation | None:
    state = _invocation.get()
    return state if state is not None and state.thread == get_ident() else None


def _invocation_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _current_invocation() is not None:
            return function(*args, **kwargs)
        token = _invocation.set(_Invocation())
        try:
            return function(*args, **kwargs)
        finally:
            _invocation.reset(token)
    return wrapped
