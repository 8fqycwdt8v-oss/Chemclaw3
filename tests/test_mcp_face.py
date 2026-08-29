"""The read-only MCP face: what this system will and will not answer for another agent.

ChemClaw3 has always been an MCP *client* and never a server, so nothing else in the building could
ask the system that holds the chemistry anything. This surface exports that value with none of an
effector's blast radius — provided two things hold, which is what this file asserts.

The advertised set is a **partition**, not an allow-list. Every read-only tool is either advertised
or named in `TURN_SCOPED` with its reason, in both directions, so a tool cannot join this surface by
being forgotten and cannot silently leave it either.
"""

from pathlib import Path

import chemclaw.agent.chemclaw_agent  # noqa: F401  (populates the capability-tool registry)
from chemclaw.agent.authz import READ_ONLY_TOOLS, STATE_CHANGING_TOOLS
from chemclaw.api.mcp_face import TURN_SCOPED, advertised_tools, build_face, face_token_env
from chemclaw.core.tool_registry import registered_tool_names

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def test_every_read_only_tool_is_advertised_or_named_as_turn_scoped() -> None:
    """The partition, in both directions.

    An allow-list drifts invisibly: a tool added to the read-only set would silently join this
    surface, and one that stopped being read-only would silently stay on it. A partition checked
    against the live registry cannot.
    """
    registered = set(registered_tool_names())
    read_only = registered & set(READ_ONLY_TOOLS)
    assert read_only == set(advertised_tools()) | (set(TURN_SCOPED) & registered), (
        "a read-only tool is neither advertised on the MCP face nor named in TURN_SCOPED. "
        "Decide which, and say why beside its entry — deciding by omission is what this prevents."
    )
    # And a name in the deny-list that no longer exists is stale state, which reads as live.
    assert set(TURN_SCOPED) <= registered, (
        f"TURN_SCOPED names {sorted(set(TURN_SCOPED) - registered)}, which nothing registers"
    )


def test_no_state_changing_tool_can_reach_the_face() -> None:
    """The one property that makes this surface safe to expose at all.

    A caller here holds a bearer token and nothing else — no Entra principal, no role set, no
    session. So the face may serve reads and only reads, and this asserts it over the derived list
    rather than over the intention.
    """
    assert not set(advertised_tools()) & set(STATE_CHANGING_TOOLS)
    for name in ("propose_knowledge_note", "request_external_input", "request_development_report"):
        assert name not in advertised_tools()


def test_an_attachment_is_not_readable_through_the_face() -> None:
    """The one `TURN_SCOPED` entry that is a disclosure surface rather than an empty answer.

    `read_attachment` returns the contents of a file somebody uploaded to a conversation. It is
    classified read-only and correctly so; serving it to whatever holds the face's token would hand
    out one person's upload. Named on its own because the others merely answer emptily.
    """
    assert "read_attachment" in TURN_SCOPED
    assert "read_attachment" not in advertised_tools()


def test_the_face_serves_exactly_what_it_advertises() -> None:
    """The `FastMCP` instance and the derived name list agree.

    Built rather than inspected statically: the registration loop is where a name could be
    advertised and not served, which is the half of the manifest contract `Chemclaw3-mcp` states
    ("every declared tool is served, or the client advertises a capability that fails at call
    time").
    """
    face = build_face()
    served = {tool.name for tool in face._tool_manager.list_tools()}
    assert served == set(advertised_tools())


def test_the_face_states_its_own_credential_rather_than_inheriting_an_absence() -> None:
    """It has no `connector.yaml`, and `connector_app`'s manifest lookup leaves such an app open.

    That default is right for the bare apps the transport tests build and wrong for a surface
    exposing the corpus, so the face passes `token_env` explicitly. Asserted over the source
    because the alternative — an anonymous MCP surface over the whole knowledge graph — is the one
    failure here that nobody would notice from the outside.
    """
    assert face_token_env()
    source = (SRC / "api" / "mcp_face.py").read_text(encoding="utf-8")
    assert "token_env=face_token_env()" in source


def test_the_face_is_not_addressable_as_a_connector() -> None:
    """No `connector.yaml` anywhere names it, so this deployment cannot dial itself.

    A `chemclaw-read` entry in `CHEMCLAW_CONNECTOR_URLS` would make the front door reach over HTTP
    for a narrower copy of tools it already holds in process — and would put the audit trail's own
    reader behind a network hop.
    """
    manifests = [path.read_text(encoding="utf-8") for path in SRC.rglob("connector.yaml")]
    assert not [text for text in manifests if "chemclaw-read" in text]
