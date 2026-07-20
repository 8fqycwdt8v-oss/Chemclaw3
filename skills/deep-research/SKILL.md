---
name: deep-research
description: >-
  How to answer any open-ended process-R&D question — about any output (yield, purity,
  impurities), any process detail or observation, or general protocol guidance — by
  composing every data source and tool, and how to draft new conditions/protocols grounded
  in that evidence. Cite everything, separate evidence from analogy, and route anything new
  through the PR-gate.
---

# Deep research

This is the judgment for Chemclaw as a **general research assistant**: the question is
open-ended and the answer must be assembled from whatever the system knows, across sources.
It is not tied to one output (yield) or one reaction — treat yield, impurities, robustness,
observations, and process choices all the same way, and reason across similar *and* unrelated
chemistry when that is what answers the question.

## The loop

1. **Decompose.** Break the question into what you actually need to retrieve: an output to
   compare, a transformation to survey, a functional group or substrate motif, a property to
   compute. Most real questions are a few of these at once.
2. **Gather from everything.** `gather_evidence` sweeps all internal sources in one call
   (the whole knowledge graph — reactions, optimization campaigns, playbooks, reports — plus
   structurally similar reactions when you pass a `reaction_smiles` anchor). Pass a
   `note_type` or `tag` filter to narrow. It returns cited chunks; there is no need to query
   sources one by one for a first pass.
3. **Drill in.** For any cited note, `expand_note` gives the full body — the step-by-step
   recipe, per-step conditions, the verbatim procedure prose, and outcomes. That prose is
   where impurities, observations, and robustness rationale live; read it, don't just read the
   headline numbers.
4. **Cross-learn by structure**, not only by text:
   - `find_similar_reactions(reaction_smiles)` — past runs of the *same* transformation (the
     history behind "what has been tried" / "what moved the yield").
   - `find_substructure_matches(smarts)` then `find_notes(smiles)` — reach reactions where a
     specific functional group is present (e.g. a free primary amine in a Buchwald–Hartwig).
   - `find_similar_molecules(smiles)` — analogous substrates when the exact one is absent.
   - An `optimization-campaign` note already lays out one transformation's runs side by side;
     a `playbook` note is the transferable rule across projects — start from these when they
     exist.
5. **Compute when the record is silent — proactively.** If the answer turns on a property the
   notes do not state (weighing an untried solvent against the tested ones, a pKa, a relative
   stability), run it yourself with `predict_solubility` / `predict_pka` / `compute_xtb_energy`
   and fold the result — with its uncertainty — into the answer; do not stop at "the ELN does
   not say". Heavy QM goes through `submit_qm_job`.
6. **Design the next experiment when that is the question.** "What should I run next / which
   condition to try" is answered with `suggest_next_experiment`: frame the decision space and
   turn the historic runs you gathered into observations, then propose the next point(s). See
   the `experiment-design` skill; the result is a proposal a human runs, not a fact.

## Discipline (non-negotiable)

- **Cite the note id behind every factual claim.** An answer a reviewer cannot trace is not
  usable. When you state a number, name the reaction/campaign note it came from.
- **Evidenced vs. analogy, kept visibly separate.** "We ran this exact reaction at 80 °C and
  got 85% ([[reaction-x]])" is evidence. "A similar coupling tolerated a free amine
  ([[reaction-y]]), so this one may too" is analogy — label it as such, never as fact.
- **Silence is an honest answer.** If `gather_evidence` returns nothing on a point, say so.
  Do not fill the gap with plausible-sounding chemistry.
- **Breadth is deliberate.** "Typical protocol for X" or "what matters when solubility is
  low" is answered by surveying *many* notes (campaigns, playbooks, and the individual runs
  behind them), not one hit. If the first sweep is thin, widen the query or drop a filter.

## Generating new protocols / conditions

When asked to *propose* something new (a new set of conditions, a starting protocol for an
untried substrate):

- Build it from retrieved evidence and state the reasoning: which past runs and which
  transferable playbook it rests on, and where you are extrapolating.
- Draft it as an agent note through `propose_knowledge_note` (type `protocol` for a proposed
  procedure). It opens a **PR for a human chemist to approve** — a proposal, never asserted as
  established fact until merged (D-005). Cite the evidence notes with `[[wikilinks]]`.
- Be explicit about the untested assumptions and the risks (safety, selectivity, scale) so the
  reviewer can judge them.

## Keep integrations dumb, reason here

Data sources (ELN, ORD, future analytical or literature feeds) only map their content into the
canonical schema and the graph. All the intelligence — which sources to combine, how to weigh
them, what to generate — is this loop. If a needed source is missing, that is a new retriever
behind the one contract, not a special case in the answer.
