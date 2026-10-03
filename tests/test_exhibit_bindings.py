"""Bindings: a value an artefact takes verbatim from a stored tool result, with its provenance.

`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`. The pure half — the RFC 6901
pointer and the shape a `$bind` may take where — runs anywhere; everything that reads the session's
stored results runs against a real database (`tests/pg.py`), because the session-scoped link join
is the authorization and a double would only prove the double consistent with itself.
"""

import asyncio
import hashlib
import itertools
import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from chemclaw.agent import exhibit_tools
from chemclaw.agent.exhibit_tools import create_exhibit, read_exhibit, revise_exhibit
from chemclaw.api.tool_results import content_address, store_tool_result
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.exhibits import bindings
from chemclaw.exhibits.bindings import bind_for_write, pointer_get, resolved_view
from chemclaw.exhibits.diff import diff_specs
from chemclaw.exhibits.export import render_export
from chemclaw.exhibits.grounding import stated_figures, unverified_figures
from chemclaw.exhibits.models import (
    InvalidExhibit,
    TableSpec,
    parse_spec,
    require_writable,
    spec_json,
)
from chemclaw.exhibits.store import default_exhibit_store
from tests.pg import migrated_db_or_skip

_RESULT = {
    "query": "solvents",
    "rows": [
        {"name": "THF", "yield": 76.5, "smiles": "C1CCOC1"},
        {"name": "2-MeTHF", "yield": 81, "smiles": "CC1CCCO1"},
        {"name": "DMF", "smiles": "CN(C)C=O"},
    ],
    "a/b": {"m~n": 4.76},
    "temps": [20, 40, 60],
    "yields": [55.0, 71.5, 80.25],
    "labels": ["a", "b", "c"],
}
_TEXT = json.dumps(_RESULT)
_REF = content_address(_TEXT)
_HANDLE = f"r:{_REF[:12]}"
_COLUMNS = [{"key": "solvent", "label": "Solvent"}, {"key": "y", "label": "Yield", "unit": "%"}]


def _bind(pointer: str, result: str = _HANDLE) -> dict[str, Any]:
    """A `$bind` value as a writer sends it."""
    return {"$bind": {"result": result, "pointer": pointer}}


# --- the pure half --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "value"),
    [
        ("", _RESULT),
        ("/query", "solvents"),
        ("/rows/1/yield", 81),
        ("/a~1b/m~0n", 4.76),
        ("/temps", [20, 40, 60]),
    ],
)
def test_a_pointer_reaches_what_rfc_6901_says_it_does(pointer: str, value: Any) -> None:
    """The empty pointer is the document, `~1` is `/`, `~0` is `~`, an index is an element."""
    assert pointer_get(_RESULT, pointer) == value


@pytest.mark.parametrize(
    ("pointer", "why"),
    [
        ("/rows/01", "not an array index"),
        ("/rows/²", "not an array index"),
        ("/rows/١", "not an array index"),
        ("/rows/-", "not an array index"),
        ("/rows/3", "past the end"),
        ("/missing", "no key"),
        ("/query/0", "indexes into a str"),
        ("query", "must start with"),
    ],
)
def test_a_pointer_that_does_not_resolve_says_where(pointer: str, why: str) -> None:
    """A leading zero and `-` name no element to read; a scalar has nothing under it."""
    with pytest.raises(KeyError, match=why):
        pointer_get(_RESULT, pointer)


