# Adversarial verification — `cli-evals-templates--correctness.md`

Lens: **is the trigger reachable, and is the consequence what is claimed?**
In scope: the two findings marked **high**. The eight medium/low findings were not examined.
No source file was mutated; `git status --porcelain src/` is empty after this work.

---

## `--regrade` crashes on the `grades.json` its own run wrote

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium

- **What I did**

  Traced the write and the read to the same directory first. `run_probes` writes transcripts to
  `Path(transcript_dir or settings.live_probe_transcript_dir)` (`evals/live.py:620-633`), and the
  corpus path calls `_write_outputs(Path(args.transcript_dir or settings.live_probe_transcript_dir), …)`
  (`cli/live_probes.py:404`) — the *same* expression, so `grades.json` lands among the transcripts by
  construction, not by operator error. `--regrade` then reads that same expression
  (`cli/live_probes.py:362`) and globs `*.json` with no filter (`:184`).

  Then ran the real functions over real shipped transcripts (`/tmp/rg/repro1.py`, `uv run`):

  ```
  regrade BEFORE any judged run: loaded 2 transcripts OK
  files after a judged run: ['an-01.json', 'an-02.json', 'grades.json', 'summary.md']
  REGRADE FAILED: TypeError list indices must be integers or slices, not str
  exit=3
  ```

  The script copies two committed transcripts, calls the real `_write_outputs`, then the real
  `_load_transcripts`. Nothing was stubbed.

  Checked what stands upstream, and nothing does. `summary.md` is not globbed; `evidence.json` is
  written only into the per-suite subdirectories by `_write_suite` and `glob` is non-recursive; so
  `grades.json` is the single poison file, and `_write_outputs` is the only thing that creates it —
  the same function the reader shares a directory expression with. The sibling reader in the same
  package does guard it: `evals/phoenix.py:66,113` filters on
  `_NOT_TRANSCRIPTS = {"grades.json", "evidence.json", "summary.md"}`. The finding's citation is accurate.

  Reachability from the outermost entry point: `make live-probes ARGS=--regrade` →
  `python -m chemclaw.cli.live_probes --regrade`, a first-class argparse flag whose help text is
  "re-grade stored transcripts without re-running any probe". No validator, no guard, no default
  stands in the way. `grep -rn "_load_transcripts" tests/` returns nothing — the function has no test
  at all, which is why this survived.

  One point in the reporter's favour they did not make: the shipped
  `tasks/live-test/transcripts/` is currently *clean* (194 files, all `<id>.json` plus a
  non-matching `durable-smoke.md`), because the historical `grades.json` sits in the **parent**
  from the era `_write_outputs`' own docstring describes as a bug it fixed. So the fix to
  `_write_outputs` is precisely what armed this: the first judged run against the default
  directory is what breaks `--regrade`, and it has not happened yet in this checkout.

- **Why**

  Deterministic, reachable from a documented flag with default settings, and the consequence is
  exactly as stated — `TypeError` out of `_load_transcripts` before any judge call is spent, and no
  way to proceed without a human moving `grades.json` aside.

  I downgrade the severity to medium only on impact, not on substance. The failure is loud (an
  immediate traceback), it destroys nothing (it aborts before `_write_outputs` and before any model
  call), it is an offline eval CLI rather than anything a chemist is shown, and the operator
  workaround is one `mv`. "A documented recovery path is 100 % broken" is a real defect and the fix
  the reporter proposes — import the existing `_NOT_TRANSCRIPTS` rather than write a second copy — is
  the right one; it is not a high because nothing is lost or misreported when it fires.

---

