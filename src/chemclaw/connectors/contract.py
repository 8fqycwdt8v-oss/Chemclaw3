"""The version handshake between a connector's manifest and the server it describes.

The manifest (from the fleet's `chemclaw-contracts` package) declares the `contract_version` of the
tool surface it advertises; the server reports the version it was built against on `/healthz`. A
different MAJOR means the advertised arguments and the served ones no longer agree, so the connector
is refused by name for the turn (the caller reports it as `capability_degraded`); a different MINOR
is additive and logged; a value missing on either side is unknown and never refuses.

Invariants: nothing here raises except `ContractMismatch`; an unanswered or unreadable `/healthz` is
"unknown" (the MCP open is the arbiter of reachability); each distinct finding is logged once per
process, so a per-turn check does not repeat itself.
"""

import asyncio
import logging
import re
from typing import Literal

import httpx

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.http import default_ssl_context

logger = logging.getLogger(__name__)

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.\d+$")

Agreement = Literal["same", "minor", "major", "unknown"]

#: Findings already logged by this process, so a check made on every connector open speaks once.
_LOGGED: set[tuple[str, str]] = set()


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


def _once(connector: str, finding: str) -> bool:
    """Whether this finding about `connector` is new to the process (and mark it seen)."""
    key = (connector, finding)
    if key in _LOGGED:
        return False
    _LOGGED.add(key)
    return True


async def check_contract(connector: str, declared: str | None, health_url: str | None) -> None:
    """Compare the manifest's `contract_version` with the server's, refusing a major mismatch.

    Raises:
        ContractMismatch: the two differ in MAJOR; the message names the connector and both values.
    """
    if declared is None or health_url is None:
        if _once(connector, "manifest"):
            logger.info(
                "connector %s: contract version unknown, its manifest declares %s; not checked",
                connector,
                "no contract_version" if declared is None else "no health route to ask",
            )
        return
    served = await served_version(health_url, settings.connector_health_timeout_seconds)
    agreement = compare(declared, served)
    if agreement == "unknown":
        if _once(connector, f"served:{served}"):
            logger.warning(
                "connector %s: contract version unknown, its manifest declares %s and its server "
                "reported %s on /healthz; not checked",
                connector,
                declared,
                served or "none",
            )
    elif agreement == "minor":
        if _once(connector, f"minor:{declared}:{served}"):
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
    """Drop what has been logged, so a test sees a finding again. Nothing else is remembered."""
    _LOGGED.clear()
