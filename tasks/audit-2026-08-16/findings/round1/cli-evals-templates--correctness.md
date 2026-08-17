# cli / evals / templates — CORRECTNESS

Slice: `src/chemclaw/cli/`, `src/chemclaw/evals/`, `src/chemclaw/templates/`.
Every finding below was reproduced by running code in this checkout (`uv run`), not read off a comment.

---

## `--regrade` crashes on the `grades.json` its own run wrote

- **Severity**: high
- **Location**: `src/chemclaw/cli/live_probes.py:184` (`_load_transcripts`), against `src/chemclaw/cli/live_probes.py:206` (`_write_outputs`)
- **Trigger**: Any judged corpus run (`python -m chemclaw.cli.live_probes`), followed by the documented recovery path `python -m chemclaw.cli.live_probes --regrade` over the same transcript directory.
- **Consequence**: `TypeError: list indices must be integers or slices, not str`, before a single probe is re-graded. `--regrade` is the one feature whose stated purpose is "re-grade without re-asking 190 live questions to fix a grader bug"; after the first judged run it cannot be used at all without a human manually moving `grades.json` out of the way. The equivalent reader in the same package already knows about this hazard and guards it (`evals/phoenix.py:66`, `_NOT_TRANSCRIPTS = {"grades.json", "evidence.json", "summary.md"}` — "so the directory is a mixed bag and the reader has to know which is which"); `_load_transcripts` is the second reader and has no such filter.
- **Evidence**:

  ```python
  # live_probes.py
  def _load_transcripts(directory: Path) -> tuple[list[Probe], list[ProbeOutcome]]:
      for path in sorted(directory.glob("*.json")):          # <- grades.json matches
          probe, outcome = judgement_from_transcript(json.loads(path.read_text()))

  def _write_outputs(transcript_dir: Path, report: str, grades: list[Judgement]) -> None:
      transcript_dir.mkdir(parents=True, exist_ok=True)
      (transcript_dir / "summary.md").write_text(report, encoding="utf-8")
      if grades:
          (transcript_dir / "grades.json").write_text(...)    # <- into the same directory
  ```

  `judgement_from_transcript` then does `payload["probe"]` on a JSON *list*.

  Script `/tmp/claude-0/repro/regrade.py` (writes one transcript, calls the real `_write_outputs`, then the real `_load_transcripts`) printed:

  ```
  files: ['grades.json', 'p1.json', 'summary.md']
  REGRADE FAILED: TypeError list indices must be integers or slices, not str
  ```

- **Fix**: import and reuse the existing constant rather than adding a second copy — `from chemclaw.evals.phoenix import _NOT_TRANSCRIPTS` (or promote it to `evals/live.py` beside the writer) and filter with `if path.name not in _NOT_TRANSCRIPTS` in `_load_transcripts`. One definition, since the two directories are written by the same function.

---

## A single out-of-enum judge verdict discards every grade in the run

- **Severity**: high
- **Location**: `src/chemclaw/evals/live_judge.py:182-187` (`judge_outcome`), reached from `src/chemclaw/cli/live_probes.py:399` and `:372`
- **Trigger**: The judge model returns well-formed JSON whose `verdict` is not one of the five `Verdict` literals — `"Served"`, `"SERVED"`, `"good"`, `"partial "` with a stray space, or any other near miss.
- **Consequence**: `Judgement(...)` raises `ValidationError` out of `judge_outcome`. Both call sites use `await asyncio.gather(*(grade(o) for o in outcomes))` with no `return_exceptions`, so the first such reply propagates out of `_main`, `_write_outputs` never runs, and **all** grades for the run (230 probes in the shipped corpus) are lost along with the summary. The corpus run is not cheap to repeat — the module docstring is explicit that "re-asking 190 live questions to correct a grader bug would have changed the thing being measured as well as the measurement". This is the exact failure class the module's own `ungraded` verdict was introduced for ("A verdict that cannot be obtained must be visibly missing"), applied to the parse failure and to the token ceiling but not to the enum.
- **Evidence**:

  ```python
  return Judgement(
      probe_id=probe.id,
      verdict=payload.get("verdict", "ungraded"),   # unchecked against Verdict
      ...
  )
  ```

  ```
  $ uv run python -c '... Judgement(probe_id="p", verdict=v) ...'
  Served -> ValidationError
  good   -> ValidationError
  SERVED -> ValidationError
  ```

