"""The figures an agent-written artefact states that no tool in its session returned (unchecked).

The answer's own grounding check (`core/quantities`, read by `evals.live` and by
`ToolResultEvent.numbers`) asks of a prose answer whether each figure is a rounding of something a
tool returned. An artefact is part of the answer, so the agent's revisions are asked the same thing,
by the same rule (`ungrounded`, `is_rounding_of`'s batch form), and the figures that fail are stored
on the revision and shown beside it.

**The wording is "unchecked", everywhere, and that is a measurement rather than a courtesy.**
`evals/live.py::_verified_numbers` measured the inverse signal at precision zero, and a figure no
tool returned may be the chemist's own, arithmetic over tool values, or a value the scan could not
see. What the flag says is that nobody can point at the tool output it came from.

**The evidence is the session's stored tool results** (`tool_result_blobs` through
`tool_result_links`), which is the one place every result of every turn of the session is kept in
full — the turn trace holds only the current turn's, and only in the front door's process. The
figures the **chemist** introduced in a revision of the same artefact count as grounded too: an
agent revision that carries the chemist's edit forward is not transcribing it. *Introduced* — a
figure their revision has and its parent did not — because a person's revision carries every figure
it did not touch, and counting those would let one unrelated edit vouch for everything the agent
wrote before it.

**`None` means "not checked"**, which is different from `[]` ("checked, nothing unaccounted for"):
with the in-memory session store or the result store switched off there is no evidence to read, and
flagging every figure would be a claim this module cannot make.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.quantities import returned_values, stated_numerals, ungrounded
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
from chemclaw.exhibits.store import ExhibitStore

# Newest first, so a figure transcribed from this turn's result is found on the first batch and the
# scan stops; only a figure that really is unaccounted for reads the whole session.
_SESSION_RESULTS = """
SELECT b.data
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s
ORDER BY l.created_at DESC
"""


def stated_figures(spec: Spec) -> list[str]:
    """Every figure a spec states as a value, as written, deduplicated in first-seen order.

    Values, not names: a table's cells, a structure's property values, a chart's points, a
    geometry's energy, a document's prose and an html page's text (`html_text`). Literal values
    only — a `$bind` is the tool's own value, so a spec is read as stored, bindings unresolved.
    Titles, column labels, units, structure labels and SMILES are names, and a "Compound 12" or a
    ring-closure digit is not a figure anybody transcribed. A geometry's coordinates are not
    figures either: they are a structure, read by a viewer rather than quoted, and three per atom
    would bury the one figure a chemist does quote.
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


class _TextOf(HTMLParser):
    """The text content of a page — every character data run, `<style>` excepted."""

    def __init__(self) -> None:
        """Start with no text and outside any `<style>`."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._style = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Note entering a stylesheet, whose numbers are lengths and colours, never figures."""
        if tag == "style":
            self._style += 1

    def handle_endtag(self, tag: str) -> None:
        """Note leaving one."""
        if tag == "style" and self._style:
            self._style -= 1

    def handle_data(self, data: str) -> None:
        """Keep a run of text, separated from its neighbours so two runs never join into one."""
        if not self._style:
            self.parts.append(data)


def html_text(page: str) -> str:
    """The text a reader of `page` could see a figure in — the grounding scan's input for `html`.

    The standard library's tolerant parser rather than a dependency: this needs character data and
    nothing else, and a malformed page still yields what text it has. **Script text is kept**, on
    purpose: a page that draws a chart from an array in a `<script>` is stating those figures as
    surely as a table cell is, and leaving them out would let a transcribed chart pass as grounded.
    Style text is not — `width: 100%` is layout. Attributes are not text and are not read.
    """
    parser = _TextOf()
    parser.feed(page)
    parser.close()
    return " ".join(parser.parts)


def _of_value(value: Number | str | Binding | None) -> Iterator[str]:
    """A literal number as the numeral it is written as; a text cell's figures by the prose rule.

    A binding states no figure of its own: its value is the tool result's, verbatim, so it is
    grounded by construction and never reaches the scan.
    """
    if value is None or isinstance(value, Binding):
        return
    if isinstance(value, str):
        yield from stated_numerals(value)
    else:
        yield repr(value)


async def chemist_figures(store: ExhibitStore, session_id: str, exhibit_id: str) -> list[str]:
    """The figures a person introduced into `exhibit_id`: in their revision, not in its parent."""
    introduced: list[str] = []
    for mine, parent in await store.human_edits(session_id, exhibit_id):
        before = set(stated_figures(parent)) if parent is not None else set()
        introduced += [figure for figure in stated_figures(mine) if figure not in before]
    return introduced


async def unverified_figures(
    session_id: str, spec: Spec, *, chemist_figures: Iterable[str] = ()
) -> list[str] | None:
    """The figures in `spec` that no tool result of `session_id` and no chemist edit accounts for.

    Args:
        session_id: The session whose stored tool results are the evidence.
        spec: The agent-authored spec being written.
        chemist_figures: Figures a person introduced into the same artefact (`chemist_figures`);
            they count as grounded.

    Returns:
        At most `exhibit_max_unverified_figures` figures, in the order the spec states them; `[]`
        when every figure is accounted for; `None` when there is no stored evidence to check
        against (see the module docstring).
    """
    remaining = stated_figures(spec)
    if not remaining:
        return []
    if settings.session_store != "postgres" or settings.stream_max_result_bytes <= 0:
        return None
    human = [float(figure.replace(",", "")) for figure in chemist_figures]
    remaining = ungrounded(remaining, human)
    async with db.connection(settings.postgres_dsn) as conn:
        # A server-side cursor, so a batch is what crosses the wire: a client-side one would fetch
        # every stored result of the session before the first comparison, and the early stop would
        # save the regex and nothing else.
        async with conn.cursor(name="exhibit_grounding") as cur:
            await cur.execute(_SESSION_RESULTS, (session_id,))
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
