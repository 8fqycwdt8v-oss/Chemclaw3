# D-2026-09-09-a-grant-set-that-contracts-is-not-a-pre-upgrade-step — the reconciliation narrows, so it gets a rollback hook, a drift report and a derived upstream matrix

**Status:** accepted · **Date:** 2026-09-09 · **Builds on:**
`D-2026-08-05-append-only-by-grant-not-by-contract` (the grant file, and why the trail's integrity
is a privilege rather than a hash chain), `D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks`
(which left that privilege as the *whole* of the claim),
`D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step` (whose argument
this ADR applies to the step next to the one it was written about), and
`D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns` (the app owns eight tables in
`public`, which is what makes the third finding possible at all). **Supersedes** nothing; it
corrects two sentences that shipped inside those decisions' files.

## Context

`tasks/lessons.md` says a grant set has to be reconciled on every deploy, and that half is
implemented and sound: `app_privileges.sql` revokes and then states the whole matrix,
`chemclaw.core.grants` re-applies it from the `pre-upgrade` hook, and
`tests/test_database_privileges.py` fails in both directions if the file and `src/` disagree. Three
things around it were not.

**All three were reproduced before they were fixed**, against PostgreSQL 16.15 on a throwaway
database migrated to `091`, with throwaway roles dropped on the way out.

### 1. The set contracts, and the contraction lands on the release that is still serving

The file is a *full restatement*. A verb removed from it is not merely un-granted, it is **revoked**
— which is the property that makes the reconciliation worth having, and also the one nothing
accounted for. Replayed on `7654cfb0`, the commit that dropped `note_proposals` from the writer list
in the same commit that deleted its writer:

```
release N   (old grant file):   note_proposals INSERT | True
release N+1 (pre-upgrade hook): note_proposals INSERT | False
as the role: INSERT INTO note_proposals ... -> 42501: permission denied for table note_proposals
```

The hook is `pre-install,pre-upgrade` at `migrate-job.yaml:20`, so that revoke happens while the
previous release's pods are the only thing serving. And `helm rollback` runs neither `pre-upgrade`
nor `post-upgrade` — `grep 'helm.sh/hook"' templates/` returned six hits, all install/upgrade — so a
rolled-back release restores the older image against the **newer** ACL and stays there until the
next successful deploy.

Two sentences in this repository said the opposite, each true of the thing beside it and false of
this one: `migrate-job.yaml:9` and `D-2026-08-27` both justified the hook point with *the grants
only widen*, while `app_privileges.sql:37` advertised narrowing as a feature nine files away. The
pair is what let the hazard sit in plain sight — read either alone and it is correct.

### 2. Reconciliation reaches one of four ACL sources

`app_privileges.sql:37` says "Start from nothing … so this file states the whole matrix". True of
direct grants to the named role; false of everything else Postgres consults for that role. Measured
as the app role, *after* a full reconciliation:

| drift | revoked by the restatement? | effect as the app role |
|---|---|---|
| `GRANT UPDATE,DELETE ON audit_events` to the role | yes | `42501` on both |
| `GRANT UPDATE (created_at) ON structures` to the role | yes | `42501` |
| the same `UPDATE` granted to `PUBLIC` | **no** | `UPDATE audit_events` succeeds |
| role membership in the owning role | **no** | `UPDATE`/`DELETE audit_events` succeed; so does `DROP TABLE audit_anchors` |
| `ALTER DEFAULT PRIVILEGES … GRANT ALL ON TABLES` | **no** | a table created between deploys holds `SELECT, INSERT, UPDATE, DELETE, TRUNCATE` |

The first two rows are the control working. The third and fourth retire
`D-2026-08-14`'s only surviving control — the trail's integrity claim is exactly *the credential
that writes a row cannot rewrite it* — without one line of the grant file changing, and the fifth
re-widens every table a later migration adds, which is the hazard the opening `REVOKE` exists to
close, arriving from the one direction it cannot see.

### 3. A ninth table upstream adds dies on the *second* deploy, and the verb map could not see a bump

`REVOKE ALL ON ALL TABLES` is indiscriminate, and an owner's own DML does not survive a materialised
ACL. `app_privileges.sql:227-240` documents this for the eight tables LangGraph creates and guards
each by name; nothing guards a ninth:

```
as app role: CREATE TABLE checkpoint_ninth ... ok
after first deploy:  checkpoint_ninth S/I/U/D | t,t,t,t
after reconcile:     checkpoint_ninth S/I/U/D | t,f,f,f
as the owner role: INSERT -> 42501 permission denied for table checkpoint_ninth
```

It installs fine, survives the deploy that creates it, and fails on the next one. The table *name*
is caught by CI (`_upstream_tables()` derives the set from the installed `MIGRATIONS`, and
`tests/test_message_migration.py` holds `CHECKPOINT_TABLES` to it). The **verbs** were not: a
hand-written map of eight entries, so an upstream minor bump turning `checkpoint_blobs`'
`ON CONFLICT … DO NOTHING` into a `DO UPDATE` would pass every check here — the map says the grant
file is right and the grant file says the map is right — and then meet `permission denied` the first
time two writers raced on one key.

## Decision

### The rollback hook, and what the contract step would have cost

