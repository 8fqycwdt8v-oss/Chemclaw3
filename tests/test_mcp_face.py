"""The read-only MCP face: what this system will and will not answer for another agent.

The advertised set is a partition, not an allow-list: every read-only tool is advertised or named
in `WITHHELD` with its reason, checked in both directions, and no state-changing tool can reach it.
"""

from pathlib import Path

import chemclaw.agent.chemclaw_agent  # noqa: F401  (populates the capability-tool registry)
from chemclaw.agent.authz import READ_ONLY_TOOLS, STATE_CHANGING_TOOLS
from chemclaw.api.mcp_face import WITHHELD, advertised_tools, build_face, face_token_env
from chemclaw.core.tool_registry import registered_tool_names

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def test_every_read_only_tool_is_advertised_or_named_as_turn_scoped() -> None:
    """Every read-only tool is advertised or withheld, checked against the live registry."""
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
    """No state-changing tool can reach the face.

    A caller holds a bearer token and nothing else (no principal, roles or session), so the face may
    serve reads only; asserted over the derived list.
    """
    assert not set(advertised_tools()) & set(STATE_CHANGING_TOOLS)
    for name in ("record_knowledge_note", "request_external_input", "request_development_report"):
        assert name not in advertised_tools()


def test_an_attachment_is_not_readable_through_the_face() -> None:
    """An attachment is not readable through the face.

    `read_attachment` returns a file someone uploaded to a conversation; serving it to any token
    holder would disclose one person's upload.
    """
    assert "read_attachment" in WITHHELD
    assert "read_attachment" not in advertised_tools()


def test_the_face_serves_exactly_what_it_advertises() -> None:
    """The `FastMCP` instance serves exactly the derived name list.

    Built rather than inspected, because the registration loop is where a name could be advertised
    and not served.
    """
    face = build_face()
    served = {tool.name for tool in face._tool_manager.list_tools()}
    assert served == set(advertised_tools())


def test_the_face_states_its_own_credential_rather_than_inheriting_an_absence() -> None:
    """The face states its own credential rather than inheriting an absence.

    It has no `connector.yaml`, and `connector_app` leaves such an app open, so the face passes
    `token_env` explicitly. An anonymous surface over the knowledge graph would not be noticed from
    outside.
    """
    assert face_token_env()
    source = (SRC / "api" / "mcp_face.py").read_text(encoding="utf-8")
    assert "token_env=face_token_env()" in source


def test_the_face_is_not_addressable_as_a_connector() -> None:
    """No `connector.yaml` names the face, so a deployment cannot dial itself.

    Otherwise the front door could reach over HTTP for a narrower copy of tools it holds in process.
    """
    manifests = [path.read_text(encoding="utf-8") for path in SRC.rglob("connector.yaml")]
    assert not [text for text in manifests if "chemclaw-read" in text]


#: Exactly what this face serves. A golden set, because `advertised_tools()` is derived as
#: `(registry ∩ READ_ONLY_TOOLS) − WITHHELD`, so the partition test holds by construction even when
#: a new read-only tool joins by being forgotten. An explicit list makes each addition a decision.
_ADVERTISED = {
    # Exported deliberately: it reads no store and opens no session, so a caller learns only the
    # arithmetic over inputs they supplied.
    "check_against_specification",
    "estimate_stability_trend",
    "expand_note",
    "find_knowledge_gaps",
    "find_notes",
    "gather_evidence",
    "recall_observations",
    "condense_protocols",
}


def test_the_face_serves_exactly_the_tools_this_list_names() -> None:
    """The face serves exactly `_ADVERTISED`; a new read-only tool cannot arrive by default.

    When this fails, do not just add the name: decide whether the tool answers about this
    deployment's people or about its chemistry, and put it on the matching side.
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
    """No deployment-wide read reaches the face: the predicate is chemistry, not people.

    Named here as well as in `WITHHELD`, so removing one from the deny-list fails a test that says
    why. Each answers something about this deployment's people: commitments, who waits on whom, a
    named person's costs, someone else's run, or a conversation's record.
    """
    people_not_chemistry = {
        "assemble_evidence_pack",
        "check_pending_requests",
        "review_activity",
        "review_commitments",
        "find_past_jobs",
        # The same disclosure as `find_past_jobs`: no actor check, it returns the run's summary,
        # result and rationale, and a job id is a pure function of its arguments, so it can be
        # guessed.
        "get_durable_job_status",
    }
    leaked = sorted(people_not_chemistry & set(advertised_tools()))
    assert leaked == [], (
        f"{leaked} are served to anything holding the face's bearer token; they answer questions "
        "about this deployment's people rather than about its chemistry"
    )


def test_the_evidence_pack_is_withheld_because_it_has_no_actor_to_authorize_against() -> None:
    """The evidence pack is withheld because there is no actor to authorize against.

    `assemble_evidence_pack` checks session ownership, and the face has no authenticated actor, so
    the gate could only refuse and the `session_id` argument would name someone else's session.
    """
    assert "assemble_evidence_pack" in WITHHELD
    assert "ownership" in WITHHELD["assemble_evidence_pack"]
