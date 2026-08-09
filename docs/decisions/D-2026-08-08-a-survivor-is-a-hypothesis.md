# D-2026-08-08-a-survivor-is-a-hypothesis — a survivor is a hypothesis, not a finding

**Status:** accepted

## Context

`[tool.mutmut]` has named seven modules since it was written, and `make mutants` had **never been
run** — no `mutants/` directory, no cache, no stored report for three of the seven. The three
unmutated ones were `kg/pr_gate.py` (the PR-gate: the "AI proposes, human signs off" line every
other control in this system is justified by), `kg/note.py` (the note schema and its link parser)
and `agent/audit_store.py` (the hash-chained GxP audit trail). The campaign's test-quality lane
(D-2026-08-08-a-test-that-survives-the-mutation-it-names) had hand-mutated nine sites, none of them
in these three.

Two things had to be worked out before anything could be measured.

**Scoping a mutmut 3 run.** `mutmut run "src.chemclaw.kg.pr_gate"` and `"…pr_gate.*"` both die on
`Filtered for specific mutants, but nothing matches`. The filter is an `fnmatch` over mutant *keys*,
and `mutmut/utils/format_utils.py::get_mutant_name` builds a key by dotting the source path **and
then stripping the literal prefix `src.`**. The working form is therefore the importable module
path, with a glob for the per-function suffix:

```
uv run mutmut run "chemclaw.kg.pr_gate.*" "chemclaw.kg.note.*" "chemclaw.agent.audit_store.*"
```

Keys look like `chemclaw.kg.note.x_split_link__mutmut_3` and, for methods,
`chemclaw.kg.note.xǁNoteǁoutgoing_links__mutmut_4`. Narrowing `source_paths` was the fallback and
was not needed.

**The run's own selection is part of the measurement.** `[tool.mutmut]` does not run the whole suite
against each mutant — it names eighteen test files, and its comment justifies the list as "the tests
that can actually kill these mutants". That comment also poses the question DA-7 asked and left
open: *does a narrow run mislead?*

## What was measured

First run, 223 mutants across the three modules: **171 killed, 39 survived, 12 "no tests", 1
timeout.**

Then every non-killed mutant was re-applied to the real source and run against a wider set. That is
the step that matters, and it reclassified most of them:

| module | mutants | killed | survived | no-tests | timeout |
| --- | --- | --- | --- | --- | --- |
| `kg/pr_gate.py` | 101 | 80 | 20 | 0 | 1 |
| `kg/note.py` | 85 | 55 | 18 | 12 | 0 |
| `agent/audit_store.py` | 37 | 36 | 1 | 0 | 0 |

**29 of the 39 survivors — and all 12 "no tests" — were killed by two test files the selection did
not name.**

- `tests/test_relations.py` kills **24** `note.py` mutants: every `split_link` mutation (the
  separator, and all four ways of breaking the `not separator or not relation or not note_id`
  guard), both `outgoing_links` key mutations, and all ten `outgoing_relations` mutations that the
  narrow run reported as covered by no test at all.
- `tests/test_metrics_bridge.py` kills **5** `pr_gate.py` mutants: the entire
  `record_metric(lambda m: m.increment("chemclaw_notes_proposed_total"))` line, including
  `record_metric(None)` (which the metrics bridge swallows, so it is a silent no-op) and the
  metric-name case flip.

Neither file mentions the module it protects. `test_relations.py` builds a graph; the graph is what
calls `split_link`. **A selection assembled by matching module names to test names will keep
understating the suite**, and a survivor from such a run is a hypothesis about the code, not a fact
about it.

`agent/audit_store.py` needed nothing: 36 of 37 killed on the first pass, and the survivor is
provably equivalent.

## Decision

**Fix the selection, close the genuine gaps with tests, and leave the equivalent and harmless
survivors alone with the reasoning written down.**

### 1. `[tool.mutmut]` gains the two files that do the killing

