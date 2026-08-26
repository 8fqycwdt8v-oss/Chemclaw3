"""Attaching a database this system does not own: resolve its driver, read its credentials.

Three seams in this tree reach a database that belongs to somebody else — the warehouse ELN and
the reaction corpora inbound (`ingest/eln/warehouse/`), the result store outbound
(`publish/`), and the external vector store (`retrieval/vectors/`). All three attach the same way,
because the same two problems come up every time:

**Late-binding the driver.** A `module:callable` reference is resolved the first time a connection
is actually needed rather than at import, so a process that never queries a warehouse never imports
its client, and a repository with no vendor package installed still runs its whole test suite
against a fake named by a test's own manifest.

**Naming credentials rather than carrying them.** A key ending in `_env` holds the *name* of an
environment variable; the value is read at connect time and the name is registered with the
log-redaction inventory first. So a rotated secret is picked up by the next connection rather than
the next deploy, a missing one fails with a message naming the variable instead of an
authentication error from inside a client, and the manifest is safe to keep in a repository.

**The driver's signature is the schema, and that is the whole generality claim.** There is no model
here enumerating `host`, `account`, `warehouse`, `role` or any other vendor's words. Everything in
a `connection:` block except `driver:` is passed to the callable as a keyword argument, so
attaching a database this repository has never heard of is one driver module plus one manifest —
no field added to a shared model, no branch in an engine, no core edit. That rule was learned the
expensive way: the first connection model was Snowflake-shaped, the second driver had to redefine
three of its fields to mean something else and *refuse* two more that had no analogue, and
`publish/connect.py` declined to reuse it at all rather than make one model mean two things
(`D-2026-08-26-the-driver-s-signature-is-the-schema`).

**Why the error type is a parameter.** Temporal matches `non_retryable_error_types` by exact class
name, so which exception a failed attachment raises is a retry contract rather than a taxonomy
preference: a binding naming a driver that is not installed must fail the ingest seam as
`BindingError` and the publish seam as `SinkConnectionError`, because those are the names each
side's activity lists. A shared error class here would quietly make one of them retryable.
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

# The suffix marking a key as naming an environment variable rather than carrying a value. Generic
# rather than a fixed list of credential names, because every driver has its own: psycopg wants a
# password, a lakehouse a token, a vector database an API key, the next one something else. What
# they share is that the secret is *named* here, never written.
ENV_SUFFIX = "_env"

# What an environment variable name looks like. Not a security boundary — an author determined to
# paste a secret still can — but it catches the realistic mistake, which is filling in
# `password_env: hunter2` because the field sits where a password goes in every other tool.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def check_env_name(key: str, value: str, *, error: type[Exception]) -> None:
    """Raise unless `value` looks like the name of an environment variable.

    Called both when a manifest loads (so a typo fails at startup) and again when the connection is
    opened (so a block that reached the resolver by another route is checked once regardless).
    """
    if value and not _ENV_NAME.fullmatch(value):
        raise error(
            f"{key} holds the NAME of an environment variable (like DATABRICKS_TOKEN), "
            f"never its value; got {value!r}"
        )


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


def signature_mismatch(driver: Any, connection: Mapping[str, Any]) -> str:
    """Empty if `driver` accepts this block's keys; a message naming what it will not take if not.

    The offline half of "the driver's signature is the schema", shared by the two manifest
    validators (`make datasource-validate`, `make sink-validate`) because the rule they are checking
    is one rule — and specifically because the `_env` stripping is part of it: a block writes
    `access_token_env` and the driver is built with `access_token`, so a checker that forgot that
    would reject every correct binding that names a secret.

    Values are irrelevant here and are bound as empty strings: this asks what the callable
    *accepts*, with nothing connected and no credential read.
    """
    options = {
        key[: -len(ENV_SUFFIX)] if key.endswith(ENV_SUFFIX) else key: ""
        for key in connection
        if key != "driver"
    }
    try:
        inspect.signature(driver).bind(**options)
    except TypeError as exc:
        return f"does not accept its block ({sorted(options)}): {exc}"
    return ""


def connect_options(
    connection: Mapping[str, Any], *, error: type[Exception], what: str = "connection"
) -> dict[str, Any]:
    """The keyword arguments a driver is built with: addresses from the block, secrets from env.

    Every `*_env` key becomes its stem, read from the environment. Each variable is registered with
    the log-redaction inventory *before* it is read, so a driver that echoes its own configuration
    into a traceback cannot put a credential in a log.

    An empty variable is treated as absent: an unset secret and one set to the empty string are the
    same failure, and letting the second through would reach the database as an anonymous login.
    """
    options: dict[str, Any] = {}
    for key, value in connection.items():
        if key == "driver":
            continue
        if not key.endswith(ENV_SUFFIX):
            options[key] = value
            continue
        variable = str(value or "")
        if not variable:
            continue
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

    Returns the driver's own object rather than a narrowed type: what each seam needs of it — a
    `Warehouse`, a `ResultSink`, a `VectorStore` — is that seam's contract to check. This function
    owns the resolution and the credentials, nothing else.
    """
    reference = str(connection.get("driver") or "")
    if not reference:
        raise error(f"a {what} block must name a `driver:`")
    driver = resolve_driver(reference, error=error, what=f"{what} driver")
    options = connect_options(connection, error=error, what=what)
    # **The same signature check the validators run, run again here.** `make datasource-validate`
    # sees only the manifests this repository ships; a deployment mounts its own directory, which no
    # CI run ever bound. Without this the mismatch surfaces as a bare `TypeError` from the
    # constructor — and `TypeError` is not in `durable/publish`'s non-retryable list, so a
    # permanently broken manifest would be *retried* by every job that touches it. The model this
    # block replaced failed such a key as a `ValidationError` (a `ValueError`), which was retried by
    # nothing; keeping that property is what makes "the driver's signature is the schema" a
    # like-for-like trade rather than a loosening.
    if mismatch := signature_mismatch(driver, connection):
        raise error(f"{what} driver {reference!r} {mismatch}")
    # Logged at the level a deployment reads to answer "which database did this pod attach to".
    # `_is_address` decides what may be named: a resolved secret sits in `options` under its stem
    # (`access_token`), so filtering has to be by key rather than by where the value came from.
    logger.info(
        "opening %s via %s (%s)",
        what,
        reference,
        ", ".join(f"{key}={value}" for key, value in sorted(options.items()) if _is_address(key)),
    )
    return driver(**options)


def _is_address(key: str) -> bool:
    """Whether a resolved option is safe to name in a log line.

    An allow-list of the words this tree's own drivers use for *where* a database is, rather than a
    deny-list of credential words: a driver naming its secret keyword something unexpected would
    slip through a deny-list, and the log line is a convenience rather than a record.
    """
    return key in {"host", "port", "database", "catalog", "schema", "server_hostname", "url"}
