"""A word the system dropped keeps reading as live prose, and the marker that says so goes stale.

When a subsystem is deleted the ADRs that describe it are not rewritten — a merged ADR is never
edited (CLAUDE.md), and a record of what was true then is the point of keeping it. What that leaves
is a corpus whose vocabulary outlives its mechanism: dozens of files still say "GxP" for a
regulatory framing that `D-2026-08-14-the-record-is-kept-because-it-is-useful-…-a-regulator-asks`
withdrew, and a larger set still describes the PR-gate in the present tense after
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted it. None of them says so from the
inside, because none of them may. (No count is written here; `grep -rl` answers it on the day it is
asked, and a transcribed measurement is the defect the next paragraph is about.)

`docs/decisions/README.md` already carries the cure the superseding ADR itself names: a marker
section telling the reader what to substitute. It was written by hand, for one vocabulary, and it
went stale the way a hand-written roster always does — the ids it named and the files that matched
had drifted apart, three of the strays were ADRs written *after* the word was dropped and so
covered by nothing at all, and no test read any of it. So this file does **not** re-derive that
roster. A roster is the wrong shape twice over: it is a second index of the same thing, which
drifts from the first the moment an old ADR is renamed, and it re-states as data what a `grep`
answers exactly. What is asserted instead is the one thing a roster cannot do — that the marker
*exists* for each dead term, that it reaches the ADR that killed the term, and that **no ADR
written after the death silently adds to the pile**.

## Deliberately nothing about ADRs written before a term died

That is the whole corpus, it is correct about the moment it was written, and it is exactly what the
marker section covers. Asserting anything about it would either fail on dozens of files of prose
nobody may edit, or degrade into the file roster that already went stale. The ratchet starts at
the killing ADR's own date and runs forward, which is the half a machine can hold.

## How the patterns were chosen

Each was run against `docs/decisions/` before it was written down, and a pattern that answered
either extreme was dropped:

* **Matches nothing** — useless, and worse than useless because it reads as coverage. Three of the
  four candidates in the audit's own list were in this state: `PR gate` (the corpus always
  hyphenates), `the knowledge PR` and `anthropic provider` match **zero** files each.
* **Matches ordinary prose** — a check that cries wolf is one nobody keeps. `must never` and
  `Anthropic` alone are the obvious ones; measured, `must never` lands on 30 files and 29 of them
  are runtime invariants ("a model must never authorize its own plan").

What replaced them are identifiers and terms of art, which is the property that makes a pattern
precise here: `propose_knowledge_note` and `note_proposals` are the gate's own symbol names, and
`ChatAnthropic`/`langchain_anthropic` are the second provider's import path. `provider seam` is
kept as the one two-word phrase, on measurement rather than on taste — 5 of its 6 occurrences are
the dead LLM seam, and the sixth is argued below rather than being reason to widen the net.

## What this bounds

That the substitution is *written down* and that the pile stops growing. Not that anybody reads it,
and not that an ADR's prose is right — an ADR's prose is a review matter, the way
`tests/test_decision_log.py` says the id checks are about identity rather than style.
"""

import re
from pathlib import Path

from tests.test_decision_log import _sort_key

_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
_INDEX = _DECISIONS / "README.md"

#: One row per dead vocabulary: the term as its marker section names it, the patterns that find it,
#: and the ADR that killed it. **This table is the single declaration** — the marker sections in
#: `docs/decisions/README.md` are asserted against it, in this order, so a fourth dead vocabulary
#: is one row here plus one section there and nothing else anywhere.
#:
#: Patterns are matched case-sensitively on purpose. Every one of them is an acronym, an identifier
#: or a hyphenated term of art that this corpus spells exactly one way, and folding case would put
#: `gamp`, `gxp` and a sentence-initial `Provider seam` into the net for no measured gain.
_DEAD: dict[str, tuple[str, tuple[str, ...]]] = {
    "GxP": (
        "D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks",
        ("GxP", "21 CFR", "ALCOA", "GAMP"),
    ),
    "the PR-gate": (
        "D-2026-09-05-the-gate-follows-behaviour-not-knowledge",
        ("PR-gate", "propose_note", "propose_knowledge_note", "note_proposals"),
    ),
    "a second LLM provider": (
        "D-2026-09-04-a-gateway-is-the-only-provider",
        ("ChatAnthropic", "langchain_anthropic", "CHEMCLAW_LLM_PROVIDER", "provider seam"),
    ),
}

