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
   this module exists to prevent. One tool is refused *by name* — `compute_thermochemistry`, whose
   `symmetry_number` defaults to 1, "no rotational symmetry", which is false for most molecules and
   silently wrong in the free energy it produces. Arity does not catch a default that is
   inappropriate rather than absent, which is why the list exists at all and why it is one entry
   and not a policy.
3. **One axis may be varied, and only where a vocabulary validates the values** (`solvents`
   today). This is the self-proposed solvent list, and it is allowed for a reason that does not
   generalise: **the harm in invention is a hidden assumption, and a swept axis is the most
   visible part of the answer.** A charge the model picked is invisible in the number it produces;
   the solvents compared *are* the result, they are reported beside it, and each value is checked
   by the job's own `precondition` (`require_supported_solvents`) before anything runs. An axis the
   model chose that nothing validates is refused (`axis-not-sweepable`).

**The two halves are separate mechanisms because the surfaces are.** A tool is a connector
endpoint — measured, every dispatchable one is; none is in-process — so its contract is read from
the JSON schema its live session advertises, reduced by `agent/template_surface.ToolArguments`, the
single existing authority for "what does this tool accept". `of_schema` is that reduction and was
extracted here, because this was the *third* place reading a live tool's schema and the module's
own header says a third reading is the drift the type was created to prevent. A durable job is
checked against its manifest's declared `params_model`, which is the same authority
`prepare_job_launch` validates against, so what reaches the child workflow is the payload the job
accepted and never the model's proposal.

**And every key the grounding produces has to be one the target declares.** These params models do
not set `extra="forbid"`, so pydantic's default *drops* an unknown key rather than rejecting it —
measured, a `solvents` axis handed to `sample_conformers` vanished on validation, the plain
gas-phase conformer search ran, and the grounded call still reported three solvents compared, one
of them a name no vocabulary had ever seen. The same held for a structure role the job does not
take, and on the tool half a swept axis was read by nothing at all. **An argument the dispatcher
silently discards is the same hidden assumption as one it invents, and worse, because the report
goes on claiming it.** All three refuse now, and the reported call is built from the *validated
payload* so that it does not depend on those refusals holding.

**A job that names no subject is not a discriminating check.** Nothing about argument-level
grounding catches this: `republish_calculations` declares no required field, so "every required
field is grounded" was vacuously true, and a model naming that string would have had the tournament
start a corpus-wide push of calculation records to an external result store. Requiring a structure
from the record is what keeps the set to calculations *about a molecule* — and the ratchet that
looked like the guard here could not see it either, because `find_job` searches every enabled
connector while the pin was derived from one bundle.

**Refusal is the default and omission fails closed.** A field this module has not classified makes
a job undispatchable; it never makes one runnable. Two ratchets pin what that currently admits —
**12 tools** and **9 of the 12 shipped calc jobs**, the job one derived over *every enabled
connector* rather than one bundle — and each fails loudly when the set moves, in either direction.

The tool pin is narrower than it looks and the ADR says so rather than leaving it to be found: it
is derived from `resolvable_signatures()`, which needs a local `server/tools.py`, so it covers the
tools this tree holds and not the **21** an enabled bundle declares and serves from
`Chemclaw3-mcp`. Those are on the surface a check is dispatched against and are judged there by the
same live-schema rule; what no reviewer has read is their *defaults*. The blind spot is asserted —
three bundles, named — for `tests/test_context_floor.SERVED_ELSEWHERE_ALLOWANCE`'s reason: a
ratchet measuring a smaller system than a turn runs, with nothing saying so, is how a pin becomes
false while staying green.

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

