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

# A relation, column or schema name, optionally qualified (`eln_prod.reactions.v_reaction`). A
# connection block contributes exactly two kinds of thing to whatever it reaches — a bound
# parameter, or an identifier of this shape — and this is the check for the second kind. `$` is
# legal inside a warehouse identifier and appears in generated views; it is not legal first.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*$")


def check_env_name(key: str, value: str, *, error: type[Exception]) -> None:
    """Raise unless `value` looks like the name of an environment variable.

    Called both when a manifest loads (so a typo fails at startup) and again when the connection is
    opened (so a block that reached the resolver by another route is checked once regardless).

    **Blank is refused, not skipped**, and that is the half this check was missing. `token_env:`
    with nothing after it is YAML `None` and `token_env: ""` is the empty string; every caller
    normalises both to `""`, and while `""` was accepted the key stopped naming anything at all —
    `connect_options` dropped the keyword and handed the driver a block with no credential in it.
    Which failure that becomes is decided by the driver rather than by the manifest: a client whose
    credential has a default (`api_key: str = ""`) attaches **anonymously**, and one whose
    credential is required raises a bare `TypeError`, which `durable/publish` does not list as
    non-retryable, so a permanently broken manifest is retried by every job that touches it. The
    key is present because its author meant to supply a credential, so an empty one is a
    misconfiguration to report, never an omission to infer.
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

    Here beside `check_env_name` because this module owns *what a connection block may contribute*,
    and it owned only the credential half of that rule until a `schema:` proved the other half was
    missing: `publish/drivers/postgres.py` interpolated it into libpq's `options`, libpq splits that
    on whitespace, and the last `-c` wins — so `schema: "public -c statement_timeout=0"` disabled
    the statement timeout whose range the same constructor had checked three lines earlier. A
    binding's identifiers were already checked by exactly this pattern; the one field that reached a
    *process argument* rather than a statement was not, and a second spelling of "is this an
    identifier" is how the two would have drifted.

    `error` is a parameter for the reason the rest of this module's are: Temporal matches
    `non_retryable_error_types` by class name, so a refusal has to arrive under the name the calling
    seam's activity lists.
    """
    # `fullmatch` rather than `match`: with a trailing `$` anchor, `match` also accepts one
    # trailing newline, so a function name ending in one passed and reached the statement text.
    # Nothing could follow that newline — the rest of the value would have to match as well — so
    # this was hygiene rather than a hole. But a checker whose whole job is "the value is exactly
    # this shape" should not rest on which of two anchor semantics it happened to get.
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
    An empty *name* is the same failure one level up and raises for the same reason — see
    `check_env_name`, which owns that rule so the manifest validators refuse it at load too.
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
