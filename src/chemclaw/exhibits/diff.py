"""What changed between two revisions of an artefact, in the shape a protocol diff already has.

The output is `protocols.diff.FieldChange`, so `Chemclaw3_ui` renders both with one component. Paths
are per kind:

- `document`: **line hunks** (`"lines 12-14"`);
- `table`: per **cell** (`"rows[3].yield"`), plus `"columns"` when the header changed;
- `structures`: per **item field** (`"items[2].smiles"`, `"items[2].props.pka"`);
- `chart`: per **point** (`"series[0].y[4]"`), plus axis and series names;
- `geometry`: per **field, whole-valued** (`"xyz"`, `"source"`, `"structure_id"`, `"label"`),
  since a re-optimised block moves every line;
- anything else, or a change of kind: one `"spec"` row.

Stored specs are compared (`raw_spec`), never resolved ones: a swept result changing what a cell
resolves to is not a revision, while binding, re-pointing or detaching a cell is. Positional rather
than keyed: rows, items and points have no identifiers.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.exhibits.models import (
    ChartSpec,
    DocumentSpec,
    ExhibitDiff,
    GeometrySpec,
    Spec,
    Structure,
    StructuresSpec,
    TableSpec,
    spec_json,
)
from chemclaw.protocols.diff import ChangeKind, FieldChange


def diff_specs(
    before: Spec | None, after: Spec, *, from_revision: int, to_revision: int
) -> ExhibitDiff:
    """Every change between two specs, in reading order.

    CPU-bound though bounded (`exhibit_diff_max_lines`); async callers run it in a thread.

    Args:
        before: The older revision's spec, or `None` for revision 1, which is all one addition: a
            document's lines as an added hunk, any other kind as one added `"spec"`.
        after: The newer revision's spec.
        from_revision: The older revision's number, carried onto the diff.
        to_revision: The newer revision's number.

    Returns:
        The changes, empty when the two specs are identical.
    """
    if before is None:
        changes = (
            _document("", after.markdown)
            if isinstance(after, DocumentSpec)
            else [_absent("spec", spec_json(after), "added")]
        )
    elif isinstance(before, DocumentSpec) and isinstance(after, DocumentSpec):
        changes = _document(before.markdown, after.markdown)
    elif isinstance(before, TableSpec) and isinstance(after, TableSpec):
        changes = _table(before, after)
    elif isinstance(before, StructuresSpec) and isinstance(after, StructuresSpec):
        changes = _indexed(
            "items",
            [item.model_dump(mode="json") for item in before.items],
            [item.model_dump(mode="json") for item in after.items],
            list(Structure.model_fields),
        )
    elif isinstance(before, ChartSpec) and isinstance(after, ChartSpec):
        changes = _chart(before, after)
    elif isinstance(before, GeometrySpec) and isinstance(after, GeometrySpec):
        changes = _fields("", spec_json(before), spec_json(after), _GEOMETRY_ORDER, nested=False)
    else:
        changes = _whole(spec_json(before), spec_json(after))
    return ExhibitDiff(from_revision=from_revision, to_revision=to_revision, changes=changes)


def _document(before: str, after: str) -> list[FieldChange]:
    """One change per differing line hunk, numbered by the *new* document's lines (1-based).

    `SequenceMatcher` is cubic on repeated lines, so the common head and tail are stripped first
    (linear), and a differing middle longer than `exhibit_diff_max_lines` on either side is reported
    as one hunk spanning it — true, just coarser. Autojunk is not a bound on the worst case.
    """
    old, new = before.splitlines(), after.splitlines()
    head = 0
    while head < min(len(old), len(new)) and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    old_mid, new_mid = old[head : len(old) - tail], new[head : len(new) - tail]
    if not old_mid and not new_mid:
        return []
    limit = settings.exhibit_diff_max_lines
    if len(old_mid) > limit or len(new_mid) > limit:
        opcodes = [(_tag(old_mid, new_mid), 0, len(old_mid), 0, len(new_mid))]
    else:
        matcher = difflib.SequenceMatcher(a=old_mid, b=new_mid, autojunk=False)
        opcodes = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    return [
        _hunk(tag, old_mid[i1:i2], new_mid[j1:j2], head + j1, head + j2)
        for tag, i1, i2, j1, j2 in opcodes
    ]


def _tag(old: list[str], new: list[str]) -> str:
    """The opcode a whole differing span is, as `SequenceMatcher` would have named it."""
    return "insert" if not old else "delete" if not new else "replace"


def _hunk(tag: str, old: list[str], new: list[str], j1: int, j2: int) -> FieldChange:
    """One differing span, located by the new document's 0-based line range `[j1, j2)`."""
    kind: ChangeKind = "added" if tag == "insert" else "removed" if tag == "delete" else "changed"
    # A deletion has no lines in the new document, so it is located where it would have been.
    first, last = (j1 + 1, j2) if j2 > j1 else (j1, j1)
    where = f"line {first}" if first == last else f"lines {first}-{last}"
    return FieldChange(path=where, kind=kind, before="\n".join(old), after="\n".join(new))


