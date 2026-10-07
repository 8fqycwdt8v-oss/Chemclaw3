"""Resolve an artefact's bound values against the stored tool results of its own session.

A spec may put `{"$bind": {"result": "r:<hex>", "pointer": "/a/0/b"}}` where it takes a cell,
property, SMILES or series, and a table may take `rows_from` instead of `rows`. This module
produces:

- the **stored** spec — bindings kept, each handle expanded to the full 64-hex ref, so a revision
  keeps pointing at the same bytes;
- the **resolved** spec — every binding replaced by its value, for renderers, exports and caps;
- the **bindings** list served beside it, one entry per bound value.

On a write every binding must resolve (the result is linked to this session, its prefix names
exactly one, the pointer reaches a value that fits the position) or the write is refused naming the
problems. On a read only retention can have changed anything: a swept binding reads `null` with `ok:
false` and the rest still reads.

The session's `tool_result_links` are the authorization, so a ref another session produced resolves
to nothing. JSON Pointers are RFC 6901, implemented here (two escapes and an array index) rather
than adding a dependency.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import OrderedDict
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.result_handle import handles_resolve
from chemclaw.exhibits.evidence import evidence_params, evidence_predicate
from chemclaw.exhibits.models import (
    ExhibitBinding,
    ExhibitView,
    GeometrySpec,
    InvalidExhibit,
    Spec,
    parse_spec,
    spec_bytes,
    spec_json,
)
from chemclaw.exhibits.sources import resolved_geometry

#: What each bound position accepts, by the name a refusal uses for it.
Expect = Literal["cell", "prop", "smiles", "x", "y", "rows"]

# Only evidence is bindable (`exhibits.evidence`): a value bound to the agent's own transcription
# handed back would carry a tool result's provenance while being exactly what binding replaces.
# Every query carries this predicate.
_EVIDENCE = evidence_predicate("tool")

_SESSION_LINKS = (
    f"SELECT content_hash, tool FROM tool_result_links WHERE session_id = %s AND {_EVIDENCE}"
)

# A read names exact refs, so it asks only for those — served by the links' primary key
# `(session_id, content_hash)` — rather than for every result the session ever stored.
_LINKS_FOR = (
    "SELECT content_hash, tool FROM tool_result_links "
    f"WHERE session_id = %s AND content_hash = ANY(%s) AND {_EVIDENCE}"
)

_BLOBS = f"""
SELECT l.content_hash, b.data
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s AND l.content_hash = ANY(%s) AND {evidence_predicate("l.tool")}
"""


# Parsed result documents by content hash, most recently used last, each with its stored size.
#
# Safe to share across sessions because a blob is immutable, but a hit is used only for a ref the
# session's own links just named, so the cache never authorizes. Per process, bounded by
# `exhibit_binding_cache_bytes` of stored bytes (the parsed form is several times larger).
_DOCUMENTS: OrderedDict[str, tuple[Any, int]] = OrderedDict()


@dataclass(frozen=True)
class _Site:
    """One bound position in a spec's JSON: where it is, what it reads, what it must be.

    `at` is the position as keys and indices from the root, used to write the value back; `path` is
    the same position spelled as the diff spells it. Both, because a key may contain `.` or `[`.
    """

    path: str
    at: tuple[str | int, ...]
    target: str
    pointer: str
    expect: Expect
    columns: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Bound:
    """A spec as it is stored, as it is shown, and the bindings between the two."""

    stored: Spec
    resolved: Spec
    bindings: list[ExhibitBinding]

    @property
    def vanished(self) -> frozenset[str]:
        """The paths of carried bindings whose result is gone, which read `null` and may."""
        return frozenset(binding.path for binding in self.bindings if not binding.ok)


class _Unresolved(Exception):
    """One binding that did not resolve, worded for the writer."""


def _sites(raw: Mapping[str, Any]) -> Iterator[_Site]:
    """Every bound position in a spec's JSON form, in reading order."""
    kind = raw.get("kind")
    if kind == "table":
        source = raw.get("rows_from")
        if isinstance(source, dict):
            yield _Site(
                "rows_from",
                ("rows_from",),
                source["result"],
                source["pointer"],
                "rows",
                source["columns"],
            )
        for index, row in enumerate(raw.get("rows") or []):
            for key, value in row.items():
                if (bound := _bound(value)) is not None:
                    yield _Site(f"rows[{index}].{key}", ("rows", index, key), *bound, "cell")
    elif kind == "structures":
        for index, item in enumerate(raw.get("items") or []):
            if (bound := _bound(item.get("smiles"))) is not None:
                yield _Site(f"items[{index}].smiles", ("items", index, "smiles"), *bound, "smiles")
            for name, value in (item.get("props") or {}).items():
                if (bound := _bound(value)) is not None:
                    at = ("items", index, "props", name)
                    yield _Site(f"items[{index}].props.{name}", at, *bound, "prop")
    elif kind == "chart":
        for index, series in enumerate(raw.get("series") or []):
            if (bound := _bound(series.get("x"))) is not None:
                yield _Site(f"series[{index}].x", ("series", index, "x"), *bound, "x")
            if (bound := _bound(series.get("y"))) is not None:
                yield _Site(f"series[{index}].y", ("series", index, "y"), *bound, "y")