- **Fix**: coerce in `judge_outcome` — `verdict = str(payload.get("verdict", "")).strip().lower()`, then `if verdict not in get_args(Verdict): verdict, reason = "ungraded", f"judge returned unknown verdict {verdict!r}"`. Independently, pass `return_exceptions=True` to both `asyncio.gather` calls in `live_probes._main` and map an exception to an `ungraded` `Judgement`, so no future grader defect can take 230 results down with it.

---

## `explain` renders a turn twice when it has both audited calls and a job record

- **Severity**: medium
- **Location**: `src/chemclaw/cli/explain.py:164` (`_render`)
- **Trigger**: A session turn whose `session_messages` row is gone (pruned by `durable/retention.py`, or never written because the turn failed after its tools ran — both cases the docstring immediately above names) and which produced **both** an `audit_events` row and a `job_records` row. Launching a durable job is itself an audited tool call, so this is the ordinary shape of a durable turn whose transcript has been pruned.
- **Consequence**: the reconstruction prints `── turn <cid>` twice, with the job and every tool call duplicated under each. An auditor reading "why was this run?" sees two launches of a job that ran once, and two invocations of a tool that was called once. This is the tool whose whole purpose is to be evidence.
- **Evidence**:

  ```python
  shown = [*order, *(cid for cid in (*calls, *jobs) if cid not in set(order))]
  ```

  `(*calls, *jobs)` concatenates the key sequences of two dicts; a correlation id present in both and absent from `order` is emitted twice, and nothing de-duplicates before the render loop.

  `/tmp/claude-0/repro/explain_dup.py` (one correlation id, one `ToolCall`, one `Job`, empty `order`) printed the same five-line block twice:

  ```
  '── turn c-1'
  '   transcript: absent (compacted, pruned, or rolled back)'
  '   job calc:compute_reaction_energy — because: because asked'
  '       → dG = -33 kJ/mol'
  '   tool compute_reaction_energy [ok, 12 ms, alice] — because: user asked'
  ''
  '── turn c-1'          <- same turn again
  ...
  ```

- **Fix**: `shown = list(dict.fromkeys([*order, *calls, *jobs]))` — order-preserving, de-duplicating, and it also drops the `set(order)` rebuilt on every iteration of the generator.

---

## Phoenix scores "expected tools met" as 0.0 for probes that expected no tools

- **Severity**: medium
- **Location**: `src/chemclaw/evals/phoenix.py:186-191` (`_evaluations`)
- **Trigger**: Publishing any run over the shipped corpus. `ProbeOutcome.expected_tools_met` is `bool | None` and stays `None` whenever the probe declares no `expects_tools` (`evals/live.py:573`, `if probe.expects_tools:`). 55 of the 230 shipped probes declare none — 51 of them the whole bucket-C set, which exists precisely to be answered by an honest refusal with no tool call.
- **Consequence**: `1.0 if outcome.expected_tools_met else 0.0` maps `None` to **0.0**, so a quarter of the corpus is recorded in Phoenix as having *failed* a check it was never subject to. The module docstring says scores are numeric "because Phoenix aggregates a score across an experiment", so the aggregate a person opens Phoenix to compare two arms on has a hard ceiling of 175/230 = 76 % and is diluted by whichever subset each arm happened to cover. `cli/live_probes._summary:78-79` computes the same fact correctly (`expected = [o for o in outcomes if o.expected_tools_met is not None]`), so the two surfaces report different numbers for one signal — the two-readers-of-one-fact shape this repository names repeatedly.
- **Evidence**:

  ```
  $ uv run python -c '... load_probes() ...'
  total probes: 230
  probes with NO expects_tools: 55
  by bucket: {'A': 2, 'B': 2, 'C': 51}

  $ uv run python -c '... _evaluations(bucket-C outcome, None) ...'
  expected_tools_met field: None
  phoenix evaluation: {'name': 'expected_tools_met', 'annotator_kind': 'CODE',
                       'score': 0.0, 'label': 'none'}
  ```

- **Fix**: skip the evaluation entirely when the signal was not taken —
  `if outcome.expected_tools_met is not None: yield {... "score": 1.0 if outcome.expected_tools_met else 0.0 ...}`.
  An unasked question is not a failed one, and Phoenix's aggregate should be over the probes that asked it, exactly as `_summary` already does.

---

## `backfill_corpus` collapses two documents with identical text into one note, silently