## A single out-of-enum judge verdict discards every grade in the run

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  **Mechanism — granted, and it reproduces.** `/tmp/rg/repro2.py`:

  ```
  'Served' -> ValidationError    'SERVED' -> ValidationError    'good' -> ValidationError
  'partial ' -> ValidationError  None -> ValidationError        ['served'] -> ValidationError
  'served' -> OK                 'ungraded' -> OK
  gather propagated: ValidationError -> results of the other 9 unreachable
  ```

  **End-to-end, driving the real `_main`.** `/tmp/rg/repro3.py` monkeypatches only
  `anthropic.AsyncAnthropic` (the client is constructed *inside* `judge_outcome`, so the counter has
  to be module-level — my first attempt got a fresh counter per call and reported a false negative)
  and runs `_main(Namespace(regrade=True, …))` over eight real transcripts with the 4th judge reply
  returning `"Served"`:

  ```
  CRASHED: ValidationError 1 validation error for Judgement
  dir after crash: ['an-01.json' … 'an-08.json']      <- no grades.json, no summary.md
  transcripts still present: 8
  ```

  So `_write_outputs` really never runs and the whole grading pass really is discarded. That half
  of the finding is right.

  **Reachability — measured, and it is 0/405.** The trigger is not something a caller can produce;
  it requires the judge model to deviate from a system prompt that enumerates the four verdicts
  verbatim. I measured the deviation rate two ways.

  Historical: `tasks/live-test/grades.json` holds 190 verdicts and
  `tasks/live-test/regrade-merged.json` another 190, distributions
  `{unserved 87, fabricated 69, served 23, partial 11}` and
  `{fabricated 62, served 56, partial 44, unserved 28}`. This is *not* survivor-biased evidence in
  the usual way: a single out-of-enum reply in any of those runs would have aborted the run and left
  no file at all, so the existence of the completed files is direct evidence of 380 consecutive
  in-enum replies.

  Live: `/tmp/rg/live_judge_probe.py` reissues the judge's real call — real `_SYSTEM`, real
  `_prompt`, real `settings.live_probe_judge_model` (`claude-sonnet-5`), 25 randomly sampled
  answered transcripts from the shipped corpus — and inspects the *raw* `verdict` string before any
  pydantic validation:

  ```
  model: claude-sonnet-5 | sampling 25 answered transcripts
  in-enum 25 / out-of-enum 0
  ```

  405 real judge calls, zero out-of-enum. That does not prove impossibility — it is a sampled model,
  not a validator — but the finding presents `"Served"`/`"good"`/`"partial "` as things that happen,
  and offers no instance of one. Nothing in the evidence section is a judge reply; all three lines
  are the reporter constructing `Judgement(verdict=…)` by hand, which is testing pydantic, not
  testing the judge.

  **Consequence — the stated cost is not the real cost.** The finding leans on
  "the corpus run is not cheap to repeat" and quotes the docstring about "re-asking 190 live
  questions". That is not what is lost. `run_probes` writes one transcript per probe *as it lands*
  (`evals/live.py:630-640`), for the reason its own docstring gives, and the entire grading block
  runs only after `run_probes` has returned — so at the moment of the crash all 230 transcripts are
  already on disk, as my repro3 output shows. Nothing needs re-asking. The recovery is `--regrade`,
  whose cost is N judge calls, not N live agent turns, and which on a *first* judged run works
  (the crash means no `grades.json` was written, so finding 1 does not bite). What is genuinely lost
  is one grading pass plus the mechanical summary, both regenerable from disk.

- **Why**

  I grant the mechanism — `payload.get("verdict", "ungraded")` is untrusted model output fed
  straight into a `Literal` field, and `asyncio.gather` without `return_exceptions` at
  `cli/live_probes.py:372` and `:399` really does make one bad reply fatal to all of them. Both
  fixes the reporter proposes are correct and cheap, and the coercion one is the more valuable of
  the two because it generalises past this field.

  But the finding is filed as **high** on two claims that do not hold up. The trigger is a
  stochastic model deviation with a measured rate of 0 in 405 real calls under this exact prompt and
  model, not a reachable input; and the consequence is a lost grading pass recoverable from
  transcripts already on disk, not a lost 230-probe live run. Strip the exaggeration and what is
  left is defensive-coding debt in an offline eval CLI that fails loudly and loses no evidence —
  low.

  Two things the reporter missed, for completeness, neither of which raises it: the same
  `gather` fragility swallows a `fabricated_claims` payload that is a non-iterable JSON scalar
  (`[str(c) for c in 3]` → `TypeError`), by the identical path; and there is a genuine
  *compounding* case with finding 1 — after any earlier judged run against the default directory,
  the `--regrade` recovery this finding relies on is itself broken by the stale `grades.json`. That
  is finding 1's harm, already counted there.
