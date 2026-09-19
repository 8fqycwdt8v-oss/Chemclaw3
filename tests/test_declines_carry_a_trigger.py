"""A refusal that cannot expire is stronger than a deferral and, unlike one, states no way out.

`docs/planning/DEFERRED.md` has always carried a **Trigger to revisit** column: a postponement is
only a postponement if somebody can tell when it stops applying. The ADR record has no such column
and has never needed one by convention, so the stronger act — *declining* a class of future work,
permanently, in a document stamped `accepted` that may never be edited — is the one written with no
condition on it. Audited on 2026-09-19 over `docs/decisions/`: **71** ADRs decline a class of work
and **4** state a condition for revisiting. That audit's detector was broader than the one that
ships below, which is the honest way round — the finding is the motive, `_DECLINES` is the bound,
and the two are not the same measurement. Under `_DECLINES` on the day it was written: 43 files
decline, 1 carries the marker.

The sharper half of that finding is that the condition is the cheap part and watching it is the
expensive one. `D-092` did state its reopening condition, precisely — *"revisit only if a deployment
vendors the weight files into the container image at build time"* — and it **was met**, in the
sibling fleet: `servers/rxnpredict/Containerfile` and `servers/rxnlabel/Containerfile` bake
SHA-pinned checkpoints in a build stage under an egress allowlist, and run offline. Nobody noticed,
because nothing watches a condition — and this file's own first draft read the trigger as satisfied
by `D-135` in *this* repository, which is a dataset retriever that has never carried a third-party
corpus. The right answer off the wrong evidence is what an unwatched trigger buys you.
So state plainly what
this file is: **a bound on the trigger being written, not a measurement of anyone checking it.** It
makes the question askable. It does not answer it, and an ADR whose trigger is executable — a test
that reds when the blocker lifts — is worth more than one whose trigger is a sentence.

## The marker

`Revisit when:` at the start of a line, after optional list punctuation and emphasis — the spelling
`CLAUDE.md`'s ADR procedure names. The colon is optional in the pattern and canonical in the
procedure, because two merged ADRs already write `**Revisit when** the …`
(`D-2026-09-09-a-grant-set-that-contracts-is-not-a-pre-upgrade-step`,
`D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns`) and a marker that rejects the
repository's own precedent is one authors route around rather than adopt.

## Detecting a decline, and what that detection costs

The starting regex was widened and then cut back against the corpus, because two of its alternatives
were almost pure noise and two matched nothing at all:

* `must never` — **30 files, 1 of them declining work.** Every other hit is a runtime invariant: *"a
  model must never be able to authorize its own plan"*, *"a connector must never reach an access
  decision"*. Narrowed to `must never be built`, which keeps the one true hit.
* `is rejected` — **11 files, 1 declining work.** The rest are input validation: an invalid
  molecule, a foreign id, a manifest missing `required_roles`. Dropped; `is declined` is this
  repository's term of art for the act, and it does not collide with anything.
* `deliberately unbuilt` and `we do not build` — **zero files each.** The corpus writes
  `deliberately not built` and `stays unbuilt`; a pattern that cannot fire reads as coverage and is
  not.

**Measured false-positive rate of what ships: 2 files in 43** — read on 2026-09-19, and the figure
is here as evidence about a measurement rather than as a live count, because the denominator moves
whenever anybody merges. `D-2026-08-29-a-review-with-fresh-context-is-a-different-instrument` says
*"refused outright when unrouted"* about a runtime refusal, and
`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision` says *"when something is declined"*
while quoting the ADR procedure. Both are the same shape — the verb describing an act rather than
performing one — and neither is separable by a pattern without also losing the real hits, which is
what `_EXEMPT` is for. `refused outright` is kept at that price for the one real declination it
catches. `is not built` carries a negative lookahead on the word "on", because `run_cached_hessian
is not built on run_cached_with_artifacts` is a composition statement rather than a refusal. A check
that cries wolf is one nobody keeps, which is why the rate is measured and written down rather than
asserted to be low.

## The cursor

`_DECLINE_CURSOR` is the forward ratchet, the same shape and the same argument as `_TOPIC_CURSOR` in
`tests/test_decision_log.py`: the record behind it is 680 documents that may not be edited, so
requiring a trigger there would be a demand nobody can satisfy. It may move **backwards** — cover
more of the record — and never forwards, which is what
`test_the_decline_cursor_is_not_ahead_of_the_record` exists to make loud, because a cursor past the
newest ADR is a ratchet that passes by asserting nothing.
"""

