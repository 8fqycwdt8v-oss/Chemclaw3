"""Substituting `${inputs.x}` and `${steps.id.result}` — the only computation a template does.

Kept pure and dependency-free on purpose: this runs *inside* the workflow
(`chemclaw.durable.template_job`), where anything non-deterministic breaks replay. Given the same
inputs
and the same step results it must produce byte-identical arguments, every time, forever — which a
pure function over plain data is, and almost nothing else is.

Two substitution modes, and the distinction matters more than it looks:

- A **whole-string** reference (`"${inputs.smiles}"`) yields the referenced *value*, with its type.
  A tool that wants `list[str]` gets a list, not the string `"['CCO']"`.
- A reference **inside** a larger string interpolates its text, because that is what an agent step's
  prompt needs ("Summarize these flags: ${steps.hazards.result}").

An unresolved reference raises rather than yielding empty. A template that quietly passed `None`
into a calculation would produce a confident wrong answer, which is the failure this system can
least afford — and `chemclaw.templates.manifest` already rejects references that cannot resolve, so
reaching
this error at run time means something is wrong beyond a typo.
"""

import json
import re
from typing import Any

from chemclaw.core.errors import ChemclawError

# The same two forms `templates.manifest` validates, plus a whole-string variant used to decide
# between value substitution and text interpolation.
_REFERENCE = re.compile(r"\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result)\}")
_WHOLE = re.compile(r"^\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result)\}$")


class UnresolvedReference(ChemclawError):
    """A template referenced an input or step result that is not available.

    A `ChemclawError` (so a `ValueError`), registered in `chemclaw.durable.publish._BAD_DATA_TYPES`
    by its own class name: Temporal matches non-retryable error types by exact name, not
    isinstance, and raised inside a workflow step this becomes an `ActivityError`/task failure that
    must fail fast rather than retry a reference that will never resolve.
    """


def _lookup(reference: str, scope: dict[str, Any]) -> Any:
    """Resolve one reference against the run's scope, or raise naming what is available."""
    if reference not in scope:
        raise UnresolvedReference(
            f"template references {reference!r}, which is not available; have: {sorted(scope)}"
        )
    return scope[reference]


def _text(value: Any) -> str:
    """Render a value for interpolation into a larger string.

    JSON for anything structured, so a step result reaching a prompt is readable and unambiguous
    rather than a Python `repr` with single quotes that a model has to guess at. `default=str` keeps
    a stray non-JSON value (a datetime) from failing the whole run over formatting.
    """
    if isinstance(value, str):
        return value
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
