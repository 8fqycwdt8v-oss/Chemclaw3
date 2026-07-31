# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/planning/implementation-plan.md`
(phase/step numbers) at session end.

## Open — Left open by the durable job record (2026-07-31, D-157)

The record closed "a finished run's data, and the reason for it, survive nowhere". Three things it
deliberately did not close, each because it is a design rather than a line of code.

- [ ] **A failed run leaves no record.** The child failure propagates out of `ConnectorJobWorkflow`
      before the write, so `job_records` holds successes only — and "what have we already tried that
      did not work" is exactly the retrospective question the table exists to answer. Needs three
      decisions before code: which status a row carries (a run that failed *after* several rounds is
      not the same as one that never started), where the write happens (the failure path is an
      exception, not a return value, so it is a `try/finally` around the child or a
      workflow-level handler), and whether a later successful re-run under the same id supersedes the
      failed row or joins it. The attempt is already in `audit_events`, so nothing is lost today
      beyond the campaign's partial history — [M].
- [ ] **`request_development_report` writes no record.** It does not run through
      `ConnectorJobWorkflow` (D-115 kept it in core), so it would need its own write site — either by
      lifting the record write into a helper both call, or by moving the report onto the wrapper. Its
      gap is the smaller one: a report's artifact is a PR-gated note whose headings state its
      subject, where a campaign's artifact was a single best point. Same for anything else that ever
      starts a durable workflow outside the seam — [S].
- [ ] **Temporal namespace retention is still unset.** Nothing in the repo configures it, so a
      deployment inherits the server's default. D-157 removed the *dependence* on that number (the
      result no longer lives only in history) but not the ambiguity: an operator reading the runbook
      still cannot say how long a running deployment keeps workflow history. It is one Helm value
      plus a runbook line, and it wants a stated policy rather than a copied default — [S].

## Open — Every capability exercised live with the flags on (2026-07-31, D-155)

Full record: `docs/archive/live-matrix-2026-07.md`. The whole stack up natively with **every**
off-by-default flag enabled, driven with real Anthropic traffic and one signed identity per probe,
plus three parallel code reviews. Eight defects fixed under D-155; what follows is confirmed and
deliberately not fixed there, each because it needs a decision rather than a patch.

- [ ] **DARK-1 [High] — the harness plan-approval gate authorizes a session, not a plan.**
  Reproduced live: approve a four-item plan (`mode` flips to `execute`, a `plan_approvals` row is
  written), then ask a *completely different* question in the same session. `GET /sessions/{id}/plan`
  reports a new `plan_hash` with `approved=false`, the session is still in `execute`, and the turn
  autonomously ran `compute_xtb_energy` and `propose_knowledge_note` — a graph write — with no
  approval for that plan. `PlanApprovalStore.decision` is read in exactly one place,
  `api/app.py`'s *display* route; no execution path consults it. The only thing gating the loop is
  MAF's session mode, and nothing ever returns a session to `plan`. A recorded rejection after an
  approval therefore does not revoke it either, contrary to migration 020's stated contract.
  Two coupled decisions block the fix, which is why it is here and not in D-155:
  **(a)** `current_plan_hash` hashes the *rendered* todo lines, so completion state is part of the
  identity and the hash changes on the first ticked box — execution cannot be bound to a hash that
  moves as it executes. Binding it means deciding that what a human approves is the set of work
  items, not their completion state, which reverses a documented choice.
  **(b)** the store is Postgres-backed, so a deployment without it (the CLI harness) must be decided
  fail-open or fail-closed. Fail-closed is right for a GxP gate and breaks `make chat --admin`.
- [ ] **DARK-2 [High] — a template step is a route around `authorize_trigger` and the audit trail.**
  `durable/template_activities.py`'s `ResolvedJob` drops `expensive` and `precondition`, and the
  connector-tool branch calls `connector.call_tool(...)` with neither `enforce_tool_authz` nor
  `make_audit_middleware` — which the in-process branch two lines below hand-applies, and which the
  module docstring says is the point of the module. So a template naming `compute_dft_energy` starts
  HPC work for anyone who may run its `run_<name>` tool, and both tool steps of the shipped
  `hazard-briefing` leave no GxP audit row. Needs a decision on whether a template runs with the
  requester's entitlements (then thread them through) or as a declared service identity (then say so
  and audit it as such).
- [x] ~~**DARK-3 — mid-turn resume claims other jobs' completions and drops them.**~~ — **fixed on main by D-153** while this pass was running. `await_job_results` no longer consumes the push-back mailbox at all: each job is awaited on its own Temporal handle, so there is no destructive claim to steal another job's completion with. Recorded rather than deleted because the review found it independently against the pre-D-153 tree.
- [ ] **DARK-4 [Med] — the durable job idempotency key omits every versioned input.**
  `job_workflow_id` hashes `[connector, job, payload]` only. Change `xtb_method` or a calibration
  constant and the calculation store correctly misses and recomputes, while `start_workflow` raises
  `WorkflowAlreadyStartedError`, rejoins the *completed* prior run, and returns numbers produced by
  the old method. `science/calc/store.py` takes the opposite and correct position for the same
  computations (`calc_version` is in the key). Fix needs to decide what "the version of a job" is.
- [ ] **DARK-5 [Med] — retention is one transaction over an unindexed column.** The docstring claims
  each table is pruned "in its own statement so one failure cannot roll back the others"; there is a
  single commit after the loop. And `session_events` has no index on `created_at` — the gap
  migration 022 closed for `session_messages` — so under the 30 s statement timeout the sweep starts
  failing permanently once the table is big enough to need it. Wants a migration.
- [ ] **DARK-6 [Med] — `verify_chain` loads the whole audit table into memory.** No LIMIT, no
  watermark, `fetchall()`. This is the one table retention refuses to prune, so the scheduled check
  eventually times out or OOMs the shared background worker.
- [ ] **DARK-7 [Low] — the digest re-reports every note at least twice.** `_is_new` compares a
  `date` against `last_seen_at.date()` with `>=`, so a note whose `valid_from` is the day of the
  last report re-qualifies; at an hourly cadence the same note is sent 24 times, against
  `subscriptions.py`'s promise that "asking twice does not double-notify".
- [ ] **DARK-8 [Low] — `embedding_dim` is cross-validated only when the `vector` source is on**, but
  `reindex_notes` writes the embedding column unconditionally, so a `lexical`-only deployment with a
  768-wide model passes config validation and fails every reindex on a pgvector dimension error.
- [ ] **DARK-9 [Low] — a reported measurement with no matching prediction is silently discarded**
  while the tool reports success. `_RECORD_OBSERVATION` is a bare `UPDATE` with no insert path;
  `record_observation`'s own docstring says the caller logs the zero-row case and the caller does
  not. This is the common case for new chemistry.
- [ ] **DARK-10 [Low] — the PR-gate's checkout window exposes unreviewed notes to readers.**
  `knowledge_path` is the same tree the submitter runs `checkout -B note/<id>` against, and
  `invalidate_cache()` is called *inside* that window, so a concurrent turn can retrieve an
  agent-proposed, unreviewed note as authoritative evidence. `_return_to_base` fixed the permanent
  version of this; the transient one spans a commit, a fetch and a push.
## Done — The daily experiment progression (2026-07-31, D-162)

Asked whether the system could read a technician's week-by-week series on one step and propose the
next run without BO. Most of it was already there; three data gaps were not, and are closed:

- [x] **PROG-1** Chronology. `memory/progression.py` orders a series by `performed_at` and names
      what changed between consecutive runs; the `optimization-campaign` note gains **Performed**
      and **Changed vs previous** columns and states in words when its row order is *not* a
      timeline. `performed_at` existed and reached no artifact the agent reads.
- [x] **PROG-2** Intent. `OrdReaction.hypothesis` (mapped by the JSON ELN adapter, rendered first
      in the reaction note) and the `follows` relation — minted by whoever can read the intent,
      never derived from two dates.
- [x] **PROG-3** The reasoned path. `experiment-progression` skill + the `experiment-proposal` note
      type through the existing PR-gate; `deep-research` §6 and `experiment-design` now name the
      fork between reasoning and BoFire and require the answer to say which it took.
- [x] **PROG-4** `since`/`until` on `gather_evidence` and `_eligible_notes`, so "what have I tried
      in the last two weeks" reaches the dates already on the notes.

Not addressed, and worth stating: nothing here reads an instrument trace or correlates impurity
profiles across runs beyond what the notes say in prose.

## Open — Surfaced by the deferral-register rewrite (2026-07-31, D-154)

Rewriting `docs/planning/DEFERRED.md` into a register meant checking each row against the tree. Two
things fell out that are work rather than bookkeeping, and neither belonged in a docs cleanup.

- [ ] **Two compound-id conventions in one graph.** The seeded corpus (D-135) names its nine compound
      notes by slug — `knowledge/compound/compound-thf.md` — while the machine path mints
      `compound-<hash>` from the canonical structure (`core.chem.compound_id`, applied at the gate by
      `eln.compound.compound_dependencies`). **Not a dangling citation today:** a molecule hit exists
      only for a row in the fingerprint index, which only ingestion writes, and ingestion mints the
      hash-id note — so the two sets do not meet. It is a *duplicate-identity* hazard on the ingest
      path: ingesting 4-bromoanisole mints `compound-24c67ba8f741` beside the seed corpus's
      `compound-4-bromoanisole`, two notes for one molecule, and the reaction/job-result notes citing
      the slug keep pointing at the older one. The structure-derived id is the one that can be
      *derived from a hit*, so a hand-written slug cannot be the convention; renaming the nine notes
      means editing the eight notes that cite them plus three test files, and it is a corpus-convention
      change that wants its own argument. A `kg-validate` rule (a compound note's id equals
      `compound_id(compound_smiles)`) is what would keep it from recurring — [M].
- [ ] **Per-step species linking from free-text prose** — moved here from `DEFERRED.md`, where it was
      listed as blocked on a name→SMILES tool. That tool exists (`core/reagents.py`, whose docstring
      names this as the thing it unblocks), so this is unscheduled work, not a deferral: wire the
      `eln-reaction-extraction` skill's per-field LLM to resolve named reagents per step, still
      PR-gated. The deterministic floor stays what it is — a coarse `StepKind` plus per-step
      temperature/time — because guessing a SMILES from a name mid-sentence fabricates structure,
      which is the one failure mode worse than the gap — [M].

## Open — Fifty live expert questions (2026-07-28, D-138)

Full record: `docs/archive/vibe-test-2026-07.md`. Fifty questions from a process/analytical development
scientist and their project manager, asked against the running stack. Five defects found, five
fixed; two left open below with the reason. Method note worth keeping: four of the five were
invisible to 1450 passing tests because in each case *the test supplied the thing the system was
supposed to supply*.

- [ ] **VIBE-1 — a durable job's domain error does not reach the model.** With the launcher fixed,
      `compute_reaction_energy` launched and `CalcJobWorkflow` correctly rejected an unbalanced
      equation, but the tool raised `WorkflowFailureError: Workflow execution failed` and the
      actionable message — "reaction is not atom-balanced (reactants minus products): C +2, H +4,
      O +2" (`calc/reaction.py:178`) — stayed in the worker log, so the model could not repair its
      own input. Two parts, and they are separable: (a) the balance/charge check is a
      *precondition* in the sense `JobSpec.precondition` means, so running it before launch would
      both relay the message through `surface_domain_errors` today and stop the five pointless
      Temporal retries; (b) relaying a workflow's failure text in general is a policy decision
      about what is safe to surface — the question `surface_domain_errors` answers by naming
      known-safe types — and wants deciding, not patching. Do (a) first; it may be enough.
- [ ] **VIBE-2 — `resolve_compound` knows solvents and bases, not substrates.**
      `chemclaw/reagents.py` holds 87 spellings, almost all reagents. Every substrate in the
      corpus misses (`4-bromoanisole`, `phenylboronic acid`, `salicylic acid`,
      `4-methoxybiphenyl`), and the model then supplies the structure from memory — right each
      time observed, which is precisely the risk, since a wrong structure propagates silently into
      every downstream calculation and search. The structures exist: `knowledge/compound/*.md`
      carries `compound_smiles`, and several notes even carry an `also written:` line nothing
      parses. The reason this is a design question and not an oversight: the resolver runs inside
      the `chem` bundle, which must not import the knowledge graph (D-115), so the fix is about
      *how* project vocabulary reaches a connector — a generated overlay, a config-pointed
      synonyms file, or a core-side resolution step — not about adding names to a dict.
- [ ] **VIBE-3 — the answer event carries the model's inter-tool narration.** `AnswerEvent.text`
      concatenates every assistant text block in the turn, so an answer reads "I'll resolve the
      compound…Let me correct that…Perfect. Here's what you have:" before it starts. Harmless when
      the turn succeeds; it is what made the failed Q11 read as a broken thought stream. Decide
      whether the final block alone is the answer and the rest is trace.

## Open — Agentic system review (2026-07-28, D-136/D-137)

Full record: `docs/archive/audit/2026-07-agentic-system-review.md`. Three shipped defaults were fatal on
first contact and are fixed; the rest is open, in priority order. The review's method note is the
part worth keeping: a shipped default is a claim about the world, and the only way to check a
claim about the world is to run it.

- [x] **REV-1 [Critical] The pre-execution approval gate does not exist.** MAF injects `mode_set`
      into the model's own tool surface with `approval_mode="never_require"`, so in `plan_only` —
      the shipped production configuration — the model flips itself to execute. Nothing binds an
      approval to a plan, and the audit trail records the flip under the *chemist's* Entra oid.
      `plan_mode_required_for`, which `docs/guides/harness-konzept.md` §6 specifies, exists nowhere in the
      code. Fix shape: drop `mode_set` from the injected surface, move the flip to an owner-scoped
      route recording `(session_id, plan_hash, actor, decided_at)` — mirroring
      `POST /approvals/{id}/decision`, which already got this right for jobs. Needs an ADR.
      **Done (D-137).** `PlanApprovalModeProvider` retracts the `mode_set` tool MAF injects, and the only path into execute mode is now the owner-scoped `POST /sessions/{id}/plan/decision`, bound to a hash of the plan the human was shown and recorded in `plan_approvals`.
- [x] **REV-2 [High] Nothing scrapes `/metrics`.** No ServiceMonitor, PodMonitor or scrape
      annotation anywhere under `deploy/`. Every metric in the system is uncollected in production.
      **Done (D-143).** A ServiceMonitor on the front-door Service, selecting it by the `http` port *name* so a port change cannot orphan the scrape. Front door only: workers and connectors record through `chemclaw.metrics_bridge`, whose contract is that a metric recorded outside the front door is a no-op, so a scrape pointed at them would collect nothing and report up. `additionalLabels` is left empty for the operator's `serviceMonitorSelector`, which is release-specific. A test asserts the scraped *path* is a route the app actually serves — the D-142 shape, since a ServiceMonitor naming `/metric` renders, validates, deploys and collects nothing forever.
- [x] **REV-3 [High] The two `expensive: true` CREST jobs heartbeat once** against a 600 s
      heartbeat timeout. `run_cached_ensemble`/`run_cached_interaction` have no `progress`
      parameter at all, so this is plumbing, not a kwarg. Each retry restarts CREST from zero
      (the store is written only on completion): ~50 min of saturated CPU to fail a job that would
      have succeeded. Third instance: `calc/reaction.py` at `level="thorough"`.
      **Done.** `_beating` in `connectors/calc/activities.py` awaits the CREST work on a timer derived from the heartbeat timeout. A timer, not a progress callback: a single subprocess has no unit boundary to report at, and "still running" is the honest signal.
- [x] **REV-4 [High] After-run compaction is a silent no-op under `session_store=postgres`** (the
      production default). MAF reads `session.state[source_id]["messages"]`, whose only writer is
      `InMemoryHistoryProvider`. So `session_messages` is read with no LIMIT every turn and a
      long-lived session re-reads its whole history before every model call. The docstring promises
      the opposite. **Confirmed by reading MAF, and the obvious fix is unsafe.**
      **Documented and pinned, not fixed (D-143).** Confirmed exactly as described. Two corrections to the framing: the `before_run` half *does* work under Postgres, so the model's input is still bounded and this is not a context-window bug — what is unbounded is the per-turn read and the forever-growing stored history. And **a `LIMIT` on the load would corrupt data**: `get_messages` repairs unmatched tool-call pairings by *writing back*, and over a windowed read a `tool_result` whose `tool_use` fell outside the window is indistinguishable from a real orphan, so the repair would strip and commit a pairing that was intact on disk. Both docstrings that promised the opposite are corrected, and `tests/test_durable_compaction_gap.py` pins the no-op *and* the write-back hazard.
      **Still open:** the real fix, which is either (a) make the read-repair in-memory-only when the load is partial, then bound the read, or (b) durable compaction that prunes whole tool-call groups from `session_messages`. Either is a design change to a durable path with a data-loss failure mode and wants its own ADR.
      **Done (D-151).** `save_messages` now runs the *same* strategy against the table after storing a turn — inline rather than on a schedule, because that is where MAF intends after-run compaction and the turn claim already guarantees one writer per session. Measured over 60 turns: uncompacted the table grows by exactly 4 rows/turn to 240; compacted it sits in a band (14 → 23 → 22 → 18) bounded by the window, not the turn count. Off by default, matching `retention_enabled`. `get_messages` is untouched — no `LIMIT` — because compaction never reads a partial history and so sidesteps the corruption class rather than accepting it.

- [x] **REV-5 [High] `retrieval_recall`/`retrieval_precision` are absent from `evals/baseline.json`**,
      so the only metrics that run a live retriever have zero drift coverage — verified by
      collapsing both to 0.0 and getting no alert. Also give `save_baseline` a Makefile target; it
      has no caller today, which is how the two metrics drifted out.
      **Done.** Both metrics are in `baseline.json`, regenerated by `scripts/refresh_baseline.py` (`make eval-baseline`) rather than hand-edited — `save_baseline` had no caller, which is how they drifted out. A test asserts both are present and that a collapsed score now alerts.