def _bound(value: object) -> tuple[str, str] | None:
    """`(result, pointer)` when `value` is a `$bind` object, else `None`."""
    if isinstance(value, dict) and isinstance(value.get("$bind"), dict):
        target = value["$bind"]
        return str(target["result"]), str(target["pointer"])
    return None


async def _links(session_id: str, refs: Collection[str] | None = None) -> dict[str, str]:
    """The session's stored results, ref to tool — all of them, or only those among `refs`.

    A write needs all, since a handle is a prefix; a read holds exact refs.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            if refs is None:
                await cur.execute(_SESSION_LINKS, (session_id, *evidence_params()))
            else:
                await cur.execute(_LINKS_FOR, (session_id, list(refs), *evidence_params()))
            rows = await cur.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


async def _blobs(session_id: str, refs: list[str]) -> dict[str, bytes]:
    """The stored bytes of each ref this session still holds; a swept one is simply absent."""
    if not refs:
        return {}
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_BLOBS, (session_id, refs, *evidence_params()))
            rows = await cur.fetchall()
    return {str(row[0]): bytes(row[1]) for row in rows}


def _cached(ref: str) -> tuple[Any, int] | None:
    """A parsed document from the cache, marked as just used — or `None`."""
    hit = _DOCUMENTS.get(ref)
    if hit is not None:
        _DOCUMENTS.move_to_end(ref)
    return hit


def _remember(ref: str, document: Any, size: int) -> None:
    """Keep a parsed document, evicting the least recently used past the byte budget."""
    budget = settings.exhibit_binding_cache_bytes
    if size > budget:
        return
    _DOCUMENTS[ref] = (document, size)
    _DOCUMENTS.move_to_end(ref)
    while sum(held for _, held in _DOCUMENTS.values()) > budget:
        _DOCUMENTS.popitem(last=False)


def _ref_for(target: str, links: Mapping[str, str]) -> str:
    """The one stored ref `target` names in this session.

    Raises:
        _Unresolved: no result of this session has that handle, or more than one does.
    """
    prefix = target.removeprefix("r:")
    found = [ref for ref in links if ref.startswith(prefix)]
    if not found:
        raise _Unresolved(f"{target} is not a tool result of this conversation")
    if len(found) > 1:
        raise _Unresolved(
            f"{target} matches {len(found)} tool results of this conversation; give more of the "
            "hex digits"
        )
    return found[0]


def _refuse_constant(name: str) -> float:
    """Refuse `NaN` and the infinities, which Python's JSON reader accepts and JSON does not."""
    raise ValueError(f"{name} is not a JSON number")


def _parsed(blobs: Mapping[str, bytes]) -> dict[str, Any]:
    """Each stored result as JSON, or the `ValueError` it raised, kept so a binding can say why."""
    parsed: dict[str, Any] = {}
    for ref, data in blobs.items():
        try:
            text = data.decode("utf-8", errors="replace")
            parsed[ref] = json.loads(text, parse_constant=_refuse_constant)
        except (ValueError, RecursionError) as exc:
            parsed[ref] = exc if isinstance(exc, ValueError) else ValueError(str(exc))
    return parsed


