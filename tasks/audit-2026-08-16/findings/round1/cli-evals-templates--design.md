# Round 1 — `cli/`, `evals/`, `templates/` — design and simplification

Slice read in full: every module under `src/chemclaw/cli/`, `src/chemclaw/evals/` and
`src/chemclaw/templates/`. Findings are ordered most-severe first. All reproductions were run
against the live stack (`dockerd` + `make up` + `make db-migrate`, Postgres and Temporal healthy).

---

## `check_result_cached` counts the whole table, so it passes on any earlier run's residue

- **Severity**: high
- **Location**: `src/chemclaw/cli/live_jobs.py:218` (`check_result_cached`)
- **Trigger**: run `make live-jobs` (or call `check_result_cached()`) against any database that
  already holds one `calculation_results` row whose `calc_type` starts with `xtb`. It need not have
  been written by this run, by this job, or by this decade.
- **Consequence**: the check the module docstring nominates as the one that "makes the
  never-recompute guarantee (D-011) **observable** rather than asserted" passes without observing
  anything. It has no bound on `run.workflow_id`, on `created_at`, or on the smoke's own species —
  it asks "does any xtb row exist anywhere". Two things make this reachable rather than theoretical:
  (a) `main()` exits `0 if run.ok else 1`, so a false pass here is the difference between a green
  lane and a red one; (b) the lane is *designed* to write no new cache row on a repeat —
  `_RUN_TEMPERATURE_K` varies the temperature so the *workflow id* is new, but
  `calculation_results` is keyed on species and method, so the second and every later run computes
  nothing new. `live_storm.py:386-399` records exactly this measurement ("one new job record, zero
  new cache rows"). So from run two onward this check is structurally incapable of failing, and it
  is precisely the failure the module's own docstring says "must not be built into it" ("a lane that
  goes green while exercising none of the system it claims to test").
- **Evidence**: the whole query is unqualified —

  ```python
  count = await _scalar(
      "select count(*) from calculation_results where calc_type like %s", ("xtb%",)
  )
  return Check(name="calculation cached in Postgres", passed=bool(count), ...)
  ```

  Reproduced against the live Postgres, with *no* smoke run in the process at all:

  ```
  pre-existing xtb* rows: 0
  check_result_cached (no smoke ran in this process) -> False | 0 xtb* row(s) in calculation_results
  after planting ONE unrelated row -> True | 1 xtb* row(s) in calculation_results
  ```

  (the planted row was `key='audit-probe-key', calc_type='xtb_energy'`, deleted afterwards).
- **Fix**: behaviour-preserving in shape, not in verdict — take a `before` count as
  `check_idempotent` already does, and assert the *smoke's own* rows. Concretely: capture
  `select now()` before `_launch`, then
  `select count(*) from calculation_results where calc_type like 'xtb%' and created_at >= %s`, and
  report the delta in `observed`. Because a warm cache legitimately writes zero, the honest check is
  "either this run wrote rows **or** the cache already answered the identical species set" — which
  means the smoke also needs to state which of the two happened, exactly as
  `family_d_durable`'s third finding does. `check_idempotent` is the model already in this file;
  this check is the one place that did not follow it.

---

## Family A's "every offered turn is accounted for" is an arithmetic identity, not a check

- **Severity**: medium
- **Location**: `src/chemclaw/cli/live_storm.py:1011-1012` and `1053-1060`
  (`family_a_admission`)
- **Trigger**: any storm run including family A, at any cap, with any result set — including one
  where the front door answered nothing.
- **Consequence**: the finding named "every offered turn is accounted for at every cap" can never be
  `ok=False`. It occupies a row in the report table and one slot in the "**N/M checks passed**"
  denominator while asserting nothing about the system. This is the shape the same file spends
  three docstrings warning about ("a bound that a run doing nothing also meets",
  "the vacuous pass this harness has now had to correct three times").
- **Evidence**: `failed` is *defined* as `turns - accepted`, and the check tests
  `accepted + failed != turns`:

  ```python
  accepted = sum(1 for r in results if r.status == 200 and r.error_code is None)
  turns, failed = len(results), len(results) - accepted   # line 1012
  ...
  lost = [row for row in rows if row["accepted"] + row["failed"] != row["turns"]]  # line 1053
  ```

  Reproduced over random result sets:

  ```
  accepted+failed == turns ? True 14 34 48
  ```

  There is no assignment path in the function that can break the identity: `max(repeats, 1)`
  guarantees the inner loop body runs, so the three names are always set together from one
  `results` list.
- **Fix**: make it a real accounting check by comparing against what was *offered* rather than
  against what came back — `sweep_turns` (and `offered`) are the numbers the harness controls, and
  `len(results)` is what `asyncio.gather` returned. `ok = row["turns"] == sweep_turns` catches a
  gather that lost a task or a `storm()` that silently returned short. Not behaviour-preserving:
  it turns a row that always passes into one that can fail, which is the point.

---

## Family B asks the audit trail an all-time question and calls it "this run"

- **Severity**: medium
- **Location**: `src/chemclaw/cli/live_storm.py:569-594` (`family_b_tool_truth`), the query at 585
- **Trigger**: run `make live-storm` twice against one database. On the second run, delete the mock
  or break the front door so the `a-retrieval` turn at line 581 produces nothing — family B still
  reports all three tools as "bodies ran".
- **Consequence**: the family whose entire purpose is "LOAD-1 made permanent — a turn count is never
  allowed to stand in for a tool count" is itself standing in an all-time row count for this run's
  tool calls. Its docstring claims the opposite in as many words: the seeding turn exists so "the
  audit question below is asked of something this run actually did rather than of residue an earlier
  run left in the table". Running a turn first does not make an unqualified `count(*)` a question
  about that turn.
- **Evidence**:

  ```python
  count = await _scalar("select count(*) from audit_events where tool = %s", (tool,))
  ... ok=bool(count), observed=f"{count} audited call(s)"
  ```

  Reproduced against the live Postgres:

  ```
  family B would observe: 0 audited call(s) -> ok = False
  after planting ONE year-old residue row -> observed 1 audited call(s), ok = True
  ```

  (row inserted with `ts = now() - interval '365 days'`, deleted afterwards).
- **Fix**: behaviour-preserving for a clean database, correct for a dirty one — take
  `since = await _scalar("select now()")` before the seeding turn and query
  `... where tool = %s and ts >= %s`. `family_d_durable` already does exactly this three functions
  up (`live_storm.py:351,355`), so the fix is to apply the pattern this file already established.

---

## `explain` prints a turn twice whenever it has both an audit row and a job row and no transcript

- **Severity**: medium
- **Location**: `src/chemclaw/cli/explain.py:164` (`_render`)
- **Trigger**: `python -m chemclaw.cli.explain <session>` for a session with a correlation id that
  appears in **both** `audit_events` and `job_records` but not in `session_messages`. The module's
  own docstring says this is routine, not exotic: `durable/retention.py` prunes message rows by age
  and an abandoned turn never writes a transcript row at all, "so the trail routinely outlives the
  words it points at".
- **Consequence**: the reconstruction renders that whole turn — header, transcript-absent line, job
  lines, tool lines — twice. An auditor reading "why was this run?" sees one job launch reported as
  two.
- **Evidence**: the de-duplication is hand-rolled against `order` only, not against what has already
  been emitted:

  ```python
  shown = [*order, *(cid for cid in (*calls, *jobs) if cid not in set(order))]
  ```

  `(*calls, *jobs)` concatenates the two key sequences, so a correlation id present in both is
  yielded twice and passes the `not in set(order)` test both times. Reproduced:

  ```
  session s1

  ── turn turn-1
     transcript: absent (compacted, pruned, or rolled back)
     job calc:compute_reaction_energy — because: because
         → done
     tool find_notes [ok, 1 ms, a]

  ── turn turn-1
     transcript: absent (compacted, pruned, or rolled back)
     job calc:compute_reaction_energy — because: because
         → done
     tool find_notes [ok, 1 ms, a]

  ---- turn header count: 2
  ```

- **Fix**: one line, behaviour-preserving except for the defect —
  `shown = list(dict.fromkeys([*order, *calls, *jobs]))`. `dict.fromkeys` preserves first-seen order,
  which is the property the current expression was reaching for, and removes the `set(order)` rebuilt
  per element as a side benefit.

---

## `session_tokens` / `TurnTokens` / `ProbeOutcome.tokens` are ~90 lines with no caller

- **Severity**: medium
- **Location**: `src/chemclaw/evals/live.py:80-88` (`_SESSION_COST_SQL`), `115-141` (`TurnTokens`),
  `244` (`ProbeOutcome.tokens`), `367-427` (`session_tokens`); plus
  `src/chemclaw/core/config/evals.py:92` (`live_probe_cost_wait_seconds`)
- **Trigger**: any probe run. `run_turn` never assigns `outcome.tokens`, so every transcript on disk
  and every re-graded outcome carries `tokens: null`.
- **Consequence**: two things at once. (1) It is dead: `session_tokens` has **no** reference anywhere
  outside its own definition — not in `cli/live_probes.py`, not in `evals/phoenix.py`, not in
  `tests/`. `live_probe_cost_wait_seconds` exists solely to be read by it. (2) The field it feeds is
  worse than absent, because its own docstring assigns `None` a meaning: "`None` means the ledger
  could not be asked … which is a different finding from 'it cost nothing'". Every transcript this
  harness has ever written therefore records the strongest available claim that the cost ledger was
  unreachable, on runs where it was not. The dynamic-registration exemptions do not apply — this is
  not a Temporal workflow, an MCP tool, an entry point or a discriminated-union member; it is an
  ordinary async function.
- **Evidence**:

  ```
  $ grep -rn "session_tokens" --include=*.py .
  src/chemclaw/evals/live.py:367:async def session_tokens(session_id: str) -> TurnTokens | None:
  $ grep -rn "\.tokens\b" src/chemclaw/evals src/chemclaw/cli
  (no matches)
  $ grep -rn "live_probe_cost_wait_seconds" src/ tests/
  src/chemclaw/evals/live.py:393  src/chemclaw/evals/live.py:397  src/chemclaw/core/config/evals.py:92
  ```

  And the field is `None` on a freshly built outcome, which is the only state it ever holds:

  ```
  default latency: 0.0 tokens: None
  ```

- **Fix**: one of two, and the choice is a product decision rather than a cleanup one. Either wire
  it — `run_turn` ends with `outcome.tokens = await session_tokens(session_id)` and `_summary`
  gains a cost row, which is what the docstrings describe and costs one `await` per probe — or
  delete all four pieces plus the setting. Deleting is behaviour-preserving; wiring is not (it adds
  a bounded per-turn Postgres poll). Leaving it is the one option that keeps writing a false
  "unmeasured" into every transcript.

---

## The sweep table prints one sample's counts beside three samples' median

- **Severity**: medium
- **Location**: `src/chemclaw/cli/live_storm.py:996-1037` (`family_a_admission`), rendered at
  `1191-1209` (`report`)
- **Trigger**: any storm with the default `--sweep-repeats 3`.
- **Consequence**: within one row of the SCALE-3 table, `accepted`, `shed/error`, `p50 s` and
  `p95 s` are the **last repeat only**, while `answered/s` and `offered drained/s` are the **median
  of all three**. A reader comparing "8 accepted of 48" against "1.43 answered/s" is comparing two
  different populations, and the row is not internally consistent — `accepted / elapsed` for the
  printed `accepted` is generally not the printed goodput. The whole argument for taking repeats
  (the docstring's "a plateau test that supplies its own noise reproduces exactly the error that ADR
  exists to prevent") applies to these columns too and was not carried through to them.
- **Evidence**: the counters are plain reassignments inside the repeat loop, and the row is built
  after it:

  ```python
  for _ in range(max(repeats, 1)):
      ...
      accepted = sum(...)                 # overwritten each repeat
      turns, failed = len(results), len(results) - accepted
      p50, p95 = percentiles(results)     # overwritten each repeat
      samples.append(accepted / max(elapsed, 0.001))
      drains.append(turns / max(elapsed, 0.001))
  rows.append({... "accepted": accepted, "p50": p50, "p95": p95,
               "goodput": statistics.median(samples), ...})
  ```

- **Fix**: accumulate them like `samples`/`drains` and take the median of each
  (`accepted_samples.append(accepted)` … `"accepted": statistics.median(accepted_samples)`), or —
  simpler and equally honest — print the *sum* of accepted and offered across repeats and label the
  column so. Not behaviour-preserving for the numbers printed; it is behaviour-preserving for every
  verdict, since no `Finding` reads these four fields.

---

## `judge_outcome` turns a bad grade into a crash, in the one module built to prevent that

- **Severity**: medium
- **Location**: `src/chemclaw/evals/live_judge.py:182-187` (`judge_outcome`), reached from
  `src/chemclaw/cli/live_probes.py:399`
- **Trigger**: the judge returns well-formed JSON whose `verdict` is not one of the five literals —
  `"SERVED"`, `"pass"`, `"partial "`, anything. One probe out of 190 is enough.
- **Consequence**: `Judgement(...)` raises `ValidationError`, which propagates out of
  `asyncio.gather(*(grade(o) for o in outcomes))` in `cli/live_probes._main`. The whole grading pass
  dies with a traceback *after* every live question has already been asked and paid for; `_summary`
  and `_write_outputs` never run, so the run produces no summary and no `grades.json` (the
  per-probe transcripts survive, because `run_probes` writes them as they land). This is exactly
  the class the module's own header declares closed: "`ungraded` is not a grade — it is the absence
  of one, and it exists because the first version of this module did not have it… A verdict that
  cannot be obtained must be visibly missing." Three failure modes are funnelled to `ungraded`
  (token ceiling, no JSON object, JSON decode error) and the fourth — a verdict outside the
  vocabulary — is the one left to raise.
- **Evidence**:

  ```python
  return Judgement(
      probe_id=probe.id,
      verdict=payload.get("verdict", "ungraded"),   # unvalidated straight into a Literal field
      ...
  )
  ```

  ```
  out-of-vocab verdict -> ValidationError
  ```

- **Fix**: two lines, behaviour-preserving for every valid reply —
  ```python
  verdict = str(payload.get("verdict", "ungraded"))
  if verdict not in get_args(Verdict):
      return Judgement(probe_id=probe.id, verdict="ungraded",
                       reason=f"judge returned unknown verdict {verdict!r}")
  ```
  Independently, `live_probes` should pass `return_exceptions=True` to both `gather` calls and
  record a failed grade rather than losing the report — the same reason `run_probe` records a
  transport failure instead of raising.

---

## Three copies of the same live-harness plumbing, and one report table written three times

- **Severity**: medium
- **Location**:
  - `_TERMINAL`: `src/chemclaw/cli/live_jobs.py:56-62` and `src/chemclaw/cli/live_storm.py:86-92` —
    **byte-identical**.
  - `_scalar`: `src/chemclaw/cli/live_jobs.py:192-197` and `src/chemclaw/cli/live_storm.py:247-252`
    — identical bodies, one word different in the docstring ("using"/"through").
  - `_workflow_status`: `src/chemclaw/cli/live_jobs.py:185-189` and
    `src/chemclaw/cli/live_storm.py:627-631` — identical bodies, different docstrings.
  - The launcher incantation `find_job(...) → build_job_tool(...) → tool.__annotations__["params"]
    → job_workflow_id(...)`: `live_jobs.py:177-180`, `live_jobs.py:304-307`,
    `live_storm.py:714-717` — three copies.
  - The verdict table (`| … | result | observed |`, `"PASS"` / `"**FAIL**"`,
    `**N/M checks passed**`): `live_jobs.report:371-386`, `live_storm.report:1212-1219`,
    `live_probes._findings_report:232-249` — three renderers over three near-identical records
    (`live_jobs.Check(name, passed, observed, detail)`,
    `live_storm.Finding(family, name, ok, observed, detail)`,
    `evals.live.Finding(probe_id, check, ok, observed)`).
- **Trigger**: not a runtime failure — a maintenance one, and it has already fired once in this
  tree. The `% 25` / `% 719` / `% 100_000` modulus divergence documented at `live_jobs.py:86-105`
  and `storm_behaviours.py:58-73` is precisely this: one reasoned value landing in one of three
  copies. That divergence needed a dedicated test (`tests/test_run_jitter.py`) to police three
  copies that should have been one function.
- **Consequence**: a change to how the harness talks to Postgres or Temporal (a pool option, an
  added terminal state, a `describe()` signature change) has to be made in two places with nothing
  connecting them; `live_jobs.report`'s own docstring says it renders "in the same shape
  `cli/live_probes.py` reports its own", which is a comment standing in for a shared function.
- **Evidence**: the diff of the two `_scalar`s is one word of prose:

  ```
  --- live_jobs                       +++ live_storm
  -    """One value from the live database, using the application's own connection helper."""
  +    """One value from the live database, through the application's own connection helper."""
  ```

- **Fix**: a small `chemclaw/cli/live_support.py` holding `_TERMINAL`, `scalar()`,
  `workflow_status()`, `launch_job(job_name, payload, rationale) -> (workflow_id, result)` and one
  `render_checks(title, rows, key)` — five functions, all behaviour-preserving, all with three or
  more existing callers (so this clears the Rule-of-Three bar the repo sets). The `Finding` types
  should stay distinct: `evals/live.py:653-659` argues that case correctly and I agree with it — a
  shared four-field record would carry a dead key. It is the *renderer* that is duplicated, not the
  record, and a renderer parameterised on the key column serves all three.

---

## `_fragments(document, n)` does not produce `n` fragments

- **Severity**: low
- **Location**: `src/chemclaw/cli/mock_llm.py:244-250` (`_fragments`), consumed at `336` and `431`,
  parameterised by `ToolCall.fragments` (`mock_llm.py:61-78`)
- **Trigger**: any `fragments` value that does not divide the argument document exactly.
- **Consequence**: the knob `ToolCall.fragments`' own docstring calls "the knob that matters" does
  not deliver the number it is set to, and the shipped catalogue is off in both directions: the
  behaviour named `c-fragmented` — whose `text` field literally reads "arguments delivered in eight
  fragments" and whose comment reasons about whether "an 8-fragment call emits 8 `tool_call`
  events" — sends **ten**. `c-parallel` asks for 3 and sends 4. Above the document length the value
  silently saturates: `fragments=40` on a 29-character document sends 29. Family C's verdict
  (`announced == returned`) does not read the count, so no check is currently wrong — but the
  harness's parameter, its behaviour text and its reasoning all name a number the code does not
  produce, which is the "naming that misleads about behaviour" case.
- **Evidence**:

  ```python
  size = max(1, len(document) // count)
  pieces = [document[i : i + size] for i in range(0, len(document), size)]
  ```

  `ceil(len/floor(len/count))`, not `count`. Measured:

  ```
  asked   1 fragments -> got   1  (doc len 29)
  asked   3 fragments -> got   4  (doc len 29)
  asked   8 fragments -> got  10  (doc len 29)
  asked  40 fragments -> got  29  (doc len 29)
  huge doc, asked 400 -> 401
  ```

- **Fix**: behaviour-preserving in intent, exact in count — cut at computed boundaries instead of a
  fixed stride:
  ```python
  n = min(count, len(document))
  bounds = [len(document) * i // n for i in range(n + 1)]
  return [document[a:b] for a, b in zip(bounds, bounds[1:], strict=True)]
  ```
  and say in the docstring that the count saturates at one character per fragment, since that
  ceiling is real and worth stating rather than discovering.

---

## `phoenix._UNKNOWN_DURATION` and its branch are unreachable

- **Severity**: low
- **Location**: `src/chemclaw/evals/phoenix.py:68-72` (`_UNKNOWN_DURATION`) and `228-240` (`_window`)
- **Trigger**: none — that is the finding.
- **Consequence**: a module constant carrying a five-line justification, plus the branch that reads
  it, cannot execute. The justification is also factually wrong about the case it names: it says
  "a transport error is the case where `latency_seconds` is most likely missing", but
  `evals/live.run_turn:563` assigns `latency_seconds` unconditionally *after* the `except` block, so
  a transport error is precisely the case where it is always present.
- **Evidence**: `ProbeOutcome.latency_seconds` is declared `float = 0.0` on a model with
  `extra="forbid"`, so it can never be `None`, and `_window`'s guard is therefore always true:

  ```
  latency_seconds=None rejected: ValidationError
  _UNKNOWN_DURATION reachable? (…06:54:51.533200+00:00, …06:54:51.533200+00:00)
  ```

- **Fix**: behaviour-preserving — delete `_UNKNOWN_DURATION` and collapse `_window` to
  `return at, at + timedelta(seconds=outcome.latency_seconds)`.

---

## `live_probes` keeps its suite registry in two maps that must agree

- **Severity**: low
- **Location**: `src/chemclaw/cli/live_probes.py:55-58` (`_M12_SUITES`) and `351-354` (the runner
  dispatch inside `_main`), joined by `choices=` at `417`
- **Trigger**: add a suite to `_M12_SUITES` (which is presented, in its own comment, as the one
  place a suite is declared: "declared as a map rather than derived from the suite name so that a
  suite whose file is missing fails at the lookup with a name a reader can search for") and forget
  the second map. `--suite <new>` is then an accepted `choices=` value that dies on
  `KeyError: '<new>'` in `_main`.
- **Consequence**: the argument parser's vocabulary and the dispatcher's vocabulary are derived from
  two different literals. The comment on `_M12_SUITES` promises the failure mode this arrangement
  does not have.
- **Evidence**:
  ```python
  parser.add_argument("--suite", default="corpus", choices=["corpus", *sorted(_M12_SUITES)], ...)
  ...
  if args.suite in _M12_SUITES:
      runner = {"plan-gate": _run_plan_gate, "degradation": _run_degradation}[args.suite]
  ```
- **Fix**: behaviour-preserving — one map,
  `_M12_SUITES: dict[str, tuple[str, Callable[[Namespace], Awaitable[int]]]] = {"plan-gate":
  ("plan_gate.yaml", _run_plan_gate), "degradation": ("degradation.yaml", _run_degradation)}`,
  with `_m12_probes` reading `[0]` and `_main` reading `[1]`.

---

## The four validators repeat the same report-and-exit body verbatim

- **Severity**: low
- **Location**: `src/chemclaw/cli/validate_connectors.py:309-318`,
  `validate_skills.py:197-208`, `validate_templates.py:293-311`,
  `validate_datasources.py:128-145`
- **Trigger**: adding the ninth validator the package README anticipates.
- **Consequence**: four copies of an eight-line body differing only in a noun. The package README's
  claim that these are "thin shims" is true; that they are *four separate* thin shims for one shape
  is the cost. Each new validator re-derives the exit-code convention by copy.
- **Evidence**: the four bodies, side by side, are identical modulo the string
  `"connector"` / `"SKILL.md"` / `"template"` / `"data source"`:
  ```python
  problems = <validate_x>()
  if problems:
      print("<x> validation failed:")
      for problem in problems:
          print(f"  - {problem}")
      return 1
  print("<x> validation passed.")
  return 0
  ```
- **Fix**: behaviour-preserving — one `report_problems(label: str, problems: Sequence[str]) -> int`
  in a shared `cli/_gate.py`, called by all four. Four callers today, so this is not a speculative
  abstraction. `validate_prose_contract.main` legitimately stays outside it (it writes to stderr and
  prints a count).

---

## `evals/__init__` publishes three private aliases as its public surface

- **Severity**: low
- **Location**: `src/chemclaw/evals/__init__.py:11-18`
- **Trigger**: `from chemclaw.evals import *`, or any reader trying to learn the package's public
  API from its `__all__`.
- **Consequence**: `__all__ = ["_autonomy", "_metrics", "_retrieval"]` names three underscore-
  prefixed module aliases that exist only to keep the linter quiet about imports whose value is a
  registration side effect. The module docstring, two lines above, states the real public surface
  ("the metric interface + registry (`metric`), the eval harness (`harness`), and the tool-utility
  A/B (`ab`)") — and `__all__` lists none of those three. Two idioms for the same side-effect import
  live in this tree; the other one is honest about it
  (`validate_skills.py:43`: `from chemclaw.agent import chemclaw_agent as _agent  # noqa: F401 —
  imported for tool registration`).
- **Evidence**: the file in full is three aliased imports and that `__all__`; nothing else in the
  repository imports `chemclaw.evals._autonomy` or its siblings.
- **Fix**: behaviour-preserving — replace with three plain
  `from chemclaw.evals import autonomy  # noqa: F401 — registers the autonomy metrics` lines and
  drop `__all__`, matching the idiom already used in `cli/validate_skills.py`.

---

## `validate_connectors` announces four rules, lists five, and numbers two of them "5"

- **Severity**: low
- **Location**: `src/chemclaw/cli/validate_connectors.py:5-31` (module docstring), `108` and `244`
  (the two "rule 5" claims), `40` (the private import)
- **Trigger**: reading the file to find out which rule a CI failure came from.
- **Consequence**: the docstring says "the four things a per-file schema cannot see" and then
  enumerates 1–5; `_served_tool_problems`'s docstring says "(rule 5)" and
  `_connector_urls_problems`'s docstring also says "(rule 5)", so the identifier a failure message
  would be traced by names two different checks. Separately, the module reaches into another
  package's private surface — `from chemclaw.connectors.jobs import _params_model` — which makes a
  rename inside `connectors/jobs.py` a silent break of the validator that guards `connectors/`.
- **Evidence**: line 5 (`the four things`) against items numbered 1–5 at lines 8–30; `(rule 5)` at
  line 108 and again at line 244; `_params_model` imported at line 40 and used at line 194.
- **Fix**: behaviour-preserving — renumber the URL check to rule 6 and correct the count to five in
  the header (or delete the count, per this repo's own "the count lives in the test not in the
  prose" rule), and either export `params_model` from `connectors/jobs.py` under a public name or
  have `_precondition_problems` obtain the model from `build_job_tool`'s annotations, which it
  already builds two lines earlier in `_job_problems`.

---

## Checked and found sound

Recorded so the absence of a finding is not read as an absence of a pass:

- `templates/manifest.py`, `templates/resolve.py`, `templates/registry.py` — the reference grammar
  is duplicated in `manifest._REFERENCE` and `resolve._REFERENCE` (byte-identical), which is a real
  clone, but `resolve.py`'s purity constraint (it runs inside a Temporal workflow) is a genuine
  reason to keep it importing nothing, and the two regexes agree today. Not reported as a finding;
  worth a comment cross-referencing them, or a shared constant in a leaf module.
  `_references_resolve_and_point_backwards` is the tightest validator in the slice: forward
  references, unknown inputs and duplicate ids all fail at load, and `_step_references` dispatches
  correctly on all three step kinds.
- `evals/soak_report.py` — `fit`/`describe` is the cleanest module in the slice. The `head`/`tail`
  split, the `_MIN_POINTS_TO_FIT` guard and the two-halves-not-tail-vs-whole comparison are each
  load-bearing and each stated once.
- `evals/baseline.py` — `drift_band` is genuinely shared between `detect_drift` and
  `compare_to_baseline`, `vanished` is kept distinct from a real 0.0, and the `TYPE_CHECKING` import
  of `EvalReport` correctly breaks a real cycle.
- `evals/metrics.py` / `evals/autonomy.py` — `precision_recall_f1` has four callers across two
  modules and is the right extraction; `plan_quality` reusing it rather than writing a second
  set-comparison is the DRY call made correctly.
- `cli/leak_probe.py::_positive` vs `cli/sync_share.py::_positive` — a deliberate 4-line duplicate
  with the reasoning written down. I agree with the call; the import would be larger than the code.
- `cli/connectors_dev.py` — `build_composite`'s lifespan handling has exactly one non-obvious
  behaviour (Starlette does not run a mounted app's lifespan) and it is the reason the function
  returns an app rather than mounting onto a bare one. `cli/leak_probe.py` importing
  `build_composite` to get the URLs rather than re-deriving the string is the right seam.
- `evals/metric.py` registry, `cli/erase_actor.py`, `cli/schedules.py`, `cli/backfill_corpus.py`,
  `cli/phoenix_publish.py` — nothing to report; each is a thin shim over library logic, which is
  what `cli/README.md` says the package is for.
- Dynamic-registration check before calling anything dead: `evals/metrics.py`,
  `evals/retrieval.py` and `evals/autonomy.py` register via the `@metric` decorator at import (so
  `evals/__init__`'s side-effect imports are load-bearing, only its `__all__` is not);
  `templates/registry.build_template_tool` mints tools by name at runtime;
  `durable/eval_drift.py` is the second caller of `detect_drift`/`aggregate_metrics`. None of these
  are reported as dead. `session_tokens` was checked against all four mechanisms before being
  called dead.