#: `(term, adr_id)` → the line saying why that ADR may use the dead word although it was written
#: after the word died. Every one of these was read before it was written; a placeholder here would
#: make the register the thing it exists to prevent.
#:
#: Two shapes recur and both are legitimate. An ADR *killing* or *reporting* a mechanism has to name
#: it — a deletion that cannot say what it deleted is not a record — and the same-day wave around a
#: deletion is all of that. The other is a name that outlived its concept: a module keeps its file
#: name after the thing it was named for is gone.
_ARGUED: dict[tuple[str, str], str] = {
    # ---- GxP, after 2026-08-14 -------------------------------------------------------------
    ("GxP", "D-2026-08-14-the-coupling-is-the-cost-not-the-line-count"): (
        "its §5 is headed 'GxP is no longer a constraint on layer 1' — it carries out the removal "
        "on the branch that was open when the framing was withdrawn — it names the word to drop it"
    ),
    ("GxP", "D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work"): (
        "cites the argument GxP's removal freed — the scratchpad declination rested on a posture "
        "that no longer constrains layer 1, and saying which posture requires naming it"
    ),
    ("GxP", "D-2026-08-15-safety-is-a-tool-not-a-gate"): (
        "the decision *is* the analogy: safety stops being structural 'the same way "
        "D-2026-08-14 made GxP stop', so the withdrawn framing is the precedent being applied"
    ),
    ("GxP", "D-2026-09-05-the-gate-follows-behaviour-not-knowledge"): (
        "quotes D-005's premise verbatim — the PR-gate 'justified itself in GxP vocabulary, and "
        "that premise is gone' — which is the sentence that deletes the gate"
    ),
    ("GxP", "D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision"): (
        "the audit that produced these marker sections: it counts the ADRs still using each "
        "dropped "
        'vocabulary and names the `## Where the record still says "GxP"` heading as the cure it '
        "is generalising, so the word is the measurement's own subject"
    ),
    # ---- the PR-gate, on and after 2026-09-05 ----------------------------------------------
    ("the PR-gate", "D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision"): (
        "same audit, other term — it reports how many ADRs still describe the gate as live and "
        "records that ten of the dozen worst-reading stale constraints rest on that one deletion"
    ),
    ("the PR-gate", "D-2026-09-05-the-gate-is-deleted-not-dormant"): (
        "the deletion itself: it counts the gate's 2,232 lines, names all nine callers of "
        "`propose_note` and records the `propose_knowledge_note` → `record_knowledge_note` rename"
    ),
    ("the PR-gate", "D-2026-09-05-a-rejection-nobody-reads-is-a-decision-taken-twice"): (
        "same-day, and it is the review-scaling question the deletion answered — it builds on "
        "D-005 by name and describes `note_proposals` as the gate's record of who decided what"
    ),
    ("the PR-gate", "D-2026-09-05-a-reader-outlives-its-writer-more-quietly-than-a-writer"): (
        "five fresh-context reviews *of the gate deletion*; `note_proposals` is named as the table "
        "whose readers outlived their writer, which is the defect class it reports"
    ),
    ("the PR-gate", "D-2026-09-05-the-cut-the-gate-could-not-see"): (
        "measures what the gate's flat-file layout did to the knowledge directory, so the removed "
        "mechanism is the thing being measured rather than a live one being described"
    ),
    ("the PR-gate", "D-2026-09-05-a-procedure-that-leaves-no-record"): (
        "same-day: it prices `propose_knowledge_note`'s 1,126-token schema as prefix the deletion "
        "reclaims, and the tool name is what makes that number checkable"
    ),
    (
        "the PR-gate",
        "D-2026-09-05-a-ratchet-that-re-derives-half-its-basis-bounds-half-a-request",
    ): (
        "cites the same 1,126 tokens as the scale a prefix ceiling must resolve below; the tool is "
        "named as a measured quantity from that day, not as a capability the agent still has"
    ),
    ("the PR-gate", "D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address"): (
        "the egress census names `git push` as a destination the KG PR-gate used to charge — a "
        "line item being struck from the destination list, which requires saying whose it was"
    ),
    ("the PR-gate", "D-2026-09-09-a-grant-set-that-contracts-is-not-a-pre-upgrade-step"): (
        "`note_proposals` is the worked example of a contracting grant: the replay that exposed "
        "the defect is the commit that dropped that very table from the writer list"
    ),
    ("the PR-gate", "D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated"): (
        "quotes the migration error verbatim — `note_proposals_state_known` — and a migration "
        "guard's evidence is the SQL identifier that failed, which no substitution can rewrite"
    ),
    ("the PR-gate", "D-2026-09-09-a-nan-is-a-wildcard-that-never-loses"): (
        "its §5 finds the BO bundle still telling the model a recommendation is PR-gated, and "
        "fixes that tool description; the dead word is the defect being quoted"
    ),
    ("the PR-gate", "D-2026-09-14-a-backfill-is-not-a-conversation"): (
        "quotes an earlier finding about headroom 'bought by deleting the PR-gate' — a citation to "
        "the deletion's own accounting"
    ),
    (
        "the PR-gate",
        "D-2026-09-16-an-approval-binds-to-the-version-that-was-shown-on-every-surface",
    ): (
        "names the `note_proposals.decided_by` defect as the precedent reappearing in the workflow "
        "seam; the value of the citation is that it is the same defect under a new mechanism"
    ),
    ("the PR-gate", "D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool"): (
        "builds the skill-proposal queue on what `note_proposals` got right and wrong, including "
        "the superseded-state migration it shipped without; the retired table is the design input"
    ),
    # ---- a second LLM provider, on and after 2026-09-04 ------------------------------------
    (
        "a second LLM provider",
        "D-2026-09-07-a-borrowed-helper-is-declared-because-copying-it-drifts",
    ): (
        "the phrase names `agent/llm_provider.py` and `tests/test_llm_provider.py`, which still "
        "exist and still hold the client-import buckets; the module name outlived the choice it "
        "was named for, and renaming a live seam is not what the substitution asks for"
    ),
}