- [x] **REV-6 [Med] `open_reachable`'s unreachable-connector list is discarded by all four
      callers**, though its docstring says it is "for the caller to surface". A turn answers with a
      silently degraded capability set; in `template_activities` the output enters the PR-gate with
      no marker.
      **Done (D-139).** The announcement moved *into* `open_reachable` — a WARNING naming the connectors plus `chemclaw_connectors_unreachable_total`, counted per connector — because a return value that must be read had been forgotten four times out of four. The front door additionally yields a `CapabilityDegradedEvent` before the first token; the CLI prints to stderr, which its docstring had promised and never done. Still degrades rather than raising: one dark connector must not become a dead front door.
- [ ] **REV-7 [Med] A push-back event lost between claim and delivery is lost permanently — and
      the fix is *not* the one this item first proposed.** The original recommendation (yield before
      marking rows consumed) is **refuted**: `agents/session_events.py` documents at-most-once as a
      deliberate trade made by COR-4, which *replaced* an at-least-once claim that double-delivered.
      Reordering would reintroduce the bug COR-4 closed. The residual risk is real — the claiming
      `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` is atomic, so a consumer that dies in the window
      between claim-commit and the event reaching the client drops it with nothing to retry — but
      closing it needs a **visibility-timeout redelivery**: claim with a lease and a delivery
      deadline, confirm on delivery, re-offer on expiry. That keeps COR-4's single-claim property
      (two tailers still cannot both hold a row) while making loss recoverable. A design change to a
      durable path, wanting its own ADR, not a reordering.
      **Partly done (D-153).** The *second* defect in this area is fixed: `await_job_results` tailed the mailbox, whose claim is destructive, so a mid-turn resume waiting on job A consumed job B's push-back and discarded it — the front door's stream never saw it. It now asks Temporal about its own job ids and never touches the mailbox, so there is no shared queue to race over. Also strictly more informative: the model resumes with the `ConnectorJobResult` envelope rather than the one-line summary the event payload carried.
      **Still open — and the cheap fix is refuted.** "Select, yield, then confirm" does not work: `stream_new_events` polls on a timer with no `try/finally`, so an event yielded but unconfirmed is re-selected every poll (`test_tailer_releases_its_connection_between_polls` would see ~37 deliveries instead of 1). Preventing re-selection *is* a visibility timeout. The fix stays as recorded above, and additionally needs a **per-stream** holder id (`_WORKER_ID` is per-process, so two streams in one pod would steal each other's leases) and a confirm shielded against cancellation (D-130's trap — the confirm is reached from a cancelled generator). It is an operator-facing contract change too.

- [ ] **The `eval_drift` push-back channel has no consumer.** `chemclaw.durable.eval_drift` writes
      `eval_drift` events to the `system-eval-drift` channel must-deliver, and nothing in the repo
      claims them, so they sit unconsumed until retention (off by default) prunes them by age. Not a
      bug: the channel constant's comment says it is "a `session_events` 'session' an operator
      surface tails" — the consumer is *unbuilt*. Either build that surface, or route drift alerts
      to the log-plus-counter path that `/metrics` now actually scrapes (D-143).

- [x] **REV-8 [Med] CHAOS-1: the blocker named in this file is the wrong object.** Not the
      in-process `active_turns` set (`discard` is synchronous, before the await) — it is the 60 s
      `session_turns` lease. `_release_turn_claim` catches `RuntimeError`, which is what Python
      raises when a closing async generator awaits something that suspends. The measured 63 s
      matches `service_turn_claim_lease_seconds`. Both previously discarded theories were about the
      wrong object; the detached-task experiment failed because the task had no strong reference.
      **Done by main (D-130)** — turn teardown is shielded so its cleanup runs in a cancelled task. That is the root cause this review identified: the release was an await in a closing generator and the `RuntimeError` was swallowed.
- [ ] **REV-9 [Med] Prompt caching: a large fixed prefix is re-paid every model call** — but
      **measure before building** (D-152), and this entry as first written overstated how reachable
      the saving is. Two corrections from verifying it:
      **(a) the ~14.6 k prefix was measured on the wrong provider.** That figure came from the
      Anthropic dev path. Production is `openai_compatible`, where `agent_framework_openai` contains
      **zero** occurrences of `cache_control` — the mechanism is not reachable from production at
      all, so this is upstream work in MAF, not a knob here.
      **(b) "the ~3.5 k system half is cacheable" is false through `Agent`.** `SkillsProvider` merges
      the skills manifest into the instructions with an f-string, which would `repr()` a structured
      block list into a string. Marking that half cacheable is also an upstream change.
      Still true: MAF exposes no `cache_control` hook for `tools` (the 11 k that dominates), and the
      prefix is not byte-stable because `tools/list` is re-fetched per turn, so one flapping
      connector invalidates it.
      **What to do now instead of building:** read `chemclaw_cache_read_tokens_total` against
      `chemclaw_input_tokens_total` on `/metrics` — the provider may already be caching the prefix
      unasked, in which case there is nothing to build. `docs/guides/runbook.md` §(viii) has the
      procedure and what each outcome implies. `chemclaw_cache_write_tokens_total` is structurally 0
      on `openai_compatible` and must not be read as a fault.
- [x] **REV-10 [Med] Token accounting is priced-blind.** `chemclaw_tokens_total` collapses input
      and output before the counter sees it; cache-read/write are not read at all; the registry
      supports no labels, so no per-model or per-profile attribution. AG-11 (cost) still open. MAF
      already implements the full GenAI token model — reachable now that OTel can start.
      **Done (D-144), the pricing half.** Four counters for the four priced dimensions, with `chemclaw_tokens_total` kept as the total. The budget guard still meters the total, so the 429 behaviour is unchanged — this splits what is published, not what is enforced. Cache counts are *not* folded into `input` (a provider reporting them has already excluded them, so folding would re-price cheap tokens as expensive), and a counter stays untouched rather than publishing a fabricated `0` when the provider reports nothing — the REV-19 rule.
      **Done (D-152), the attribution half — and half of it turned out to be already solved.** Per-*model* attribution needs nothing built: MAF emits `gen_ai.client.token.usage` labelled by request model, response model, provider and token type, and the shipped chart turns OTel on. Duplicating that axis in this registry would mean two systems to reconcile, so it is deliberately not done — with two gaps recorded: MAF records only the `input`/`output` token types, so D-144's cache-read/cache-write dimensions are *not* in that histogram, and OTel has no notion of a Chemclaw `profile`. Per-*profile* attribution is the real gap and is what shipped: the registry gained declared labels (an undeclared label name raises exactly as an undeclared metric does, because a label typo's failure mode is a second silent time series rather than a crash), a per-counter series cap against the unbounded-map leak this codebase has already fixed three times, and the five spend counters carry `profile`. `/metrics` is unauthenticated, so `test_metrics_carry_no_identifiers_or_turn_content` became an allowlist of *declared* label names rather than "`le` is the only label": a profile is configuration, low-cardinality, and not user-derived.
- [x] **REV-11 [Med] `correlation_id` stops at the process boundary.** Not in the connector
      identity headers, not in `ConnectorJobInput`, not into HPC. ~4 lines to make the audit trail
      joinable across all four runtimes. Note that fixing OTel does not fix this.
      **Done (D-141).** An `X-Chemclaw-Correlation-Id` header beside the actor/roles/session, and a `correlation_id` on `ConnectorJobInput` that becomes a workflow memo beside `requested_by`. Both follow the shape already established for the actor: advisory-never-authorization for the header, in the input rather than ambient for the job (a workflow has no request context), and a memo rather than `payload` so it is not something the model can write. HPC is unchanged — the bridge runs under a shared service identity and wants its own pass.
- [x] **REV-12 [Med] Prediction calibration pools every calculator version.** `calc_version` is
      never passed when recording, so the unique index degenerates and a v2 prediction destroys
      v1's row; the read path has no version predicate either. `calculator_trust` reports the
      pooled figure. Dormant while `calibration_enabled` is off.
      **Done (D-139).** Both halves — the tools pass the running version, and `calibration_for` now *requires* one and filters on it. The observation write stays version-blind on purpose: a measurement is a fact about the molecule, which is what makes a version-over-version comparison possible. Verified against live Postgres by simulating the pooled read, where a high version and a low one cancel to a bias of exactly 0.0.
- [x] **REV-13 [Med] `find_job` does filesystem I/O inside workflow code**, and the comment above
      it says it is I/O-free. `ConnectorError` is a `ValueError`, not a `FailureError`, and no
      `failure_exception_types` is declared — so it fails the *workflow task* and Temporal retries
      indefinitely. The run hangs rather than failing. No test constructs a `JobStep`.
      **Done (D-140).** The lookup moved to a local activity, `resolve_job_step`, following `orchestrator.resolve_fan_out_limit`'s precedent — the resolution is now recorded in history rather than re-read from the replaying worker's disk. That also turns the `ConnectorError` into an `ActivityError`, which `BAD_DATA_RETRY` fails on the first attempt. `TemplateWorkflow` gains `failure_exception_types=[Exception]` for the sequencer's own raw raises. `tests/test_template_job_step.py` is the first test to construct a `JobStep`.
- [x] **REV-14 [Med] Rehydrated and LRU-evicted sessions revert to the default profile**,
      permanently. The profile is never persisted. Eviction matters more than restart: no TTL, so
      session 1001 evicts session 1. All three rehydration tests discard the profile argument.
      **Done (D-141).** Persisted as a nullable column on `session_owners` (`infra/sql/021`) and rehydrated onto. The old comment called the loss graceful — "the conversation resumes with the full tool surface rather than a narrowed one" — which has the direction backwards: a profile is attenuation only, so restoring the full surface is the control being switched off, and the LRU has no TTL so it happens on a live pod without any restart. `None` surviving as `None` is pinned separately; storing `""` would ask for a profile named empty-string.
- [x] **REV-15 [Med] Chart parity test proves nothing about behaviour.** It constructs
      `Settings(**helm_values)`; `otel_enabled=True` constructs perfectly and then kills the pod.
      Two holes: keys from `templates/config.yaml` (`note_repo_dir`, `connector_urls`) are outside
      it, and there is no inverse test that a production value is *executed*. This is the test
      class that would have caught two of the three Criticals.
      **Done (D-142).** The derived keys are discovered from `templates/config.yaml` and rendered offline, and `connector_urls` is now *asserted*, not merely constructed — a render of `{}` builds a valid `Settings` while pointing the front door at nothing. Writing it surfaced the sharper point: pydantic-settings JSON-decodes a complex field from an env var and **not** from an init kwarg, so the old model of "the pod environment" was the wrong mechanism for these keys, not just incomplete. The inverse direction now has tests too (below).
- [x] **REV-16 [Med] Dark-by-default flags that arguably should not be.** `budget_enabled` off
      (the load test that validated the system ran with budgets *on*); `audit_verify_enabled` off,
      so the tamper-evident chain is never verified; `connectors_required` off.
      **Done (D-142), two of three.** `budget_enabled` and `audit_verify_enabled` are on in the chart, each pinned by an *executed* test rather than by asserting the flag. `connectors_required` deliberately stays false: unlike the other two its docstring is a real considered trade, and the review's argument for flipping it — that the degradation was silent — stopped being true when D-139 landed `CapabilityDegradedEvent`, the WARNING and the counter. Fail-fast would now trade availability away for visibility that already exists.
- [x] **REV-17 [Med] `deployment_revision` can never be set in production** — no chart key,
      Containerfile ARG or build step sets it, though its docstring says the image build injects
      the digest. AG-14 is unmet while reading as done.
      **Done (D-140).** A `CHEMCLAW_REVISION` build ARG exported as `CHEMCLAW_DEPLOYMENT_REVISION`, with the image workflow passing the commit SHA — a build arg rather than a chart value because the image is the thing that has a revision, and one that disagrees with the running bytes is worse than an honest "unknown". The wiring is pinned offline in `test_deploy_chart.py`; the image job runs the built image and compares, because only that proves the value arrived.
- [x] **REV-18 [Low] Missing validators** for combinations the config comments already forbid in
      prose: `session_store="memory"` with `uvicorn_workers > 1`, `mid_turn_resume_timeout >=
      turn_timeout`, `budget_enabled` with all caps zero, `embedding_dim` vs the `vector(N)` column.
      **Done.** Four validators on the composed `Settings`. The `embedding_dim` check is scoped to `"vector" in data_sources`: unconditional, it rejected three hash-embedder unit tests that never touch pgvector — the tests were right and the validator was wrong.
- [x] **REV-19 [Low] `chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` are never
      incremented** — a permanent `0`. The gauge path refuses to fabricate zeros; counters get no
      such protection. Increment them or delete them.
      **Done (D-139).** Incremented at the durable-job launch and the PR-gate proposal. The note counter moves *after* the submitter returns: counting the attempt would report a healthy gate during exactly the outage the metric exists to reveal. `agents/audit.py`'s private `_record_metric` was promoted to `chemclaw/metrics_bridge.py` at its fourth caller rather than imported by its underscore name.
- [x] **REV-20 [Low] Anthropic client ignores `llm_timeout_seconds`/`llm_max_retries`/CA bundle.**
      Actual timeout is the SDK's 600 s, not the configured 60 s. Default for CLI and dev.
      **Done.** `AsyncAnthropic` now carries `llm_timeout_seconds`, `llm_max_retries` and the CA bundle. Verified against the live API.
- [x] **REV-21 [Low] Docs disagree with code:** `harness_max_loop_iterations` is 25, not the 15 in
      `docs/guides/harness-konzept.md`, and the cap applies in both modes, not only execute.
      `workflows/template_job.py` calls its own lookup "I/O-free". `agents/chemclaw_agent.py`
      calls `plan_only` "the pre-execution GxP gate" (REV-1).
- [ ] **Heal sessions already bricked by a stranded `tool_result`.** `get_messages`'s repair
      strips orphaned *calls* and cannot see an orphaned *result* (D-145), so any session the old
      age-based retention split is unusable forever with no automatic recovery. Adding the mirror
      strip to the read repair would fix them — deliberately **not** shipped with D-145, because
      doing so would mask a regression in `droppable_rows` rather than surface it. Needs its own
      argument: it is a new destructive behaviour on the read path, and it destroys evidence of how
      the split happened.

## Storage & knowledge substrate (docs/archive/audit/13-storage-and-knowledge-audit.md)

The layer under retrieval and capability, audited as one system for the first time: what the
system writes down, what it throws away, and what it cannot reconstruct. Twelve of the fourteen
findings are closed (D-124, D-132, D-133, D-134, D-135); what remains is listed at the end of the
section, and STO-5/11/13 stay deliberately open.

      **Done.** `harness-konzept.md` says 25 and "both modes"; `template_job.py` no longer calls its lookup I/O-free; `chemclaw_agent.py` names what enforces the gate now that D-137 makes the claim true.
- [x] **STO-1 [High] Every expensive by-product was deleted with its tempdir.** **Done (D-124,
      read path added by D-132):** content-addressed `artifact_blobs` + `calculation_artifacts`
      (migration 019), `calc/artifacts.py` + `calc/postgres_artifacts.py`, capture derived from
      `_REQUIRED_OUTPUTS`. Correction to the original finding: the optimizer and CREST have **no**
      by-product worth keeping — `xtbopt.xyz` and the CREST ensemble are parsed in full into the
      cached result, so capturing them would be a second copy of the cache (`_ALREADY_STORED`).
- [x] **STO-2 [High] A stored Hessian could not be reused.** **Done (D-132):** `HessianSpec` split
      out of `ThermoSpec` (`calc/xtb_hessian.py`); the matrix is a `.npy` artifact and the row
      holds its address. A second temperature is now a miss on the thermochemistry and a hit on the
      Hessian. A cached row whose artifact is gone is treated as a miss, which is what keeps
      eviction a reclaim rather than data loss.
- [x] **STO-3 [High] `max_members` was in the conformer cache key.** **Done (D-132):** excluded via
      `XtbSpec.unkeyed_fields()`; `run_cached_ensemble` truncates at read. Cold-starts existing
      `xtb.conformers` rows and stores the whole ensemble, which `total_found` already counted.
- [x] **STO-4 [Med] No cross-method geometry reuse.** **Done (D-132):** `calc/geometry.py` records
      a subject-keyed pointer as an ordinary cached calculation. `run_cached_optimization` writes
      it and deliberately does not read it — the reuse is an explicit `starting_geometry` lookup,
      so a cache key always names what actually ran.
- [ ] **STO-5 [Med] No converged electronic structure kept** — [L]. Deferred with DFT (D-010).
      D-124/D-132 define the media types (`density.restart`, `orbitals.molden`) and the link role;
      nothing writes them. Published measurement: reusing a converged density cuts mean SCF
      iterations from ~33 to ~2.
- [x] **STO-6 [Med] The cache had no cost policy.** **Done (D-124/D-132):** `compute_seconds` is
      recorded on every miss and `workflows/artifact_eviction.py` consumes it — evicting blobs by
      cost-over-idle-time and by size ceiling, never `calculation_results`, so D-011 and
      `workflows/retention.py`'s standing refusal both remain literally true.
- [x] **STO-7 [High] Computed results were graph islands.** **Done (D-133):** `NoteSubmission`
      carries `files: list[NoteFile]`, so a note lands with the notes its links depend on;
      `eln.compound.compound_dependencies` applies the rule once at the gate; `calc_refs`/
      `artifact_refs` are shape-validated frontmatter (not wikilinks — they point out of the
      graph); `kg/crosslink.py` is the reverse lookup.
