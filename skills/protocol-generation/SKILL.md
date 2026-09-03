---
name: protocol-generation
description: >-
  Use when the chemist asks for a protocol, a procedure, a recipe or conditions to run — "how
  should I run this coupling", "write me a procedure for", "what conditions for this substrate",
  "give me a protocol for the deprotection". Structures their free-text ask, argues the conditions
  from the record and from computation, and stores a runnable design a human can edit. For a plate
  of conditions rather than one experiment, use hte-campaign-design instead; for "which point
  should I try next" over a table of runs you already have, that is experiment-design.
tools:
  - structure_experiment_request
  - draft_experiment_protocol
  - read_experiment_protocol
  - find_experiment_protocols
  - resolve_compound
  - substrate_precedent
  - conditions_for_similar_reaction
  - conditions_for_similar_product
  - reagent_frequency
  - workup_precedent
  - similar_reactions
  - similar_molecules
  - condense_protocols
  - gather_evidence
  - expand_note
  - recall_observations
  - find_past_jobs
  - screen_hazards
  - screen_genotoxic_alerts
  - ich_impurity_limit
  - stoichiometry_table
  - green_metrics
  - predict_pka
  - predict_solubility
  - predict_logd
  - compare_solvents
  - compute_reaction_energy
  - predict_site_reactivity
  - ask_clarifying_question
---

# Writing a protocol

A protocol is the one artifact in this system that somebody acts on in a laboratory. Everything
else here is a claim about the world; this is an instruction to spend a chemist's day and a
milligram of material they may not be able to replace. Write it accordingly.

The mechanics — the shape, the checks, the plate arithmetic, the revision history — are in
`chemclaw.protocols` and you do not have to think about them. What is yours is the argument: *why
these conditions and not the twenty other sets that would also have looked plausible.*

## The order, and why it is this order

**1. Structure the ask before you search.** Call `structure_experiment_request` first. A chemist
writes one sentence containing a transformation, a scale, an exclusion and a deadline, and your
reading of it is a hypothesis. Structuring it costs one call and puts your reading in front of them
while correcting it is free; discovering on the fifth tool call that they meant the *other* aryl
halide costs the whole turn.

Mark every slot honestly. `stated` obliges the chemist's verbatim words and is checked against
their text — a paraphrase is refused, deliberately. `inferred` is the right answer more often than
you expect and is not an admission of weakness: "24 wells" implying a 24-well plate is an
inference, and saying so is what lets a chemist correct the one you got wrong. `absent` is honest.
A silently defaulted scale is the field that gets somebody's material wasted.

Resolve every named species with `resolve_compound` before you fill `components`. **Never write a
SMILES from a name by hand.** A component that will not resolve stays without a structure and is
reported — which is a question the chemist answers in one word, and a guessed structure is one they
never get asked.

**2. Read the record before you compute anything.** This is the step that gets skipped and it is
the one that carries most of the answer. In roughly this order:

- `substrate_precedent` — has *this substrate* been used in this role before? The single most
  informative query, because a substrate that has failed four times is the whole answer.
- `conditions_for_similar_reaction` and `conditions_for_similar_product` — what conditions did
  transformations like this one actually run under? These are the corpus's own answer to the
  question you are being asked, and they are grounded in runs rather than in a model.
- `reagent_frequency` — for the named reaction or the product's functional group, what catalyst,
  ligand, base and solvent does the record reach for, and how often? A modal answer with a count
  behind it is an argument; your prior is not.
- `workup_precedent` — how was this reagent actually quenched and worked up? Work-up is where a
  generated protocol is usually thinnest and where a chemist notices first.
- `similar_reactions` / `similar_molecules` for structural neighbours the labelled facets miss,
  and `gather_evidence` for anything written down that is not a reaction record — a share
  document, a playbook, a campaign note.
- `recall_observations` and `find_past_jobs` — what has this system already noticed or already
  computed? A calculation you re-request is a minute somebody waits for an answer that exists.

Then `condense_protocols` over the hits. Do not `expand_note` twenty procedures one at a time; the
comparison is what you want and reading twenty bodies will exhaust the turn before you draft
anything.

**3. Compute what the record does not state.** The record is a record of what people did, so it is
silent on exactly the questions a new substrate raises. Reach for a tool whenever a decision turns
on a number nobody has measured for *this* molecule:

- a base or a wash pH, and the substrate has an ionizable group → `predict_pka`
- "will it dissolve at that concentration" → `predict_solubility`
- an extraction or a partition → `predict_logd`
- choosing between solvents on more than availability → `compare_solvents`
- "is this even downhill" → `compute_reaction_energy`
- "which position will it go at" → `predict_site_reactivity`
- what to weigh out at the chemist's scale → `stoichiometry_table`
- how wasteful the route is, when the chemist cares or the scale is real → `green_metrics`