- **Severity**: medium
- **Location**: `src/chemclaw/cli/backfill_corpus.py:36-51` (`note_for_document`), with `src/chemclaw/kg/pr_gate.py` branch naming (`note/<id>`)
- **Trigger**: A backfill directory containing two files whose extracted text is byte-identical but whose names differ — the same SOP filed under two project folders, a report and its `-copy` — which is the normal state of "a decade of existing reports, SOPs and filings" this module is written for.
- **Consequence**: the note id is `doc-{stable_hash(attachment.text)}`, derived from the text alone, while `source` and the first line of `body` both embed `path.name`. So the two documents produce one id with **two different bodies**, both proposed onto the same `note/doc-<hash>` branch: the second submission rewrites the first, and only the last-sorted filename survives in the graph. The command then prints `proposed 2 note(s)` for one note. The docstring's stated guarantee — "the PR-gate's byte-identical no-op then makes a repeat run genuinely free" — is also false for a *renamed* file, which is the case the id scheme was chosen for: the id is stable, the body is not, so a rename produces a rewrite rather than a no-op.
- **Evidence**:

  ```python
  return Note(
      id=f"doc-{stable_hash(attachment.text, chars=12)}",   # content only
      source=f"backfill:{path.name}",                        # name-dependent
      body=f"Backfilled from `{path.name}`.\n\n{attachment.text}",   # name-dependent
  )
  ```

  `propose_note` docstring: "The branch is always named `note/<id>`, so the reference stays stable across re-proposals". Same id ⇒ same branch ⇒ the second proposal's content wins.

- **Fix**: make the identity and the body agree. Either hash the content *and* nothing else (drop the filename from `source` and `body`, recording the paths as a `tags`/`source` list appended when a duplicate is seen), or hash the content plus the share-relative path so two copies are two notes. The first preserves the deduplication the docstring wants and makes the repeat run genuinely byte-identical; the second preserves provenance. Either way, count *distinct note ids* rather than files when printing the total.

---

## The soak/leak trend fits against sample position, not round number, so a dropped sample inflates the slope

- **Severity**: medium
- **Location**: `src/chemclaw/cli/soak_report.py:141-153` (`_series`) feeding `fit`/`describe` at `:60` and `:85`; same `fit`/`describe` reused by `src/chemclaw/cli/leak_probe.py:46`
- **Trigger**: Any soak round in which one sample is absent. `infra/live/soak.sh` makes every sample optional by construction — "Every sample is optional: a scrape that times out must cost its own field, never the round" — with `curl --max-time 5 ... || true` for `/metrics`, `ps -o rss= -p $(cat api.pid)` for RSS, and `psql ... 2>/dev/null || true` for the row counts. The RSS series is the most exposed of the three: family A of the storm restarts the API fifteen times per round, so `api.pid` being momentarily stale drops `api_rss_kb` for that round.
- **Consequence**: `_series` "drops rounds that lack it" and `fit` then regresses the surviving values against `range(n)` — their *position*, not their round. Every reported number is per-position and printed as per-round. Measured on a synthetic series that grows exactly 100 KB/round over rounds 0–9 with round 5 missing:

  ```
  no gap : slope 100.0   -> "grows and steady — first half +100.0, second half +100.0 KB/round (± 0.0)"
  one gap: slope 116.667 -> "grows and steady — first half +100.0, second half +120.0 KB/round (± 23.1)"
  ```

  A 17 % overstatement of the leak rate from one missing scrape, and the two-halves reading — the module's whole "leak versus warm-up" discriminator — now shows the second half 20 % steeper than the first when nothing accelerated. This is the module whose docstring rejects endpoint subtraction because "a threshold chosen before the noise is measured produces a confident answer at random"; the x-axis has the same problem.

- **Evidence**: `_series` returns only `out.append(float(cursor))` with no index; `fit` builds `xs = [float(i) for i in range(n)]`. Reproduction above run with the real `chemclaw.cli.soak_report.fit`/`describe`.
- **Fix**: have `_series` return `(round_number, value)` pairs and give `fit` the x values instead of synthesising them — `fit(xs, ys)` with `xs` from `row["round"]`. `leak_probe` already has real x values (`turns=[float(s.turns) for s in samples]`) and currently discards them for the same reason; passing them through fixes both callers and makes `leak_probe`'s "per turn" column and its verdict agree.

---

## `--sweep-repeats 1` makes the noise guard vacuous and lets the knee finder fabricate an answer