def pointer_get(document: Any, pointer: str) -> Any:
    """The value `pointer` (RFC 6901) reaches in `document`.

    Raises:
        KeyError: naming the reference token that does not exist, or that indexes an array
            with something other than an index in range (`-` included: it names no element).
    """
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError(f"{pointer!r} is not a JSON Pointer (it must start with `/`)")
    node = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                raise KeyError(f"no key {token!r}")
            node = node[token]
        elif isinstance(node, list):
            # ASCII digits only: `str.isdigit` also accepts `²` (which `int` then refuses) and
            # `١` (which `int` reads as 1), and an RFC 6901 index is `0` or `[1-9][0-9]*`.
            ascii_index = token.isascii() and token.isdigit()
            if not ascii_index or (token != "0" and token.startswith("0")):
                raise KeyError(f"{token!r} is not an array index")
            if int(token) >= len(node):
                raise KeyError(f"index {token} is past the end of a {len(node)}-element array")
            node = node[int(token)]
        else:
            raise KeyError(f"{token!r} indexes into a {type(node).__name__}, not an object")
    return node


def _is_number(value: object) -> bool:
    """A finite JSON number, never a boolean (`true` is not a yield)."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _fitted(value: Any, site: _Site) -> Any:
    """`value` checked against what `site` accepts, and shaped for it.

    Raises:
        _Unresolved: the value is not the type the position takes.
    """
    expect = site.expect
    if expect == "cell":
        if value is None or isinstance(value, str) or _is_number(value):
            return value
        raise _Unresolved(f"is a {_json_type(value)}, and a table cell takes text or a number")
    if expect == "prop":
        if isinstance(value, str) or _is_number(value):
            return value
        raise _Unresolved(f"is a {_json_type(value)}, and a property takes text or a number")
    if expect == "smiles":
        if isinstance(value, str) and value:
            return value
        raise _Unresolved(f"is a {_json_type(value)}, and a SMILES is non-empty text")
    if expect == "rows":
        return _rows(value, site.columns or {})
    if not isinstance(value, list):
        raise _Unresolved(f"is a {_json_type(value)}; a series' {expect} binds to an array")
    for index, item in enumerate(value):
        fits = _is_number(item) or (expect == "x" and isinstance(item, str))
        if not fits:
            wanted = "a number or text" if expect == "x" else "a number"
            raise _Unresolved(f"element {index} is a {_json_type(item)}, not {wanted}")
    return value


def _rows(value: Any, columns: Mapping[str, str]) -> list[dict[str, Any]]:
    """A table's rows from an array of records: one row per element, one cell per column pointer.

    A column pointer an element does not reach gives an empty cell (optional fields are ordinary).

    Raises:
        _Unresolved: the value is not an array, or a cell is not text, a number or null.
    """
    if not isinstance(value, list):
        raise _Unresolved(f"is a {_json_type(value)}; rows_from binds to an array")
    rows: list[dict[str, Any]] = []
    for index, element in enumerate(value):
        row: dict[str, Any] = {}
        for key, pointer in columns.items():
            try:
                cell = pointer_get(element, pointer)
            except KeyError:
                cell = None
            if not (cell is None or isinstance(cell, str) or _is_number(cell)):
                raise _Unresolved(
                    f"element {index} at {pointer!r} is a {_json_type(cell)}, and a table cell "
                    "takes text or a number"
                )
            row[key] = cell
        rows.append(row)
    return rows


def _json_type(value: object) -> str:
    """The JSON name of a parsed value's type, for a refusal a writer can act on."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _container(raw: dict[str, Any], site: _Site) -> Any:
    """The object or array that holds `site`'s value in the spec JSON `raw`."""
    node: Any = raw
    for step in site.at[:-1]:
        node = node[step]
    return node


def _placed(raw: dict[str, Any], site: _Site, value: Any) -> None:
    """Put `value` where `site` sits in the spec JSON `raw`, in place.

    A table's `rows_from` becomes its `rows` — `[]` when it did not resolve.
    """
    if site.expect == "rows":
        raw.pop("rows_from", None)
        raw["rows"] = value if value is not None else []
        return
    _container(raw, site)[site.at[-1]] = value


def _restamped(raw: dict[str, Any], site: _Site, ref: str) -> None:
    """Replace the handle `site` was written with by the full ref, in the stored JSON `raw`."""
    held = _container(raw, site)[site.at[-1]]
    (held if site.expect == "rows" else held["$bind"])["result"] = ref


@dataclass(frozen=True)
class _Plan:
    """What each site resolves against, decided before anything is parsed.

    `refs` is the ref each site reads (by index), `refusals` the sites refused before any read, and
    `tolerated` the sites a missing result does not refuse — every site on a read, and on a write a
    binding carried unchanged from the parent revision.
    """

    refs: Mapping[int, str]
    refusals: Mapping[int, str]
    tolerated: frozenset[int]


