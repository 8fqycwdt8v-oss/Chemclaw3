"""Resolve an artefact's bound values against the stored tool results of its own session.

`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`. A spec may put
`{"$bind": {"result": "r:<hex>", "pointer": "/a/0/b"}}` where it takes a cell, a property, a SMILES
or a series, and a table may take `rows_from` instead of `rows`. This module turns such a spec into
the three things the rest of the package needs:

- the **stored** spec — the bindings kept, each handle replaced by the full 64-hex ref it names, so
  a revision keeps pointing at the same bytes whatever later results share its prefix;
- the **resolved** spec — every binding replaced by its value, which is what a renderer, an export
  and the caps see, so the existing renderers work unchanged;
- the **bindings** list the view serves beside it, one entry per bound value.

**A write and a read differ in one thing: what a failure is.** On a write every binding must
resolve — the result exists in this session's `tool_result_links`, its prefix names exactly one,
the pointer reaches a value and the value fits the position — or the write is refused naming the
first few problems. On a read the stored ref is already exact and the only thing that can have
changed is that retention swept the result; that binding reads `null` with `ok: false` and a
reason, and the rest of the artefact still reads.

**The session is the scope, and the link is the authorization** — the join `api/tool_results.py`
makes for its fetch route, made again here because `exhibits` sits below `api`: a ref another
session produced resolves to nothing, so neither the model nor a person can bind bytes they were
never shown. A person's revision may keep any ref already linked to the session (copied from
`raw_spec`), replace one with a literal, and no more.

**JSON Pointers are RFC 6901, written here rather than imported**: the grammar is two escapes and
an array index, and a dependency for it would be one more thing every image installs.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.exhibits.models import (
    ExhibitBinding,
    ExhibitView,
    InvalidExhibit,
    Spec,
    parse_spec,
    spec_json,
)

#: What each bound position accepts, by the name a refusal uses for it.
Expect = Literal["cell", "prop", "smiles", "x", "y", "rows"]

_SESSION_LINKS = "SELECT content_hash, tool FROM tool_result_links WHERE session_id = %s"

_BLOBS = """
SELECT l.content_hash, b.data
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s AND l.content_hash = ANY(%s)
"""

#: How many binding problems one refusal names before it counts the rest.
_SHOWN = 5


@dataclass(frozen=True)
class _Site:
    """One bound position in a spec's JSON: where it is, what it reads, what it must be.

    `at` is the position as keys and indices from the spec's root (`("rows", 3, "yield")`), which
    is what the value is written back through; `path` is the same position as the diff spells it,
    for a reader. Two forms because a column key or a property name may itself contain `.` or `[`,
    and a dotted string cannot be walked back reliably.
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


def _available() -> bool:
    """Whether this deployment keeps the session's tool results a binding could read."""
    return settings.session_store == "postgres" and settings.stream_max_result_bytes > 0


