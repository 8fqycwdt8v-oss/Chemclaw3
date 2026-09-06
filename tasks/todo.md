# Task — ungate knowledge, and delete the PR-gate

Carries `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`, which decided the axis and
explicitly did not claim the code shipped. Owner's call this session, put as a question because the
ADR did not foresee it: **every `propose_note` caller is knowledge**, so ungating leaves the gate
with no subject at all. Chosen: ungate everything and delete the gate rather than leave ~2,232
lines unreachable (`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`).

## Blast radius, measured

- **15 src files** import the gate; **~17 test files** exercise it.
- 348 files mention it in prose; **11 of 11 eval probe files** grade the agent on gate behaviour.

## The write path

`settings.knowledge_path` is `note_repo_dir / knowledge_dir` — one location for read and write, so a
file written there is readable by `load_notes` *immediately*. That is what makes "global the moment
it is learned" true without a new store.

- [x] 1. `kg/record.py`: `record_note(note, writer, ...)` replaces `pr_gate.propose_note`. Renders
      the subject note, its dependencies and its retirements; writes them; returns the reference.
      **Dependencies are written before the subject**, so a note never appears in the graph before
      what it cites — the invariant that replaces "one PR is one reviewable unit" (D-133).
- [x] 2. `kg/submission.py` → the write vocabulary: `NoteFile` kept, `NoteSubmission` → `NoteWrite`
      (no branch/title/body), `NoteSubmitter` → `NoteWriter`. The injection seam stays; tests need it.
- [x] 3. `kg/git_submitter.py` → `GitNoteWriter`: keep the repo guard, the lock and the error
      classification; drop the per-note branch, the worktree and the force-push. Commit on the
      checkout's own branch and push.
- [x] 4. The 9 call sites: `graph_tools` (x2), `memory_jobs` (x2), `observation_jobs`,
      `report_workflow` (x2), `backfill_corpus`, `memory/interaction`.

## The deletions

- [x] 5. `kg/proposal.py`, `kg/proposal_store.py`, `api/routes/proposals.py`,
      `cli/reconcile_proposals.py`, the `Proposal*` schemas, `_visible_proposal`/`VisibleProposal`,
      the knowledge-merged webhook. **`_is_reviewer` stays** — `routes/jobs.py` uses it too.
- [x] 6. Migrations 027/036/058 keep their files with `RETIRED` headers and the tables stay empty,
      the forward-only rule `D-2026-08-14` already paid for with `audit_anchors`.
      `durable/retention.py`'s `note_proposals` refusal goes with the reason it stated.
- [x] 7. Metrics whose subject is gone (`chemclaw_notes_proposed_total` and the proposal-state
      series), the `proposal_*` settings, and `make proposals-reconcile`.

## The one real correctness question

- [x] 8. **D-161's support count was "distinct *merged* notes" and there is no merge any more.**
      `mine_interactions` counts `interaction` notes as support; ungated, those are agent-written
      with no human step, which is the self-confirming loop migration `025`'s CHECK exists to stop,
      one level up. Decide and state it: either support counts only human-authored notes, or the
      thresholds mean something new and `observations.py` says so. **Not a detail to discover
      while editing** — it is the reason D-161 wrote a CHECK rather than a convention.

## Then

- [x] 9. Tests: delete `test_note_proposals*.py` and `test_pr_gate.py`; rewrite the gate assertions
      in the other ~14; add the direct-write tests including the dependency-ordering invariant.
- [x] 10. Probes: 11 files grade gate behaviour. Regrade against what the system now does.
- [x] 11. Prose: `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md`, package READMEs, the skills that
      teach the gate, the system prompt and `make prose-validate`.
- [x] 12. ADR + ledger + BACKLOG; `make lint type test` green with Postgres up.

## Review

**The fork the deciding ADR had not foreseen, and how it was resolved.** All nine `propose_note`
callers write knowledge, so ungating did not shrink the gate's subject — it removed it, leaving
2,232 lines with no caller. That is not a call to make while editing: shipping them dormant is what
`D-2026-08-15` deleted 1,442 lines over, and deleting throws away tested machinery including #323's
reviewer history from hours earlier. Put to the owner; answer was delete.

**Three things the change turned over, each argued rather than assumed:**

1. **Write order replaces D-133.** A PR merged every file at once; a direct write can be read
   mid-flight. Dependencies → subject → retirements, each citing the one before it, with the
   accepted window (a note and its replacement both current) stated against the rejected one (a
   dangling `superseded-by`).
2. **The cache is now busted where the gate deliberately did not bust it.** Both correct for their
   own design — the gate wrote where no reader scanned. The test that asserted "leave the cache
   alone" now asserts the opposite and keeps both earlier readings in its docstring.
