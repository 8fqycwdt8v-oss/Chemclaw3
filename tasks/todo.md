# The condenser's headline measurement was taken on a path production does not use

Plan: `/root/.claude/plans/as-if-you-ask-fuzzy-crab.md` (approved).
Base: `85a3c51`. Branch: `claude/condenser-wire-payload`.

## Measured

- [x] **`exclude=True` does nothing.** `_stringify` tries `json.dumps` (fails on a `BaseModel`) and
      falls back to `str()` = pydantic repr, which ignores `exclude`. Wire evidence from a compiled
      graph: `table='' rows=[] complete=True oversized=[] degraded=[]`.
- [x] **The ratio is 2.7x, not 9.1x** — 39,890 vs 14,611 tokens at N=80, against 6,352 claimed.
- [x] **Repo-wide**: `EvidenceSweep` reaches the model as repr too. Pre-existing.
- [x] **§4 confirmed** (was unverified): `EXPLAIN (ANALYZE)` shows `loops=400` on both
      `CITATION_SQL` and `_MODIFIED_SQL` to return **2** rows — the window forces per-row
      evaluation, and both subqueries are row-invariant.

## Steps

- [x] 1. The tool renders a string; drop `exclude=True`; re-measure on the wire.
- [x] 2. Hoist the two row-invariant subqueries out of the windowed subquery.
- [x] 3. Pin the stringification shape in `tests/test_upstream_surface.py` (absence form).
- [x] 4. Superseding ADR with the corrected table; ledger row.
- [x] 5. BACKLOG row for the repo-wide repr payload; `tasks/lessons.md`.

## Review

| what | before | after |
|---|---|---|
| wire payload at N=80 | 14,611 tokens (2.7x) | **6,368 (6.3x)** |
| `CITATION_SQL`/`_MODIFIED_SQL` | `loops=400`, 854 buffers, 3.257 ms | **0 per-row subplans, 56 buffers, 1.530 ms** |
| both backends' `modified_at` | 2026-03-04 vs 2026-01-01 | **agree** |

Two findings arrived *while fixing*, not from the plan:

- The `EXPLAIN` that confirmed the per-row subqueries also exposed a **backend disagreement** on
  `modified_at` that had been there since the reader landed — Postgres took `max` across copies as
  the rule states, the reference backend took the cited path's own time. The cross-backend test
  could not see it: one file row, no mtime.
- My first strengthened fixture for that gave the same row both the smallest path *and* the newest
  time, so it passed against either rule. The cited copy is now deliberately not the newest one.

That is the third and fourth time this session a fixture held constant the axis that broke. The
`lessons.md` entry says so plainly rather than filing it as a one-off.

Suite: 4,303 passed, 3 skipped (shallow git history; no Postgres skips). `make lint type` clean.
Six validators green.