def test_a_binding_is_accepted_exactly_where_a_literal_value_is() -> None:
    """Cells, properties, SMILES, series and `rows_from` take one; a label or a document not."""
    table = parse_spec({"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/a")}]})
    assert spec_json(table)["rows"][0]["y"] == _bind("/a"), "a binding does not round-trip"
    parse_spec(
        {"kind": "structures", "items": [{"smiles": _bind("/s"), "props": {"p": _bind("")}}]}
    )
    parse_spec(
        {
            "kind": "chart",
            "chart": "line",
            "x_label": "T",
            "y_label": "Y",
            "series": [{"name": "s", "x": _bind("/temps"), "y": _bind("/yields")}],
        }
    )
    for refused in (
        {"kind": "document", "markdown": _bind("/q")},
        {"kind": "table", "columns": [{"key": "y", "label": _bind("/q")}], "rows": []},
        {"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/a", "r:abc")}]},
        {"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/a~2")}]},
        {"kind": "table", "columns": _COLUMNS, "rows": [{"y": 1}], "rows_from": _rows_from()},
        {"kind": "table", "columns": _COLUMNS, "rows_from": _rows_from({"z": "/name"})},
    ):
        with pytest.raises(InvalidExhibit):
            parse_spec(refused)


def _rows_from(columns: dict[str, str] | None = None, pointer: str = "/rows") -> dict[str, Any]:
    """A table-wide binding over the fixture's records."""
    return {
        "result": _HANDLE,
        "pointer": pointer,
        "columns": columns or {"solvent": "/name", "y": "/yield"},
    }


def test_a_literal_null_is_refused_where_only_a_vanished_binding_may_leave_one() -> None:
    """The positions a binding occupies read `null` when its result is gone; a writer may not."""
    for raw in (
        {"kind": "structures", "items": [{"smiles": "CCO", "props": {"p": None}}]},
        {"kind": "structures", "items": [{"smiles": None}]},
        {
            "kind": "chart",
            "chart": "line",
            "x_label": "a",
            "y_label": "b",
            "series": [{"name": "s", "x": [1], "y": None}],
        },
    ):
        with pytest.raises(InvalidExhibit, match="not null"):
            require_writable(parse_spec(raw), title="t", change_note="")
    # A table cell has always taken null, and still does.
    require_writable(
        parse_spec({"kind": "table", "columns": _COLUMNS, "rows": [{"y": None}]}),
        title="t",
        change_note="",
    )


def test_a_bound_value_is_never_an_unchecked_figure_and_a_literal_still_is() -> None:
    """The scan reads the stored spec: a binding states no figure of its own."""
    spec = parse_spec(
        {"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/a")}, {"y": 99.9}]}
    )
    assert stated_figures(spec) == ["99.9"]


def test_the_diff_compares_bindings_not_what_they_resolve_to() -> None:
    """Detaching a cell is a change at its path; the same binding on both sides is none."""
    bound = parse_spec({"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/a")}]})
    detached = parse_spec({"kind": "table", "columns": _COLUMNS, "rows": [{"y": 76.5}]})
    assert diff_specs(bound, bound, from_revision=1, to_revision=2).changes == []
    [change] = diff_specs(bound, detached, from_revision=1, to_revision=2).changes
    assert change.path == "rows[0].y" and json.loads(change.before) == _bind("/a")
    table_wide = parse_spec({"kind": "table", "columns": _COLUMNS, "rows_from": _rows_from()})
    changes = diff_specs(table_wide, detached, from_revision=1, to_revision=2).changes
    assert [c.path for c in changes] == ["rows_from", "rows[0]"]


# --- against the session's stored results --------------------------------------------------------


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A session holding `_TEXT` as a stored tool result, on a deployment that keeps them."""
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "session_store", "postgres")
    session = uuid4().hex
    asyncio.run(
        store_tool_result(session_id=session, correlation_id="c", tool="screen", text=_TEXT)
    )
    yield session


def _table(rows: list[dict[str, Any]]) -> Any:
    """A table spec over the fixture's two columns."""
    return parse_spec({"kind": "table", "columns": _COLUMNS, "rows": rows})


