"""The `geometry` artefact kind and the calc-artifact download behind its `source`.

What is pinned: an XYZ block is held to the layout every program writes (count, comment, one
`El x y z` per atom, known elements, finite coordinates) and to `exhibit_max_atoms` on a write; a
`source` must name a stored calculation artifact when it is written, by the agent and over REST,
and an evicted one reads as a missing file rather than a broken artefact; the spec is served with
absent optionals absent; the export is the XYZ text either way; the diff names whole fields; and
`GET /calc-artifacts/content` serves the stored bytes under the stored type to any signed-in caller,
404 for nothing there and 413 above the cap, judged before the read. The Postgres arm proves the
widened kind constraint (`116_exhibit_geometry_kind.sql`) admits the kind.
"""

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import exhibit_tools
from chemclaw.agent.exhibit_tools import create_exhibit
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import exhibits as routes
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.exhibits import sources
from chemclaw.exhibits.diff import diff_specs
from chemclaw.exhibits.export import render_export, resolve_export
from chemclaw.exhibits.grounding import stated_figures
from chemclaw.exhibits.models import (
    GeometrySpec,
    InvalidExhibit,
    parse_spec,
    require_writable,
    spec_json,
)
from chemclaw.exhibits.store import InMemoryExhibitStore, PostgresExhibitStore
from chemclaw.science.calc.artifacts import InMemoryArtifactStore
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.structures import InMemoryStructureStore
from tests.pg import migrated_db_or_skip
from tests.test_service import _FakeOwnerStore, _no_connectors

_WATER = "3\nwater, GFN2-xTB\nO 0.000 0.000 0.117\nH 0.000 0.757 -0.467\nH 0.000 -0.757 -0.467\n"
_CALC_KEY = "xtb.opt@1:abc:def"
_ANA = Principal(oid="ana-geometry", upn="ana@corp", roles=frozenset({"process-chemist"}))


def _geometry(**fields: Any) -> dict[str, Any]:
    return {"kind": "geometry", **fields}


@pytest.fixture
def calc_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryArtifactStore:
    """A calc artifact store holding one optimised water, swapped in where the artefacts read."""
    store = InMemoryArtifactStore()
    monkeypatch.setattr(sources, "default_artifact_store", lambda: store)
    return store


async def _stored_water(store: InMemoryArtifactStore) -> None:
    ref = await store.put(_CALC_KEY, "xtbopt.xyz", _WATER.encode(), media_type="chemical/x-xyz")
    assert ref is not None


@pytest.mark.parametrize(
    ("xyz", "problem"),
    [
        ("2\nc\nO 0 0 0\n", "count line says 2"),
        ("1\nc\nO 0 0 0\nH 0 0 1\n", "count line says 1"),
        ("x\nc\nO 0 0 0\n", "atom count"),
        ("0\nc\n", "at least 1"),
        ("1\nc\nXx 0 0 0\n", "not an element"),
        ("1\nc\nO 0 0 nan\n", "not finite"),
        ("1\nc\nO 0 0 inf\n", "not finite"),
        ("1\nc\nO 0 zero 0\n", "not a number"),
        ("1\nc\nO 0 0\n", "expected `El x y z`"),
        ("1\nc\nO 0 0 0\n1\nc\nO 0 0 1\n", "count line says 1"),
    ],
)
def test_an_xyz_block_that_is_not_one_structure_is_refused_naming_the_line(
    xyz: str, problem: str
) -> None:
    """Each malformation is refused on parse, by the same worded error a writer corrects from."""
    with pytest.raises(InvalidExhibit, match=problem):
        parse_spec(_geometry(xyz=xyz))


def test_a_geometry_takes_exactly_one_structure_and_highlights_inside_it() -> None:
    """Neither or both of `xyz`/`source` is refused; a 0-based highlight must name an atom."""
    with pytest.raises(InvalidExhibit, match="exactly one"):
        parse_spec(_geometry())
    with pytest.raises(InvalidExhibit, match="exactly one"):
        parse_spec(_geometry(xyz=_WATER, source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}))
    with pytest.raises(InvalidExhibit, match=r"\[3\]"):
        parse_spec(_geometry(xyz=_WATER, highlight_atoms=[0, 3]))
    ok = parse_spec(_geometry(xyz=_WATER, highlight_atoms=[0, 2], label="water"))
    assert isinstance(ok, GeometrySpec) and ok.highlight_atoms == [0, 2]
    assert isinstance(parse_spec(_geometry(xyz="1\n\ncl 0 0 0\n")), GeometrySpec)


