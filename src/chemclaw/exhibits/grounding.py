"""The figures an agent-written artefact states that no tool in its session returned (unchecked).

The same rule the answer's own grounding check uses (`core/quantities`: `ungrounded`,
`is_rounding_of`), applied to agent revisions; failing figures are stored on the revision and shown
beside it. The word is "unchecked", not "wrong": such a figure may be the chemist's own, arithmetic,
or something the scan cannot see.

The evidence is the session's stored tool results (`tool_result_blobs` via `tool_result_links`),
plus figures a chemist introduced in a revision of the same artefact — introduced, i.e. absent from
that revision's parent, so an unrelated edit does not vouch for everything carried along. `None`
means not checked (no stored evidence available), distinct from `[]`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.quantities import returned_values, stated_numerals, ungrounded
from chemclaw.core.result_handle import handles_resolve
from chemclaw.exhibits.evidence import evidence_params, evidence_predicate
from chemclaw.exhibits.models import (
    Binding,
    ChartSpec,
    DocumentSpec,
    GeometrySpec,
    HtmlSpec,
    Number,
    Spec,
    StructuresSpec,
    TableSpec,
)

# Newest first, so a figure from this turn's result is found in the first batch. Only evidence
# counts (`exhibits.evidence`), filtered by the link's tool, so the agent reading its own work back
# cannot ground its figures.
_SESSION_RESULTS = """
SELECT b.data
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s AND {evidence}
ORDER BY l.created_at DESC
""".format(evidence=evidence_predicate("l.tool"))


def stated_figures(spec: Spec) -> list[str]:
    """Every figure a spec states as a value, as written, deduplicated in first-seen order.

    Values only: table cells, structure property values, chart points, a geometry's energy, document
    prose and html text (`html_text`). Literal values only; a `$bind` is the tool's own value, so
    the spec is read as stored. Names (titles, labels, units, SMILES) and geometry coordinates are
    not figures.
    """
    seen: dict[str, None] = {}
    for figure in _figures(spec):
        seen.setdefault(figure, None)
    return list(seen)


def _figures(spec: Spec) -> Iterator[str]:
    """The figures of one spec, in reading order, repeats included."""
    if isinstance(spec, DocumentSpec):
        yield from stated_numerals(spec.markdown)
    elif isinstance(spec, TableSpec):
        for row in spec.rows:
            for value in row.values():
                yield from _of_value(value)
    elif isinstance(spec, StructuresSpec):
        for item in spec.items:
            for value in item.props.values():
                yield from _of_value(value)
    elif isinstance(spec, ChartSpec):
        for series in spec.series:
            for axis in (series.x, series.y):
                if isinstance(axis, list):
                    for value in axis:
                        yield from _of_value(value)
    elif isinstance(spec, GeometrySpec):
        yield from _of_value(spec.energy_hartree)
    elif isinstance(spec, HtmlSpec):
        yield from stated_numerals(html_text(spec.html))


#: Elements whose character data is code, not text a reader sees: never scanned for figures.
_NOT_TEXT = frozenset({"script", "style"})


class _TextOf(HTMLParser):
    """The text content of a page — every character data run outside `<script>` and `<style>`."""

    def __init__(self) -> None:
        """Start with no text and outside any code element."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._inside = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Note entering a script or stylesheet."""
        if tag in _NOT_TEXT:
            self._inside += 1

    def handle_endtag(self, tag: str) -> None:
        """Note leaving one."""
        if tag in _NOT_TEXT and self._inside:
            self._inside -= 1

    def handle_data(self, data: str) -> None:
        """Keep a run of text, separated from its neighbours so two runs never join into one."""
        if not self._inside:
            self.parts.append(data)


def html_text(page: str) -> str:
    """The text a reader of `page` sees — the grounding scan's input for `html`.

    The standard library's tolerant parser: character data only, including inline SVG `<text>`, and
    a malformed page still yields its text. Not `<script>`, `<style>` or attributes, where numbers
    are code and layout; a chart drawn by a script from a JS array therefore goes unchecked.
    """
    parser = _TextOf()
    parser.feed(page)
    parser.close()
    return " ".join(parser.parts)


def _of_value(value: Number | str | Binding | None) -> Iterator[str]:
    """A literal number as the numeral it is written as; a text cell's figures by the prose rule.

    A binding states no figure of its own: its value is the tool result's, grounded by construction.
    """
    if value is None or isinstance(value, Binding):
        return
    if isinstance(value, str):
        yield from stated_numerals(value)
    else:
        yield repr(value)


def introduced_figures(mine: Spec, parent: Spec | None) -> list[str]:
    """The figures a person's revision states that its parent did not — what they introduced.

    Computed once at write time and recorded (`store.chemist_figures`). CPU work over a whole spec,
    so event-loop callers run it in a thread.
    """
    before = set(stated_figures(parent)) if parent is not None else set()
    return [figure for figure in stated_figures(mine) if figure not in before]


async def unverified_figures(
    session_id: str, spec: Spec, *, chemist_figures: Iterable[str] = ()
) -> list[str] | None:
    """The figures in `spec` that no tool result of `session_id` and no chemist edit accounts for.

    Args:
        session_id: The session whose stored tool results are the evidence.
        spec: The agent-authored spec being written.
        chemist_figures: Figures a person introduced into the same artefact
            (`ExhibitStore.chemist_figures`); they count as grounded.

    Returns:
        At most `exhibit_max_unverified_figures` figures, in the order the spec states them; `[]`
        when every figure is accounted for; `None` when there is no stored evidence to check.
    """
    remaining = stated_figures(spec)
    if not remaining:
        return []
    if not handles_resolve():
        return None
    human = [float(figure.replace(",", "")) for figure in chemist_figures]
    if human:
        remaining = await asyncio.to_thread(ungrounded, remaining, human)
    async with db.connection(settings.postgres_dsn) as conn:
        # A server-side cursor, so only a batch crosses the wire and the early stop saves the fetch.
        async with conn.cursor(name="exhibit_grounding") as cur:
            await cur.execute(_SESSION_RESULTS, (session_id, *evidence_params()))
            while remaining:
                rows = await cur.fetchmany(settings.exhibit_grounding_batch)
                if not rows:
                    break
                texts = [bytes(row[0]).decode("utf-8", errors="replace") for row in rows]
                remaining = await asyncio.to_thread(_still_ungrounded, remaining, texts)
    return remaining[: settings.exhibit_max_unverified_figures]


def _still_ungrounded(figures: list[str], texts: list[str]) -> list[str]:
    """The figures none of `texts`' numbers rounds to. Off the loop: it is a regex over results."""
    values = [value for text in texts for value in returned_values(text)]
    return ungrounded(figures, values)