- [x] **STO-8 [High] Graph edges carried no relation.** **Done (D-134):** `[[rel:target]]` plus a
      frontmatter `relations:` list; vocabulary in `kg/relations.py` adopted from
      RXNO/CHMO/CHEMINF/OntoRXN and enforced at `kg-validate` like `KNOWN_NOTE_TYPES`. Stayed on
      `nx.DiGraph` with a tuple of relations per edge rather than moving to `MultiDiGraph`.
- [x] **STO-9 [Med] Bi-temporality stopped at the node.** **Done (D-134):** `Relation` carries its
      own `valid_from`/`valid_to`, honoured by `kg.graph.related(..., as_of=)`.
- [x] **STO-10 [Med] `knowledge/` was empty.** **Done (D-135):** 37 seed notes covering all ten
      types and all fourteen relations, with real instances of the awkward cases (a superseded
      pair, a declared conflict, calculation crosslinks). `evals/retrieval_corpus/` was correctly
      **not** promoted — a test asserts the two share no ids.
- [x] **STO-12 [Low] `embed_texts` re-embedded every query.** **Done (D-135):** bounded cache keyed
      on provider+model+dimension+text. The rest of "tool result caching" is confirmed a **non-gap**
      and left alone: every `calc` connector tool already routes through `run_cached`, and the
      `chem` tools are RDKit calls a Postgres round trip would make slower.
- [x] **STO-14 [Med] Vendored reference data.** **Done (D-135):** `sources/vendored/` behind the
      manifest seam, checksummed and licence-labelled, retrieve-only, off by default;
      `tests/test_no_egress.py` extended (not relaxed) to assert it can make no request. Ships the
      mechanism plus a first-party `common-reagents` table — **no third-party dataset is vendored
      yet**, which is a build-pipeline step plus a licence review.

**Follow-ups this work leaves open (not blockers):**

- [ ] **Vendor a real third-party reference corpus** — [M]. The mechanism is in place and
      documented (`data/vendored/README.md`); what remains is choosing a licence-clean source and
      adding the build step that installs it.
- [x] **Reconcile the ADR-number convention with its own test.** `CLAUDE.md` said "reserve it in
      your first commit"; `tests/test_decision_log.py` required the registry and the log to name
      exactly the same ADRs, which forbade it. The convention was not aspirational — `1f1f233`
      followed it and `8f6a319` had to undo it. Fixed at the root: a ledger row may read
      `RESERVED — …`, which is exempt from the registry-matches-log check and **not** exempt from
      the duplicate check, so the number is claimed the moment it is pushed. A new test fails a
      marker left on after the ADR merges, because a stale `RESERVED` row reads as a free number.

**Closed as not-gaps:** STO-11 (`embedding_provider="hash"` is the documented offline dev path),
STO-13 (audit-trail disposal stays refused — deleting from a hash chain is indistinguishable from
the tampering it detects; needs an ADR with QA sign-off, already in `docs/planning/DEFERRED.md`).

## Done — Process/analytical-development capability research (2026-07-26, D-092)

A survey of open-source ML/cheminformatics and fast-ab-initio packages for chemical and analytical
process development (new data-source connectors like LIMS out of scope), landed through the
existing connector seams only (fast calculator, BoFire adapter, Temporal workflow — no ad hoc
wiring). Full rationale, including two researched-and-rejected candidates (ML interatomic
potentials, retrosynthesis — both blocked on a runtime external-data fetch D-089 rejects, not on a
missing prerequisite), in D-092.

- [x] `predict_developability_profile` — RDKit-only Ro5/Veber descriptor panel (`calc/descriptors.py`).
- [x] `predict_logd` — pH-dependent logD from the existing cached pKa + Crippen LogP (`calc/logd.py`).
- [x] ~~`estimate_reaction_energy`~~ — **superseded (D-108)**. Its exotherm flag moved onto
      `compute_reaction_energy`, which computes the same difference over optimized geometries and
      enforces atom/charge balance. `calc/reaction_energy.py` removed.
- [x] `generate_screening_design` — full-factorial categorical DoE screen (`bo/engine.py::factorial_design`).
- [x] ~~`ConformerEnsembleWorkflow`~~ — **superseded (D-108)**. Conformer ensembles are
      `calc/conformers.py` (CREST metadynamics with rotamer degeneracies and conformational
      entropy) behind `sample_conformers`, routed through the one xTB durable job. The ETKDG
      implementation and its dedicated workflow/models/activities were removed.

> **Every open item below was assessed on 2026-07-25** — trigger held? real defect? offline-verifiable?
> KISS? — in **`docs/archive/plans/backlog-plan.md`** (verdict table + specs for the survivors + the working queue in
> `tasks/todo.md`). Verdicts: 8 BUILD (waves A/B/C), 14 DEFER, 5 DROP, 12 BLOCKED. The DROP verdicts are
> corrected in place below, because they were claims about the tree that are no longer true.

## Open — Production scale, after the 50-user load test (2026-07-27, D-119)

The load test's fixes landed (see D-119). What it surfaced and did **not** close:

- [x] **CHAOS-1 A session whose client walks away mid-turn stayed 409 for ~63 s.** Closed by D-130:
      **60.9 s → 0.0 s measured**, and 409 → 200 on a second replica.

      Three theories were tested across two sessions and **all three were wrong** — the in-process
      `active_turns` entry leaking, the abandoned turn running on, and the claim release simply
      needing to be detached. What settled it was sampling both guards once per second while
      polling: `chemclaw_turns_in_flight` was 0 from the first sample (the `finally` *did* run
      promptly) while the `session_turns` row counted down from exactly 60 s with no refresh. The
      recovery time was the lease, so the release had never landed. Tracing the store then showed
      it *entered* on every abandoned turn and *completed* on none: a bare `await` in a cancelled
      task raises at its first suspension point. Shielded now, with the error handling inside the
      shielded task so it cannot end as a stray `Task exception was never retrieved`.

- [x] **CHAOS-1b The disconnect rollback was dead code on the only path that reaches it.** Found by
      the same trace, closed by D-130. `service/runner.py` rolled a half-written turn back under
      `except GeneratorExit:` — what `aclose()` raises. sse-starlette answers `http.disconnect` by
      cancelling its task group and never calls `aclose()` on the body iterator, so the real path
      delivers `CancelledError` and the rollback never ran. It looked covered because
      `tests/test_turn_cancellation.py` tore every stream down by hand under the comment *"what
      sse-starlette does when the client disconnects"*. It is not. Both teardowns are now
      exercised; the clause also brings the turn deadline under the same rollback.

- [x] **CHAOS-1c The connector health probe ignored the deployment's address override.** Found by
      re-running the Stage 5e connector-kill scenario, which could not tell a killed connector from
      a mis-probed one. Closed by D-131. `connector_urls` moved the tool endpoint and left the probe
      on the manifest's loopback dev default; the shipped chart always sets that override, so in a
      cluster `/readyz` reported every connector unreachable however healthy it was — and under
      `connectors_required: true` the front door would have failed to start every time. Measured
      before/after on the running stack: all six `unreachable` → all six `healthy`, and they now
      flip back when the fleet is killed.


- [x] **STREAM-1 The front door shared one chat client across concurrent turns, corrupting streamed
      tool calls.** Closed by D-123: `AgentPool` leases one agent — and with it one chat client —
      per concurrent turn, sized to `service_max_concurrent_turns`.

      Root-caused by elimination (8 live attempts each): sequential turns never failed across three
      variants; 8 concurrent turns on one shared agent failed 8/8; per-turn agents passed 8/8; and
      per-turn agents with a **shared client** failed 8/8 — which named the client.
      `agent_framework_anthropic` keeps the tool call it is parsing on the client instance, and an
      argument delta carries `name=""` and recovers its identity from it, so two interleaved streams
      file one turn's arguments under the other's call id.

      Verified on the same live 50-user run that exposed it: **150 answers / 0 errors** (was 120/30),
      zero empty `tool_use` names, p50 19.8 s → 16.9 s, throughput 1.76 → 1.99 turns/s, and 208 tool
      calls against 151 — the count of tools that now run to completion rather than dying with their
      turn. The upstream fix is tracked in `docs/planning/DEFERRED.md`; when it lands the pool goes away.

- [x] **CI-1 `main` was red from D-117 until PR #37. Fixed: `check` is cancelled at the
      30-minute job timeout, every run.** Runs on `main` at `5f95166`, `5e0827a` and `33d454e` all
      end `cancelled` after exactly 30:00, and so does every PR run since. This is a regression from
      D-117 — the gates it moved into the root workflow are correct, but nobody checked the job
      could finish inside `timeout-minutes: 30`.

      It is a **hang, not slowness**. The log stops dead:

      ```
      17:54:52  tests/test_bo_campaign.py ................  [  8%]
      17:54:52  tests/test_bo_doe.py ...                    [  8%]
      17:54:58  tests/test_bo_featurize.py .............     [  9%]
      18:22:46  ##[error]The operation was canceled.
      ```

      28 minutes of silence at 9 %, and the orphan-process list at cleanup is
      `make`, `uv`, `pytest`, `temporal-test-server-sdk-python-1.30.0` — so it hangs on the first
      test needing the Temporal test server.

      **`timeout = 180` did not fire, and that is the second half of the bug.** pyproject picks the
      `signal` method deliberately (to fail one test rather than `os._exit` the session). SIGALRM is
      delivered to the main thread, but `temporalio` blocks inside its Rust core via PyO3, so the
      interpreter never gets back to run the handler. The one guard against exactly this failure is
      inert against exactly this failure.

      Not reproducible locally: the Temporal tests **skip** in this sandbox (the test server cannot
      be downloaded), which is why 1277-passing local runs say nothing about it. Fixing it needs a
      runner or an equivalent, and the first step is naming the test — `--timeout-method=thread` in
      CI would at least fail loudly with a name instead of burning the job silently.

- [x] **AUDIT-1 — RETRACTED. The middleware fires; my harness was sending invalid arguments.**
      I reported that the `@function_middleware` chain records zero invocations and warned that
      `enforce_tool_authz`, the RBAC gate, might therefore be inert. **That was wrong, and the error
      was mine.** The stub model sent `{"query": "benzene"}` while `find_notes` takes `text`, so
      every tool call failed argument validation inside `_auto_invoke_function` and returned at the
      parse-error branch — which sits *before* the middleware branch. No tool body ever ran, so of
      course nothing was audited.

      With the stub corrected, on the D-122 tree: `PIPELINE.EXECUTE fired n=4`, the tool returned
      `exc=None`, and `audit_events: 4 -> 5`. The GxP trail works end to end. RBAC is not affected.

      Two real things survive it, both smaller than the retracted claim:

- [ ] **AUDIT-2 A tool call rejected for bad arguments is neither audited nor authorization-checked.**
      `_auto_invoke_function` returns the parse error before reaching the middleware pipeline, so
      "the model asked for `find_notes` with arguments it could not satisfy" leaves no trace in
      `audit_events`. Authorization not running is harmless (nothing executed); the *audit* gap is
      not, for a GxP trail whose purpose is to answer "what did the agent attempt". Upstream
      behaviour in `agent_framework._tools`, so the fix is either a wrapper or an upstream change.

- [x] **LOAD-1 Re-state the load-test tool claim.** Closed by the re-run with the stub fixed (150/150, 2.08 turns/s, 100 tool bodies actually executing, cross-checked against `audit_events`). The runs reported "100 tool calls" and "the
      tool path genuinely exercised". Both are wrong for the same reason: those calls were
      dispatched and every one failed argument validation, so no tool body — no RDKit, no note
      scan, no database read — ever executed. The infrastructure findings (pool, event loop,
      worker count) stand, since they concern the request path rather than the tool body, but the
      absolute throughput numbers are optimistic and are being re-measured with the stub fixed.

