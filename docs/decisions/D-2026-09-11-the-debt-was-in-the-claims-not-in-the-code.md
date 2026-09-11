# D-2026-09-11-the-debt-was-in-the-claims-not-in-the-code — The debt was in the claims, not in the code

**Status:** accepted · **Date:** 2026-09-11

## Context

Five waves were planned to find simplification, refactoring and maintainability debt. They were
planned on measurement rather than intuition, and **the measurement rejected the plan before any
code was touched.**

Every standard proxy for "this codebase needs cleaning up" came back negative:

| proxy | measured | verdict |
|---|---|---|
| structural duplication | 33 duplicated 8-statement shapes in 8,643 windows, nearly all pydantic field blocks and SQL row mapping | **DRY — wave cancelled before it ran** |
| function length | median body **9** lines, p90 36 | well-factored |
| the twenty 130–315 line functions | **1 split of 20** recommended after reading all of them | long for a stated reason |
| dead code | **1 dead definition in 3,133** (`DesignListing`, 6 lines) | clean |
| Rule of Three | 45% of defs have one caller; net actionable **~109 lines** | not a defect rate |
| settings with no reader | 2 of 422, both read by `entrypoint.sh` and parity-tested | no `D-2026-08-11` pattern |
| prose:code at 1.28 : 1 | **98.9% DECISION, 1.1% restatement** over a 176-docstring stratified read | the ratio is not the finding |
| tests at 1.63x source code | exactly **one** true duplicate test in 5,747; 101 fixtures; 1.6% of test prose restates | the ratio is fine |

A review that stopped there would have reported a clean tree and deleted a hundred lines.

**What the waves actually found was in none of those categories.** Every real defect was a
*claim that nothing could falsify*:

- **Three of four processes serving HTTP ran unbounded.** `deploy/entrypoint.sh` passes
  `--limit-concurrency`, `--timeout-keep-alive` and `--h11-max-incomplete-event-size` in its
  `service)` case, and a test pins them — to that case. The MCP face, *every* `connector-*` pod and
  the worker probe server call uvicorn themselves and passed none, running at unlimited
  concurrency, a 5 s keep-alive and a 16 KiB header ceiling. The cause was one sentence in
  `deploy/README.md`: the settings are ones "the app can impose on itself" cannot. True of an ASGI
  *application*; false of `uvicorn.run()`, which takes all three as keyword arguments at all three
  call sites.
- **Two metric series named as alert targets are never emitted.** `durable/retention.py` told an
  operator that `chemclaw_table_bytes`, `chemclaw_retention_rows_deleted_total` and
  `chemclaw_retention_bytes_reclaimed_total` "are what an operator can alert on". The module emits
  exactly one.
- **A settings field documented for a subsystem that does not exist.** `xtb_embed_seed` — described
  in the present tense as fixing RDKit embedding and being part of the cache key. No such field.
  Found by an audit on 2026-08-16, recorded, and never fixed.
- **Fourteen dangling symbol references**, including one file that says `surrogate_fit_quality` is
  trustworthy 144 lines above the line recording that it was deleted unreferenced.
- **Parametrised tables that could not say why they passed.** Nine assert only a bare
  `pytest.raises(X)`. One was proven vacuous by deleting the guard it protects: all seven cases
  stayed green. A second contained a row *already* passing for the wrong reason — the case named
  for a fleet product was refused three statements earlier by a different guard, over an axis that
  is unreachable by construction.
- **A knob whose only implemented behaviour is to refuse itself.** `service_uvicorn_workers`
  described how many worker processes the container starts and advised raising it; every value
  above 1 is refused, and no `--workers` flag is passed anywhere.

## Decision

**The maintainability question to ask this tree is not "what is badly shaped" but "what does it
assert that nothing checks".** Its code shape is sound on every axis a linter or a metric can see.
Its debt is in statements — a README's reason, a field's comment, a docstring's count, a test's
assertion — that were true once, or were never true, and that no gate can falsify.

Where a gate is feasible, build it and watch it refuse. `tests/test_transport_bounds.py` is a
partition rather than a list: every `uvicorn.run`/`uvicorn.Config` site either applies
`core/asgi.transport_bounds` or is named with a reason it serves nobody, both halves of the
register held, all three arms driven. That shape catches the *fifth* process, which is the one that
would otherwise repeat this.