**4. Screen for hazards, every time.** `screen_hazards` over the species, and
`screen_genotoxic_alerts` and `ich_impurity_limit` when the chemistry or the solvent calls for
them. This system **flags and never certifies** — say that in the same breath as the flag — but an
unscreened protocol does not even carry the flag, and this is the one omission that can hurt
somebody. `draft_experiment_protocol` warns when no screen is cited; do not let it have to.

**5. Draft, then read the checks back.** `draft_experiment_protocol` takes the `design_id` step 1
returned and the *protocol* half only — the shared body, the factors, the arms, the evidence. It
does not take the ask back, because the design already holds the one the chemist corrected, and a
second copy is a second thing that can be wrong.

It refuses a design that fails any *blocker*, and there are seven: a document that is not a protocol
at all, a **structure that does not parse**, a charge table whose equivalents contradict its amounts,
an arm setting a level no factor declares, a plate that cannot hold its arms, a reagent the chemist
forbade, and — the one that should never fire if you did steps 2 and 3 — no followable citation.
This paragraph used to name only the last of those and call everything else a warning.

The second one is worth stating exactly, because this line used to say "a species that will not
resolve" and that is the *warning*, not the blocker. A component you named and could not resolve
comes back as a failed warning — the design still stores, and you are expected to say so. What
blocks is a SMILES the design already carries that does not parse, which is a different fault: a
structure somebody wrote down wrong rather than one nobody looked up.

What is *not* a blocker is a *warning* or a *note*, which means it is a judgment for the chemist,
not a nuisance for you to suppress. A warning you do not mention is a warning you have decided on
their behalf.

## What separates a good protocol from a plausible one

**Every number has a provenance, and there are only three kinds.** It came from a run
(`precedent`), from a tool (`predicted`), or from nowhere (`assumed`). Put the right one on
`expected.basis` and say which is which in the prose. `assumed` is a perfectly good answer that a
chemist can argue with; a fabricated `precedent` is one they cannot, and it is the failure that
destroys trust in everything else you wrote.

**Cite what supports what.** An `EvidenceRef` carries `supports` — the paths in the design it is
offered for, like `base.setpoints.temperature_c`. A bibliography at the bottom is not evidence; a
citation next to the number is. This is also what the chemist checks first when they disagree.

**Write the procedure a chemist can follow, not a summary of one.** Ordered steps with real
instructions. Charge, addition order, atmosphere, temperature ramp, hold, sampling points, quench,
work-up, purification. The two places a generated procedure is reliably thin are the **work-up**
and the **in-process control**, and they are the two a bench chemist reads first — `workup_precedent`
exists because of the first, and `in_process_controls` is a field because of the second.

**Name what you would measure and when.** An `Analytic` whose `measures` does not cover the stated
objective means the experiment comes back unanswerable; the check says so, and it is a warning
rather than a blocker because sometimes the chemist has an assay you cannot know about. Ask.

**Say what would falsify it.** A protocol that cannot fail informatively is a protocol that only
tells you whether you were lucky. State the outcome that would send the chemist somewhere else.

## When to stop and ask

`ask_clarifying_question` when the answer changes the whole design and no default is defensible:
the scale is unknown and the chemistry is scale-sensitive; two readings of the transformation give
different products; the substrate has a stereocentre the ask does not mention. Do **not** ask about
something the record can answer — that is a search you have not run.

## Revising

A first draft is almost always altered, and that is the system working. When the chemist asks for a
change, `read_experiment_protocol` first — revise the *current head*, not your memory of it, because
they may have edited it in the browser since you last saw it. Then call
`draft_experiment_protocol` again with the same `design_id` and the head's `parent_revision`: it is
one tool for the first draft and every later one, so there is nothing else to learn. A stale parent
is refused rather than allowed to discard their edit, and the refusal names the revision you should
have started from.

Send the whole protocol each time, not only the part that changed — the revision is a complete
document, and the diff against its parent is computed for you and comes back in `changed_paths`.

Say what you changed and why in `change_note`. That note and the diff beside it are what a later
reader has when they ask why this protocol looks the way it does.

## What this skill is not for

- **A plate of conditions** — that is `hte-campaign-design`, which owns factor choice, levels,
  design type, controls and replication.
- **"What should I run next" over a table of runs** — that is `experiment-design`, which frames a
  Bayesian-optimization problem. A protocol is what you write once that question is answered.
- **Reading a series to see where it got to** — `experiment-progression`.
- **Asserting something new about the world** — a protocol is a proposal to act, so it is a stored
  document rather than a knowledge note. If a *rule* comes out of it later, that is
  `propose_knowledge_note` through the PR-gate, citing the design.
