"""Whether the knowledge a question rests on still holds.

A durable wait (`durable/awaiting.py`) can hold a question open for weeks while the notes it cites
are superseded or refuted. This checks current state, not history: comparing stored fingerprints
across pods with independently synced checkouts would report spurious changes. State suffices
because a wait whose premise is already broken is refused when opened
(`agent/pending_tools.request_external_input`), so a break found at answer time happened since.

Only retirement and absence count, not suspected conflicts, which are heuristics without the
authority to refuse an approval. A decisive refutation already closes the note's `valid_to`.
"""

import asyncio
from datetime import date
from functools import partial

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.metrics import Metrics
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.graph import build_graph, note_in
from chemclaw.kg.note import resolves_outside_graph


class BrokenPremise(BaseModel):
    """One note a question rested on that no longer holds, and why."""

    note_id: str
    #: `retired`: `valid_to` is in the past (superseded or refuted). `not-yet-valid`: `valid_from`
    #: is in the future, a different fact that `is_current` reports identically. `absent`: no note
    #: with this id resolves in the corpus.
    reason: str

    def describe(self) -> str:
        """One line naming the note and what happened to it, for a refusal a person reads."""
        if self.reason == "absent":
            return f"{self.note_id} is no longer in the knowledge base"
        if self.reason == "not-yet-valid":
            return f"{self.note_id} does not take effect until a later date"
        return f"{self.note_id} has been superseded or refuted"

    def blocks_an_answer(self) -> bool:
        """Whether this break may refuse an answer, as opposed to only refusing the ask.

        Only `retired` and `not-yet-valid`, where a present note says of itself that it does not
        hold. `absent` cannot distinguish a deleted note from a replica whose checkout lacks it (a
        wedged sync or empty mount yields an empty graph), so refusing an answer on it would err in
        the wrong direction with no override. The ask still refuses on `absent`, where the model can
        correct its citation.
        """
        return self.reason != "absent"


def _breaks(note_ids: list[str], as_of: date) -> list[BrokenPremise]:
    """The synchronous scan: one graph build, then a by-id lookup per premise note.

    Uses `note_in`, since graph membership is true for cited-but-undefined ids. External citations
    (`[[reaction-...]]`) name store rows, not notes, so they are skipped rather than reported
    `absent`; judging them would mean asking the record store, a different question.
    """
    graph = build_graph(settings.knowledge_path)
    broken: list[BrokenPremise] = []
    for note_id in note_ids:
        if resolves_outside_graph(note_id):
            continue
        note = note_in(graph, note_id)
        if note is None:
            broken.append(BrokenPremise(note_id=note_id, reason="absent"))
        elif not note.is_current(as_of):
            # `is_current` is false at both ends of the window; a note that takes effect later has
            # not been superseded.
            future = note.valid_from is not None and as_of < note.valid_from
            reason = "not-yet-valid" if future else "retired"
            broken.append(BrokenPremise(note_id=note_id, reason=reason))
    return broken


async def premise_breaks(note_ids: list[str], as_of: date | None = None) -> list[BrokenPremise]:
    """Which of these notes no longer hold; empty when the premise is whole (or when there is none).

    Offloaded to a thread because `build_graph` is synchronous and this runs on an HTTP answer path.
    An empty `note_ids` returns without touching the corpus.
    """
    if not note_ids:
        return []
    return await asyncio.to_thread(_breaks, note_ids, as_of or date.today())


def _increment(metrics: Metrics, *, end: str, reason: str) -> None:
    """One labelled increment, as a partial rather than a closure over a loop variable."""
    metrics.increment("chemclaw_premise_refusals_total", labels={"end": end, "reason": reason})


def count_refusals(end: str, breaks: list[BrokenPremise]) -> None:
    """Count a premise refusal, once per broken note, at the end (`ask`/`answer`) that refused.

    In one place so both ends use the same labels, making refusals visible as a series.
    """
    for item in breaks:
        record_metric(partial(_increment, end=end, reason=item.reason))
