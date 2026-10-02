# Artefacts phase 0 — raw measurements (2026-10-02, base commit 5ca596a)

Read with `docs/decisions/D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect.md`.

## 0.1 What answers are made of

`python -m chemclaw.evals.answer_shape tasks/live-*`:

| set | answers | with a table | with a table >= min rows | table share of answer tokens | figures in tables | checked → verbatim tool values | listing >= min structures | document-shaped | render_structure / tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| live-test/durable | 3 | 0 (0.0%) | 0 (0.0%) | 0.0% | 0 | 0 → n/a | 0 | 0 (0.0%) | 0 / 43 |
| live-test/harness | 2 | 0 (0.0%) | 0 (0.0%) | 0.0% | 0 | 0 → n/a | 0 | 1 (50.0%) | 0 / 23 |
| live-test/transcripts | 193 | 17 (8.8%) | 14 (7.3%) | 3.8% | 340 | 0 → n/a | 1 | 49 (25.4%) | 8 / 636 |
| live-test/transcripts/ab/augmented | 221 | 13 (5.9%) | 10 (4.5%) | 1.5% | 114 | 114 → 64.0% | 7 | 19 (8.6%) | 4 / 578 |
| live-test/transcripts/ab/baseline | 221 | 10 (4.5%) | 8 (3.6%) | 1.8% | 170 | 170 → 4.1% | 3 | 13 (5.9%) | 0 / 194 |
| live-test/transcripts/corpus/2026-09-14T18-56-27Z | 15 | 0 (0.0%) | 0 (0.0%) | 0.0% | 0 | 0 → n/a | 2 | 3 (20.0%) | 6 / 70 |
| live-test/transcripts-after-fix | 6 | 0 (0.0%) | 0 (0.0%) | 0.0% | 0 | 0 → n/a | 0 | 2 (33.3%) | 0 / 11 |
| live-test/transcripts-sonnet | 43 | 5 (11.6%) | 5 (11.6%) | 2.8% | 105 | 0 → n/a | 0 | 8 (18.6%) | 1 / 280 |
| live-test-2026-08-03 | 36 | 3 (8.3%) | 3 (8.3%) | 2.1% | 37 | 0 → n/a | 0 | 6 (16.7%) | 0 / 102 |
| live-test-2026-08-17/transcripts | 230 | 20 (8.7%) | 16 (7.0%) | 3.5% | 328 | 328 → 71.3% | 1 | 46 (20.0%) | 6 / 618 |
| live-verify-2026-08-03 | 15 | 3 (20.0%) | 2 (13.3%) | 6.2% | 33 | 0 → n/a | 0 | 4 (26.7%) | 0 / 68 |
| **all** | 985 | 71 (7.2%) | 58 (5.9%) | 2.7% | 1127 | 612 → 51.3% | 14 | 151 (15.3%) | 25 / 2623 |

table tokens per table-bearing answer: 186
table rows per table-bearing answer: 7.8

## 0.2 What the artefact tools cost in the prefix

```
create_exhibit (typed union)     1492
create_exhibit (untyped spec)     464
revise_exhibit                    358
read_exhibit                      139
create_table_artefact (1 of 6)    255

total, typed       1989
total, untyped      961
total, per-kind    2027  (6 x the table tool, approximated)
```

Default-profile static prefix at the base commit (`tests/test_context_floor._floor("default")`): **72,446** against `CEILINGS["__default__"]` 72,850 — 404 tokens of headroom. `MAX_SINGLE_TOOL_TOKENS` is 900. A one-line result handle (`⟨r:3fa9c1e2b7d0⟩`) costs **4 tokens** per tool result by `count_tokens_approximately`.

The drafts, kept here rather than in the tree because nothing imports them (`tests/test_repo_map.py` keeps Python inside `src/`). Re-run by saving the block as a `.py` file outside the repository and running it with `.venv/bin/python`:

```python
"""Phase 0.2 of the artefacts plan: what the artefact tools would add to every model call.

Drafts `create_exhibit`, `revise_exhibit` and `read_exhibit` with the docstrings they would ship
with, and measures them exactly the way `tests/test_context_floor.py::_floor` measures a bound tool:
`agent/tool_schema.as_structured_tool` (what `build_langgraph_agent` binds), then
`convert_to_openai_tool`, then `count_tokens_approximately` over the JSON.

Three shapes of `spec` are measured, because the shape is the decision:

- **typed**: a pydantic discriminated union over the six kinds, so the provider can constrain
  generation — the shape `draft_experiment_protocol` took, at 2,738 tokens;
- **untyped**: `spec: dict[str, Any]` with each kind's fields described in the docstring and
  validated server-side, refusing with a worded `ValueError`;
- **per-kind**: one `create_<kind>_artefact` per kind (shown for the `table` kind only and
  multiplied, since the per-kind tools are near-identical in size).

These are measurement drafts, not the implementation: nothing here is imported by `src/`.
Run from the repository root: `.venv/bin/python tasks/artefacts-phase-0/measure_prefix.py`.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

from chemclaw.agent.tool_schema import as_structured_tool

# --- the typed spec ------------------------------------------------------------------------------


class Binding(BaseModel):
    """A value taken from a tool result rather than written out."""

    result: str = Field(description="Handle printed under a tool result, e.g. 'r:3fa9c1e2b7d0'.")
    pointer: str = Field(description="JSON Pointer into that result, e.g. '/rows/0/pka'.")


class Column(BaseModel):
    key: str
    label: str
    unit: str = ""


class DocumentSpec(BaseModel):
    kind: Literal["document"]
    markdown: str


class TableSpec(BaseModel):
    kind: Literal["table"]
    columns: list[Column]
    rows: list[dict[str, str | float | Binding]] = Field(default_factory=list)
    rows_from: Binding | None = Field(None, description="Bind every row from a result's array.")


class Structure(BaseModel):
    smiles: str
    label: str = ""
    props: dict[str, str | float | Binding] = Field(default_factory=dict)


class StructuresSpec(BaseModel):
    kind: Literal["structures"]
    items: list[Structure]


class Series(BaseModel):
    name: str
    x: list[float] | Binding
    y: list[float] | Binding


class ChartSpec(BaseModel):
    kind: Literal["chart"]
    chart: Literal["line", "scatter", "bar"]
    x_label: str
    y_label: str
    series: list[Series]


class ResultSpec(BaseModel):
    kind: Literal["result"]
    result: str = Field(description="Handle printed under the tool result to pin.")


class LinkSpec(BaseModel):
    kind: Literal["link"]
    target: Literal["protocol", "note", "job"]
    id: str


Spec = Annotated[
    DocumentSpec | TableSpec | StructuresSpec | ChartSpec | ResultSpec | LinkSpec,
    Field(discriminator="kind"),
]

_CREATE_DOC = """Show the chemist a document, table, structure set or chart beside your answer.

Use it for a deliverable the chemist will reread, edit or export: a report or plan draft, a table
of >= 4 rows, a set of structures, a series to plot, or a tool result worth pinning. Not for one
value or a short list — say those in the answer. Revise an existing artefact with `revise_exhibit`
rather than creating a second one.

**Bind figures; do not retype them.** A tool result ends with a handle like `⟨r:3fa9c1e2b7d0⟩`;
`{"result": "r:3fa9c1e2b7d0", "pointer": "/rows/0/pka"}` puts that exact value in a cell, with
its provenance. A number you type yourself is checked against what tools returned and flagged if
nothing did.
"""

_KINDS_DOC = """
`spec` by kind (`kind` is required):
- document: `markdown`.
- table: `columns` [{key, label, unit}], and `rows` [{key: value|binding}] or `rows_from` binding.
- structures: `items` [{smiles, label, props: {name: value|binding}}].
- chart: `chart` line|scatter|bar, `x_label`, `y_label` (with units), `series` [{name, x, y}],
  x and y each a list of numbers or a binding.
- result: `result` handle. link: `target` protocol|note|job, `id`.
"""


def create_exhibit_typed(title: str, spec: Spec) -> str:
    return ""


create_exhibit_typed.__doc__ = (
    _CREATE_DOC
    + """
Args:
    title: What the chemist sees on the tab, e.g. "pKa screen — series B".
    spec: The content; its `kind` decides the shape.

Returns:
    JSON with `exhibit_id` and `revision`.
"""
)
create_exhibit_typed.__name__ = "create_exhibit"


def create_exhibit_untyped(title: str, spec: dict[str, Any]) -> str:
    return ""


create_exhibit_untyped.__doc__ = (
    _CREATE_DOC
    + _KINDS_DOC
    + """
Args:
    title: What the chemist sees on the tab, e.g. "pKa screen — series B".
    spec: The content, shaped by its `kind` as listed above.

Returns:
    JSON with `exhibit_id` and `revision`.

Raises:
    ValueError: the spec does not match its kind, a handle does not name a result in this
        session, or the artefact is over its size cap — the message says which.
"""
)
create_exhibit_untyped.__name__ = "create_exhibit"


class Edit(BaseModel):
    old: str = Field(description="Exact text to replace; must occur once.")
    new: str


def revise_exhibit(
    exhibit_id: str,
    base_revision: int,
    note: str,
    edits: list[Edit] | None = None,
    spec: dict[str, Any] | None = None,
) -> str:
    """Change an artefact you or the chemist created, as a new revision.

    For a document, prefer `edits` (exact replacements) over resending the whole `spec`. Refused
    when `base_revision` is not the latest — the chemist edited it; call `read_exhibit` first and
    keep their change.

    Args:
        exhibit_id: The artefact, e.g. "xb-1a2b3c4d5e6f7a8b".
        base_revision: The revision you are changing.
        note: One line on what changed and why; the chemist sees it in the history.
        edits: Replacements for a document; each `old` must occur exactly once.
        spec: A whole new spec of the same kind, instead of `edits`.

    Returns:
        JSON with the new `revision`.
    """
    return ""


def read_exhibit(exhibit_id: str, revision: int = 0) -> str:
    """Read an artefact, with figures resolved, and what the chemist changed since your last edit.

    Args:
        exhibit_id: The artefact, e.g. "xb-1a2b3c4d5e6f7a8b".
        revision: 0 for the latest.

    Returns:
        JSON with `kind`, `title`, `revision`, `spec` and `changes_since_agent`.
    """
    return ""


def create_table_artefact(title: str, columns: list[Column], rows: list[dict[str, Any]]) -> str:
    """Show the chemist a table beside your answer. (One of six per-kind tools.)

    Use it for >= 4 rows the chemist will reread or export. Bind figures from tool results with
    `{"result": "r:…", "pointer": "/…"}` rather than retyping them.

    Args:
        title: What the chemist sees on the tab.
        columns: Each with `key`, `label` and `unit`.
        rows: One mapping per row, a value or a binding per key.

    Returns:
        JSON with `exhibit_id` and `revision`.
    """
    return ""


def tokens(fn: Any) -> int:
    """One bound tool's schema, counted as `test_context_floor._count(_tool_schema(...))` counts."""
    schema = json.dumps(convert_to_openai_tool(as_structured_tool(fn)))
    return int(count_tokens_approximately([HumanMessage(schema)]))


def main() -> None:
    """Print each draft's cost and the three totals."""
    rev, read = tokens(revise_exhibit), tokens(read_exhibit)
    typed, untyped, table = (
        tokens(create_exhibit_typed),
        tokens(create_exhibit_untyped),
        tokens(create_table_artefact),
    )
    print(f"create_exhibit (typed union)   {typed:>6}")
    print(f"create_exhibit (untyped spec)  {untyped:>6}")
    print(f"revise_exhibit                 {rev:>6}")
    print(f"read_exhibit                   {read:>6}")
    print(f"create_table_artefact (1 of 6) {table:>6}")
    print()
    print(f"total, typed     {typed + rev + read:>6}")
    print(f"total, untyped   {untyped + rev + read:>6}")
    print(f"total, per-kind  {6 * table + rev + read:>6}  (6 x the table tool, approximated)")


if __name__ == "__main__":
    main()
```
