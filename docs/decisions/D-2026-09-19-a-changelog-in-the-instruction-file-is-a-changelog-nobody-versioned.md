# D-2026-09-19-a-changelog-in-the-instruction-file-is-a-changelog-nobody-versioned — `CLAUDE.md` states what is true, not how it got that way

**Status:** accepted · **Date:** 2026-09-19 · **Builds on:**
D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision (the same argument over the ADR corpus),
D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit, D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose

## Context

`CLAUDE.md` is loaded into every session as authoritative. Classified paragraph by paragraph it was
**795 lines, 9,204 words, ~16,100 tokens**, of which **42.9%** was a rule an implementer needs and
**44.3%** was narrative about defects already fixed — one unbroken chronology from line 69 to line
400, 56% of the file, with every instruction living after line 506. A reader met roughly 5,500 words
of history before the first thing they had to do.

That would be merely expensive. What made it harmful is that a chronology is *made of claims*, and
claims go stale:

- the per-turn spend cap was described as "ships at 0" after two raises had moved it, and the same
  stale clause had propagated into the ADR index's own cost row;
- the compaction budget and the context ceiling were quoted from three rewrites ago, in a file whose
  own text says "the live number is whatever `tests/test_context_floor.py` measures" and then prints
  a dozen more digits on either side of that sentence;
- the helper's tool counts were 21 and 27 against a caller base of 54 — and the base had moved to
  60, so the subtraction the paragraph existed to make no longer worked, while the only test over it
  pinned the subset relation and could never see it;
- **"There is no specialist team and no challenge panel"** stood as a bolded headline 68 lines above
  **"A specialist roster now ships"**, with a third, deleted sentence quoted in between to reconcile
  them — and `agent_helper_roster` ships three specialists on by default. An implementer asked to add
  a fourth could not tell from the file whether that was forbidden, routine, or needed an ADR.

Two prohibitions had also outlived their mechanism: *"no Temporal Schedule opens a pull request"*,
stated 31 lines after the paragraph saying the gate and every module behind it are gone, and a
`Bash`-tool refusal phrased flatly enough to read as "no code execution" while `pyexec` runs Python
over a connector by naming it in a deployment.

**The one section that never went stale is the validator list — the one that refuses to state a
count.**

## Decision

**`CLAUDE.md` states present-tense state and rules. The chronology moves to `docs/decisions/`,
which already holds it.** The status section is rewritten from 444 lines to 153, the file from 795
lines to under 500, and every load-bearing invariant buried in the narrative is kept: the bare
`SubAgent` dict that runs with no audit trail, authorization or plan gate; the attenuation rule;
`ModelCallLimitMiddleware`'s composition hazard; the chart's refusal to render without a stated
posture; `temporal.namespace` having no default; the write order in `kg/record.py`; the
semiempirical-only tier and what to do when a decision falls inside its error bar.

**No figure appears in that section, and `tests/test_claude_md_figures.py` is what keeps one from
coming back.** A figure of four digits or more — the class that actually rotted — must resolve to
the symbol that holds it or be declared with the reason it cannot move. Identifiers and dates are
stripped first. The allowlist is currently empty, and may not outlive its figures.

**The three contradictions are resolved rather than annotated.** The specialist sentence is replaced
by one rule: a router and a panel were deleted, a roster ships on by default, and adding a profile is
a file rather than a decision. The code-execution sentence states the real rule — no *local* shell,
a sandboxed connector is an ordinary connector question. The dead Schedule prohibition is restated as
what it was always about, which is who asked, and the five places in `src/` that carried the same
dead phrasing are corrected in the same commit.

## What this deliberately does not do

**It does not move the history anywhere new.** Every sentence removed is already in an ADR with an
index entry. A changelog kept in the instruction file is a changelog nobody versioned; a changelog
kept in `docs/decisions/` is the thing that record is for.

**It does not lower the file's authority.** Nothing that constrains an implementer was dropped — only
the account of how each constraint came to exist, and the figures that made the account checkable
and wrong.

## Revisit when

A rule turns out to be unfollowable without its history — i.e. a reviewer has to reconstruct *why*
from `git log` more than occasionally. The fix then is a one-line ADR citation beside the rule, not
the chronology back.

## What keeps it true

- `tests/test_claude_md_figures.py` — no undeclared figure, and no exemption that outlives its figure.
- `tests/test_prose_contract.py` — the operator documents name only things that exist.
- `tests/test_repo_map.py` — the architecture rows still match the tree.