def test_a_handle_resolves_to_the_value_and_is_stored_as_the_full_ref(stored: str) -> None:
    """Stored keeps the binding with all 64 digits; shown is the value; the binding is listed."""
    spec = _table([{"solvent": _bind("/rows/0/name"), "y": _bind("/rows/0/yield")}])

    bound = asyncio.run(bind_for_write(stored, spec))

    assert spec_json(bound.stored)["rows"][0]["y"] == _bind("/rows/0/yield", _REF)
    assert isinstance(bound.resolved, TableSpec)
    assert bound.resolved.rows == [{"solvent": "THF", "y": 76.5}]
    assert [(b.path, b.result_ref, b.tool, b.ok) for b in bound.bindings] == [
        ("rows[0].solvent", _REF, "screen", True),
        ("rows[0].y", _REF, "screen", True),
    ]
    assert asyncio.run(unverified_figures(stored, bound.stored)) == []


def test_rows_from_binds_a_whole_table_with_an_empty_cell_for_a_missing_field(
    stored: str,
) -> None:
    """One entry for the table; an element lacking the field is an empty cell, not a refusal."""
    spec = parse_spec({"kind": "table", "columns": _COLUMNS, "rows_from": _rows_from()})
    bound = asyncio.run(bind_for_write(stored, spec))
    assert isinstance(bound.resolved, TableSpec) and bound.resolved.rows_from is None
    assert [row["y"] for row in bound.resolved.rows] == [76.5, 81, None]
    assert [b.path for b in bound.bindings] == ["rows_from"]
    assert render_export(bound.resolved, "csv") == (
        "Solvent,Yield (%)\r\nTHF,76.5\r\n2-MeTHF,81\r\nDMF,\r\n"
    )


def test_series_and_structures_bind_arrays_and_smiles(stored: str) -> None:
    """A chart axis binds an array of the right type; a SMILES binds text RDKit then reads."""
    chart = parse_spec(
        {
            "kind": "chart",
            "chart": "line",
            "x_label": "T (°C)",
            "y_label": "Yield (%)",
            "series": [{"name": "run", "x": _bind("/temps"), "y": _bind("/yields")}],
        }
    )
    bound = asyncio.run(bind_for_write(stored, chart))
    assert spec_json(bound.resolved)["series"][0] == {
        "name": "run",
        "x": [20, 40, 60],
        "y": [55.0, 71.5, 80.25],
    }
    panel = parse_spec(
        {"kind": "structures", "items": [{"smiles": _bind("/rows/0/smiles"), "props": {}}]}
    )
    resolved = asyncio.run(bind_for_write(stored, panel)).resolved
    require_writable(resolved, title="t", change_note="")


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        (_table([{"y": _bind("/nowhere")}]), "does not resolve"),
        (_table([{"y": _bind("/temps")}]), "is a array, and a table cell"),
        (_table([{"y": _bind("/a", "r:" + "0" * 12)}]), "not a tool result of this conversation"),
        (
            parse_spec(
                {
                    "kind": "chart",
                    "chart": "line",
                    "x_label": "a",
                    "y_label": "b",
                    "series": [{"name": "s", "x": _bind("/temps"), "y": _bind("/labels")}],
                }
            ),
            "element 0 is a string, not a number",
        ),
    ],
)
def test_a_binding_that_does_not_fit_is_refused_naming_its_path(
    stored: str, spec: Any, why: str
) -> None:
    """Every refusal names the spec path and the handle, so one retry can fix it."""
    with pytest.raises(InvalidExhibit, match=why):
        asyncio.run(bind_for_write(stored, spec))


def test_another_sessions_result_is_not_bindable(stored: str) -> None:
    """The link is the authorization: bytes another conversation produced resolve to nothing."""
    elsewhere = uuid4().hex
    with pytest.raises(InvalidExhibit, match="not a tool result of this conversation"):
        asyncio.run(bind_for_write(elsewhere, _table([{"y": _bind("/rows/0/yield", _REF)}])))