async def _links(session_id: str) -> dict[str, str]:
    """The session's stored results, ref to tool — the set every target must fall inside."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SESSION_LINKS, (session_id,))
            rows = await cur.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


async def _texts(session_id: str, refs: list[str]) -> dict[str, str]:
    """The stored text of each ref this session still holds; a swept one is simply absent."""
    if not refs:
        return {}
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_BLOBS, (session_id, refs))
            rows = await cur.fetchall()
    return {str(row[0]): bytes(row[1]).decode("utf-8", errors="replace") for row in rows}


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


def _parsed(texts: Mapping[str, str]) -> dict[str, Any]:
    """Each stored text as JSON, or the `ValueError` it raised — kept, so a binding can say why."""
    parsed: dict[str, Any] = {}
    for ref, text in texts.items():
        try:
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
            if not token.isdigit() or (token != "0" and token.startswith("0")):
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

    A column pointer an element does not reach gives an empty cell — a list of records with an
    optional field is the ordinary case, and refusing it would refuse most real results.

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

    A table's `rows_from` becomes its `rows` — `[]` when it did not resolve, since a table's rows
    are a list whatever happened to the result they came from.
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


def _resolve(
    raw: dict[str, Any],
    sites: list[_Site],
    links: Mapping[str, str],
    parsed: Mapping[str, Any],
    *,
    writing: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[ExhibitBinding], list[str]]:
    """The stored JSON, the resolved JSON, the bindings and the problems — pure, off the loop.

    `writing` decides what a problem is: a refusal (collected, so a writer sees several at once) or
    a `null` value with `ok: false` (a read of a revision whose result has been swept).
    """
    stored = json.loads(json.dumps(raw))
    resolved = json.loads(json.dumps(raw))
    bindings: list[ExhibitBinding] = []
    problems: list[str] = []
    for site in sites:
        ref = ""
        try:
            ref = _ref_for(site.target, links) if writing else site.target
            document = parsed.get(ref)
            if document is None:
                raise _Unresolved("its tool result is no longer stored (retention swept it)")
            if isinstance(document, ValueError):
                raise _Unresolved(f"its tool result is not JSON to point into ({document})")
            try:
                found = pointer_get(document, site.pointer)
            except KeyError as exc:
                missing = exc.args[0]
                raise _Unresolved(f"pointer {site.pointer!r} does not resolve: {missing}") from exc
            value = _fitted(found, site)
        except _Unresolved as exc:
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


async def _run(raw: dict[str, Any], sites: list[_Site], session_id: str, *, writing: bool) -> Any:
    """Read what `sites` need from the store, then resolve — the parse off the loop when large."""
    links = await _links(session_id)
    if writing:
        refs: list[str] = []
        for site in sites:
            try:
                refs.append(_ref_for(site.target, links))
            except _Unresolved:
                continue
    else:
        refs = [site.target for site in sites]
    distinct = sorted(set(refs))
    if writing and len(distinct) > settings.exhibit_max_bound_results:
        raise InvalidExhibit(
            f"the spec binds into {len(distinct)} tool results, over the "
            f"{settings.exhibit_max_bound_results}-result cap; write some values, or split it"
        )
    texts = await _texts(session_id, distinct)
    size = sum(len(text) for text in texts.values())
    if size > settings.exhibit_binding_offload_bytes:
        return await asyncio.to_thread(
            lambda: _resolve(raw, sites, links, _parsed(texts), writing=writing)
        )
    return _resolve(raw, sites, links, _parsed(texts), writing=writing)


async def bind_for_write(session_id: str, spec: Spec) -> Bound:
    """The spec to store and the spec to show, or a refusal naming every binding that fails.

    A spec with no binding is returned as both, with no read of the store.

    Raises:
        InvalidExhibit: a binding names a result this session does not hold (or names it
            ambiguously), its pointer does not resolve, its value does not fit the position, the
            spec binds into more results than `exhibit_max_bound_results`, or this deployment keeps
            no tool results to bind to.
    """
    raw = spec_json(spec)
    sites = list(_sites(raw))
    if not sites:
        return Bound(stored=spec, resolved=spec, bindings=[])
    if not _available():
        raise InvalidExhibit(
            "this deployment keeps no tool results to bind to; write the values as literals"
        )
    stored, resolved, bindings, problems = await _run(raw, sites, session_id, writing=True)
    if problems:
        more = len(problems) - _SHOWN
        raise InvalidExhibit(
            "a binding does not resolve: "
            + "; ".join(problems[:_SHOWN])
            + (f"; and {more} more" if more > 0 else "")
        )
    return Bound(stored=parse_spec(stored), resolved=parse_spec(resolved), bindings=bindings)


async def resolved_view(view: ExhibitView) -> ExhibitView:
    """`view` as a reader is served it: `spec` resolved, `raw_spec` as stored, `bindings` listed.

    A binding whose stored result is gone reads `null` with `ok: false`; nothing here raises for
    it, because an artefact must stay readable when retention takes what one cell pointed at.
    """
    raw = spec_json(view.raw_spec)
    sites = list(_sites(raw))
    if not sites:
        return view
    if not _available():
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