- **Severity**: medium
- **Location**: `src/chemclaw/cli/live_storm.py:1035` (`"spread"`), `:1112` (`noise`), `:1124` (`_knee`), `:1073-1087` (the "noise is small enough" finding)
- **Trigger**: `python -m chemclaw.cli.live_storm --sweep-repeats 1` (or any invocation where a cap yields one sample).
- **Consequence**: `spread = (max(samples) - min(samples)) / median(samples)` is identically `0.0` for a one-element list, so `noise(rows) == 0.0`. Two things follow. First, the finding *"the sweep's own noise is small enough to read a knee against"* passes with `0.0 <= 0.15` and reports `largest within-cap spread 0% over 1 sample(s)` — a measured-looking claim about a quantity nothing measured; the docstring calls this "the check that would have caught the first version", and at one repeat it cannot catch anything. Second, `_knee` compares each step against `lower["goodput"] * (1 + 0.0)`, i.e. strict improvement, and returns the first cap whose successor is even fractionally lower. The module's own docstring records that three single-sample runs of this sweep put the 8→16 step at +6.3 %, +3.9 % and +13.5 % — so with one sample the knee is decided by exactly the run-to-run scatter the ceiling exists to refuse, and reported as an answer. That is `_knee`'s own stated failure mode ("the failure mode is not a missing knee but a fabricated one") reachable through a supported flag.
- **Evidence**:

  ```python
  "spread": (max(samples) - min(samples)) / max(statistics.median(samples), 1e-9),
  ...
  def _knee(rows):
      floor = noise(rows)                 # 0.0 when every cap has one sample
      if floor > _MAX_READABLE_NOISE:     # 0.0 > 0.15 is False -> proceeds
          return None
      for lower, upper in zip(rows, rows[1:], strict=False):
          if upper["goodput"] < lower["goodput"] * (1 + floor):   # strict improvement
              return int(lower["cap"])
  ```

  With one sample, `max(samples) == min(samples)` by definition, so no configuration of the system under test can make this guard fire.

- **Fix**: a spread is undefined below two samples — make it so. Return `float("nan")`/`float("inf")` from the `spread` expression when `len(samples) < 2`, have `noise` propagate that, and let both the noise finding and `_knee` fail closed (`ok=False`, `None`) with `observed` saying "one sample per cap — the spread is unmeasured". Alternatively reject `--sweep-repeats < 2` at the parser, the same way `leak_probe._positive` rejects the batch size that makes its own loop degenerate.

---

## `_fragments` does not produce the number of fragments it is asked for

- **Severity**: low
- **Location**: `src/chemclaw/cli/mock_llm.py:244-250` (`_fragments`)
- **Trigger**: Any `ToolCall(fragments=N)` where `len(document) % N != 0`, or where `N > len(document)`.
- **Consequence**: the storm's streaming-shape family measures a different fragmentation than it declares. `c-fragmented` asks for 8 fragments of `{"text":"buchwald amination"}` (29 chars) and emits **10**; its behaviour text and the check name both say eight. A request for the "four hundred argument fragments" the mock's own docstring advertises as its reason to exist silently degrades to `len(document)` fragments — 29 for that payload — because `size = max(1, len // count)` floors to 1 and the count is never revisited. The docstring's "never losing a character" holds; "into `count` roughly equal pieces" does not.
- **Evidence**:

  ```
  asked 8 fragments of 29 chars -> got 10
  asked 3 fragments of 29 chars -> got 4
  asked 400 fragments of 29 chars -> got 29
  ```

  (run against the real `chemclaw.cli.mock_llm._fragments`)

- **Fix**: cut at computed boundaries rather than at a fixed stride —
  `n = min(count, len(document)); bounds = [len(document) * i // n for i in range(n + 1)]; return [document[a:b] for a, b in zip(bounds, bounds[1:])]`.
  That yields exactly `min(count, len)` pieces, loses no character, and lets a behaviour that wants more fragments than characters say so loudly (raise, or pad the document) instead of quietly getting fewer.

---

## A one-reference prompt written as a YAML literal block substitutes the raw value, not text

- **Severity**: low
- **Location**: `src/chemclaw/templates/resolve.py:32` (`_WHOLE`), used at `:81`
- **Trigger**: A template step whose whole prompt or argument is a single reference written with a literal block scalar:

  ```yaml
  - id: brief
    kind: agent
    prompt: |
      ${steps.hazards.result}
  ```

  YAML `|` keeps the trailing newline, and Python's `$` in `_WHOLE = re.compile(r"^\$\{...\}$")` matches *before* a trailing newline.

