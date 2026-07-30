# D-149 — What two finished migrations left behind, and the guard for the kind that rots silently

A cleanup pass, asked for in the general form: *"check the current codebase for opportunities for
simplification, removing of doublings or things not needed anymore."* The interesting result is how
little there was of the kind that question usually finds, and what there was instead.

## The survey came back clean, which is itself the finding

Four mechanical scans over `src/` and `tests/`, so the answer would be a measurement rather than an
impression:

| Scan | Result |
| --- | --- |
| Top-level defs/classes with no reference anywhere (AST + whole-repo grep, decorator-registered symbols excluded) | **3** |
| Public methods with no reference anywhere | **0** |
| `Settings` fields nothing reads (checked against every text file, including `CHEMCLAW_*` env spellings) | **0** |
| Function bodies identical after name normalisation | **1**, a `Protocol`'s in-memory and Postgres backends agreeing as intended |

There is no dead-code layer here and no duplication layer, so this ADR does not invent one.
Deleting things for being *old* rather than *unreferenced* is how a cleanup becomes churn, and a
codebase whose scans come back at zero has earned not being rearranged.

What the scans did surface is narrower: residue from two migrations that were each finished except
for a last mile nothing was watching.

## 1. The Replit deployment surface outlived the Replit deployment

D-091 restored the tree the Replit restructure rewound, and listed six Replit-only additions it
deliberately did **not** revert — correctly, since the overlay it used could not touch what it did
not contain. D-146 then deleted `services/chemclaw/`, calling it "the last remnant of a Replit
restructure". It was not quite: three of those six additions were still sitting at the repository
root, and one of them is the largest file in the repository.

- `start.sh` and `start-background-worker.sh` bridge `AI_INTEGRATIONS_ANTHROPIC_*` into an SDK
  convention this system no longer uses — the LLM provider has been config-selected since D-039 —
  and hard-code `$SCRIPT_DIR/.venv/bin/python`.
- `start-temporal.sh` executes `.bin/temporal`, a **138 MB Git-LFS binary** every clone pays for.
  `make up` brings Temporal up from `infra/docker-compose.yml`, which is what `README.md` and
  `docs/guides/runbook.md` have documented throughout.
- `.gitattributes` existed solely to carry that binary's LFS rule, so it goes with it.

Nothing referenced any of them: not CI, not the Makefile, not `deploy/`. The only surviving mentions
are in the ADR record, which is append-only and where they belong.

The pattern worth extracting: **D-091 was right to defer, and deferral needs an owner.** "Left in
place because this change cannot safely touch it" is a correct call that silently becomes "left in
place because nobody looked again". The three files survived a *second* restructure whose stated
purpose was removing exactly this.

## 2. A merge left four names where one belongs

`agents/job_events.py` — a fourth Replit-only addition — was folded into `agent/turn_signals.py`,
and the fold was right: two contextvar sinks drained separately leave the relative order of a
launched job and a proposed note undefined, which is precisely what a transcript must get right.
But its four caller-facing names were kept beside the canonical ones, each documented as "the name
main's callers already use".

Those callers never arrived. `set_job_sink`, `reset_job_sink` and `drain_started_jobs` have had zero
callers since the day they were written. `announce_job_started` had only test callers, and was
actively lossy: it hard-coded `kind="job"`, discarding the field `JobSignal` exists to carry, so the
three tests that used it were exercising a shape no production caller produces. They now name the
kind, which is both more honest and what the real callers do.

A compatibility alias is a promise that something will migrate. When nothing does, the alias is not
compatibility — it is a second name for one thing, which is the cost DRY is about.

## 3. `connectors/queues.py` documented a seam it did not have

`task_queue_for` and the `JobRuntime` literal had no callers; only `bundle_queue` did. The module
docstring nonetheless explained a `bundle`/`background` routing choice at length and asserted of
`JobRuntime` that it had "two members, both with a real caller" — false when written or shortly
after, and unfalsifiable by any tool the gate runs.

