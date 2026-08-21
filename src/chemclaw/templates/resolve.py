"""Substituting `${inputs.x}` and `${steps.id.result}` — the only computation a template does.

Kept pure on purpose: this runs *inside* the workflow (`chemclaw.durable.template_job`), where
anything non-deterministic breaks replay. Given the same inputs and the same step results it must
produce byte-identical arguments, every time, forever — which a pure function over plain data is,
and almost nothing else is.

It reads pydantic models as well as mappings, and that is the one dependency it has. A `job` step's
result *is* a model (`ConnectorJobResult`), so a resolver that could only walk dicts could not
address a field of one — and the workflow that imports this already imports pydantic at module
scope, so nothing new enters the sandbox.

Two substitution modes, and the distinction matters more than it looks:

- A **whole-string** reference (`"${inputs.smiles}"`) yields the referenced *value*, with its type.
  A tool that wants `list[str]` gets a list, not the string `"['CCO']"`.
- A reference **inside** a larger string interpolates its text, because that is what an agent step's
  prompt needs ("Summarize these flags: ${steps.hazards.result}").

**A step reference may name a field inside the result** (`${steps.search.result.summary}`), and
without that the deterministic path could not chain a computed value at all
(`D-2026-08-21-a-geometry-is-an-address-not-a-payload`). A `job` step's result is the whole
`ConnectorJobResult` envelope, so the only thing a later step could receive was the envelope — which
satisfies no next step's argument schema. The consequence was that every template wanting to carry a
number or an address from one calculation into the next had to route it through an *agent* step that
re-typed it, putting an LLM in the middle of the one execution mode that exists to keep it out.

It is a dotted **attribute path** and nothing more: no indexing, no wildcards, no expressions, no
arithmetic. That is the same line `chemclaw.templates.manifest` already draws and the reason it is
drawn — a config format with expressions is a programming language with no debugger. Selecting a
field out of a value the run already holds is addressing, not computation.

An unresolved reference raises rather than yielding empty. A template that quietly passed `None`
into a calculation would produce a confident wrong answer, which is the failure this system can
least afford — and `chemclaw.templates.manifest` already rejects references that cannot resolve, so
reaching
this error at run time means something is wrong beyond a typo.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from chemclaw.core.errors import ChemclawError

# The same two forms `templates.manifest` validates, plus a whole-string variant used to decide
# between value substitution and text interpolation. A step reference may carry a dotted field path
# after `.result`; an input reference may not, because an input is a declared scalar or list rather
# than a structure a step produced.
_REFERENCE = re.compile(
    r"\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result(?:\.[a-z][a-z0-9_]*)*)\}"
)
_WHOLE = re.compile(f"^{_REFERENCE.pattern}$")


class UnresolvedReference(ChemclawError):
    """A template referenced an input or step result that is not available.

    A `ChemclawError` (so a `ValueError`), registered in `chemclaw.durable.publish._BAD_DATA_TYPES`
    by its own class name: Temporal matches non-retryable error types by exact name, not
    isinstance, and raised inside a workflow step this becomes an `ActivityError`/task failure that
    must fail fast rather than retry a reference that will never resolve.
    """


def _field(value: Any, name: str, reference: str) -> Any:
    """One step of a dotted path into a step result, or raise naming what is there.

    Mappings and pydantic models both, because a step result is one or the other depending on which
    kind of step produced it — a `tool` step returns whatever its tool returns, a `job` step returns
    a `ConnectorJobResult`. A caller writing `${steps.x.result.summary}` means the same thing in
    both cases and should not have to know which.

    A missing field raises rather than yielding `None`, by the same argument the module makes for a
    missing reference: a template that quietly passed `None` into a calculation would produce a
    confident wrong answer.
    """
    if isinstance(value, Mapping) and name in value:
        return value[name]
    if isinstance(value, BaseModel) and name in type(value).model_fields:
        return getattr(value, name)
    available = (
        sorted(value)
        if isinstance(value, Mapping)
        else sorted(type(value).model_fields)
        if isinstance(value, BaseModel)
        else []
    )
    raise UnresolvedReference(
        f"template references {reference!r}, but {name!r} is not a field of the "
        f"{type(value).__name__} it names" + (f"; it has: {available}" if available else "")
    )


def _lookup(reference: str, scope: dict[str, Any]) -> Any:
    """Resolve one reference against the run's scope, or raise naming what is available.

    A step reference may address a field inside the result, so the longest prefix that is in scope
    is taken first and the remainder walked as a field path. Longest-prefix rather than "split on
    the third dot", because a step id may itself contain no dots but `inputs.x` and
    `steps.x.result` have different shapes, and one rule that works for both is one rule.
    """
    if reference in scope:
        return scope[reference]
    head, _, tail = reference.rpartition(".")
    while head:
        if head in scope:
            value = scope[head]
            for name in tail.split("."):
                value = _field(value, name, reference)
            return value
        head, _, rest = head.rpartition(".")
        tail = f"{rest}.{tail}"
    raise UnresolvedReference(
        f"template references {reference!r}, which is not available; have: {sorted(scope)}"
    )


def _text(value: Any) -> str:
    """Render a value for interpolation into a larger string.

    JSON for anything structured, so a step result reaching a prompt is readable and unambiguous
    rather than a Python `repr` with single quotes that a model has to guess at. `default=str` keeps
    a stray non-JSON value (a datetime) from failing the whole run over formatting.

    **A pydantic model is dumped, not `str()`-ed, and the docstring above was false for the one
    step kind whose result is always a model.** `json.dumps` cannot serialize a `BaseModel`, so it
    fell through `default=str` to the model's `repr` and then quoted *that* as a JSON string: a
    `job` step's result reached a later agent step as
    `"summary='...' data={'kind': 'ensemble', ...} note=None"` — single quotes, `None`, and a
    stringly-typed envelope, which is exactly what this function exists to avoid.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, default=str)


def resolve(value: Any, scope: dict[str, Any]) -> Any:
    """Substitute every reference in `value` against `scope`, recursing through lists and dicts.

    Args:
        value: An argument tree (or a prompt string) straight from the template.
        scope: `{"inputs.x": …, "steps.id.result": …}` — what has been resolved so far.

    Returns:
        The same shape with references replaced: whole-string references by value, embedded ones by
        their rendered text.

    Raises:
        UnresolvedReference: When a reference names something absent from `scope`.
    """
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole:
            return _lookup(whole.group(1), scope)
        return _REFERENCE.sub(lambda m: _text(_lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {key: resolve(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, scope) for item in value]
    return value
