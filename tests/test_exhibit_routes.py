"""Artefacts over HTTP, through the production app: who may reach them, and every route's contract.

Driven through `create_app` and `TestClient` because each property is a claim about a request — who
it is, which session it names, what it is told — and the session gate is the app's. The artefact
store is parametrized over both backends: the app runs on the in-memory session layer (so ownership
and membership are the real routes' own), and the `postgres` arm swaps a real database in under the
artefact routes only.
"""

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from chemclaw.agent import exhibit_tools
from chemclaw.agent import session_members as members_module
from chemclaw.agent.exhibit_tools import create_exhibit
from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.api import app as front_door
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import exhibits as routes
from chemclaw.api.routes.streams import _exhibit_event
from chemclaw.api.tool_results import content_address, store_tool_result
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.exhibits import bindings
from chemclaw.exhibits.export import MEDIA_TYPES
from chemclaw.exhibits.grounding import html_text, stated_figures
from chemclaw.exhibits.models import PUSH_KIND, parse_spec
from chemclaw.exhibits.store import InMemoryExhibitStore, PostgresExhibitStore
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import migrated_db_or_skip
from tests.test_service import _FakeOwnerStore, _no_connectors

_ANA = Principal(oid="ana-artefacts", upn="ana@corp", roles=frozenset({"process-chemist"}))
_BEN = Principal(oid="ben-artefacts", upn="ben@corp", roles=frozenset({"analyst"}))
_CAT = Principal(oid="cat-artefacts", upn="cat@corp", roles=frozenset({"analyst"}))

_TABLE: dict[str, Any] = {
    "kind": "table",
    "columns": [
        {"key": "solvent", "label": "Solvent"},
        {"key": "y", "label": "Yield", "unit": "%"},
    ],
    "rows": [{"solvent": "=1+1", "y": 76}],
}


@pytest.fixture(params=["memory", "postgres"])
def app(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The production app, with the artefact routes on the parametrized store."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    members_module.session_member_store.cache_clear()
    if request.param == "postgres":
        asyncio.run(migrated_db_or_skip())
        store: Any = PostgresExhibitStore()
    else:
        store = InMemoryExhibitStore()
    monkeypatch.setattr(routes, "default_exhibit_store", lambda: store)
    yield create_app(owner_store=_FakeOwnerStore(), connector_factory=_no_connectors)
    members_module.session_member_store.cache_clear()


def _as(app: Any, principal: Principal) -> TestClient:
    """A client whose requests arrive as `principal` — authentication is not under test."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def _shared(app: Any) -> str:
    """A session Ana owns with Ben admitted."""
    session = str(_as(app, _ANA).post("/sessions").json()["session_id"])
    assert _as(app, _ANA).put(f"/sessions/{session}/members/{_BEN.oid}").status_code == 204
    return session


def _create(app: Any, principal: Principal, session: str, body: dict[str, Any]) -> Any:
    return _as(app, principal).post(f"/sessions/{session}/exhibits", json=body)


def test_the_owner_and_a_member_reach_artefacts_and_a_stranger_gets_404(app: Any) -> None:
    """Every route answers a stranger as an unknown session, before and after an artefact exists."""
    session = _shared(app)
    made = _create(app, _ANA, session, {"kind": "table", "title": "Screen", "spec": _TABLE})
    assert made.status_code == 201, made.text
    view = made.json()
    xid = view["exhibit_id"]
    assert (view["revision"], view["author_kind"], view["author"]) == (1, "human", _ANA.oid)
    assert view["unverified_figures"] == []

    paths = [
        f"/sessions/{session}/exhibits",
        f"/sessions/{session}/exhibits/{xid}",
        f"/sessions/{session}/exhibits/{xid}/revisions",
        f"/sessions/{session}/exhibits/{xid}/diff",
        f"/sessions/{session}/exhibits/{xid}/export.csv",
    ]
    for path in paths:
        assert _as(app, _BEN).get(path).status_code == 200, f"{path} refused a member"
        assert _as(app, _CAT).get(path).status_code == 404, f"{path} leaked to a stranger"
    assert (
        _create(app, _CAT, session, {"kind": "table", "title": "x", "spec": _TABLE}).status_code
        == 404
    )
    revise = {"parent_revision": 1, "spec": _TABLE}
    assert (
        _as(app, _CAT)
        .post(f"/sessions/{session}/exhibits/{xid}/revisions", json=revise)
        .status_code
        == 404
    )

    listed = _as(app, _BEN).get(f"/sessions/{session}/exhibits").json()
    assert listed["enabled"] is True
    assert [h["exhibit_id"] for h in listed["exhibits"]] == [xid]


def test_a_member_revises_and_a_stale_edit_is_409_with_the_head(app: Any) -> None:
    """Ben's revision lands as Ben's; Ana's edit of revision 1 is refused naming the head."""
    session = _shared(app)
    xid = _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": _TABLE}).json()[
        "exhibit_id"
    ]
    edited = {**_TABLE, "rows": [{"solvent": "THF", "y": 71}]}
    ben = _as(app, _BEN).post(
        f"/sessions/{session}/exhibits/{xid}/revisions",
        json={"parent_revision": 1, "spec": edited, "change_note": "THF", "title": "S2"},
    )
    assert ben.status_code == 201, ben.text
    body = ben.json()
    assert (body["revision"], body["author"], body["title"], body["change_note"]) == (
        2,
        _BEN.oid,
        "S2",
        "THF",
    )

    stale = _as(app, _ANA).post(
        f"/sessions/{session}/exhibits/{xid}/revisions", json={"parent_revision": 1, "spec": _TABLE}
    )
    assert stale.status_code == 409
    assert stale.json() == {"detail": {"code": "stale_revision", "head_revision": 2}}

    history = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/revisions").json()
    assert [(r["revision"], r["author"]) for r in history["revisions"]] == [
        (1, _ANA.oid),
        (2, _BEN.oid),
    ]
    diff = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/diff?from=1&to=2").json()
    assert diff["from_revision"] == 1 and diff["to_revision"] == 2
    assert [(c["path"], c["kind"]) for c in diff["changes"]] == [
        ("rows[0].solvent", "changed"),
        ("rows[0].y", "changed"),
    ]
    assert _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/diff").json() == diff
    first = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}?revision=1").json()
    assert first["revision"] == 1 and first["head_revision"] == 2


