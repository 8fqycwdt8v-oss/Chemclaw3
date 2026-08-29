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
        """The changed paths, in reading order."""
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
    """
    flat: dict[str, Any] = {}
    for key, value in document.items():
        path = f"{prefix}{key}"
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
    """Each member with the key it is flattened under, falling back to its index on a repeat.

    **The fallback is the whole of this function, and it is a data-loss fix rather than a tidy-up.**
    Keying by a member's own identifier is what makes a reordered plate diff as no change, but
    nothing guarantees the identifier is unique: `base.charge` is keyed by `component`, and a
    solvent charged in two portions — an addition and a rinse — is entirely ordinary. Measured, the
    second line silently overwrote the first, so a chemist editing the *first* toluene charge from
    5 mL to 9 mL was recorded as the *second* moving 2 mL to 9 mL: not a lost row but a
    misattributed edit, in the one table this system keeps precisely to learn from those edits.

    A repeated key falls back to `<label>#<index>` for **every** member sharing it, not only the
    later ones, so the two lines are `toluene#0` and `toluene#1` rather than `toluene` and
    `toluene#1` — a reorder of an unambiguous list still diffs as nothing, and an ambiguous one
    diffs by position, which is the only identity it has.
    """
    if identifier is None:
        return [(str(index), item) for index, item in enumerate(items)]
    labels = [
        str(item.get(identifier, index)) if isinstance(item, dict) else str(index)
        for index, item in enumerate(items)
    ]
    repeated = {label for label in labels if labels.count(label) > 1}
    return [
        (f"{label}#{index}" if label in repeated else label, item)
        for index, (label, item) in enumerate(zip(labels, items, strict=True))
    ]


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
    for path in sorted(set(left) | set(right)):
        old, new = left.get(path), right.get(path)
        if path not in right:
            changes.append(FieldChange(path=path, kind="removed", before=_render(old)))
        elif path not in left:
            changes.append(FieldChange(path=path, kind="added", after=_render(new)))
        elif old != new:
            changes.append(
                FieldChange(path=path, kind="changed", before=_render(old), after=_render(new))
            )
    return DesignDiff(from_revision=from_revision, to_revision=to_revision, changes=changes)
