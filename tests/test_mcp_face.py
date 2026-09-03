"""The read-only MCP face: what this system will and will not answer for another agent.

ChemClaw3 has always been an MCP *client* and never a server, so nothing else in the building could
ask the system that holds the chemistry anything. This surface exports that value with none of an
effector's blast radius — provided two things hold, which is what this file asserts.

The advertised set is a **partition**, not an allow-list. Every read-only tool is either advertised
or named in `WITHHELD` with its reason, in both directions, so a tool cannot join this surface by
being forgotten and cannot silently leave it either.
"""

from pathlib import Path

import chemclaw.agent.chemclaw_agent  # noqa: F401  (populates the capability-tool registry)
from chemclaw.agent.authz import READ_ONLY_TOOLS, STATE_CHANGING_TOOLS
from chemclaw.api.mcp_face import WITHHELD, advertised_tools, build_face, face_token_env
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
    assert read_only == set(advertised_tools()) | (set(WITHHELD) & registered), (
        "a read-only tool is neither advertised on the MCP face nor named in WITHHELD. "
        "Decide which, and say why beside its entry — deciding by omission is what this prevents."
    )
    # And a name in the deny-list that no longer exists is stale state, which reads as live.
    assert set(WITHHELD) <= registered, (
        f"WITHHELD names {sorted(set(WITHHELD) - registered)}, which nothing registers"
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
    """The one `WITHHELD` entry that is a disclosure surface rather than an empty answer.

    `read_attachment` returns the contents of a file somebody uploaded to a conversation. It is
    classified read-only and correctly so; serving it to whatever holds the face's token would hand
    out one person's upload. Named on its own because the others merely answer emptily.
    """
    assert "read_attachment" in WITHHELD
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


#: Exactly what this face serves. **A golden set, because the partition test cannot fail for the
#: case it exists to catch.** `advertised_tools()` is *derived* as
#: `(registry ∩ READ_ONLY_TOOLS) − WITHHELD`, so "every read-only tool is advertised or withheld" is
#: true by construction and stays true when a new read-only tool joins this surface by being
#: forgotten — which is precisely how four deployment-wide reads were being served. Only an explicit
#: list makes adding one a decision somebody has to take.
_ADVERTISED = {
    "expand_note",
    "find_knowledge_gaps",
    "find_notes",
    "gather_evidence",
    "recall_observations",
    "condense_protocols",
}


def test_the_face_serves_exactly_the_tools_this_list_names() -> None:
    """A new read-only tool must be classified here or in `WITHHELD` — it cannot arrive by default.

    The failure this replaces: the derived partition assertion passes whether or not a new tool
    should be on this surface, because the surface *is* the derivation. Dropping a name from
    `WITHHELD` re-advertises it and every existing assertion stays green.

    When this fails, do not just add the name. Ask the question `WITHHELD` is organised around —
    does this tool answer something about this deployment's *people* or about its *chemistry* — and
    put it on whichever side the answer says.
    """
    advertised = set(advertised_tools())
    arrived = advertised - _ADVERTISED
    vanished = _ADVERTISED - advertised
    assert not arrived, (
        f"{sorted(arrived)} joined the read-only MCP face without anyone deciding they should be "
        "exported. Classify each in WITHHELD or add it here deliberately"
    )
    assert not vanished, (
        f"{sorted(vanished)} no longer reach the face; if that is intended, remove them here"
    )


def test_no_deployment_wide_read_reaches_the_face() -> None:
    """Read-only is not the predicate; "about chemistry rather than about people" is.

    Named individually rather than derived, because nothing in the tree classifies this property —
    but named *here* as well as in `WITHHELD` so that removing one from the deny-list fails a test
    that says why it was there, rather than quietly widening the surface. Each of these was
    advertised at one point, and each answers a question about this deployment's people: what the
    programme committed to, who is waiting on whom, what a named employee's turns cost, what
    somebody else's run was for, and one conversation's entire record.
    """
    people_not_chemistry = {
        "assemble_evidence_pack",
        "check_pending_requests",
        "review_activity",
        "review_commitments",
        "find_past_jobs",
        # The same disclosure as `find_past_jobs` through the other door, and it reached `WITHHELD`
        # a review later than the rest — which is exactly the drift this second list exists to
        # catch. It applies no actor check and returns the run's summary, result and free-text
        # rationale, and a job id is `job_workflow_id(connector, job, payload)`: a pure function of
        # its arguments, so an external caller guesses rather than discovers them.
        "get_durable_job_status",
    }
    leaked = sorted(people_not_chemistry & set(advertised_tools()))
    assert leaked == [], (
        f"{leaked} are served to anything holding the face's bearer token; they answer questions "
        "about this deployment's people rather than about its chemistry"
    )


def test_the_evidence_pack_is_withheld_because_it_has_no_actor_to_authorize_against() -> None:
    """The reason matters as much as the exclusion, and is asserted so it cannot be lost.

    `assemble_evidence_pack` gained a session-ownership gate, which is the right control in a
    conversation and is *unavailable* here: the face has no authenticated actor at all, so the gate
    could only ever refuse — and the tool's `session_id` argument means a caller would be naming
    somebody else's session by construction.
    """
    assert "assemble_evidence_pack" in WITHHELD
    assert "ownership" in WITHHELD["assemble_evidence_pack"]