def test_an_invalid_spec_or_a_kind_change_is_422_and_an_unknown_id_404(app: Any) -> None:
    """Validation is the agent's validation, worded; a revision cannot change the kind."""
    session = _shared(app)
    bad = _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": {"kind": "table"}})
    assert bad.status_code == 422 and "columns" in bad.text
    mismatch = _create(
        app, _ANA, session, {"kind": "document", "title": "S", "spec": {"kind": "table", **_TABLE}}
    )
    assert mismatch.status_code == 422
    xid = _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": _TABLE}).json()[
        "exhibit_id"
    ]
    other_kind = _as(app, _ANA).post(
        f"/sessions/{session}/exhibits/{xid}/revisions",
        json={"parent_revision": 1, "spec": {"kind": "document", "markdown": "x"}},
    )
    assert other_kind.status_code == 422
    assert (
        _as(app, _ANA).get(f"/sessions/{session}/exhibits/xb-0000000000000000").status_code == 404
    )
    assert _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}?revision=9").status_code == 404


def test_a_pinned_result_must_be_one_this_session_can_fetch(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session's stored bytes are not pinnable; this session's are."""
    session = _shared(app)
    mine = "d" * 64

    async def _refs(session_id: str) -> frozenset[str]:
        return frozenset({mine}) if session_id == session else frozenset()

    monkeypatch.setattr(front_door, "fetchable_refs", _refs)

    def pin(ref: str) -> dict[str, Any]:
        spec = {"kind": "result", "tool": "t", "result_ref": ref}
        return {"kind": "result", "title": "screen_hazards", "spec": spec}

    assert _create(app, _ANA, session, pin("e" * 64)).status_code == 422
    pinned = _create(app, _ANA, session, pin(mine))
    assert pinned.status_code == 201, pinned.text
    assert pinned.json()["spec"] == {"kind": "result", "result_ref": mine, "tool": "t"}


def test_the_session_cap_is_409_exhibit_limit(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract's code, so a surface can say why rather than show a generic conflict."""
    monkeypatch.setattr(settings, "exhibit_max_per_session", 1)
    session = _shared(app)
    body = {"kind": "document", "title": "a", "spec": {"kind": "document", "markdown": "a"}}
    assert _create(app, _ANA, session, body).status_code == 201
    full = _create(app, _ANA, session, body)
    assert full.status_code == 409 and full.json()["detail"]["code"] == "exhibit_limit"


def test_an_export_is_an_attachment_guarded_against_formulas(app: Any) -> None:
    """`Content-Disposition: attachment`, the formula cell neutralised, a format not offered 404."""
    session = _shared(app)
    xid = _create(app, _ANA, session, {"kind": "table", "title": "Screen", "spec": _TABLE}).json()[
        "exhibit_id"
    ]
    exported = _as(app, _BEN).get(f"/sessions/{session}/exhibits/{xid}/export.csv")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == (
        f'attachment; filename="Screen-{xid}-r1.csv"'
    )
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows == [["Solvent", "Yield (%)"], ["'=1+1", "76"]]
    markdown = _as(app, _BEN).get(f"/sessions/{session}/exhibits/{xid}/export.md")
    assert markdown.status_code == 200 and markdown.text.startswith("| Solvent |")
    assert _as(app, _BEN).get(f"/sessions/{session}/exhibits/{xid}/export.smi").status_code == 404


def test_the_listing_says_when_artefacts_are_off(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`enabled` mirrors `agent_exhibits_enabled`, so a surface can hide the pane."""
    monkeypatch.setattr(settings, "agent_exhibits_enabled", False)
    session = _shared(app)
    assert _as(app, _ANA).get(f"/sessions/{session}/exhibits").json() == {
        "enabled": False,
        "html_enabled": True,
        "exhibits": [],
    }


def test_the_cross_session_listing_is_empty_without_a_durable_registry(app: Any) -> None:
    """`GET /exhibits` on the in-memory session layer answers `[]`, as `GET /sessions` does."""
    session = _shared(app)
    _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": _TABLE})
    listed = _as(app, _ANA).get("/exhibits?limit=5")
    assert listed.status_code == 200 and listed.json() == {"exhibits": []}


def test_a_message_referencing_an_unknown_artefact_is_422_before_any_turn(app: Any) -> None:
    """`exhibit_refs` is resolved within the session first; a miss is a refusal, never a drop."""
    session = _shared(app)
    refused = _as(app, _ANA).post(
        f"/sessions/{session}/messages",
        json={"message": "explain it", "exhibit_refs": [{"exhibit_id": "xb-0000000000000000"}]},
    )
    assert refused.status_code == 422 and "xb-0000000000000000" in refused.text
    assert refused.json()["detail"]["code"] == "invalid_exhibit_ref"
    assert "xb-0000000000000000" in refused.json()["detail"]["message"]
    too_many = [{"exhibit_id": f"xb-{i:016x}"} for i in range(settings.exhibit_max_refs + 1)]
    assert (
        _as(app, _ANA)
        .post(f"/sessions/{session}/messages", json={"message": "x", "exhibit_refs": too_many})
        .status_code
        == 422
    )


async def test_a_persons_write_is_pushed_to_the_sessions_other_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The push lands in the session's mailbox and the event stream renders it as `exhibit`."""
    await migrated_db_or_skip()
    monkeypatch.setattr(settings, "session_store", "postgres")
    session = uuid4().hex
    view = await PostgresExhibitStore().create(
        session,
        title="Screen",
        spec=parse_spec(_TABLE),
        author_kind="human",
        author=_BEN.oid,
    )
    await routes._announce(view, "created")
    (pushed,) = await claim_unconsumed(session, kinds=(PUSH_KIND,))
    event = _exhibit_event(pushed.payload)
    assert event is not None
    assert (event.type, event.exhibit_id, event.op, event.author_kind, event.author) == (
        "exhibit",
        view.exhibit_id,
        "created",
        "human",
        _BEN.oid,
    )
    assert event.call_id == "", "a person's write was made by no tool call"
    assert _exhibit_event({"exhibit_id": "x"}) is None


class _Recorder(ScriptedTurn):
    """A turn that answers once and keeps the message the model was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Record what reached the model, then answer."""
        self.messages.append(message)
        yield "ok"


def test_a_turn_is_told_the_chemists_artefact_once_and_never_handed_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the front door: news rides on the turn's message, state does not.

    The chemist's words lead; the artefact they created is announced as theirs; the referenced
    revision follows framed as data. The *listing* is absent — it is request-only state on the
    instructions (`ExhibitListing`), and a turn input is persisted with the thread. The read mark
    moves after the turn completes, so the second turn is not told the same thing again; and the
    transcript keeps only the chemist's words.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    turn = _Recorder()
    app = create_app(
        owner_store=_FakeOwnerStore(),
        connector_factory=_no_connectors,
        graph_factory=turn.graph_factory,
    )
    session = str(_as(app, _ANA).post("/sessions").json()["session_id"])
    xid = _create(app, _ANA, session, {"kind": "table", "title": "Screen", "spec": _TABLE}).json()[
        "exhibit_id"
    ]

    def _send(body: dict[str, Any]) -> None:
        with _as(app, _ANA).stream("POST", f"/sessions/{session}/messages", json=body) as res:
            assert res.status_code == 200, res.read()
            for _line in res.iter_lines():
                pass

    _send({"message": "make the yield a fraction", "exhibit_refs": [{"exhibit_id": xid}]})
    _send({"message": "and sort it"})
    first, second = turn.messages
    assert first.startswith("make the yield a fraction")
    assert f'{xid} "Screen" (table): created by the chemist' in first
    assert "refers to these artefacts" in first and '"y": 76' in first
    assert "Artefacts in this conversation" not in first, "the listing is not turn input"
    assert second == "and sort it", "an edit already told was told again"
    transcript = _as(app, _ANA).get(f"/sessions/{session}/messages").json()
    assert transcript[0]["text"] == "make the yield a fraction"


def test_revision_one_diffs_as_one_addition(app: Any) -> None:
    """Revision 1 has no parent, so all of it is added — never an empty diff reading "unchanged"."""
    session = _shared(app)
    table = _create(app, _ANA, session, {"kind": "table", "title": "T", "spec": _TABLE}).json()
    diff = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{table['exhibit_id']}/diff").json()
    assert (diff["from_revision"], diff["to_revision"]) == (0, 1)
    ((change,),) = (diff["changes"],)
    assert (change["path"], change["kind"], change["before"]) == ("spec", "added", "")
    doc = {"kind": "document", "markdown": "# Plan\n\nStep one."}
    made = _create(app, _ANA, session, {"kind": "document", "title": "D", "spec": doc}).json()
    diff = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{made['exhibit_id']}/diff").json()
    assert [(c["path"], c["kind"]) for c in diff["changes"]] == [("lines 1-3", "added")]


def test_the_revision_cap_is_409_exhibit_limit(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """One artefact holds at most `exhibit_max_revisions`; the next write is refused, worded."""
    monkeypatch.setattr(settings, "exhibit_max_revisions", 2)
    session = _shared(app)
    xid = _create(app, _ANA, session, {"kind": "table", "title": "T", "spec": _TABLE}).json()[
        "exhibit_id"
    ]
    url = f"/sessions/{session}/exhibits/{xid}/revisions"
    assert _as(app, _ANA).post(url, json={"parent_revision": 1, "spec": _TABLE}).status_code == 201
    full = _as(app, _ANA).post(url, json={"parent_revision": 2, "spec": _TABLE})
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "exhibit_limit"
    assert "create a new artefact" in full.json()["detail"]["message"]


def test_a_malformed_artefact_id_is_refused_as_malformed(app: Any) -> None:
    """Path segments and message refs are held to the minted `xb-` shape before any lookup."""
    session = _shared(app)
    for path in (
        f"/sessions/{session}/exhibits/not-an-id",
        f"/sessions/{session}/exhibits/xb-ZZZZ/revisions",
        f"/sessions/{session}/exhibits/xb-0000000000000000x/diff",
    ):
        assert _as(app, _ANA).get(path).status_code == 422, path
    refused = _as(app, _ANA).post(
        f"/sessions/{session}/messages",
        json={"message": "x", "exhibit_refs": [{"exhibit_id": "../etc"}]},
    )
    assert refused.status_code == 422


def test_exhibit_refs_while_artefacts_are_off_are_refused_not_dropped(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off, no note is composed — so a reference is refused, never checked and then ignored."""
    session = _shared(app)
    xid = _create(app, _ANA, session, {"kind": "table", "title": "T", "spec": _TABLE}).json()[
        "exhibit_id"
    ]
    monkeypatch.setattr(settings, "agent_exhibits_enabled", False)
    refused = _as(app, _ANA).post(
        f"/sessions/{session}/messages",
        json={"message": "explain it", "exhibit_refs": [{"exhibit_id": xid}]},
    )
    assert refused.status_code == 422 and "switched off" in refused.text
    assert refused.json()["detail"]["code"] == "invalid_exhibit_ref"


def test_a_persons_write_is_recorded_with_who_and_which_request(
    app: Any, caplog: pytest.LogCaptureFixture, request: pytest.FixtureRequest
) -> None:
    """A person's write is audited by its row and its event, as every human REST decision is.

    The revision row names its author and the request's correlation id, and each write emits one
    structured `exhibit.*` event naming the actor. No `AuditEvent`: that trail is the tool-call
    middleware's (`api/routes/workflows.py` states the convention), so this is what an auditor
    joins on.
    """
    session = _shared(app)
    caplog.set_level("INFO", logger="chemclaw.exhibits.telemetry")
    made = _create(app, _ANA, session, {"kind": "table", "title": "T", "spec": _TABLE})
    xid = made.json()["exhibit_id"]
    revised = _as(app, _BEN).post(
        f"/sessions/{session}/exhibits/{xid}/revisions",
        json={"parent_revision": 1, "spec": _TABLE, "change_note": "checked"},
    )
    assert revised.status_code == 201
    events = [(r.getMessage(), getattr(r, "actor", None)) for r in caplog.records]
    assert any("created by" in m and actor == _ANA.oid for m, actor in events), events
    assert any("revised by" in m and actor == _BEN.oid for m, actor in events), events
    history = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/revisions").json()
    assert [(r["author_kind"], r["author"]) for r in history["revisions"]] == [
        ("human", _ANA.oid),
        ("human", _BEN.oid),
    ]
    if request.node.callspec.params["app"] == "postgres":
        expected = [
            made.headers["x-chemclaw-correlation-id"],
            revised.headers["x-chemclaw-correlation-id"],
        ]
        assert asyncio.run(_correlations(xid)) == expected


async def _correlations(exhibit_id: str) -> list[str]:
    """The correlation id each revision of `exhibit_id` was written under, oldest first."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT correlation_id FROM session_exhibit_revisions WHERE exhibit_id = %s "
                "ORDER BY revision",
                (exhibit_id,),
            )
            return [str(row[0]) for row in await cur.fetchall()]


def test_a_report_job_keeps_its_artefact_only_for_a_reader_of_its_origin(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /jobs/{id}?session_id=` keeps `exhibit_id` for the origin's readers and only them.

    The owner and a member reading the origin keep it, so a reloaded conversation can still offer
    "Open report"; a stranger naming the origin, a reader naming another session, an unknown
    session and no session at all get the same answer without it — never an error, so the
    parameter says nothing about which sessions exist.
    """
    from chemclaw.agent.durable_tools import completed_job_status
    from chemclaw.durable.connector_job import ConnectorJobResult

    origin = _shared(app)
    elsewhere = str(_as(app, _ANA).post("/sessions").json()["session_id"])
    raw = ConnectorJobResult(
        summary="Drafted 'W'",
        data={
            "note_ref": "commit://1",
            "exhibit_id": "xb-00000000000000ab",
            "exhibit_session": origin,
        },
    ).model_dump()

    async def _status(job_id: str) -> Any:
        return completed_job_status(job_id, raw)

    monkeypatch.setattr(front_door, "job_status", _status)

    def _result(principal: Principal, session: str | None) -> dict[str, Any]:
        params = {"session_id": session} if session is not None else {}
        response = _as(app, principal).get("/jobs/report-x", params=params)
        assert response.status_code == 200, response.text
        return dict(response.json()["result"])

    kept = {"note_ref": "commit://1", "exhibit_id": "xb-00000000000000ab"}
    stripped = {"note_ref": "commit://1"}
    assert _result(_ANA, origin) == kept
    assert _result(_BEN, origin) == kept
    assert _result(_CAT, origin) == stripped
    assert _result(_ANA, elsewhere) == stripped
    assert _result(_ANA, "0" * 32) == stripped
    assert _result(_ANA, None) == stripped


def test_the_published_job_route_declares_its_reading_session(app: Any) -> None:
    """The OpenAPI document names the optional `session_id` on `GET /jobs/{id}`."""
    parameters = app.openapi()["paths"]["/jobs/{job_id}"]["get"]["parameters"]
    named = {parameter["name"]: parameter for parameter in parameters}
    assert "session_id" in named and named["session_id"]["required"] is False


# --- wave 3: the `html` kind and bound values over HTTP -------------------------------------------
#
# `D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves` and
# `D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`. The property this block
# exists for above all is a negative one — **no route answers `text/html` for an artefact** — and
# it is asserted by walking the routes the app serves rather than a list written here, so a new
# export format or artefact route is covered the day it lands.

_PAGE = (
    "<!doctype html><style>.bar{width:100%}</style><h1>Screen</h1><p>THF gave 76.5 %.</p>"
    "<script>const points = [12.25, 40];</script>"
)
_HTML = {"kind": "html", "html": _PAGE, "height": 320}


def test_an_html_page_reads_back_and_exports_as_text_never_as_html(app: Any) -> None:
    """Created by a person, read as JSON, saved as a `.html` attachment served as plain text."""
    session = _shared(app)
    made = _create(app, _ANA, session, {"kind": "html", "title": "Screen page", "spec": _HTML})
    assert made.status_code == 201, made.text
    xid = made.json()["exhibit_id"]
    assert made.json()["spec"] == _HTML and made.json()["raw_spec"] == _HTML
    assert made.json()["bindings"] == []

    exported = _as(app, _BEN).get(f"/sessions/{session}/exhibits/{xid}/export.html")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "text/plain; charset=utf-8"
    disposition = exported.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Screen-page-') and disposition.endswith(
        '.html"'
    )
    assert exported.text == _PAGE + "\n"


def test_no_route_answers_text_html_for_artefact_content(app: Any) -> None:
    """Every GET route under an artefact, every export format, on an html artefact and a table."""
    session = _shared(app)
    xids = [
        _create(app, _ANA, session, {"kind": kind, "title": "t", "spec": spec}).json()["exhibit_id"]
        for kind, spec in (
            ("html", _HTML),
            ("table", {"kind": "table", "columns": [{"key": "a", "label": "A"}], "rows": []}),
        )
    ]
    assert "text/html" not in " ".join(MEDIA_TYPES.values())
    routes = [
        route.path
        for route in app.routes
        if isinstance(route, Route)
        and "GET" in (route.methods or set())
        and "exhibit" in route.path
    ]
    assert any("export" in path for path in routes), routes
    answered = 0
    for xid in xids:
        for path in routes:
            formats = [*MEDIA_TYPES, "htm", "xhtml"] if "{fmt}" in path else [""]
            for fmt in formats:
                url = (
                    path.replace("{session_id}", session)
                    .replace("{exhibit_id}", xid)
                    .replace("{fmt}", fmt)
                )
                response = _as(app, _ANA).get(url)
                answered += response.status_code == 200
                assert not response.headers.get("content-type", "").startswith("text/html"), (
                    f"{url} answered {response.headers['content-type']}"
                )
    assert answered >= 10, "the walk reached too few routes to mean anything"


def test_switching_html_off_refuses_a_new_page_and_keeps_the_ones_held(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off: a create is 422 and the listing says so; an existing page still reads and revises."""
    session = _shared(app)
    xid = _create(app, _ANA, session, {"kind": "html", "title": "p", "spec": _HTML}).json()[
        "exhibit_id"
    ]
    monkeypatch.setattr(settings, "agent_html_artefacts_enabled", False)
    refused = _create(app, _ANA, session, {"kind": "html", "title": "p", "spec": _HTML})
    assert refused.status_code == 422 and "switched off" in refused.text
    listed = _as(app, _ANA).get(f"/sessions/{session}/exhibits").json()
    assert listed["html_enabled"] is False and [h["exhibit_id"] for h in listed["exhibits"]] == [
        xid
    ]
    assert _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}").status_code == 200
    revised = _as(app, _ANA).post(
        f"/sessions/{session}/exhibits/{xid}/revisions",
        json={"parent_revision": 1, "spec": {**_HTML, "height": 400}},
    )
    assert revised.status_code == 201, revised.text


def test_the_page_cap_is_its_own_bytes(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`exhibit_max_html_bytes` refuses a page past it, naming the cap."""
    monkeypatch.setattr(settings, "exhibit_max_html_bytes", 10)
    session = _shared(app)
    refused = _create(app, _ANA, session, {"kind": "html", "title": "p", "spec": _HTML})
    assert refused.status_code == 422 and "10-byte cap" in refused.text


def test_a_pages_figures_are_its_visible_text_never_its_code_or_attributes() -> None:
    """Paragraph and SVG `<text>` figures are read; script, style and attributes are not."""
    assert "100" not in html_text(_PAGE) and "76.5" in html_text(_PAGE)
    assert stated_figures(parse_spec(_HTML)) == ["76.5"]
    svg = (
        '<svg width="640" viewBox="0 0 640 480"><rect x="12" height="300"/>'
        '<text x="40" y="20">81.5 %</text><script>for (let i = 0; i < 999; i++) {}</script></svg>'
    )
    assert stated_figures(parse_spec({"kind": "html", "html": svg})) == ["81.5"]


def test_create_exhibits_description_drops_html_where_the_kind_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off, the model is not offered a kind it would be refused; on, the bound tool is unchanged."""
    from chemclaw.agent.exhibit_tools import HTML_CLAUSE, described_for_deployment
    from chemclaw.agent.tool_schema import as_structured_tool

    bound = as_structured_tool(create_exhibit)
    assert HTML_CLAUSE in bound.description
    assert described_for_deployment(bound) is bound
    monkeypatch.setattr(settings, "agent_html_artefacts_enabled", False)
    off = described_for_deployment(bound)
    assert "html" not in off.description and off.args_schema is bound.args_schema
    assert HTML_CLAUSE in bound.description, "the shared, cached tool was edited in place"

    # And it is what a compiled graph binds, not only what the function returns.
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from tests.fakes_langgraph import ScriptedChatModel
    from tests.test_context_floor import _bound_tools

    graph = build_langgraph_agent(ScriptedChatModel(["ok"]), audit_sink=NullAuditSink())
    [compiled] = [tool for tool in _bound_tools(graph) if tool.name == "create_exhibit"]
    assert "html" not in compiled.description


def test_the_agent_creates_a_page_and_is_refused_one_when_html_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_exhibit` lists the kind; off, the refusal says what to do instead."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(exhibit_tools, "record_exhibit", lambda signal: None)
    session_token = set_current_session_id(uuid4().hex)
    identity = set_current_identity("oid-ana", frozenset())
    try:
        made = json.loads(asyncio.run(create_exhibit("Page", _HTML)))
        assert made["revision"] == 1
        monkeypatch.setattr(settings, "agent_html_artefacts_enabled", False)
        with pytest.raises(ChemclawError, match="switched off"):
            asyncio.run(create_exhibit("Page", _HTML))
    finally:
        reset_current_identity(identity)
        reset_current_session_id(session_token)
    assert "html" in (create_exhibit.__doc__ or "")


# --- a person and bound values --------------------------------------------------------------------

_RESULT = json.dumps({"rows": [{"name": "THF", "yield": 76.5}]})
_REF = content_address(_RESULT)
_COLUMNS = [{"key": "solvent", "label": "Solvent"}, {"key": "y", "label": "Yield", "unit": "%"}]


def _bound_table(result: str = f"r:{_REF[:12]}") -> dict[str, Any]:
    """A table whose yield cell is bound to the stored result."""
    bound = {"$bind": {"result": result, "pointer": "/rows/0/yield"}}
    return {"kind": "table", "columns": _COLUMNS, "rows": [{"solvent": "THF", "y": bound}]}


def test_a_person_keeps_or_detaches_a_binding_and_cannot_invent_one(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Served resolved with `raw_spec` and `bindings`; a kept binding is no change, a detach is one.

    The app runs on the in-memory session layer, so the deployment check is opened by hand and the
    results are stored in the real database the binding reads.
    """
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(bindings, "handles_resolve", lambda: True)
    session = _shared(app)
    asyncio.run(
        store_tool_result(session_id=session, correlation_id="c", tool="screen", text=_RESULT)
    )
    made = _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": _bound_table()})
    assert made.status_code == 201, made.text
    view, xid = made.json(), made.json()["exhibit_id"]
    assert view["spec"]["rows"] == [{"solvent": "THF", "y": 76.5}]
    assert view["raw_spec"]["rows"][0]["y"]["$bind"]["result"] == _REF
    assert view["bindings"] == [
        {
            "path": "rows[0].y",
            "result_ref": _REF,
            "tool": "screen",
            "pointer": "/rows/0/yield",
            "ok": True,
            "error": "",
        }
    ]
    exported = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/export.csv")
    assert exported.text.splitlines()[1] == "THF,76.5"

    revisions = f"/sessions/{session}/exhibits/{xid}/revisions"
    kept = _as(app, _BEN).post(
        revisions, json={"parent_revision": 1, "spec": view["raw_spec"], "change_note": "same"}
    )
    assert kept.status_code == 201, kept.text
    diff = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/diff?from=1&to=2").json()
    assert diff["changes"] == []

    detached = {**view["raw_spec"], "rows": [{"solvent": "THF", "y": 76.5}]}
    moved = _as(app, _ANA).post(revisions, json={"parent_revision": 2, "spec": detached})
    assert moved.status_code == 201 and moved.json()["bindings"] == []
    diff = _as(app, _ANA).get(f"/sessions/{session}/exhibits/{xid}/diff?from=2&to=3").json()
    assert [change["path"] for change in diff["changes"]] == ["rows[0].y"]

    elsewhere = content_address("another conversation's bytes")
    asyncio.run(
        store_tool_result(
            session_id=uuid4().hex,
            correlation_id="c",
            tool="t",
            text="another conversation's bytes",
        )
    )
    invented = _as(app, _ANA).post(
        revisions, json={"parent_revision": 3, "spec": _bound_table(elsewhere)}
    )
    assert invented.status_code == 422 and "not a tool result of this conversation" in (
        invented.text
    )


def test_a_persons_write_records_the_figures_it_introduced(app: Any) -> None:
    """A create records its figures; a revision records only what it added over the one edited.

    What an agent revision's grounding check then reads (`ExhibitStore.chemist_figures`) instead
    of re-parsing every person's revision.
    """
    session = _shared(app)
    made = _create(app, _ANA, session, {"kind": "table", "title": "S", "spec": _TABLE})
    xid = made.json()["exhibit_id"]
    revised = {**_TABLE, "rows": [*_TABLE["rows"], {"solvent": "THF", "y": 81.5}]}
    answer = _as(app, _BEN).post(
        f"/sessions/{session}/exhibits/{xid}/revisions",
        json={"parent_revision": 1, "spec": revised, "change_note": "added THF"},
    )
    assert answer.status_code == 201, answer.text
    # The store the fixture swapped in, read back through the seam it patched.
    store = getattr(routes, "default_exhibit_store")()  # noqa: B009
    assert asyncio.run(store.chemist_figures(session, xid)) == ["1", "76", "81.5"]
