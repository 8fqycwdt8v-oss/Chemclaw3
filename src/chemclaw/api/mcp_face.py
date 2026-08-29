"""The read-only MCP face: this system, reachable as a tool by somebody else's agent.

**ChemClaw3 is an MCP client and has never been an MCP server**, so the assistant in the chat
client, the one in the portfolio tool and the one a partner runs cannot ask the system that holds
this programme's chemistry anything. That is defensible for a chat product and costly for a
platform: the one durable advantage here over a general assistant is a governed, cited, auditable
record, and it is worth far more if it can be *reached* than if it can only be visited.

## Read-only first, and that is the whole trick

The advertised set is the intersection of the registered in-process tools with
`agent.authz.READ_ONLY_TOOLS` — **derived, never listed**. A hand-kept list would be an allow-list
that drifts, and drift in an allow-list of what may be exposed is invisible: a tool added to the
read-only set would silently join this surface, and a tool that stopped being read-only would
silently stay on it. Deriving it means the classification a merged tool already has is the one that
decides, and `tests/test_mcp_face.py` asserts the derivation in both directions.

Nothing here can launch a job, propose a note, write a preference or settle a wait. So this exports
the value with none of the blast radius an effector seam has, and a caller reaching it holds
strictly less authority than one talking to the front door.

## What it is not

Not a second agent, and not a second definition of any tool. It advertises the *same functions*
`build_langgraph_agent` advertises, over `connectors/server.py`'s transport — the one this
repository already runs, with its bearer auth, its caller re-binding per tool call, its error
sanitising and its per-tool metrics. Bearer auth is mandatory here as everywhere: the header trio a
caller sends is logged and never trusted, and this surface has no authorization of its own beyond
"holds the token", which is why it may only ever serve reads.
"""

import logging

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

# Seeds the capability-tool registry this module reads. **Load-bearing, not incidental**: the
# registry is populated by import side effect, and without this the face advertised nothing
# at all in production while every test passed — `agent/tool_modules.py` records what that
# cost and why the seeding is a module.
from chemclaw.agent import tool_modules as _tool_modules  # noqa: F401
from chemclaw.agent.authz import READ_ONLY_TOOLS
from chemclaw.connectors.server import connector_app
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tools

logger = logging.getLogger(__name__)

#: The name this surface reports as, in its health payload and its metric labels.
FACE_NAME = "chemclaw-read"

#: Read-only tools that are nonetheless **not** advertised here, each with the reason.
#:
#: **Read-only is necessary and nowhere near sufficient, and getting the second predicate wrong is
#: what this list is really about.** It was first written as "turn-scoped": an external caller has
#: no turn, so a tool reading the turn's own state would answer emptily and `read_attachment` would
#: answer worse. That reasoning is sound and it covers the wrong set. It says nothing about a tool
#: that is read-only *and* deployment-wide — and four of those were being advertised. A partner
#: agent given a token to look up melting points could enumerate every open lab request with the
#: reasoning a chemist typed into it, the whole mirrored portfolio with owners and due dates, every
#: named employee's token spend, and other people's job rationales.
#:
#: So the predicate is not "does this need a turn". It is **"is this about this deployment's people
#: or about its chemistry"** — the face exports the second and none of the first. A tool that
#: answers "what does the programme know" belongs here; one that answers "who is doing what, and
#: what did it cost" does not, however read-only it is.
#:
#: A deny-list rather than a derivation, because nothing in the tree classifies this property and
#: inventing a marker to derive it would be a second classification to keep in step with the first.
#: What makes a hand-kept list safe is that it is a **partition**: `tests/test_mcp_face.py` asserts
#: every read-only tool is either advertised or named here, so a new one cannot join this surface by
#: being forgotten — the same discipline `agent.authz` uses for read versus write.
WITHHELD: dict[str, str] = {
    # Scoped to a turn this caller does not have.
    "ask_clarifying_question": "puts a question to the chemist in the conversation; there is none",
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
}


def advertised_tools() -> list[str]:
    """The names this face serves: read-only, and not scoped to a turn this caller does not have.

    The first half is derived rather than declared — a tool's classification in `agent.authz` is
    the single statement of whether it writes, and this asks that statement rather than restating
    it, so a tool that changes side never has to be remembered in two places. The second half is
    `WITHHELD`, which is a list because nothing classifies that property; it is held honest by
    being a partition rather than an allow-list.
    """
    return sorted(
        name
        for fn in registered_tools()
        if (name := getattr(fn, "__name__", "")) in READ_ONLY_TOOLS and name not in WITHHELD
    )


def build_face() -> FastMCP:
    """A `FastMCP` serving exactly the read-only in-process tools.

    The functions are registered unchanged — same signature, same docstring, same return type — so
    an external caller sees the tool a chemist sees, including its caveats. A wrapper that
    reformatted them would be a second description of one capability.
    """
    server = FastMCP(FACE_NAME)
    allowed = set(advertised_tools())
    for fn in registered_tools():
        if getattr(fn, "__name__", "") in allowed:
            server.tool()(fn)
    return server


def face_token_env() -> str:
    """The environment variable the face's bearer token is read from.

    Named here rather than in a manifest because this face is not a connector: nothing discovers
    it, and `CHEMCLAW_CONNECTOR_URLS` must never name it — a `chemclaw-read` entry there would
    make this deployment dial *itself* for a narrower copy of tools it already has in process.
    """
    return settings.mcp_face_token_env


def create_face_app() -> FastAPI:
    """The FastAPI app for the read-only MCP face, on the transport connectors already use.

    Reusing `connector_app` rather than assembling a second transport is the point: it owns the
    five non-obvious things about serving MCP that this repository has already paid to learn — the
    session manager the parent app has to run, the route order that decides what reaches `/mcp`,
    the caller re-binding per tool call, the error sanitising, and the forced log configuration.
    A hand-rolled second one would rediscover them.
    """
    logger.info(
        "mcp_face.serving: %d read-only tool(s): %s",
        len(advertised_tools()),
        ", ".join(advertised_tools()),
    )
    return connector_app(build_face(), name=FACE_NAME, token_env=face_token_env())


def main() -> None:
    """Configure this process, then serve the read-only face.

    A process role of its own for the reason `connectors/server_entry.py` states at length: a
    process that is exec'd straight at an app object has no module owning its startup, and so runs
    with no secret redaction, no correlation id and no meter provider. The app target is passed to
    uvicorn as a string so it is built *after* logging is configured.
    """
    import uvicorn

    from chemclaw.core.logging import configure_logging, configure_telemetry

    configure_logging()
    configure_telemetry()
    logger.info("mcp face starting on %s:%s", settings.service_host, settings.service_port)
    uvicorn.run(
        "chemclaw.api.mcp_face:create_face_app",
        factory=True,
        host=settings.service_host,
        port=settings.service_port,
        # Ours is already applied above; letting uvicorn install its own would replace it.
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