3. **A regression the suite caught.** The gate's linked worktree had its own index, so staged
   residue structurally could not reach a note's commit. One shared index removed that guarantee
   and a plain `git commit` swept the stray in. Fixed in the *code* (path-limited commit and
   path-limited idempotence check), not in the test.

**D-161's anti-feedback rule was restated, not repaired.** `load_notes` now returns agent-written
notes, so "support counts *merged* notes" would have become the description of a self-confirming
loop. It is not one: `project_of` admits only reaction records, so support is real experiments plus
the chemist's own confirmation. The property doing the work was never the merge — it was the kind of
thing counted, and the merge was a second human step on top of a human act that had already
happened.

**What was found only because a validator exists.** `make prose-validate` caught seven stale
references including one in a *merged* ADR, which must never be edited — the sanctioned remedy is
`_RETIRED_METRIC_NAMES`, empty until now, and this is its first entry. `test_docstring_paths`
caught 22 files with dangling module pointers. Neither would have been visible by reading.

**Cost accepted and written down rather than smoothed over**: a wrong machine-written claim is now
served until contradicted. (The second cost stated here — "a write that dies between two files
leaves the first on disk" — was closed in the fix-forward below rather than accepted: every path is
validated before any byte lands, each file is replaced atomically, and a failure restores what was
there.)

**Verification.** `make lint`, `make type` (448 files), `make prose-validate`, `make skill-validate`
green. Full `make test` reported in the commit, with Docker and Postgres up so the DB-backed tests
run rather than skip — the first run of this change reported 475 skips against a normal 63, which
was Postgres being down and is exactly the trap `CLAUDE.md` warns about.


---

# Fix-forward — five fresh-context reviews of the merge above

Five subagents over `7654cfb`, each given the diff and one dimension, none given the account of why
it was written. ADR: `D-2026-09-05-a-reader-outlives-its-writer-more-quietly-than-a-writer`.

**The shape four of the nine findings share**, and the reason a deletion is riskier than a build: a
component whose *producer* was removed keeps its readers, and a reader with no writer passes every
test it has. Grep finds the writers; the readers name a table or a concept and survive the grep.

## Stranded readers

- [x] `operations.authorship` answered "how much of this was AI-written" out of `note_proposals` —
      a table nothing writes. On any deployment installed since it would report `proposed=0`: not an
      error, a truthful-looking zero about the thing that was asked. Rebased onto `audit_events`.
- [x] The evidence pack's `proposals` section, same table. Worse setting: the pack's own `limits`
      train a reader to read an empty section as "this system recorded nothing". Deleted; the one
      thing genuinely lost is named in `LIMITS`.
- [x] `kg-validate`'s docstring claimed it gates the PR that adds notes. It never runs between a
      write and its readability now. Restated as what it catches.
- [x] `backfill_corpus`'s stated safety was "nothing lands in the graph unreviewed". Re-grounded on
      the reason that is actually true — a deterministic transcription infers nothing.

## Two defects the deletion created in the write path, both measured

- [x] **A failed push wedged every later write on that pod, forever.** The writer deliberately
      leaves a note committed locally when a push fails; once the remote moves, `merge --ff-only`
      refuses and every subsequent write raises — while `_push`'s docstring said the next attempt
      "fetches, fast-forwards past whatever landed, and pushes this commit along with its own".
      `_replay_our_unpushed_commits` rebases *our* commits and refuses anything without the trailer,
      which is the case the old refusal was really for and could not distinguish.
- [x] **The knowledge-sync sidecar deleted notes the pod had just recorded.** `rsync -a --delete`
      from a replica into the tree the writer now commits to removed a note whose push had failed —
      permanently, since it stays in the local `HEAD` and no path-limited `git add` restores it.
      Where there is a writer's clone the refresh is that clone's own fast-forward; the replica
      stays for a pod that records nothing. A divergence warns rather than failing, because `once`
      is an init container and a non-zero exit would crash-loop the pod on a stranded note.

## Test quality

- [x] Five mutations the suite did not kill, each re-planted to verify the fix: the retirements half
      of the write order (148 tests green with the loop hoisted), the supporting-note count
      (`test_pr_gate.py` was deleted and had been pinning it), `record.py`'s `overwrite=False` on
      dependencies, the `if outcome.written:` metric guard, and the `diff --cached` scoping.
- [x] Three inert assertions: the connector-bundle guard scanned for a module *this commit deleted*
      and otherwise matched one call spelling (two bypasses planted and now caught); the
      fast-forward test built its second clone after the first push, so it was never stale; and two
      assertions billed as "the absence of the mutation" checked for artefacts nothing creates.

## Prose, verified at the emitted artifact

Nine model-facing passages still promised a reviewer — six skills, the `reporting` profile's live
prompt, eight eval probes whose `forbids_claims` contradicted their own `direction`. Checked by
rendering every registered profile's `SystemMessage` and every bound tool's `.description`, not by
grep, because grep is what let them ship.

