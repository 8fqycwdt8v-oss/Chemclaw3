# D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks — GxP framing and the audit hash chain are removed; the trail, the gates and the INSERT-only grant stay

**Status:** accepted · **Date:** 2026-08-14 · Supersedes the framing of `D-027`, `D-055` and `D-122`; retires the chain built by `D-034`/`D-061`, versioned by `D-2026-07-31-the-audit-chain-is-versioned` and anchored by `D-2026-08-01-a-restore-is-a-truncation-nobody-can-see`; leaves `D-2026-08-05-append-only-by-grant-not-by-contract` standing as the whole of what remains.

## Context

Two different things wore the same word.

The first was **vocabulary**: 184 occurrences of "GxP", "21 CFR Part 11", "ALCOA", "GAMP" and
"computerized system validation" across ~110 files, almost all of them in docstrings, config
comments, SQL headers and package READMEs. They were doing rhetorical work — "in a GxP system, X is
worse than Y" is a way of saying an argument is serious — and the mechanisms they justified are
ordinary engineering: record every tool call once, stamp who asked, route agent-authored notes
through a human review, keep provenance on a computed number.

The second was **one real control**: a tamper-evident hash chain over `audit_events`
(`infra/sql/011`), signed high-water anchors over the chain (`032`), a paging verifier, a Temporal
Schedule that ran it, a CLI, `make audit-verify`, five settings and an HMAC secret. That was built
to make tampering cryptographically detectable, which is a thing a *regulated* deployment needs.

The system is not one, and it already said so. `data/evals/probes/knowledge.yaml` kn-29 grades the
agent on **refusing** to claim validated status; `reporting.yaml` rp-13 forbids claiming 21 CFR
Part 11 compliance. So the tree simultaneously disclaimed the posture and described itself in its
vocabulary — and shipped the machinery for it.

The cost of keeping the chain was not theoretical:

- **A serializing advisory lock on every audit write.** `PostgresAuditSink.record` took
  `pg_advisory_xact_lock`, read the chain tip, hashed onto it and inserted. Three round trips and a
  global lock, on the hot path of every tool call, so that rows could be linked.
- **A key to manage**, without which the anchors did nothing — and the anchors were the only thing
  that could see a trailing truncation, which is the alteration a point-in-time restore performs.
- **`audit_events` could never be pruned.** `durable/retention.py` refused the table *because*
  deleting from a chain is indistinguishable from tampering. The refusal was right; the reason was
  not the real one.
- **Every widening of `AuditEvent` was a schema-versioning exercise.** Migrations 026 and 044 each
  spend a screen of comment on `chain_version` and frozen field tuples, because adding a column
  changes the bytes every historical row should hash to.

## Decision

**1. The regulatory vocabulary goes, everywhere outside the frozen records.** "The GxP audit trail"
becomes "the tool-audit trail"; "the GxP line" becomes "the review line"; `"AI proposes, human signs
off"` becomes `"the agent proposes, a human decides"`. Where the sentence was making a real
argument, the argument stays and is restated on its own terms — *this is the only record of who ran
that tool* is a better reason than *a regulation requires it*, and it is true.

`docs/decisions/` and `docs/archive/` are not rewritten. A merged ADR is never edited (CLAUDE.md) and
an archived document is a record of what was true then; this ADR is what supersedes them.

**2. The hash chain and the anchors are removed.** Deleted: `agent/audit_anchor.py`,
`durable/audit_chain.py`, `durable/audit_verify.py`, `cli/verify_audit_chain.py`, their three test
modules, the `audit-verify` Schedule and Make target, `audit_verify_*`/`audit_anchor_secret`, and the
chart's `CHEMCLAW_AUDIT_VERIFY_ENABLED` and `auditAnchorSecret`. `PostgresAuditSink` keeps its table
and loses the lock, the tip read and three columns from its INSERT.

**3. The INSERT-only grant is now the whole of the integrity claim, and is stated as such.**
`grants/app_privileges.sql` gives the application role `INSERT` on `audit_events` and neither
`UPDATE` nor `DELETE`. The chain *detected* rewriting; the grant *prevents* it, and always did — the
chain was the weaker half of the pair. `tests/test_database_privileges.py` pins it in both
directions.

What the system can no longer claim, and now says out loud in kn-23: there is no cryptographic proof
that a row was never edited. A database owner can still change one. What is on offer is a controlled
record, not tamper-evidence.

**4. Everything else keeps working, and only its wording changes.** The audit trail, the PR-gate, the
plan-approval gate, the interaction-approval holds, bi-temporal note validity and `memory/supersede`,
and the retention tier in `agent/leaver.py` are all untouched mechanisms. Two were considered for
relaxation and deliberately kept:

- **Bi-temporal validity.** `valid_from`/`valid_to`/`is_current` filter five read paths so a
  superseded note is not served as current evidence. That is retrieval correctness that happened to
  be *described* in GxP terms; removing it would be a regression, not a relaxation.
- **The erasure retention tier.** `audit_events` stays undeletable on actor erasure. Its stated
  reason had to be rewritten regardless — it cited the hash chain — and the honest one is that for a
  tool call which changed nothing durable, the trail is the only place it is recorded at all.

## Consequences

**No migration, and none was possible.** `tests/test_migrations_are_additive.py` refuses every
destructive statement with no exemption, and checksums merged migrations so their statements cannot
change. `prev_hash`/`row_hash` default to `''` and `chain_version` to `1`, so the writer simply stops
supplying them and every insert keeps working against an already-migrated database. Migrations 011
and 032 stay on disk with `RETIRED` headers; `audit_anchors` stays as an empty table. The columns are
dead weight in the schema, and that is the price of a forward-only schema — paid knowingly, recorded
here so the next reader does not mistake them for live.

**`"audit-verify"` stays in `OWNED_SCHEDULE_IDS`, marked retired.** That frozenset is the prune
namespace: it is the only thing authorising the applier to *delete* a Schedule. Removing the name
would strand a live `audit-verify` Schedule on an existing Temporal namespace, firing a workflow no
worker registers, forever. It can be dropped once every deployment has run one apply.

**A grant outlived its writer, and the suite caught it.** The first version of this change left
`GRANT INSERT ON audit_events, audit_anchors` alone on the argument that a privilege on an empty
table costs nothing. `tests/test_database_privileges.py::test_the_grant_matches_the_writes_the_code_actually_performs`
derives the grant matrix from the SQL the code actually issues and fails in **both** directions, so
deleting `agent/audit_anchor.py` made `audit_anchors: ['INSERT']` a grant nobody exercises — which
is the test's own phrasing for the hazard: *a privilege nobody uses is a privilege that only matters
when someone else uses it*. `audit_anchors` now carries no grant at all, and the "no grant" is
asserted rather than merely dropped, so one silently reappearing fails a test.

**`audit_events` becomes prunable in principle**, and is still refused in practice —
`durable/retention.py` now argues the refusal from what the record is for rather than from the chain,
and `tests/test_retention.py` keeps the guard so the removal is not later read as permission.

**The eval contract moved with the code.** kn-23, kn-27 and kn-29 described the chain and
`make audit-verify` in their expected answers; they now describe the trail, the grant and the limit.
The `forbids_claims` guardrails that mention GxP and 21 CFR Part 11 are **kept deliberately**: they
forbid the agent from claiming a compliance posture, which is more correct after this change, not
less.

**What this does not change:** anyone who later needs the regulated posture needs the chain back, and
it is in `git log` — but they would also need a validation package, a qualification process and a QA
owner, none of which a repository can supply. The chain without those was the appearance of the
posture, which is the thing this decision is actually removing.
