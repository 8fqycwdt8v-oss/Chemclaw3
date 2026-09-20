# Finish the four points the ten-wave review left open — plan and review

The ten-wave review (`tasks/review-2026-09-19-ten-waves.md`) closed with four things it had **not**
done, stated rather than implied. All four are closed. Each got its own PR, merged when CI was green.

The previous occupant of this file was the peer-handoff plan, whose twelve items were all
**unchecked and all shipped** — verified item by item before it was moved to
`docs/archive/plans/peer-handoff-plan.md`. A plan file whose boxes are empty over finished work reads
as live state, which is the defect `D-154` records about `DEFERRED.md`, one register over.

## OP4 — `helm-validate` end to end (PR #420)

- [x] 1. Install `kubeconform` and `promtool` and run the target. **It passes**: Valid 31 and 35 over
      the two arms, promtool parsing 219/220/220 rules, every figure the runbook states.
- [x] 2. What the first real run found: `_EXPECTED_SKIPPED_RESOURCES = 1` compared a count of
      *resources* against `len(_UNVALIDATED_KINDS)`, a count of *kinds*, and the union arm reports
      `Skipped: 3`. The second skipped kind sat in the set whose stated reason was that it could not
      be skipped.
- [x] 3. Derive the count per arm; measure it against kubeconform's own summary line; read the arms
      out of the `Makefile`. Three mutations watched failing.
- [x] 4. **And the test would have run nowhere.** `check` runs the suite with only `helm`; `chart`
      has the other two and runs no pytest. Three tests could execute in neither job — mine and the
      two PromQL checks, whose subject is a failure the cluster reports as `Valid`. `check` installs
      all three now, plus a mechanism so the next binary-gated test fails the day it is written.
      Its first allowlist was itself the hole; four mutations red it now.

## OP3 — the `model_copy(update=…)` fixtures (PR #421)

- [x] 1. **The proposed heuristic separates nothing, measured.** `model_copy` patched for a full run,
      every copy re-validated: of 149 executing sites, **none** injects an undeclared key and
      **none** builds an object `model_validate` refuses; `extra="ignore"` is pydantic's *default*,
      so 80 sites declaring nothing behave like the 6 declaring it.
- [x] 2. The property that does work is derived from the tree: the model's own `model_validator`
      bodies, and `src/`'s own `model_validate(` calls. 27 fields over 22 of 151 sites.
- [x] 3. Triaged: **two weak, one of them green over the deletion its own name implies** — verified
      independently by driving the mutation. 24 argued with a reason each, held both ways.

## OP2 — `tasks/lessons.md` (PR #421)

- [x] 1. 3,212 lines → **1,424**; all 88 dated sections folded into 53 new rules plus four
      sharpenings. Rule numbers preserved, 31 left unallocated.
- [x] 2. Narrative archived unedited in `docs/archive/lessons-2026-09.md`.
- [x] 3. `tests/test_lessons_stay_a_digest.py`: the assertion is **structural**, not a date regex —
      every line under a theme belongs to a numbered rule. Verified by appending an *undated*
      narrative section: caught by that test and nothing else.
- [x] 4. Three citations repaired, one of them already wrong before the fold.

## OP1 — the delegation experiment's run half (this PR)

- [x] 1. Three symmetric arm profiles, bodies held byte-identical by a test.
- [x] 2. `evals/delegation_run.py` — the arm table, `delegated` off `audit_events`, `billed_tokens`
      off `turn_costs`, `ArmRun` assembly, the report.
- [x] 3. `cli/live_probes.py --suite delegation`, `make live-delegation`.
- [x] 4. `cli/delegation_behaviours.py` plus `mock_llm --catalogue`, so the double can emit `task`.
- [x] 5. Driven against the mock on loopback: **all four compliance buckets plus `incomplete`**,
      none of which any run had ever exercised.
- [x] 6. Tests with the mutation discipline; the BACKLOG row rewritten to what is left.

## Review

**All four closed, and three of the four changed shape once measured.** OP4's fix was not the one
the finding suggested (the count was mis-*based*, not merely stale) and turned up a second, larger
defect — three tests that could run in neither CI job. OP3's proposed mechanism was **refuted**: the
danger is not a property of the object but a relation between fixture and assertion, and the obvious
heuristic would have flagged 65 harmless sites. OP1's instrument was confounded as specified.

**What the delegation runner proves and what it cannot.** Driven against
`chemclaw.cli.mock_llm --catalogue delegation` on loopback with a real front door, it recorded real
`ArmRun`s from real turns. **None of its figures is evidence about delegation**: the double supplies
the decision to delegate, so the run is evidence about the *runner*, and the suite exits non-zero
against the mock for exactly that reason. Whether delegation pays is still open and now needs one
credential and a gateway URL rather than any code.

**Two things measurement fixed in OP1's plan.** A profile's `instructions:` *replace* the shipped
prose, so one control profile against a default treatment arm would have varied ~14,000 characters of
system prompt alongside the treatment — on a measurement whose headline axis is cost. Three symmetric
profiles instead; `D-2026-09-20-an-arm-that-varies-the-prompt-and-the-treatment-varies-neither`
records that the comparison is internally valid and is *not* a reading of the shipped prompt. And the
mock cannot script a handoff: `available_tool_names()` does not carry the handoff name space although
its docstring claims all six, so the peer arm lands in `undelegated` — honest ITT, left as a BACKLOG
paragraph because three validators read that function.

**One of these four points caught another, on the day it landed.** OP3's guard
(`test_every_model_copy_fixture_that_skips_a_check_its_model_applies_is_argued`, merged in #421)
failed CI on #422 against OP1's brand-new `tests/test_delegation_run.py`: a fixture built the second
recorded pass with `ArmRun.model_copy(update={"delegated": False})`, and `src/` obtains an `ArmRun`
through `model_validate`, so the fixture was not crossing the boundary production crosses. It was
also the single field whose value is the entire finding of that module, which makes it the worst one
to assign past a check. Rebuilt through the constructor; reverting the fixture reds the guard again.
That is the mechanism doing what the review kept saying mechanisms are for — the instance would have
gone unnoticed, and OP1 had run neither that guard nor `test_docstring_paths.py`.

**A CI failure that was not the PR's, established rather than asserted.** `check` went red on #421
with two *timing* tests. The PR changed **zero files under `src/`** and neither test imports anything
it did change; both pass 3-of-3 serially on an unloaded machine. One of the two is tabulated in
`D-2026-09-13-a-stable-failure-set-is-not-two-green-runs` as failing 2-of-5 parallel runs with the
serial column reading an unqualified **"passes"** — this run falsifies that. Parallelism was never the
mechanism; **load is**, and `-n 4` is one way to produce it. Recorded here as a refinement because a
merged ADR is never edited. The single permitted re-run went green.

**Two disclosures.** A subagent ran `git checkout .gitignore` once to undo an empty append — a
verified no-op on an unmodified file, and still the rule `tasks/lessons.md` names as the habit rather
than the outcome. And my own first probe of a BACKLOG row set `service.replicas=0` while the shipped
default takes the `autoscaling.maxReplicas` branch, so the knob was ignored and the row looked wrong;
re-probed on the live branch, the row is right. Same shape as the `relax_structure`/`smiles` probe
this review already recorded: a bad probe reads exactly like a real finding.

**Still open, and each is a new piece of work rather than an unfinished one**: the delegation run
itself (a credential); the handoff act, never once observed; the two load-sensitive timing bounds,
with a proposed patch on #421; `src/`'s 76 unexamined `model_copy` sites; and 48 of 149 test sites
that OP3's subject-model resolver cannot name.
