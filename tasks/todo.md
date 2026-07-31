# Task: the daily experiment progression — chronology, intent, and a non-BO next-experiment path

Requested 2026-07-31. Branch: `claude/chemclaw-experiment-progression-056btx`. ADR: **D-156**.

(The previous occupant of this file, the agentic-system review, is merged; its record lives in
D-145/D-151/D-152/D-153 and git history.)

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

- [ ] **§0** Reserve D-156 in `docs/decisions/README.md` (first commit).
- [ ] **§1 Chronology.** New `memory/progression.py`: order a series by `performed_at`, and name
      what changed between consecutive runs (temperature, time, and the species set per role).
      `optimization_campaign_note` renders Date + "Changed vs previous" columns, in time order, and
      states plainly when the ordering is *not* a timeline.
- [ ] **§2 Intent.** `OrdReaction.hypothesis`, mapped by the JSON ELN adapter, rendered in the
      reaction note and carried into the campaign note's per-run block. New `follows` relation.
- [ ] **§3 The proposal.** `experiment-proposal` note type; new `experiment-progression` skill
      (judgment: read chronologically, name the moved variable, what is untested, what the failures
      rule out, propose one experiment with a rationale and a falsifiable expectation);
      `deep-research` §6 rewritten to route the two questions to their own paths.
- [ ] **§4 Time-scoped retrieval.** `since`/`until` on `_eligible_notes` and on `gather_evidence`.
- [ ] **§5 Corpus + docs.** Seed notes for the new type and the new relation; `knowledge/README.md`
      counts; ADR D-156.
- [ ] **§6 Verify.** New `tests/test_progression.py`; `make lint type test` green.

## Review

(filled in at the end)