**One budget over both halves, spent down the ranking.** A tool check is a real semiempirical
calculation on a cache miss exactly as a job check is, so bounding only the jobs left the
cheaper-*looking* half free to start one per hypothesis. And the budget is spent in the order the
fit produced, not the order the generators emitted: taking the first two could refuse the
**leader's** check for budget while running one that placed last, which inverts the only thing the
ranking is used for. `_propose` has always taken its own budget off `outcome.ranked`; this is the
same rule for the more expensive resource. A check past the cut says it placed below the cut —
never that a calculation "had already started", which is a claim about the allowed checks that
nothing verified, since the affordable one can itself refuse at grounding and start nothing.

**A check that did not run says why, in a form that counts.** `CheckOutcome.refusal_code` is a
stable token (`no-subject`, `subject-has-no-structure`, `field-cannot-be-grounded`,
`axis-not-declared`, `axis-too-wide`, `over-budget`, …) beside the readable detail, and
`chemclaw_hypothesis_check_refusals_total{code}` counts them — a deployment whose corpus carries no
structures looks identical from outside to one whose questions all need a laboratory, and the code
is what separates them. `CheckOutcome.ran` carries the call as it was made, swept axis and the
job's own unstated defaults included; the report's verb comes from `CheckOutcome.verdict`, never
from `check.kind` — reading it off the kind is how the shipped version printed "(ran; …)" for a
check nothing had run.

**The defaults are disclosed because on the job half the degradation is otherwise invisible.** A
job warns loudly about an unstated `symmetry_numbers` — a reaction reports no ΔG at all, a species
ranking is computed at sigma=1 and says so — but those warnings live in the structured result and
the one-line `summary` is what a check reads. Derived from the params model rather than curated, so
a job that gains an optional field discloses it without anyone remembering to.

**The launch is governed like a chemist's own.** `audited_launch` puts `prepare_job_launch` through
the same chain a chat turn's launch goes through, so an expensive job a tournament chose leaves an
audit row that reads like one a person asked for, and the child carries the manifest's
`publish_to_graph`, `timeout_seconds` and `awaits_answer` — "a field the template path does not
carry is a field that silently means something else on that path", which was written about exactly
this mistake one path over. The requester's **roles** ride to both activities: every calc job is
`expensive: true`, so binding an empty set made `authorize_trigger` decide against an actor holding
nothing and killed the durable half wherever Entra runs, as an ordinary-looking grounding refusal.

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

**Revisit when:** a check can name a *tool result* as a subject, rather than only a note id.

The trigger this was first written with — "when a deterministic enumerator ships" — was **already
met on the day it was written**, and checking rather than assuming is the only reason it did not go
into the record wrong. `connectors/chem/connector.yaml` declares `enumerate_bond_cleavages`,
`enumerate_torsions`, `enumerate_tautomers`, `enumerate_protonation_states`,
`enumerate_stereoisomers` and `enumerate_degradants`, served from `Chemclaw3-mcp`, and
`BondCleavageSpec`'s own docstring says it is "one bond to break, **as `chem`'s
`enumerate_bond_cleavages` reports it**" — the two halves were built to fit and nothing joins them.
That is `D-092`'s failure exactly, the one `CLAUDE.md` names: a condition met in the sibling
repository that no reader of the ADR was watching.

So the missing piece is not an enumerator. It is that `CheckCall.subjects` carries **note ids and
nothing else**, so there is no way to express "enumerate the cleavages of this compound, then
compute their bond strengths" — the enumeration's output is a set of structures that exist in no
note. Closing it means a second grounded source beside the corpus: a *deterministic* tool whose
output is the candidate set, with the same rule applied to it (the model selects the enumerator and
the subject; it writes no member of the result). That also unlocks the scenario this ADR cannot
serve today — ranking substitution products nobody has written down — and would need a
substitution-product enumerator, which is the one shape the six above do not cover.

The file that shows it has fired is
`tests/test_hypothesis_dispatch.py::test_the_dispatchable_job_set_is_exactly_what_is_pinned`, whose
pinned set of 9 would have to grow: `scan_coordinate`, `profile_rotation` and
`survey_bond_strengths` are exactly the three whose required field an enumerator produces.