def test_an_absent_optional_is_absent_on_the_wire_not_null() -> None:
    """`xyz?`, `source?` and `energy_hartree?` are left out when not given, never sent as `null`."""
    cited = spec_json(parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"})))
    assert cited == {
        "kind": "geometry",
        "format": "xyz",
        "source": {"calc_key": _CALC_KEY, "name": "xtbopt.xyz"},
        "label": "",
        "highlight_atoms": [],
    }
    inline = spec_json(parse_spec(_geometry(xyz=_WATER, energy_hartree=-5.07)))
    assert "source" not in inline and inline["energy_hartree"] == -5.07
    assert parse_spec(inline) == parse_spec(_geometry(xyz=_WATER, energy_hartree=-5.07))


def test_the_atom_cap_binds_a_write_and_not_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Above `exhibit_max_atoms` a write is refused; the stored spec still parses."""
    spec = parse_spec(_geometry(xyz=_WATER))
    require_writable(spec, title="water", change_note="")
    monkeypatch.setattr(settings, "exhibit_max_atoms", 2)
    with pytest.raises(InvalidExhibit, match="3 atoms, over the 2-atom cap"):
        require_writable(spec, title="water", change_note="")
    assert parse_spec(spec_json(spec)) == spec


def test_the_energy_is_a_figure_and_the_coordinates_are_not() -> None:
    """The grounding scan asks after the one value a chemist quotes, not 3N coordinates."""
    assert stated_figures(parse_spec(_geometry(xyz=_WATER, energy_hartree=-5.07))) == ["-5.07"]
    assert stated_figures(parse_spec(_geometry(xyz=_WATER))) == []


async def test_a_source_must_be_stored_when_it_is_written(
    calc_store: InMemoryArtifactStore,
) -> None:
    """Nothing stored is refused naming the reference; once stored, the same spec passes."""
    spec = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}))
    with pytest.raises(InvalidExhibit, match=f"{_CALC_KEY}#xtbopt.xyz"):
        await sources.require_source_stored(spec, "s")
    await _stored_water(calc_store)
    await sources.require_source_stored(spec, "s")
    await sources.require_source_stored(parse_spec(_geometry(xyz=_WATER)), "s")


async def test_a_source_must_be_a_geometry_not_any_stored_artifact(
    calc_store: InMemoryArtifactStore,
) -> None:
    """A Hessian is stored too; citing it as a geometry is refused naming what it is."""
    hessian = await calc_store.put(
        _CALC_KEY, "hessian", b"$hessian\n0.1 0.2\n", media_type="application/x-turbomole-hessian"
    )
    assert hessian is not None
    spec = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "hessian"}))
    with pytest.raises(InvalidExhibit, match="application/x-turbomole-hessian artifact, not a"):
        await sources.require_source_stored(spec, "s")


def test_a_reference_splits_at_its_first_hash_whatever_the_key_holds() -> None:
    """Keys carry `/`, `+`, `@` and `:` (the amended contract); only a `#` ends one."""
    from chemclaw.science.calc.artifacts import split_ref

    key = "xtb.opt@1:ab/c+d=:e"
    assert split_ref(f"{key}#xtbopt.xyz") == (key, "xtbopt.xyz")
    assert split_ref(f"{key}#frame#2") == (key, "frame#2")
    assert split_ref("no-separator") is None
    assert split_ref(f"{key}#") is None
    assert split_ref("#name") is None


async def test_the_export_is_the_xyz_text_and_an_evicted_source_has_none(
    calc_store: InMemoryArtifactStore,
) -> None:
    """Inline or cited, `xyz` is the block; a source gone from the store exports nothing."""
    inline = parse_spec(_geometry(xyz=_WATER.rstrip("\n")))
    assert render_export(inline, "xyz") == _WATER
    assert render_export(inline, "md") is None
    cited = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}))
    assert render_export(cited, "xyz") is None
    assert await resolve_export(cited, "xyz") is None
    await _stored_water(calc_store)
    assert await resolve_export(cited, "xyz") == _WATER


def test_a_geometry_diffs_by_whole_field() -> None:
    """`xyz`, `source` and `label` are named bare and compared whole."""
    before = parse_spec(_geometry(xyz=_WATER, label="a"))
    moved = _WATER.replace("0.117", "0.120")
    after = parse_spec(_geometry(xyz=moved, label="b"))
    diff = diff_specs(before, after, from_revision=1, to_revision=2)
    assert [(c.path, c.kind, c.after) for c in diff.changes] == [
        ("xyz", "changed", moved),
        ("label", "changed", "b"),
    ]
    cited = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}))
    swap = diff_specs(before, cited, from_revision=1, to_revision=2)
    assert [(c.path, c.kind) for c in swap.changes] == [
        ("xyz", "removed"),
        ("source", "added"),
        ("label", "changed"),
    ]


@pytest.fixture
def turn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A turn's ambient — a session and an actor — with announcements discarded."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(exhibit_tools, "record_exhibit", lambda signal: None)
    session = uuid4().hex
    session_token = set_current_session_id(session)
    identity = set_current_identity("oid-ana", frozenset())
    try:
        yield session
    finally:
        reset_current_identity(identity)
        reset_current_session_id(session_token)


async def test_the_agent_cannot_cite_a_calculation_artifact_it_guessed(
    turn: str, calc_store: InMemoryArtifactStore
) -> None:
    """`create_exhibit` refuses an unstored source and stores a stored one."""
    spec = _geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}, label="water")
    with pytest.raises(ChemclawError, match="not a stored calculation artifact"):
        await create_exhibit("Water", spec)
    await _stored_water(calc_store)
    answer = json.loads(await create_exhibit("Water", spec))
    assert answer["revision"] == 1


@pytest.fixture(params=["memory", "postgres"])
def app(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, calc_store: Any
) -> Iterator[Any]:
    """The production app on the parametrized artefact store, signed in as Ana."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    if request.param == "postgres":
        import asyncio

        asyncio.run(migrated_db_or_skip())
        store: Any = PostgresExhibitStore()
    else:
        store = InMemoryExhibitStore()
    monkeypatch.setattr(routes, "default_exhibit_store", lambda: store)
    built = create_app(owner_store=_FakeOwnerStore(), connector_factory=_no_connectors)
    built.dependency_overrides[require_principal] = lambda: _ANA
    yield built


async def test_a_geometry_is_created_over_rest_and_exported_as_xyz(
    app: Any, calc_store: InMemoryArtifactStore
) -> None:
    """A cited geometry is 422 until stored, then 201; its export is the stored block."""
    client = TestClient(app)
    session = client.post("/sessions").json()["session_id"]
    body = {
        "kind": "geometry",
        "title": "Optimised water",
        "spec": _geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}),
    }
    refused = client.post(f"/sessions/{session}/exhibits", json=body)
    assert refused.status_code == 422 and "not a stored calculation artifact" in refused.text
    await _stored_water(calc_store)
    made = client.post(f"/sessions/{session}/exhibits", json=body)
    assert made.status_code == 201, made.text
    assert made.json()["spec"] == spec_json(parse_spec(body["spec"]))
    xid = made.json()["exhibit_id"]
    exported = client.get(f"/sessions/{session}/exhibits/{xid}/export.xyz")
    assert exported.status_code == 200 and exported.text == _WATER
    assert exported.headers["content-type"].startswith("chemical/x-xyz")
    assert "attachment" in exported.headers["content-disposition"]
    assert client.get(f"/sessions/{session}/exhibits/{xid}/export.md").status_code == 404