`tests/test_relations.py` and `tests/test_metrics_bridge.py` join
`pytest_add_cli_args_test_selection`, each with a comment saying which mutants it kills. The
comment above the list keeps its argument — running the whole suite really does fail on the
`mutants/` copy — and now records DA-7's measured answer instead of posing it.

Re-run with the corrected selection and the new tests below: **206 of 223 killed, 17 survivors, 0
"no tests", 0 timeouts.**

### 2. The proposal record's provenance is asserted (the one that mattered)

`propose_note` reads `(actor, session_id, correlation_id)` from the ambient turn and stamps them on
the `NoteProposal`. All three fields default to `""`, and **nothing anywhere in the suite read any
of them back**. Each of these deleted in turn left the whole suite green:

| mutation (verbatim) |
| --- |
| `        actor=actor,` → *(line deleted)* |
| `        session_id=session_id,` → *(line deleted)* |
| `        correlation_id=correlation_id,` → *(line deleted)* |

This is the highest-consequence hole the run found, and it is not only a record-keeping one.
`chemclaw/api/deps.py` scopes a non-reviewer with `proposal.actor != principal.oid`, and
`proposal.listing` filters on the same field — so an unstamped row is one **its own author is served
a 404 for**. `test_a_non_reviewer_sees_only_their_own_proposals` covers the route, but it builds its
proposal directly from the `_proposal()` helper, which hard-codes `actor`. The rule was tested; the
stamping the rule depends on was not. That seam — a route test that constructs the row the writer
was supposed to fill in — is the same shape as the fake that hard-codes the field its branch keys
on, from the predecessor ADR.

`tests/test_note_proposals.py::test_a_recorded_proposal_carries_the_turn_that_made_it` sets a real
ambient identity, session and correlation id, proposes through `FakeSubmitter`, and asserts all
three on the stored row plus that `listing(..., actor=...)` selects on it.

### 3. The reviewer's file count is asserted

The PR body says `with N supporting note(s)`. It is the reviewer's summary of the unit they are
signing off on, and no test read it. All four of these survived the whole suite:

| mutation (verbatim) |
| --- |
| `f" with {len(files) - 1} supporting note(s)"` → `f" with {len(files) + 1} supporting note(s)"` |
| `f" with {len(files) - 1} supporting note(s)"` → `f" with {len(files) - 2} supporting note(s)"` |
| `if len(files) > 1 else ""` → `if len(files) >= 1 else ""` |
| `if len(files) > 1 else ""` → `if len(files) > 2 else ""` |

`tests/test_pr_gate.py` now asserts the count for a two-file and a three-file submission, and that a
dependency-free note's body offers no count at all. The one-dependency case is included
deliberately: it is both the boundary of `> 1` and the commonest shape the gate sees — a
`job-result` and the `compound` its wikilink needs to resolve.

### 4. Deduplication gets a deterministic example — and the reason is speed, not coverage

This one corrected itself under measurement, and the correction is the more useful result.

`continue` → `break` (which drops **every dependency after the first repeat**) was recorded
SURVIVED, and `seen.add(dependency.id)` → `seen.add(None)` (which writes one path twice) **timed
out**, which `timeout_multiplier`'s comment correctly declines to score as a kill. The obvious
reading — nothing tests the dedup — is wrong.
`test_properties_core.py::test_a_submission_writes_each_note_once_with_its_subject_first` asserts
exactly this invariant and kills both. The problem is how often and how fast:

- Its `_SLUGS` strategy draws from `[a-z0-9][a-z0-9._-]{0,20}`, so collisions are rare. Instrumenting
  the generator over 100 examples: **13 contained a duplicate id at all, and 1 reached the shape
  that discriminates these two mutations** — a repeated dependency followed by a *further, new* one.
- On a cold hypothesis database the kills cost **83 s** and **158 s**. Five repeats each after the
  falsifying example is cached: ~1.1 s.