import re
from pathlib import Path

from tests.test_decision_log import _sort_key

_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"

#: ADRs with an id on or after this date must carry the marker if they decline a class of work.
#: **Backwards only.** It is deliberately not the date of the audit that produced this file: the
#: newest ADR on disk when it was written was `D-2026-09-18`, and a cursor set a day later would
#: have put nothing in scope and passed by looking at nothing — the exact vacuity this file's own
#: last assertion refuses.
_DECLINE_CURSOR = "D-2026-09-18"

#: The act of declining a class of future work, as this corpus writes it. See the module docstring
#: for what was cut and what each cut cost.
_DECLINES = re.compile(
    r"is (?:deliberately )?declined"
    r"|(?:are|stays) declined"
    r"|deliberately not built"
    r"|stays unbuilt"
    r"|is not built(?! on\b)"
    r"|must never be built"
    r"|not shipped at all"
    r"|refused outright"
)

#: The trigger line. Anchored to the start of a line after optional bullet and emphasis punctuation,
#: so it is a *section marker* rather than a phrase that can appear mid-sentence — the point is that
#: a reader scanning the document finds it, which a clause buried in a paragraph does not achieve.
_TRIGGER = re.compile(r"^[ \t]*(?:[-*+]\s*)?(?:\*\*|__)?Revisit when\b", re.MULTILINE)

#: ADR id → why it trips `_DECLINES` while declining nothing anybody could reopen. Same no-stale
#: guard as `_ARGUED` in `tests/test_dead_vocabulary.py` and `_NOT_A_TOPIC` in
#: `tests/test_decision_log.py`: a declared exception that is never re-checked is a second, unread
#: index, and one that outlives its ADR goes on silencing a document that has been rewritten.
_EXEMPT: dict[str, str] = {
    "D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision": (
        "a false positive of `_DECLINES`, and the in-scope one this file was measured against: the "
        "phrase is the ADR *procedure* it writes — 'write an ADR when a choice between options is "
        "taken, or when something is declined' — not an act of declining. It declines nothing; its "
        "one unmechanised rule is explicitly a review rule, and the trigger it asks for is the one "
        "this test enforces"
    ),
    "D-2026-09-18-a-seam-read-in-one-direction-cannot-see-a-surface-grow": (
        "its trigger is executable rather than written: the two `calc` tools it declines are "
        "declined for reasons derived on every run — `optimize_geometry` collides with "
        "`relax_structure`'s cache key, `predict_logd` has no key at all — and "
        "`test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason` "
        "reconciles `_DECLINED` against the fleet's published surface, so the day either reason "
        "stops holding the suite reds. A sentence saying 'revisit when the key stops colliding' "
        "would be a weaker copy of a check that already runs"
    ),
}


def _adr_files() -> list[Path]:
    """Every ADR file in record order, reusing the one definition of that order."""
    return sorted(_DECISIONS.glob("D-*.md"), key=_sort_key)


def _in_scope(path: Path) -> bool:
    """True for a dated ADR at or after the cursor.

    Numbered `D-NNN` ids fall out by construction: `_sort_key` puts them in tier 0, before every
    dated id, so they are always behind the cursor. That helper is imported rather than restated
    because "which id is older" has exactly one definition in this repository, and its `D-900` case
    is the one a plain string compare gets wrong.
    """
    tier, _, stem = _sort_key(path)
    return tier == 1 and stem >= _DECLINE_CURSOR


def _declining_adrs() -> list[Path]:
    """In-scope ADRs whose body declines a class of work."""
    return [
        path
        for path in _adr_files()
        if _in_scope(path) and _DECLINES.search(path.read_text("utf-8"))
    ]


