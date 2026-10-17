# D-2026-10-17-an-erasure-is-logged-outside-the-database — Every applied erasure appends a line to a ledger file outside the database, and a restore replays the lines after its recovery point

**Status:** accepted · **Date:** 2026-10-17

## Context

D-2026-10-15-postgres-is-backed-up-by-wal-archiving-to-an-object-store makes erasure apply on
restore: backups are not rewritten, and after a restore the erasures made since the recovery point
are re-applied before the system serves traffic. That needs a record of those erasures that the
restore does not rewind, and `make user-erase` (`cli/erase_actor.py::main`) kept none — the runbook
asked the operator to keep one by hand. A record in the database cannot serve: the restore it exists
for discards it with the erasures it lists. An erasure applied and then lost to a restore is a
person's data back in service with nothing to say it should not be.

What an erasure is, precisely, decides what a record must hold. `agent/leaver.erase_actor` deletes
the conversation tier of one id (both its spellings) in one tenant; `finish_erasure` deletes, by
session id, the rows an interrupted erasure left under sessions whose ownership row is already gone.
A restore can land between the two, so a finish must be recorded too. Re-applying either removes
nothing the first application did not, so a replay is idempotent; replaying an erasure from before
the recovery point is not harmless, though, because a person who returned after it would lose data
they created since.

## Options

1. **An append-only ledger file at a configured path**, on a volume the operator backs up apart from
   the database. One JSON line per applied erasure, written by the command itself the moment the
   deletion commits. Needs a claim on the pod that runs the erasure and a backup of it; no new
   client, credential or network path. **Chosen by the owner.**
2. **An object per erasure in the backup bucket.** The bucket already outlives the database and is
   versioned and locked. But the application would need an object-store client and write
   credentials to the bucket its own backups live in — the credential the runbook keeps out of the
   application (*Keys and credentials are as sensitive as the data*) — and a rehearsal namespace
   reads that bucket read-only, so a replay there could not record itself. Declined.
3. **An operator-kept manual log** (the status quo). Costs nothing to build and depends on a person
   remembering every run; nothing refuses an erasure nobody logged, and a replay is retyped by hand.
   Kept only as an explicitly stated posture (below), not as the default.

## Decision

Option 1.

- **The record.** `{actor, at, scope}` and nothing else: the id given to the actor route (`null`
  for a finish), an instant no earlier than the commit, and the scope — route `actor` with its
  tenant, or route `finish` with its tenant and session ids. No counts, ticket or name.
- **The clock.** `at` is the session database's `clock_timestamp()`, read on the same connection
  after the commit, so a record is newer than any recovery point the commit is not inside; the host
  clock stands in, with a warning, only when that read fails.
- **The append.** One line, written under an exclusive POSIX record lock (`lockf`, honoured by NFS
  where `O_APPEND` alone is not atomic), then the file and its directory fsynced. A torn last line
  is closed before the next record, and a line that does not parse stops a replay by its number.
- **Fail closed.** `--apply` with `erasure_ledger_path` unset is refused before anything is deleted,
  unless `erasure_ledger_kept_by_hand` states option 3; then each run prints the line to keep. An
  erasure that committed and could not be recorded exits `3` with the line; re-running it records it.
- **The replay.** `erase_actor --replay-since <recovery point>` (`make user-erase-replay`)
  re-applies the distinct records stamped strictly after the point, in ledger order and each in the
  tenant it names, and records each re-application, so a later restore to before the replay finds it
  too. It refuses a ledger that is not there rather than re-applying nothing.
- **The chart** mounts an operator-provided claim read-write on the background worker
  (`erasureLedger`), the pod the runbook runs the erasure in, and refuses two workers on a claim
  that is not `ReadWriteMany`.

## Consequences

- `make restore-drill` proves it end to end (`erasure_replay`): a person erased after the marker is
  back in the restore, and replaying the ledger removes their rows; a second replay changes nothing.
- The ledger is personal data (actor ids) with a lifetime of its own: a line is needed while a backup
  from before it can be restored, and the runbook says so.
- `tests/test_erasure_ledger.py` holds the line format, the lock and the fsyncs;
  `tests/test_leaver.py` the refusal, the record and the replay through the CLI.

Revisit when: a site cannot provision a claim backed up apart from the database and runs with
`CHEMCLAW_ERASURE_LEDGER_KEPT_BY_HAND=true` instead, or the application gains an object-store client
for another reason — either makes option 2's cost the smaller one.
