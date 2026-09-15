"""Whether the knowledge a question rests on still holds.

A durable wait (`durable/awaiting.py`) can hold a question open for up to `awaiting_max_days` — 90
by default, and the BO case deliberately waits a week for plates. Over that span the corpus moves:
a note is superseded by a synthesis run (`memory/supersede.retire_note`), or a chemist reports
a failure that refutes it and the refutation is held until a date
(`memory/failure.close_refuted_note`).
Nothing asked whether any of that had happened before an answer was applied. So "approve running
these eight conditions" could be answered on Friday against a recommendation that was retired on
Tuesday, and the workflow would release exactly as though nothing had changed.

**This asks about state, not about history, and that is a deliberate narrowing of a question this
tree cannot answer.** There is no arrival signal for a note anywhere in this repository —
`valid_from`/`valid_to` are *valid* time at day granularity, and a note retired before a question
was asked reads identically to one retired since. Comparing a stored fingerprint across the wait
would answer "did it change", and would have to compare two readings taken on two pods whose
knowledge checkouts are refreshed independently by a sidecar and are routinely minutes apart — so a
sync landing between the ask and the answer would read as a change that never happened.

What makes the narrower question sufficient is that the *asking* side refuses too. A wait whose
premise is already broken is refused when it is opened
(`agent/pending_tools.request_external_input`),
so every wait that exists had a whole premise at ask time, and a break found at answer time is
therefore a change since the ask — established by construction rather than by a comparison.

**Only retirement and absence count, not suspected conflict.** `kg/conflicts.py` mixes *declared*
disagreement with `_suspected` heuristics, and a heuristic that refuses a chemist's approval is a
heuristic with authority it has not earned. A refutation that its reporter judged decisive already
closes the note it refutes (`close_refuted_note` sets `valid_to`), which is the case that matters
and arrives here as an ordinary retirement.
"""

import asyncio
from datetime import date

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.kg.graph import build_graph, note_in


class BrokenPremise(BaseModel):
    """One note a question rested on that no longer holds, and why."""

    note_id: str
    #: `retired` — the note has a `valid_to` in the past, so a synthesis superseded it or a reported
    #: failure closed it. `absent` — no note with this id resolves in the corpus at all.
    reason: str

    def describe(self) -> str:
        """One line naming the note and what happened to it, for a refusal a person reads."""
        if self.reason == "absent":
            return f"{self.note_id} is no longer in the knowledge base"
        return f"{self.note_id} has been superseded or refuted"


def _breaks(note_ids: list[str], as_of: date) -> list[BrokenPremise]:
    """The synchronous scan — one graph build, then a by-id lookup per premise note.

    `note_in` rather than `note_id in graph`, because `_assemble_graph` mints a bare node for every
    cited-but-undefined id: membership is `True` for exactly the ids that resolve to nothing, which
    is the opposite of the answer this function needs.
    """
    graph = build_graph(settings.knowledge_path)
    broken: list[BrokenPremise] = []
    for note_id in note_ids:
        note = note_in(graph, note_id)
        if note is None:
            broken.append(BrokenPremise(note_id=note_id, reason="absent"))
        elif not note.is_current(as_of):
            broken.append(BrokenPremise(note_id=note_id, reason="retired"))
    return broken


async def premise_breaks(note_ids: list[str], as_of: date | None = None) -> list[BrokenPremise]:
    """Which of these notes no longer hold; empty when the premise is whole (or when there is none).

    Offloaded, for the reason `durable/digest.py` records at its own corpus read: `build_graph` is a
    synchronous `rglob` + `stat` + parse behind a blocking `threading.RLock`, measured at 1,223.8 ms
    inline against 27.0 ms threaded on a 2,000-note corpus. This runs on the answer path of an HTTP
    route, so blocking the loop would stall every other request on the pod.

    An empty `note_ids` returns immediately **without touching the corpus**, which is what keeps a
    question citing no notes free rather than merely cheap — that is most questions, and paying a
    graph build for each of them would be a tax on a feature they do not use.
    """
    if not note_ids:
        return []
    return await asyncio.to_thread(_breaks, note_ids, as_of or date.today())
