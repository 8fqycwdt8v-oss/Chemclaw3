# D-2026-08-01-a-restore-is-a-truncation-nobody-can-see — A restore is a truncation nobody can see

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-034 (migrations as a pre-deploy hook),
F10-G1 / `infra/sql/011` (the audit hash chain),
D-2026-07-31-the-audit-chain-is-versioned · **Partially closes:** the `DEFERRED.md` row *"Audit
chain: provable tail completeness and disposal"*, whose disposal half stays open

## Context

The readiness review found zero occurrences of backup, `pg_dump`, PITR, RPO or RTO anywhere under
`deploy/`, `infra/` or `docs/`, across four stores nobody owns: Postgres (the audit chain, sessions,
the calculation cache, the note index), Temporal's own store, the knowledge git repo, and the
external HPC artifact store.

The interesting part is not the absence. It is what writing the missing procedure would have done.

`agent/audit_store.py` chains every audited row to its predecessor, so modification, reordering,
interior deletion and prefix truncation each break a link. Deleting a **trailing** run does not: the
survivors chain cleanly and nothing ever recorded how many rows there should have been.
`cli/verify_audit_chain.py` has carried that as a "Known limit" paragraph since it was written, and
`DEFERRED.md` held the fix pending a regulated deployment asking for provable tail completeness. The
same fact is why `durable/retention.py` refuses to prune `audit_events` at all.

**A point-in-time restore is a trailing deletion.** So the recovery procedure this system lacks is,
if written without anything else, a documented instruction to silently shorten the GxP compliance
trail in the exact way the chain was built not to notice — and to leave it verifying clean
afterwards. The deferral's trigger had been a regulator asking a question. It turned out the real
trigger was writing down how to recover, which is not optional.

## Decision

**Ship the anchor first, and treat it as a precondition of the backup story rather than a companion
to it.**

A signed high-water anchor records what the trail held at a moment — row count, highest id, and the
tip's `row_hash` — and `verify_chain` holds the live trail against it. Three numbers rather than
one, because they fail differently:

| Recorded | Catches |
|---|---|
| `row_count` | the trail is short — a restore, or a deletion |
| `max_event_id` | rows past the anchor are gone (a count-only anchor misses delete-then-append) |
| `tip_hash` | a trail of the anchored height with different content — rebuilt, not shortened |

Two properties do the real work, and both were arrived at by asking what an attacker with the access
required to truncate the trail could also do.

**Signed, with a key that is not in the database.** Anyone able to delete rows from `audit_events`
can insert a lower row into `audit_anchors`, so an unsigned high-water mark defends against
accidents and nothing else. `audit_anchor_secret` is the sixth plain secret in the chart, and the
argument for it being a secret rather than a config value is exactly this one.

**Published out of band, because the table is not the control.** A PITR rolls `audit_anchors` back
together with `audit_events`, so an anchor that lived only in Postgres would be restored into
agreement with the truncated trail it exists to catch — the chain's own mistake, one level up. Every
anchor is therefore also written to the process log at a stable marker (`audit_chain_anchor=`),
landing in a store Postgres cannot roll back; after a restore an operator recovers that line and
passes it to `verify-audit-chain --anchor`. The table catches tampering that did not think to
rewrite the anchors; the log line catches the restore.

Anchors are taken by `AuditChainVerifyWorkflow` **after** a clean verification, never before —
anchoring an unverified trail signs whatever damage is in it and makes that the baseline. The
schedule's cadence is therefore also the resolution of this detection: a trail can be shown to have
lost rows since the last anchor, never since the last event.

`--reseal "<reason>"` records a new anchor after a legitimate recovery, storing who accepted the gap
and why, and refuses outright on a broken chain. That refusal is the point of the flag existing:
re-sealing over a break would sign the damage and the trail would verify clean forever after.

## Why not the alternatives

**Ship `pg_dump`/PITR tooling in this chart.** The chart does not deploy Postgres or Temporal — it
dials `chemclaw-temporal-frontend.temporal.svc:7233` and an external DSN, which is its own open
backlog row. A backup CronJob here would be this chart claiming ownership of stores it explicitly
does not own, and would be wrong the moment an operator runs a managed instance with its own
snapshot policy (which is the expected case). What the system can honestly state is *what it
requires of whoever does own them*, and where the audit trail's requirement differs from ordinary
data. That is `runbook.md` §(xiii).

**Anchor into the knowledge git repo instead of the log.** Genuinely appealing — it is a second
store, append-only, already present, and already the place agent-authored records go through a human
gate. Rejected because it inverts a boundary the whole architecture rests on: `knowledge/` is what
the *agent* proposes and a human merges (D-005), and putting a compliance control in the same tree
makes an operator's integrity evidence subject to the PR-gate's review latency and to whatever a
merge conflict does to it. The log is worse as a database and better as a witness.

**Store the anchor in the same transaction as each audit append.** That would give per-row rather
than per-schedule resolution, and give up the whole property: an anchor written by the same
connection, in the same database, at the same instant as the row it anchors is restored with it.

**Wait for the regulated ask, as `DEFERRED.md` said.** That deferral's reasoning was sound and its
*trigger* was wrong. It named an auditor, and the real trigger was any deployment that can recover
from a backup — which is every deployment that should exist.

## Consequences

- A trail shortened by a restore, a deletion, or a rebuild is detectable, given an anchor from
  before it. That last clause is the honest bound: this is evidence of a gap, not prevention of one,
  and it can only see back as far as the newest anchor an operator still holds.
- Anchoring is **off without a secret**, and that is stated rather than defaulted around. A
  generated key would live in the same place as the data it attests, which is not evidence.
- The `DEFERRED.md` row is rewritten, not deleted: completeness is closed and **disposal is not**.
  They are opposites — an anchor makes a shortened trail *visible*, which is exactly not making one
  *permissible* — so archive-then-reseal still needs a real retention obligation and QA sign-off.
- One mutation here cannot be killed by any test and is recorded rather than left implied:
  replacing `hmac.compare_digest` with `==` passes everything, because a timing side channel is not
  observable from a functional assertion. It stays because it is correct, not because it is covered.
- A different mutation *was* worth its finding: deleting the "no secret configured" guard in
  `signature_ok` left every test green, because an anchor sealed with a real key fails under an
  empty one anyway. The case it opened is the other direction — with anchoring off, the key is the
  empty string, which everyone knows, so an attacker can seal their own anchor and have it verify.
  There is now a test for exactly that.

## Not in this change

The backup *tooling*, per the reasoning above — and Postgres/Temporal ownership, which is the
backlog row it belongs to. Provable disposal. And the other three unowned stores' recovery, which
the runbook now states requirements for without pretending this repo implements them.
