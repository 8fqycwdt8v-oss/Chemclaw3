"""The read-only MCP face: this system, reachable as a tool by somebody else's agent.

Serves the registered in-process tools that `agent.authz.READ_ONLY_TOOLS` classifies as read-only,
minus `WITHHELD` — derived, not listed, so a tool's classification decides and
`tests/test_mcp_face.py` checks both directions. Nothing here can launch a job, write a note or a
preference, or settle a wait, so a caller holds strictly less authority than one using the front
door.

The same functions the agent binds, over `connectors/server.py`'s transport (bearer auth, per-call
caller re-binding, error sanitising, per-tool metrics). The only authorization is holding the
token, which is why it may only serve reads; identity headers are logged, never trusted.
"""

import logging

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# Seeds the capability-tool registry, which is populated by import side effect; without it the face
# advertises nothing.
from chemclaw.agent import tool_modules as _tool_modules  # noqa: F401
from chemclaw.agent.authz import READ_ONLY_TOOLS
from chemclaw.connectors.server import connector_app
from chemclaw.core.asgi import transport_bounds
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tools

logger = logging.getLogger(__name__)

#: The name this surface reports as, in its health payload and its metric labels.
FACE_NAME = "chemclaw-read"

# Read-only tools that are not advertised here, each with the reason.
#
# Read-only is not sufficient. The face exports what the programme knows about its chemistry, never
# what it knows about its people — who is doing what and what it cost — and nothing scoped to a turn
# an external caller does not have. A deny-list because nothing classifies that property; it is kept
# honest as a partition: `tests/test_mcp_face.py` asserts every read-only tool is either advertised
# or named here.
WITHHELD: dict[str, str] = {
    # Scoped to a turn this caller does not have.
    "ask_clarifying_question": "puts a question to the chemist in the conversation; there is none",
    # Artefacts are resolved against the turn's session, which an external caller does not have.
    "create_exhibit": "writes an artefact into a conversation's pane; there is no conversation",
    "revise_exhibit": "revises an artefact in a conversation's pane; there is no conversation",
    "read_exhibit": "reads an artefact of the caller's session, which an external caller lacks",
    "list_attachments": "files uploaded to a session, which an external caller does not have",
    "read_attachment": (
        "the contents of a file somebody uploaded to a conversation — a disclosure surface rather "
        "than a capability, and the reason this list exists rather than a comment"
    ),
    "list_watches": "one person's standing queries, addressed by the turn's own actor",
    "recall_preferences": "how one chemist likes to work, addressed by the turn's own actor",
    # About this deployment's people rather than its chemistry.
    "assemble_evidence_pack": (
        "one conversation's whole record, and the gate on it is session ownership — there is no "
        "actor here to own anything, so the caller could only ever name somebody else's session"
    ),
    "find_experiment_protocols": (
        "every experiment design open in the deployment, with the project it belongs to and the "
        "named employee who opened it — the same enumeration `check_pending_requests` is withheld "
        "for, and the discovery path for the ids `read_experiment_protocol` takes"
    ),
    "experiment_arms_from_campaign": (
        "the same disclosure as `read_experiment_protocol` one door over: a campaign's objective, "
        "the parameter space a team is exploring and the exact conditions they are about to run. "
        "A `campaign_id` is a hash of the decision space, so it is guessable by anyone who knows "
        "the space — which is the guessability argument that withholds the two entries below"
    ),
    "read_experiment_protocol": (
        "one chemist's in-flight design: the goal they typed, their `prior_work` and `notes`, and "
        "what they ruled out. A `design-<hash>` id is derived from the ask, which is the same "
        "guessability argument that withholds `get_durable_job_status`"
    ),
    "rescale_experiment_protocol": (
        "the same document `read_experiment_protocol` above is withheld for, reached by the same "
        "guessable `design-<hash>` id — it returns the whole design, not only its charge table, so "
        "the chemist's goal, `prior_work` and `notes` ride out with the scaled numbers. Being a "
        "read that stores nothing is what makes it read-only to the plan gate; it is not an "
        "argument for exporting it to an unauthenticated face"
    ),
    "read_plate_results": (
        "one team's plate and what it gave: the conditions they ran and the numbers they got, "
        "reached by the same guessable `design-<hash>` id `read_experiment_protocol` above is "
        "withheld for. An unreported campaign's results are the most commercially sensitive "
        "thing this tier holds"
    ),
    "check_pending_requests": (
        "every open request in the deployment with the reasoning a chemist typed, who asked and "
        "which session it belongs to — also the discovery path for the session ids above"
    ),
    "review_activity": (
        "per-actor turns, tokens and refusals: a named employee's usage, which `leaver.py` "
        "classifies as a retained personal identifier"
    ),
    "review_commitments": (
        "what the programme has committed to, who owns it and when it is due — the portfolio, not "
        "the chemistry"
    ),
    "find_past_jobs": "runs from other people's conversations, each with its free-text rationale",
    "get_durable_job_status": (
        "the same disclosure as `find_past_jobs` through the other door — it applies no actor "
        "check, returns the run's summary, result and free-text rationale, and its job ids are a "
        "pure function of connector, job name and payload, so they are guessable rather than secret"
    ),
}