## Companion repo

`Chemclaw3_ui` #68: the review queue's proposals section called the deleted `/proposals` routes, and
`orEmpty` folds a 404 into `[]` — so a chemist saw "everything has been decided" for a decision that
cannot occur. Second time that policy has bitten that page. `/review` itself stays: its plans and
questions sections are live.

## Verification

`ruff`, `mypy --strict` (799 files) and every validator green; full `pytest` with Docker and
Postgres up, so the DB-backed tests run rather than skip.

The context floor moves 43,063 -> 43,316 against the unraised 43,500 ceiling: **184 tokens of
headroom**, from one optional argument on `record_confirmed_answer`. That is tight enough to be the
next person's problem, and the reclaim is already a `BACKLOG.md` row.

---

# Implementing the 200-user performance findings

Source: `docs/archive/REVIEW-2026-09-04-performance-at-200-users.md`. Every item below names the
measurement that justifies it; a fix that does not move its number is not done.

## Done (lead, committed dc9d2a9)

- [x] **Generic plans on pooled connections.** `plan_cache_mode=force_custom_plan` in
      `core/db._merged_options`. Dense note query 1,280 ms -> 9 ms at 100k chunks; measured cost on
      a point lookup 135.7 -> 140.5 us. Both candidate remedies measured before choosing;
      `prepare_threshold=None` rejected because it discards the parse cache too. Checkpointer pool
      deliberately excluded (PK lookups, generic is correct and cheaper there).
- [x] **One TLS trust store per process.** `core.http.default_ssl_context`, wired into both client
      factories. 7 clients per turn: 110 ms -> 0.26 ms (up to 424x), blocking loop CPU.

## In flight (seven agents, disjoint file ownership)

- [ ] **CORE-A** floor ratchet blind to connector tools (42,730 measured vs ~74,700 shipped);
      compaction trigger floored to 1; prefix byte-stability for server-side caching; executor
      sizing vs 64-way fan-out; lazy helper roster.
- [ ] **CORE-B** saturation as a third retry category, both sides of the wire; `queue_wait_timeout`
      at the three bundle call sites; the 60 s `schedule_to_close` that drops push-back and job
      records; cross-process single-flight (assess).
- [ ] **CORE-C** `reaction_records(reaction_id)` index; agent-callable leading-wildcard ILIKE;
      fingerprint HNSW predicates; KG scan off the loop; retention's unbounded DELETE and its
      non-converging pass cap; keyset pagination in backfill.
- [ ] **CORE-D** HPA on permit occupancy not CPU (218 millicores at 100% saturation); the shed made
      alertable; the liveness cascade and the missing stream-vs-socket cross-check; fleet Postgres
      arithmetic counting 1 pool per process where a process holds 3; retention absent from the
      chart; capacity defaults.
- [ ] **MCP-A** memoised jsonschema validators (7.071 -> 0.008 ms); cgroup-aware default executor;
      session idle reaping; the request log's time-to-headers duration.
- [ ] **MCP-B** calc singleton (replicas/HPA/PDB/grace); fleet-wide missing PDB/grace/spread;
      timeouts that can never fire; pyexec rlimit vs pod limit; unbounded emptyDir; the false
      RDKit-GIL justification.
- [ ] **UI-A** BFF socket pool below the streams it holds (512 < 600); uncompressed assets
      (634,903 -> 194,190 B); the fixed-interval recovery herd; no pagehide turn cancel; visibility
      gating.

## Verification plan

- Each agent runs lint + `mypy --strict` + the tests covering its modules, and reports the exact
  result. Postgres and Temporal are up, so Postgres-backed tests actually execute.
- Lead then runs `make lint type test` per repo and reports what it skipped, per the repo rule that
  a local green line is not evidence about the Postgres-backed set.
- Re-measure the headline numbers after merge rather than trusting the per-agent claims.

All seven workstreams landed. `make lint` and `make type` are green over 804 files in `Chemclaw3`;
`Chemclaw3-mcp` is 1634 passed / 7 skipped; `Chemclaw3_ui` is 855 passed over 87 files.

### What moved, measured

| | before | after |
|---|---|---|
| Dense note query, 100k chunks (generic plan) | 1,280 ms | 9 ms |
| Seven connector clients per turn (CA parse) | 110 ms | 0.26 ms |
| One MCP tool call, output-schema validation | 16.89 ms p50 | 3.96 ms p50 |
| `job_records` ILIKE miss, 500k rows | 1,036 ms | 1.09 ms |
| `reaction_records`, 50 ids | 152.3 ms / 217 MB | 0.80 ms / 203 buffers |
| `all_records` sort, 200k rows | 2,228 ms (136 MB spill) | 10.7 ms |
| Retention DELETE, 300k rows under a 5 s timeout | 0 rows removed | all 300,000 in 11.04 s |
| Loop lag during a graph build | 52.2 ms | 16.6 ms |
| KG scan loop lag at concurrency 8 | p50 418 ms | p50 26 ms |
| Frontend cold load per user | 797,679 B | 268,615 B |
| BFF healthz with 600 streams held | never answered | 11 ms |

