"""The enrichment drain: find the rows nothing has labelled, label them, stamp them.

One bounded pass. `stale()` is a `WHERE labeller_version IS DISTINCT FROM $1` — so "as soon as
entries are identified that miss these things" is a query, and nothing anywhere has to remember to
mark a row as needing work. A new corpus arrives unlabelled and is found; an upgraded labeller
changes the version and the whole corpus is found again.

**Why a batch failure retries reaction by reaction.** `stale()` is deterministic — same `ORDER BY`,
same `LIMIT`, the same first batch on every attempt — so one reaction the server chokes on failed
this activity identically on every retry and stopped labelling *the entire corpus*, permanently.
That is not a hypothetical: it is what `ingest/documents/sync.py::reembed_stale` was changed to
prevent after one un-embeddable chunk stalled every share. The isolation is the same here, and so
is the rule that follows from it: a reaction that genuinely cannot be labelled is still **stamped**
with the current version, so it leaves the stale set instead of being retried forever. What it
carries is a row with nothing derived, which the coverage report counts honestly as unlabelled.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.labels.labeller import Labeller, ReactionNaming, ReactionRepresentation
from chemclaw.ingest.labels.merge import merge
from chemclaw.science.labels.policy import LabelPolicy
from chemclaw.science.labels.records import ReactionLabel
from chemclaw.science.labels.store import LabelIndex

logger = logging.getLogger(__name__)

# The half of a labelling answer a `_batch` call returns — a representation or a naming. One
# variable rather than two overloads, because the retry-per-reaction logic is identical for both
# and duplicating it is how the two halves end up degrading differently.
_T = TypeVar("_T")

# The policy applied to a row whose source declares none — everything derived, nothing trusted,
# which is the ordinary case rather than an edge one: of the sources in this tree exactly one
# declares a `labels:` block, and the other reaction corpora are ELNs that carry no labels at all.
# The drain therefore reads every source and looks the policy up per row. It used to narrow
# `stale()` to the declaring sources instead, which meant an ELN corpus was never labelled by any
# configuration — and the pass reported `has_more=False` while it happened. A `labels:` block says
# what a source *carries*; it is not permission to label it
# (`D-2026-08-25-a-label-is-derived-not-recorded`: `provides` is read for the coverage report and
# the `override` subset check, and nothing else).
_DERIVE_EVERYTHING = LabelPolicy()

# What this drain calls itself on `chemclaw_ingest_records_total{source}` and on its own
# `ingest.finished` record. Deliberately **not** the corpus source each row came from: those rows
# were already counted under that name when the ELN sync ingested them, and counting them again
# here would make one series mean two different passes over the same records. The labelling drain
# is its own ingest stage — it reads the labelling server, not a corpus — so it is its own source.
_LABEL_PASS = "labels"


# The index key of a row. The reaction id alone is not one, and that is the whole of why these
# two helpers exist: `reaction_labels` keys on `(source, reaction_id)` precisely because two ELNs
# may legitimately use one entry id, and `stale()` spans sources, so a single batch can hold both.
# Keying the labeller's answers on the bare id let the second overwrite the first and gave one
# reaction the other's atom map, named reaction and — positionally, via `merge._species` — the
# other's per-species roles. Silently, and stamped as cleanly labelled.
_Key = tuple[str, str]


def _key(row: ReactionLabel) -> _Key:
    """The index key a labelling answer has to be matched back to."""
    return (row.source, row.reaction_id)


def _token(index: int, row: ReactionLabel) -> str:
    """A correlation id for one call: unique within the batch, and readable in a server log.

    The server has no stake in our identity — it echoes whatever id it is handed — so what goes on
    the wire is a token for this call rather than the reaction's name. The position is what makes
    it unique; the reaction id rides along so a line in the server's own log still says what was
    being labelled.
    """
    return f"{index}:{row.reaction_id}"


class LabelReport(BaseModel):
    """What one drain pass did, in the shape the workflow's loop condition reads."""

    labelled: int = Field(default=0, ge=0, description="Rows stamped with the current version.")
    unlabelled: int = Field(
        default=0,
        ge=0,
        description=(
            "Rows stamped but carrying nothing derived — the server could not label them. Counted "
            "separately because a pass that stamps 200 rows and derives nothing is a broken "
            "labeller, and a pass that reports only `labelled` cannot say so."
        ),
    )
    has_more: bool = False


