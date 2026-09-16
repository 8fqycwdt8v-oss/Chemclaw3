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
    #: `retired` — the note has a `valid_to` in the past, so a synthesis superseded it or a reported
    #: failure closed it. `not-yet-valid` — it has a `valid_from` in the future, which `is_current`
    #: reports identically and which is not the same fact at all. `absent` — no note with this id
    #: resolves in the corpus at all.
    reason: str

    def describe(self) -> str:
        """One line naming the note and what happened to it, for a refusal a person reads."""
        if self.reason == "absent":
            return f"{self.note_id} is no longer in the knowledge base"
        if self.reason == "not-yet-valid":
            return f"{self.note_id} does not take effect until a later date"
        return f"{self.note_id} has been superseded or refuted"

    def blocks_an_answer(self) -> bool:
        """Whether this break may refuse an answer, as opposed to only refusing the *ask*.

        **Only the arms that are self-validating, which `absent` is not.** A `retired` or
        `not-yet-valid` break requires the note to be *present* and to say of itself that it does
        not hold now; the corpus has been read and it answered. `absent` is the absence of an
        answer, and this module cannot tell "the note was deleted" from "this replica's checkout
        has not got it yet" — a wedged knowledge sidecar, a note pushed with broken frontmatter, or
        a PVC that mounted empty all produce it, and `build_graph` returns an *empty graph* rather
        than raising for a missing directory, so a whole missing corpus arrives here as every
        premise being `absent`.

        Refusing on that is the wrong failure direction, and it contradicted this feature's own
        stated one: the ADR argues that "a stale replica admits an answer it would later refuse,
        rather than refusing one it should admit", which is true of `retired` and was exactly
        backwards for `absent`. Measured, a pod whose checkout simply lacked the note answered a
        chemist's approval with a 409 reading "the knowledge this question rests on has changed
        since it was asked" when nothing had changed at all — and the route has no override.

        The *ask* still refuses on `absent`, and should: there the model is the one being told, it
        gets the error text back, and it can rewrite the citation. That is a self-correcting loop
        with a model in it; the answer-time 409 is a dead end with a chemist in it.
        """
        return self.reason != "absent"


def _breaks(note_ids: list[str], as_of: date) -> list[BrokenPremise]:
    """The synchronous scan — one graph build, then a by-id lookup per premise note.

    `note_in` rather than `note_id in graph`, because `_assemble_graph` mints a bare node for every
    cited-but-undefined id: membership is `True` for exactly the ids that resolve to nothing, which
    is the opposite of the answer this function needs.

    **An external citation is skipped, not judged.** `[[reaction-…]]` and its siblings name a row in
    a record store rather than a note in the graph, so they resolve to nothing here and were being
    reported `absent` — which refused the questions this tool exists for, since `measurement` is
    `request_external_input`'s default kind and "confirm the yield reported for [[reaction-abc]]"
    is its archetypal use. Both of this tree's other readers of a citation exempt them first
    (`kg.graph.dangling_links`, which `kg-validate` fails a merge on, and
    `agent.graph_tools.expand_note`, which falls back to the store), and `dangling_links` says in
    as many words that without the exemption "every campaign and optimization note would be
    reported broken for links that resolve". This function had become the counter-example to that
    sentence. Judging them properly would mean asking the record store whether a row is retracted,
    which is a different question against a different backend; skipping is the honest narrowing.
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
            # `is_current` is false at *both* ends of the validity window, and the two ends are
            # different facts: telling a chemist a standing note "has been superseded or refuted"
            # when it simply does not take effect until next month is a false statement about
            # their own knowledge base.
            future = note.valid_from is not None and as_of < note.valid_from
            reason = "not-yet-valid" if future else "retired"
            broken.append(BrokenPremise(note_id=note_id, reason=reason))
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


def _increment(metrics: Metrics, *, end: str, reason: str) -> None:
    """One labelled increment, as a partial rather than a closure over a loop variable."""
    metrics.increment("chemclaw_premise_refusals_total", labels={"end": end, "reason": reason})


def count_refusals(end: str, breaks: list[BrokenPremise]) -> None:
    """Count a premise refusal, once per broken note, at the end (`ask`/`answer`) that refused.

    Here rather than at each call site so the two ends cannot drift into different label values,
    and because without it neither refusal was visible at all: a 409 left nothing but a generic
    `http.request ... 409` line and the ask-time refusal left only a tool error. Two of the ways
    this guard fired were spurious and there was no series on which either could have been noticed
    — which is the "checkable rather than believed" standard this tree applies to its own controls.
    """
    for item in breaks:
        record_metric(partial(_increment, end=end, reason=item.reason))
