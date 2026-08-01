# D-2026-08-01-trust-travels-on-the-value-line — Trust travels on the value line

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** the "uncertainty never reaches a
note" backlog row · **Extends:** D-2026-08-01-unknown-is-not-fine (the `Estimate` shape), D-157
(the BO note's searched space), D-158 (`calc_refs` on the QM note)

## Context

`Estimate` gave every prediction one shape for how far to trust it, and left one hop open: its own
consequences section says "not closed: uncertainty reaching notes". A note is where a number stops
being a return value and becomes a durable record a human signs off on, so it is the hop that
decides whether any of this is load-bearing.

Two writers put a number into a note. `qm/knowledge.py` wrote `total energy: {x:.6f} Hartree`, and
`bo/knowledge.py` wrote `objective value: {x:.6g} ({provenance})`. Neither said anything about how
far to trust the figure, and both were also read a second time — the QM number through
`_envelope`'s job summary, which is the line a chemist sees *first*, and the BO number through a
retrieval excerpt into a report note.

**Verifying the row changed what it is.** It reads as a note-writer change. It is not:

- **QM has no uncertainty to carry, and cannot acquire one.** An absolute total energy has no
  meaningful error bar — this repository already says so where it differences them
  (`science/calc/reaction.py`): the method and basis-set error is enormous in absolute terms and
  cancels almost entirely in a difference.
- **BO's uncertainty exists, is computed on every model-guided ask, and was discarded.** BoFire's
  `ask()` returns `<objective>_pred` and `<objective>_sd` beside the parameter columns;
  `engine._frame_to_candidates` read the parameters and dropped the rest. The row's title was
  literally true there and pointed at the wrong module.
- **The excerpt truncates.** `retrieval/retrievers._excerpt` is a blind character prefix of the
  body at `note_excerpt_chars` (240). The BO objective value sat *after* the full conditions list,
  so a campaign over enough parameters produced an excerpt quoting the recommended conditions with
  no number attached at all.

## Decision

**One rendering, on the value line.** `Estimate.render()` puts the value, its unit, its uncertainty,
where that uncertainty came from, and any domain failure into a single inline fragment. Inline is a
constraint rather than a preference: given a blind-prefix excerpt, a trust stanza placed below the
value is cut from precisely the notes carrying the most prose and kept in the short ones that needed
it least. The trust rides on the value or it does not travel.

**Silence means checked-and-passed, and only because the other two states are spoken.** An in-domain
estimate adds no domain remark. `None` renders "applicability not assessed" explicitly — without
that, a reader could not distinguish an unasked question from an answered one, which is the whole
reason `in_domain` has three values.

**The QM half delivers a unit and a domain flag, not an error bar, and says so.** `uncertainty` is
`None` and renders as "no uncertainty established". Inventing a figure would be a fabricated
uncertainty, refused for the same reason D-2026-08-01-unknown-is-not-fine refused fabricated domain
bounds: a number in a validated record is expected to have a provenance, and one that exists gets
trusted.

**Convergence is the QM domain question.** `in_domain` asks whether the model could speak about this
case at all, and an SCF that did not converge never produced an answer it can stand behind. Carrying
it as the uniform flag is what lets a consumer which has never heard of an SCF — a skill, a note
writer, a retrieval excerpt — decline the number. `- converged:` stays as its own line: it is the
primitive fact, and the estimate is the reading derived from it.

**`qm_energy_estimate` lives in `knowledge.py`, not on `QMJobResult`.** The natural home is a
property on the model, and it is the exact mistake `tests/test_connector_isolation.py` exists to
catch: `specs.py` is the leaf module `connector.yaml` names as `params_model`, so the chat service
imports whatever it imports, and `chemclaw.science.calc.uncertainty` imports RDKit. That is how
`connectors/calc/specs.py` once dragged four science modules into every `build_agent` (D-118).

**The surrogate's posterior sd is recovered and carried to the note.** Onto `Candidate` as
`predicted_value`/`predicted_sd`, then onto the `Observation` that outlives it as `surrogate_sd`.
It is the optimizer's own statement about why it proposed a point: a small sd is an exploit of
chemistry the model has learned, a large one an excursion into chemistry it has not, and the
recommended value reads identically either way.

**`surrogate_sd`, deliberately not `uncertainty`.** It does not qualify the observation's `value`.
That value came from the evaluator — a calculator, or a real measurement — while the sd is what the
model believed *before* seeing it. Labelling a model's prior spread as the measurement's error would
be exactly the overclaim this line of work exists to prevent, so the field name, the note's wording
("at the time it was proposed") and the docstring all carry the distinction.

**A missing sd is a claim, not a gap.** A space-filling seed can win a campaign outright, and a note
that merely omitted the surrogate clause there would read as the model endorsing a point it never
saw. It says "a space-filling seed point, proposed before any surrogate had an opinion".

**The BO note leads with the value.** Value, provenance and surrogate belief first; conditions and
searched space after. This is the excerpt fix, and it is held by a test that renders a wide campaign
and runs the real `_excerpt` over it rather than asserting a character count.

## Why not the alternatives

**A structured `Estimate` field in the note front-matter.** This is what "units are prose, never a
structured field" asks for literally, and it buys nothing today: `_excerpt` reads the body, and
`EvidenceChunk` would need the field threaded through `NoteRef` and `propose_knowledge_note` to
reach any consumer at all — three places changed for zero readers, which is the abstraction-with-one-
caller this repo inlines on sight. The prose line is what retrieval actually quotes, so that is where
the fix belongs. When a machine consumer appears, the front-matter field is additive and `Estimate`
is already the shape it would carry.

**Set `Note.confidence` from the surrogate sd.** Tempting: the field exists, is plumbed end-to-end,
and doubles as the graph retriever's ranking score. It requires a mapping from an sd in the
objective's own units to a 0–1 scalar, and there is no principled one — any choice is an invented
threshold that would silently reweight retrieval. The same refusal as the statistical applicability
domain, for the same reason.

**Force the BO objective value through `Estimate`.** `unit` is `min_length=1` and a generic BO
objective has no unit — "yield" is a name, not a dimension. Passing "dimensionless" would be a
fabricated unit in the one field whose job is to stop numbers travelling without one.

**Carry BoFire's `_des` column too.** It is the acquisition/desirability score, a ranking quantity in
the strategy's own units rather than a statement about the chemistry. Recording it beside a real
posterior sd would invite reading it as a second confidence.

**Drop `- converged:` now that the estimate line says it.** It only says it in the failure case; in
the success case silence carries it. Removing a primitive fact from a GxP record as a side effect of
a formatting change is not a trade this row is entitled to make.

## Consequences

- Every number these two writers put into a note now arrives with its unit, its uncertainty or an
  explicit statement that none is established, and a loud marker when it is out of domain — inside
  the excerpt a reader actually sees.
- The QM job summary and the note's energy line are one rendering, so the surface read first can no
  longer say less than the durable record. Nothing pinned that summary's format before: the one test
  naming it builds the string as fixture data rather than calling `_envelope`.
- A `bo-candidate` note distinguishes a model recommendation from a lucky seed, which
  `evals/metrics.py::bo_regret` has no way to express and a reviewer needs before booking lab time.
- `Observation` and `Candidate` gained optional fields. Both default to `None`, so stored campaign
  records deserialize unchanged.
- **Still open: `Estimate` is a three-writer contract.** `pka`, `logd`, `reaction` and `xtb_thermo`
  each carry an uncertainty in their own field name and have not been converted; they do not
  currently write notes, which is why this row did not force it.
- **Still open: `conformal_uncertainty` has no caller.** It needs a database read of the calibration
  ledger, so it belongs on the cached path, and `calibration_conformal_coverage`/`_min_samples`
  stay configured and unread until that is wired.
- **Still open: measured values in ELN notes are prose too** (`ingest/eln/note.py` writes
  `temperature: {x} °C`). A measurement's uncertainty is the instrument's, which the ORD payload
  does not carry, so this is an ingest-schema question rather than a formatting one.
