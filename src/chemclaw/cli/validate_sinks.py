"""Validate the result-sink manifests — `make sink-validate`.

Three checks pydantic cannot make from a manifest alone, each guarding a declaration against the
live surface. Rule 1 is a property of the enabled set; rules 2 and 3 run over every **discovered**
manifest, because a sink that is broken while disabled is a sink nobody can enable:

1. an **enabled** sink that no manifest declares — a deployment believing it publishes and not
   doing so is indistinguishable from one with nothing to publish, which is the whole failure this
   subsystem exists to end;
2. a **driver** that cannot be imported or is not callable;
3. a **config block** the driver's signature will not accept — the same "the callable is the
   schema" rule the data-source seam applies, checked by binding rather than by a second model.

The property registry is deliberately **not** checked here: pydantic already refuses a definition
with no prose, and `tests/test_publish_registry.py` holds the checks that need more than a manifest
— that units convert within a dimension, and that no two properties of one dimension land on the
same subject. A third copy here would be a check nobody maintains.

Deliberately does *not* connect to anything — `make sink-validate` is exactly the three rules
above. A sink's reachability is a deployment fact and belongs to `/readyz`-style probing, not to a
manifest check that CI runs with no results database in sight.

**`--preflight` is the separate, opt-in thing that does connect**, and it is a different question
from the three rules — which is why it is a flag rather than a widening of what `sink-validate`
means. What it answers is the one class of drift nothing anywhere detects: the site's schema and
this release's property registry can disagree, and today the first thing that notices is a *write*.
A missing column is dropped with a WARNING (correct), an unexpected NOT NULL dead-letters, a
missing table dead-letters the whole corpus — and worst of the four, `property_value.property`
REFERENCES `property_definition`, whose rows are seeded by a **separate manual command**
(`cli/sink_schema.py --seed`). `publish/dialect.definition_for` checks only the *local* registry,
so a release that adds a property publishes rows the site's foreign key rejects until a DBA re-runs
the seed, and there is no signal at all until the drain dead-letters them. Run this after a
deployment and after applying DDL:

    python -m chemclaw.cli.validate_sinks --preflight
"""

import argparse
import asyncio
import inspect
import logging
import sys
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.connect import (
    ENV_SUFFIX,
    check_env_name,
    check_no_inline_credential,
    check_no_inline_dsn_password,
    signature_mismatch,
)
from chemclaw.core.logging import configure_logging
from chemclaw.publish.connect import SinkConnectionError
from chemclaw.publish.connect import resolve_driver as _resolve_connection_driver
from chemclaw.publish.driver import SinkRejectedError
from chemclaw.publish.drivers.sql import SqlResultSink
from chemclaw.publish.manifest import ResultSinkManifest
from chemclaw.publish.properties import REGISTRY
from chemclaw.publish.registry import ResultSinkError, _resolve, discovered

logger = logging.getLogger(__name__)


def _enabled_problems(manifests: dict[str, ResultSinkManifest]) -> list[str]:
    """An enabled name with no manifest (rule 1)."""
    return [
        f"CHEMCLAW_RESULT_SINKS names {name!r}, which no manifest declares "
        f"(discovered: {sorted(manifests) or 'none'})"
        for name in settings.result_sink_list
        if name not in manifests
    ]


def _driver_problems(manifest: ResultSinkManifest) -> list[str]:
    """A driver that will not resolve, or will not take its config (rules 2 and 3)."""
    try:
        driver = _resolve(manifest.driver)
    except ResultSinkError as exc:
        return [f"{manifest.name}: {exc}"]

    problems: list[str] = []
    supplied = {"name": manifest.name, "tenant_id": manifest.tenant_id or manifest.name}
    supplied.update(manifest.config)
    try:
        # Bound rather than called: constructing would open a connection, and this check must run
        # in CI against no database at all.
        inspect.signature(driver).bind(**supplied)
    except TypeError as exc:
        problems.append(
            f"{manifest.name}: driver {manifest.driver!r} does not accept its config "
            f"({sorted(manifest.config)}): {exc}"
        )

    # A nested `connection:` block names a driver of its own, and gets the same two checks — it is
    # the half a deployment is most likely to get wrong, because it is where a vendor client lives.
    connection: dict[str, Any] = manifest.config.get("connection") or {}
    if reference := str(connection.get("driver") or ""):
        try:
            nested = _resolve_connection_driver(reference)
        except Exception as exc:
            return [*problems, f"{manifest.name}: connection driver {reference!r}: {exc}"]
        if mismatch := signature_mismatch(nested, connection):
            problems.append(f"{manifest.name}: connection driver {reference!r} {mismatch}")
        # A `*_env` key holds the NAME of an environment variable. The inbound seam checks this when
        # its binding loads; this seam has no model to hang a validator on, so the gate is the only
        # place it can be caught before a publish attempt fails on a variable that was never a
        # variable name — the realistic mistake being a pasted value, or a lower-case one.
        for key, value in connection.items():
            try:
                if key.endswith(ENV_SUFFIX):
                    check_env_name(key, str(value or ""), error=SinkConnectionError)
                else:
                    # The other half of the same rule, and this gate could not see it: a key that
                    # does *not* end in `_env` was passed to the driver verbatim, so `password:
                    # hunter2` or a `dsn:` with an inline password validated clean, went
                    # unregistered for log redaction, and sat in a repository.
                    check_no_inline_credential(key, value, error=SinkConnectionError)
                    check_no_inline_dsn_password(key, value, error=SinkConnectionError)
            except SinkConnectionError as exc:
                problems.append(f"{manifest.name}: connection: {exc}")
    return problems