Removing the dead half exposed a live gap it had been hiding. Every `connector.yaml` spells out a
`task_queue` per job that must equal `bundle_queue(connector)`; all eight do; **nothing checks that
they do**. A typo there is a job that starts successfully and then sits forever in a queue nobody
polls — the exact failure the docstring claimed to have designed out.

That is not fixed here, deliberately. The two ways to close it — validate the declaration, or delete
the field and derive it — differ in whether a connector-declared job may *ever* run on core's
`background-jobs` worker, which is the escape hatch `JobSpec`'s docstring advertises and the one
`task_queue_for` was built for. Settling that inside a cleanup pass would be deciding a capability
question by side effect. It is written up in `docs/planning/BACKLOG.md` with both options and a
trigger.

## 4. Sixty-six of the codebase's own signposts pointed at demolished buildings

This is the substantial one, and the only one with a lasting fix.

Prose is how this repository navigates. A module opens by naming the two or three modules a reader
must hold alongside it. D-148 renamed five packages and moved modules between them; the ~1200
imports were carried along by tools that understand imports, and the pointers inside docstrings —
which are not imports — were not carried by anything. **78 dangling pointers across 51 files**
survived, naming `workflows/`, `workers/`, `service/` and `agents/`. Two had been actively corrupted
by a blind substitution into `…/chemclaw.durable.py`, a filename that has never existed.

So `durable/artifact_eviction.py` opened by citing `workflows/retention.py`, the pruner whose
boundary it exists to respect. `agent/turn_signals.py` cited `service/events.py` for the event types
it feeds. Every one of them is wrong in the first place a reader looks, and the whole tree was green
throughout: `mypy` cannot see prose, and neither can `ruff`.

**The repair is not the point; the guard is.** These pointers were correct when written and rotted
in a rename that touched none of them. Repairing them alone buys one clean tree and the identical
rot at the next restructure. `tests/test_docstring_paths.py` asserts that every backticked path
ending in `.py`, in `src/` or `tests/`, resolves to a file that exists. It was confirmed to fail on
the pre-repair tree — 51 failing files — before being made to pass.

Three scope decisions inside it, each with a reason:

- **Backticks are the trigger.** They are this repository's own marker for "this is a name, not
  English", which keeps the rule precise rather than heuristic — the same reasoning
  `cli/validate_prose_contract.py` uses for requiring an underscore.
- **`docs/decisions/` is out of scope.** The ADR record is append-only and its stale paths are
  *accurate about the past*; `docs/README.md` already states this as policy.
- **Past-tense sentences name the current file anyway.** "They used to live in
  `connectors/calc/specs.py`" reads oddly for a second and is still right: the sentence exists to
  tell a reader where the code is, and the old name is preserved in the ADR that moved it. An
  exemption for prose that merely *sounds* historical is a hole wide enough for the next restructure
  to walk through.

A file that was **deleted** rather than moved is the one case with no current name to point at, and
naming it is often the whole point of the sentence — `agent/durable_tools.py` exists partly to
explain the absence of `agents/job_status.py`. Those live in a four-entry `_REMOVED` allowlist, the
same explicit-and-short shape `validate_prose_contract.py` uses, so that adding one costs a review
conversation.

The check also asserts its own corpus is non-trivial. That is D-148's post-mortem lesson applied
directly: a rename's characteristic failure is a test that iterates a now-empty set and reports
green while asserting nothing, and no type checker or linter can see it happen.

## Why this belongs in the record

Because the general lesson is not "delete dead code". It is that **this repository guards its
declarations against the live surface in seven places** — `kg-validate`, `skill-validate`,
`connector-validate`, `template-validate`, `prose-validate`, `eln-validate`, `audit-verify` — and
every class of statement that lacks such a guard has drifted. The prose pointers drifted through
D-148. The manifest's `task_queue` has not drifted yet and can. The pattern to reach for on finding
stale prose is not a careful rewrite; it is asking what would have caught it, and then adding that.
