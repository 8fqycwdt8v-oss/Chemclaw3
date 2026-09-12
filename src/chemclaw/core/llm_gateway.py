"""The boot guard on the model gateway's address, for every process this deployment starts.

**"Every process" is the five `deploy/entrypoint.sh` dispatches plus the terminal CLI, and the
partition is checked rather than claimed** (`tests/test_llm_gateway_guard.py`): `service`,
`background-worker` and `mcp-face` call this in their entrypoints, `connector-*` and
`connector-worker-*` reach no model at all, and the three hook Jobs are DDL, GRANTs, a stored-row
rewrite and Temporal Schedules. Two *operator* CLIs also build a model outside that set and are
named there rather than left implied — `cli/verifier_margin` now calls this (its own docstring
already refused to "measure a mock"), and `retrieval/vector_index` deliberately does not, because
`make reindex` is a documented local target against the local embedding endpoint and the index it
writes is regenerable by definition. This sentence used to read "for every process that makes a
model call", which was wider than anything asserted it.

**Why it is here and not in `api/middleware.py`, where it was written.** The guard's subject is
*configuration* — where this deployment's one OpenAI-compatible gateway lives
(`D-2026-09-04-a-gateway-is-the-only-provider`) — and a front door is not the only process that
dials it. `durable/template_activities.run_agent_step` builds a graph inside a Temporal activity,
so the background worker takes turns; `api/mcp_face` advertises `condense_protocols`, which builds
a chat model of its own. Driven 2026-09-12 against the live broker with this call deleted again,
nothing configured: the worker logged `background worker connected: address=localhost:7233 …
queue=background-jobs` with `run_agent_step` among its registered activities, while
`settings.llm_base_url` held the mock's loopback address — and the row that opened this had already
had the second half, a model reply and a loopback recorder logging `/v1/chat/completions` in that
same guard-free process. Living in `api/` is what made this unreachable, so it lives in the kernel
every entrypoint already imports, and each entrypoint calls it.

**What it does not guard.** `_refuse_unauthenticated_exposure` stayed behind, deliberately: its
signal is `service_host` being non-loopback, which is a property of a *bind*, and what "exposed"
means for a process that only makes outbound calls is an open design question rather than a move
(`docs/planning/BACKLOG.md`). Reading that field from a worker is exactly the confusion this
module exists to end, which is why nothing below reads it.
"""

import logging
from urllib.parse import urlsplit

from chemclaw.core.config import settings
from chemclaw.core.http import is_loopback_url, parse_host

logger = logging.getLogger(__name__)


def _gateway_cannot_leave_this_pod() -> bool:
    """Whether `llm_base_url` names an address whose traffic never reaches the network.

    `is_loopback_url` answers most of it. The one address it deliberately does **not** answer is the
    unspecified one, and that is right for the two callers that read a *bind* — as a bind it is
    every interface, which is the whole subject of SEC-2 — and wrong here, because `llm_base_url` is
    a
    **destination**. Measured against a real listener on loopback: a gateway URL whose host is the
    unspecified address booted past this guard and its socket's peer came back as the loopback
    address, so every prompt went to whatever answered inside the pod. The normalisation is here
    rather than in `core.http` for exactly that reason: widening the shared predicate would waive
    the front door's unauthenticated-bind refusal for the address it exists to catch. (The address
    is described rather than written as a URL: `tests/test_no_egress.py` scans this file's *text*
    for host literals and cannot tell a measurement in a docstring from a default in code, which is
    that guard working — `core/http.py` carries the same note for the same reason.)
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

    `llm_base_url` ships with a value — the local mock `chemclaw.cli.mock_llm` serves — so a fresh
    checkout needs no credential. That default is safe by construction (a loopback address cannot
    leave the pod) and it is still a default: a deployment that forgot to override it meets it as a
    refused connection on a chemist's first question, or worse, inside a durable activity's retry
    loop where nobody is watching. This says it at boot instead.

    **The exemption is stated, not inferred, and that is the change from the shape this guard had
    in `api/middleware.py`.** It used to return early when `service_host` named a loopback
    interface — a bind, which is a fact about the front door and a field a worker has no business
    reading. So the dev posture is now said out loud by the one lane that means it
    (`CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY`, exported by `make chat`, by `infra/live/processes.sh`
    for the whole live lane, and by the suite's own autouse fixture — not by `.env.example`, which
    ships the code defaults and therefore ships this `false`), and every process kind asks the same
    question with no reference to how, or whether, it binds a socket.

    Two consequences of that swap, both real:

    - It is **stricter** for the front door. A loopback bind used to skip the check entirely, so a
      developer's exposed-gateway typo was invisible; now the gateway is checked in every posture.
    - It is **narrower** in one case it was wrong about. A gateway sidecar on loopback in the same
      pod is an ordinary deployment, and the old predicate refused it as though the mock were the
      only thing that could answer there. Such a deployment sets the flag and says so.

    An *empty* `llm_base_url` is not checked here and the omission is deliberate: `Settings`'
    `_gateway_is_addressed` validator already refuses it unconditionally, in every process, before
    this function could run. A second check would be dead code claiming a control.

    **What counts as "loopback" is `_gateway_cannot_leave_this_pod`, not `is_loopback_url` alone**,
    because a destination and a bind disagree about exactly one address. See that function.

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
