# Task: make the restructured repository consistent — the second, smaller pass

Requested 2026-07-31, as a follow-up to the restructure that merged as PR #51: *"The repo still is a
bit overwhelming. Is there a way to make it even more consistent or would I overshoot doing that
removing needed granularity?"* Branch: `claude/github-repo-structure-6yyibc`, restarted from
`origin/main` because #51 is merged. ADR: **D-154**.

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

- [x] Restart the branch from `origin/main`; reserve **D-154** in `docs/decisions/README.md` in the
      first commit, per `CLAUDE.md`.
- [ ] **Dissolve `src/chemclaw/mcp/`.** Engines → `science/fingerprints/{store.py,molfp/,rxnfp/}`;
      the two `FastMCP` instances → `connectors/{molfp,rxnfp}/server/tools.py`, the shape every other
      bundle already has. Carry the `mcp_servers/calc` cautionary history into
      `connectors/README.md`, and state the D-016 shadowing rule accurately in the ADR (a *top-level*
      `mcp/` shadows the SDK; `chemclaw.mcp` never did — the deleted README claimed the directory
      "cannot be named `mcp`" while being named `mcp`).
- [ ] **A README in every directory**, plus `tests/test_repo_map.py`: README coverage, and
      `ARCHITECTURE.md`'s tables matching the directories on disk in both directions.
- [ ] **Fold `evals/`, `templates/`, `profiles/` into `data/`** and repoint the five config defaults,
      `.env.example`, the Containerfile COPY set, `_RUNTIME_DATA`, and the Helm chart.
- [ ] **Archive the five finished `*-plan.md`**; repoint the eight code citations. Fix the stale
      `mcp_servers/` directory paths and `retrieval/__init__.py`'s docstring.
- [ ] **ADR D-154**, `ARCHITECTURE.md`, `CLAUDE.md`; verify; ship.

## Verification plan

`make lint type test`, `make cov`, all eight validators, `make eval`, and both workflows — `image`
is the only thing that proves the entrypoint dispatch and the COPY set, because it smokes every
component as UID 1001.

**`tests/test_repo_map.py` must be shown failing before it is trusted.** Last pass hit the same
failure mode twice: `test_image_ships_every_first_party_package` iterated an empty set and reported
green, and `make db-migrate` globbed a missing directory and applied zero migrations in silence. A
structural test that can find nothing has to be caught finding something.

## Review

_(filled in at the end)_
