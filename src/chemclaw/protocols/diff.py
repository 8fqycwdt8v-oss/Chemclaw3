"""What changed between two revisions of a design.

**This is the product, not a debugging aid.** The first shot at a protocol is almost always
altered by the chemist who runs it, and that alteration is the single most informative thing this
system can observe about its own suggestions — it is a labelled correction, made by the person with
the most context, at the moment they had it. Storing revisions without being able to say what moved
would keep the data and throw away the signal.

Flattened to dotted paths rather than compared as a tree, because that is the form both consumers
want: a UI puts a marker next to one field, and a later miner asks "how often does the chemist
change `base.setpoints.temperature_c`, and in which direction". A structural tree diff answers
neither without being flattened first.
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

    A list named in `_KEYED_LISTS` is keyed by its member's own identifier (`arms.A1.control`)
    rather than by position, so reordering a plate is not read as rewriting it. Every other list is
    keyed by index, which is right where position *is* the identity — `base.steps.0.text` is the
    first instruction and stays the first instruction.

    **`None` is an absent path, not a leaf holding `None`, and the difference inverted a word on
    the product surface.** An optional sub-model stored as a scalar leaf meant `layout: None`
    produced the path `layout`, while a populated one produced `layout.rows`, `layout.wells.A1.…`
    and no `layout` at all — so the set difference reported *adding* a plate as `layout removed`
    and removing one as `layout added`. It also filled a diff with rows carrying no information:
    87% of the paths for a per-arm setpoint were `'' -> ''`, which a miner asking "how often does a
    chemist change this field" counts as changes that never happened.
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

    **The disambiguation is the whole of this function, and it is a data-loss fix rather than a
    tidy-up.** Keying by a member's own identifier is what makes a reordered plate diff as no
    change, but nothing guarantees the identifier is unique: `base.charge` is keyed by `component`,
    and a solvent charged in two portions — an addition and a rinse — is entirely ordinary.
    Measured, the second line silently overwrote the first, so a chemist editing the *first* toluene
    charge from 5 mL to 9 mL was recorded as the *second* moving 2 mL to 9 mL: not a lost row but a
    misattributed edit, in the one table this system keeps precisely to learn from those edits.

    **The ordinal counts within the key, not within the list, and that is the correction to the
    first fix.** `<label>#<position>` replaced the overwrite with a different misattribution:
    positions shift, so deleting an *unrelated* line renumbered both toluenes and the diff reported
    "toluene volume 5.0 → 2.0", an edit nobody made — 33 paths for a one-line deletion. Counting
    within the key makes the two lines `toluene#0` and `toluene#1` whatever else is added or
    removed around them, so an unrelated edit is not attributed to them at all.

    A key that is still not unique after that — a component literally named `toluene#1` beside two
    called `toluene` — makes the whole list positional. That loses reorder-freeness for that one
    list and is the only answer that cannot silently merge two members, which is the property worth
    keeping.
    """
    if identifier is None:
        return [(str(index), item) for index, item in enumerate(items)]
    labels = [
        str(item.get(identifier, index)) if isinstance(item, dict) else str(index)
        for index, item in enumerate(items)
    ]
    # `Counter`, not `labels.count(label)` in a comprehension, which is O(n²) over a list whose
    # length a browser chooses through `POST /protocols/{id}/revisions`. That scan measured **46 s
    # of blocked event loop** for one authenticated request — but on a payload that predated the
    # `max_length` ceilings and can no longer be posted, so **the ceilings are what closed that,
    # not this line.** At the largest list those ceilings now admit (1536, a full plate) the scan
    # costs 22.4 ms against this `Counter`'s 0.107 ms, and the whole largest legal design diffs in
    # 0.060 s. The right claim for `Counter` here is that it keeps a bounded cost flat rather than
    # quadratic in the bound; the earlier comment credited it with the 46 s, which was false.
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

    Plain `sorted` is lexicographic, which `paths`'s docstring called "reading order" and is not:
    it interleaves the sections alphabetically (`arms` before `base` before `request`) and orders
    twelve arms `A1, A10, A11, A12, A2, …`, so a reviewer scanning a 96-arm diff meets arm 10
    between arm 1 and arm 2. Sections take the document's order and each segment sorts its digit
    runs numerically, which is what makes `A2` precede `A10`.

    **Which run is a number is the regex's answer and not `str.isdigit`'s**, because the two
    disagree and a path segment is chemist-supplied text. `'²'.isdigit()` is `True` and `int('²')`
    raises, while `\D` matches `'²'` — so a factor named `²` reached `int()` through the
    non-digit branch and raised `ValueError` out of `diff_designs`, which is a 500 on the diff
    route from a name somebody typed. Reading the group the match came from cannot disagree with
    the pattern that produced it: whatever `\d+` matched, `int` accepts.

    **The raw path is the last term, because the key before it is not a total order.** `A1` and
    `A01` produce identical keys (`int('01') == int('1')`, and the residual text is empty in both),
    so two distinct paths compared equal and their order fell to `sorted`'s stability over a
    **set**, whose iteration order varies with the interpreter's hash seed. The same diff of the
    same two revisions listed its rows in a different order from one process to the next.
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
    for path in sorted(set(left) | set(right), key=_reading_order):
        old, new = left.get(path), right.get(path)
        if path not in right:
            # **An appearing or vanishing path whose value is empty is not a change.** `flatten`
            # skips `None` for exactly this reason and empty-string leaves were never skipped, so
            # the same rows survived on the same field family: replacing an arm's `setpoints: None`
            # with an all-default `Setpoints()` changes nothing a chemist can see — `setpoints_for`
            # returns the identical resolved conditions — and produced
            # `added arms.A1.setpoints.solvent : '' -> ''`. A miner asking how often a chemist
            # changes a field counts those as changes that never happened. A path whose value
            # *changes* to empty is a real deletion and is kept, below.
            if _render(old):
                changes.append(FieldChange(path=path, kind="removed", before=_render(old)))
        elif path not in left:
            if _render(new):
                changes.append(FieldChange(path=path, kind="added", after=_render(new)))
        elif old != new:
            changes.append(
                FieldChange(path=path, kind="changed", before=_render(old), after=_render(new))
            )
    return DesignDiff(from_revision=from_revision, to_revision=to_revision, changes=changes)