def test_an_ambiguous_prefix_is_refused_by_name(stored: str) -> None:
    """Two results sharing a prefix: the shorter handle is refused, a longer one resolves."""
    seen: dict[str, str] = {}
    for counter in itertools.count():
        text = json.dumps({"n": counter})
        prefix = hashlib.sha256(text.encode()).hexdigest()[:8]
        if prefix in seen:
            first, second = seen[prefix], text
            break
        seen[prefix] = text
    for text in (first, second):
        asyncio.run(store_tool_result(session_id=stored, correlation_id="c", tool="n", text=text))
    with pytest.raises(InvalidExhibit, match="matches 2 tool results"):
        asyncio.run(bind_for_write(stored, _table([{"y": _bind("/n", f"r:{prefix}")}])))
    full = content_address(second)
    bound = asyncio.run(bind_for_write(stored, _table([{"y": _bind("/n", f"r:{full[:12]}")}])))
    assert bound.bindings[0].result_ref == full


def test_the_caps_hold_on_what_a_binding_resolves_to(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rows_from` past the row cap is refused, and so is binding into too many results."""
    monkeypatch.setattr(settings, "exhibit_max_rows", 2)
    spec = parse_spec({"kind": "table", "columns": _COLUMNS, "rows_from": _rows_from()})
    with pytest.raises(InvalidExhibit, match="2-row cap"):
        bound = asyncio.run(bind_for_write(stored, spec))
        require_writable(bound.resolved, title="t", change_note="", stored=bound.stored)
    other = json.dumps({"v": 1})
    asyncio.run(store_tool_result(session_id=stored, correlation_id="c", tool="t", text=other))
    monkeypatch.setattr(settings, "exhibit_max_bound_results", 1)
    two = _table([{"y": _bind("/a~1b/m~0n")}, {"y": _bind("/v", f"r:{content_address(other)}")}])
    with pytest.raises(InvalidExhibit, match="1-result cap"):
        asyncio.run(bind_for_write(stored, two))


def test_a_large_result_is_parsed_off_the_event_loop(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the configured size the parse and the pointer walk run in a worker thread."""
    monkeypatch.setattr(settings, "exhibit_binding_offload_bytes", 0)
    offloaded: list[Any] = []
    real = asyncio.to_thread

    async def _spy(function: Any, *args: Any) -> Any:
        offloaded.append(function)
        return await real(function, *args)

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    asyncio.run(bind_for_write(stored, _table([{"y": _bind("/rows/0/yield")}])))
    assert any(getattr(f, "__module__", "") == bindings.__name__ for f in offloaded), (
        "the result was parsed on the event loop"
    )


def test_with_no_result_store_a_binding_is_refused_and_a_literal_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing kept, nothing to bind to: the writer is told to write the value."""
    monkeypatch.setattr(settings, "session_store", "memory")
    with pytest.raises(InvalidExhibit, match="keeps no tool results"):
        asyncio.run(bind_for_write(uuid4().hex, _table([{"y": _bind("/a")}])))
    literal = _table([{"y": 1}])
    assert asyncio.run(bind_for_write(uuid4().hex, literal)).stored is literal


def _sweepable(session: str) -> tuple[str, str]:
    """A result only this test holds — `(text, ref)` — so sweeping it touches no other test's."""
    text = json.dumps({**_RESULT, "nonce": uuid4().hex})
    asyncio.run(store_tool_result(session_id=session, correlation_id="c", tool="screen", text=text))
    return text, content_address(text)


def _sweep(ref: str) -> None:
    """What retention does to a result: the blob goes, and its links with it (042's cascade)."""

    async def _delete() -> None:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM tool_result_blobs WHERE content_hash = %s", (ref,))
            await conn.commit()

    asyncio.run(_delete())


def test_a_swept_result_reads_null_with_a_reason_and_the_rest_still_reads(stored: str) -> None:
    """Retention takes the blob; the cell is `null`, `ok` false, the table still opens.

    Read once before the sweep, so the parsed document is in the cache: a cache hit is never the
    authorization, and the link the sweep removed is what decides.
    """
    _, ref = _sweepable(stored)
    spec = _table([{"solvent": "THF", "y": _bind("/rows/0/yield", f"r:{ref[:12]}")}])
    bound = asyncio.run(bind_for_write(stored, spec))
    view = asyncio.run(
        default_exhibit_store().create(
            stored, title="Screen", spec=bound.stored, author_kind="human", author="oid-ana"
        )
    )
    assert spec_json(asyncio.run(resolved_view(view)).spec)["rows"][0]["y"] == 76.5
    _sweep(ref)

    shown = asyncio.run(resolved_view(view))
    assert spec_json(shown.spec)["rows"] == [{"solvent": "THF", "y": None}]
    assert spec_json(shown.raw_spec)["rows"][0]["y"] == _bind("/rows/0/yield", ref)
    [binding] = shown.bindings
    assert (binding.ok, binding.result_ref) == (False, ref)
    assert "no longer stored" in binding.error


def test_a_revision_carrying_a_swept_binding_unchanged_is_accepted_and_reads_null(
    stored: str,
) -> None:
    """One expired cell must not block every whole-spec revision.

    A binding copied unchanged from the parent is kept, reading `null` with `ok: false` — even at a
    position (a structure property) a writer may not put a literal `null` in. A *new* binding to the
    swept ref is refused naming that cause, and a ref the session never held is refused as such.
    """
    _, ref = _sweepable(stored)
    handle = f"r:{ref[:12]}"
    panel = parse_spec(
        {
            "kind": "structures",
            "items": [{"smiles": "C1CCOC1", "props": {"y": _bind("/rows/0/yield", handle)}}],
        }
    )
    parent = asyncio.run(bind_for_write(stored, panel)).stored
    _sweep(ref)
    carried = spec_json(parent)
    carried["items"][0]["label"] = "THF, relabelled"

    bound = asyncio.run(bind_for_write(stored, parse_spec(carried), parent=parent))
    require_writable(
        bound.resolved,
        title="t",
        change_note="",
        stored=bound.stored,
        vanished=bound.vanished,
    )
    assert spec_json(bound.resolved)["items"][0]["props"] == {"y": None}
    assert [(b.path, b.ok) for b in bound.bindings] == [("items[0].props.y", False)]
    with pytest.raises(InvalidExhibit, match="not null"):
        require_writable(bound.resolved, title="t", change_note="", stored=bound.stored)

    moved = json.loads(json.dumps(carried))
    moved["items"][0]["props"]["y"] = _bind("/rows/1/yield", ref)
    with pytest.raises(InvalidExhibit, match="no longer stored.*detach the value"):
        asyncio.run(bind_for_write(stored, parse_spec(moved), parent=parent))
    stranger = json.loads(json.dumps(carried))
    stranger["items"][0]["props"]["y"] = _bind("/a", "f" * 64)
    with pytest.raises(InvalidExhibit, match="not a tool result of this conversation"):
        asyncio.run(bind_for_write(stored, parse_spec(stranger), parent=parent))
    # Without the parent the same carried spec is a new binding to a swept ref, and is refused.
    with pytest.raises(InvalidExhibit, match="not a tool result of this conversation"):
        asyncio.run(bind_for_write(stored, parse_spec(carried)))


def test_a_non_ascii_index_is_a_worded_refusal_not_a_crash(stored: str) -> None:
    """`²` is a digit to `str.isdigit` and not to `int`; it is refused like any bad index."""
    with pytest.raises(InvalidExhibit, match="not an array index"):
        asyncio.run(bind_for_write(stored, _table([{"y": _bind("/temps/²")}])))


def test_a_read_asks_only_for_its_own_refs_and_reuses_parsed_documents(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read asks the links for its own refs, and a second read fetches no blob.

    The parsed document is cached by content hash; the link query still runs every time, because
    it — not the cache — is what says the session holds the result.
    """
    _, ref = _sweepable(stored)
    spec = _table([{"y": _bind("/rows/0/yield", f"r:{ref[:12]}")}])
    view = asyncio.run(
        default_exhibit_store().create(
            stored,
            title="S",
            spec=asyncio.run(bind_for_write(stored, spec)).stored,
            author_kind="human",
            author="oid-ana",
        )
    )
    bindings._DOCUMENTS.clear()
    asked: list[Any] = []
    read: list[list[str]] = []
    real_links, real_blobs = bindings._links, bindings._blobs

    async def _links(session_id: str, refs: Any = None) -> Any:
        asked.append(refs)
        return await real_links(session_id, refs)

    async def _blobs(session_id: str, refs: list[str]) -> Any:
        read.append(list(refs))
        return await real_blobs(session_id, refs)

    monkeypatch.setattr(bindings, "_links", _links)
    monkeypatch.setattr(bindings, "_blobs", _blobs)
    for _ in range(2):
        shown = asyncio.run(resolved_view(view))
        assert spec_json(shown.spec)["rows"][0]["y"] == 76.5
    assert asked == [{ref}, {ref}], "a read asked for the whole session's links"
    assert read == [[ref], []], "the second read fetched a blob the cache held"


def test_the_document_cache_holds_its_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Least recently used goes first; a document over the whole budget is not kept at all."""
    bindings._DOCUMENTS.clear()
    monkeypatch.setattr(settings, "exhibit_binding_cache_bytes", 10)
    bindings._remember("a", {"a": 1}, 4)
    bindings._remember("b", {"b": 1}, 4)
    assert bindings._cached("a") is not None  # now the most recently used
    bindings._remember("c", {"c": 1}, 4)
    assert list(bindings._DOCUMENTS) == ["a", "c"]
    bindings._remember("huge", {}, 11)
    assert "huge" not in bindings._DOCUMENTS
    bindings._DOCUMENTS.clear()


def test_the_offload_threshold_counts_stored_bytes_not_characters(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result of multi-byte text is over the threshold in bytes while under it in characters."""
    text = json.dumps({"label": "µ" * 400, "v": 1}, ensure_ascii=False)
    asyncio.run(store_tool_result(session_id=stored, correlation_id="c", tool="t", text=text))
    assert len(text) < 600 < len(text.encode("utf-8"))
    monkeypatch.setattr(settings, "exhibit_binding_offload_bytes", 600)
    bindings._DOCUMENTS.clear()
    offloaded: list[Any] = []
    real = asyncio.to_thread

    async def _spy(function: Any, *args: Any) -> Any:
        offloaded.append(function)
        return await real(function, *args)

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    ref = content_address(text)
    asyncio.run(bind_for_write(stored, _table([{"y": _bind("/v", f"r:{ref[:12]}")}])))
    assert any(getattr(f, "__module__", "") == bindings.__name__ for f in offloaded)


# --- the agent's tools ----------------------------------------------------------------------------


def test_the_agent_binds_through_create_and_reads_both_forms_back(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create_exhibit` stores the binding; `read_exhibit` shows the value and the raw spec."""
    monkeypatch.setattr(exhibit_tools, "record_exhibit", lambda signal: None)
    session_token = set_current_session_id(stored)
    identity = set_current_identity("oid-ana", frozenset())
    try:
        spec = {"kind": "table", "columns": _COLUMNS, "rows": [{"y": _bind("/rows/1/yield")}]}
        xid = json.loads(asyncio.run(create_exhibit("Screen", spec)))["exhibit_id"]
        readout = json.loads(asyncio.run(read_exhibit(xid)))
        assert readout["spec"]["rows"] == [{"y": 81}]
        assert readout["raw_spec"]["rows"][0]["y"] == _bind("/rows/1/yield", _REF)
        assert readout["bindings"][0]["path"] == "rows[0].y"
        assert readout["unchecked_figures"] == []
        with pytest.raises(ChemclawError, match="does not resolve"):
            asyncio.run(
                revise_exhibit(xid, 1, "n", spec={**spec, "rows": [{"y": _bind("/rows/9/yield")}]})
            )
    finally:
        reset_current_identity(identity)
        reset_current_session_id(session_token)
