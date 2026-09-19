# D-2026-09-18-a-mutation-watched-failing-is-half-a-guard — watching a guard refuse maps its connection, not its boundary

**Status:** accepted · **Date:** 2026-09-18 · Completes
`D-2026-09-18-a-default-and-an-implementation-are-not-one-defect-class`, which stands, and
generalises `D-2026-09-11-a-guard-nobody-watched-refusing-is-a-claim-that-a-control-exists`. The
calc prose scan it also hardens carries no ADR of its own — it was hardened at commit level in
`d6d828ba`, which is part of why this was worth writing down. Supersedes nothing.

## Context

`D-2026-09-11-a-guard-nobody-watched-refusing-is-a-claim-that-a-control-exists` mutation-tested 30
guards and found eight that passed against their own defect. The rule it left, and which
`tasks/lessons.md` carries, is that **a guard is not written until its mutation has been watched
failing**. Two guards were then written to that rule and hardened under it — the calc prose scan in
`tests/test_repo_map.py` and the readiness record's shipped-default check in
`tests/test_readiness_record.py`.

A fresh-context review drove seven further mutations against them. All ten of the hardening commit's
claims reproduced and both of its merge resolutions were proven lossless; the guards were genuinely
better than what they replaced. **Six of the seven mutations were nonetheless green over a live,
false statement**, and four of them in the very files that had carried the original defect:

| what was mutated | the guard's state |
|---|---|
| the record's spend row reworded *and the backticks dropped*, field back to `Field(default=0)` | `5 passed` — the record telling a deployment team a spend ceiling is on that ships off |
| only `tools.py` moved out of the four-file surface package, stale count restored | `1 passed` — the other three files satisfied `assert scanned_in_package` |
| every live `` `calc` bundle `` reworded to `` `calc` connector bundle ``, defect re-added to a live-lane script | `1 passed` — that half of the scope had no basis at all |
| the claim split across a full stop | `1 passed` — the pairing window was one sentence |
| a *fresh* count in another test's docstring inside the exempt file | `1 passed` — the exemption was the file, not the quotation |
| "all seventeen **read** tools" | `1 passed` — one adjective evaded the pattern |

The seventh was not a false pass: the `ships on` arm still raised a bare `AttributeError` that the
hardening commit's message said it had replaced — and per that commit's own measurement it is the
arm the record actually writes, so the arm that got the fix had no live instance and the arm with
the live instance kept the defect.

## Decision

### 1. The rule gains its complement

**A guard is finished when *other* mutations of the same property have been watched still failing —
at least two, and at least one of them a reword rather than a deletion.**

Watching a guard fail on the defect it was written for establishes that it is *connected* to that
defect. It establishes nothing about the **boundary** of what it catches, and the boundary is where
the next instance lands, because the next author is not re-introducing anyone's mutation — they are
writing a sentence. A guard that only reds on the one mutation it was written for is a regression
test for a fixed bug, which is a smaller and different thing.

### 2. Derive the scope; do not assert that it is non-empty

Both prose-guard holes were an unanchored string, and the first fix for the first of them was a
*stronger assertion* over the same string. That is the weaker move every time, and the round
replaced it:

- the package half of the scope is resolved through `find_spec` from the module that **defines**
  the surface, so a rename carries the scope with it and a rename the constant does not follow
  fails by name;
- the bundle half's predicate is built from the `name:` that bundle's own `connector.yaml`
  declares;
- the readiness guard's subject population is derived from `settings.model_fields` rather than from
  a markup convention.

An assertion then guards only the residue resolution cannot see — the surface module untracked, the
bundle named nowhere outside its package — and the failure message says which residue that is.

### 3. State a guard's narrowness beside its rule, and prefer widening when the measured
false-positive cost is zero

Three of the six holes were narrownesses the docstring had honestly described and a `#:` comment had
then overstated — "in scope, **any cardinal immediately before 'tools' is refused**" against a
pattern that one adjective defeats. A reader believes the comment. Each widening in this round was
measured before it was taken and each measured **zero** new offenders over the tree: paragraph-scope
pairing, a two-word modifier run between the bundle's name and "bundle", and the same run between
the cardinal and "tools". That is what makes the choice an argument rather than a preference, and it
is also why `_COUNTED_JOBS` — which had admitted one such word since it was written — decided the
question: two patterns in one file disagreed, and the narrower one was the one whose comment
overstated.

### 4. An exemption is exempt from *something*, and the implementation must say what