async def test_the_calc_artifact_download_serves_the_stored_bytes(
    app: Any, calc_store: InMemoryArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored type, a sanitised attachment name, 404 for nothing there, 413 before the read."""
    client = TestClient(app)
    await _stored_water(calc_store)
    hostile = await calc_store.put(_CALC_KEY, 'a"b\r\nc.xyz', b"1\n\nH 0 0 0\n")
    assert hostile is not None

    served = client.get("/calc-artifacts/content", params={"ref": f"{_CALC_KEY}#xtbopt.xyz"})
    assert served.status_code == 200 and served.content == _WATER.encode()
    assert served.headers["content-type"] == "chemical/x-xyz"
    assert served.headers["content-disposition"] == 'attachment; filename="xtbopt.xyz"'
    named = client.get("/calc-artifacts/content", params={"ref": hostile.as_str()})
    assert named.headers["content-disposition"] == 'attachment; filename="a-b-c.xyz"'

    for ref in (f"{_CALC_KEY}#hessian", "no-separator", f"{_CALC_KEY}#", "#xtbopt.xyz"):
        assert client.get("/calc-artifacts/content", params={"ref": ref}).status_code == 404, ref

    reads: list[str] = []
    monkeypatch.setattr(calc_store, "open", lambda digest: reads.append(digest))
    monkeypatch.setattr(settings, "calc_artifact_max_download_bytes", len(_WATER) - 1)
    over = client.get("/calc-artifacts/content", params={"ref": f"{_CALC_KEY}#xtbopt.xyz"})
    assert over.status_code == 413 and reads == []


def test_the_published_schema_shows_the_geometry_fields(app: Any) -> None:
    """The OpenAPI document `Chemclaw3_ui` generates from names every field of the spec.

    A wrapping serializer published `GeometrySpec` as `{"additionalProperties": true}`, so a client
    generated from the document had no fields to type; absent optionals are excluded per field.
    """
    schema = app.openapi()["components"]["schemas"]["GeometrySpec"]
    fields = {"kind", "format", "xyz", "source", "structure_id", "label", "energy_hartree"}
    assert fields | {"highlight_atoms"} <= set(schema["properties"])
    assert "kind" in schema["required"]


# --- `structure_id` (the hardening contract, item 1) and the calc download cap ------------------

_WATER_STRUCTURE = Structure(
    elements=[8, 1, 1],
    positions=[[0.0, 0.0, 0.117], [0.0, 0.757, -0.467], [0.0, -0.757, -0.467]],
)
_ENSEMBLE = _WATER + _WATER


@pytest.fixture
def structures(monkeypatch: pytest.MonkeyPatch) -> InMemoryStructureStore:
    """A structure store holding one water, swapped in where the artefacts read."""
    store = InMemoryStructureStore()
    monkeypatch.setattr(sources, "default_structure_store", lambda: store)
    return store


def test_a_geometry_takes_exactly_one_of_three_structures() -> None:
    """`structure_id` is a third way to name the structure, and still exactly one is taken."""
    sid = _WATER_STRUCTURE.structure_id
    cited = parse_spec(_geometry(structure_id=sid))
    assert isinstance(cited, GeometrySpec) and spec_json(cited)["structure_id"] == sid
    assert "xyz" not in spec_json(cited)
    with pytest.raises(InvalidExhibit, match="exactly one"):
        parse_spec(_geometry(structure_id=sid, xyz=_WATER))
    with pytest.raises(InvalidExhibit, match="structure_id"):
        parse_spec(_geometry(structure_id="water"))


async def test_a_structure_id_must_be_stored_one_frame_and_inside_its_highlights(
    structures: InMemoryStructureStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guessed id is refused naming it; a stored one passes; the atom and highlight caps hold."""
    sid = _WATER_STRUCTURE.structure_id
    with pytest.raises(InvalidExhibit, match=f"{sid}.*no stored structure"):
        await sources.require_source_stored(parse_spec(_geometry(structure_id=sid)), "s")
    await structures.put([_WATER_STRUCTURE])
    await sources.require_source_stored(parse_spec(_geometry(structure_id=sid)), "s")
    with pytest.raises(InvalidExhibit, match=r"highlight_atoms \[3\]"):
        await sources.require_source_stored(
            parse_spec(_geometry(structure_id=sid, highlight_atoms=[3])), "s"
        )
    monkeypatch.setattr(settings, "exhibit_max_atoms", 2)
    with pytest.raises(InvalidExhibit, match="3 atoms, over the 2-atom cap"):
        await sources.require_source_stored(parse_spec(_geometry(structure_id=sid)), "s")


async def test_a_cited_structure_is_served_as_xyz_and_a_vanished_one_says_so(
    structures: InMemoryStructureStore,
) -> None:
    """`spec.xyz` resolved and `raw_spec.structure_id` kept; gone, no `xyz` and an `ok: false`."""
    from chemclaw.exhibits.bindings import resolved_view

    sid = _WATER_STRUCTURE.structure_id
    store = InMemoryExhibitStore()
    session = uuid4().hex
    view = await store.create(
        session,
        title="water",
        spec=parse_spec(_geometry(structure_id=sid)),
        author_kind="agent",
        author="a",
    )
    gone = await resolved_view(view)
    assert spec_json(gone.spec) == spec_json(gone.raw_spec)
    assert [b.model_dump() for b in gone.bindings] == [
        {
            "path": "xyz",
            "result_ref": "",
            "tool": "structure",
            "pointer": sid,
            "ok": False,
            "error": "no structure is stored under this id any more",
        }
    ]
    assert await resolve_export(gone.spec, "xyz") is None

    await structures.put([_WATER_STRUCTURE])
    shown = await resolved_view(view)
    assert isinstance(shown.spec, GeometrySpec) and shown.bindings == []
    assert "structure_id" not in spec_json(shown.spec)
    assert spec_json(shown.raw_spec)["structure_id"] == sid
    xyz = shown.spec.xyz or ""
    assert xyz.splitlines()[0] == "3" and xyz.splitlines()[2] == "O 0.0000 0.0000 0.1170"
    assert parse_spec(spec_json(shown.spec)) == shown.spec, "the served spec is a valid one"
    assert await resolve_export(shown.spec, "xyz") == xyz


async def test_the_agent_cites_a_structure_and_reads_back_its_address(
    turn: str, structures: InMemoryStructureStore
) -> None:
    """`create_exhibit` takes a `structure_id`; `read_exhibit` shows the id, not 3N coordinates."""
    from chemclaw.agent.exhibit_tools import read_exhibit

    sid = _WATER_STRUCTURE.structure_id
    with pytest.raises(ChemclawError, match="no stored structure"):
        await create_exhibit("Water", _geometry(structure_id=sid))
    await structures.put([_WATER_STRUCTURE])
    xid = json.loads(await create_exhibit("Water", _geometry(structure_id=sid)))["exhibit_id"]
    readout = json.loads(await read_exhibit(xid))
    assert readout["spec"]["structure_id"] == sid and "xyz" not in readout["spec"]
    assert "structure_id" in (create_exhibit.__doc__ or "")
    assert "calc_key" not in (create_exhibit.__doc__ or ""), "source is no longer advertised"


async def test_a_cited_ensemble_or_an_artifact_over_the_download_cap_is_refused(
    calc_store: InMemoryArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conformer ensemble is one stored XYZ file of many frames; a geometry shows one."""
    await calc_store.put(
        _CALC_KEY, "crest_conformers.xyz", _ENSEMBLE.encode(), media_type="chemical/x-xyz"
    )
    ensemble = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "crest_conformers.xyz"}))
    with pytest.raises(InvalidExhibit, match="not one XYZ structure"):
        await sources.require_source_stored(ensemble, "s")

    await _stored_water(calc_store)
    cited = parse_spec(_geometry(source={"calc_key": _CALC_KEY, "name": "xtbopt.xyz"}))
    reads: list[str] = []
    real_open = calc_store.open

    async def _open(digest: str) -> bytes | None:
        reads.append(digest)
        return await real_open(digest)

    monkeypatch.setattr(calc_store, "open", _open)
    monkeypatch.setattr(settings, "calc_artifact_max_download_bytes", len(_WATER) - 1)
    with pytest.raises(InvalidExhibit, match="download cap"):
        await sources.require_source_stored(cited, "s")
    assert await resolve_export(cited, "xyz") is None
    assert reads == [], "the cap is decided from the recorded size, before the blob is opened"


async def test_a_structure_cited_must_be_one_this_conversation_was_shown(
    turn: str, structures: InMemoryStructureStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session scope: an id a calculation here reported is citable; one from elsewhere is not.

    `D-2026-10-03-a-cited-structure-is-one-this-conversation-was-shown`. A mention in the agent's
    own text handed back (a helper's report) is not a report; a revision carrying the cited id
    forward is accepted without asking again.
    """
    from chemclaw.agent.exhibit_tools import revise_exhibit
    from chemclaw.api.tool_results import store_tool_result

    await migrated_db_or_skip()
    monkeypatch.setattr(settings, "session_store", "postgres")
    store = InMemoryExhibitStore()
    monkeypatch.setattr(exhibit_tools, "_store", lambda: store)
    sid = _WATER_STRUCTURE.structure_id
    await structures.put([_WATER_STRUCTURE])
    spec = _geometry(structure_id=sid)
    with pytest.raises(ChemclawError, match="not one a tool result of this conversation reported"):
        await create_exhibit("Water", spec)
    await store_tool_result(
        session_id=turn, correlation_id="c", tool="task", text=f'{{"seen": "{sid}"}}'
    )
    with pytest.raises(ChemclawError, match="not one a tool result of this conversation reported"):
        await create_exhibit("Water", spec)
    report = json.dumps({"structure_id": sid, "energy_hartree": -5.07})
    elsewhere = uuid4().hex
    await store_tool_result(
        session_id=elsewhere, correlation_id="c", tool="optimize_geometry", text=report
    )
    with pytest.raises(ChemclawError, match="not one a tool result of this conversation reported"):
        await create_exhibit("Water", spec)
    await store_tool_result(
        session_id=turn, correlation_id="c", tool="optimize_geometry", text=report
    )
    xid = json.loads(await create_exhibit("Water", spec))["exhibit_id"]
    relabelled = {**spec, "label": "water, GFN2-xTB"}
    assert json.loads(await revise_exhibit(xid, 1, "label", spec=relabelled))["revision"] == 2
