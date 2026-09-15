# D-2026-09-15-a-relation-with-no-legal-target-is-a-question-nobody-can-answer — the analytical record could not hold a method, and somebody had already worked around it

**Status:** accepted · **Date:** 2026-09-15

## Context

`kg/relations.py` declares `measured-by` — *"this claim rests on that experimental method or
instrument"*. `KNOWN_NOTE_TYPES` is a **closed** vocabulary (`kg-validate` fails an unknown type)
of eleven: reaction, compound, campaign, optimization-campaign, playbook, interaction, report,
job-result, experiment-proposal, failure-mode, plus `bo-candidate` from a bundle. None of them is a
method or an instrument.

So the edge had no legal target. **And this is not a gap nobody had met.** The one `measured-by`
edge in the shipped corpus, in `knowledge/reaction/rxn-aspirin-acetylation.md`:

> Yield determined by mass after recrystallisation from ethanol/water; see
> `[[measured-by:playbook-recrystallisation-purity]]`.

It points at a **playbook** — a transferable rule about quoting a yield with the purification that
produced it. A corpus author wanted to say how a number was measured, found nothing in the
vocabulary that a method could be, and pointed at the nearest thing.

Two more of the same shape, found in the same sweep:

- **`Impurity` had no RRT field.** Its own docstring says an ELN reports "often only a
  chromatographic name/RRT"; `ingest/eln/warehouse/binding.py` says a site's analytics table
  carries "a chromatographic name or RRT far more often than a structure". The model held `name`,
  `smiles`, `area_percent`. So the one identifier that distinguishes two unresolved peaks at 0.11%
  and 0.19% fell to `OrdReaction.attributes`, a `dict[str, str]` whose own docstring says it holds
  "strings, not values" with "no unit to normalise to".
- **The system prompt denied a "method store".** That clause sat in a list of capability
  denials — a chromatographic model, a column database, an NMR prediction — every one of which is
  true of every deployment.

## Decision

**Give the relation a target, give the peak a name, and stop denying what a chemist can record.**

- **`analytical-method` joins the vocabulary.** It holds a method somebody ran, as they recorded
  it. Nothing here devises one: there is no chromatographic model in this family and this change
  adds none. What changes is that a method a chemist states can be recorded, cited, and pointed at
  by the results that rest on it, instead of being prose inside a reaction note that no edge can
  reach.
  **It has a producer the day it lands**, which is why it is not the defect it would otherwise be:
  `record_knowledge_note` takes a free `type`, so a chemist telling the agent their assay method is
  a path that already exists. A note type with no producer is the
  claim-that-a-capability-exists shape this tree deleted with `reject_widening` and
  `map_to_hpc_identity`.
- **`Impurity.rrt`**, wired through *both* ingest producers (`json_adapter`, `warehouse/adapter`)
  and the warehouse binding — a field only one path could fill would be half the same defect.
  Positive and unbounded above; unitless and **method-relative by construction**, which is what the
  new note type and the `measured-by` edge are for. The validator still refuses an RRT-only
  impurity, deliberately: an RRT says where a peak eluted, not what it is, and a row nobody can name
  is a row no query can join. A chemist's own way of referring to one — "the RRT 0.94 peak" — is a
  name, and belongs in the name.
- **The prompt drops "method store" and states the capability instead.** The distinction is the
  point: every other clause denies a *capability*, and a method store is *content*. This system has
  always been able to hold a note, so the denial was false wherever a chemist had written one down,
  and telling a model it cannot reach something it can reach costs a turn. What replaces it is
  explicit — *"Nothing here predicts a separation"* — plus one clause distinguishing quoting a cited
  parameter from devising one.

## What did not need changing, and the check that established it

The obvious worry was `verifier.ungrounded_parameter_shapes`, which flags `\d{3} nm`, flow rates and
column brands in a finished answer, and exists because a stronger model once emitted a complete
branded HPLC method table in the same reply as the sentence "not a validated method". A method note
type looked like it would make that guard fire on a *correct citation*.

It does not. The guard already exempts any parameter class present in `tool_outputs`, and reading a
note is a tool output — so a cited method's wavelength has always been clean. The guard needed no
change; the prompt clause exists only to stop the *model* over-refusing what the verifier would
already allow. Checking that before building it is the difference between a change and a change
plus an unnecessary edit to a safety control.

One interaction is worth recording rather than acting on: that guard over-fires *because* no tool
returns these classes, and it ships off (`answer_shape_gate_enabled=False`) for exactly that reason.
A deployment with real method notes would see fewer false positives — which is an argument for this
schema, not for flipping a default whose reasoning still holds.

## Consequences

- `measured-by` can be used as declared, **and the wrong edge now refuses**. This consequence
  originally read that the corpus's workaround edge would be left alone — falsified within the hour
  by `tests/test_seed_corpus.py::test_every_note_type_has_a_real_instance`, whose argument is that
  "a type nothing in the corpus uses is a type no retrieval filter has ever been exercised on". It
  is right, and it forced the better answer: the seed corpus gains the method note that reaction had
  been describing in prose all along, and its edge points at it.
- **`RELATION_SIGNATURES` gains `measured-by`**, which is what makes this structural. The type
  existing makes the right edge *possible*; only a signature makes the wrong one refuse. Driven
  against the corpus's own former edge, `kg-validate` now reports: *"asserts 'measured-by' toward
  'playbook-recrystallisation-purity', a 'playbook' note — that relation targets
  ['analytical-method']"*. That is the same argument the signatures block already makes about twelve
  backwards edges that merged green under a validator checking relation *names* only.
- An analytics table with an RRT column can be bound and ingested. Nothing infers one.
- **The prefix cost was paid down twice, not waived.** The prompt edit measured 39 tokens over
  `tests/test_context_floor.py`'s ceiling, then 1 over; it was tightened both times. The ceiling did
  not move — the same outcome as
  `D-2026-09-15-a-suggestion-is-points-and-a-design-needs-labels`, one commit earlier.
- This is the first increment of the analytical record and is deliberately schema only. The repo's
  own capability audit scores analytical development at 8%
  (`docs/archive/REVIEW-2026-09-13-capability-audit-and-plan.md`), and most of the remainder —
  retention models, NMR/MS prediction, ICH guidance text — is `Chemclaw3-mcp` work with real
  data-sourcing questions. What this repo owns is the record, and a record that cannot hold a method
  gives every one of those servers nowhere to write.

## What keeps it true

- `tests/test_knowledge_gaps.py::test_every_relation_this_graph_declares_has_a_note_type_that_can_be_its_target`
- `tests/test_eln.py::test_an_impurity_carries_the_rrt_its_docstrings_have_always_named`
- `tests/test_eln.py::test_an_rrt_alone_does_not_identify_an_impurity`
- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling`