- [x] **SCALE-1 The 409 same-session guard is per-process.** Closed by D-121: a turn also takes a
      leased `session_turns` row, refreshed three times per lease and released at the end, so the
      guard holds across workers *and* replicas. The advisory lock stayed rejected for the reason
      recorded here — connection-scoped, so it would pin a pooled connection for the whole turn.
      **Admission control is deliberately still per-process**: it bounds load on the shared LLM
      endpoint, and the deployment's real ceiling is `service_max_concurrent_turns × workers ×
      replicas`. Making it exact would cost a durable write and a heartbeat per turn to bound a
      *resource*, which is a worse trade than tuning the per-process number (SCALE-3).
- [ ] **SCALE-1b Attachments and harness todos are still per-process, and always were.**
      `agents.attachments.STORE` and the harness `TodoProvider` state live on the live
      `AgentSession` in one process's memory, so a chemist who uploads a CSV and then asks about it
      must reach the same pod. The Route now asserts session affinity (D-121), which pins to a
      *pod* — nothing can pin below one, which is why `service_uvicorn_workers` still defaults to 1
      for any deployment that uses uploads or the harness. Making attachments durable is the fix if
      intra-pod workers are ever wanted.
- [ ] **SCALE-2 The HPA still scales on CPU.** `values.yaml` documents this as the wrong signal for
      a stream-bound service and now has better ones to use: `chemclaw_turns_in_flight` against
      `chemclaw_turn_capacity`, and the new `chemclaw_turn_duration_seconds` histogram. Needs a
      Prometheus adapter in the cluster.
- [ ] **SCALE-3 `service_max_concurrent_turns` is still a guess (8).** At 50 users it shed 75% of
      turns; at 64 it shed none but p50 went to 37 s. The measured value depends on the fixes in
      D-119, so it should be re-derived from the next load test, not from this one.
- [ ] **SCALE-4 Make the rollback watermark unnecessary rather than merely loud.** Having
      `save_messages` remember the ids it inserted would remove the pre-turn read entirely, but the
      history provider is shared across every session on the pod, so it needs per-turn state that
      does not collide. Counted for now (`chemclaw_rollback_watermark_unavailable_total`).
- [ ] **SCALE-5 A turn still opens and tears down one MCP session per connector.**
      `connectors.registry.open_reachable` enters every connector tool for the turn and closes it
      after, because a connector's connection must belong to exactly one turn
      (`agents.chemclaw_agent.connector_tools`). At six connectors that is ~900 MCP handshakes for
      150 turns. **Measured, and it is not the ceiling:** against the live fleet the six handshakes
      cost 139–198 ms per turn, ~0.6 % of a 26.7 s p50 — so the "most likely remaining per-turn
      fixed cost" is noise, and pooling the connections across turns (which would trade a real
      isolation guarantee for it) is not worth doing. Connecting the six *concurrently* instead of
      sequentially is the cheap, isolation-preserving half; it measured no better here only because
      the dev fleet is one process serving all six, which is also why a `--workers 4` load run may
      find its ceiling in `scripts.connectors_dev` rather than in the front door.
- [x] **SCALE-6 All three measured.** The live-Anthropic 50-session run (150/150 answered after
      D-123, p50 16.9 s, 1.99 turns/s), the multi-replica run (two processes, one database: the
      cross-process turn guard held 6/6, rehydration 6/6, cross-talk 0/6) and the chaos pass
      (connector killed mid-flight — turn still answers and `/readyz` names it; Postgres bounced —
      pool recovered with no restart; client disconnect — **CHAOS-1**, open). Full record in
      `docs/archive/load-test-2026-07.md`.

## Open — Live e2e testing pass (2026-07-27, D-109)

Nine stages against the real running stack (Postgres+pgvector, Temporal, real Anthropic calls,
real signed tokens). Four findings, all fixed; two corrections to what the pass first reported are
kept because the wrong root cause is the more instructive record.

- [x] **LIVE-1 [Critical] Harness mode failed on 100% of tool calls.** `tool_use ids were found
      without tool_result blocks immediately after`, both autonomy modes, single and parallel.
      Cause is an upstream interaction, not chemclaw's tools or middleware — see `docs/planning/DEFERRED.md`.
      Neutralised locally by disabling per-service-call history persistence in
      `_build_harness_agent`. **The reason it was never caught matters more than the fix:**
      `ScriptedChatClient` derived from `BaseChatClient`, which is deliberately the base *without*
      middleware wrapping, so every harness test ran a pipeline with zero chat middleware and
      passed green while production failed every time. Fixed; two regression tests now reproduce
      the real defect offline.
- [x] **LIVE-2 [High] The test suite destroyed live data.** Nine test files wrote to production
      tables with no isolation; `test_audit_chain` truncated `audit_events` (the GxP hash chain)
      and left a deliberately corrupted row behind, so `make audit-verify` failed permanently
      afterwards — observed on the dev database, where rows 1–3 of the real audit trail were the
      test's own fixtures. Now migrated into a dedicated schema (`tests/pg.py`, `tests/conftest.py`).
      CI never noticed because its database is a throwaway container.
- [x] **LIVE-3 [Med] `chemclaw.db.connect` silently discarded a DSN's libpq `options`.** Found
      while building LIVE-2: `options=` was passed as a psycopg keyword, which overrides the
      connection string — but only when a statement timeout was set, so an operator's `search_path`
      or `application_name` vanished on some call sites and survived on others. Now merged.
- [x] **LIVE-4 [Med] The orphan-`tool_use` rollback was inert on the production path.** ISSUE-B-10
      (D-091 §2) restored `session.state` only; under `session_store="postgres"` the rows are
      already committed, so a client disconnect still bricked the session permanently. Now:
      repair-on-read in `PostgresHistoryProvider` (covers `SIGKILL`/eviction, which run no handler
      at all, and heals already-broken sessions) plus a real watermark rollback in `service/runner.py`.
- [x] **LIVE-5 [Low] RBAC denials narrated inconsistently.** *Corrected root cause:* the pass first
      blamed tool docstrings; no docstring mentions gating at all. The three refusal messages in
      `authorize_tool` simply differed, and the deny-default one was phrased for whoever edits the
      config ("not in the tool allowlist"), so the model relayed it as "a configuration issue" —
      sending a chemist to report a bug instead of requesting access. All three now share one
      chemist-facing shape, and `_INSTRUCTIONS` says how to narrate a refusal.
- [x] **LIVE-7 [Med] ADR numbers collided three times in one day.** This branch's ADR was written
      as D-092, renumbered to D-095, then to D-109 — each collision found only when a merge broke.
      Structural, not careless: concurrent branches all append to the end of `docs/decisions/` and all
      compute "highest visible + 1" against their own branch. Added `docs/decisions/README.md` (the
      allocation ledger, one line per number) and the procedure in `CLAUDE.md` — enumerate against
      `origin/main`, reserve in the first commit, and on a collision the branch merging *second*
      renumbers. Does **not** prevent collisions, only makes them a one-line conflict a grep finds;
      the collision-proof escalation (date-plus-slug ids) is recorded in D-109 rather than done
      unilaterally.
- [x] **LIVE-8 [High] The CLI could not take a turn under the configuration the Helm chart ships.**
      Found by the review's live harness smoke test (D-152), which is the first time the production
      agent-construction path met a live model with `harness_enabled=true` — the flag the chart
      sets while the code default and every test run `false`. The first turn crashed before the
      model with `RuntimeError: ToolApprovalMiddleware requires an AgentSession`: `cli/chat.py`
      called `agent.run` with no session, relying on the agent's implicit thread, and the harness
      middleware refuses that. The front door always passed a session and never met it. Fixed —
      `_run` creates one `AgentSession` per CLI run and threads it through `converse` and `_repl`,
      with a regression test that fails on the unfixed code. The smoke test then passed end to end:
      27 skills, `resolve_compound` → `predict_pka` over the calc connector, the Postgres
      calculation cache, and the whole turn in the audit trail under one correlation id.
      **Same shape as LIVE-1's lesson:** a configuration that only production sets is a
      configuration nothing tests.
- [ ] **LIVE-6 [Low] Test-to-table locality.** LIVE-2 isolates the schema but the tests still share
      one within a run, so ordering can still couple them (`test_postgres_store` asserts on a global
      migration result). A per-test schema or transactional rollback would close it — [S].

## Open — Deep codebase analysis (docs/archive/audit/12-deep-analysis.md)

Seven-track analysis of the dimensions the 2026-07-22 forensic audit under-covered (performance,
test *effectiveness*, complexity, doc↔code drift, configurability-to-run, feature triage, live-edge
risk). Four findings fixed in `a96932d`; the rest need a decision or are follow-ups.

- [x] **DA-1 [High] `cp .env.example .env` crashed every entry point.** `extra="forbid"` + two
      documented-but-nonexistent keys → `Settings()` raised at import. The README quickstart was
      broken. Fixed at the root: three parity tests now enforce no-stale-key, no-undocumented-field,
      and file-loads-as-real-`.env`.
- [x] **DA-2 [Med] 19 Settings fields undocumented** in `.env.example` (all 6 `budget_*`,
      `service_allow_insecure`, the D-066 clamps, ELN cursor slack) — contradicting the "every field
      mirrored" promise in `docs/guides/runbook.md`. Fixed; now machine-enforced by DA-1's tests.
- [x] **DA-3 [Med] `build_graph` reassembled the graph on every query.** KM-14's cache spared only
      the parse; `find_notes`→`expand_note` paid it twice per turn. Assembled graph now cached behind
      the same fingerprint and frozen (not copied — same rationale as frozen `Note`).
      Measured 162ms → 83ms at 10k notes.
- [x] **DA-4 [Med] `find_notes` was the last unbounded model-context surface.** Now capped by
      `graph_max_results` (50), sorted-id order, with the D-066 truncation warning.
- [x] **DA-5 [Med] Graph query floor is now the stat scan** — [S]. **Decided (D-1) and done**
      (D-082): `graph_cache_ttl_seconds` (default 5.0) skips the scan inside the window — measured
      **164ms → 0.52ms** on a warm query at 10k notes. Cost, stated: a change made *outside* this
      process can lag by up to the window. `kg.graph.invalidate_cache()` is the bust hook and the
      PR-gate submitter calls it, so the authoring loop never waits; `0` restores scan-every-query
      for deployments where no staleness is acceptable.
- [ ] **DA-7 [Low] Test-to-module locality is weak** — 3 of 5 mutations survived their "obvious" test
      file and died only under the full suite — [S]. Not a correctness gap (CI runs everything); a
      developer feedback-loop one.
- [x] **DA-10 [Med] Buy down live-edge risk offline** — [M]. **Decided (D-2) and done** (D-082,
      and actually reaching CI only in D-117 — same stranded-workflow cause as the coverage entry
      below; until then only the offline half ran):
      `make helm-validate` (`helm template` | `kubeconform -strict`) runs in CI, plus
      `tests/test_helm_chart.py` for the gap a schema check cannot see — a chart key that is not a
      `Settings` field (silently ignored as an env var, unlike the `.env` path that broke DA-1) and a
      malformed value on a real field (crashes every pod at import). Both mutation-verified.
      Entra/Nextflow contract tests still deferred until a real tenant exists — recorded-response
      tests written against a guessed shape assert one's own assumptions, not correctness.
- [ ] **Migration rollback is unaddressed** — `infra/sql` migrations are forward-only; a GxP
      deployment needs a tested down-path or an explicit ADR that forward-only is the policy — [M].

Track F verdict: do **not** re-derive the 29 AG-*/KM-* proposals. Load-bearing few, ranked:
**KM-13 retrieval evaluation** (everything else in the knowledge layer is unfalsifiable without it,
and the corpus is the smallest it will ever be), **AG-14 prompt/skill version provenance** (direct GxP
reproducibility hit), **KM-7 fingerprint re-indexing on mutation**. Recommend **downgrading** AG-12
(model routing/fallback) and KM-10 (near-dup detection) — ceremony for a single-endpoint,
Git-curated, human-signed-off system.

## Open — Config extensibility investigation (docs/archive/audit/10-config-extensibility.md)

Super-extensive investigation of how new skills/MCP-servers/tools/datasources/use-case agent
workflows are added, plus a substrate challenge and three passing offline spikes. Full analysis,
options matrices, and the two worked designs (datasource-*type*; `AgentProfile`) live in the doc.
Prioritized, dependency-ordered follow-ups (each ADR-ready, none needs live infra):

- [x] **Fix `.env.example` merge conflict** (unresolved markers at lines 156/170/173) — [S]. Done
      (both sides were real, non-overlapping Settings fields → kept both). Commit `b07a2b2`.
- [x] **Tool registry** (`@tool` + `_TOOL_REGISTRY`, mirror `evals/metric.py`) so a new tool is a
      decorator, not an edit to the hardcoded `_capability_tools()` list — [M]. Done: `agents/tool_registry.py`,
      12 tools decorated, `_capability_tools()` assembles from the registry, audit+authz middleware
      unchanged. Commit `76c03b2`. **KISS deviation:** Spike 1's `agent_facing` flag dropped (no hidden
      in-process tool exists — Rule of Three). **No `make tool-validate`:** name-drift is already guarded
      by `tests/test_agent.py::test_instructions_only_name_available_tools` + the registration guard; a
      separate CLI gate would be redundant.
- [x] **`AgentProfile` seam, Stage 1** (`agents/profiles.py` + one `"default"` entry ==
      today's agent + `build_agent(profile=…)` narrowing) — [M]. Done: default reproduces today's agent
      byte-for-byte; a profile narrows tools/MCP + swaps instructions/harness; unknown tool names fail
      fast; the *attenuate-not-authorize* invariant is test-proven (audit+authz attach regardless of
      profile). Stage 2 (front-door selection) triggers on a **second real use case**.
- [x] **`DataSourceSpec` discriminated union (scoped), Stage 1** — [M]. Done (D-076), then
      **superseded by D-120**. The union answered "how does a source carry per-instance config?"
      correctly, but priced every new source against core: a pydantic model, an arm of the union and
      a branch in `build_data_source`. A source is now a folder with a `datasource.yaml`, so config
      lives with the source and attaching one touches zero core Python. `DataSourceSpec`,
      `JsonElnSourceSpec`, `OrdElnSourceSpec` and `data_source_specs` are deleted; two ELN instances
      with different dirs are two manifests. **Snowflake connector still deferred** — now a manifest
      plus an adapter class, with nothing owed by core, when a real tenant/cluster exists
      (docs/planning/DEFERRED.md).
- [x] **Per-extension manifest + explicit enable-list** — [S]. Done (D-081): `SkillManifest`
      (pydantic `SKILL.md` frontmatter, `extra="forbid"`, optional `tools`/`mcp_servers`/`tags`) +
      `EnabledSkillsSource` + `skills_enabled`. `make skill-validate` now checks declared deps against
      the live registries — a skill teaching a renamed/deleted tool fails CI instead of surviving as
      stale prose (only possible because of D-075's tool registry). Four shipped skills declare real
      deps. Empty enable-list = every discovered skill (no regression); role gates still apply on top.
      Profile Stage 3 (filesystem-discovered profiles) remains deferred.
- [x] **MCP transport `type` union** — [S]. Done (D-081): `StdioMcpServerSpec | HttpMcpServerSpec`
      discriminated on `transport`; `_mcp_tool` dispatches to `MCPStdioTool`/`MCPStreamableHTTPTool`,
      exhaustively. A **callable** `Discriminator` reads a missing tag as `stdio`, so every config
      written before the union (`.env.example`, Helm values, deployments) keeps working untouched.
      `allowed_tools` — the boundary keeping write/index tools off the agent — is transport-independent.
- [x] **Config idiom convergence (doc, not churn)** — [S]. Done (D-081): the house rule is recorded in
      `chemclaw/config.py`'s module docstring — *typed JSON list when elements carry their own config
      (discriminate when they vary by kind); delimited string when elements are bare keys resolved
      against a registry, read via a derived `*_list` property*. Existing fields deliberately **not**
      migrated (churn without a defect); the two idioms coexist by design.

Substrate verdict: **evolve the flat `pydantic-settings` singleton additively** (nested sections +
discriminated unions); do **not** adopt entry-points/pluggy/Django-apps — all target the
out-of-tree plugin problem this single-repo app does not have.

## Open — the connector seam (D-109, docs/archive/plans/connector-plan.md)

Stages A, B, D and E are **done** (the seam, the reference bundles, the durable path, profiles, step
templates — D-109/D-110/D-111/D-112). Stage C is partly done: `molfp`, `rxnfp`, `safety`, `chem`,
`calc` and `bo` have moved. What remains is staged in `docs/archive/plans/connector-plan.md` §9, with the trigger
for each recorded here rather than left implicit.

- [x] ~~**Stage C, remainder — the `kg` bundle**~~ — **WON'T BUILD**, and the reason is also the
      answer to the open question (D-114). The graph is not a peripheral capability; it is core's
      own data layer. Thirteen core modules import `kg` — the PR-gate, all six memory layers, the
      report retrievers, the eval verifier, the note index — so a bundle would move three thin read
      tools and leave every one of those imports where it is: the dependency win is **zero**, and
      the cost is a second read path to one note tree. Re-indexing stays in core for the same
      reason, on the background queue, triggered by a merge into the repo core owns. The rule is
      written down where the next author will read it (`connectors/manifest.py`, runbook §(iv)), so
      "why isn't `find_notes` behind a connector?" has an answer in place rather than being
      re-litigated.
- [ ] **Stage C, remainder — the `report` job** — [S]. The last bespoke durable adapter that can
      move; it follows `bo`'s shape once its workflow returns the `ConnectorJobResult` envelope
      directly instead of being wrapped a third time. *Trigger: now; mechanical after D-111/D-113.*
- [~] **`submit_qm_job` stays in core** — not a Stage C remainder. It needs the HPC identity bridge
      (a federated credential exchanged per submission), which is core's, not a capability's. The
      in-process rule covers it: it is plumbing, not chemistry.
- [ ] **`mcp_servers/molfp|rxnfp` bodies could move into their bundles** — [S]. Cosmetic: both are
      already *reached* only through their connectors, so this is about there being one obvious
      place to look, not about behaviour. `mcp_servers/README.md` says so explicitly meanwhile.
      *Trigger: the next substantive edit to either capability.*
- [ ] **A second step template** — [S]. `hazard-briefing` is Stage E's only caller. The engine was
      built ahead of its recorded trigger at the user's request (D-112), so the "does this earn its
      keep" question is still open rather than answered. *Trigger: the next procedure whose order
      must not vary — or, if none appears, a decision to fold it back.*
- [ ] **Entra auth modes for connectors** — [M]. The manifest's auth union ships `none` and `bearer`.
      `entra_workload` (client credentials over the federated SA assertion) and `entra_obo` are the
      documented extension point, each one variant plus one branch in `connectors.identity.auth_for`.
      OBO additionally needs the user's *raw* access token, which `service.auth.Principal` deliberately
      does not carry — a security-relevant change with no caller today. *Trigger: a real tenant (the
      same one blocking every other live Entra edge).*
- [x] ~~**A manifest's `task_queue` is unchecked against `bundle_queue`**~~ — **DONE (D-150)**, and
      the option that looked like a trade-off turned out not to be one. Raised in D-149: a bundle's
      queue name was derived in code *and* spelled out per job in `connector.yaml`, all eight
      agreeing, with nothing checking that they did. The choice looked like validate-it versus
      derive-it, where deriving forecloses routing a connector job onto core's `background-jobs`
      worker — the escape hatch `JobSpec`'s docstring advertised and `task_queue_for`/`JobRuntime`
      were built for. Checking the dispatch path showed that hatch cannot open: core's background
      worker serves `registered_workflows("background")`, populated at import time by modules it
      imports, and it never imports a bundle (the boundary `tests/test_workflow_registry.py`
      asserts). A job declaring `background-jobs` would start and then wait forever. The field
      could therefore hold exactly one correct value, so it is gone and the queue is derived at
      dispatch.
- [ ] **Concurrent-turn MCP lifecycle, the general case** — [S]. Per-turn connector instances fixed
      this for connectors (D-109), and the *shape* that caused it — a process-lived tool whose context
      is entered per turn — should not reappear. A guard test asserting no MCP tool is attached to the
      process-lived agent would make that structural rather than remembered. *Trigger: next time
      anything is tempted to put an MCP tool on `build_agent`.*

## Open — OKF-inspired graph polish (D-074)

Two conventions from Google's Open Knowledge Format, checked against our already-equivalent
design (D-004/D-005) and queued rather than adopted wholesale — see D-074 for the comparison.

- [ ] ~~**Per-bundle `log.md` changelog** appended by the PR-gate~~ — **DROPPED as designed**
      (assessment 2026-07-25): every note lands on its own branch, so N concurrent proposals all
      append to the same `log.md` and every one after the first conflicts — manufacturing merge
      failures to duplicate what git already records. Redesigned as a *generated* view (`git log` →
      rendered changelog), deferred until a reviewer/auditor actually asks for one.
- [ ] **External ontology anchoring on notes.** Frontmatter `type`/tags are free strings today —
      no class hierarchy, so an agent can't query by subsumption (e.g. "all electrophilic aromatic
      substitutions" matching a `reaction_class: acetylation` note). Add optional frontmatter
      fields carrying **existing** external ontology IDs — ChEBI for compounds, RXNO for reaction
      classes — rather than building an in-house OWL/RDF ontology (no second caller yet; KISS).
      Needs: which notes get which field, whether resolution is validated at `kg-validate` time or
      left as an unchecked reference.

## Done — Resilience hardening (D-066, four-failure-mode review)

Reviewed Chemclaw against four failure modes from another agent system (no memory on restart, no
idempotency, no budget, unbounded DB queries). Idempotency (D-011 cache + workflow-id dedup) and
durable job execution (Temporal) already covered; three residual gaps closed, each config-gated:

- [x] **DB clamps (#4).** `find_matches` clamps model-supplied `top_k` to `[1, fingerprint_max_top_k]`
      (mirrors the `graph_max_hops` clamp); `all_records(limit)` + `substructure_scan_max_records`
      bound the substructure scan with a truncation **warning** (no silent cap). `mcp_servers/fpstore.py`,
      `mcp_servers/molfp/search.py`, `chemclaw/config.py`. Tests: `test_molfp.py`.
- [x] **Session reattach (#1).** `session_owners` table (migration `013`) + `SessionOwnerStore`
      persist one owner row per session; the front door rehydrates a live handle over durable history
      on a cache miss (owner-scoped, gated on `session_store="postgres"`). `agents/session_store.py`,
      `service/app.py`. Tests: `test_service.py` (reattach + owner-scope), `test_session_store.py` (PG).
- [x] **Turn/token budgets (#3).** `service/budget.py::BudgetTracker` meters token usage + counts
      turns per session/user; front door refuses over-budget turns with 429 (`budget_*` config, off by
      default). `service/runner.py` (`_usage_tokens` + `record`). Tests: `test_budget.py`, `test_service.py`.
- [ ] **Deferred (docs/planning/DEFERRED.md):** durable/rolling-window budget quota (survives restart/multi-pod);
      substructure pattern-fingerprint prefilter (sound screening past ~10⁴ molecules). The deeper
      *mid-flight same-turn* resume stays open (see the harness follow-ups below) — distinct from the
      front-door restart-reattach closed here.

## Phase F11 — Gap closure (docs/archive/plans/gap-closure-plan.md; analysis: docs/archive/audit/12-capability-gap-analysis.md)

Implementing the whole-codebase capability gap analysis. **Waves 0–2 complete and W3 partial**;
everything below is built, tested, and green under `make lint type test` (688+ passing).

### Done — W0 deployment truth
- [x] **DEP-1** knowledge sync: `deploy/knowledge-sync.sh` (clone-or-refresh replica; `once` /
      `loop` / `checkout` modes) as an init container + refresh sidecar on the service and both
      workers, so a merged note reaches a live pod instead of needing a rebuild. Refresh is
      `fetch`+`reset --hard`, never `pull` — a replica must not be able to land on a conflict.
- [x] **DEP-2** push credential: `knowledgeRepoToken` secret + a full writable submitter clone on
      every component that calls `propose_note` (the front door too — `propose_knowledge_note` is
      an agent tool), on a *different* volume from the read replica (`checkout -B` switches a whole
      working tree). Token via a credential helper, never in `.git/config` or a log line.
- [x] **DEP-3** the MCP Deployments were default-on but stdio-only (a server with no stdin =
      crash loop, while the agent spawned its own subprocess anyway). Defaulted off; the template
      guard now also requires a networked transport, matching its own stated intent.
- [x] **DEP-5 (found while implementing)** the image never COPYed `skills/`, `scripts/`, `evals/`
      or `knowledge/` and never installed `git`. In-cluster: the agent advertised **no skills**, no
      Schedule could ever be created, and the PR-gate could not shell out to git. Fixed, plus a
      post-install hook Job that applies the Schedules.
- [x] **SCH-2** `NoteReindexWorkflow` + Schedule (`note_reindex_enabled`). Stale hybrid entries
      previously ranked confidently beside live graph hits — RRF carries no staleness signal.
- [x] **RCH-3** the durable approval hold finally has a human: `GET /approvals`,
      `GET /approvals/{id}`, `POST /approvals/{id}/decision` (owner-scoped; someone else's hold is
      a 404, no existence leak) + Yes/No buttons in the chat UI. Deliberately **not** an agent
      tool — that would let the agent approve its own candidate. A test pins it.
- [x] `tests/test_deploy_chart.py` — the offline half of `helm template | kubeconform`.

### Done — W1 reachability
- [x] **RCH-1/RCH-2** `agents/durable_tools.py`: `request_development_report`,
      `start_optimization_campaign`, `get_durable_job_status` on the `qm_tools` seam. Both
      subsystems were built, tested, worker-registered and unreachable.
- [x] **RCH-4/RCH-5** `agents/turn_signals.py` + runner wiring: `PlanEvent` (from the harness's own
      todo store), `JobStartedEvent`, and a new `NoteProposedEvent` so a chemist sees their
      contribution open a branch. All three were contracted and UI-rendered but never emitted.
- [x] **IDEA-7** `make prose-validate` gates that agent-facing prose only names tools that exist.
      It immediately found a second live instance: `deep-research/SKILL.md` taught the agent three
      tool names (`find_similar_*`) that would have failed at call time.
- [x] **AGT-1 WITHDRAWN** — verified false. Cancellation was already correct (4bc9b04); the claim
      rested on a grep. `tests/test_turn_cancellation.py` measures it and is kept.

### Done — W2 chemistry
- [x] **KNW-1** `OrdReaction.performed_at` → `Note.valid_from`, finally feeding F10-G2's
      bi-temporal fields for the largest note class.
- [x] **KNW-2** `purity_percent` + `Impurity` list; both adapters map them, the note renders them.
      A test pins that none of it touches `reaction_smiles()` — that would have invalidated every
      DRFP fingerprint.
- [x] **TOOL-2** `chemclaw/reagents.py` — 87 spellings → canonical structure. Uses
      `require_canonical_smiles`; the lenient variant would have resolved every miss to itself.
- [x] **TOOL-3** `chemclaw/hazard.py` + `screen_hazards` + the `process-safety` skill. SMARTS
      motifs (catches a novel acyl azide), a substance table, and a symmetric incompatible-pair
      table (NaN3+DCM). Advisory by design; `unresolved` is as load-bearing as `findings`.
- [x] **TOOL-4/TOOL-5** `stoichiometry_table` and `render_structure`.

### Done — W3 (partial)
- [x] **SCH-3** `ScheduleOverlapPolicy.SKIP` + a deterministic per-job phase offset, so the three
      memory jobs stop firing simultaneously against one background worker.
- [x] **SCH-1** `workflows/retention.py` + Schedule. Prunes only spent operational rows and
      **refuses** `audit_events` (deleting from a hash chain is indistinguishable from tampering —
      needs archive-then-reseal in an ADR) and `calculation_results` (age is the wrong axis for a
      cache; D-011 makes eviction a recomputation). Off until a deployment states a policy.

### Done — W3 (complete)
- [x] **SCH-3** `ScheduleOverlapPolicy.SKIP` + a deterministic per-job phase offset.
- [x] **SCH-1** `workflows/retention.py` + Schedule; **refuses** `audit_events` (deleting from a
      hash chain is indistinguishable from tampering) and `calculation_results` (age is the wrong
      axis for a cache — D-011 makes eviction a recomputation). Off until a policy is stated.
- [x] **DEP-4** `service/metrics.py` + `GET /metrics` (Prometheus text, no new dependency). Counts
      shed turns, budget refusals, 409s, timeouts, audit-sink failures; gauges read live structures
      so they cannot drift. Names the HPA problem in `values.yaml`: CPU is noise for a stream-bound
      service, `turns_in_flight`/`turn_capacity` is the saturation signal.
- [x] **SCH-4** `GET /schedules` from Temporal's own state (no mirrored table). A planned Schedule
      missing from Temporal is *reported*, not omitted. Surfaces `skipped_overlap`, the early
      warning that a job no longer fits its interval.
- [x] **SCH-5** `AuditChainVerifyWorkflow` on a cadence, alerting via the must-deliver notify seam.
- [x] **AGT-2** mid-turn durable-job resume: opt-in, bounded, non-recursive, degrading to the
      previous behavior (result next turn) rather than to an error.

### Done — W4
- [x] **KNW-5** `kg/analytics.py` + `find_knowledge_gaps`: isolated notes, projects with evidence
      but no distillation, hubs. The graph could only be walked outward from a hit, so "what don't
      we know" — the question that steers experiment design — was unaskable.
- [x] **KNW-6** `KNOWN_NOTE_TYPES` enforced by `kg-validate` (not the schema, so the agent can still
      propose a new type for a human to review).
- [x] **KNW-3** `outcome_class` + required `failure_reason`, and failures are filtered out of
      playbook distillation — without that filter a repeated failure would distil into a
      recommendation, inverting the record.
- [x] **KNW-7 + KNW-4** `eln/compound.py` (structure-derived compound notes so a structural hit can
      cite something) and `memory.canonical_condition` (DMF / N,N-dimethylformamide / CN(C)C=O fold
      to one token), both reusing the one identity table.
- [x] **TOOL-1** networked (streamable-HTTP) MCP transport — "adding a capability is a config
      entry" is now true at org level, and DEP-3's `transport: http` guard is satisfiable.
- [x] **IDEA-5** optional per-retriever weights in the RRF fusion; ships inert.
- [x] **IDEA-3 (tool half)** `green_metrics` exposes E-factor/PMI to the agent.
- [x] **SCH-6** `POST /events/knowledge-merged` — the first inbound event path; collapses SCH-2's
      staleness window from an interval to seconds.
- [x] **AGT-5** `QuestionEvent` + `ask_clarifying_question` — the agent can ask instead of guessing.
- [x] **IDEA-4** dry-run mode: ambient (never a tool argument, so the model can neither set nor
      clear it), gating all three durable launchers.
- [x] **AGT-4** `agents/preferences.py` + migration `015` — per-user working preferences,
      deliberately *not* graph notes (the PR-gate protects shared knowledge, not personal trivia).

### Done — the W4 remainder (each previously blocked; the blocking decision is now made explicitly)

- [x] **IDEA-2 predicted-vs-actual calibration** — `calc/calibration.py` + migration `016`. Three
      figures, not one: **bias** (a reliable offset is correctable, the same MAE scattered is not),
      **MAE**, and **uncertainty coverage** — the one a mean error cannot show, because a calculator
      whose error bars never contain the truth is misleading precisely where it claims confidence.
      `calculator_trust` and `report_measurement` expose it, so "how far to trust this" is measured
      rather than asserted in prose. Off until `calibration_enabled`.
- [x] **IDEA-1 standing queries** — `agents/subscriptions.py` + `workflows/digest.py` + migration
      `017`. The watermark advances *after* delivery: a crash must re-report, never silently skip.
      Rides the existing push-back channel — no second notification system.
- [x] **AGT-3 file ingress** — `agents/attachments.py` + `POST /sessions/{id}/attachments`.
      **Decision made:** a closed allowlist of formats parseable completely and deterministically
      offline (markdown/plain text, CSV/TSV). Binary scientific formats are **refused with a message
      naming what is supported**, never half-parsed — a PDF "read" by scraping text-like bytes
      produces confident nonsense a chemist cannot distinguish from a real reading. Attachments are
      session-scoped working material; anything worth keeping still goes through the PR-gate.
- [x] **IDEA-6 corpus backfill** — `scripts/backfill_corpus.py`, reusing AGT-3's parsers verbatim.
      One note per document, **verbatim**, through the PR-gate. Deliberately no summarizing: a
      backfill makes documents *reachable*; an LLM-summarized one would put thousands of unreviewed
      paraphrases into the corpus. Content-derived ids, so a rename does not mint a duplicate.
- [x] **TOOL-6 external literature — built, then REMOVED (D-089).** The PubChem retriever was
      implemented and reviewed, and the scope decision came back: **no external sources at all.**
      `report/literature.py` and the registry entry are deleted; `tests/test_no_egress.py` now
      fails on any first-party module naming a third-party data host, because the prose form of
      this constraint had already existed in `docs/planning/DEFERRED.md` and did not prevent the build.

### Closed as not-gaps after assessment (do not re-open blindly)
- [x] **TOOL-7 units** — carried in field names throughout (`temperature_c`, `mass_g`,
      `moles_mmol`), including every model added in F11. A `Quantity` type would be an abstraction
      with no second caller.
- [x] **AGT-6 structured outputs** — the W1 tools take typed pydantic arguments, so MAF already
      forces a validated payload at the machine-consumed call site whose absence was the original
      reason to defer.
- [x] **AGT-1 turn cancellation** — verified correct as of `4bc9b04`; now measured by
      `tests/test_turn_cancellation.py`.

**Phase F11 is complete.** Remaining open items are the pre-existing live edges (a real Entra
tenant / Temporal broker / OpenShift cluster) plus the audit-trail archive-then-reseal design, which
is recorded in `docs/planning/DEFERRED.md` as needing its own ADR with QA sign-off rather than a cleanup job.

## Next — Platform-parity hardening (docs/archive/plans/parity-plan.md, Phase F10)

Closes the platform-capability deltas found against a commercial pharma-agent platform. Full
tickets + disposition table: `docs/archive/plans/parity-plan.md`.

- [x] **F10-E** per-task model routing: `build_chat_client(task)` consults `model_routes`
      (task→model) in the one provider seam; empty map = today's single model. Test:
      `test_llm_provider.py`, `test_config.py`.
- [x] **F10-C** per-tool authorization: `agents/authz.py::authorize_tool` (`tool_role_gates` +
      `tool_authz_default`) enforced by one middleware `agents/tool_authz.py::enforce_tool_authz`,
      wired into `build_agent` after audit; default-allow, active only under `entra_required`. The
      coarse expensive-trigger gate now shares `_has_required_role` (DRY). Tests:
      `test_tool_authz.py`, `test_agent.py`, `test_config.py`.
- [x] **F10-G1** tamper-evident audit hash-chain: migration `011_audit_hash_chain.sql`
      (`prev_hash`/`row_hash`), `audit_store.chain_hash` + advisory-lock-serialized chained insert,
      `scripts/verify_audit_chain.py` + `make audit-verify`. Tests: `test_audit_chain.py` (offline
      tamper/deletion detection; PG round-trip skips offline).
- [x] **F10-G2** bi-temporal note validation: `kg/note.py` rejects `valid_to < valid_from` (fields
      already existed); surfaced by the parser + `kg-validate`. Test: `test_note.py`.
- [x] **F10-A** hybrid retrieval (executes/extends F8-T2): embedding provider seam
      (`agents/embedding_provider.py`, `hash` offline / `openai_compatible` prod); derived
      `note_index` (`infra/sql/012`, `report/vector_index.py` — `NoteIndex` with in-memory +
      pgvector/FTS backends, `reindex_notes` + `make reindex`); `VectorRetriever` + `LexicalRetriever`
      attached via the F7 registry (`vector`/`lexical` keys — registry membership is the enable
      switch, D-018); RRF fusion (`report/hybrid.py`) under `retrieval_mode="hybrid"` in
      `gather_evidence`, graph flat-union default unchanged. Graph traversal stays the reasoning path
      (D-004). Config: `embedding_*`, `retrieval_top_k`/`_mode`/`_fusion_k`. Tests:
      `test_embedding_provider`, `test_vector_index`, `test_hybrid_retrieval`, `test_config`.
      Deferred (follow-up): a scheduled `background-jobs` reindex activity (today `make reindex` /
      the CLI populates the index); the enable-flag booleans were intentionally folded into registry
      membership rather than added.
- [x] **F10-B** answer verification + confidence routing: `agents/verifier.py` — `verify_answer`
      scores citation faithfulness, LLM-as-judge (structured output on the routed `verifier` model,
      F10-E) when `verifier_enabled`, else the deterministic `verify_claims` gate (DRY, offline).
      `verify_turn_answer` resolves an answer's `[[wikilink]]` citations (shared `kg.note.cited_ids`)
      to the notes it cites; the runner stamps `AnswerEvent.confidence` + `unsupported_claims` and
      sets `review_required` when `confidence < verifier_confidence_threshold` (the routing signal a
      surface/future hold keys off — the durable D-032 hold is deferred, docs/planning/DEFERRED.md). Default-off =
      today's plain answer. Config: `verifier_enabled`, `verifier_confidence_threshold`. Tests:
      `test_verifier`, `test_runner`, `test_config`. (F10-B3 — LLM faithfulness of *report* prose —
      deferred: the durable report path has no in-workflow prose to judge, only citations, which
      `verify_claims` already gates. See docs/planning/DEFERRED.md.)
- [x] **F10-F** quality metrics — P/R/F1 + drift: `evals/metrics.py` adds `precision`/`recall`/`f1`
      (pure `precision_recall_f1` over predicted vs `expected_note_ids`; report/drift metrics, no
      per-case gate); `evals/retrieval.py` scores a live retriever's P/R/F1 (`run_retrieval_eval`,
      reuses `run_eval`); `evals/baseline.py` (`aggregate_metrics`/`detect_drift`, committed
      `evals/baseline.json`) + `workflows/eval_drift.py` (`EvalDriftWorkflow` on background-jobs,
      alerts via the *must-deliver* notify seam so a dropped alert fails the run). `detect_drift`
      uses a *relative* band (`_epsilon` × baseline) so one knob fits metrics of different scales;
      `DriftAlert.vanished` distinguishes an absent metric from a 0.0 score. Config:
      `eval_drift_enabled`/`_schedule_minutes`/`_epsilon`/`_timeout_seconds`, `eval_baseline_path`.
      Committed pinned case `retrieval-precision-recall.md`. Tests: `test_metrics_classification`,
      `test_retrieval_eval`, `test_eval_drift` (incl. a baseline-matches-case-set guard),
      `test_schedules`, `test_config`. (Over the deterministic committed case-set the scheduled job
      is a deployment-consistency tripwire; live-retriever drift is deployment-local — docs/planning/DEFERRED.md.)
- [x] **F10-D** sub-agent orchestration via Temporal child workflows: `workflows/orchestrator.py`
      `fan_out(child, inputs)` runs N sub-tasks as bounded-parallel child workflows with per-child
      retry + D-030 isolation (a poison child is dropped, siblings unaffected; results in input
      order). Adopted by two real callers (Rule of Three): the report workflow (`ReportSectionWorkflow`
      per section) and the memory jobs (pure `build_*_notes` extracted in `memory/jobs.py`, each note
      published by a shared `PublishNoteWorkflow` child). Config `orchestrator_max_parallel_children`.
      Tests: `test_orchestrator` (`_batches` offline + a Temporal-env fan-out isolation test),
      `test_memory` (builder is behavior-preserving), `test_report_workflow`/`test_workers`
      registration. A failed report section degrades to a visible `retrieval_failed` marker in the
      draft (not silently dropped, GxP); `fan_out` re-raises `CancelledError` and carries no
      redundant child-level retry. Conversational multi-agent mesh stays gated (single agent + skills
      is KISS).
- [ ] Gate-until-trigger (documented, not built): OCR/vision ingestion, vendor connectors
      (Veeva/SAP/LIMS), GAMP-5 validation artifacts, conversational multi-agent mesh — each with its
      trigger recorded in `docs/archive/plans/parity-plan.md`.

## Now — Foundation build (docs/archive/plans/foundation-plan.md + docs/planning/implementation-tickets.md)

The target-stack foundation: MAF harness experience on OpenShift + HPC/Nextflow, internal
OpenAI-compatible LLM (generic credential), Entra everywhere with every backend workflow
user-specific, a generic data-source seam (first source ELN — a **custom Snowflake connector via
an internal data pipeline, no vendor**). Full ticket breakdown: `docs/planning/implementation-tickets.md`.

### Phase F0 — LLM provider seam + tool-calling spike
- [x] **F0-T1** LLM provider config block (`llm_provider`/`llm_base_url`/`llm_model`/`llm_api_key`/
      `llm_tls_ca_bundle`/`llm_timeout_seconds`/`llm_max_retries`/`llm_temperature`/`llm_max_tokens`
      + `_llm_provider_config` validator). Test: `test_config.py`.
- [x] **F0-T2** Provider adapter `agents/llm_provider.py::build_chat_client` — the one place a client
      class is imported; `openai_compatible` → MAF `OpenAIChatClient` over an `AsyncOpenAI`
      (base_url + generic key + CA/timeout/retries), `anthropic` dev path retained. `build_agent`
      rewired off `_default_chat_client`. Dep added: `agent-framework-openai`. Test:
      `test_llm_provider.py`, `test_agent.py`.
- [x] **F0-T3** Streaming + generation params: `Agent(default_options=ChatOptions(temperature,
      max_tokens))` from config. Test: `test_agent.py::test_agent_applies_default_generation_options`.
- [ ] **F0-T4** Tool-calling capability spike (the H0 risk) — `scripts/spike_toolcalling.py` +
      `docs/spikes/f0-toolcalling.md` verdict. **Needs the live internal endpoint**; run before
      building on the harness. (The "stand-in OpenAI-compatible server" variant is **dropped** —
      it would test the stand-in; the client-wiring half is already proven live by
      `tests/test_harness_execution.py`, D-058. Only the endpoint's own fidelity is still unknown.)

### Phase F1 — Harness backbone (autonomous plan/execute)
MAF ships the harness natively (`create_harness_agent` + `TodoProvider`/`AgentModeProvider`/
`todos_remaining`), so F1 is *wiring* it, not reimplementing providers.
- [x] **F1-T1** Harness config (`harness_enabled`/`harness_autonomy`/`harness_max_loop_iterations`).
      Test: `test_config.py`.
- [x] **F1-T2** `build_agent` branch → `_build_harness_agent` wires `create_harness_agent` over the
      full shared `_capability_tools()` + `RoleFilteredSkillsSource` + audit + shared
      `_compaction_strategy()`, generic batteries off. Classic path is the fallback. Test:
      `test_agent.py` (todo/mode providers added; full toolset kept; audit kept; classic has no
      harness providers).
- [x] **F1-T3** Plan→approve→execute: `AgentModeProvider(default_mode=plan|execute)` +
      `todos_remaining(looping_modes=["execute"])` → plan_only stops for approval, execute loops
      (capped). Test: `test_agent.py::test_harness_autonomy_sets_start_mode`.
- [x] ADR **D-020** finalized + **D-A1** (F0) — written in docs/decisions/ (D-020, and D-039 = foundation
      D-A1, D-040 = foundation D-020). Checkbox was stale; confirmed present.
- [x] **F1-T4** The loop proven live, not just constructed: `test_harness_execution.py` drives
      `build_agent`'s real harness path with a scripted-but-real `BaseChatClient`/
      `FunctionInvocationLayer` chat client — genuine multi-iteration execute-mode looping,
      plan-mode's loop actually not continuing, and the iteration cap actually capping. ADR **D-058**.

### Phase F2 — Front door + run service (the agent finally runs)
- [x] **F2-T1** `service/app.py::create_app` (FastAPI) + `service/runner.py::run_turn` — builds/holds
      one agent, per-session `AgentSession`, opens the MCP lifecycle once per turn (`AsyncExitStack`
      over `agent.mcp_tools`), runs `agent.run(stream=True)`, streams events. Routes: `/healthz`,
      `/readyz`, `POST /sessions`, `POST /sessions/{id}/messages` (SSE). Config: `service_host`/
      `service_port`/`service_cors_origins`. Test: `test_service.py`.
- [x] **F2-T2** Thin web chat surface `service/static/{index.html,app.js}` (vanilla + fetch-stream SSE;
      renders plan/tool-trace/tokens/approval/answer). Served at `/`. Test: `test_service.py`.
- [x] **F2-T3** Typed event contract `service/events.py` (discriminated union on `type`:
      plan/tool_call/token/job_started/approval_request/answer/error). Test: `test_service_events.py`.
- [ ] Deferred within F2: emit `PlanEvent` from harness todo state, and real `JobStartedEvent` when a
      tool starts a Temporal job (wired in F3 with job→session push-back). ADR **D-A2** (front door).

### Phase F3 — Durable session + job→session push-back
- [x] **F3-T1** Postgres session history: `agents/session_store.py::PostgresHistoryProvider`
      (overrides get/save_messages, `Message.to_dict/from_dict` → `session_messages`), migration
      `infra/sql/008_sessions.sql`, config `session_store`/`session_store_dsn`, `build_agent` selects
      via `_history_provider()`. Tests: `test_session_store.py` (unit selection + PG round-trip that
      skips offline), `test_config.py`.
- [x] **F3-T2** Session-events push-back channel: `infra/sql/009_session_events.sql`,
      `agents/session_events.py` (`SessionEvent` + `record_session_event`/`fetch_unconsumed`/
      `mark_consumed` + dependency-injected `stream_new_events` tailer), `workflows/notify.py`
      (`record_session_event_activity` + `SessionEventInput`), config `session_event_poll_seconds`.
      Tests: `test_session_events.py` (tailer loop + model + activity forwarding as unit; PG
      round-trip skips offline).
- [x] **F3-T3** job→session push-back wiring: ambient session id (`agents/session_context.py`
      contextvar, stamped by the runner); `QMJobInput.session_id` (excluded from `qm_job_key`);
      `submit_qm_job` stamps it; QM workflow calls `notify_session_best_effort` on completion (activity
      on the background queue, registered on the worker); front-door `GET /sessions/{id}/events` SSE
      streams `job_completed` push-back (`JobCompletedEvent`). Tests: `test_session_context.py`,
      `test_service.py` (all offline with fakes); the workflow-emit + DB round-trip prove live.
- [x] Deferred-within-F3-T3 item resolved: flipping the harness `awaiting` todo on completion.
      `agents/harness_todo.py` (`mark_awaiting_job`/`complete_awaiting_job`, direct
      `TodoSessionStore` mutation); `submit_qm_job` marks on a fresh submit, `/sessions/{id}/events`
      flips on `job_completed`. Gated on `harness_enabled` + the ambient live session
      (`agents.session_context.get_current_session`, new). Tests: `test_harness_todo.py`,
      wiring tests in `test_qm_tools.py`/`test_service.py`. ADR **D-058**. Still open: resuming the
      *same* streamed turn mid-flight (vs. picked up next turn) — see the F1 follow-up below.
- [x] `PlanEvent`/live `JobStartedEvent` emission (ADR **D-042**, closed by **D-077**): a per-turn
      contextvar sink (`agents/job_events.py`) carries a launch from `submit_qm_job` to the runner,
      which drains it between updates and after the stream; `PlanEvent` renders the harness todo
      list (`agents.harness_todo.todo_titles`) and is emitted only when it changes. The idempotent
      re-submit announces nothing (that job may already be complete and will never push back).
      Tests: `test_runner.py`, `test_service.py`, `test_qm_tools.py`.

### Phase F4 — Entra ID identity & RBAC (system-wide)
- [x] **F4-T1** Front-door user auth (Entra OIDC): `service/auth.py` (`Principal`, `validate_token`
      with RS256 + audience + issuer checks, `require_principal` FastAPI dep), config
      `entra_required`/`entra_tenant_id`/`entra_audience` + derived
      `entra_jwks_endpoint`/`entra_issuer_url`; guards all non-health routes; dev stand-in when
      `entra_required` is off. Dep `pyjwt[crypto]`; ruff allows `fastapi.Depends` (B008). Tests:
      `test_auth.py` (local-RSA token validation, 401 gate, dev mode), `test_config.py`.
- [x] **F4-T3** The core rule as one reusable guard: `agents/authz.py::require_actor()` returns the
      turn's ambient Entra oid and, under `entra_required`, **rejects** a user-triggered workflow with
      no user before any durable work (dev → `service_actor_id`). Wired into `submit_qm_job`
      (`requested_by = require_actor()`); `requested_by` stays out of `qm_job_key` (D-011). BO/report
      inputs adopt the same guard when they gain live triggers (no dead field now); scheduled
      ELN-sync/memory jobs run as the service by design. ADR D-044. Tests: `test_authz.py`.
- [x] **F4-T5** Authorize at one point + actor into audit: `agents/authz.py::authorize_trigger`
      (config `entra_expensive_actions`/`entra_privileged_roles`) called by `submit_qm_job` before the
      durable job; ambient identity via `agents/identity_context.py` (contextvar, stamped by the
      runner from the `Principal`); `make_audit_middleware` records the ambient Entra oid over its
      build-time default. Tests: `test_authz.py`, `test_audit.py`. T5's "roles→skills per request"
      remainder is **done** — delivered by D-052 as the ambient skills filter
      (`agents/skill_access.py::RoleScopedSkillsSource`, wired at `agents/chemclaw_agent.py:139`).
- [x] **F4-T2** Workload identity federation: `agents/identity/workload.py::WorkloadTokenProvider`
      (SA-JWT→Entra client-credentials exchange, per-scope cache). ADR D-045. `test_workload_identity.py`.
- [x] **F4-T4** OBO exchange: `agents/identity/obo.py::exchange_obo` (wired, dormant). ADR D-046.
      `test_obo.py`.
- [x] **F4-T6** Non-Entra bridges: `chemclaw/temporal_client.py::connect_options` (mTLS/api-key) +
      `agents/identity/hpc_bridge.py::map_to_hpc_identity` (logs every mapping). ADR D-047.
      `test_hpc_bridge.py`.
- [ ] **F4 live edges** (need a real tenant/broker/cluster; code + fake-endpoint tests already green):
      real Entra token validation against a live JWKS, real federation/OBO exchanges, live Temporal
      mTLS handshake. (Per-request role→skills scoping is **not** open — done in D-052, see F4-T5.)
- [x] **F5** Real HPC path behind the QM activities: `workflows/hpc/nextflow.py` (Tower REST adapter
      `launch_run`/`poll_run`/`fetch_artifacts`, fake-HTTP tested), dispatched by `hpc_launch_interface`
      (mock kept for CI). `hpc_pipeline_version` in the cache key when set (F5-T3). Worker unchanged
      (F5-T4). ADR D-048. `test_nextflow_adapter.py`.
- [ ] **F5 deferred**: ~~`QMJobWorkflow→CalculationWorkflow` rename~~ — **DROPPED** (assessment
      2026-07-25): the workflow type name is durable-history state, so the rename is exactly the
      un-versioned change the workflow-versioning policy below exists to forbid; real `cclib`
      parsing once a live QM output format is fixed; live-cluster durability spike (needs a cluster).
- [x] **F6** OpenShift delivery: one rootless multi-target image (`deploy/Containerfile` +
      `entrypoint.sh`), Helm chart (`deploy/helm/chemclaw/`: ConfigMap/Secret, SA with federation,
      service/route/HPA, both workers, MCP, NetworkPolicy, pre-deploy migrate hook), `deploy.yml` CI
      (build + `helm template | kubeconform`), `deploy/README.md`. Config `otel_endpoint`. ADR D-049
      (D-A6/D-A6a: Temporal self-hosted). Offline-verified: YAML parse + brace-balance + Settings map.
- [ ] **F6 live edges** (CI/cluster-gated): actual image build+push, `helm template`/`kubeconform`,
      dry-run rollout to a dev namespace, OTel collector wiring, ExternalSecret wiring.
- [x] **F7** Generic data-source seam: `sources/base.py` (`DataSource` composes the existing
      `ElnAdapter`+`SourceRetriever` halves, `SourceSpec` rejects neither-half), `sources/registry.py`
      (`data_sources` config → `active_ingest_sources()`/`active_retrieve_sources()`). Re-hosted with
      no behavior change: `gather_evidence` fans out over the registry; `eln_sync` ingests active
      sources. All existing ELN/research tests pass unchanged. ADR D-050. `test_datasource_seam.py`.
- [ ] **F7 deferred (the first live connector)**: custom Snowflake ELN source — one registry entry
      (ingest half over the internal data pipeline) + per-source pipeline cursor over Snowflake's
      load-timestamp; Snowflake specifics stay inside that one adapter, nothing Snowflake-shaped above
      the seam. Also: LIMS/MES/analytical/literature adapters.

## Later — Phase 6 items now folded into F4 above (infra-gated pieces need live Entra/Temporal)

### Done — role-scoped skill visibility (D-052)
- [x] `RoleScopedSkillsSource` + `settings.skill_role_gates` gate advertised skills by the turn's
      ambient Entra roles, replacing F4's dead `allowed_skills` placeholder. Salvaged (the one
      superior, non-redundant piece) from the parallel `phase6-authz` branch; its duplicate
      `Principal` and second tool-authz path were dropped as already covered better by F4.
      `test_skill_access.py`.

- [x] Testing CLI (`agents/cli.py`, `make chat` / `uv run chemclaw`): interactive REPL + `-m`
      one-shot over the same `build_agent`. Identity is the Phase-6 seam — `resolve_identity`
      returns `(actor, allowed_skills)`; `--admin` bypasses the (unimplemented) Entra auth
      (all skills, `CHEMCLAW_CLI_ADMIN_ACTOR`), and the non-admin branch (Entra resolution)
      raises until 6.1/6.2 land. When Entra auth is built, wire it as that branch and gate the
      admin bypass off in hardened deployments. Tests: `test_cli.py`.

## Deep-review follow-ups (D-030)

### Done — robustness/correctness fixes (D-030)
- [x] Bounded `BAD_DATA_RETRY` (`maximum_attempts=CHEMCLAW_ACTIVITY_MAX_ATTEMPTS`) so an
      unclassified deterministic failure gives up instead of retrying forever; added
      `ValidationError`/`OrdFormatError`/`EvalCaseError` to the non-retryable names; shared the
      list with `note_publish_retry`. Test: `test_publish.py`.
- [x] Slug rejects trailing `.` and `.lock` (git-invalid `note/<id>` refs). Test: `test_note.py`.
- [x] Git subprocess timeout + kill (`CHEMCLAW_GIT_COMMAND_TIMEOUT_SECONDS`). Test:
      `test_knowledge.py::test_git_command_timeout_kills_the_child_and_raises`.
- [x] Solubility/pKa cache keys version on the reported uncertainty.
- [x] `test_mcp_transport.py` skip narrowed to a missing toolchain (won't mask a CI regression).

### Done — deferred items worked off (D-031)
- [x] Fingerprint-definition guard: each `*_fingerprints` row records its definition
      (`ecfp:r{radius}:b{bits}` / `drfp:b{bits}`); similarity search filters to the store's
      current definition so a changed radius/width + re-index can't rank incomparable bits.
      Migration `004`; runbook (vi). Guard tested in-sandbox via the in-memory store.
- [x] ELN reject re-drive: `RejectedEntry.created_at` + the WARNING log give the exact `since`
      to re-run the (idempotent) sync from after fixing a source record. Runbook (v). No
      automatic dead-letter by design (KISS).
- [x] KISS cleanups: inlined the `SolubilityModel` seam (removed Protocol + dead `model=` param);
      deleted `report.harness.gather_report` (tests assemble via `gather_section`); wired
      `note_from_confirmed_answer` into the `record_confirmed_answer` agent tool (completes plan
      5.5). Kept `StoredResult.provenance` as GxP audit metadata (docstring clarified — not read
      into logic, but a legitimate audit column + the `measured` seam).

## Admin-experience audit (configurability / error-handling / logging)

### Done — P0 observability floor (D-026)
- [x] Config-driven logging: `chemclaw/logging.py::configure_logging()` + `CHEMCLAW_LOG_LEVEL`/
      `_LOG_FORMAT`, called at both workers' entrypoints. Worker startup logs (address/namespace/
      queue/registered workflows). ELN sync logs `ingested/rejected` + a WARNING per rejection;
      both adapters log skipped broken files. Shared `chemclaw/db.py::connect` → `ConnectionError`
      "Postgres unreachable at <host>" with the DSN password redacted (not a retry-blocking
      `ChemclawError`). Tests: `test_logging.py`, `test_db.py`, ELN caplog assertions.

### Done — P1 pluggability & docs (D-028)
- [x] Cache hit-vs-compute log at the `calc/store.py` decision point (DEBUG) — the "why did this
      recompute?" trail, behind the D-026 log-level switch.
- [x] ELN adapter registry (`eln/registry.py`): `CHEMCLAW_ELN_SYNC_ADAPTER` selects the durable
      sync's source; memory jobs read `all_eln_adapters()`. Replaced the hardcoded adapter classes
      in `eln_sync.py` and `memory_jobs.py`.
- [x] `skills_dir` → OS-path-separator list via the `skills_dirs` property (add a second skills
      directory with no code change) + SKILL.md front-matter schema/template in `skills/README.md`.
- [x] MCP-attach the agent's fingerprint search (D-029): `build_agent` attaches config-driven
      `MCPStdioTool` servers (`CHEMCLAW_MCP_SERVERS`), so structural search runs over MCP and
      adding a capability is a config entry. `allowed_tools` keeps write/index tools off the
      agent. Transport verified in-sandbox (`test_mcp_transport.py`). `docs/guides/runbook.md` (iv)
      rewritten for the MCP procedure.
- [x] `make skill-validate` (D-037): `scripts/validate_skills.py` checks every SKILL.md's
      frontmatter (name/description present, name matches directory) and gates in CI, like
      kg-validate. Migrating the in-process agent tools (calculators/graph/BO) to MCP stays
      unplanned — local RDKit/BoFire functions are simpler in-process (KISS).

### Open — P2 polish
- [x] `docs/guides/runbook.md`: the four admin tasks (add skill / add-repoint DB / add-or-switch ELN
      source / add capability), the log switch, the Temporal UI at :8080, DB-unreachable message.
- [x] Startup preflight for `ANTHROPIC_API_KEY` presence (D-037): `_default_chat_client` fails
      with a clear message at agent build, not on the first turn.
- [x] Migration-status visibility (D-034): `schema_migrations` ledger records each applied file
      by name + checksum; an edited applied file is flagged as drift.
- [x] Coverage threshold in CI (D-037): `[tool.coverage.report] fail_under = 80` and CI runs
      `make lint type cov` as its gate. Floor set safely below the measured offline baseline (86%,
      Postgres/Temporal skipped; CI runs those and is higher). Ratchet upward as coverage climbs.
      *(This entry was false from the Replit restructure until D-117: the only workflow that ran
      `cov` had been stranded at `services/chemclaw/.github/`, where GitHub Actions never reads, so
      the executing gate ran `make test`. The floor is now enforced by the root workflow, and
      measured over the shipped package set rather than one that omitted `connectors/`.)*

### MAF out-of-the-box features (analysis done)
- [x] **Function middleware** (`@function_middleware`) — one DRY GxP tool-audit trail
      (`agents/audit.py::make_audit_middleware`: name/args/outcome/latency, observe-only) over all
      agent tools, on the logging floor. Attached via `Agent(..., middleware=[...])` (D-027).
- [x] **OpenTelemetry** — opt-in `chemclaw.logging.configure_telemetry()` gated on
      `CHEMCLAW_OTEL_ENABLED`; calls MAF's `configure_otel_providers` at each worker's entrypoint.
      Ships as a config toggle (default off) because the OTel SDK/OTLP exporter extras are not
      installed and are only useful with a collector — enabling it requires adding those extras
      (D-027).
- [ ] **Structured outputs** (`response_format` + `resp.value`) — force validated pydantic
      payloads for agent proposals instead of parsing prose. Deferred to the first call site that
      needs a validated payload (changes call sites, not startup wiring).
- Do-not-adopt / defer: Redis/mem0 history (durability belongs to Temporal, and neither extra is
      installed), the MAF `_harness` providers (duplicate the memory layer + background queue),
      the wholesale MAF eval harness (have `evals/`; cherry-pick only its tool-call checks). FIDES
      security layer is `@experimental` → a DEFERRED candidate for untrusted ELN/literature text.


## Done — Whole-repo production-readiness review (post-5b; commit d51f0b5, D-021)
- [x] 4 adversarial review agents over all packages; ~45 verified findings fixed with regression
      tests (134 → 169 passing). Criticals: PR-gate submitter concurrency/checkout corruption
      (lock + `note_repo_dir` config + slug-validated note ids + path containment + fetch before
      `--force-with-lease`); ELN sync poison pill (one `ChemclawError` bad-data base, sync
      catches it → reject-and-continue actually holds). Majors: temperature range mis-parse
      (`60-80 °C` → -80), stoichiometry-unsound mass balance → element subsumption, per-file
      fetch robustness, BoFire off-thread, pKa cache key engine-versioned, QM tool no longer
      recomputes completed jobs, report publish got the bounded retry discipline
      (`workflows/publish.py`), vacuous-green eval gate fails loudly. Cross-cutting: CLAUDE.md
      status un-falsified, `.env.example` complete, CI runs eval+eln-validate, dependency hygiene.
- [x] Test-helper dedup pass: one `FakeSubmitter` in conftest (replaced ~10 local fakes),
      QM tests use `tests/temporal_env.py` (inline copies + cross-test private imports gone),
      shared `tests/pg.py` Postgres bootstrap, redundant `fast_mock` fixtures deleted.
- [ ] Multi-process note-submit serialization (lock is per-process; per-submission worktrees or
      a distributed lock) — revisit when >1 background worker replica exists.

## Done — Phase 5b: report / deep-research harness (no new store — D-020)
- [x] 5b.1/5b.2 Source-agnostic harness core (`report/harness.py`) over the `SourceRetriever`
      contract + mandatory-citation `EvidenceChunk` (`report/evidence.py`).
- [x] 5b.3 Two concrete retrievers (`report/retrievers.py`): `GraphRetriever` (Phase 2) +
      `FingerprintReactionRetriever` (Phase 3) — thin adapters, no new store.
- [x] 5b.4 Adversarial verify (`verify_claims`): a claim survives only if it cites retrieved
      evidence; uncited/fabricated claims discarded. Unsupported sections marked, not invented.
- [x] 5b.5/5b.6 Durable `DevelopmentReportWorkflow` (per-section activities = resumable long runs),
      each section declares its memory layer (structural provenance separation). Registered on bg worker.
- [x] 5b.7 Draft is a PR-gated `report` note citing every source. `development-report` skill (judgment:
      decompose, write only what evidence supports, keep evidenced vs analogy apart).
- [x] CHECKMATE 5b (G1–G7 + citation fidelity): core correct (verify_claims guards the `all([])`
      trap; every chunk cited), no new store. 4 fixes — (F1/F2) report id is now ref-safe + unique
      (slug + title hash) instead of a raw slug that broke git branches and collided across titles;
      (F3) fingerprint-retriever citation honesty documented (PR-gate catches a pending-note link);
      (F4) `load_notes` resilient to a malformed note (no longer aborts retrieval); + docstring
      honesty on substring matching and the verify gate. **Phase 5b complete.**

## Done — Agent-harness backbone core (MAF Agent Harness — D-038, docs/guides/harness-konzept.md)
- [x] H0 spike: verified `create_harness_agent` in the installed `agent-framework-core` 1.11
      constructs with no LLM call; providers reduce to `TodoProvider`+`AgentModeProvider` when the
      generic batteries are off; default modes are `plan`/`execute`; `todos_remaining(looping_modes=
      ["execute"])` binds the loop to execute mode natively.
- [x] H1/H2/H3(loop): `build_agent` wires the harness behind `harness_enabled` over the *same*
      tools/skills, classic `Agent` fallback stays default; file-memory/file-access/shell/web
      batteries disabled (§6, G6); `harness_autonomy` gates the loop (`plan_only` interactive /
      `execute` looped-in-execute-mode), hard-capped by `harness_max_loop_iterations`. Config in
      `chemclaw/config.py` + `.env.example`; 8 tests in `tests/test_agent.py` (backbone select,
      provider set, same tools, batteries off, loop present/absent + bounded). `make lint type test`
      green (133 passed, 15 offline-skipped).
- [x] Evaluation: the agent harness does **not** replace Temporal or graph-based flows — Phase 5b's
      report pipeline is a deterministic core + Temporal workflow, no MAF graph-workflow code exists;
      complementary third backbone (see D-038, harness-konzept §11).
- [x] Re-integrated onto the post-5b/D-037 main: harness branch now reuses main's history,
      deterministic compaction (D-025, passed as last context provider), GxP audit middleware
      (D-027), role-filtered skills, and MCP capability tools (D-029). ADR renumbered D-020→D-038
      (D-020 was taken by the report harness on main).
- [x] The awaiting-todo half of the resume follow-up: flipping the todo on job completion, closed —
      see F3-T3 above and `agents/harness_todo.py` (D-058).
- [ ] **Follow-ups (still open):** resuming the *same* streamed turn mid-flight (vs. picked up on the
      session's next turn) via the durable-approval seam (D-032/D-035) · plan/loop metrics for Phase
      2b · plan-mode approval + finer autonomy behind RBAC (Phase 6, authz in the MCP server) ·
      agent-harness ↔ report-pipeline interplay (open research per section vs. fixed synthesis flow).

## Done — Phase 5: memory layers (episodic + semantic, no new infra — D-019)
- [x] 5.1/5.2/5.3 episodic: `memory/chains.py` (chain detection — product A = reactant B via the
      canonical-SMILES compound identity, Phase 3) + `memory/campaign.py` (`campaign` note citing each
      member reaction via wikilinks) + `memory/jobs.py::synthesize_campaigns` + Temporal workflow.
      `campaign-narrative-synthesis` skill (judgment; every claim cites a member reaction).
- [x] 5.4 semantic: `memory/playbook.py` (`find_playbook_candidates` — DRFP similarity across ≥2
      projects; `playbook_note` with mandatory evidence refs) + `distill_playbooks` job + workflow.
      `playbook-distillation` skill (transferable-only, process-chemist approval).
- [x] 5.5 user interaction as a 4th source: `memory/interaction.py` (`interaction` note via the same
      PR-gate); reachable via the `record_confirmed_answer` agent tool (synchronous) and the durable
      `InteractionApprovalWorkflow` (async Yes/No hold — D-032). 5.6 retrieval separation: judgment in
      the playbook skill (evidenced vs analogy kept visibly separate; experiment outranks analogy).
- [x] Jobs registered on the background worker; `project` field added to `OrdReaction`/adapter.
- [x] CHECKMATE 5 (G1–G7 + no-new-infra check confirmed): 3 findings fixed — (F1, G4) a degenerate
      reaction is skipped in `find_playbook_candidates` instead of aborting the whole distillation;
      (F2) a cyclic chain is flagged `ordered=False` and the campaign note says so, not a fake causal
      sequence; (F3) the merged-reaction-notes precondition for citations is documented (kg-validate
      enforces it). Also stabilized a pre-existing flaky BO test by seeding BoFire (`bo_seed` config).
      **Phase 5 complete.**

## Done — Phase 4: ELN ingestion (adapter pattern) — COMPLETE
- [x] 4.1 Stable ORD-subset schema (`eln/ord.py`: `OrdReaction`/`Component`/`Role`) — ELN-agnostic;
      `reaction_smiles()` for DRFP, role consistency validated.
- [x] 4.2 Adapter contract (`eln/adapter.py`: `RawEntry` + `ElnAdapter` Protocol —
      `fetch_new_entries`/`map_to_ord`). Only the contract is fixed (G6).
- [x] 4.3 One concrete adapter (`eln/json_adapter.py`, JSON-export ELN): structured mapping +
      deterministic free-text regex (temperature/time). No universal abstraction (D-018).
- [x] 4.4 `eln-reaction-extraction` skill (judgment: structured-first, per-field LLM fallback,
      validation gate) + `eln/validate.py` (RDKit parse + atom/mass balance) + `make eln-validate`
      / `scripts/validate_ord.py`. LLM-per-field wiring deferred (D-018).
- [x] 4.5 Durable ELN sync (`eln/sync.py` core + `workflows/eln_sync.py` activity/workflow on the
      background queue): fetch → map → validate → **index reaction+compound fingerprints** (Phase 3)
      + **PR-gated `reaction` note** (Phase 2). Reject-and-continue; idempotent. Registered on the
      bg worker. Seed corpus in `eln/exports/`. Server test in CI; full chain tested in-memory.
- [x] CHECKMATE 4 (G1–G7 + deep review over Phase 3+4): end-to-end chain sound; 3 real bugs fixed —
      (F1) mapping failures (unknown role / schema violation) now raise a contract-level
      `ElnMappingError` so the batch sync rejects-and-continues instead of aborting (also removes a
      G6 leak); (F2) structured `temperature_c`/`time_h` of `0` no longer discarded as falsy by the
      `or` fallthrough (ice-bath 0 °C preserved); (F3) temperature regex now requires the degree sign
      so `13C NMR`/`pH 7 C` can't fabricate a temperature; + dead-param cleanup. **Phase 4 complete.**

## Done — Phase 3: fingerprint search (molecules + reactions) — COMPLETE
- [x] 3.1 `mcp-molfp` capability: ECFP4 (Morgan r2, 2048-bit) via RDKit (`mcp_servers/molfp/
      fingerprint.py`), config-sized, deterministic. Thin FastMCP `server.py` advertises the tools.
      (Dir is `mcp_servers/`, not `mcp/` — the `mcp` name is the SDK's, D-016.)
- [x] 3.2 Postgres `bit(2048)` table + HNSW `bit_jaccard_ops` index (`infra/sql/002_...sql`) +
      `PostgresFingerprintStore` (Tanimoto in SQL). In-memory backend proves the ranking everywhere.
- [x] 3.3 `find_similar_molecules(smiles, top_k)` (Tanimoto, threshold+top_k from config) +
      `find_substructure_matches` (exact RDKit match), backend-agnostic (`mcp_servers/molfp/search.py`).
- [x] 3.5 `reaction-search` skill: the judgment (similarity vs substructure, what Tanimoto counts as
      precedent, combine with metadata/graph) — thresholds in config, not code (G6).
- [x] 3.4 `mcp-rxnfp` (DRFP reaction fingerprints, `mcp_servers/rxnfp/`) + `find_similar_reactions`
      + thin FastMCP server + `infra/sql/003`. Reactions are the 2nd fingerprint domain, so the
      Tanimoto store is now the **generic** `mcp_servers/fpstore.py` shared by molfp+rxnfp (D-017,
      DRY); molfp refactored onto it (molecule tests still green = no regression). `reaction-search`
      skill covers both molecule and reaction search.
- [x] CHECKMATE 3 (G1–G7 + deep review): core correct, MCP/skill split clean, threshold configurable.
      4 fixes — (F1) docstrings no longer overclaim exact HNSW ordering (approximate NN, up to recall);
      (F2) `bit(N)` width derived from `ecfp_bits` (single source; mismatch fails loud, not silent pad);
      (F3) substructure docstring clarified (SMARTS-first); (F4) all-zero-fp guard noted. **Molecule
      path complete.**



## Done — Phase 2b: evaluation & metric layer (cross-cutting)
- [x] 2b.1 Metric interface: pure `Metric = (EvalCase) -> MetricResult` + registry
      (`evals/metric.py`, `@metric` decorator = the 2b.5 extension seam). Thresholds from config (G3).
- [x] 2b.2 Eval harness (`evals/harness.py`): `run_eval` over a versioned case-set +
      `render_report` (citable Markdown, case id + provenance per row) + `load_eval_cases`
      (frontmatter files) + `make eval` CLI. Cases versioned in `evals/cases/` (D-014).
- [x] 2b.3 Seed metrics (`evals/metrics.py`): green-chemistry **E-factor** + **PMI** (mass balance),
      **prediction_error** (vs held-out reference), **bo_regret** (1d.6). All pure, config-gated.
- [x] 2b.4 Per-task tool-utility A/B (`evals/ab.py`): direction-aware delta, buckets help/hurt/
      no-effect over a task set — proves ≥1 case where tooling does NOT help (F8/F9 steering).
- [x] 2b.5 Wiring: each later capability phase registers ≥1 metric via `@metric`; regressions are
      pinned by the test suite (expected pass/fail per case), not a CI hard-gate (the seed set
      deliberately holds a failing case to prove gating).
- [x] CHECKMATE 2b (G1–G7 + deep review): 5 robustness findings fixed — (F1) `EvalCase`
      `extra="forbid"` so a misspelled frontmatter key can't silently drop and mis-score;
      (F2) unknown metric name wrapped as case-named `EvalCaseError`, not a raw traceback;
      (F3) mass coercion routes through the guarded `_scalar` (no escaping `TypeError`);
      (F4) mass-balance violation (product > input) rejected, not a negative-E gate pass;
      (F5) `bo_regret` provenance/docstring corrected (signed, not `|abs|`). **Phase 2b complete.**

## Prior — Phase 2: knowledge graph + PR-gate
- [x] 2.1 Note schema (`kg/note.py`, one pydantic model); 2.2 parser (frontmatter → Note, clear errors).
- [x] 2.3 Wikilink extraction + NetworkX indexer (`kg/graph.py`, `neighborhood` 1–2 hop traversal).
- [x] 2.4 Validation CLI (`kg/validate.py`, `make kg-validate`) — broken links / dup ids / bad notes; in CI.
- [x] 2.5/2.6 skills `knowledge-graph-query` + `knowledge-graph-write` (judgment).
- [x] 2.7 **PR-gate** built once (`kg/pr_gate.py` `propose_note` + `NoteSubmitter` seam + `kg/render.py`);
      agent-only, notes land at `<knowledge_dir>/<type>/<id>.md` on a per-note branch. Tested with a fake.
- [x] 2.6b real `NoteSubmitter`: `kg/git_submitter.py` `GitNoteSubmitter` (branch off base, write, commit,
      push) — tested against a local bare remote. PR-object creation is the git platform's step.
- [x] 2.8 Temporal activity `write_knowledge_node` (`workflows/knowledge.py`): QM result → agent
      `job-result` note (links to a method-independent compound id) → PR-gate. Registered on the bg worker.
- [x] Agent tools for graph query/write (`agents/graph_tools.py`: find_notes, expand_note,
      propose_knowledge_note) registered on the MAF agent; shared `default_submitter` (DRY).
- [x] Wire `write_knowledge_node` into a workflow caller: `QMJobWorkflow` gains opt-in
      `publish_to_graph`, routing the note write to the background-jobs queue (best-effort). Server test.
- [x] CHECKMATE 2 (G1–G7 + deep review over Phase 1+2): 5 findings fixed — (F1) bounded retry so
      best-effort publish gives up instead of hanging; (F2) job-result note no longer dangling-links a
      non-existent compound note (would fail kg-validate); (F3) git submitter idempotent on identical
      re-submit; (F4) stray `body:` frontmatter key no longer crashes the parser; (F5) dedicated
      note-write timeout/attempts config. **Phase 2 complete.**

## Later compute items (reprioritized; HPC/DFT deferred — D-010)

### Phase 1b — Result store / calc cache (first-class; "never compute twice") — DONE
- [x] 1b.1 Store interface `get/put` (Protocol); 1b.2 versioned key `(calc_type, calc_version, input_hash, params_hash)`.
- [x] 1b.3 In-memory backend (tests) + Postgres backend (`calculation_results` table) + `make db-migrate` + CI DB.
- [x] 1b.4 One `cached_compute()` path (lookup-before-compute, DRY); returns was_cached for hit/miss metric.
- [x] 1b.5 Temporal lookup/persist activities — folded into 1c.5 by design (no stub); checkbox was
      stale, cleared 2026-07-25.

### Phase 1c — Fast predictors + semiempirical (first *real* calculations)
- [x] 1c.2 **xTB / GFN2** calculator via `tblite` (real single-point energy, RDKit 3D embed, CPU) —
      `calc/xtb.py`, cached through the store (`run_cached_xtb`). Real GFN2 tests run everywhere.
- [x] 1c.1 Calculator **contract**: `calc.store.run_cached` (offload blocking compute → store dict →
      reconstruct typed model) — each `run_cached_*` now only derives its key and delegates (DRY,
      Rule of Three across xTB/solubility/pKa). Name→calculator **registry deferred** (no dispatch
      consumer yet; would be a one-caller abstraction — D-015).
- [ ] 1c.3 GNN solubility model (inference only; value + uncertainty) — **needs model choice** (see open Qs).
      **Blocked on user input** (which GNN + weights/license); the calculator contract makes the swap cheap.
- [x] 1c.4 **pKa via xTB** (`calc/pka.py`): GFN2-xTB ALPB-solvated deprotonation energy of the most
      acidic O-H/S-H site + linear calibration (R²0.93 over 10 acids). Agent tool `predict_pka`. Real tests.
- [x] 1c.5/1c.6 xTB exposed to the MAF agent as tool `compute_xtb_energy` + `calculation-selection` skill.
- [x] **X1 xTB capability seams** (`docs/guides/xtb-tools-proposal.md`, D-095): `calc/structure.py`
      (content-addressed `Structure`) + `calc/xtb_spec.py` (`XtbSpec`, the one cache-key derivation);
      `calc/xtb.py` ported onto them with its public API and energies unchanged.
- [x] **X2 properties + site reactivity** (`calc/xtb_props.py`): `compute_electronic_properties`
      (HOMO/LUMO/gap, dipole, Mulliken charges, Wiberg bond orders — all read from the SCF the energy
      calculator already ran) and `predict_site_reactivity` (condensed Fukui indices, three single
      points) + the `reactivity-descriptors` skill. No new dependency.
- [x] **X3 geometries + thermochemistry** (D-098): `calc/xtb_opt.py` (scipy L-BFGS-B over tblite's
      analytic gradient — no `ase`), `calc/xtb_thermo.py` (finite-difference Hessian, quasi-RRHO
      thermochemistry, **and IR intensities**, which came free from the dipole the same SCF
      produced), `calc/xtb_scan.py` (relaxed scans). Validated against measurement: water's entropy
      45.05 vs 45.10 cal/mol/K. Found and fixed three defects — open-shell energies had no
      spin-polarization term (triplet O2 came out *above* singlet), the optimizer's first step could
      collapse a bond, and ordinary molecules optimize onto rotor saddle points.
- [x] **X4 the composite** (D-098): `calc/reaction.py` — `compute_reaction_energy` (balance
      enforced, every species treated identically, per-species cache reuse) and
      `compare_solvent_effects`. Homolysis/BDEs work because multiplicity is read from the SMILES'
      own radical electrons.
- [x] **Durable routing for the expensive xTB tasks** (D-098, brought forward from X5): the
      inline-vs-Temporal decision the phase turned out to need. `calc/xtb_cost.py` predicts the cost,
      `XtbJobWorkflow` runs what is over budget on the existing `hpc-jobs` queue, and
      `get_qm_job_status` is generalized to `get_job_status` across both job kinds.
- [x] **X5 the `xtb` binary** (D-101): `calc/xtb_cli.py`, a hardened argv-only subprocess backend
      selected by `settings.xtb_engine`. ANCopt is **8-11x faster** than the Cartesian optimizer on
      drug-sized substrates; GFN-FF optimizes 118 atoms in 0.7 s. The binary supplies the Hessian;
      the validated RRHO stays in `calc.xtb_thermo`, so both backends reproduce water's measured
      entropy identically.
- [x] **X6 CREST ensembles** (D-101): `calc/crest_cli.py` + `calc/conformers.py` — conformer,
      tautomer and protomer searches with degeneracy-weighted populations and the conformational
      entropy every single-conformer free energy is missing. `compute_reaction_energy` gains
      `level="thorough"`. The system's first non-deterministic calculator; the store is what makes
      it stable.
- [x] **X7 the expert seam** (D-101): `run_xtb_task` over a typed spec, role-gated by default.
- [x] **X9 ANC preconditioning** (D-102): X5 retired the *general* case, not the scope. Relaxed
      scans (frozen atoms are not an xtb flag) and radicals (the binary cannot spin-polarize) still
      run the in-process optimizer, and a scan pays that cost once per point. Optimizing in the
      eigenbasis of a Lindh model Hessian gives a measured **~2x** on both. The remaining headroom
      is an angle/torsion model with a Wilson B matrix — recorded, not built, because 37% of the
      pairwise model's directions have no curvature and a floor stands in for them.
- [ ] **X10 transition states** — the largest remaining gap at the *model* level, unchanged by
      X5-X7. There is no saddle-point search, so every "how fast" question is unanswerable and a
      relaxed-scan maximum is a sketch of a barrier rather than one. `xtb --path` (the reaction-path
      finder) and CREST's transition-state tooling are the obvious routes.
- [x] **X11 CREST's unexploited searches** (D-104): `--nci` is now `calc.complexes` +
      `compute_interaction_energy` + the `molecular-association` skill — the only route in the
      system to a question about two molecules together, validated against CCSD(T)/CBS to a few
      tenths of a kcal/mol. **U2 (basic amines) is half solved and half refused**, which the
      measurement decided rather than the plan: aromatic/aryl nitrogen calibrates to Spearman
      **1.000** (RMSE 0.17, better than the acid path) and ships; aliphatic amines rank at
      **-0.17** and are refused, because a continuum solvent cannot represent the ammonium ion's
      hydrogen bonding to water and no linear recalibration recovers a non-monotonic relationship.
      The `--protonate`/`--deprotonate` *structural* route was not needed for that split and is
      left unbuilt. See `docs/guides/xtb-skill-catalogue.md` §9 for the skills these unlock.

### Ranked out of the xTB use-case review (`docs/guides/xtb-use-cases.md`) — above X3 in value

- [ ] **U1 xTB descriptors as BO featurization** — BoFire campaigns treat ligand/base/solvent as
      *categorical*, so the surrogate cannot generalize to an option never tried. Replacing the
      category with computed electronic descriptors lets it interpolate across the space. Needs **no
      new xTB capability** — only wiring `calc.xtb_props` into `bo/`. Highest value per unit of work
      in the review.
- [ ] **X9 internal-coordinate optimizer** — measured on the stated workload (200-800 Da): the
      atorvastatin core (76 atoms) needs **177 Cartesian L-BFGS steps** and 97 s to optimize, and
      the step count grows with size. A redundant-internal-coordinate optimizer typically cuts that
      3-5x, which is the single largest speedup available for this workload and compounds through
      every scan point and every species of a reaction. The Cartesian optimizer was the right first
      choice (dependency-free, easy to reason about); it is now the bottleneck.
- [x] **X8 the calculators as an MCP server** (D-103): `mcp_servers/calc` hosts the seven tools
      that compute; the four that submit durable jobs stay in-process because they need the turn's
      actor and session. `scripts/validate_skills` now resolves a declared tool against MCP
      `allowed_tools` too, so a skill names a capability and the transport is a deployment
      decision — no skill changed in the move.
- [ ] **U2 pKa domain extension to bases / N-H acids** — v1 covers neutral O-H/S-H acids only, so
      the most common pharma pKa question (a basic amine API) is unanswerable; the tool errors out,
      which is correct but not useful. A calibration + domain problem, not a new capability.
      **Priority raised** on measured evidence: see U3.
- [ ] **U3 pKa accuracy characterized** — benchmarked against 12 experimental values spanning
      pKa 0.2–15.9 (`tests/test_pka.py`): Spearman ρ **0.965**, RMSE 1.25 (so the reported ±1.6 is
      honest), **worst individual error +2.08**. Conclusion, now enforced by tests and carried by the
      `ionization-and-partitioning` skill: **rank with it, never set a process pH with it** — a
      2-unit error inverts a "pKa ± 2" extraction or salt rule. No further action required unless the
      calibration is revisited; recorded so it is not re-derived.
- [ ] **U4 descriptor enrichment of ELN-ingested structures** — compute descriptors once per ingested
      substrate so the graph becomes searchable by electronic character, not just substructure.
      Cheap (cached forever) and it makes retrieval smarter. Available now; not built.
- [x] 1c.5b calculator contract landed (see 1c.1); name-registry consciously deferred (D-015).
- [ ] 1c.7 optional graph note via PR-gate for a *fast* calc result — deferred: the QM path already
      publishes (2.8) and BO recommendations now publish (1d.5); a fast-calc publish waits for a real
      need (avoids a third near-identical mapper before it is asked for). CHECKMATE 1c: G1–G7 met.
- Note: fast calcs run **without** a Temporal workflow (sub-second) — the store gives "never twice";
  durability (Temporal) is reserved for long jobs (BO campaigns 1d, later HPC).

### Phase 1d — Bayesian optimization (BoFire, pulled forward)
- [x] 1d.1 Domain adapter (`bo/engine.py`, BoFire fully encapsulated behind neutral `bo/problem.py` types).
- [x] 1d.2 ask/tell: `initial_candidates` (random seed) + `propose_candidates` (SOBO); `optimize()` loop
      (`bo/campaign.py`) — convergence-tested on known minima/maxima (CHECKMATE 1d spike met).
- [x] 1d.2b categorical BO support (`CategoricalParameter`) + real reaction benchmark:
      **Reizman Suzuki–Miyaura** (`bo/benchmarks/reizman_suzuki.py`, data vendored from Summit/MIT),
      RandomForest yield surrogate → BoFire mixed categorical+continuous campaign beats dataset median.
- [x] 1d.4 **durable BO campaign**: `BoCampaignWorkflow` (Temporal) + activities (heavy BoFire work
      isolated) + `bo/objectives.py` name→objective registry + **`workers/background_worker.py`**
      (first real background-jobs job — retro-satisfies 1.8, no empty stub). Server test runs in CI.
- [x] 1d.3 **calculator-backed objective**: `solubility_objective(store)` (cached solubility via the
      store) registered as `solubility_max`, plus `molecule_library_problem`. **Candidate-set BO works**:
      BoFire drives a pure-categorical domain by exhaustive-discrete acquisition — finds a top molecule
      without evaluating the whole library (test: best found evaluating 9/14). Constraint: evaluation
      budget must be < library size, else the unique-candidate pool exhausts.
- [x] Robustness: `optimize` and the durable BO workflow stop gracefully when a discrete candidate
      set is exhausted (`discrete_candidate_count`/`distinct_candidate_count` guard) instead of crashing
      inside BoFire. Tests: budget 2+10 over a 4-molecule library returns cleanly.
- [x] 1d.5 recommendation PR-gated: `workflows/bo_knowledge.py` (`note_from_campaign_result` +
      `write_campaign_node`) maps a campaign's best point to an agent `bo-candidate` note through the
      **same** PR-gate the QM path uses (DRY: reuses `propose_note`/`default_submitter`). Opt-in
      `CampaignSpec.publish_to_graph` routes it to the background queue, best-effort with bounded
      retry (mirrors QM 2.8). Registered on the bg worker. Pure mapper + PR-gate tests; server test in CI.
- [x] 1d.6 progress/regret metric: `bo_regret` registered in the Phase 2b metric layer
      (`evals/metrics.py`, direction-aware, non-negative) — Phase 1d's registered scientific metric.
- [x] CHECKMATE 1d: G1–G7 met (recommendation publish mirrors the deep-reviewed QM path; best-effort
      + bounded retry; no dangling wikilink; idempotent note id). **Phase 1d complete.**

## Done
- [x] **Phase 0** — foundation (tooling, config, infra compose, CI, ADR-0001, layer READMEs). CHECKMATE 0 green.
- [x] **Phase 1 spine (1.1–1.6, 1.9)** — hpc worker; `QMJobWorkflow` + activities (mock HPC, heartbeat poll,
      parse); agent tools `submit_qm_job`/`get_qm_job_status`; MAF agent + `qm-job-submission` skill;
      `requested_by` audit field; shared Temporal client + result models. Server-backed tests run in CI.
- [x] **Orchestrator** — reconsidered MAF vs LangGraph → keep MAF (D-013).
- Folded/deferred Phase-1 tails: **1.7** notify callback (defer until an async result must reach a live
  session); **1.8** background-jobs worker — **DONE** (`workers/background_worker.py`, hosts the BO
  campaign); **1.10** → generalized into **Phase 1b**. **CHECKMATE 1** (worker-restart durability spike)
  runs against a live Temporal (`make up`) — pending, needs a live cluster (not runnable in sandbox).

## Capability gaps to triage (from `docs/archive/research-review.md`) — decide per item
- [x] **Evaluation / scientific-output metrics layer** → promoted to first-class **Phase 2b**
      (see plan + D-009). No longer a backlog decision.
- [x] **Chemical/biological safety layer** (D-080) — shipped as a deterministic, **advisory**
      structural screen: committed cited SMARTS table (`safety/rules.yaml`), `screen_structure`/
      `screen_reaction`, the `screen_hazards` agent tool + `safety-screening` skill, a `kg-validate`
      gate requiring a `## Hazards` section on flagged agent-proposed procedures, and the
      `hazard_flag_recall` metric (gated at 1.0) so a silently-broken SMARTS fails `make eval`.
      Invariant: the system flags, it never certifies — an empty result reads "no rule matched",
      never "safe". Non-goals (each still open, none implied): GHS/SDS database, toxicity
      prediction, route-level verdicts, regulatory classification. Config: `safety_rules_path`,
      `safety_gate_severity`, `safety_gate_enabled`, `eval_hazard_recall_min`. Original entry:
      distinct from Entra-ID/RBAC (IT security).
      GxP / data-integrity + hazard checks. **Kept in backlog** (user decision); decide scope
      before any capability phase that could propose a hazardous route/procedure. **Assessment
      2026-07-25: that precondition is already past** — BO recommendations (1d.5) and development
      reports (5b) publish agent-authored procedures today with no hazard awareness anywhere in the
      tree. Promoted to **wave C2** with a proposed advisory-only, deterministic slice (committed
      SMARTS rule table + `@tool` + skill + `kg-validate` hazard-section rule + a recall metric);
      three scope questions await the user — `docs/archive/plans/backlog-plan.md` §3/§5.
