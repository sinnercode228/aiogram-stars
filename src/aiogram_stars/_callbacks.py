"""Calling user callbacks the way aiogram calls handlers."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["Callback", "CallbackFunc"]

CallbackFunc = Callable[..., Awaitable[object]]
"""An async callable: the subject first, then any keyword arguments it names."""

_NAMED = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _signature(func: CallbackFunc) -> inspect.Signature:
    if sys.version_info >= (3, 14):  # pragma: no cover - annotations are lazy from 3.14
        import annotationlib  # noqa: PLC0415

        return inspect.signature(func, annotation_format=annotationlib.Format.FORWARDREF)
    return inspect.signature(func)


class Callback:
    """A user callback that takes a subject and the handler data it names.

    Like an aiogram handler, the callback gets only the keyword arguments its signature names,
    or all of them if it takes ``**kwargs``.
    """

    __slots__ = ("_first", "_func", "_names", "_takes_all")

    def __init__(self, func: CallbackFunc, *, role: str) -> None:
        params = list(_signature(func).parameters.values())
        positional = (inspect.Parameter.POSITIONAL_ONLY, *_NAMED)
        if not params or params[0].kind not in positional:
            msg = f"{role} must accept a positional argument for the object it handles"
            raise TypeError(msg)
        self._func = func
        self._first = params[0].name
        self._names = frozenset(p.name for p in params[1:] if p.kind in _NAMED)
        self._takes_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)

    async def __call__(self, subject: object, data: Mapping[str, Any]) -> object:
        """Call the callback with ``subject`` and the parts of ``data`` it asks for.

        Returns:
            What the callback returned.
        """
        if self._takes_all:
            kwargs = {name: value for name, value in data.items() if name != self._first}
        else:
            kwargs = {name: value for name, value in data.items() if name in self._names}
        return await self._func(subject, **kwargs)
