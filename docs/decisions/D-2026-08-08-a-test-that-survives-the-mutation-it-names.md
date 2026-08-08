# D-2026-08-08-a-test-that-survives-the-mutation-it-names — a test that survives the mutation it names

**Status:** accepted

## Context

A hardening review (lane T11) went looking for tests that raise coverage without constraining
behaviour. The method was mutation: change the implementation in a scratch clone, run the suite, and
record what stays green. Nine findings came back. Every one of them was a test — often a
well-written one, with a docstring stating exactly the right invariant — whose *assertions* could
not see the thing the docstring was about.

Three shapes recur, and they are worth naming because none of them looks wrong in review:

**1. Asserting on the shape of a statement instead of on the rows it leaves.**
`tests/test_artifact_eviction.py` had seven tests, all substring checks against the SQL strings:
`"calculation_results" not in statement`, `"compute_seconds" in _EVICT_TO_FIT`, `"cumulative >" in
_EVICT_TO_FIT`. They read as a careful policy statement and they survive any rewrite of the `WHERE`
clause, because a substring cannot see a predicate's meaning. Four mutations passed all seven,
including `cumulative >= 0 AND %s IS NOT NULL` (deletes every blob in the store on every pass) and
reversing the value `ORDER BY` from `DESC` to `ASC` (evicts the most expensive artifacts first — the
exact inversion of the cost policy the module's own docstring argues for).

**2. A `pytest.raises` with no `match=`, where the function has two ways to raise.**
`authorize_trigger` refuses an unauthenticated turn, then refuses a turn whose user lacks a
privileged role — both `AuthorizationError`. `test_no_user_is_forbidden` and
`test_missing_role_is_forbidden` each caught the bare type, so deleting the `if actor is None:` block
outright left both green: the unauthenticated turn fell through to the role check and was refused
there instead, under a message naming the wrong problem.

**3. A fake that hard-codes the field the branch keys on.** Twenty test files define a fake
streamed update, and every one of them sets `user_input_requests=[]` as an independent attribute.
The runner's approval branch had therefore never been executed by any test at all.

Alongside them sat a defect in the suite's own machinery, found while the campaign was running and
serious enough to have corrupted its baseline: `pyproject.toml` caps every test at 180 s and two
files tighten that with `@pytest.mark.timeout(90)` / `(60)`. **A marker overrides `--timeout` and
`PYTEST_TIMEOUT`**, so the tightest caps are precisely the ones no command line can relax. On a box
running five agents, two `test_pka.py` tests failed with `Failed: Timeout (>180.0s) from
pytest-timeout`; given `--timeout=0` on the same tree and the same box they **passed in 1071 s**.
Both were recorded as pre-existing numerical failures on unchanged `main`, six lane agents were
briefed to ignore them, and one lane spent effort refuting the wrong thing. Nothing about a pKa
value was ever wrong.

## Decision

**Every finding is closed by a test that has been shown to fail against the exact mutation that
survived before, and to pass with it reverted.** The mutation is quoted verbatim below so a later
reader can re-run it. Where the honest answer was that the finding did not reproduce, it is recorded
as refuted rather than papered over with a test that adds nothing.

### Eviction: assert on the surviving rows, against a live database

`tests/test_artifact_eviction.py` keeps all seven substring tests — redundancy is not the defect,
indistinguishability is — and adds four that seed `artifact_blobs` with known sizes, idle times and
costs and assert on **which blobs survive**, the shape `tests/test_retention.py` already takes.

| mutation (verbatim) | now fails |
| --- | --- |
| `WHERE content_hash IN (SELECT content_hash FROM ranked WHERE cumulative > %s)` → `… WHERE cumulative >= 0 AND %s IS NOT NULL)` | 2 tests |
| `) DESC,` → `) ASC,` (the value `ORDER BY`) | 2 tests |
| `WHERE last_access_at < now() - make_interval(days => %s)` → `WHERE last_access_at < now() AND %s IS NOT NULL` | 1 test |
| `MAX(a.compute_seconds) / GREATEST(EXTRACT(EPOCH FROM (now() - b.last_access_at)) / 86400.0, 1.0),` → `MAX(a.compute_seconds),` | 1 test |

The fourth is the reason to record method as well as result. The first version of that test gave
two equally costly blobs different idle times, and the mutation **survived** — the ranking's
tiebreaker (`b.last_access_at DESC`) points the same way as the divisor, so the outcome was
unchanged. It only discriminates when the two axes disagree: a 10-second artifact read yesterday
must outlive a 100-second one unread for a hundred days. A property test that cannot fail is the
same defect one level up.

A fifth test asserts that an evicted blob takes its `calculation_artifacts` link row with it (the
`ON DELETE CASCADE` in migration 019) and leaves `calculation_results` untouched. The existing test
asserted the *absence of a `DELETE`*, which stays true if the cascade is dropped from the schema.

### `compute_seconds` is written once and never erased — in both tables

Two upserts carry the same `COALESCE(EXCLUDED.compute_seconds, <table>.compute_seconds)` clause and
neither had a test. `put`'s `compute_seconds` is keyword-only with a `None` default and only
`cached_compute` / `run_cached_with_artifacts` pass one, so a costless rewrite is the *ordinary*
case, not an exotic one.

- `postgres_artifacts._UPSERT_LINK` → `compute_seconds = EXCLUDED.compute_seconds,` survived the
  whole artifact + eviction suite (42 passed). This is the one that costs money: `_EVICT_TO_FIT`
  ranks by `COALESCE(MAX(a.compute_seconds) / …, 0)`, and 0 is the bottom of the order, so the
  expensive Hessian the ranking exists to protect is evicted *first* and the next question about
  that molecule pays for the run again.
- `postgres_store._UPSERT` → the same mutation survived 47 tests. Cheaper, but it makes
  `find_calculations` report an expensive run as costless — the one number that says what the cache
  has saved, wrong in the direction that argues the cache is worthless.

### `default_store()` is asserted to be Postgres-backed

One line, mirroring `test_default_artifact_store_is_postgres_backed` and the audit sink's
equivalent. `InMemoryStore` satisfies the same Protocol, so a seam left pointing at it type-checks,
passes every store test, and discards the cache at process exit. Mutation: `return PostgresStore()`
→ `return InMemoryStore()`.

### The budget guard's "no-op when disabled" is read back through an enabled tracker

`test_disabled_is_a_no_op`'s docstring said "record books nothing" and its body could not see it:
while budgets are off `check` returns before looking at anything. Deleting `record`'s own
`if not settings.budget_enabled: return` changed nothing 77 tests could observe. The test now
re-reads the disabled period with `budget_enabled` flipped on, which is what a deployment does — the
chart ships `budget_enabled: true` — and would otherwise start every live session partway through
its cap.

### Two `pytest.raises` gain a `match=`

`match="requires an authenticated user"` and `match="user u-2 lacks a privileged role"`. Deleting
`authorize_trigger`'s `if actor is None:` block now fails the first. Neither test was removed:
they are not redundant, they were indistinguishable.

### The approval branch is executed, and the prompt names the tool

`_Update.user_input_requests` in `tests/test_runner.py` becomes a property over `contents`, exactly
as MAF's `AgentResponseUpdate` derives it, so a fake carrying a real `function_approval_request`
reaches the branch without the fake having to know it exists. Driving it produced two results:

- **The hypothesised mid-stream `ValidationError` is refuted.** The stream does not raise.
- **A real defect showed up instead.** `approval_prompt` scanned `prompt`/`message`/`text`/
  `description`; MAF sets none of them on a `function_approval_request` and puts the subject on a
  nested `function_call`. Every approval this system can raise rendered as the bare fallback
  `"Approval requested."` — a chemist asked to approve an unnamed something. `approval_prompt` now
  falls through to `Approve calling <name>?`.

The comment beside `_signal_event` justified the empty `approval_id` on that branch by calling it "a
plan prompt … answered by the next turn". It is not: plan approval is `chemclaw.agent.plan_gate`, a
middleware refusal plus a `plan_approvals` row, and it never reaches this stream. Corrected.

### Timeouts become scalable rather than larger

`tests/conftest.py` gains `CHEMCLAW_TEST_TIMEOUT_SCALE`, a multiplier applied to **every** cap —
the `pyproject.toml` default and each `@pytest.mark.timeout(...)` marker, by writing an explicit
scaled marker onto each collected item.

Raising the constants was considered and rejected on the measurement: the observed single-test
runtime under five concurrent agents was ~6x the cap, and that multiplier is a property of the
machine, not of the test. A constant chosen for a loaded box is no cap at all on an idle one, which
throws away what these markers are for — `test_bo_predict.py` says so in its own docstring ("at 60s
a spike names itself instead of eating the 180s the whole file shares"). Deleting them was rejected
too: a runaway xTB optimisation hanging CI indefinitely is a real failure they catch. Scaling keeps
each cap's ratio to its work and moves them together.

Two details are load-bearing and both are pinned by `tests/test_suite_timeouts.py`, which runs a
real pytest session importing the real hook rather than a transcription of it:

- The replacement marker copies the original's `method=`/`func_only=` kwargs.
  `pytest_timeout._get_item_settings` reads timeout and method off the *one* closest marker, so
  dropping them would silently return every Temporal-backed module to the `signal` method that
  cannot interrupt a thread blocked in `temporalio`'s Rust core — the 28-minute silent hang D-117's
  successor fixed, reintroduced invisibly, since those modules skip without a Temporal server.
  Mutation: `pytest.mark.timeout(seconds * scale, **kwargs)` → `pytest.mark.timeout(seconds * scale)`.
- A `pytest_terminal_summary` hook prints a `timeouts — these assertions never ran` section listing
  every timed-out node and naming the knob. This is the part that addresses the actual damage: a
  timed-out test is not weak evidence about the code, it is **none**, and two separate readers of
  this repository read `Failed: Timeout (>180.0s)` as a numerical failure.

`0`, a negative and an unparseable value are refused rather than clamped, because each would read as
"no timeout at all" — turning the knob that keeps the caps usable into one that deletes them.

### Four claims made in prose become property tests

`tests/test_properties_core.py` grows from six invariants to nine (`hypothesis` was already a
dependency):

- **`parse_note(write(render_note(n))) == n`.** `kg/render.py` stated this as an equation. It is
  not one, and generating notes found the two exceptions in seconds: `python-frontmatter` strips the
  content it parses (a body of `" "` returns `""`), and `Path.read_text` translates line endings
  (`"a\rb"` returns `"a\nb"`). Neither survives Markdown rendering, so neither is worth fixing — but
  the docstring now says which two things the equation is up to, because a note whose body is only
  whitespace coming back empty would otherwise send whoever hit it looking for a schema bug. Every
  frontmatter field round-trips exactly. Mutation: adding `valid_to` to `render_note`'s
  `exclude` set now fails.
- **`_build_submission` writes one file per note, subject first.** Its docstring argues that a
  caller "may legitimately list the same dependency twice" and that two renderings of one path in a
  commit is "at worst two different renderings racing". The generator produces exactly the
  collisions a fixed example cannot enumerate, including two distinct notes sharing an id.
  Mutation: removing the `if dependency.id in seen: continue` guard now fails.
- **A refused budget scope stays refused.** The tracker is documented as best-effort about
  *overshoot*; it is not best-effort in the other direction, and nothing upstream re-checks.
  Negative token counts are generated deliberately, because `_book` clamps with `max(tokens, 0)` and
  a provider's usage field is not trustworthy. Mutation: letting a negative token count decrement
  the turn counter now fails.
- **In-memory vs Postgres `find` agreement is NOT here.** It needs a database, which this file will
  not take; `docs/planning/BACKLOG.md` carries it.

### `InMemoryStore.find` no longer raises on a mixed store

Writing the undated-ordering test exposed a live crash: the sort key was
`s.created_at or datetime.max`, and `datetime.max` is **naive** while every real `created_at` here
is timezone-aware. One store holding one dated and one undated result raised `TypeError: can't
compare offset-naive and offset-aware datetimes` — not a wrong order, no order at all. The existing
undated test never saw it because a single-element list is never compared. The sentinel is gone;
undated rows are partitioned out and lead, which is the same policy with nothing left to get wrong.

## What was refuted

- **The approval path's mid-stream `ValidationError`.** Driven with the content MAF actually
  produces, the stream completes normally. A different, real defect was found in its place.
- **`InMemoryStore.find`'s `created_at` sort direction.** Both directions are already killed by
  existing tests — `test_an_empty_query_returns_everything_newest_first` pins the in-memory order,
  and `test_find_matches_the_in_memory_backend`'s `limit=1` query kills the Postgres
  `ORDER BY created_at DESC` → `ASC` mutation. Only *where an undated row lands* was unpinned, and
  only that is added. Claiming otherwise would have been the "now pinned by a test" overclaim this
  campaign keeps finding.

## What was deliberately not done

**The Helm chart tests still read template source, not rendered YAML.** Every assertion in
`tests/test_helm_chart.py` is against `values.yaml` parsed as YAML and `templates/*` read as text,
so "the mount is read-only" really means "that string appears in that helper's body" — true of a
helper wrapped in a `{{- if }}` no deployment satisfies. `helm` is not a Python dependency and is
absent from this sandbox, so rendering here is not possible, and faking a renderer would be worse
than the gap. CI does render (`make helm-validate`) but only pipes the result to `kubeconform`,
which asks whether the YAML is schema-valid and never whether it says what these tests claim. The
module docstring now states that limit in the first paragraph a reader reaches, and
`docs/planning/BACKLOG.md` carries the fix with its trigger.

**The twenty per-file fake updates were not unified.** The brief allowed a shared fake-update
builder; the evidence gap is closed by one fake deriving `user_input_requests` from `contents` the
way MAF does, and rewriting nineteen other files in a lane that owns none of them would be a large
change with no test behind it.

## Consequences

- Six product-code lines changed, all exposed by a new test: `approval_prompt`'s fallback,
  `InMemoryStore.find`'s sentinel, and two corrected comments (`runner._signal_event`,
  `kg/render`'s module docstring).
- `tests/conftest.py` enables the `pytester` plugin, which is only possible in the rootdir conftest.
  It registers fixtures and changes nothing else.
- A contended machine now has a supported answer (`CHEMCLAW_TEST_TIMEOUT_SCALE=4 make test`) and a
  run that hits a cap says so in its own words. CI does not set it: the gate runs in ~5 minutes on
  a dedicated runner, and a scale there would only hide a real regression in test runtime.
- The nine surviving mutations recorded above are the regression suite for this ADR. Re-applying any
  of them should be red.
