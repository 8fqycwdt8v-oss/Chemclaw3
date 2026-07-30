# Task: make the codebase as clean and simple as possible

Requested 2026-07-30: "Check the current code base for opportunities for simplification,
removing of doublings or things not needed anymore."

Branch: `claude/codebase-cleanup-y7osy3`. One ADR: D-149.

## What the survey found

The survey was mechanical, not impressionistic — four scans over `src/` and `tests/`:

| Scan | Result |
| --- | --- |
| Unreferenced top-level defs/classes (AST, whole-repo grep) | **3 real hits**, all merge residue |
| Unreferenced public methods (AST, decorator-aware) | **0** |
| Unread `Settings` fields (AST vs. every text file, incl. `CHEMCLAW_*` env spellings) | **0** |
| Identical / name-normalised duplicate function bodies | **1**, a legitimate `Protocol` pair |

So there is no dead-code layer and no duplication layer to remove: the ordinary cleanup targets are
already clean, and inventing work there would be churn. What the scans *did* surface is narrower and
real — residue from two migrations that were each completed except for their last mile.

**1. The Replit deployment surface outlived the Replit deployment.** D-091 restored the tree the
Replit restructure rewound, and listed six Replit-only additions it deliberately did not revert
(the overlay could not touch what it did not contain). D-146 then deleted `services/chemclaw/` —
"the last remnant of a Replit restructure". Three of those six are still at the repository root,
referenced by nothing: `start.sh`, `start-temporal.sh`, `start-background-worker.sh`, plus the
138 MB Git-LFS `.bin/temporal` binary the second one runs. They bridge `AI_INTEGRATIONS_ANTHROPIC_*`
into an SDK convention this system no longer uses (the provider is config-selected, D-039) and
hard-code `$SCRIPT_DIR/.venv/bin/python`. `README.md` and `docs/guides/runbook.md` document
`make up` + `python -m chemclaw…` instead; CI touches none of them.

**2. A merge left four aliases where one name belongs.** `agents/job_events.py` — a fourth
Replit-only addition — was consolidated into `agent/turn_signals.py`, correctly, but its four
caller-facing names were kept beside the canonical ones "as the name main's callers already use".
Three have had zero callers ever since; the fourth has only test callers and is lossy — it
hard-codes `kind="job"`, discarding the field the signal exists to carry.

**3. `connectors/queues.py` documents a seam it does not have.** `task_queue_for` and the
`JobRuntime` literal have no callers; only `bundle_queue` does. The module docstring nonetheless
explains a `bundle`/`background` routing choice and asserts "Two members, both with a real caller",
which is false.

**4. 78 docstring pointers name directories D-148 deleted.** The restructure renamed five packages
(`agents/`→`agent/`, `service/`→`api/`, `workflows/`+`workers/`→`durable/`, `calc/`→`science/calc/`)
and moved modules across them. The prose that navigates a reader between modules was not carried
along: `durable/artifact_eviction.py` opens by citing `workflows/retention.py`,
`agent/turn_signals.py` cites `service/events.py`, and so on — dangling pointers in the one place a
reader looks first. This repository guards seven other declarations against the live surface
(`kg-validate`, `skill-validate`, `connector-validate`, `template-validate`, `prose-validate`,
`eln-validate`, `audit-verify`); this class had no guard, which is why it drifted through a rename
in silence.

## Plan

### Stage 0 — reserve the ADR number

- [x] Add the `D-149` row to `docs/decisions/README.md`, marked `RESERVED`, in the first commit

### Stage 1 — delete the Replit deployment surface (D-149)

- [x] Delete `start.sh`, `start-temporal.sh`, `start-background-worker.sh`
- [x] Delete `.bin/temporal` (138 MB LFS) and the `.gitattributes` rule that exists only for it
- [x] Confirm no reference survives outside the append-only ADR record
- Acceptance: `git grep` finds the scripts only in `docs/decisions/D-091-*`; `make up` and the
  README's worker commands are the only documented way to start anything.

### Stage 2 — one name per signal (`agent/turn_signals.py`)

- [x] Delete `set_job_sink`, `reset_job_sink`, `drain_started_jobs` (zero callers)
- [x] Delete `announce_job_started`; point its three test call sites at `record_job_started` with
      the `kind` the wrapper was discarding
- Acceptance: `tests/test_runner.py` and `tests/test_service.py` pin the same behaviour, and the
  module exposes exactly one name per operation.

### Stage 3 — drop the unbuilt queue-routing seam (`connectors/queues.py`)

- [x] Delete `task_queue_for` and `JobRuntime`
- [x] Rewrite the module docstring to describe the seam that exists (one queue per bundle, derived
      from the bundle name) rather than the routing choice that does not
- Acceptance: `bundle_queue` is the module's whole surface, and the docstring's claims are
  checkable against it.

### Stage 4 — repair the 78 dangling docstring pointers, and guard them (D-149)

