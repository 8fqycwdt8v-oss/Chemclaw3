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

- [ ] 1. The tool renders a string; drop `exclude=True`; re-measure on the wire.
- [ ] 2. Hoist the two row-invariant subqueries out of the windowed subquery.
- [ ] 3. Pin the stringification shape in `tests/test_upstream_surface.py` (absence form).
- [ ] 4. Superseding ADR with the corrected table; ledger row.
- [ ] 5. BACKLOG row for the repo-wide repr payload; `tasks/lessons.md`.

## Review

_(pending)_
