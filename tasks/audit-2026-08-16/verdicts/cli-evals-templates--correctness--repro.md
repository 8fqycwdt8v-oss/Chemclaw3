# cli / evals / templates — CORRECTNESS · reproduction verdicts

Lens: **does it actually reproduce?** Scope: the two findings marked **high**. The seven medium/low
findings in the source file were not examined.

Working tree checked against `HEAD` before every measurement:
`diff pristine/src/chemclaw/cli/live_probes.py`, `.../evals/live_judge.py` → identical, and
`git status --porcelain` on the three touched files is clean. Every line number cited below is the
one in the current file. No source file was mutated during this verification.

---

## `--regrade` crashes on the `grades.json` its own run wrote

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**: wrote my own repro (`/tmp/v1/regrade.py`) rather than running the reporter's — it
  builds the on-disk state a real run leaves by calling the two *real* writers with real models
  (`load_probes()[0]`, one `<probe-id>.json` in exactly the shape `evals/live.run_probes` writes at
  `src/chemclaw/evals/live.py:633`, then the real `cli/live_probes._write_outputs`), and then calls
  the real `_load_transcripts`:

  ```
  $ uv run python /tmp/v1/regrade.py
  files: ['an-01.json', 'grades.json', 'summary.md']
  REGRADE FAILED: TypeError list indices must be integers or slices, not str
  ```

  Then the asymmetry the finding asserts, both readers over one directory
  (`/tmp/v1/asym.py`):

  ```
  phoenix.load_transcripts: OK, 1 transcript(s)
  live_probes._load_transcripts: TypeError: list indices must be integers or slices, not str
  ```

  I also re-derived reachability from the CLI rather than taking it on trust. `_main` at
  `src/chemclaw/cli/live_probes.py:361` reads `Path(args.transcript_dir or
  settings.live_probe_transcript_dir)`; the judged run at `:404` writes its outputs to the *same*
  expression; `run_probes` writes each `<probe-id>.json` into the same directory
  (`evals/live.py:621,633`). So the corpus run and `--regrade` are guaranteed to share a directory —
  this is not a configuration the operator has to choose badly.

- **Why**: reproduces exactly as filed, from source, with no scaffolding of the reporter's. The
  mechanism is `directory.glob("*.json")` with no exclusion list against a directory the same module
  deliberately writes `grades.json` into, and `judgement_from_transcript` doing `payload["probe"]` on
  a JSON *list* (`live_judge.py:196`). The failure is total and deterministic: `_load_transcripts`
  returns before any grading starts, so `--regrade` cannot re-grade a single probe after any judged
  run. `_NOT_TRANSCRIPTS` at `evals/phoenix.py:66` is a real, current, in-repo constant that solves
  exactly this for the second reader of the same directory, so the fix as filed is right and needs no
  new concept.

  Two things I checked that could have refuted it and did not: nothing filters upstream of the glob,
  and no test covers this path (`grep return_exceptions|_load_transcripts|regrade tests/` finds only
  unrelated docstrings and `tests/test_m12_probes.py:10`, which mentions `--regrade` in prose).

  Severity: I keep **high**. It is not data loss — the crash happens before any write — and the
  workaround (move one file) is one command. But the feature's entire reason to exist is recovering a
  grading defect without re-running the corpus, and it is broken 100% of the time on its only normal
  path, with a `TypeError` from `json` that names nothing an operator could act on.

---

## A single out-of-enum judge verdict discards every grade in the run

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**: drove the *real* `_main --regrade` end to end with a fake `anthropic` module
  injected into `sys.modules` before import (`judge_outcome` does `from anthropic import
  AsyncAnthropic` inside the function, so this substitutes the model and nothing else), five real
  probes from the shipped corpus, and one out-of-enum verdict among the five
  (`/tmp/v1/verdict_enum.py`):

  ```
  $ uv run python /tmp/v1/verdict_enum.py
    'Served'     -> ValidationError
    'SERVED'     -> ValidationError
    'good'       -> ValidationError
    'partial '   -> ValidationError
  RUN CRASHED: ValidationError 1 validation error for Judgement
  files after: ['an-01.json', 'an-02.json', 'an-03.json', 'an-04.json', 'an-05.json']
  ```

  Note the last line: no `grades.json`, no `summary.md`. The four *valid* grades and the report were
  discarded by one bad reply. (My first attempt printed 5/5 served — the fake client is constructed
  per call, so a per-instance counter never advanced; I moved the counter to module scope. Worth
  recording because it is the kind of scaffolding artefact that can manufacture either verdict.)

  I also confirmed there is nothing upstream that could constrain the verdict: `judge_outcome`
  (`live_judge.py:145-187`) sends a plain text prompt with no tool schema and no structured-output
  mode, `Judgement.verdict` is a bare `Literal` (`:42`, `:73`) which pydantic will not coerce, and the
  only `try/except` in the function wraps `json.loads`. Both `gather` calls (`live_probes.py:372`,
  `:399`) are bare. Line numbers `182-187` are current and correct.

- **Why**: the *mechanism* is real and I reproduced it, so this is not a refutation. Two parts of the
  finding do not hold, and together they take it below high.

  **The trigger is asserted, not shown.** The reporter's evidence proves only that
  `Judgement(verdict="Served")` raises — i.e. that a `Literal` is a `Literal`. It contains no
  instance, in the shipped transcripts or anywhere else, of the judge model actually emitting an
  out-of-enum verdict, and the system prompt (`live_judge.py:44-66`) enumerates the five words
  explicitly to a deliberately strong model. Contrast the two failure modes the module *does* handle:
  each is annotated with a measured incident (65 of 190 probes truncated; 40% false-positive
  fabrication rate). This one has no such number and I could not produce one without simulating the
  model.

  **The stated cost is inflated.** "The corpus run is not cheap to repeat … re-asking 190 live
  questions" does not follow: `run_probes` writes each probe's transcript *as it lands*
  (`evals/live.py:630-636`), before any grading begins, so a grading crash costs the judge calls
  only — one model call per probe — and `--regrade` over the same directory recovers them. In this
  case finding #1 does not even block that, because the crash means `grades.json` was never written.
  So the loss is the summary plus a cheap re-grade, not the live corpus.

  What the reporter *under*-weighted, and which is the stronger reason to take the `gather` half of
  the fix: the bare `asyncio.gather` makes **any** exception out of `judge_outcome` fatal to the whole
  run, and an `anthropic` `RateLimitError` / `APIStatusError` (529 overloaded) across ~230 calls at
  `live_probe_concurrency` is far more likely than a model inventing a sixth verdict word. That is
  the same defect with a trigger that is genuinely reachable without postulating model misbehaviour,
  and it is why I would still fix this — at medium, on that ground rather than on the enum.
