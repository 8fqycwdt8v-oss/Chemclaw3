"""Validate the result-sink manifests — `make sink-validate`.

Four checks pydantic cannot make from a manifest alone, each guarding a declaration against the
live surface:

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

Deliberately does *not* connect to anything. A sink's reachability is a deployment fact and belongs
to `/readyz`-style probing, not to a manifest check that CI runs with no results database in sight.
"""

import argparse
import inspect
import logging
import sys
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging
from chemclaw.publish.connect import _resolve as _resolve_connection_driver
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
        options = {
            key[:-4] if key.endswith("_env") else key: "" for key in connection if key != "driver"
        }
        try:
            inspect.signature(nested).bind(**options)
        except TypeError as exc:
            problems.append(
                f"{manifest.name}: connection driver {reference!r} does not accept its block "
                f"({sorted(options)}): {exc}"
            )
    return problems


def problems() -> list[str]:
    """Every finding across every discovered sink."""
    manifests = discovered()
    found = _enabled_problems(manifests)
    for name in settings.result_sink_list:
        if name in manifests:
            found.extend(_driver_problems(manifests[name]))
    return found


def main(argv: list[str] | None = None) -> int:
    """Report every problem, or confirm the manifests are sound."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_sinks", description=__doc__
    )
    parser.parse_args(argv)
    configure_logging()

    found = problems()
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