- [ ] Retrosynthesis + reaction prediction · DoE/Bayesian optimization · lab automation/SiLA2
      closed-loop · process flowsheet synthesis · multimodal analytical data · domain foundation
      models — all currently in `docs/planning/DEFERRED.md` with triggers; confirm or pull forward.
- [x] Design caution "apply Skills/tools **selectively + measured per task**" — **satisfied**:
      `evals/ab.py` (2b.4) measures per-task tool utility including where tooling hurts, and
      `AgentProfile` (D-075) narrows the toolset per use case. Nothing left to build.
- [ ] Design caution: evaluate the CoALA memory layer against DMR/LongMemEval, not by assumption —
      deferred with AG-13 (needs an external benchmark + a live LLM to score it).

## Open questions / awaiting input (see `docs/archive/research-review.md`)
- [ ] **"pKs models"** — interpreted as **pKa** prediction; confirm (could mean PK/ADMET). The
      pluggable calculator registry (1c.1) makes a rename/swap cheap.
- [ ] **Which models** for solubility (GNN weights + license?) and pKa (tool/model)? xTB binary
      availability + license in the target runtime.
- [ ] BoFire scope for v1: which problem (reaction-condition? formulation?) is the first real BO case?
- [ ] Temporal vs. Restate/DBOS/Prefect/Dapr — no head-to-head source found; our choice stands
      on maturity/fit. Revisit if operability/cost becomes a concern.
