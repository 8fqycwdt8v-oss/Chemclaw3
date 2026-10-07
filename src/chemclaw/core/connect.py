"""Attaching a database this system does not own: resolve its driver, read its credentials.

Shared by the warehouse ELN and corpora (`ingest/eln/warehouse/`), the result store (`publish/`) and
the external vector store (`retrieval/vectors/`). Drivers are `module:callable` references resolved
on first use, so an unused client is never imported. Keys ending in `_env` name an environment
variable that is read at connect time and registered for log redaction, so manifests carry no
secrets and rotation needs no deploy.

The driver's signature is the schema (D-2026-08-26-the-driver-s-signature-is-the-schema): every key
of a `connection:` block except `driver:` is passed as a keyword argument, so a new database is one
driver plus one manifest. The error type is a parameter because Temporal matches
`non_retryable_error_types` by class name, and each seam lists its own (`BindingError`,
`SinkConnectionError`).
"""

import importlib
import inspect
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from chemclaw.core.logging import register_secret_env

logger = logging.getLogger(__name__)

# Suffix marking a key as naming an environment variable rather than carrying a value; generic
# because every driver names its secret differently.
ENV_SUFFIX = "_env"

# What an environment variable name looks like. Not a security boundary; it catches a secret pasted
# where its name belongs (`password_env: hunter2`).
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# A relation, column or schema name, optionally dotted. A connection block contributes only bound
# parameters and identifiers of this shape. `$` is legal inside a warehouse identifier, not first.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*$")


def check_env_name(key: str, value: str, *, error: type[Exception]) -> None:
    """Raise unless `value` looks like the name of an environment variable.

    Called when a manifest loads and again when the connection opens. Blank is refused: an empty
    name would drop the credential keyword, and the driver would then attach anonymously or raise a
    retryable `TypeError`.
    """
    if not value:
        raise error(
            f"{key} names the environment variable holding the credential (like "
            "DATABRICKS_TOKEN) and was left blank; remove the key if this connection takes no "
            "credential, rather than leaving it empty"
        )
    if not _ENV_NAME.fullmatch(value):
        raise error(
            f"{key} holds the NAME of an environment variable (like DATABRICKS_TOKEN), "
            f"never its value; got {value!r}"
        )


def check_identifier(value: str, what: str, *, error: type[Exception]) -> str:
    """Raise unless `value` is a bare or dotted SQL identifier safe to interpolate. Returns it.

    Used for identifiers that reach a statement or a process argument (e.g. libpq `options`, where
    whitespace would smuggle extra `-c` flags). `error` is the caller's non-retryable class.
    """
    # `fullmatch`, because `match` with a `$` anchor also accepts one trailing newline.
    if not _IDENTIFIER.fullmatch(value):
        raise error(
            f"{what} {value!r} is not a plain SQL identifier; a binding may only name relations "
            "and columns, and every value it contributes is a bound parameter"
        )
    return value


