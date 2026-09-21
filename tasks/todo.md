# Backlog implementation — waves

Working `docs/planning/BACKLOG.md` in waves. Each wave: implement, prove, fresh-context subagent
review, PR, merge on green. A closed row is **deleted** from `BACKLOG.md` in the commit that closes
it (the file's own rule).

## Wave 1 — merged as `b5c68498` (PR #429)

Five rows closed, one queued for what the change deliberately does not bound. Both reviews found
real defects in the first draft, the worst being that `PatternBudgetError` was swallowed by a
handler I had not checked, so the shipped behaviour was the `rows x budget` stall the class existed
to prevent — under a green test asserting the wrong non-membership. Lessons 103-107.

## Wave 2 — five rows

**Two of the ten rows worked so far were stale**, so each of these is re-checked against `HEAD`
before any code is written, which the backlog's own header asks for.

- [ ] **R6 — `ToolScopedSkills` is applied to neither stored tier** (§1). Verified live: both
  `agent/local_skills.py` and `agent/org_skills.py` admit the gap in their own module docstrings.
  The seam is already there — `validated_skill()` parses the frontmatter on both publish routes and
  discards `manifest.tools` / `manifest.requires`. Keep them beside the body; the `store` table is
  upstream's, so a sibling key in the JSON value rather than a migration.
- [ ] **R7 — a shared skill a narrowing hides leaves its name to the personal tier** (§1). Verified
  live: `skills/deep-research/SKILL.md` carries no `requires:`, so one `skill_role_gates` entry is
  enough. The write side uses the *discovered* basis (`shipped_skill_names()`); the invariant is
  the read side agreeing with it.
- [ ] **R8 — the turn-wide caps only bind where a watch is open, and two drivers open none** (§4).
  `api/runner.py` opens four; `durable/template_activities.py` opens one; the CLI opens none. Note
  the existing fan-out test opens the watch *itself*, so it measures the mechanism and not the
  wiring — that hole is part of the fix.
- [ ] **R9 — the substructure deadline test asserts a timing ratio where it means a record count**
  (§2). Bigger than it reads: the bounded arm *raises*, so the count has to survive the
  `TimeoutError`, and the two scan paths must agree. `tests/test_label_search.py` has the same
  proxy at a bar that already failed `main` on the molfp side — test-only there, since
  `_verify_within` already counts.
- [ ] **R10 — one parse budget serves two pods** (§3). The row's "four times the difference in
  room" is **~2x**, not 4x: solving the worker's own inequality gives 332.7 MiB against the shipped
  160, because it runs 8 activity slots to the front door's 2. Correct the row while closing it.
  The chart already has the precedent (`deployment-connectors.yaml` sets a per-Deployment `env:`
  for exactly this reason).

## Verification

`make lint` · `make type` · `make test`, each on its own line with its exit code read, and **the
gate is the call immediately before the commit** (lesson 107 — a fix prompted by one gate is
measured by another).

Infrastructure this environment now has, so these run as evidence rather than skipping: dockerd +
`make up` + migrations; `Chemclaw3-mcp` cloned with a built `.venv`; `helm`, `kubeconform`,
`promtool`; a baked tiktoken merge table at `/opt/tiktoken`.