So the property test is a correct but seed-dependent, minutes-long killer. The deterministic
example in `tests/test_pr_gate.py` kills both in milliseconds every time; the property test keeps
its wider job. Both docstrings now say which is which, because the predecessor ADR's line about
this property test ("the generator produces exactly the collisions a fixed example cannot
enumerate") is true of *some* collisions and measurably not of this one.

### 5. One line on `split_link`, on a principle already stated

`target.partition(":")` → `target.rpartition(":")` survived everything. It differs only on a
two-colon target, and there it does the thing `test_a_malformed_typed_link_is_reported_as_written`
is named for: `[[precursor-of:compound:x]]` becomes relation `precursor-of:compound` pointing at the
note `x`, which **resolves** — a silent repair into a link the author did not write, where the
correct behaviour dangles on the literal text. One assertion joins that test rather than a new one.

## What was deliberately not fixed, and why

Seventeen mutants survive the corrected run. Every one is equivalent or harmless, and chasing them
would make the suite worse — this repository already has a lane whose entire subject is tests that
raise coverage without constraining behaviour.

**Equivalent — the mutation cannot change behaviour (each measured, not argued):**

- `stable_hash({...}, chars=_CHAIN_HASH_CHARS)` → `chars=None`, the lone `audit_store.py` survivor.
  `_CHAIN_HASH_CHARS = 64` and a SHA-256 hexdigest is exactly 64 characters, so `[:None]` and
  `[:64]` are the same slice. Verified equal for a sample payload. No test can distinguish them,
  and the constant is still right to keep — it states the intent that the chain link is the full
  digest.
- Five `ordered.setdefault(key, None)` → `ordered.setdefault(key)` mutations across `cited_ids`,
  `cited_links`, `mentioned_ids` (×2) and `outgoing_links`. `dict.setdefault`'s default *is* `None`;
  verified identical. All five dicts are ordered sets whose values are never read.
- `path.read_text(encoding="utf-8")` → `encoding="UTF-8"`. Codec names are normalised
  case-insensitively (`codecs.lookup` verified).

**Harmless — behaviour changes, nothing a caller can observe or should care about:**

- Seven mutations of human-facing prose: the GxP sentence in the PR body wrapped and case-flipped,
  the `ValueError` text wrapped and case-flipped, and the two `else ""` branches replaced with
  `"XXXX"`. The existing tests assert the *meaning* loosely — `"human review" in body.lower()`,
  `match="agent-authored"` — which is the right strength. Killing these requires pinning exact
  prose, which converts every future wording improvement into a test failure and asserts nothing
  about behaviour.
- `type(self).__name__.lower()` → `.upper()` and `type(None)` in `TemporalWindow._window_owner`.
  Both change only the capitalisation of the subject in an invalid-window error message, whose
  substantive half (`valid_to … is before valid_from`) is already pinned.
- `path.read_text(encoding="utf-8")` → `encoding=None`. This one is a real, if narrow, difference:
  `None` falls back to the locale encoding, so it is identical wherever the locale is UTF-8 (this
  sandbox, CI, the container image) and wrong on a CP1252 or C-locale host. The explicit encoding is
  the right code and stays. Killing the mutant would require a test that manipulates the process
  locale to prove an argument is passed — pinning the call, not the behaviour — so it is recorded
  here instead.

## Consequences

- **No product code changed.** The run found no defect in `pr_gate.py`, `note.py` or
  `audit_store.py` — the untested provenance stamp is correct, it was simply unobserved. The one
  defect found is in the mutation harness's own configuration, and it is fixed.
- `make mutants` now has a documented scoping form. Anyone re-running one module uses
  `make mutants ARGS='chemclaw.kg.pr_gate.*'` — the importable path, not the source path.
- **A survivor from `make mutants` is a hypothesis.** Before acting on one, re-apply it to the real
  source and run the full suite. This run's first report would have produced 29 tests for behaviour
  already pinned, which is precisely the kind of test the campaign exists to remove.
- The three tests added are the regression suite for this ADR: re-applying any mutation quoted above
  should be red, and each was shown red before the fix and green after.
