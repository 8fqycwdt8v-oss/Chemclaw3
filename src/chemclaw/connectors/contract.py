"""The version handshake between a connector's manifest and the server it describes.

The manifest (from the fleet's `chemclaw-contracts` package) declares the `contract_version` of the
tool surface it advertises; the server reports the version it was built against on `/healthz`. A
different MAJOR means the advertised arguments and the served ones no longer agree, so the connector
is refused by name for the turn (the caller reports it as `capability_degraded`); a different MINOR
is additive and logged; a value missing on either side is unknown and never refuses.

The server's answer is remembered per connector for `connector_breaker_window_seconds`, because
the fleet's `/healthz` runs a readiness check and a probe on every turn would add that to every
open. The MCP handshake cannot carry it: `serverInfo.version` is the build's revision.

Invariants: nothing here raises except `ContractMismatch`; an unanswered or unreadable `/healthz` is
"unknown" and never replaces a version already learned (a flaky probe cannot flip a verdict); a
refusal is not a reachability verdict (the server answered); a finding is logged again only when
its value changes, and the memory is bounded by connectors, not by values.
"""

import asyncio
import logging
import re
import time
from typing import Literal

import httpx

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.http import default_ssl_context

logger = logging.getLogger(__name__)

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.\d+$")

Agreement = Literal["same", "minor", "major", "unknown"]

#: The last value logged per `(connector, kind)`, so a check made on every open speaks once per
#: change. Bounded by connectors times kinds.
_LOGGED: dict[tuple[str, str], str] = {}

#: What each connector's server last said, and when: `(url, monotonic seconds, version or None)`.
_SERVED: dict[str, tuple[str, float, str | None]] = {}


def _now() -> float:
    """Monotonic seconds, a function so a test can move the clock."""
    return time.monotonic()


class ContractMismatch(ChemclawError):
    """A connector's server was built against a different MAJOR of its contract."""


def compare(declared: str | None, served: str | None) -> Agreement:
    """How the manifest's version relates to the server's: same, minor, major or unknown.

    `unknown` is any value that is absent or not `MAJOR.MINOR.PATCH`. A patch difference is `same`.
    """
    if declared is None or served is None:
        return "unknown"
    left, right = _SEMVER.match(declared), _SEMVER.match(served)
    if left is None or right is None:
        return "unknown"
    if left.group(1) != right.group(1):
        return "major"
    return "minor" if left.group(2) != right.group(2) else "same"


async def served_version(url: str, budget: float) -> str | None:
    """The `contract_version` the server reports on `/healthz`, or `None` if it gave none.

    Any failure to ask or to read the answer is `None`: the open that follows decides reachability.
    The answer is read whatever the status, because a degraded server still names its build.
    """
    try:
        async with httpx.AsyncClient(
            timeout=budget, trust_env=False, verify=default_ssl_context()
        ) as client:
            response = await asyncio.wait_for(client.get(url), budget)
        body = response.json()
    except (httpx.HTTPError, TimeoutError, ValueError):
        return None
    reported = body.get("contract_version") if isinstance(body, dict) else None
    return reported if isinstance(reported, str) else None


def _once(connector: str, kind: str, value: str) -> bool:
    """Whether `value` is new for this `(connector, kind)` (and mark it seen)."""
    if _LOGGED.get((connector, kind)) == value:
        return False
    _LOGGED[(connector, kind)] = value
    return True


async def _served(connector: str, url: str) -> str | None:
    """The server's `contract_version`, from memory while it is fresh, else asked.

    A failed or silent probe keeps what was learned before and is itself remembered for the same
    window, so a dark `/healthz` is not asked again every turn and cannot switch the check off.
    """
    window = settings.connector_breaker_window_seconds
    remembered = _SERVED.get(connector)
    if remembered is not None and remembered[0] == url and _now() - remembered[1] < window:
        return remembered[2]
    asked = await served_version(url, settings.connector_health_timeout_seconds)
    if asked is None and remembered is not None and remembered[0] == url:
        asked = remembered[2]
    _SERVED[connector] = (url, _now(), asked)
    return asked


async def check_contract(connector: str, declared: str | None, health_url: str | None) -> None:
    """Compare the manifest's `contract_version` with the server's, refusing a major mismatch.

    Raises:
        ContractMismatch: the two differ in MAJOR; the message names the connector and both values.
    """
    if declared is None or health_url is None:
        if _once(connector, "manifest", str(declared)):
            logger.info(
                "connector %s: contract version unknown, its manifest declares %s; not checked",
                connector,
                "no contract_version" if declared is None else "no health route to ask",
            )
        return
    served = await _served(connector, health_url)
    agreement = compare(declared, served)
    if agreement == "unknown":
        if _once(connector, "served", f"{declared}:{served}"):
            logger.warning(
                "connector %s: contract version unknown, its manifest declares %s and its server "
                "reported %s on /healthz; not checked",
                connector,
                declared,
                served or "none",
            )
    elif agreement == "minor":
        if _once(connector, "minor", f"{declared}:{served}"):
            logger.warning(
                "connector %s: the manifest declares contract_version %s and the server reports "
                "%s; the surfaces differ additively, so the connector is used",
                connector,
                declared,
                served,
            )
    elif agreement == "major":
        raise ContractMismatch(
            f"connector {connector} is refused: its manifest declares contract_version {declared} "
            f"and its server reports {served}, a different MAJOR, so the arguments this process "
            "advertises are not the ones that server accepts. Align the installed "
            "`chemclaw-contracts` and the server image."
        )


def forget_contract_findings() -> None:
    """Drop what has been logged and what servers said, so a test starts from nothing."""
    _LOGGED.clear()
    _SERVED.clear()