def advertised_tools() -> list[str]:
    """The names this face serves: read-only, and not scoped to a turn this caller does not have.

    Read-only is asked of `agent.authz`, the single statement of whether a tool writes; the rest is
    `WITHHELD`.
    """
    return sorted(
        name
        for fn in registered_tools()
        if (name := getattr(fn, "__name__", "")) in READ_ONLY_TOOLS and name not in WITHHELD
    )


def build_face() -> FastMCP:
    """A `FastMCP` serving exactly the read-only in-process tools.

    Functions are registered unchanged, so an external caller sees the same tool, caveats included.
    """
    server = FastMCP(FACE_NAME)
    allowed = set(advertised_tools())
    for fn in registered_tools():
        if getattr(fn, "__name__", "") in allowed:
            server.tool()(fn)
    return server


def face_token_env() -> str:
    """The environment variable the face's bearer token is read from.

    Not in a manifest: the face is not a connector, and `CHEMCLAW_CONNECTOR_URLS` must never name
    it,
    or the deployment would dial itself.
    """
    return settings.mcp_face_token_env


def create_face_app() -> FastAPI:
    """The FastAPI app for the read-only MCP face, on the transport connectors already use.

    `connector_app` already handles serving MCP correctly (session manager lifespan, route order,
    per-call caller binding, error sanitising, log configuration); a second transport would not.
    """
    logger.info(
        "mcp_face.serving: %d read-only tool(s): %s",
        len(advertised_tools()),
        ", ".join(advertised_tools()),
    )
    return connector_app(build_face(), name=FACE_NAME, token_env=face_token_env())


def main() -> None:
    """Configure this process, then serve the read-only face.

    Its own process role so startup configures redaction, correlation ids and metrics (see
    `connectors/server_entry.py`). The app target is a string so it is built after logging is
    configured.
    """
    import uvicorn

    from chemclaw.core.llm_gateway import refuse_unconfigured_llm_gateway
    from chemclaw.core.logging import configure_logging, configure_telemetry

    configure_logging()
    configure_telemetry()
    # This process makes model calls (`condense_protocols` builds its own chat model), so the
    # gateway
    # guard applies.
    refuse_unconfigured_llm_gateway()
    logger.info("mcp face starting on %s:%s", settings.service_host, settings.service_port)
    uvicorn.run(
        "chemclaw.api.mcp_face:create_face_app",
        factory=True,
        host=settings.service_host,
        port=settings.service_port,
        # Ours is already applied above; letting uvicorn install its own would replace it.
        log_config=None,
        # The three bounds D-2026-08-01 established. This face serves the same kind of traffic the
        # front door does and ran without them until 2026-09-11 — see `core/asgi.transport_bounds`.
        **transport_bounds(),
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
