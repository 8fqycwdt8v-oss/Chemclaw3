# D-2026-09-20-a-swept-axis-is-a-choice-an-invented-argument-is-a-lie — a computable check runs when every argument is grounded in the record, and the one argument the model may choose is the one the answer reports

**Status:** accepted · **Date:** 2026-09-20 · Closes the deferral
`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` opened and
`docs/planning/BACKLOG.md` carried ("A `computable` discriminating check is derived and then not
run"). Does **not** supersede it: that ADR's refusal named a condition, and this is the condition
being met.

## Context

`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` shipped the tournament with half of the
original ask unbuilt. The ask was "if it can be done using tools available to the agent it should
simply happen"; what shipped identified which checks this system's tools could settle and handed
every one of them to the chemist anyway, `run_computable_check` returning `not-run` with its
reason.

The reason was not effort. A check arrives as free text — *"compare the barrier for the two
pathways"* — and running it means producing tool arguments: which molecule, which conformer, which
solvent, which charge, which atoms. A model asked to fill a schema fills it. A fabricated argument
produces a **real number**, and a real number in a table headed "computed" is read as computed.
That is strictly worse than not running the check: a missing verdict is visible, a confidently
wrong one is not.

The backlog row named what would close it: arguments validated against the target tool's own
surface *offline*, the way a warehouse `connection:` block already is, so a check that cannot be
grounded in the record is refused rather than guessed at.

What forced the shape past that was the second half of the ask, put later: the chemist wants the
system to calculate things it was not handed — *"solubility in different solvents … in a
self-proposed list of solvents"*. That is a model choosing an argument, which is the exact thing
the refusal exists to prevent.

## Decision

**Every argument of a dispatched check is grounded, and grounding has one definition: the value
came from the record or from the tool.** Three sources and no fourth.

1. **A structure comes from a note.** The check names `subject_note_id` (or `subjects`, role →
   note ids). `dispatch.structure_of` requires the note to be a `compound`, requires it to carry a
   structure, and puts that structure through `core.chem.require_canonical_smiles`. A model that
   writes a SMILES string into the check cannot reach the calculator with it — there is no field
   that carries one.
2. **Everything else stays at the tool's own default.** A required argument that is neither a
   structure nor the swept axis and has no default is refused, because supplying it is the failure
   this module exists to prevent. Two tools whose *defaults* are wrong for an arbitrary molecule
   (`compute_thermochemistry`, `symmetry_number`) are refused by name, since arity does not catch
   a default that is silently inappropriate.
3. **One axis may be varied, and only where a vocabulary validates the values** (`solvents`
   today). This is the self-proposed solvent list, and it is allowed for a reason that does not
   generalise: **the harm in invention is a hidden assumption, and a swept axis is the most
   visible part of the answer.** A charge the model picked is invisible in the number it produces;
   the solvents compared *are* the result, they are reported beside it, and each value is checked
   by the job's own `precondition` (`require_supported_solvents`) before anything runs. An axis the
   model chose that nothing validates is refused (`axis-not-sweepable`).

**The two halves are separate mechanisms because the surfaces are.** An in-process tool is checked
against `agent/template_surface.ToolArguments` — the single existing authority for "what does this
tool accept", used offline via `of_signature` — and a durable job against its manifest's declared
`params_model`, which is the same authority `prepare_job_launch` validates against, so what reaches
the child workflow is the payload the job accepted and never the model's proposal.

**Refusal is the default and omission fails closed.** A field this module has not classified makes
a job undispatchable; it never makes one runnable. Two ratchets pin what that currently admits —
**12 tools** and **9 of the 12 shipped calc jobs** — and each fails loudly when the set moves, in
either direction.

**The three refused jobs are refused on the same ground and are the sharpest case for it.**
`scan_coordinate` needs `atoms`, `profile_rotation` a `torsion`, `survey_bond_strengths`
`cleavages`. A model produces atom indices fluently and wrongly, and **nothing in the resulting
number says which atoms were driven** — a scan over the wrong pair reads exactly like a scan over
the right one. They are declined, not deferred-by-omission, and the condition for reopening is
stated below.

**Bounded twice, because one bound does not see the other.**
`hypothesis_max_calculations` (2) caps how many checks one tournament starts — a command count, so
it is resolved through `resolve_field_limits` rather than read in workflow code. It counts
*checks*, and one check sweeping a dozen solvents is a dozen conformer searches inside a single
child workflow, which is the budget escaping through the one argument the model is allowed to
choose. `hypothesis_max_sweep_values` (6) is what bounds that. An over-wide axis is **refused,
not trimmed**: the swept values are reported beside the answer, so dropping some would make that
report untrue, which is the failure this whole ADR is about, arriving by the back door.

**A check that did not run says why, in a form that counts.** `CheckOutcome.refusal_code` is a
stable token (`subject-not-a-compound`, `subject-has-no-structure`, `field-cannot-be-grounded`,
`axis-too-wide`, `over-budget`, …) beside the readable detail, and `CheckOutcome.ran` carries the
call as it was made, swept axis included. The report's verb comes from `ran`, never from
`check.kind` — reading it off the kind is how the shipped version printed "(ran; …)" for a check
nothing had run.

## Alternatives considered

**Let the model write the SMILES.** Rejected on the asymmetry that runs through this whole file: a
wrong structure is undetectable downstream, and the tournament's hypotheses already cite notes, so
the record is right there. The cost is real and is stated rather than hidden — a check about a
compound with no note is `physical`, which is sometimes the wrong answer.

**Validate the model's arguments with a schema and run whatever passes.** This is what the backlog
row could be read as asking for, and it is not enough: `atoms=[3, 7]` passes every schema
`scan_coordinate` has. Type-validity and groundedness are different properties, and only the second
one is about whether the number answers the question asked.

**Trim an over-wide sweep to the budget.** Cheaper and friendlier, and wrong for the same reason as
everything above: the reported axis would no longer be the axis computed.

**Let a check ask for a *second* varied argument.** Declined for now — two axes is a factorial
design, and the budget is a per-check count that would stop bounding it. Nothing in the ask needed
it.

## Consequences

The half of the ask that did not ship now does: a tournament whose leading check is a solubility, a
pKa, an xTB energy or a solvent comparison over compounds the record holds runs it and returns a
verdict, and one whose check needs a laboratory — or an argument nothing grounds — still returns a
proposal and says which.

What a reader has to do differently: read the `ran:` line before the verdict. A number computed in
the default solvent answers a different question from one computed in the solvent the hypothesis is
about, and that line is the only thing that distinguishes them.

Verified against the shipped manifests rather than fixtures: the ratchets derive their sets from
`connectors.registry.discovered()` and the jobs' own `model_fields`, so a manifest change that
widens or narrows what is dispatchable reds the suite.

**Revisit when:** a deterministic enumerator ships that produces the candidate set a refused job
needs — bond cleavages, rotatable torsions, substitution products — so the check can rank an
*enumeration* instead of naming indices. That is this repository's existing doctrine that
enumeration and calculation are separate tools and the order is not optional, and it is the thing
that would make `scan_coordinate`, `profile_rotation` and `survey_bond_strengths` groundable
without a model choosing an index. The file that shows it has fired is
`tests/test_hypothesis_dispatch.py::test_the_dispatchable_job_set_is_exactly_what_is_pinned`, whose
pinned set would have to grow.
