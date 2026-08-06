# Task: implement the open app fixes

Branch: `claude/temporal-workflows-llm-testing-5nziyp`.

**The brief was "implement app fixes" against a backlog with 110 open rows across 20 sections**, so
the first job was picking the ones that are *product* defects — things a chemist or the system does
wrong — as opposed to further measurement, documentation or test work. Four qualified, plus the
bookkeeping for three rows a merged PR had already closed.

Two of the four were filed as *decisions rather than diffs*. Both turned out to be smaller than
their rows implied, and in one case the recorded blocker was simply not true.

---

## Plan

### 1 — a solvent the method cannot model, refused at launch (`2c434f8`)
- [x] `science/calc/solvents.py`: `ALPB_SOLVENTS` measured against the installed tblite, not
      recalled; `require_supported_solvents` as the precondition; `SUGGESTED_SOLVENTS` a strict
      subset so the shortlist cannot drift
- [x] All five solvent-taking calc jobs declare it in `connector.yaml`
- [x] `xtb_engine.COMMON_SOLVENTS` retired — it had drifted to omit dmf, dioxane, benzene and
      nitromethane while claiming to name what process chemistry asks about
- [x] Tests re-derive the set in both directions; end-to-end through `prepare_job_launch`
- [x] ADR: `D-2026-08-06-the-method-decides-which-solvents-exist`

### 2 — the conflict flag becomes a signal (`cdf71ec`)
- [x] Each note carries its **widest** disagreements (`conflict_max_per_note`, 3); the walk takes
      the wider end first and stops at the threshold
- [x] `Conflict.severity` pins declared conflicts above every suspected pair
- [x] `NoteConflicts.total` + `EvidenceChunk.conflicts_total` + "(the 3 strongest of 141)" in the
      report, so the truncation is never silent
- [x] Re-measured: 141,156 -> 5,937 pairs, 637 -> 44 ms, 3 ids per chunk instead of ~141
- [x] ADR: `D-2026-08-06-a-flag-is-a-signal-not-an-inventory`

### 3 — the durable BO campaign writes its record (`09c7700`)
- [x] `BoCampaignWorkflow` reads `requested_by` off the run's memo — the mechanism core built for
      exactly this, and which `connectors/qm/workflows.py` has used since F5
- [x] `record_campaign_run` activity reusing `record_suggestion` unchanged
- [x] `infra/sql/037_*.sql`: the problem snapshot, and `job_id` + a partial unique index for
      idempotency (keyed on the run, never the content)
- [x] Both store backends implement the retry rule
- [x] `_BO_ACTIVITIES` derived from the registry instead of a hand-written list
- [x] ADR: `D-2026-08-06-the-memo-already-carried-the-actor`

### 4 — a turn stops re-asking the same question
- [x] `agent/repeat_guard.py`: refuse (never cache) the identical call past
      `max_identical_tool_calls`; `RepeatedCallRefusal` reuses the audit + surfacing layers
- [x] `chemclaw_repeated_tool_calls_total{tool}` — the only trace, since the turn still answers
- [x] Wired in `chemclaw_agent.build_agent` and the runner's per-turn ambients; the middleware
      count assertions in `test_agent.py`/`test_profiles.py` moved 6 -> 7
- [x] ADR: `D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice`

### 5 — close the record
- [x] The three CHECKMATE rows PR #133 fixed but left `[ ]` (the `refresh` rowcount, the webhook
      translator, the layering granularity) — verified against `main`, then ticked
- [x] The seven rows this pass closed, each with what it turned out to be
- [x] `.env.example` for the two new settings

---

## Verification

- `make lint type test` green; every fix has a test **verified to fail against its own mutant**
  (18 mutants driven across the four changes; the two that survived are named below).
- **Live, not offline**, for the two that could not be settled in a sandbox:
  - migration 037 applied with `make db-migrate` against the running database, then the partial
    unique index and the `ON CONFLICT ... WHERE` inference asserted against real Postgres;
  - the whole durable campaign through the real broker and workers — `recorded as
    campaign-a9957bf78a2212aa`, resumed with `opened_by` `chemist@example.com` off the memo.
- The ALPB set probed against the installed tblite rather than written from memory, which is what
  turned up the two *different* rejection messages and the four solvents the old shortlist omitted.

---

## Review

**Both "decisions" dissolved on contact with the code.** The BO row said closing the gap meant
either threading identity through a seam built to keep it out or fabricating an actor; the seam was
built to keep identity out of the **payload**, and it has carried the actor on the memo since D-118,
with a production reader in another bundle and a test pinning the crossing. The conflicts row said a
per-note cap "changes what KM-8 shows a chemist"; `Conflict.kind` already separated author-stated
from heuristic, so the cap applies to `suspected` alone and the declared-conflict promise is
byte-identical — and the gap magnitude was already computed at the line that decides whether to
report a pair, so "widest first" needed `max` instead of `append` and no new signal at all.

**The cost and the noise were one fact.** The conflicts row listed 637 ms and ~141 ids per chunk as
two symptoms with two possible fixes. Ordering the group by confidence fixes both, because the same
ordering that ranks the output is the one that lets the scan stop early.

**Measuring beat recalling, again.** The ALPB list was going to be written from the shortlist that
already existed. Probing tblite instead showed that shortlist was missing four ordinary process
solvents, and that the library rejects a name from its *dielectric* table and from its *Born
parameter* table with two different messages — so "which names work" is an intersection, not a list.

**Two surviving mutants, named rather than driven to zero.** In `repeat_guard`, replacing the
off-the-request-path early return with a fresh `Counter()` is behaviourally equivalent (every call
is then the first). In `conflicts`, the early `break` is invisible in the output — bounding what
each note emits already bounds the flags — so it is pinned by a test that counts *reads* rather than
results.

**What was deliberately not done.** The `xtb_job_heartbeat_timeout_seconds` coupling (one 600 s
setting is both the CREST-sized legitimate gap tolerance and the only dead-worker signal) stays
open: it is a config-surface change on the durable spine, and a per-job value wants measuring
against a real eviction rather than reasoning about. The behavioural half of du-03 — a turn that
loops on retrieval and never reaches the capability it needed — is partly addressed (the loop now
costs two calls instead of eight), but whether the *cause* is retrieval, prose, or a 38-note corpus
is still unmeasured and cannot be settled on that data.
