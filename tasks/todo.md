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

**4. 66 docstring pointers name directories D-148 deleted.** The restructure renamed five packages
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

- [ ] Add the `D-149` row to `docs/decisions/README.md`, marked `RESERVED`, in the first commit

### Stage 1 — delete the Replit deployment surface (D-149)

- [ ] Delete `start.sh`, `start-temporal.sh`, `start-background-worker.sh`
- [ ] Delete `.bin/temporal` (138 MB LFS) and the `.gitattributes` rule that exists only for it
- [ ] Confirm no reference survives outside the append-only ADR record
- Acceptance: `git grep` finds the scripts only in `docs/decisions/D-091-*`; `make up` and the
  README's worker commands are the only documented way to start anything.

### Stage 2 — one name per signal (`agent/turn_signals.py`)

- [ ] Delete `set_job_sink`, `reset_job_sink`, `drain_started_jobs` (zero callers)
- [ ] Delete `announce_job_started`; point its three test call sites at `record_job_started` with
      the `kind` the wrapper was discarding
- Acceptance: `tests/test_runner.py` and `tests/test_service.py` pin the same behaviour, and the
  module exposes exactly one name per operation.

### Stage 3 — drop the unbuilt queue-routing seam (`connectors/queues.py`)

- [ ] Delete `task_queue_for` and `JobRuntime`
- [ ] Rewrite the module docstring to describe the seam that exists (one queue per bundle, derived
      from the bundle name) rather than the routing choice that does not
- Acceptance: `bundle_queue` is the module's whole surface, and the docstring's claims are
  checkable against it.

### Stage 4 — repair the 66 dangling docstring pointers, and guard them (D-149)

- [ ] Rewrite every backticked module pointer in `src/` and `tests/` to its post-D-148 location.
      Past-tense provenance sentences name the current file rather than being deleted — the old
      name stays in the ADR record, which is where history belongs.
- [ ] Add `tests/test_docstring_paths.py`: every backticked `<pkg>/<…>.py` in `src/` or `tests/`
      must resolve to a file that exists, under `src/chemclaw/`, `tests/`, or the repository root
- Acceptance: the new test fails on the tree as it stands today and passes after the rewrite —
  demonstrated, not asserted.

### Stage 5 — record and verify

- [ ] Write `docs/decisions/D-149-*.md`; swap the `RESERVED` marker for the real title and link
- [ ] `make lint type test` green
- [ ] CHECKMATE G1–G7

## Review

_(filled in at the end)_
