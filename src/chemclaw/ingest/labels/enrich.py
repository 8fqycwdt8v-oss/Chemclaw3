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
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
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

# The policy applied to a row whose source declares none — everything derived, nothing trusted.
# A row can only exist because something recorded it, so this is reachable exactly when a source
# was disabled after its rows were written; deriving is the safe answer, because the alternative is
# leaving those rows out of every facet answer with no way for an operator to see why.
_DERIVE_EVERYTHING = LabelPolicy()


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
    stale = await index.stale(version, limit, sources=sorted(policies) or None)
    if not stale:
        return LabelReport()

    representations, namings = await _label(labeller, stale)
    labelled = 0
    unlabelled = 0
    for row in stale:
        policy = policies.get(row.source, _DERIVE_EVERYTHING)
        representation = representations.get(row.reaction_id)
        naming = namings.get(row.reaction_id)
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
    return LabelReport(labelled=labelled, unlabelled=unlabelled, has_more=len(stale) == limit)


async def _label(
    labeller: Labeller, stale: list[ReactionLabel]
) -> tuple[dict[str, ReactionRepresentation], dict[str, ReactionNaming]]:
    """Both halves for a whole batch, degrading to per-reaction calls when the batch fails.

    The two halves are independent on purpose: a reaction the atom mapper cannot handle may still
    be named, so a failure of one must not cost the other. And a batch failure is retried one
    reaction at a time rather than abandoned, because `stale()` is deterministic and abandoning
    would mean this batch — and therefore every batch behind it — never completes.
    """
    representations = await _batch(
        lambda rows: labeller.represent(
            [(r.reaction_id, r.record_smiles, [s.smiles for s in r.species]) for r in rows]
        ),
        stale,
        "represent",
    )
    namings = await _batch(
        lambda rows: labeller.name([(r.reaction_id, r.record_smiles) for r in rows]),
        stale,
        "name",
    )
    return representations, namings


async def _batch(
    call: Callable[[list[ReactionLabel]], Awaitable[dict[str, _T]]],
    stale: list[ReactionLabel],
    what: str,
) -> dict[str, _T]:
    """Run one batch call, falling back to one call per reaction if the batch is refused.

    Only `ChemclawError` is caught — the bad-data contract. A `LabelServerError` is an outage and
    must propagate, so Temporal retries the activity instead of this drain making 200 doomed
    single-reaction calls against a server that is not there.
    """
    try:
        return await call(stale)
    except ChemclawError as exc:
        logger.warning(
            "batch %s failed (%s); retrying %d reaction(s) individually", what, exc, len(stale)
        )
    answered: dict[str, _T] = {}
    for row in stale:
        try:
            answered.update(await call([row]))
        except ChemclawError as exc:
            # `%r` on the id because it is external text: repr escapes the control characters that
            # would otherwise let a corpus row forge a log line.
            logger.warning("could not %s reaction %r: %s", what, row.reaction_id, exc)
    return answered
