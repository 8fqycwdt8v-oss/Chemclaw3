# Task: analyse the BO capability, and plan the features worth adding

Branch: `claude/bofire-capabilities-roadmap-pmeipd`. Deliverable: **documentation only, no `src/`
change** — a capability map, an ADR holding the measured numbers, and the backlog/deferred rows
that make the roadmap actionable.

_(The previous occupant of this file was the 2026-08-03 grounded-live-run fix list, merged in
`1f10ae3`; it is in `git log`.)_

---

## What was asked

Three things: which chemical- and analytical-development use cases the current BoFire wiring already
serves; what BoFire can do that is not wired up; and a plan of features to integrate. Scope
confirmed with the user as **analysis and roadmap only**, covering all four use-case families
(reaction/process optimisation, DoE/HTE screening, analytical method development, campaign health).

## Done

- [x] Map the implementation from source — one BoFire-importing module, three strategies, two
      objectives, four feature types, `Domain(constraints=…)` never passed.
- [x] Catalogue BoFire 0.4.1's full surface and diff it against that.
- [x] Locate the demand side: `tasks/story-audit-optimization.md` §2/§3/§4,
      `story-audit-analytical.md` §7/§8, and what `OrdReaction` actually records.
- [x] **Run the measurement register M-1…M-7 before writing the roadmap** (`uv sync` first; bofire
      was not installed in the session).
- [x] `docs/reference/bo-capability-map.md` — §1 wiring, §2 what it serves, §3 what is unused,
      §4 the gap by family, §5 five waves, §6 what the map does not tell you.
- [x] `docs/decisions/D-2026-08-04-what-bofire-does-when-you-actually-run-it.md` + ledger row.
- [x] BACKLOG rows (one per wave, plus the `method` note type), five DEFERRED rows with triggers,
      the `docs/README.md` reference row.
- [x] Gate: `make lint` · `make type` (537 files) · `make test` (2908 passed, 129 skipped) ·
      `prose-validate` · `kg-validate` · `skill-validate` · `connector-validate` ·
      `test_decision_log` · `test_deferred_register` · `test_repo_map`.

## Review

**The measurement register earned its cost, which was the open question going in.** It was run
because `tasks/lessons.md` requires it — the last BO roadmap said "just thread `n_generators`
through" about a parameter that was inert — and the worry was that it would only confirm a plan
already written. It did not. Three of seven measurements changed a wave and one reversed a refusal:

- **M-5** turned "admit continuous factors, and also thread the four unused knobs" into "admitting
  continuous factors is the *precondition* for three of those knobs" — on the all-categorical domain
  `factorial_design` accepts today, `n_repetitions` and `n_center` are as inert as `n_generators`
  was. The same trap as D-2026-08-02, two parameters wider, and it would have shipped as three
  no-ops implemented elegantly.
- **M-3** could have grown the constraints wave: `RandomStrategy` seeds every cold start, and had it
  ignored `Domain.constraints` the schema would have claimed a limit was honoured while every seed
  point violated it. It honours them, so no rejection-sampling path is needed.
- **M-7** reversed a refusal already written down as settled. The argument against a
  cross-validation tool was that it forces naming a surrogate class in `engine.py`;
  `strategy.surrogate_specs` exposes the one BoFire itself chose, so the number describes the model
  that made the recommendation and no class is named.

**The most useful finding is a negative one.** Analytical method development is the largest family
by story count (24 stories) and gets no wave: it is blocked on a missing `method` note type, not on
BO. Building `TargetObjective` for it first would have been building on air. Naming that explicitly
is worth more than any single one of the five waves.

**One process note.** `prose-validate` caught the map naming `science/bo/progress.py` — a file the
roadmap proposes and the tree does not have. That is the validator working exactly as intended on a
document class it was not written for: a roadmap that names paths reads as a claim about the tree.
Rephrased to describe the module without asserting it exists.

## Not done, deliberately

No `src/` change. Each wave carries its own ADR, its own tests and its own gating measurement, and
those are what stop these findings from rotting. The numbers were taken on one machine at
`bofire==0.4.1`; a version bump invalidates them.