async def label_stale(
    index: LabelIndex,
    labeller: Labeller,
    policies: dict[str, LabelPolicy],
    version: str,
    limit: int,
) -> LabelReport:
    """Label one bounded batch of stale rows and stamp them `version`.

    Args:
        index: The label index to read stale rows from and write labels back to.
        labeller: The client for the labelling server.
        policies: Per-source label policy, from each source's `datasource.yaml`.
        version: The labeller version this pass stamps — read once in the planning activity and
            carried, never re-read here (D-093: a redeploy mid-drain would otherwise shift the
            stale set under the loop).
        limit: How many rows this pass may take.

    Returns:
        What was written, and whether more stale rows remain.
    """
    started = time.perf_counter()
    stale = await index.stale(version, limit)
    if not stale:
        # Reported, not returned in silence. "Nothing was stale" is the steady state and it is also
        # what a broken `stale()` predicate looks like, and until this the two were the same
        # absence of output — a clean pass logged nothing at all.
        _record_pass(
            labelled=0, unlabelled=0, has_more=False, duration_s=time.perf_counter() - started
        )
        return LabelReport()

    representations, namings = await _label(labeller, stale)
    labelled = 0
    unlabelled = 0
    for row in stale:
        policy = policies.get(row.source, _DERIVE_EVERYTHING)
        representation = representations.get(_key(row))
        naming = namings.get(_key(row))
        await index.store_labels(merge(row, policy, representation, naming), version)
        labelled += 1
        # "Unlabelled" is *the server answered for neither half*, read off the answers rather than
        # inferred from the merged row — because a merged row always has roles: `_species` falls
        # back to the coarse map of what the source recorded, deliberately, and a check on the
        # stored value would therefore report every failure as a success.
        if representation is None and naming is None:
            unlabelled += 1
    if unlabelled:
        logger.warning(
            "%d of %d reaction(s) were stamped with nothing derived; they leave the stale set so "
            "the drain can advance, and the coverage report counts them as unlabelled",
            unlabelled,
            labelled,
        )
    report = LabelReport(labelled=labelled, unlabelled=unlabelled, has_more=len(stale) == limit)
    _record_pass(
        labelled=report.labelled,
        unlabelled=report.unlabelled,
        has_more=report.has_more,
        duration_s=time.perf_counter() - started,
    )
    return report


def _record_pass(*, labelled: int, unlabelled: int, has_more: bool, duration_s: float) -> None:
    """Emit the one record this drain leaves behind, whatever the pass did.

    **A clean pass used to log nothing**: only the two failure paths spoke, so "the drain is
    keeping up" and "the drain has not run since Tuesday" were the same silence. The outcomes split
    the stamped rows by whether anything was actually derived — `rejected` is a row the server
    answered for with neither half, which the module's own docstring calls out as the population a
    `labelled` count alone cannot report.
    """
    _count_records("ingested", labelled - unlabelled)
    _count_records("rejected", unlabelled)
    log_event(
        logger,
        "ingest.finished",
        "labels: labelled=%d unlabelled=%d in %.3fs",
        labelled,
        unlabelled,
        duration_s,
        source=_LABEL_PASS,
        labelled=labelled,
        unlabelled=unlabelled,
        has_more=has_more,
        duration_s=round(duration_s, 3),
    )


def _count_records(outcome: str, count: int) -> None:
    """Add `count` to the label drain's tally of one outcome (a named function; see `sync.py`'s)."""
    record_metric(
        lambda m: m.increment(
            "chemclaw_ingest_records_total", count, {"source": _LABEL_PASS, "outcome": outcome}
        )
    )


async def _label(
    labeller: Labeller, stale: list[ReactionLabel]
) -> tuple[dict[_Key, ReactionRepresentation], dict[_Key, ReactionNaming]]:
    """Both halves for a whole batch, degrading to per-reaction calls when the batch fails.

    The two halves are independent on purpose: a reaction the atom mapper cannot handle may still
    be named, so a failure of one must not cost the other. And a batch failure is retried one
    reaction at a time rather than abandoned, because `stale()` is deterministic and abandoning
    would mean this batch — and therefore every batch behind it — never completes.
    """
    representations = await _batch(
        lambda rows: labeller.represent(
            [(token, r.record_smiles, [s.smiles for s in r.species]) for token, r in rows]
        ),
        stale,
        "represent",
    )
    namings = await _batch(
        lambda rows: labeller.name([(token, r.record_smiles) for token, r in rows]),
        stale,
        "name",
    )
    return representations, namings


async def _batch(
    call: Callable[[list[tuple[str, ReactionLabel]]], Awaitable[dict[str, _T]]],
    stale: list[ReactionLabel],
    what: str,
) -> dict[_Key, _T]:
    """Run one batch call, falling back to one call per reaction if the batch is refused.

    Each row is tagged with a correlation token before the call and the answers are placed back on
    their rows afterwards, so what the caller receives is keyed by the index key rather than by
    whatever id went over the wire.

    Only `ChemclawError` is caught — the bad-data contract. A `LabelServerError` is an outage and
    must propagate, so Temporal retries the activity instead of this drain making 200 doomed
    single-reaction calls against a server that is not there.
    """
    tagged = [(_token(index, row), row) for index, row in enumerate(stale)]
    rows = dict(tagged)
    try:
        return _placed(await call(tagged), rows, what)
    except ChemclawError as exc:
        logger.warning(
            "batch %s failed (%s); retrying %d reaction(s) individually", what, exc, len(stale)
        )
    answered: dict[_Key, _T] = {}
    for pair in tagged:
        try:
            answered.update(_placed(await call([pair]), rows, what))
        except ChemclawError as exc:
            # `%r` on the id because it is external text: repr escapes the control characters that
            # would otherwise let a corpus row forge a log line.
            logger.warning("could not %s reaction %r: %s", what, pair[1].reaction_id, exc)
    return answered


def _placed(answers: dict[str, _T], rows: dict[str, ReactionLabel], what: str) -> dict[_Key, _T]:
    """Re-key one call's answers from their correlation tokens onto the rows they belong to.

    A token this batch did not send is dropped with a warning rather than raised on: the server is
    versioned separately from this repository, and one answer we cannot place is not a reason to
    lose the ones we can.
    """
    placed: dict[_Key, _T] = {}
    for token, answer in answers.items():
        row = rows.get(token)
        if row is None:
            logger.warning(
                "%s: server answered for id %r, which this batch did not send", what, token
            )
            continue
        placed[_key(row)] = answer
    return placed