def test_a_decline_states_what_would_reopen_it() -> None:
    """An ADR that declines a class of future work says what would make it worth revisiting.

    A deferral expires and a refusal does not, which is backwards: the record's *stronger* word is
    the one written with no condition attached, and a permanent, never-edited document stamped
    `accepted` is where a constraint outlives the reason for it. Measured over the whole corpus, 71
    ADRs decline a class of work and 4 say what would reopen the question.

    This cannot reach the 67 — they may not be edited, and demanding it would be a gate nobody can
    pass. It reaches forward instead, which is where the next one is written.

    **A bound on the sentence existing, not on the condition being watched.** `D-092` wrote a
    precise trigger, the trigger was met by `D-135` and `ingest/sources/vendored_dataset.py`, and
    the decision stayed closed because nothing looks. Where the condition can be expressed as a
    check that goes red when it lifts, write that instead and exempt the ADR here saying so.
    """
    silent = sorted(
        path.stem
        for path in _declining_adrs()
        if path.stem not in _EXEMPT and not _TRIGGER.search(path.read_text("utf-8"))
    )
    assert not silent, (
        f"{len(silent)} ADR(s) on or after {_DECLINE_CURSOR} decline a class of future work "
        f"without saying what would reopen it: {silent}. Add a `Revisit when:` line naming the "
        "condition — the shape docs/planning/DEFERRED.md's 'Trigger to revisit' column has always "
        "required — or, if the decline is held by a check that reds when its reason lifts, add the "
        "ADR to _EXEMPT with the line naming that check."
    )


def test_no_exemption_is_stale() -> None:
    """An exemption may outlive neither its ADR nor the sentence it was granted for.

    Three ways to go stale and all three are asked, because an unspent row is not merely clutter: it
    is a *silencer* aimed at a document, and if that document is later rewritten to decline
    something real the row goes on covering it. The ADR may be gone; it may no longer decline
    anything, in which case nothing reads the row; or it may have fallen behind a cursor that moved.
    """
    on_disk = {path.stem for path in _adr_files()}
    missing = sorted(adr for adr in _EXEMPT if adr not in on_disk)
    assert not missing, f"_EXEMPT names ADRs that do not exist: {missing}"

    spent = sorted(adr for adr in _EXEMPT if adr not in {path.stem for path in _declining_adrs()})
    assert not spent, (
        f"_EXEMPT covers ADRs the check never looks at — they no longer trip the decline regex, or "
        f"they now sit behind {_DECLINE_CURSOR}: {spent}. Nothing reads these rows; delete them."
    )


def test_the_decline_cursor_is_not_ahead_of_the_record() -> None:
    """The cursor may only move backwards in time, i.e. cover *more* of the record, never less.

    A cursor set past the newest ADR would make the check above pass while asserting nothing — the
    failure mode of every ratchet, and the same reason `_TOPIC_CURSOR` carries this assertion and
    `tests/test_context_floor.py` states its ceiling instead of deriving one. Asserting that at
    least one ADR is in scope is the cheap half; the real bound is that raising this constant is a
    deliberate line in a diff that a reviewer can see and ask about.
    """
    in_scope = [path.stem for path in _adr_files() if _in_scope(path)]
    assert in_scope, (
        f"_DECLINE_CURSOR = {_DECLINE_CURSOR} is newer than every ADR on disk, so the ratchet "
        "asserts nothing. Lower it — it may only ever move backwards."
    )


def test_the_marker_accepts_the_spelling_the_record_already_writes() -> None:
    """The trigger pattern accepts the spelling the record already uses, and rejects prose.

    Two merged ADRs write `**Revisit when** …` with the colon outside the emphasis or absent, and a
    marker that rejects them would be a convention nobody adopts. What it must still reject is the
    phrase appearing mid-sentence: the marker's whole job is that a reader scanning the document
    finds the condition, which a clause buried in a paragraph does not do.
    """
    assert _TRIGGER.search("**Revisit when:** a deployment vendors the weights\n")
    assert _TRIGGER.search("- **Revisit when** the expand/contract split becomes available\n")
    assert _TRIGGER.search("Revisit when a deployment's table says otherwise.\n")
    assert not _TRIGGER.search("The sentence says we should revisit when somebody asks.\n")
    assert not _TRIGGER.search("…scan the database does in milliseconds. Revisit when it grows.\n")