def _resolve(
    raw: dict[str, Any],
    sites: list[_Site],
    links: Mapping[str, str],
    parsed: Mapping[str, Any],
    plan: _Plan,
    *,
    writing: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[ExhibitBinding], list[str]]:
    """The stored JSON, the resolved JSON, the bindings and the problems — pure, off the loop.

    A problem on a non-tolerated site refuses a write (all collected); otherwise it reads as `null`
    with `ok: false`.
    """
    stored = json.loads(json.dumps(raw))
    resolved = json.loads(json.dumps(raw))
    bindings: list[ExhibitBinding] = []
    problems: list[str] = []
    for index, site in enumerate(sites):
        ref = plan.refs.get(index, "")
        swept = False
        try:
            if index in plan.refusals:
                raise _Unresolved(plan.refusals[index])
            document = parsed.get(ref)
            if document is None:
                swept = True
                raise _Unresolved("its tool result is no longer stored (retention swept it)")
            if isinstance(document, ValueError):
                raise _Unresolved(f"its tool result is not JSON to point into ({document})")
            try:
                found = pointer_get(document, site.pointer)
            except (KeyError, ValueError) as exc:
                missing = exc.args[0] if exc.args else exc
                raise _Unresolved(f"pointer {site.pointer!r} does not resolve: {missing}") from exc
            value = _fitted(found, site)
        except _Unresolved as exc:
            if writing and not (swept and index in plan.tolerated):
                problems.append(f"{site.path} ({site.target}): {exc}")
            bindings.append(
                ExhibitBinding(
                    path=site.path,
                    result_ref=ref or site.target,
                    tool=links.get(ref, ""),
                    pointer=site.pointer,
                    ok=False,
                    error=str(exc),
                )
            )
            _placed(resolved, site, None)
            continue
        if writing:
            _restamped(stored, site, ref)
        _placed(resolved, site, value)
        bindings.append(
            ExhibitBinding(
                path=site.path,
                result_ref=ref,
                tool=links.get(ref, ""),
                pointer=site.pointer,
                ok=True,
            )
        )
    return stored, resolved, bindings, problems


def _write_plan(sites: list[_Site], links: Mapping[str, str], parent: Spec | None) -> _Plan:
    """Which ref each site of a write names, and which are refused or carried.

    A binding copied unchanged from the parent (same ref and pointer) is carried even if retention
    swept its result; refusing it would block every revision until someone detached a value they
    never touched. A swept parent ref at another pointer is refused naming that cause; anything else
    not linked to the session is refused as not a result of this conversation.
    """
    carried = _bound_pairs(parent)
    once_held = {ref for ref, _ in carried}
    refs: dict[int, str] = {}
    refusals: dict[int, str] = {}
    tolerated: set[int] = set()
    # One prefix scan per distinct target, not per site: a table binding a column cell by cell would
    # otherwise scan every link once per cell.
    named: dict[str, str | _Unresolved] = {}
    for index, site in enumerate(sites):
        if site.target not in named:
            try:
                named[site.target] = _ref_for(site.target, links)
            except _Unresolved as exc:
                named[site.target] = exc
        found = named[site.target]
        if isinstance(found, str):
            refs[index] = found
            continue
        reason = str(found)
        if (site.target, site.pointer) in carried:
            refs[index] = site.target
            tolerated.add(index)
        elif site.target in once_held:
            refusals[index] = (
                f"{site.target} was a tool result of this conversation and is no longer stored "
                "(retention swept it); detach the value — write it as a literal — or bind to a "
                "result that is still held"
            )
        else:
            refusals[index] = reason
    return _Plan(refs=refs, refusals=refusals, tolerated=frozenset(tolerated))


def _bound_pairs(spec: Spec | None) -> frozenset[tuple[str, str]]:
    """Each `(full ref, pointer)` a stored spec binds — what a revision of it may carry."""
    if spec is None:
        return frozenset()
    return frozenset((site.target, site.pointer) for site in _sites(spec_json(spec)))


