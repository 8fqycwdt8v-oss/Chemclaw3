# Task: make the restructured repository consistent — the second, smaller pass

Requested 2026-07-31, as a follow-up to the restructure that merged as PR #51: *"The repo still is a
bit overwhelming. Is there a way to make it even more consistent or would I overshoot doing that
removing needed granularity?"* Branch: `claude/github-repo-structure-6yyibc`, restarted from
`origin/main` because #51 is merged. ADR: **D-155**.

(The previous occupant of this file, the agentic-system review, is merged; its record is D-145 and
D-151…D-153, its findings the `REV-*` entries in `docs/planning/BACKLOG.md`.)

## The question, and why both halves of it get an answer

The user asked whether more consistency was available *or* whether chasing it would cost needed
granularity. Measured against `origin/main`, three real inconsistencies survive PR #51 — and four
plausible-looking ones would each destroy a distinction. Recording both is the point: the second
list is what keeps the next pass from "tidying" the architecture.

### The three that are real

1. **`src/chemclaw/mcp/` is a false duplicate.** `mcp/molfp` beside `connectors/molfp` looks exactly
   like `science/calc` beside `connectors/calc`, which *is* principled. Its own README concedes it:
   "moving the bodies into the bundles would be churn with no behavioural change". True when the
   only destination was `connectors/`; false since `science/` exists.
2. **Directory READMEs are the exception** — 5 of 14 subpackages, 4 of 12 root directories. GitHub
   renders a folder's README on click, which is the exact surface called messy.
3. **`evals/` and `templates/` exist twice**, once as code and once as root data.

### The four that would be over-shooting

- Merging `science/*` into `connectors/*` — puts Temporal imports inside the physics.
- Merging `memory/` into `retrieval/` — episodic layer vs. evidence harness; a level of depth for a
  word.
- Uniform file sets across connector bundles — the variance says which capabilities own durable work.
- Burying `knowledge/` and `skills/` under `data/` — they are layers 4 and 3; root position is their
  documentation. (User chose the exception deliberately.)

Splitting `core/config.py` (1726 lines) and `api/app.py` (1350) was also raised and declined: file
size is a different problem from repository structure, and mixing them makes the diff unreviewable.

## Steps

- [x] Restart the branch from `origin/main`; reserve **D-155** in `docs/decisions/README.md` in the
      first commit, per `CLAUDE.md`.
- [x] **Dissolve `src/chemclaw/mcp/`.** Engines → `science/fingerprints/{store.py,molfp/,rxnfp/}`;
      the two `FastMCP` instances → `connectors/{molfp,rxnfp}/server/tools.py`, the shape every other
      bundle already has. Carry the `mcp_servers/calc` cautionary history into
      `connectors/README.md`, and state the D-016 shadowing rule accurately in the ADR (a *top-level*
      `mcp/` shadows the SDK; `chemclaw.mcp` never did — the deleted README claimed the directory
      "cannot be named `mcp`" while being named `mcp`).
- [x] **A README in every directory**, plus `tests/test_repo_map.py`: README coverage, and
      `ARCHITECTURE.md`'s tables matching the directories on disk in both directions.
- [x] **Fold `evals/`, `templates/`, `profiles/` into `data/`** and repoint the five config defaults,
      `.env.example`, the Containerfile COPY set, `_RUNTIME_DATA`, and the Helm chart.
- [x] **Archive the five finished `*-plan.md`**; repoint the eight code citations. Fix the stale
      `mcp_servers/` directory paths and `retrieval/__init__.py`'s docstring.
- [x] **ADR D-155**, `ARCHITECTURE.md`, `CLAUDE.md`; verify; ship.

## Verification plan

`make lint type test`, `make cov`, all eight validators, `make eval`, and both workflows — `image`
is the only thing that proves the entrypoint dispatch and the COPY set, because it smokes every
component as UID 1001.

**`tests/test_repo_map.py` must be shown failing before it is trusted.** Last pass hit the same
failure mode twice: `test_image_ships_every_first_party_package` iterated an empty set and reported
green, and `make db-migrate` globbed a missing directory and applied zero migrations in silence. A
structural test that can find nothing has to be caught finding something.

## Review

### What the pass actually found

Three things the plan predicted, and three it did not.

**Predicted.** `chemclaw.mcp` dissolved along the `science/` ↔ `connectors/` line into
`science/fingerprints/` plus two `server/tools.py`; 17 READMEs written and enforced by
`tests/test_repo_map.py`; `evals/`, `templates/` and `profiles/` folded into `data/` (root: 13
directories → 10, both code/data name collisions gone).

**Not predicted, and each the same shape — prose asserting something no test reads:**

1. **`deploy/README.md` documented an `mcp-molfp`/`mcp-rxnfp` component** that `entrypoint.sh` has
   no case for and the chart has never declared. This is D-117 exactly, in the one file
   `tests/test_deploy_chart.py` does not read. Fixed; the bundles deploy as `connector-molfp` and
   `connector-rxnfp` like every other.
2. **A quotation of history had been rewritten by D-148.** `tests/test_deploy_chart.py` quotes the
   entrypoint line that kept `mcp-calc` routable — `python -m mcp_servers.calc.server`. The
   repository-wide `mcp_servers.…` rewrite caught the quotation too, so it said something the file
   never said. Restored.
3. **The deleted `mcp/README.md` insisted the directory "cannot be named `mcp`"** while sitting in a
   directory named `mcp`. Both halves were true — a *top-level* `mcp/` shadows the SDK, a submodule
   never could — and the rule outlived the condition that made it absolute.

### The one real breakage, and why it is the useful part

Moving `evals/` broke `tests/test_retrieval_eval.py`, which pinned the corpus at a hardcoded
`_REPO / "evals" / "retrieval_corpus"`. Every gold case then scored `0/2 expected sources
retrieved` — **which reads as a retrieval regression, not as a missing directory.** Same family as
D-148's silent `glob` over the moved migrations directory.

The fix is two-part and worth copying: derive the literal from the setting's own default so it
cannot drift again, *and* assert the directory exists, because an empty corpus and a wrong path
produce identical numbers.

### Verification

`make lint type test` green (419 files type-checked), all seven offline validators pass, `make eval`
scores the case-set with its three intended gate failures and no others — the 21 moved data files
are `R100` byte-identical renames, so the numbers cannot have moved.

**`tests/test_repo_map.py` was broken five ways and observed failing each time** before being
trusted: a missing subpackage README, a missing root README, an unmapped directory, a row for a
vanished directory, and a `.py` file beside `src/`. This is not ceremony — two tests in this
repository have already gone green by iterating an empty set.

### For next time

- A mechanical substitution cannot tell a claim about the present from a quotation of the past.
  Scope it to files the branch authored, or read the diff.
- A rule written as an absolute ("cannot be named X") should name its condition, or it will outlive
  it and be believed.
- When a structural test moves, break it on purpose before trusting the green.