def _dated_adrs() -> list[Path]:
    """Every dated ADR file, newest last.

    Numbered `D-NNN` ADRs are excluded by construction: `_sort_key` puts them in tier 0, which is
    before every dated id, so they are always older than any death date and never in scope. Reusing
    that helper rather than restating the ordering is deliberate — a second definition of "which id
    is older" is the defect `tests/test_decision_log.py` already argues at length, and its `D-900`
    case is the one a hand-rolled string compare gets wrong.
    """
    return sorted(
        (path for path in _DECISIONS.glob("D-*.md") if _sort_key(path)[0] == 1), key=_sort_key
    )


def _death_date(term: str) -> str:
    """The `YYYY-MM-DD` a term stopped being live, read off the killing ADR's id."""
    return _DEAD[term][0][2:12]


def _matcher(term: str) -> re.Pattern[str]:
    """One alternation over a term's patterns — a file is read once per term, not once per word."""
    return re.compile("|".join(re.escape(pattern) for pattern in _DEAD[term][1]))


def _in_scope(term: str, path: Path) -> bool:
    """True for a dated ADR written on or after the term died, excluding the killing ADR itself."""
    return path.stem[2:12] >= _death_date(term) and path.stem != _DEAD[term][0]


def _marker_sections() -> list[tuple[str, str]]:
    """Every `## Where the record still says "<term>"` section in the index, as `(term, body)`."""
    index = _INDEX.read_text("utf-8")
    heads = re.finditer(r'^## Where the record still says "([^"]+)"\s*$', index, re.MULTILINE)
    sections: list[tuple[str, str]] = []
    for head in heads:
        following = re.compile(r"^## ", re.MULTILINE).search(index, head.end())
        end = following.start() if following else len(index)
        sections.append((head.group(1), index[head.end() : end]))
    return sections


def test_every_dead_term_has_its_marker_section() -> None:
    """The index carries one marker per dead vocabulary, in the table's order, linking the killer.

    The marker is the only thing standing between a reader and prose that describes a deleted
    mechanism in the present tense, and the superseding ADR names this index as the mechanism
    precisely because it may not edit the ADRs themselves. A section that exists but does not reach
    the ADR that killed the term is half a marker: it tells the reader to substitute and gives them
    no way to find out what the substitution rests on.

    Order is asserted so that this table stays the single declaration — a section added here without
    a row, or a row without a section, is the two-indexes-drifting failure in miniature.
    """
    sections = _marker_sections()
    assert [term for term, _ in sections] == list(_DEAD), (
        "docs/decisions/README.md must carry one '## Where the record still says \"<term>\"' "
        f"section per row of _DEAD, in that order; found {[term for term, _ in sections]}"
    )
    for term, body in sections:
        killer = _DEAD[term][0]
        assert f"({killer}.md)" in body, (
            f'the "{term}" marker section does not link {killer}.md. A substitution instruction '
            "without the decision behind it cannot be checked by the reader it is written for."
        )


