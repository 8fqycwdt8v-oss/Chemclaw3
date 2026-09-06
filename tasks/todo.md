# Multi-wave codebase review (2026-09-06)

Full re-review of the tree as if new. Prior review waves ignored by construction: every
agent gets the code, not the history, and every finding must be proved by running something.

## Wave 0 — baseline (done before any review)
- [x] Start the daemon: `sudo -n dockerd`, `make up`, `make db-migrate` (Postgres + Temporal live)
- [x] `make lint` green
- [x] `make type` green (804 files, mypy --strict)
- [x] `make test` green with Postgres up — record the skip count the epilogue reports

## Wave 1 — per-package correctness sweep (fan-out, find-only)
Ten agents, one package cluster each, reading for defects that change behaviour.
- [x] agent/ graph + middleware chain + budgets
- [x] agent/ sessions, authz, tools, skills, subagents
- [x] core/ + protocols/
- [x] ingest/
- [x] science/ + evals/
- [x] connectors/ + templates/
- [x] durable/ + operations/
- [x] api/ + deliver/
- [x] cli/ + publish/
- [x] kg/ + retrieval/ + memory/
- [x] Verify each finding adversarially, fix, `make lint type test`, PR, merge on green (#331)

## Wave 2 — cross-cutting invariants (fan-out, find-only)
Cuts that no per-package reader can see.
- [x] Concurrency and async correctness (event loops, pools, locks, cancellation)
- [x] SQL, migrations and data integrity (schema vs. reader, index vs. query, transactions)
- [x] Security: authorization chain, redaction, injection, credential handling
- [x] Resource lifecycle: leaks, unclosed clients, unbounded growth
- [x] Config/settings: readerless settings, magic numbers, chart vs. code drift
- [x] Tests that prove nothing (mocks asserting themselves, dead fixtures, absent arms)
- [x] Prose vs. code: CLAUDE.md / ADR / docstring claims falsified by the current commit
- [x] Verify, fix, `make lint type test`, PR, merge on green (#332)

## Wave 3 — deep semantic pass on what waves 1–2 disturbed
- [x] Re-review the fixed regions plus anything two waves both flagged
- [x] Sweep the surfaces the first two waves only read (small packages, validators, corpora, examples)
- [x] Verify, fix, `make lint type test`, PR (#333)

## Review

Three waves, ~2,300 lines of finding reports, **107 defects fixed** across 26 review and fix
agents. The suite was green before any of it (6,799 passed), so every one of them was a gap the
suite could not see. It is green after, at 6,989 passed / 6 skipped — and the skip count is the
other half of the story: it was 67, and 62 of those were the Helm chart tests, which had been
skipping because `helm` was not installed. They now run.

**The one pattern worth carrying forward, because it recurred in every wave:**
*a gate that has never been watched refusing is a claim that a gate exists.*
- Wave 1: `make template-validate` resolved the profile surface before profiles were loaded, so
  every shipped profile read as unknown and one of its three rules was unreachable. 61 tests
  passed over it, because the tests loaded profiles themselves.
- Wave 2: the egress guard's `connect` hook could be deleted with the whole egress corpus green.
  Every refusal assertion called the private decision function; the one socket-level test was
  refused by the patched `getaddrinfo` before `connect` was reached. One wrapper of nine observed.
- Wave 3: `make skill-validate` could not see a tool name in backticks — the way Markdown
  naturally names one — hiding 10 skill/profile pairs from agents that hold the tool.

The corollary is the method: **mutation testing is what finds this class.** Wave 2's mutation pass
killed ~25 guards it tried and could not break, which is what makes the six survivors credible.

**Second pattern: a fix is a hypothesis until a second reader runs it.** The fixers overturned
their own reviewers five times — the diff's named cost measured at 5% of it (the real lever was
elsewhere, 1.67 s → 0.18 s); a proposed redaction carve-out was measured to *stop* redacting
base32 credentials; a proposed cache bound was measured to make peak memory worse (+102 → +194 MB);
pre-creating tables would not have fixed the grant (`CREATE TABLE IF NOT EXISTS` checks the ACL
before existence); and a reported 1,846 ms loop block was 210 ms once measured on an idle box.

**Third: fixes collide.** Wave 3 existed to check that, and found three defects the earlier waves
introduced — including a capped turn answering `''` because wave 1's correct fix dropped the
partial answer the cap exists to preserve, hidden by a fake too generous to express the failure.

**Nine findings were declined rather than fixed**, each against a merged ADR or a measurement:
a running job has no owner by construction, the activity reading is ungated by decision,
retention-off is the argued default, `HandoffEvent` and `EffectSpec.compensation` are deliberate,
and `_SKILL_READ_LIMIT` was a leftover whose "missing control" reading measurement disproved.

**Left open, with reasons, in the fix reports:** the quadratic checkpoint growth (both candidate
fixes contradict merged decisions; the in-thread prune SQL is written down), the KG cache bound
(counterproductive, measured), the ELN quadratic re-read (needs a `limit` through an interface
outside the fixer's scope), and a narrower split-principal posture than the CREATE grant.
