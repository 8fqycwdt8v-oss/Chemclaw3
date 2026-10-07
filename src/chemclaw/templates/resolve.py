"""Substituting `${inputs.x}` and `${steps.id.result}` — the only computation a template does.

Pure on purpose: this runs inside the workflow (`chemclaw.durable.template_job`), where anything
non-deterministic breaks replay. It reads pydantic models as well as mappings, because a `job`
step's result is a `ConnectorJobResult`.

Two substitution modes:

- A **whole-string** reference (`"${inputs.smiles}"`) yields the referenced value with its type.
- A reference **inside** a larger string interpolates its text, as an agent step's prompt needs.

A step reference may name a field inside the result (`${steps.search.result.summary}`), a dotted
attribute path only, so a computed value can be chained without an agent step re-typing it.

An unresolved reference raises rather than yielding empty: passing `None` into a calculation gives a
confident wrong answer. The manifest already rejects unresolvable references, so this firing at run
time means something beyond a typo.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from chemclaw.core.errors import ChemclawError

# The two forms `templates.manifest` validates, plus a whole-string variant that chooses value
# substitution over interpolation. Only a step reference may carry a field path; an input is a
# declared scalar or list.
_REFERENCE = re.compile(
    r"\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result(?:\.[a-z][a-z0-9_]*)*)\}"
)
_WHOLE = re.compile(f"^{_REFERENCE.pattern}$")


class UnresolvedReference(ChemclawError):
    """A template referenced an input or step result that is not available.

    Registered by class name in `chemclaw.durable.publish._BAD_DATA_TYPES`, because Temporal matches
    non-retryable error types by exact name; it must fail fast rather than retry.
    """


def _field(value: Any, name: str, reference: str) -> Any:
    """One step of a dotted path into a step result, or raise naming what is there.

    Handles mappings and pydantic models alike, since a `tool` step returns whatever its tool
    returns and a `job` step returns a `ConnectorJobResult`. A missing field raises rather than
    yielding `None`.
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

    The longest in-scope prefix is taken first and the remainder walked as a field path, one rule
    for both `inputs.x` and `steps.x.result` shapes.
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

    JSON for anything structured, so a prompt gets an unambiguous rendering rather than a Python
    `repr`. A pydantic model is dumped first, since `json.dumps` cannot serialize one. `default=str`
    keeps a stray non-JSON value from failing the run.
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