def _table(before: TableSpec, after: TableSpec) -> list[FieldChange]:
    """The header as one change, then every cell that differs, row by row."""
    changes: list[FieldChange] = []
    old_columns = [column.model_dump(mode="json") for column in before.columns]
    new_columns = [column.model_dump(mode="json") for column in after.columns]
    if old_columns != new_columns:
        changes.append(
            FieldChange(
                path="columns", kind="changed", before=_text(old_columns), after=_text(new_columns)
            )
        )
    old_json, new_json = spec_json(before), spec_json(after)
    old_from, new_from = old_json.get("rows_from"), new_json.get("rows_from")
    if old_from != new_from:
        kind: ChangeKind = (
            "added" if old_from is None else "removed" if new_from is None else "changed"
        )
        changes.append(
            FieldChange(path="rows_from", kind=kind, before=_text(old_from), after=_text(new_from))
        )
    changes.extend(
        _indexed(
            "rows",
            old_json["rows"],
            new_json["rows"],
            [column.key for column in after.columns],
        )
    )
    return changes


def _chart(before: ChartSpec, after: ChartSpec) -> list[FieldChange]:
    """The chart's own fields, then each series' name and points."""
    changes = [
        FieldChange(path=name, kind="changed", before=_text(old), after=_text(new))
        for name, old, new in (
            ("chart", before.chart, after.chart),
            ("x_label", before.x_label, after.x_label),
            ("y_label", before.y_label, after.y_label),
        )
        if old != new
    ]
    for index in range(max(len(before.series), len(after.series))):
        path = f"series[{index}]"
        if index >= len(after.series):
            changes.append(_absent(path, before.series[index].model_dump(mode="json"), "removed"))
            continue
        if index >= len(before.series):
            changes.append(_absent(path, after.series[index].model_dump(mode="json"), "added"))
            continue
        old, new = before.series[index], after.series[index]
        if old.name != new.name:
            changes.append(
                FieldChange(path=f"{path}.name", kind="changed", before=old.name, after=new.name)
            )
        for axis, before_values, after_values in (("x", old.x, new.x), ("y", old.y, new.y)):
            if isinstance(before_values, list) and isinstance(after_values, list):
                changes.extend(_values(f"{path}.{axis}", before_values, after_values))
            elif before_values != after_values:
                # A bound axis is one value — the binding — so binding, re-pointing or detaching
                # it is one change of the whole axis rather than a point-by-point one.
                changes.append(
                    FieldChange(
                        path=f"{path}.{axis}",
                        kind="changed",
                        before=_text(_jsonable(before_values)),
                        after=_text(_jsonable(after_values)),
                    )
                )
    return changes