def test_every_killing_adr_exists() -> None:
    """A table row naming an ADR that is not on disk silences a term against nothing.

    `_in_scope` derives its date from that id, so a typo does not fail loudly — it moves the ratchet
    to a date nothing is on or after, and the term goes quietly unchecked.
    """
    missing = sorted(
        killer for killer, _ in _DEAD.values() if not (_DECISIONS / f"{killer}.md").is_file()
    )
    assert not missing, f"_DEAD names ADRs that do not exist: {missing}"


def test_no_adr_written_after_a_word_died_uses_it_unargued() -> None:
    """The ratchet: the pile of stale vocabulary may not grow after the decision that ended it.

    The corpus that predates a death is covered by the marker section and is not touched here — it
    is correct about when it was written and it may not be edited. What a machine can hold is the
    forward half, and the forward half is where the damage is: an ADR written *after* the gate was
    deleted and describing it in the present tense is not a record of the past, it is a new false
    claim about the system, and it lands in the same directory as the decision that deleted it.

    An ADR that names the dead word to *kill*, quote or measure it is doing the one thing a record
    must be able to do, so it goes in `_ARGUED` with the line saying which. That is a sentence of
    friction per entry, deliberately, on the `_NOT_A_TOPIC` model.
    """
    offenders: list[str] = []
    for term in _DEAD:
        matcher = _matcher(term)
        for path in _dated_adrs():
            if not _in_scope(term, path) or (term, path.stem) in _ARGUED:
                continue
            hits = sorted(set(matcher.findall(path.read_text("utf-8"))))
            if hits:
                offenders.append(f"{path.name} says {hits} of the retired '{term}' vocabulary")
    assert not offenders, (
        f"{len(offenders)} ADR(s) written after their subject was deleted use its vocabulary "
        f"unargued: {offenders}. Say it in today's words, or add the (term, id) pair to _ARGUED "
        "with the one line saying why the dead word is the right word there."
    )


def test_no_argued_entry_is_stale() -> None:
    """An entry may outlive neither its file, nor its term, nor the sentence it was granted for.

    Same shape and same reason as `_RETIRED_TEST_CITATIONS` in `tests/test_decision_log.py`: an
    allowlist nobody re-checks becomes a second, unread index — and a row that outlives its use is
    also a *silencer*, so the next time that ADR is rewritten the check that should have caught it
    is already switched off. Three ways to go stale and all three are asked: the ADR is gone, the
    ADR no longer says the word, or the row was never in scope to begin with.
    """
    on_disk = {path.stem for path in _dated_adrs()}
    unknown_terms = sorted({term for term, _ in _ARGUED if term not in _DEAD})
    assert not unknown_terms, f"_ARGUED names terms _DEAD does not declare: {unknown_terms}"

    missing = sorted(f"{term}/{adr}" for term, adr in _ARGUED if adr not in on_disk)
    assert not missing, f"_ARGUED names ADRs that do not exist: {missing}"

    out_of_scope = sorted(
        f"{term}/{adr}" for term, adr in _ARGUED if not _in_scope(term, _DECISIONS / f"{adr}.md")
    )
    assert not out_of_scope, (
        f"_ARGUED exempts ADRs the ratchet never looks at — they predate the death, or they are "
        f"the killing ADR: {out_of_scope}. Nothing reads these rows; delete them."
    )

    spent = sorted(
        f"{term}/{adr}"
        for term, adr in _ARGUED
        if not _matcher(term).search((_DECISIONS / f"{adr}.md").read_text("utf-8"))
    )
    assert not spent, (
        f"_ARGUED exempts ADRs that no longer use the dead vocabulary: {spent}; delete the row"
    )


def test_a_pattern_that_finds_nothing_is_not_a_pattern() -> None:
    """Every declared pattern matches somewhere in the record, or it is coverage nobody has.

    This is the half of the audit that produced the table. Three of the candidate patterns handed
    over matched **zero** files — `PR gate`, `the knowledge PR`, `anthropic provider` — and a
    pattern in that state is worse than an absent one: the row reads as though the vocabulary is
    policed, the ratchet above passes forever, and the word it was supposed to catch is spelled the
    way the corpus actually spells it. The check is over the *whole* corpus rather than the in-scope
    slice, because a term dies precisely when the old spelling stops being written.
    """
    barren = [
        f"{term}/{pattern!r}"
        for term, (_, patterns) in _DEAD.items()
        for pattern in patterns
        if not any(pattern in path.read_text("utf-8") for path in _DECISIONS.glob("D-*.md"))
    ]
    assert not barren, (
        f"declared as dead vocabulary but matching nothing in docs/decisions/: {barren}. Check the "
        "spelling the corpus uses, or delete the pattern — a pattern that cannot fire is a claim "
        "that a word is policed when it is not."
    )
