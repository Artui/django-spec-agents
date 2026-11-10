from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext


def run_context_parameter(sig: inspect.Signature) -> str | None:
    """The name of ``sig``'s leading ``RunContext`` parameter, if it has one."""
    params = list(sig.parameters.values())
    if params and _is_run_context(params[0].annotation):
        return params[0].name
    return None


def _is_run_context(annotation: Any) -> bool:
    """Whether ``annotation`` denotes a ``RunContext``, parameterised or bare."""
    target = typing.get_origin(annotation) or annotation
    return isinstance(target, type) and issubclass(target, RunContext)


def first_doc_paragraph(fn: Callable[..., Any]) -> str:
    """The first paragraph of ``fn``'s docstring on one line, ``""`` without one."""
    doc = inspect.getdoc(fn)
    if not doc:
        return ""
    paragraph: list[str] = []
    for line in doc.splitlines():
        if not line.strip():
            break
        paragraph.append(line)
    return " ".join(paragraph).strip()


__all__ = ["first_doc_paragraph", "run_context_parameter"]
