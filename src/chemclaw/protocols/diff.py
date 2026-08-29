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
            identifier = _KEYED_LISTS.get(path)
            for index, item in enumerate(value):
                label = (
                    str(item.get(identifier, index))
                    if identifier and isinstance(item, dict)
                    else str(index)
                )
                if isinstance(item, dict):
                    flat.update(flatten(item, f"{path}.{label}."))
                else:
                    flat[f"{path}.{label}"] = item
        else:
            flat[path] = value
    return flat


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
