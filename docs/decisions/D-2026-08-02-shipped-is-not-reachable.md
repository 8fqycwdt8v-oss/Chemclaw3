# D-2026-08-02-shipped-is-not-reachable — Shipped is not reachable

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-011 (persist once, never recompute),
D-118 (the connector seam holds capability), D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet

## Context

The 106-story capability audit (`docs/reference/user-story-capability-map.md`) produced one number
that reframed the roadmap: **57% of user stories already have working machinery behind them**, and
only 2 of 106 were `MISSING-TOOL`. Read alone that is a good result. Read beside the 190-probe live
run — where the same system answered those stories by inventing — it is a different result: the
machinery was not what was missing.

Five subsystems in this repository were built, tested, ADR'd and **could not be reached from a
conversation**. Not deprecated, not half-finished — complete, with green tests, and no caller:

- `read_campaign` / `suggestions_for` (`science/bo/campaign_record.py`): the campaign entity, the
  stable `campaign_id_for` hash, both backends and the migration all exist and are written on every
  suggestion. `suggest_next_experiment`'s docstring already told the agent to quote the campaign id
  back "so a later session picks the thread back up", and **nothing could pick it up**. Zero
  non-test callers.
- `failure_note` (`memory/failure.py`): builds exactly the right `failure-mode` note with a
  `contradicts` edge and `reported_by`. Zero non-test callers, so "correct what the assistant
  knows" (story 17.5) had a mechanism and no door.
- Impurity structures: `ingest/eln/ingest.py` indexed `reaction.compounds()`, which is
  `[*inputs, *outcomes]`. A recorded impurity's SMILES was rendered into note *text* and never into
  the molecule index — so lexical search could find an impurity and structural search could not,
  the exact inverse of what a structure-similarity question needs.
- Project provenance: `ingest/eln/note.py` set no `tags` at all, so `OrdReaction.project` never
  reached the graph. `gather_evidence(tag=…)` is documented for project filtering and was inert on
  the largest note class in the corpus (993 reaction notes; 6 carried any tag).
- Evidence fields: the retrievers populate `conflicts_with`, `confidence`, `created_by` and
  `source` on every chunk; `retrieval/harness.py` rendered `content` and `source_note_id` and
  discarded the rest. A report that silently drops a **declared conflict between two notes** is the
  one output where dropping it matters most.

The common shape is not laziness. Each of these was finished at the layer its author owned and
never crossed the seam into the layer that calls it, and nothing in the suite could tell — a unit
test of a function with no callers passes forever.

## Decision

**A capability is reachable or it does not exist, and the test that says so is a caller test.**
Concretely, three things follow.

**1. The last mile is part of the change, not a follow-up.** A store gets a tool
(`resume_campaign`), a note builder gets a write path (`record_failure`, through the existing
PR-gate — `kg/pr_gate.py` already accepts multiple files, so the refutation and the amended
original ship in one submission), an index gets the records it was meant to hold. None of this
week's work invented a mechanism; all of it connected one.

**2. Reachability is asserted from the caller's side.** `tests/test_tool_registry.py` pins the
agent-callable surface, and every item here is proved by a test that drives the *public* entry
point — the tool, the ingest, the rendered report — and fails on the parent commit. A test that
calls `read_campaign` directly would have passed for the whole time nobody could reach it.

**3. Rendering is not free-form.** `retrieval/harness.py` now renders what the evidence carries,
and an absent field renders as nothing rather than as an empty heading. The failure mode being
designed against is an empty "Conflicts" section that reads as "no conflicts" — a false clean bill
rather than a missing one.

## Consequences

- Stories 3.2, 3.5 (cross-session campaign threads), 1.2/17.5 (correcting the record), 8.8
  (structural impurity search), 1.3 (project history), 12.4/13.2/13.5 (conflict and provenance in a
  report), 11.2/6.6 (scale) and 6.3/6.18 (side-by-side campaign reading) move off their audit
  verdicts. None of them needed a new concept.
- `record_failure` is a **write** tool and is gated as one (`agent/authz.py`
  `DEFAULT_WRITE_TOOL_GATES`). A refutation is a claim about the record; it goes through the same
  human sign-off as every other agent-authored note.
- Tagging ELN reaction notes with their project is deliberately the *existing* tag mechanism and
  not a schema field. It is the precondition for making `project` first-class — it puts real values
  on the largest note class so the migration has data to migrate and the value is provable before
  the schema changes.
- The audit's "zero non-test callers" check is worth keeping as a habit rather than a test: a
  lint for it would fire on every genuinely internal helper, and the honest version of the check is
  a human asking "who calls this from outside `science/`?" when a subsystem is declared done.