def _indexed(
    name: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    order: list[str] | None = None,
) -> list[FieldChange]:
    """Members compared by position, field by field; a member present on one side is one change.

    `order` is the reading order of a member's keys (a table's column order); without it keys are
    sorted, never taken in stored order, because `jsonb` reorders keys and the two backends would
    disagree.
    """
    changes: list[FieldChange] = []
    for index in range(max(len(before), len(after))):
        path = f"{name}[{index}]"
        if index >= len(after):
            changes.append(_absent(path, before[index], "removed"))
        elif index >= len(before):
            changes.append(_absent(path, after[index], "added"))
        else:
            changes.extend(_fields(path, before[index], after[index], order))
    return changes


#: A geometry's fields in reading order; `source` is one whole value, its two halves never apart.
_GEOMETRY_ORDER = [
    "xyz",
    "source",
    "structure_id",
    "label",
    "energy_hartree",
    "highlight_atoms",
    "format",
]


def _fields(
    path: str,
    before: dict[str, Any],
    after: dict[str, Any],
    order: list[str] | None = None,
    *,
    nested: bool = True,
) -> list[FieldChange]:
    """Every key of two objects that differs, a nested object descended one level (`props`).

    `nested=False` reports a nested object as one value (a geometry's `source`, whose fields only
    mean something together). An empty `path` names a top-level key bare (`"xyz"`).
    """
    changes: list[FieldChange] = []
    present = set(before) | set(after)
    keys = [key for key in order or [] if key in present]
    keys += sorted(present - set(keys))
    for key in keys:
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        where = f"{path}.{key}" if path else key
        if nested and isinstance(old, dict) and isinstance(new, dict):
            changes.extend(_fields(where, old, new, None))
            continue
        kind: ChangeKind = (
            "added" if key not in before else "removed" if key not in after else "changed"
        )
        changes.append(FieldChange(path=where, kind=kind, before=_text(old), after=_text(new)))
    return changes


def _values(path: str, before: list[Any], after: list[Any]) -> list[FieldChange]:
    """Each position of two value lists that differs."""
    changes: list[FieldChange] = []
    for index in range(max(len(before), len(after))):
        old = before[index] if index < len(before) else None
        new = after[index] if index < len(after) else None
        if old == new and index < len(before) and index < len(after):
            continue
        kind: ChangeKind = (
            "added" if index >= len(before) else "removed" if index >= len(after) else "changed"
        )
        changes.append(
            FieldChange(path=f"{path}[{index}]", kind=kind, before=_text(old), after=_text(new))
        )
    return changes


def _jsonable(value: Any) -> Any:
    """A spec value as JSON: a binding as its `{"$bind": …}` object, anything else as it is."""
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _whole(before: dict[str, Any], after: dict[str, Any]) -> list[FieldChange]:
    """The whole spec as one change, or none when it did not move."""
    if before == after:
        return []
    return [FieldChange(path="spec", kind="changed", before=_text(before), after=_text(after))]


def _absent(path: str, value: Any, kind: ChangeKind) -> FieldChange:
    """A member that exists on one side only."""
    text = _text(value)
    return FieldChange(
        path=path,
        kind=kind,
        before=text if kind == "removed" else "",
        after=text if kind == "added" else "",
    )


def _text(value: Any) -> str:
    """One value as the string a diff shows: empty for absent, JSON for a structure."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def capped(diff: ExhibitDiff, *, max_changes: int, max_chars: int) -> tuple[ExhibitDiff, int]:
    """`diff` cut to what a model is shown: the first changes, each value bounded.

    Returns:
        The bounded diff and how many changes it left out, so the caller can say so.
    """
    kept = [
        change.model_copy(
            update={
                "before": _clip(change.before, max_chars),
                "after": _clip(change.after, max_chars),
            }
        )
        for change in diff.changes[:max_changes]
    ]
    return diff.model_copy(update={"changes": kept}), len(diff.changes) - len(kept)


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters with a visible marker."""
    marker = " …[cut]"
    return text if len(text) <= limit else text[: max(limit - len(marker), 0)] + marker
