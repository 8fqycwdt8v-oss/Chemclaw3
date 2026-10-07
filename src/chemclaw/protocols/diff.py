"""What changed between two revisions of a design.

The product, not a debugging aid: a chemist's alteration of the first-shot protocol is a
labelled correction, the most informative signal this system observes about its suggestions.
Flattened to dotted paths because both consumers want that form: a UI marks one field, and a
miner asks how often a given path changes and in which direction.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.protocols.models import ExperimentDesign

#: How a path differs. `changed` means both revisions have the path with different values.
ChangeKind = Literal["added", "removed", "changed"]

#: Paths whose *content* is what changed rather than their identity. A list of arms reordered is
#: not fourteen changes, and flattening one by index would say it was.
_KEYED_LISTS: dict[str, str] = {
    "arms": "arm_id",
    "factors": "name",
    "base.charge": "component",
    "base.analytics": "name",
    "evidence": "summary",
    "layout.wells": "label",
}


class FieldChange(BaseModel):
    """One path that differs between two revisions."""

    path: str
    kind: ChangeKind
    before: str = ""
    after: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DesignDiff(BaseModel):
    """Every path that differs, plus the two revisions it is between."""

    from_revision: int
    to_revision: int
    changes: list[FieldChange] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def paths(self) -> list[str]:
        """The changed paths, in the order the document lays them out — see `_reading_order`."""
        return [change.path for change in self.changes]


def _render(value: Any) -> str:
    """One value as the string a diff shows. `None` and `""` both render empty, deliberately."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(document: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """A design document as `{dotted.path: scalar}`.

    A list named in `_KEYED_LISTS` is keyed by its member's identifier (`arms.A1.control`), so
    reordering a plate is not read as rewriting it; other lists are keyed by index, where position
    is the identity. `None` is an absent path rather than a leaf, so adding an optional sub-model
    reads as an addition and unset fields produce no rows.
    """
    flat: dict[str, Any] = {}
    for key, value in document.items():
        path = f"{prefix}{key}"
        if value is None:
            continue
        if isinstance(value, dict):
            flat.update(flatten(value, f"{path}."))
        elif isinstance(value, list):
            for label, item in _labelled(value, _KEYED_LISTS.get(path)):
                if isinstance(item, dict):
                    flat.update(flatten(item, f"{path}.{label}."))
                else:
                    flat[f"{path}.{label}"] = item
        else:
            flat[path] = value
    return flat


def _labelled(items: list[Any], identifier: str | None) -> list[tuple[str, Any]]:
    """Each member with the key it is flattened under, disambiguating a repeated key.

    Identifiers are not guaranteed unique (a solvent charged in two portions), and a repeated key
    would overwrite or misattribute an edit. Repeats get `<label>#<n>` counted within the key, so
    an unrelated insertion or deletion does not renumber them. If a key is still not unique after
    that, the whole list falls back to positional keys, losing reorder-freeness but never merging
    two members.
    """
    if identifier is None:
        return [(str(index), item) for index, item in enumerate(items)]
    labels = [
        str(item.get(identifier, index)) if isinstance(item, dict) else str(index)
        for index, item in enumerate(items)
    ]
    # `Counter` keeps the cost linear in a list whose length the browser chooses (bounded by the
    # model's `max_length` ceilings).
    repeated = {label for label, n in Counter(labels).items() if n > 1}
    seen: Counter[str] = Counter()
    resolved: list[str] = []
    for label in labels:
        if label in repeated:
            resolved.append(f"{label}#{seen[label]}")
            seen[label] += 1
        else:
            resolved.append(label)
    if len(set(resolved)) != len(resolved):
        return [(str(index), item) for index, item in enumerate(items)]
    return list(zip(resolved, items, strict=True))


#: The document's own order, which is the order `render_markdown` lays the page out in and the order
#: a reviewer reads a diff in.
_SECTION_ORDER: tuple[str, ...] = ("request", "base", "factors", "arms", "layout", "evidence")


def _reading_order(path: str) -> tuple[int, list[tuple[int, int, str]], str]:
    r"""Sort key putting a path where a reader expects it.

    Sections take the document's order and digit runs sort numerically, so `A2` precedes `A10`.
    Digit runs are read from the regex match, not `str.isdigit` (which accepts `'²'`, which
    `int` rejects), since segments are chemist-supplied text. The raw path is the final term so the
    key is a total order (`A1` vs `A01`) and the result is stable across hash seeds.
    """
    head, _, _ = path.partition(".")
    section = _SECTION_ORDER.index(head) if head in _SECTION_ORDER else len(_SECTION_ORDER)
    segments = [
        (0, int(digits), "") if (digits := match.group("digits")) else (1, 0, match.group())
        for segment in path.split(".")
        for match in _NATURAL.finditer(segment)
    ]
    return section, segments, path


#: Digit runs and non-digit runs, so a segment sorts as the alternating sequence it reads as.
_NATURAL = re.compile(r"(?P<digits>\d+)|\D+")


def diff_designs(
    before: ExperimentDesign,
    after: ExperimentDesign,
    *,
    from_revision: int = 0,
    to_revision: int = 0,
) -> DesignDiff:
    """Every path that differs between two designs, in path order."""
    left = flatten(before.model_dump(mode="json"))
    right = flatten(after.model_dump(mode="json"))
    changes: list[FieldChange] = []
    # Ordered after the comparison, not before: sorting every path in a ceiling-sized document
    # dominates the cost, and a subset of a totally ordered set keeps its order.
    # `tests/test_protocol_diff.py` bounds the diff against the cost of flattening.
    for path in set(left) | set(right):
        old, new = left.get(path), right.get(path)
        if path not in right:
            # An appearing or vanishing path whose value is empty is not a change (an all-default
            # sub-model
            # replacing `None` changes nothing a chemist can see). A value changing *to* empty is a
            # real
            # deletion and is kept below.
            if _render(old):
                changes.append(FieldChange(path=path, kind="removed", before=_render(old)))
        elif path not in left:
            if _render(new):
                changes.append(FieldChange(path=path, kind="added", after=_render(new)))
        elif old != new:
            changes.append(
                FieldChange(path=path, kind="changed", before=_render(old), after=_render(new))
            )
    changes.sort(key=lambda change: _reading_order(change.path))
    return DesignDiff(from_revision=from_revision, to_revision=to_revision, changes=changes)
