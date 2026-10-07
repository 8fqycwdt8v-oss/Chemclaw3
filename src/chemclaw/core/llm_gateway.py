"""The boot guard on the model gateway's address, for every process this deployment starts.

Every process that can dial the gateway calls it at boot: `service`, `background-worker` and
`mcp-face` entrypoints and the terminal CLI (`tests/test_llm_gateway_guard.py` checks the
partition). It lives in the kernel rather than `api/` because its subject is configuration, and
the background worker and MCP face build models too. `make reindex` deliberately does not call it.

It does not guard a bind: the front door's unauthenticated-exposure check reads `service_host`, and
nothing here does. A worker states its unauthenticated posture separately
(`durable/serve.refuse_unauthenticated_worker`).
"""

import logging
from urllib.parse import urlsplit

from chemclaw.core.config import settings
from chemclaw.core.http import is_loopback_url, parse_host

logger = logging.getLogger(__name__)


def _gateway_cannot_leave_this_pod() -> bool:
    """Whether `llm_base_url` names an address whose traffic never reaches the network.

    `is_loopback_url`, plus the unspecified address: as a destination it connects to the local host,
    while the shared predicate must keep treating it as non-loopback for binds. Normalised here so
    the
    front door's unauthenticated-bind refusal is not weakened. (Addresses are described rather than
    written as URLs because `tests/test_no_egress.py` scans this file's text for host literals.)
    """
    if is_loopback_url(settings.llm_base_url):
        return True
    try:
        host = urlsplit(settings.llm_base_url).hostname
    except ValueError:
        return False
    address = parse_host(host)
    return address is not None and address.is_unspecified


def refuse_unconfigured_llm_gateway() -> None:
    """Fail closed when a process that makes model calls still points at a loopback gateway.

    `llm_base_url` defaults to the local mock (`chemclaw.cli.mock_llm`) so a fresh checkout needs no
    credential; a deployment that forgot to override it should fail at boot, not on a chemist's
    first
    question or inside a retry loop. The dev posture is stated explicitly with
    `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` (set by `make chat`, the live lane and the test suite,
    never
    by `.env.example`), so every process kind asks the same question regardless of how it binds. A
    same-pod gateway sidecar on loopback sets that flag too. An empty URL is already refused by the
    `Settings` validator. "Loopback" here is `_gateway_cannot_leave_this_pod`.

    Raises:
        RuntimeError: naming the address, why it cannot be right, and the two edits that proceed.
    """
    if not _gateway_cannot_leave_this_pod():
        return
    if not settings.llm_allow_loopback_gateway:
        raise RuntimeError(
            "SECURITY: this process makes model calls while CHEMCLAW_LLM_BASE_URL names a loopback "
            f"address ({settings.llm_base_url!r}) — the local dev mock is what ships there, and "
            "every turn would fail on a refused connection. Set CHEMCLAW_LLM_BASE_URL to the model "
            "gateway this deployment should use, or set "
            "CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true to state that a loopback gateway (the dev "
            "mock, or a sidecar in this pod) is what you mean."
        )
    logger.warning(
        "the model gateway is a loopback address (%r) and CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY is "
        "set — every model call this process makes goes to something on this host. That is right "
        "for local dev against `chemclaw.cli.mock_llm` and for a gateway sidecar, and wrong for "
        "anything else.",
        settings.llm_base_url,
    )