### What was declined, and why

- **Fingerprint HNSW restructure.** 14x faster and returns a *different result set* for 22 of 60
  queries — ties, not recall. Exact-versus-approximate is a decision, not a patch; it wants an ADR.
- **Two derived queue bounds as settings.** A setting could name a bound above the budget it must
  stay below; the derivation cannot express that.
- **A lazy helper roster.** Saves nothing on a delegating turn and turns a build-time check into a
  promise. Building the whole graph off the loop was taken instead.
- **Cross-process single-flight.** Its `DEFERRED.md` trigger is not tripped at two processes, and
  the obvious remedy starves the pool (8 advisory locks -> `PoolTimeout` in 5.00 s).
- **Lowering `CHEMCLAW_CREST_THREADS`.** Doubles an hours-long search to free one slot.

### Where the review itself was wrong

Six of the seven agents corrected a claim in it, and in every case the error was mine: a figure
carried from a track report into the synthesis without being re-derived. The *magnitudes* held —
880x, 424x, the 42,730-vs-75,695 prefix gap — while three of the **mechanisms** did not.

- "Nothing prunes by default" — the chart *refuses to render* without a retention posture.
- "Input and structured output are validated" — input validation is off; the win is output only.
- "A 16x timeout mismatch" — no reader; the real defect was worse, all three budgets shipped *equal*.
- "A pod running CREST is busy at low CPU" — it draws ~4 cores, so CPU does lead saturation there.
- "Key the validators by identity" — unsafe; addresses are reused across `list_tools` calls.
- "Moving the build off the loop takes all 42.5 ms" — it takes about two thirds.

Corrections are in the review document, in place, each with the measurement that overturned it.

### Costs a human must accept

- Front-door CPU request 500m -> 1 (3 -> 6 CPUs reserved at max replicas).
- MCP fleet baseline ~0.7 CPU / 2 Gi -> **8 CPU / 10.5 Gi**, HPA ceiling 23 CPU / 26.5 Gi.
- Postgres must be provisioned for 288 connections; the old 136 was never real (~208 already).
- Fleet turn ceiling 48 -> 72, **and the HPA can now reach it** — real LLM spend.
- ~112 MB of new index; UI image +13 MB.
- A saturated-backend calc job now takes ~28 min of backoff before failing instead of failing at once.

### Still open

- An ADR for the exact-versus-approximate fingerprint decision.
- Four `bo` tools are 12,055 tokens of the prefix — four inlined copies of one model.
- `CHEMCLAW_FRAMING_ENVELOPE_SECRET` must be set for server-side prefix caching to hit across pods.
- `_DELETE_BATCH_ROWS` is a module constant; promote to config if the batch size should be tunable.
- A `serverConfig.test.ts` flake in the UI, pre-existing, ordering-dependent.

## 2026-09-05 — five defects an adversarial review found in the context-floor work

Scoped to `tests/test_bo_tools.py`, `tests/test_context_floor.py`, `tests/test_compaction.py`,
`tests/test_context_budget.py`, `core/config/agent.py`, `agent/context_budget.py`, `.env.example`,
`deploy/helm/chemclaw/values.yaml`.

- [x] **1 (blocking).** Restore the three `bo` schema tests `7576713` deleted while keeping the
      narrowing `70912a0` landed. All three re-run against HEAD; one needed adapting (the sentence
      it asserts wraps across a source line, so the served prose is whitespace-normalised before
      the `in` check — the property is that the sentence travels, not where it wraps). Each proved
      able to fail by a mutation of `science/bo/problem.py` in a restored-afterwards copy.
- [x] **2 (major).** `agent_context_token_budget = 133,000` permits a request no 128k model
      accepts. Capped to 120,000, derived against the smallest window this stack targets, and the
      chart now states `CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS` so the second bound is real.
      Both halves asserted in `tests/test_compaction.py`.
- [x] **3 (major).** `SERVED_ELSEWHERE`'s 9,538 was stale on the day it was written; re-measured
      **9,864**. Figures corrected, and the allowance is now guarded by a test that measures the
      sibling checkout for real and skips *loudly* when it is not there.
- [x] **4 (minor).** The budget's half of the derivation is asserted, so collapsing the split
      fails a test instead of passing 150 of them.
- [x] **5 (minor).** The stale numbers inside `agent/context_budget.py` are gone or point at what
      measures them.