"This file has to write the sentences it refuses, because it quotes them to say what they are"
licenses the quotations. The implementation licensed the file — a thousand lines — and a previous
commit had already had to hand-delete two fresh counts from it by hand. The exemption is now per
match, conditioned on the phrase sitting inside quotation marks. It red this round's own first
draft, which is the only evidence available that such a guard works on an author who is not trying
to defeat it.

### 5. A test that asserts a wall-clock ratio must be asked what else is inside the clock

`tests/test_molfp.py::test_a_build_that_cannot_meet_its_budget_costs_the_query_a_fraction_of_that_budget`
failed serially and alone while the file it lives in was `63 passed`. It was not a regression: it
timed `_scan_for_matches` whole against a reference loop, and that call ends by deriving a compound
note id per hit through `core.chem`'s canonicalisation, which the reference loop does not do and
whose caches are process state — 0.764 s cold against 0.109 s warm in the same process, over a
refusal costing 0.020 s. And at its own fixture's 0.001 s build budget, a refusal taken by
*exhaustion* also costs 0.001 s, so the two mechanisms the test exists to distinguish were
indistinguishable by time.

It now asserts **records parsed** against a budget calibrated from a full build measured in the same
run: a projecting build gives up at the first check past the budget, a deadline-watching one reaches
half the corpus — 64 against 576 on the 1,200-record slice, a gap no machine's speed moves because
both sides are fractions of one corpus. The bar is a fraction of the corpus rather than a multiple
of `_BUILD_CHECK_STRIDE`, because written against the stride, widening the stride raises the bar
with it.

### 6. Two figures corrected, and one judgement recorded

The commit message claiming a live count of `run_cached_*` wrappers had a sibling in shipped code:
`connectors/calc/remote.py` named **twelve** such wrappers where `src/` holds zero, and one sentence
below named **eleven** tools passing through `cached_remote` where an AST walk finds fifteen literal
names. Both figures are deleted rather than corrected. The PR body's "twice in `deploy/helm/`" is
three occurrences across two files; it lives only in a merged commit message, which is not edited,
and is recorded here instead.

`connectors/calc/connector.yaml:7` carries a live subset count — "the composition of the two tools
that were never shipped whole (`compute_thermochemistry`, `predict_logd`)" — and stays out of scope.
Measured: widening the package half to the whole bundle directory catches four paragraphs, two of
which count something else entirely (tools on the sibling server; *reaction* tools in a skill).
Buying one sentence for three exemptions is the allowlist-of-its-own-exceptions CLAUDE.md refuses.
And that sentence names both members in the same parenthesis, which is the remedy the rule asks for.

### 7. A round's plan does not live in a shared scratch file

The hardening commit's PR body claimed a plan and review under `tasks/todo.md`, and
`git diff 44e7c0e1 d6d828ba -- tasks/` is empty: that file is shared across parallel sessions and
this branch's copy lost the merge. Nothing depended on it, so nothing broke — the defect was the
claim. This round's plan and review are at `tasks/fix-round-2026-09-18-guard-mutations.md`, which is
the convention `tasks/` already uses and which cannot lose that merge.

## What this does not decide

Three residues are stated rather than closed, each where a reader of the guard finds it:

- a **glob** subject in the readiness record whose backticks are dropped is invisible, because
  `retention_*_days` is no field name for a literal scan to find;
- the bundle half of the prose scope has an *any* basis, because the files that discuss a bundle
  from outside are not a derivable set; what carries the weight is the predicate being hard to
  evade;
- `docs/planning/BACKLOG.md`'s row for `tests/test_prose_contract.py` is the same *shape* and is
  untouched: what is open there is the universe those guards read and the patterns they read it
  with, and no derivation used here reaches it.

## What keeps it true

- `tests/test_repo_map.py::test_the_calc_tool_surface_is_not_counted_in_prose` — the scope derived
  from the surface module and the manifest, both halves' bases, paragraph pairing, the quotation
  exemption and the modifier runs.
- `tests/test_readiness_record.py::test_a_claim_about_a_shipped_default_agrees_with_the_setting` —
  the subject population as a union, and one resolution above both arms.
- `tests/test_molfp.py::test_a_build_that_cannot_meet_its_budget_costs_the_query_a_fraction_of_that_budget`
  — the refusal asserted in records against a calibrated budget.
- `tests/test_decision_log.py` — this ADR's id, filename, heading and ledger row.