- [x] Rewrite every backticked module pointer in `src/` and `tests/` to its post-D-148 location.
      Past-tense provenance sentences name the current file rather than being deleted — the old
      name stays in the ADR record, which is where history belongs.
- [x] Add `tests/test_docstring_paths.py`: every backticked `<pkg>/<…>.py` in `src/` or `tests/`
      must resolve to a file that exists, under `src/chemclaw/`, `tests/`, or the repository root
- Acceptance: the new test fails on the tree as it stands today and passes after the rewrite —
  demonstrated, not asserted.

### Stage 5 — record and verify

- [x] Write `docs/decisions/D-149-*.md`; swap the `RESERVED` marker for the real title and link
- [x] `make lint type test` green
- [x] CHECKMATE G1–G7

## Review

**What shipped**, in five commits on `claude/codebase-cleanup-y7osy3`: 138 MB of Git-LFS binary and
three shell scripts belonging to a deployment target that no longer exists; four alias functions a
merge left behind; a two-symbol routing seam that was documented but never built; and 78 docstring
pointers repaired and pinned by a new test. `docs/decisions/D-149-*.md` has the reasoning.

**Verification.** `make lint type test` green: **2006 passed, 76 skipped**, which is exactly the
1591-test baseline taken before the first commit plus the new guard's 415 parametrized cases — so no
test was dropped or made vacuous by the edits. All seven declaration validators still pass, `mypy
--strict` reports no issues over 414 source files, and `tests/test_decision_log.py` accepts the
D-149 ledger row.

**The bar was "is this residue, or is it merely old?"** Every deletion here is something whose
*reason to exist* is gone — a Replit runner with no Replit, a compatibility alias with nothing to be
compatible with, a `runtime:` switch no manifest ever set. Nothing was removed for being
unfashionable and nothing working was restructured, which is why the survey table above is in the
plan at all: when the scans come back at zero, the honest output is a short change, not a long one.

**The one piece worth more than the deletions.** Stage 4's real output is not the 78 repaired
pointers — it is `tests/test_docstring_paths.py`. Those pointers were correct when written and
rotted in a rename that touched none of them; repairing them without a guard buys one clean tree and
the identical rot at the next restructure. The test was confirmed to fail on the pre-repair tree (51
failing files) before it was made to pass, and it asserts its own corpus is non-empty — the failure
mode D-148's post-mortem named, where a rename leaves a test iterating nothing and reporting green.

## Follow-up: the open finding, closed (D-150)

Asked to fix it. The choice looked like validate-the-field versus derive-it, where deriving
forecloses routing a connector job onto core's `background-jobs` worker. Following the dispatch path
showed that hatch cannot open: core's background worker serves `registered_workflows("background")`,
populated at import time by modules it imports, and it never imports a bundle — so a job declaring
`background-jobs` would start cleanly and then wait forever. The field could hold exactly one
correct value and any number of unrunnable ones, which also retires the validator option: a check
asserting a field equals its own derivation proves the field carries no information.

- [x] Remove `JobSpec.task_queue`; derive at both dispatch sites (`connectors/jobs.py`,
      `durable/template_activities.py`)
- [x] Strip the eight declarations from four manifests and the fixtures in five test modules
- [x] Drop the two now-vacuous assertions in `test_workers.py`; pin the derivation where it matters
      — `test_connector_jobs.py` asserts `payload.task_queue == "connector-calc"` against a manifest
      that no longer contains that string
- [x] `ConnectorJobInput`/`ResolvedJob` left alone: they carry the resolved value across a workflow
      boundary, and narrowing a durable input model buys nothing here
- [x] ADR D-150; BACKLOG entry closed; gate green at the same 2006 passed / 76 skipped

**The refinement to D-149's closing line.** It said the move on finding an unguarded statement is to
ask what would have caught it and add that. Incomplete: ask *first* whether the statement needs to
exist. Here the guard was the wrong instinct and would have locked in the redundancy while looking
like a fix.

**The original finding, as it was left before that.** Deleting the dead half of
`connectors/queues.py` exposed a live gap it was hiding: every `connector.yaml` declares a
`task_queue` that must equal `bundle_queue(connector)`, all eight agree, and nothing checks it.
Closing it means choosing whether a connector job may ever run on core's worker — a capability
decision, not a cleanup — so it went to `docs/planning/BACKLOG.md` with both options and a trigger
rather than being settled here by side effect.

**Also considered, and not done.** `docs/planning/` holds five completed build plans
(`backlog-plan`, `connector-plan`, `foundation-plan`, `gap-closure-plan`, `parity-plan`) that read
as archive material, but `docs/README.md` declares that directory maintained *including* the build
plans — moving them is a documentation-policy change and belongs in its own request. The bare
`calc/…` pointers inside `science/calc/` were rewritten like every other rather than tolerated as
sibling-relative shorthand: two spellings of one path is exactly the ambiguity the guard exists to
remove.
