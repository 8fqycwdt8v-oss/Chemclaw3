"""Whether the tree and the Postgres store hold the same notes, and putting Postgres right.

The two stores are compared on what each *serves*: the canonical note (`rows.canonical_note`, the
note as its redacted file text reads back) and the edges the note asserts. Never on a digest, since
`content_sha` is of the canonical render and a hand-laid file has other bytes for the same note.
A difference names the note and what differs: it is missing from one side, its fields differ, its
validity window differs, or its edges do. The tree is the authority while both are written
(`kg_store=dual`), so a repair copies the tree's note into Postgres and never the other way.

Invariants: a note present on one side only is reported, never guessed at; a difference must survive
a second look after `settle_seconds` to be reported, so a write another pod has not synced yet is
not a divergence; repair refuses what it cannot decide (a note only Postgres holds, a human's note
an agent's copy would replace).
"""

import logging
import time
from asyncio import sleep
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

import networkx as nx

from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, NoteError
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.rows import canonical_note, content_sha
from chemclaw.kg.store import NoteStore, NoteUnit, NoteWriteError

log = logging.getLogger(__name__)

#: The ways one note can differ, in the order a report lists them.
KINDS = ("missing_in_postgres", "missing_in_git", "canonical", "validity", "edges")

_WINDOW = frozenset({"valid_from", "valid_to"})
_Edge = tuple[str, str, object, object, object]

# The last published count per kind, read by `chemclaw_kg_compare_differences`, and when it was
# published, read by `chemclaw_kg_compare_last_run_timestamp_seconds` (0 until this process has
# completed a comparison). Kept here and not in the workflow module, which Temporal's sandbox
# imports again and would rebind the gauges to empty copies. The counts are not seeded: a process
# that has not compared publishes no series rather than an invented clean day. Replicas each hold
# their own pair, so a reader selects the series of the newest timestamp.
_PUBLISHED: dict[str, float] = {}
_LAST_RUN = [0.0]

record_metric(lambda m: m.bind_gauge_family("chemclaw_kg_compare_differences", lambda: _PUBLISHED))
record_metric(
    lambda m: m.bind_gauge("chemclaw_kg_compare_last_run_timestamp_seconds", lambda: _LAST_RUN[0])
)


@dataclass(frozen=True)
class Difference:
    """One note the stores disagree about: its id, the kinds of difference, and the fields.

    `fields` names the `Note` fields that differ (never their values, which may be long) or, for a
    note missing from a side, `unparseable` when that side has a file or row that fails the schema.
    """

    note_id: str
    kinds: tuple[str, ...]
    fields: tuple[str, ...] = ()
    #: The digest of the note Postgres held when it was compared (`None`: it held none), so a repair
    #: can tell that the note moved since.
    postgres_sha: str | None = None


@dataclass(frozen=True)
class Comparison:
    """What a comparison found: how many notes each side served and every difference, by id."""

    git_notes: int
    postgres_notes: int
    differences: tuple[Difference, ...]

    @property
    def clean(self) -> bool:
        """Whether the two stores serve the same notes and edges."""
        return not self.differences

    def counts(self) -> dict[str, int]:
        """Notes per kind of difference, every kind present (a clean kind is 0)."""
        return {
            kind: sum(1 for difference in self.differences if kind in difference.kinds)
            for kind in KINDS
        }

    def as_json(self) -> dict[str, Any]:
        """The report as plain JSON values."""
        return {
            "clean": self.clean,
            "git_notes": self.git_notes,
            "postgres_notes": self.postgres_notes,
            "counts": self.counts(),
            "differences": [
                {"id": d.note_id, "kinds": list(d.kinds), "fields": list(d.fields)}
                for d in self.differences
            ],
        }


@dataclass(frozen=True)
class Repair:
    """What a repair did: the ids put right, and the ids it left with the reason for each."""

    repaired: tuple[str, ...]
    unrepaired: tuple[tuple[str, str], ...]

    def as_json(self) -> dict[str, Any]:
        """The result as plain JSON values."""
        return {
            "repaired": list(self.repaired),
            "unrepaired": [{"id": note_id, "reason": why} for note_id, why in self.unrepaired],
        }


def _edges_by_source(graph: nx.DiGraph) -> dict[str, set[_Edge]]:
    """Every typed edge of `graph` as `(rel, dst, valid_from, valid_to, confidence)` per source."""
    found: dict[str, set[_Edge]] = {}
    for src, dst, data in graph.edges(data=True):
        for relation in data["relations"]:
            found.setdefault(src, set()).add(
                (relation.rel, dst, relation.valid_from, relation.valid_to, relation.confidence)
            )
    return found


def _differing_fields(served: Note, stored: Note) -> tuple[str, ...]:
    """The `Note` fields whose values differ, by name."""
    left, right = served.model_dump(mode="json"), stored.model_dump(mode="json")
    return tuple(sorted(name for name in left if left[name] != right.get(name)))


def _difference(
    note_id: str,
    tree: Note,
    stored: Note,
    tree_edges: set[_Edge],
    stored_edges: set[_Edge],
) -> Difference | None:
    """How `tree`'s note, as Postgres would hold it, differs from `stored`, or `None`."""
    kinds: list[str] = []
    fields: tuple[str, ...]
    try:
        fields = _differing_fields(canonical_note(tree), stored)
    except NoteError:
        # The tree's note cannot be stored at all (redaction leaves a value the schema refuses).
        fields = ("unstorable",)
    if set(fields) & _WINDOW:
        kinds.append("validity")
    if set(fields) - _WINDOW:
        kinds.append("canonical")
    if tree_edges != stored_edges:
        kinds.append("edges")
    return Difference(note_id, tuple(kinds), fields, content_sha(stored)) if kinds else None


