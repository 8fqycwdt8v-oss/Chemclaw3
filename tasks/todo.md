# Dispatching a computable discriminating check — plan

**Status:** plan. Closes the `BACKLOG.md` row opened by
`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate`, whose `Revisit when:` names the exact
condition: *"a structured check type exists whose arguments are validated against the target tool's
own signature offline, the way a `connection:` block already is."*

The previous occupant of this file was the hypothesis tournament's own plan and review, merged as
#424 and archived to `docs/archive/plans/hypothesis-tournament.md`.

## The problem, restated from the deferral

A tournament derives a discriminating check per hypothesis. Where the check is answerable by this
system's tools, it is currently reported and **not run**, because running it means producing tool
arguments — which molecule, which solvent, which charge — and every one of those is a field a model
invents when asked to fill a schema. A fabricated argument yields a real number a chemist reads as
computed, which is worse than a missing verdict: the gap is visible, the wrong number is not.

So the whole task is: **make fabrication structurally impossible, not merely discouraged.**

## The shape that achieves that

**Nothing the model writes becomes a tool argument.** The model may only *select*:

- a **tool name**, which must be in a set derived from signatures rather than written down; and
- a **subject note id**, which must resolve, in this deployment's own corpus, to a `compound` note
  whose `compound_smiles` frontmatter parses as a molecule.

The structure handed to the calculator is read from the resolved note. The model never writes it.
That is the same move `protocol_design_tools.structure_experiment_request` makes when it refuses a
`stated` slot without a verbatim quote from the chemist: the model points at something real, and
the pointer is checked.

**The dispatchable set is computed, not curated.** A tool qualifies iff its schema requires exactly
one argument and that argument is the structure. Measured over the bundles whose servers ship in
this repo, twelve qualify (`predict_pka`, `predict_solubility`, `compute_xtb_energy`,
`predict_site_reactivity`, `predict_logd`, `compute_thermochemistry`, … — every one takes `smiles`
required and everything else defaulted). A tool that later gains a second required argument drops
out of the set on its own and its checks refuse, rather than the dispatcher guessing the new field.

**Every other argument takes the tool's own default, and the outcome says so.** `predict_logd` has
a `ph`, `compute_thermochemistry` a `solvent` and a `temperature_k`. Left at their defaults the
computation is well-defined; supplied by a model they are invented. So they are never supplied, and
`CheckOutcome.detail` records the exact call including the defaults it ran under — an assumption
disclosed rather than hidden.

## Steps

- [x] 1. `hypotheses/dispatch.py` — pure. `CheckCall` (tool + subject note id), the schema
      validator (`requires exactly the structure`), and the refusal reasons as a closed set. No
      Temporal, no MCP, no graph access: it takes a schema and a resolved structure and answers.
- [x] 2. `DiscriminatingCheck` gains `call: CheckCall | None`. A `computable` check without a
      resolvable call is reported as computable-but-refused, with the reason, rather than silently
      dropping to prose.
- [x] 3. Ground the subject: resolve the note id against the corpus, require `type: compound`,
      require `compound_smiles` to pass `core.chem.require_canonical_smiles`. Refuse on each miss
      with a distinct reason.
- [x] 4. `derive_check` asks for the structured call, and is shown the note ids the evidence sweep
      actually returned so the model selects from what the system saw rather than from memory.
- [x] 5. `run_computable_check` loses its early return: open connector sessions the way
      `durable/template_activities.py` does, find the tool by name, re-check the **live** schema
      before invoking, call it, and return the result verbatim.
- [x] 6. Verdict: a model reads the computed value against the check's stated expectation and
      returns `supported` / `refuted` / `inconclusive`. It judges a real number rather than
      inventing one, `inconclusive` is explicitly available for a difference inside the method's
      error bar (`CLAUDE.md` requires saying so), and the raw value rides in `detail` so a chemist
      can check the reading. **The outcome does not change the rating** — the ranking stays a
      product of pairwise comparison, and one tool call must not silently reorder the field.
- [x] 7. `report.py` renders a check that ran with its value, and a refused one with its reason.
- [x] 8. Tests: the validator refuses a tool requiring a second argument, refuses an unresolvable
      id, refuses a non-compound note, refuses an unparseable SMILES; the workflow dispatches end
      to end against a stubbed connector; a **ratchet** asserting the in-repo `calc` tools still
      satisfy the one-required-argument rule, so a tool gaining a required field fails here.
- [x] 9. Delete the `BACKLOG.md` row in the same commit (the register's own rule), and write the
      ADR recording what the dispatcher does and does not cover.
- [x] 10. `make lint type test` green; run the suite with the daemon up so the Postgres-backed
      tests are not silently skipped.

## What this will still not do

A check whose subject is not a compound in the corpus, or that needs a second argument, or that
belongs to a bundle served from `Chemclaw3-mcp` (no server here to introspect). Those stay
`physical` or refused-with-a-reason. The point is that the boundary is now *checked* rather than
assumed.

## Review

**Shipped, and it grew a second half while being built.** The plan above is the tool path — one
structure, one note, every other argument at its default. What the chemist asked for next ("its own
list of solvents") is a *job* path with one varied axis, and the reason that is safe where an
invented argument is not turned out to be sharp enough to build on: **the harm in invention is a
hidden assumption, and a swept axis is the most visible part of the answer.** The solvents compared
are the result, they are printed beside it, and each value is checked by the job's own
`require_supported_solvents` before anything runs.

**What the plan got wrong, corrected in the code rather than argued:**

- "Thirteen qualify" was twelve, and the pin is on the tools this tree can *introspect* — 21 more
  are declared and served from `Chemclaw3-mcp`, on the live surface a check is dispatched against.
  That blind spot is now asserted rather than implied.
- Step 6's verdict model reads a real number, as planned. But the composed `detail` reaches a
  committed note body and was not celled, so a model-written `[[...]]` would have minted a graph
  edge. `proposal_body` twenty lines away had done this correctly since it was written.

**Two fresh-context reviews found seven defects and every one was the feature's own rule applied
somewhere it had not been.** Worth recording as a pattern rather than a list: *the rule was right
and its scope was assumed.* Arguments are validated — except a key the target does not declare,
which pydantic silently drops while the report keeps claiming it. Subjects are grounded — except
for a job that requires no subject at all, which made `republish_calculations` reachable. The
budget is bounded — except over the half that looked cheap, and spent in generation order so it
could refuse the leader's own check. The launch is governed — except it bound no roles, so every
`expensive: true` job was refused wherever Entra runs, as an ordinary-looking grounding refusal.

**The most useful finding was in the ADR's `Revisit when:`**, which named a condition that was
*already met* on the day it was written — `chem` ships six enumerators and `BondCleavageSpec` is
documented as taking exactly what one of them emits. That is `D-092`'s failure reproduced inside
the very file whose rule is that a refusal must carry an expiry. It is rewritten around what is
actually missing: a check can name a note and cannot name a tool's output.

**Verification.** `make lint`, `make type` (944 files) and every validator but `helm-validate`
(helm is not installed here) are green; the feature suites are 47 + 19 tests. The full `make test`
run is against Postgres and Temporal started locally, so the Postgres-backed set is not skipped —
and the one failure it produced was a 180-second pytest timeout in the workflow-replay control
under a saturated machine, which passes in isolation and in its own file.
