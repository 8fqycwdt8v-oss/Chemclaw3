# Task: the daily experiment progression — chronology, intent, and a non-BO next-experiment path

Requested 2026-07-31. Branch: `claude/chemclaw-experiment-progression-056btx`. ADR: **D-162**.

(The previous occupant of this file, the repo-consistency pass, is merged as D-156; its record is
that ADR and git history.)

**On the number.** This branch reserved D-156 in its first commit and ended up at **D-162**:
four other branches merged into `main` while it was open, and three of them took the number this
one held. Each time, the rule in `CLAUDE.md` decided it without a judgement call — whoever merges
second renumbers. Worth recording as evidence for that file's own closing paragraph: a reservation
protects a number only against sessions that can see it, and with this many concurrent branches
the global sequence is now colliding routinely rather than exceptionally.

## The question behind it

A lab technician works one step for weeks, running one experiment a day, each one testing a
hypothesis formed from yesterday's result. Can ChemClaw — with an ELN connected — read that
progression and propose tomorrow's experiment with a rationale, *without* Bayesian optimization?

Investigated first (see the ADR for the full finding). Most of it was already there: ELN ingest
carries conditions/outcomes/prose/`performed_at`, `memory.optimization` groups the same
transformation into an `optimization-campaign` note, `gather_evidence` +
`optimization-campaign-synthesis` reason over it, and `deep-research` already has an
evidence-based (non-BO) proposal path. Three things were structurally missing:

1. **No chronology.** Clusters come back id-sorted (`memory/similarity.py`), and the campaign table
   had no date column and no date sort — the agent read six weeks of work as an unordered *set*.
2. **No intent.** `OrdReaction` had no field for what a run was testing, and `KNOWN_RELATIONS` had
   no sequential edge, so "yesterday's result → today's question" existed nowhere.
3. **"What next?" was wired to BoFire.** `deep-research` §6 routed it to `suggest_next_experiment`;
   there was no agent-only progression skill and no note type for a proposal.

## Plan

- [x] **§0** Reserve D-162 in `docs/decisions/README.md` (first commit).
- [x] **§1 Chronology.** New `memory/progression.py`: order a series by `performed_at`, and name
      what changed between consecutive runs (temperature, time, and the species set per role).
      `optimization_campaign_note` renders Date + "Changed vs previous" columns, in time order, and
      states plainly when the ordering is *not* a timeline.
- [x] **§2 Intent.** `OrdReaction.hypothesis`, mapped by the JSON ELN adapter, rendered in the
      reaction note and carried into the campaign note's per-run block. New `follows` relation.
- [x] **§3 The proposal.** `experiment-proposal` note type; new `experiment-progression` skill
      (judgment: read chronologically, name the moved variable, what is untested, what the failures
      rule out, propose one experiment with a rationale and a falsifiable expectation);
      `deep-research` §6 rewritten to route the two questions to their own paths.
- [x] **§4 Time-scoped retrieval.** `since`/`until` on `_eligible_notes` and on `gather_evidence`.
- [x] **§5 Corpus + docs.** Seed notes for the new type and the new relation; `knowledge/README.md`
      counts; ADR D-162.
- [x] **§6 Verify.** New `tests/test_progression.py`; `make lint type test` green.

## Review

Shipped as planned. Four judgment calls worth recording, each of them a place where the easy
implementation would have been the wrong one:

- **`follows` is never derived from dates.** Emitting it from the campaign's own chronology was one
  line and was rejected: `performed_at` proves that run B came after run A, never that it was run
  *because* of A. Manufacturing that edge is precisely the failure this work exists to prevent, so
  the edge is minted only by an author who knows the intent.
- **The hypothesis is read from a field, not extracted from prose.** A pattern-matched motive is
  indistinguishable downstream from testimony.
- **No new agent tool and no new note artifact.** The proposal goes through the existing
  `propose_knowledge_note` with a new type; the chronology enriches the existing
  `optimization-campaign` note instead of minting a parallel one over the same cluster. D-078's
  supersede machinery then keeps a daily-growing series current for free.
- **An undated note fails a windowed query** rather than passing it. It cannot be shown to fall in
  the window, and a question about a period should not be answered with a note of unknown date.

Verified: new `tests/test_progression.py` (18 cases) plus date-window cases in
`tests/test_research_tools.py` and hypothesis-mapping cases in `tests/test_eln.py`;
`make lint type test` green (2061 passed) and all four affected validators
(`skill-validate`, `kg-validate`, `prose-validate`, `eln-validate`) pass.

## Follow-up review (D-163)

Re-read the merged change with fresh eyes. It found one defect the change did not introduce but
made visible, and three cleanups in the new code:

- [x] **The real one.** `deep-research` and `experiment-design` instructed the agent to write
      `protocol` / `experiment-batch` notes; neither type exists, so the proposal opens a branch
      `kg-validate` rejects. `make prose-validate` gains a note-type rule (it checked tool names
      only, so the blind spot and the bug were the same shape), and both fold into
      `experiment-proposal`. ADR **D-163**.
- [x] The campaign table is driven off `Progression.steps` with the run looked up by id, not two
      independently-sorted lists zipped positionally.
- [x] `_in_window` reads as two early returns instead of a double negative.
- [x] `gather_evidence` documents that a date window scopes the note sources, not the structural
      anchor.
- [ ] Left open (PROSE-4): `propose_knowledge_note`'s docstring restates the type list with an
      ellipsis — a third copy synced by nothing. Deriving it needs a change to how tool
      descriptions are built, so it is a backlog item rather than a drive-by.