**Where a gate is not feasible, decline it and say so with the number.** A general guard over the
21,391 unchecked symbol references in `src/` prose was built, measured, and **rejected at an 82.9%
false-positive rate** over a hand-checked sample of 35. The reason is structural rather than
fixable: this tree's prose explains a design by naming what is *not* there — "there is deliberately
no `labels_enabled`", "the alternative — `dipole_averaged` — would…" — so a guard on bare
identifiers penalises exactly the sentences carrying the most reasoning. Two narrow rules ship
instead: a near-miss rename within the declaring file (75% precision, no allowlist) and metric
names in `src/` prose (the highest-consequence class, since those are alerting claims). Together
they move the guarded share of in-code references from ~7.5% to ~7.6%, and **saying that plainly is
the point** — the remaining defects stay findable only by reading.

**A named function beats an attribute assigned onto one.** `discovered.cache_clear` existed at
runtime and not to `mypy`; one suppression at the definition produced an error at all 35 call
sites. `forget_discovered()` is the idiom this tree already had (`forget_reachability`,
`forget_vector_store`, `forget_open_warehouses`).

**A number without its method is not a measurement.** See Consequences.

## Consequences

**The suite's runtime is 31 tests, not 179,332 lines.** A profiled run measured 60 tests — 0.8% of
the suite — at 58.4% of wall clock, of which 31 BoFire surrogate-fit tests are 43.5% alone; the
other ~7,654 average 106 ms. **Not one `setup` or `teardown` entry reaches the top 60**, which is
direct evidence against the per-test-database story the wave was planned around. The mechanism is
one missing fixture: `tests/test_bo_predict.py` has 41 tests and none, so each refits the same
surrogate from the same constant inputs to assert a different property of the result.

**Three of this wave's own measurements were wrong, all in the reassuring direction, and that is
the finding that generalises.**

1. The dead-code scan counted `tokenize` NAME tokens. On Python 3.11 an f-string is one `STRING`
   token, so every interpolated identifier was invisible: 27 live functions read as orphans — nine
   of them consecutive helpers in one file, which looks exactly like a dead cluster. 144/46 was
   really 117/22.
2. `src/` prose:code was measured three times independently and returned **1.16, 1.28 and 1.49 : 1**
   — a 28% spread on the number the prose wave was planned around. The tree did not change; the
   method did, in how each treated a blank line inside a docstring. None stated its method.
   Settled at 1.28 : 1 under a stated rule where four buckets sum to the file's line count.
3. A duration profile was declared invalid and an agent told to skip the question. It had been read
   while still being written; the data was there all along. The agent re-checked rather than
   complying, and was right to.

So the rule this ADR adds to `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`: **state the
method beside the number.** A count whose method is unstated cannot be reproduced, and three
disagreeing measurements of one quantity are indistinguishable from one correct one until someone
puts them side by side.

**What was declined, and why it is not a gap.** No prose was trimmed for length: 98.9% of long
docstrings record a decision, and 65.6% of sampled test-prose *lines* record a measured defect.
Nineteen of twenty long functions stay, thirteen of them having already had their extractable parts
extracted. `get_effect` stays — a merged ADR four days old decides it explicitly, and the audit
recommending its deletion had not read that ADR. `BoundedLru.peek` stays — it is not an alias;
`get` calls `move_to_end` and `peek` deliberately does not.

**Three of the audit's own deletion recommendations did not survive re-derivation**, each in the
direction that would have removed live code. That is why nothing here was deleted on a name count,
and why the near-miss in `D-2026-08-15` — reporting the egress guard's arming function as dead
because it is imported under an alias — is quoted in the brief of every sweep that follows.

## What keeps it true

- `tests/test_transport_bounds.py::test_every_http_surface_is_bounded_or_argued`
- `tests/test_transport_bounds.py::test_no_exemption_outlives_its_launcher`
- `tests/test_transport_bounds.py::test_no_exemption_sits_on_a_server_that_is_already_bounded`
- `tests/test_transport_bounds.py::test_the_probe_surface_keeps_its_keep_alive_and_header_bounds`
- `tests/test_docstring_symbols.py::test_the_near_miss_rule_refuses_a_rename_that_left_its_docstring_behind`
- `tests/test_docstring_symbols.py::test_the_metric_rule_refuses_an_alert_target_nothing_emits`
- `tests/test_docstring_symbols.py::test_both_rules_have_something_to_check`
- `tests/test_config.py::test_configurations_the_comments_forbid_are_rejected`