def publish_differences(comparison: Comparison) -> None:
    """Make the gauges read this comparison's count of each kind, and the time it completed."""
    _PUBLISHED.update({kind: float(count) for kind, count in comparison.counts().items()})
    _LAST_RUN[0] = time.time()


async def _unparseable(store: NoteStore, note_ids: Iterable[str]) -> set[str]:
    """Which of `note_ids` `store` holds in a form that fails the schema."""
    return set(await store.unparseable(list(note_ids)))


async def compare_once(tree: NoteStore, postgres: NoteStore) -> Comparison:
    """Compare every note and edge of the two stores as they are now, with no second look.

    Reads each store's notes and graph whole, so memory is the corpus twice; a nightly job, not a
    request.
    """
    git_notes = {note.id: note for note in await tree.notes()}
    stored_notes = {note.id: note for note in await postgres.notes()}
    tree_edges = _edges_by_source(await tree.graph())
    stored_edges = _edges_by_source(await postgres.graph())
    only_tree = sorted(git_notes.keys() - stored_notes.keys())
    only_stored = sorted(stored_notes.keys() - git_notes.keys())
    # A file that fails the schema is "missing" from the notes a store serves; say so.
    broken_tree = await _unparseable(tree, only_stored)
    broken_stored = await _unparseable(postgres, only_tree)
    differences = [
        *(
            Difference(i, ("missing_in_postgres",), ("unparseable",) if i in broken_stored else ())
            for i in only_tree
        ),
        *(
            Difference(i, ("missing_in_git",), ("unparseable",) if i in broken_tree else ())
            for i in only_stored
        ),
    ]
    for note_id in sorted(git_notes.keys() & stored_notes.keys()):
        found = _difference(
            note_id,
            git_notes[note_id],
            stored_notes[note_id],
            tree_edges.get(note_id, set()),
            stored_edges.get(note_id, set()),
        )
        if found is not None:
            differences.append(found)
    return Comparison(
        len(git_notes), len(stored_notes), tuple(sorted(differences, key=lambda d: d.note_id))
    )


async def compare_stores(
    tree: NoteStore, postgres: NoteStore, *, settle_seconds: float = 0.0
) -> Comparison:
    """`compare_once`, then, if anything differs, again after `settle_seconds`: only what persists.

    A pod's clone of the tree lags a write another pod made by its sync interval, and a write is in
    the tree a moment before it is in Postgres. Both look like a difference at one instant and
    neither survives a pause longer than the sync, so a difference is reported only when the second
    look finds that note different too, with the second look's detail.
    """
    first = await compare_once(tree, postgres)
    if first.clean or settle_seconds <= 0:
        return first
    log.info(
        "kg.compare: %d difference(s); looking again in %.0fs before reporting",
        len(first.differences),
        settle_seconds,
    )
    await sleep(settle_seconds)
    await tree.refresh(reparse=True)
    await postgres.refresh()
    second = await compare_once(tree, postgres)
    first_ids = {difference.note_id for difference in first.differences}
    return replace(
        second,
        differences=tuple(d for d in second.differences if d.note_id in first_ids),
    )


async def repair_from_tree(
    tree: NoteStore, postgres: PostgresNoteStore, differences: Iterable[Difference]
) -> Repair:
    """Put the tree's version of every differing note into Postgres, and say what could not be.

    A note only Postgres holds is left: the tree is the authority but nothing here deletes a note,
    so someone decides whether it is a write the tree lost or one Postgres should not have. A note
    the store refuses (an agent's copy over a human's note) is left with the store's reason. A note
    that changed in Postgres since it was compared is left too, and the put itself is conditional
    on the revision read just before, so a write that lands between the two is refused rather than
    overwritten. The caller owes the check that the tree is current
    (`GitNoteWriter.lagging_reason`): a repair from a clone behind the remote would put old notes
    over newer ones. A repair is a `put`, so repeating it changes nothing.
    """
    arrivals = await tree.arrivals()
    repaired: list[str] = []
    unrepaired: list[tuple[str, str]] = []
    for difference in differences:
        if "missing_in_git" in difference.kinds:
            unrepaired.append(
                (
                    difference.note_id,
                    "only Postgres holds it; the tree is the authority and no repair deletes it",
                )
            )
            continue
        note = await tree.get(difference.note_id)
        if note is None:
            unrepaired.append((difference.note_id, "the tree does not serve it (unparseable?)"))
            continue
        revision = await postgres.note_revision(difference.note_id)
        held = await postgres.get(difference.note_id)
        if (None if held is None else content_sha(held)) != difference.postgres_sha:
            unrepaired.append(
                (difference.note_id, "Postgres changed it after the comparison; compare again")
            )
            continue
        try:
            await postgres.put(
                NoteUnit(note, expected_revision=revision, arrived=arrivals.get(note.id))
            )
        except (NoteWriteError, NoteError) as exc:
            unrepaired.append((difference.note_id, f"{type(exc).__name__}: {exc}"))
            continue
        repaired.append(difference.note_id)
    return Repair(tuple(repaired), tuple(unrepaired))