- **Consequence**: `resolve` takes the whole-string branch and returns the referenced value with its type, instead of the text interpolation the mixed-string branch would have produced. For a structured step result this hands a `dict`/`list` where the contract is a string: `AgentStepInput.prompt: str = Field(min_length=1)` then rejects it and the durable template run fails mid-sequence — after the earlier steps have already spent compute, which is the exact failure `make template-validate` exists to prevent and cannot see (the manifest validator only checks that references *resolve*, never how). For a string-valued result the newline is silently dropped instead, changing the rendered prompt. The shipped `hazard-briefing.yaml` uses `>-` throughout and is unaffected; the next template written with `|` is not.
- **Evidence**:

  ```
  prompt repr: '${steps.hazards.result}\n'
  resolved -> dict {'flags': ['peroxide'], 'n': 1}
  expected: an interpolated STRING, got a dict
  ```

  (`yaml.safe_load` of the block above, then the real `chemclaw.templates.resolve.resolve`)

- **Fix**: anchor at the true end of string — `_WHOLE = re.compile(r"\A\$\{(...)\}\Z")`. `\Z` has no newline exception, so a trailing newline correctly falls through to the interpolating branch. Worth a companion case in `tests/` using a `|` block, since the shipped corpus's uniform `>-` is what hides this.

---

## `check_result_cached` passes on any pre-existing row, not on this run's

- **Severity**: low
- **Location**: `src/chemclaw/cli/live_jobs.py:218-232` (`check_result_cached`)
- **Trigger**: Any second or later `make live-jobs` against a database that already holds an `xtb%` row — which is every run after the first, and every run on a lane whose storm has run family D.
- **Consequence**: the check named "the calculation landed in the Postgres cache" is `count(*) from calculation_results where calc_type like 'xtb%'` with no scoping to this run's workflow, species, temperature or time. It reports PASS on residue. That defeats the whole point of `_RUN_TEMPERATURE_K`, whose 100,000-value modulus was chosen (and documented at length two functions above) specifically so that "the *second* `make live-jobs` against the same database would start nothing, compute nothing, and pass every check against the first run's residue" could not happen — the workflow id and `job_records` checks were made run-specific and this one was not. A regression that stopped persisting results entirely would still show green here.
- **Evidence**: the query carries no run-scoped predicate, while its two siblings do (`check_job_recorded` filters `where job_id = %s`; `check_idempotent` diffs the count around a launch).
- **Fix**: take the count before `_launch` in `run_smoke` and pass it in, asserting `after > before` — the same before/after shape `check_idempotent` already uses, which is the only thing in this file that can distinguish a computation from a cache hit. Failing that, filter on `created_at >= <run start>` the way `family_d_durable` scopes `job_records` by `select now()`.

---

## What I checked and found sound

- `evals/metrics.py` arithmetic against the primary quantities: E-factor = (Σ inputs − product)/product and PMI = Σ inputs/product do satisfy the stated `PMI = E-factor + 1`; `bo_regret`'s sign follows the required `direction`; `precision_recall_f1`'s three degenerate cases are each defined and consistent with the docstring.
- Chemistry payloads in `cli/live_jobs.py`, `cli/storm_behaviours.py` and `cli/live_storm.py`: N₂ + 3 H₂ → 2 NH₃, CH₃OH + H₂ → CH₄ + H₂O and C₆H₆ + 3 H₂ → C₆H₁₂ are all atom-balanced as written in SMILES, and every rotational symmetry number (N₂ 2, H₂ 2, NH₃ 3, MeOH 1, CH₄ 12, H₂O 2, benzene 12, cyclohexane 6) is the textbook value.
- `soak_report.fit`: the OLS slope and its standard error, `sqrt((SSR/(n−2))/Sxx)`, are correct, and the `n < 4` / `Sxx == 0` / `n ≤ 2` guards all return before dividing.
- `templates/registry.run_workflow_id` and `connectors/jobs.job_workflow_id` agree: `stable_hash` canonicalises with `sort_keys=True`, so `live_jobs`'s independently-derived id matches the one the real launcher computes from the dumped params model (verified `ReactionJobSpec` adds no non-`None` default outside `SMOKE_PAYLOAD`).
- `templates/manifest`'s forward-reference validator genuinely walks `steps` in order and only admits `steps.<id>.result` already added to `available`.
- `evals/retrieval._RETRIEVAL_MEMO`: the key includes the corpus signature and stale entries are evicted on insert, so an on-disk edit is a miss rather than a stale hit.
- `evals/baseline.compare_to_baseline` / `detect_drift`: the band is symmetric, `vanished` is kept distinct from a real 0.0, and `is_worsening` reads the registered `Direction` rather than assuming one.
