---
name: deep-research
description: >-
  How to answer any open-ended process-R&D question — about any output (yield, purity,
  impurities), any process detail or observation, or general protocol guidance — by
  composing every data source and tool, and how to draft new conditions/protocols grounded
  in that evidence. Cite everything, separate evidence from analogy, and route anything new
  through the PR-gate.
tools:
  - gather_evidence
  - find_notes
  - expand_note
  - condense_protocols
  - similar_reactions
  - similar_molecules
  - substructure_matches
  - compute_xtb_energy
  - predict_pka
  - predict_solubility
  - suggest_next_experiment
  - propose_knowledge_note
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
   `note_type` or `tag` filter to narrow. It returns cited `chunks`; there is no need to query
   sources one by one for a first pass.

   **Read what the sweep says about itself before you read the chunks.** `sources_failed` names
   a source that could not be asked at all — with a name there, the sweep is about less than the
   whole corpus however complete the chunks look, and that has to reach the chemist.
   `truncated_by` says a cap cut the list and `total_before_cap` says how much there was:
   `count` means narrow with a `note_type`/`tag`/date filter, `chars` means the sources are
   returning long chunks and a narrower question will reach further. A truncated sweep is not a
   small corpus.
3. **Drill in — one protocol or many.** For a *single* cited note, `expand_note` gives the
   full body: the step-by-step recipe, per-step conditions, the verbatim procedure prose, and
   outcomes. That prose is where impurities, observations, and robustness rationale live; read
   it, don't just read the headline numbers.

   For **many** protocols — the usual case after `similar_reactions`, or any question of the
   form "what have we tried" — use `condense_protocols` with the whole list of ids instead of
   calling `expand_note` once per protocol. It reads each protocol whole (never split, and one
   too large to read is named rather than shortened) and returns one comparison: the recorded
   conditions and outcomes side by side, the solvent/reagents/work-up read out of each
   procedure, and **what each run changed relative to the one before it**. That last column is
   usually the answer to "what moved the yield" — read the trajectory, not the rows.

   Calling `expand_note` twenty times is the failure this replaces: it costs a model round-trip
   each, and the earliest bodies are cleared from your context before you write the answer, so
   you end up reasoning about the last two and your memory of the rest. Cite straight from the
   comparison — every row carries its reference — and fall back to `expand_note` for the one
   protocol whose full text you actually need.

   Read `complete` on the result. It means every reference *you passed* was read; it never means
   you have seen every protocol on file — that question belongs to the search that produced the
   references, whose own `verdict`/`hits_truncated` you must read separately.
4. **Cross-learn by structure**, not only by text:
   - `similar_reactions(reaction_smiles)` — past runs of the *same* transformation (the
     history behind "what has been tried" / "what moved the yield").
   - `substructure_matches(query)` then `find_notes(smiles)` — reach reactions where a
     specific functional group is present (e.g. a free primary amine in a Buchwald–Hartwig).
   - `similar_molecules(smiles)` — analogous substrates when the exact one is absent.
   - An `optimization-campaign` note already lays out one transformation's runs side by side;
     a `playbook` note is the transferable rule across projects — start from these when they
     exist.
5. **Compute when the record is silent — proactively.** If the answer turns on a property the
   notes do not state (weighing an untried solvent against the tested ones, a pKa, a relative
   stability), run it yourself with `predict_solubility` / `predict_pka` / `compute_xtb_energy`
   and fold the result — with its uncertainty — into the answer; do not stop at "the ELN does
   not say". Everything available is semiempirical: where the question needs more accuracy than
   that, say so rather than quoting a number the method cannot support.
6. **Design the next experiment when that is the question** — by one of two paths, chosen
   deliberately and named in the answer.
   - *Reason it from the series* when the chemist is working a step day by day and the question
     is really "what do the last few runs mean, and what follows from them": read the
     `optimization-campaign` note in its time order and follow the `experiment-progression`
     skill. No surrogate model, and no pretending there is one.
   - *Optimize it* when the objective is one scalar over well-bounded variables with enough runs
     behind it: `suggest_next_experiment` (BoFire's ask step) — frame the decision space, turn
     the historic runs you gathered into observations, and propose the next point(s). See the
     `experiment-design` skill.

   Either way the result is a proposal a human runs, not a fact.

## Discipline (non-negotiable)

- **Cite the note id behind every factual claim.** An answer a reviewer cannot trace is not
  usable. When you state a number, name the reaction/campaign note it came from.
- **Evidenced vs. analogy, kept visibly separate.** "We ran this exact reaction at 80 °C and
  got 85% ([[reaction-x]])" is evidence. "A similar coupling tolerated a free amine
  ([[reaction-y]]), so this one may too" is analogy — label it as such, never as fact.
- **Silence is an honest answer — once you have checked it is silence.** If `gather_evidence`
  returns no chunks on a point, say so, and do not fill the gap with plausible-sounding
  chemistry. But check `sources_failed` and `truncated_by` first: an outage and a cut both look
  like an absence in the chunk list alone, and reporting either as "we have no prior art" is a
  confident claim about a question that was never fully asked.
- **Breadth is deliberate.** "Typical protocol for X" or "what matters when solubility is
  low" is answered by surveying *many* notes (campaigns, playbooks, and the individual runs
  behind them), not one hit. If the first sweep is thin, widen the query or drop a filter.

## Generating new protocols / conditions

When asked to *propose* something new (a new set of conditions, a starting protocol for an
untried substrate):

- Build it from retrieved evidence and state the reasoning: which past runs and which
  transferable playbook it rests on, and where you are extrapolating.
- Draft it as an agent note through `propose_knowledge_note` — type `experiment-proposal`, the
  kind for anything you are suggesting be *run* rather than reporting as done. It opens a **PR
  for a human chemist to approve** — a proposal, never asserted as established fact until merged
  (D-005). Cite the evidence notes with `[[wikilinks]]`.
- Be explicit about the untested assumptions and the risks (safety, selectivity, scale) so the
  reviewer can judge them.

## Keep integrations dumb, reason here

Data sources (ELN, ORD, future analytical or literature feeds) only map their content into the
canonical schema and the graph. All the intelligence — which sources to combine, how to weigh
them, what to generate — is this loop. If a needed source is missing, that is a new retriever
behind the one contract, not a special case in the answer.