async def _preflight_problems(manifest: ResultSinkManifest) -> list[str]:
    """Connect to one sink and compare what it holds against what this release will write.

    Three questions, in the order a site fails them: is the schema there, does every table this
    writer names exist, and does the site's `property_definition` hold every property this release
    can publish. The third is the one with no other detector — see the module docstring — and it is
    a foreign key, so a gap is a dead-lettered row rather than a dropped column.

    Read-only: three `SELECT`s and no write. A sink that cannot be reached is reported as such
    rather than raised, because a preflight over several sinks must report all of them.
    """
    from chemclaw.publish.registry import build

    try:
        sink = build(manifest)
    except ResultSinkError as exc:
        return [f"{manifest.name}: {exc}"]
    if not isinstance(sink, SqlResultSink):
        # An HTTP sink has no schema to compare against: what it accepts is the receiver's
        # business, and this repository has nothing to check it with. Named rather than silently
        # passed, so "preflight found nothing" cannot be read as "the endpoint is ready".
        await sink.aclose()
        return [f"{manifest.name}: not a SQL sink; there is no schema here to preflight"]

    problems: list[str] = []
    try:
        warehouse = sink._connect()
        try:
            columns = await sink._known_columns(warehouse)
        except SinkRejectedError as exc:
            return [f"{manifest.name}: {exc}"]
        # Only the tables the sink itself probes: the shipped DDL also creates the registry and
        # solvent dimensions, which this writer reads through and never writes.
        missing_columns = {
            table: sorted(set(expected) - columns[table])
            for table, expected in _writable_columns().items()
            if table in columns and set(expected) - columns[table]
        }
        for table, absent in sorted(missing_columns.items()):
            problems.append(
                f"{manifest.name}: {table} lacks {', '.join(absent)}; those values will be "
                "dropped with a warning on every publish until the site applies the DDL"
            )
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT property FROM property_definition", [])
            rows = await cursor.fetchall()
        seeded = {str(row.get("property") or row.get("PROPERTY") or "").lower() for row in rows}
        unseeded = sorted(name for name in REGISTRY if name.lower() not in seeded)
        if unseeded:
            problems.append(
                f"{manifest.name}: property_definition is missing {len(unseeded)} of the "
                f"{len(REGISTRY)} properties this release publishes ({', '.join(unseeded[:8])}"
                f"{', …' if len(unseeded) > 8 else ''}). `property_value.property` references that "
                "table, so every fact naming one of them will be refused and dead-lettered. Run "
                "`python -m chemclaw.cli.sink_schema --seed` and apply it."
            )
    except (ConnectionError, OSError, SinkConnectionError) as exc:
        problems.append(f"{manifest.name}: could not be reached: {exc}")
    finally:
        await sink.aclose()
    return problems


def _writable_columns() -> dict[str, list[str]]:
    """Every column this release writes, per table, taken from the shipped DDL.

    Derived rather than listed: the DDL in `schema/result-store/` is the one statement of what a
    site's schema should be, and a second list here would be the drift this command exists to find.
    """
    from chemclaw.cli.sink_schema import ddl

    tables: dict[str, list[str]] = {}
    current = ""
    for line in ddl().splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("CREATE TABLE"):
            current = stripped.split()[-2] if stripped.endswith("(") else ""
            tables.setdefault(current, [])
            continue
        if not current or stripped.startswith("--"):
            continue
        if stripped.startswith(")"):
            current = ""
            continue
        word = stripped.split()[0] if stripped else ""
        if word and word.isidentifier() and word.upper() not in _NOT_A_COLUMN:
            tables[current].append(word)
    return {table: columns for table, columns in tables.items() if columns}


#: Words that open a table-level clause rather than a column definition.
_NOT_A_COLUMN = frozenset({"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"})


def problems() -> list[str]:
    """Every finding across every **discovered** sink, plus rule 1 over the enabled set.

    Discovery, not enablement — the convention both sibling seams already state and this one did
    not follow. `CHEMCLAW_RESULT_SINKS` is empty by default and empty in CI, and iterating it meant
    rules 2 and 3 resolved zero drivers, bound zero config blocks and checked zero `*_env` names on
    the shipped configuration: a gate that could only fail on rule 1, which by construction was
    empty too. A rename in `publish/drivers/sql.py` would have stayed green through every release
    and failed on the first deployment to enable publishing — in a worker, against a database a DBA
    had already provisioned. `validate_connectors` and `validate_datasources` both give the reason
    in the same words: a sink that is broken while disabled is a sink nobody can enable, and CI is
    where that should surface rather than the day an operator turns it on.

    Rule 1 stays a property of the enabled *set* rather than of any one manifest, which is why it
    is computed separately and not folded into the loop.
    """
    manifests = discovered()
    found = _enabled_problems(manifests)
    for manifest in manifests.values():
        found.extend(_driver_problems(manifest))
    return found


def main(argv: list[str] | None = None) -> int:
    """Report every problem, or confirm the manifests are sound."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_sinks", description=__doc__
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "also connect to every ENABLED sink and compare its schema and property registry "
            "against what this release writes (not part of `make sink-validate`)"
        ),
    )
    args = parser.parse_args(argv)
    configure_logging()

    found = problems()
    if args.preflight:
        manifests = discovered()
        for name in settings.result_sink_list:
            if name in manifests:
                found.extend(asyncio.run(_preflight_problems(manifests[name])))
    for problem in found:
        sys.stderr.write(f"result sink: {problem}\n")
    if found:
        return 1
    logger.info(
        "result sinks: %d discovered, %d enabled, %d properties registered",
        len(discovered()),
        len(settings.result_sink_list),
        len(REGISTRY),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