- [ ] When does Markdown+NetworkX tip to Neo4j/Memgraph + GraphRAG? (deterministic traversal
      sidesteps the NL-query risk for now.)
- [ ] Concrete lab-automation/SiLA2 + DoE + retrosynthesis integration wiring.
- [ ] Domain safety/compliance layer design beyond RBAC.

## Later
- [ ] Phase 2 knowledge-graph core + PR-gate · Phase 3 fingerprint search · Phase 4 ELN
      ingestion · Phase 5 memory layers · Phase 5b report harness · Phase 6 identity/RBAC.

## Post-campaign follow-ups (2026-07-24, D-072) — worked off 2026-07-25

Assessed then implemented per `docs/archive/plans/backlog-plan.md` (waves A/B/C); all six are now closed.

- [x] **ELN late-file detection** — both file adapters compare a dropped file's mtime against the
      fetch floor and emit one aggregated WARNING naming the late files plus the backfill recovery
      (`eln/adapter.py::is_late_arrival`/`warn_late_arrivals`). Runbook §(v). Tests: `test_eln.py`.
- [x] **Memory cluster merge/shrink supersede** (D-078) — `memory/supersede.py` retires the notes a
      run's clusters replaced: `valid_to` closed (dropped from current-evidence sweeps, never
      deleted) plus a plain-text successor line, proposed through the same PR-gate from inside the
      three `build_*_notes` builders. This also auto-retires notes minted under the old set-derived
      ids, so the one-time manual cleanup noted here is no longer needed. Tests: `test_memory.py`.
- [x] **`system-eval-drift` consumer surface** — each alert is logged at WARNING where operators
      already look (a vanished metric stays distinct from a 0.0 score), and the runbook §(vii)
      documents the SQL for the durable channel. No UI, by design. Tests: `test_eval_drift.py`.
- [x] **Deployment docs** — runbook + `deploy/README.md` cover `CHEMCLAW_ENTRA_REQUIRED` (with the
      exact refuse-to-boot message) and the removed `CHEMCLAW_ENTRA_CLIENT_ID`; `values.yaml`
      records why the background worker stays at one replica.
- [x] **Substructure match compute bound** — the match loop runs in a worker thread under
      `substructure_match_timeout_seconds`, so an adversarial SMARTS no longer stalls every
      session's stream. Documented limit: the bound frees the event loop, not the CPU.
      Tests: `test_molfp.py` (incl. loop responsiveness).
- [x] **Workflow versioning policy before first live deploy** (D-079) —
      `docs/guides/workflow-versioning.md` + deploy checklist: what counts as a logic change, patch-gate
      vs drain, and why there is no CI guard. Today's un-gated changes need no retroactive patches
      (no live histories); binding from the first production deploy.