async def _run(
    raw: dict[str, Any],
    sites: list[_Site],
    session_id: str,
    *,
    writing: bool,
    parent: Spec | None = None,
) -> Any:
    """Read what `sites` need from the store, then resolve — the parse off the loop when large.

    The session's links are asked first, and only a ref they name is read (from cache or blob
    table): a cached document is never the authorization.
    """
    if writing:
        links = await _links(session_id)
        plan = _write_plan(sites, links, parent)
    else:
        links = await _links(session_id, {site.target for site in sites})
        plan = _Plan(
            refs={index: site.target for index, site in enumerate(sites)},
            refusals={},
            tolerated=frozenset(range(len(sites))),
        )
    distinct = sorted({ref for ref in plan.refs.values() if ref in links})
    if writing and len(distinct) > settings.exhibit_max_bound_results:
        raise InvalidExhibit(
            f"the spec binds into {len(distinct)} tool results, over the "
            f"{settings.exhibit_max_bound_results}-result cap; write some values, or split it"
        )
    documents: dict[str, Any] = {}
    sizes: dict[str, int] = {}
    for ref in distinct:
        if (hit := _cached(ref)) is not None:
            documents[ref], sizes[ref] = hit
    blobs = await _blobs(session_id, [ref for ref in distinct if ref not in documents])
    sizes.update({ref: len(data) for ref, data in blobs.items()})

    def _work() -> tuple[dict[str, Any], Any]:
        fresh = _parsed(blobs)
        result = _resolve(raw, sites, links, {**documents, **fresh}, plan, writing=writing)
        return fresh, result

    # Measured in stored UTF-8 bytes, cached documents included: the walk over a large document
    # costs the same whether its parse was saved or not.
    if sum(sizes.values()) > settings.exhibit_binding_offload_bytes:
        fresh, result = await asyncio.to_thread(_work)
    else:
        fresh, result = _work()
    for ref, document in fresh.items():
        _remember(ref, document, sizes[ref])
    return result


async def bind_for_write(session_id: str, spec: Spec, *, parent: Spec | None = None) -> Bound:
    """The spec to store and the spec to show, or a refusal naming every binding that fails.

    `parent` is the stored spec of the revision being revised, `None` for a create; unchanged
    bindings from it are carried even when swept (`_write_plan`). A spec with no binding is returned
    as both, without reading the store. Every writer comes through here, so the spec byte cap is
    applied first.

    Raises:
        InvalidExhibit: the spec is over `exhibit_max_spec_bytes`; a binding names a result this
            session does not hold (or names it ambiguously, or it was swept), its pointer does not
            resolve, its value does not fit the position, the spec binds more than
            `exhibit_max_bound_results` results, or this deployment keeps no tool results.
    """
    raw = spec_json(spec)
    # The byte cap before anything is resolved, so an oversized spec does not buy reads and parses.
    # The stored form (with full refs) is checked again by `require_writable`.
    if (size := spec_bytes(spec)) > settings.exhibit_max_spec_bytes:
        raise InvalidExhibit(
            f"the spec is {size} bytes, over the {settings.exhibit_max_spec_bytes}-byte cap; "
            "split it into more than one artefact"
        )
    sites = list(_sites(raw))
    if not sites:
        return Bound(stored=spec, resolved=spec, bindings=[])
    if not handles_resolve():
        raise InvalidExhibit(
            "this deployment keeps no tool results to bind to; write the values as literals"
        )
    stored, resolved, bindings, problems = await _run(
        raw, sites, session_id, writing=True, parent=parent
    )
    if problems:
        shown = settings.exhibit_binding_problems_shown
        more = len(problems) - shown
        raise InvalidExhibit(
            "a binding does not resolve: "
            + "; ".join(problems[:shown])
            + (f"; and {more} more" if more > 0 else "")
        )
    return Bound(stored=parse_spec(stored), resolved=parse_spec(resolved), bindings=bindings)


async def resolved_view(view: ExhibitView) -> ExhibitView:
    """`view` as a reader is served it: `spec` resolved, `raw_spec` as stored, `bindings` listed.

    A binding whose result is gone reads `null` with `ok: false`; an artefact must stay readable
    after retention. A geometry citing a `structure_id` is resolved too
    (`sources.resolved_geometry`).
    """
    if isinstance(view.raw_spec, GeometrySpec):
        return await resolved_geometry(view)
    raw = spec_json(view.raw_spec)
    sites = list(_sites(raw))
    if not sites:
        return view
    if not handles_resolve():
        bindings = [
            ExhibitBinding(
                path=site.path,
                result_ref=site.target,
                tool="",
                pointer=site.pointer,
                ok=False,
                error="this deployment keeps no tool results",
            )
            for site in sites
        ]
        resolved = json.loads(json.dumps(raw))
        for site in sites:
            _placed(resolved, site, None)
        return view.model_copy(update={"spec": parse_spec(resolved), "bindings": bindings})
    _, resolved, bindings, _ = await _run(raw, sites, view.session_id, writing=False)
    return view.model_copy(update={"spec": parse_spec(resolved), "bindings": bindings})
