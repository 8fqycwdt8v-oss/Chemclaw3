"""Validate the result-sink manifests — `make sink-validate`.

Three checks pydantic cannot make from a manifest alone:

1. an **enabled** sink that no manifest declares — a deployment believing it publishes and not
   doing so;
2. a **driver** that cannot be imported or is not callable;
3. a **config block** the driver's signature will not accept (the callable is the schema).

Rules 2 and 3 run over every *discovered* manifest: a sink broken while disabled is one nobody can
enable. The property registry is checked by `tests/test_publish_registry.py`, not here. Connects to
nothing: reachability is a deployment fact.
"""

import argparse
import inspect
import logging
import sys
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.connect import ENV_SUFFIX, check_env_name, signature_mismatch
from chemclaw.core.logging import configure_logging
from chemclaw.publish.connect import SinkConnectionError
from chemclaw.publish.connect import resolve_driver as _resolve_connection_driver
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
        # A `*_env` key holds the NAME of an environment variable. This seam has no model to
        # validate it, so the gate catches a pasted value or a lower-case name before a publish
        # fails on it.
        for key, value in connection.items():
            if not key.endswith(ENV_SUFFIX):
                continue
            try:
                check_env_name(key, str(value or ""), error=SinkConnectionError)
            except SinkConnectionError as exc:
                problems.append(f"{manifest.name}: connection: {exc}")
    return problems


def problems() -> list[str]:
    """Every finding across every discovered sink, plus rule 1 over the enabled set.

    Discovery, not enablement: `CHEMCLAW_RESULT_SINKS` is empty in CI, so iterating it would check
    no driver at all and let a broken sink surface only on the first deployment that enables it.
    Rule 1 is a property of the enabled set, so it is computed separately. Zero discovered manifests
    is itself a finding.
    """
    try:
        manifests = discovered()
    except ResultSinkError as exc:
        # A malformed or mis-named manifest stops discovery; report it as one problem line naming
        # the file, not a traceback.
        return [str(exc)]

    if not manifests:
        # Zero discovered manifests (a typo in the `PATH`-style `CHEMCLAW_RESULT_SINKS_DIR`, or an
        # image missing `data/publish/sinks/`) would turn all three rules off behind a green line.
        return [
            f"no result sinks discovered under {settings.result_sinks_dir!r} — no driver, no "
            "config block and no `*_env` name would be checked, and this gate would have checked "
            "nothing"
        ]
    found = _enabled_problems(manifests)
    for manifest in manifests.values():
        found.extend(_driver_problems(manifest))
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