**The reconciliation runs on `pre-rollback` as well.** `helm rollback` restores the target
revision's manifest, so the Job that runs is the target's — its image, its `infra/sql`, its grant
file — and the restored pods find the matrix they were written against. `pre-` rather than `post-`,
so the ACL is in place before the pods are; the release that loses verbs during the rollback is the
one being abandoned, which is the correct direction for that trade. The `migrate` half of the same
container is a no-op there by construction: `chemclaw.core.migrate` iterates the files *in the
image* and skips every one the ledger records, and a newer release's extra ledger rows name no file
an older image has.

**The alternative was to make narrowing an explicit contract step** — a verb may leave the file only
one release after its writer leaves `src/`. It is the better *shape*: it fixes the upgrade window as
well as the rollback, because a release only ever revokes what the previous release had already
stopped using. It is declined because **nothing here can enforce it.** The check would need a
declared "held for one release" set, and then two things neither of which this repository has: a
notion of what a release is, and a view of the *previous* file. A test at one commit sees a verb
that was deleted outright and a verb that was never there as the same thing. What it would actually
buy is a lag set that the excess ratchet has to tolerate — weakening
`test_the_grant_matches_the_writes_the_code_actually_performs` in exactly the direction it was
written for — in exchange for a discipline no test enforces. That is the `reject_widening` shape:
a control that exists as a claim.

**The upgrade window is not closed, and this is what it is waiting on.** The mechanical fix is the
one `D-2026-08-27` already found for the conversion: split expand from contract, apply the grants
additively `pre-upgrade` and the full restatement `post-upgrade`, once the new pods are up. That
needs a second Job document in `migrate-job.yaml` and a mode on `chemclaw.core.grants`, and
`tests/test_helm_chart.py` asserts `set(documents) == {"migrate", "convert"}` over that file. Not
taken here because that file belongs to another change; recorded so the next session does not
re-derive it. Until then the window is a rollout, bounded and loud, where the rollback window was
unbounded and silent.

### Report, not refuse — and the line is *CI fails, the deploy reports*

`app_privileges.sql` ends in a drift audit over the three channels above plus the ninth-table gap,
raising a server `WARNING` per finding, which `chemclaw.core.grants` prints onto the deploy log.

**Refusing was considered and is worse here.** A raise fails the `pre-upgrade` hook, which blocks
the release — and the operator whose hand-grant caused it is the one person who cannot fix it from
the deploy. That converts a hazard into an outage, on a database somebody deliberately configured.
The usual answer to that is a refusal with a stated opt-out, which is what
`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` does for the chart's egress posture — but
an opt-out is a setting, and this file is applied as plain SQL by a module that mounts no settings
into it.

So the two halves are split by what each can see. **CI fails**: `tests/test_runtime_ddl_privilege.py`
introduces each drift against a probe role, reconciles, and fails if the audit says nothing — plus
the other direction, that a clean reconciliation reports *nothing*, without which four warn-tests
prove only that it warns. **The deploy reports**: a live database's hand-grants are the one thing no
test in this repository can see, and that is exactly what the `WARNING` is for. The printing is
load-bearing rather than cosmetic — psycopg discards diagnostics with no handler attached, so
without it the audit would run on every deploy and be read by nobody, which is the "a control
exists" claim this repository keeps deleting, in its purest form.

### The upstream verb map is derived, and the ninth table is reported rather than guessed

`_upstream_verbs()` reads the verbs off the installed distributions' own SQL — the same basis
`_upstream_tables()` reads the names off — so an upstream `DO NOTHING` that becomes a `DO UPDATE`
turns CI red instead of turning into a runtime `permission denied`. Verified to produce exactly the
matrix that was hardcoded, which is the point: nothing was widened, the basis moved.
`checkpoint/postgres/shallow.py` is excluded deliberately — `ShallowPostgresSaver` writes
`checkpoint_blobs` with `DO UPDATE` where the saver this repository runs writes it with
`DO NOTHING`, so scanning it would derive a verb no process here performs, and the basis is the code
that runs.

The ninth table itself is **reported, not auto-granted**. A rule that hands DML back to any table
the app role happens to own is the boundary widening on a schedule; a report names the table at the
deploy that breaks it rather than at the first turn after it.

## Consequences

- `helm rollback` re-applies the target revision's grant file. This takes effect for rollbacks *to*
  a revision carrying the annotation — Helm reads hooks off the target's stored manifest — so the
  first release after this change is not yet protected on the way back to its predecessor.
- The `pre-rollback` Job mounts `chemclaw.migrationEnv`, as the same Job already does at its other
  two hook points. No new credential, no new value, no new document.
- A deploy against a database with hand-granted role membership, a `PUBLIC` write or default
  privileges now prints a `grant drift:` line per finding and **continues**. An operator reading
  a successful deploy log is the intended reader; nothing alerts.
- `tests/test_database_privileges.py` no longer lists upstream's verbs. A langgraph bump that
  changes how it writes one of its own tables now fails there, naming the table.
- Two sentences are corrected and an absence test fails whoever writes them again: neither
  `migrate-job.yaml` nor `app_privileges.sql` may claim the grants only widen. `D-2026-08-27` says
  it too and is **not** edited — a merged ADR is a record of what was decided, and this is the ADR
  that supersedes that clause of it.
- `docs/guides/runbook.md`'s rollback section lists what `helm rollback` does not undo and does not
  mention the grants; it needs the same correction, in the change that owns that file.
- **Revisit when** the expand/contract split becomes available — when `migrate-job.yaml` may carry a
  third document — at which point the upgrade window closes and this ADR's first decision keeps only
  its rollback half. Not on a date.