def resolve_driver(reference: str, *, error: type[Exception], what: str = "driver") -> Any:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise error(
            f"{what} {reference!r} is not 'module:callable' "
            "(e.g. 'chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse')"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise error(
            f"cannot import {module_name!r} for {what} {reference!r}: {exc}. "
            "A driver's client package is installed only where that database is actually reached."
        ) from exc
    driver = getattr(module, attribute, None)
    if driver is None:
        raise error(f"{module_name!r} has no attribute {attribute!r} (from {reference!r})")
    if not callable(driver):
        raise error(f"{reference!r} is not callable")
    return driver


#: The annotations `option_type_mismatch` judges, and the YAML scalar types each accepts. Only the
#: four scalars: unions, optionals and containers are passed over rather than guessed at. `bool`
#: accepts only `bool` (not `0`/`1`), and `int` refuses `bool`.
_ACCEPTED_SCALARS: dict[type, tuple[type, ...]] = {
    bool: (bool,),
    int: (int,),
    float: (int, float),
    str: (str,),
}


def _evaluated_parameters(target: Any) -> Mapping[str, inspect.Parameter] | None:
    """`target`'s parameters with string annotations evaluated, or `None` if it has no signature.

    Evaluated because `from __future__ import annotations` stores annotations as strings, which
    `_ACCEPTED_SCALARS` would never match. An unevaluable annotation (a `TYPE_CHECKING` import)
    falls back to the raw signature and that parameter is skipped.
    """
    try:
        return inspect.signature(target, eval_str=True).parameters
    except (NameError, TypeError, ValueError, AttributeError, SyntaxError):
        pass
    try:
        return inspect.signature(target).parameters
    except (TypeError, ValueError):
        return None


def option_type_mismatch(target: Any, options: Mapping[str, Any]) -> str:
    """Empty if every option's value fits the parameter's annotation; a message naming the first.

    `signature_mismatch` checks only names, which suffices for a `connection:` block; a `config:`
    block's values are behaviour. A quoted `"false"` for a `bool` parameter is a truthy string that
    silently inverts the flag. Only `_ACCEPTED_SCALARS` annotations are judged; anything else is
    passed over.

    Args:
        target: The callable the options will be passed to.
        options: The manifest's block, as parsed.

    Returns:
        An empty string when nothing is judged wrong, else one sentence naming the key, what was
        written, and what the parameter is annotated as.
    """
    parameters = _evaluated_parameters(target)
    if parameters is None:
        # Same reason `signature_mismatch` returns empty: a callable with no introspectable
        # signature is "nothing to say", not a failure. A C `connect` is the ordinary case.
        return ""
    for key, value in options.items():
        parameter = parameters.get(key)
        if parameter is None:
            continue  # `signature_mismatch` owns the unknown-key case, and names it better.
        accepted = _ACCEPTED_SCALARS.get(parameter.annotation)
        # `bool` is a subclass of `int`, so `isinstance` alone would let `True` through an `int` or
        # `float` parameter — the coercion `_ACCEPTED_SCALARS` says it refuses.
        if accepted is None:
            continue
        if isinstance(value, accepted) and (bool in accepted or not isinstance(value, bool)):
            continue
        written = f"{value!r}"
        got = type(value).__name__
        article = "an" if got[0] in "aeiou" else "a"
        return (
            f"{key}={written} is {article} {got} where {parameter.annotation.__name__} is "
            f"declared. A manifest value reaches the callable exactly as YAML parsed it, so "
            f"{written} is not coerced — and for a flag that means a quoted word is truthy and "
            f"does the opposite of what it says. Write a flag as `true` or `false`."
        )
    return ""


def signature_mismatch(driver: Any, connection: Mapping[str, Any]) -> str:
    """Empty if `driver` accepts this block's keys; a message naming what it will not take if not.

    Shared by `make datasource-validate` and `make sink-validate`; `*_env` keys are checked by their
    stem. Values are bound as empty strings: nothing connects and no credential is read. A driver
    with no introspectable signature (`inspect.signature` raises `ValueError` for C callables like
    `sqlite3.connect`) yields "nothing to say", not an unnamed exception.
    """
    options = {
        key[: -len(ENV_SUFFIX)] if key.endswith(ENV_SUFFIX) else key: ""
        for key in connection
        if key != "driver"
    }
    try:
        signature = inspect.signature(driver)
    except (TypeError, ValueError):
        return ""
    try:
        signature.bind(**options)
    except TypeError as exc:
        return f"does not accept its block ({sorted(options)}): {exc}"
    return ""


def connect_options(
    connection: Mapping[str, Any], *, error: type[Exception], what: str = "connection"
) -> dict[str, Any]:
    """The keyword arguments a driver is built with: addresses from the block, secrets from env.

    Every `*_env` key becomes its stem, read from the environment after registering the name for log
    redaction. An empty variable is treated as absent, never as an anonymous login; an empty name
    raises (see `check_env_name`).
    """
    options: dict[str, Any] = {}
    for key, value in connection.items():
        if key == "driver":
            continue
        if not key.endswith(ENV_SUFFIX):
            options[key] = value
            continue
        variable = str(value or "")
        check_env_name(key, variable, error=error)
        register_secret_env(variable)
        # Read at call time rather than captured at import: a worker whose secret was rotated in
        # place sees the new value on its next connection.
        resolved = os.environ.get(variable, "")
        if not resolved:
            raise error(
                f"the {what} names {variable!r} for its {key[: -len(ENV_SUFFIX)]!r}, "
                "but that environment variable is unset or empty"
            )
        options[key[: -len(ENV_SUFFIX)]] = resolved
    return options


def open_connection(
    connection: Mapping[str, Any], *, error: type[Exception], what: str = "connection"
) -> Any:
    """Build whatever `connection['driver']` names, from the rest of the block.

    Returns the driver's own object; each seam checks its own contract. This function owns only
    resolution and credentials.
    """
    reference = str(connection.get("driver") or "")
    if not reference:
        raise error(f"a {what} block must name a `driver:`")
    driver = resolve_driver(reference, error=error, what=f"{what} driver")
    options = connect_options(connection, error=error, what=what)
    # Re-run the validators' signature check: a deployment's own manifests were never validated in
    # CI, and the constructor's `TypeError` would be retried by every job instead of failing as
    # `error`.
    if mismatch := signature_mismatch(driver, connection):
        raise error(f"{what} driver {reference!r} {mismatch}")
    # Log which database this pod attached to. Resolved secrets sit in `options` under their stem,
    # so `_is_address` filters by key.
    logger.info(
        "opening %s via %s (%s)",
        what,
        reference,
        ", ".join(f"{key}={value}" for key, value in sorted(options.items()) if _is_address(key)),
    )
    return driver(**options)


def _is_address(key: str) -> bool:
    """Whether a resolved option is safe to name in a log line.

    An allow-list of address words, since a deny-list of credential words would miss an unusual
    secret keyword.
    """
    return key in {"host", "port", "database", "catalog", "schema", "server_hostname", "url"}
