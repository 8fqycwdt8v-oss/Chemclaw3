# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/planning/implementation-plan.md`
(phase/step numbers) at session end.

## Open — Left by the whole-codebase security sweep (2026-08-06)

Eleven disjoint review lanes, every finding re-checked by a second agent required to execute a
repro: 43 confirmed, 2 refuted, 3 already tracked. Records:
`D-2026-08-06-the-caller-chooses-the-kid-not-the-workload`,
`D-2026-08-06-a-redactor-that-only-reads-the-message`,
`D-2026-08-06-a-pair-rule-is-a-cross-product`,
`D-2026-08-06-a-swallowed-write-reported-as-a-store`,
`D-2026-08-06-an-envelope-that-only-survives-its-own-process`.

**Untrusted content that reaches the model unframed.** `frame_untrusted` is applied at five call
sites; these are not among them. Deferred from the framing pass deliberately: the envelope wraps a
prose *string*, while each of these returns a structured model, so covering them is a decision
about which fields to wrap without corrupting the shape the model reads — a design question, not a
mechanical fix. Ranked by how attacker-reachable the content is.

All four are closed by D-2026-08-06-a-mitigation-shipped-and-left-switched-off, and the shape
decision the section describes turned out to be smaller than stated: every one of the five existing
call sites already frames *a named free-text field of a structured result*, so three of these are
that pattern applied to one more field each. **The lane's real find was underneath them** — the
envelope tag was per-process in every shipped deployment, because the chart never set
`framing_envelope_secret` while running postgres sessions behind six replicas. Content framed by one
pod was replayed by another as ordinary text, and `framing.py` claimed a `Settings` warning that did
not exist. Both fixed.

- [x] **[M] `find_past_jobs` returns other users' free-text job rationales unframed** — framed.
      `summary` deliberately is not, and that is pinned: it is written by the bundle's own code, and
      a marker applied to our own output dilutes what the envelope means.
- [x] **[M] No connector/MCP tool result is ever framed** — **one tool, not a surface.** Measured
      against what each bundle returns: `fetch_artifact` is a file a pipeline wrote; everything else
      is a number, a key or a name the bundle computed, and ELN text already arrives framed because
      it reaches the model as a *note*. Framed in the connector, since core sees a connector result
      through a generic MCP boundary and cannot know which field of an arbitrary payload is
      untrusted — which is legitimate only because the tag is now deployment-stable. A manifest
      declaration (the shape `endpoint.privileged` took) is the right answer at a *second* such
      tool; that is the recorded trigger.
- [x] **[L] `recall_observations` returns corpus-mined free text unframed** — framed. The ungated
      tier had the least review and the most direct reading, which inverted the ordering the gate
      exists to create.
- [x] **[L] `gather_evidence` frames `chunk.content` but not the same note's `source`** — closed by
      *reducing* rather than framing. A provenance label only has to be recognisable, so stripping
      it to an identifier's charset removes the capability instead of marking it — the treatment
      `frame_untrusted` already applies to the envelope's own `id`, against the same threat.
## Open — Quality findings left by the whole-codebase security sweep (2026-08-06)

Eleven disjoint review lanes, each finding re-checked by a second agent required to **execute** a
repro rather than re-read the code: **43 confirmed, 2 refuted, 3 already tracked**. Six work
packages shipped (#135–#140, ADRs `D-2026-08-06-*`). These are the confirmed findings those
packages did not close, at the severity the verifier corrected them to. Every one has a file:line
and was reproduced; none is a reading.

**Data plane and knowledge integrity**

- [x] **[M] ELN free text becomes real knowledge-graph edges** — closed by
      D-2026-08-06-a-wikilink-is-an-edge-not-a-word. Escaped once at the composition point rather
      than per field, which is sound because the mapper emits no wikilinks of its own — an
      invariant its docstring has always claimed and a test now asserts, so if that ever changes
      the escaping is told to move. Escaped rather than stripped: the reviewer is the control, so
      they must see what was attempted.
- [x] **[M] A report note wikilinks non-note evidence ids** — closed by the same ADR, and the
      shipped scope is wider than the row implies: *two* live retrievers return non-note ids
      (`warehouse` gives `<source>:<row key>`, `vendored_dataset` gives `vendored:<dataset>:<index>`),
      so any report drawing on either was refused by `kg-validate` for a relation nobody wrote. The
      rule is the **reader's** — link a target exactly when `cited_ids` returns it unchanged —
      because a hand-written "no colons" rule is measurably cruder: it would also refuse `[[:x]]`
      and `[[rel:]]`, which are dangling citations rather than forged relations and want a different
      answer.
- [ ] **[M] Two enabled ELN sources with the same entry id silently collapse** onto one note and
      one fingerprint row (`ingest/eln/ingest.py:51`), contradicting the manifest's stated
      per-source guarantee.
- [ ] **[L] `vector.server_embed_function` reaches the SQL text unchecked**
      (`ingest/eln/warehouse/binding.py:462`), so the module's "only checked identifiers are
      written" invariant is false. Distinct from the documented `where:` trust boundary.
- [ ] **[L] A warehouse row key is interpolated into a filesystem path** with no slug validation
      (`ingest/eln/warehouse/retriever.py:184`).

**The store seam** — measured by the Q-A lane rather than assumed. The ten `Protocol + InMemory +
Postgres` triads are *not* one abstraction waiting to be extracted; what is genuinely shared is the
connect/execute plumbing, and the divergences below are the real prize.

- [ ] **[M] Two of the ten stores read/write a database the migrator never touches**
      (`agent/turn_cost_store.py:60`, the `session_store_dsn` split).
- [ ] **[L] `InMemoryStore.find` raises `TypeError`** on any row with a timezone-aware
      `created_at` (`science/calc/store.py:250`) — the in-memory and Postgres halves disagree.
- [ ] **[L] Only one of the three jsonb writers rejects non-finite floats**
      (`science/calc/postgres_store.py:116`).
- [ ] **[L] The Postgres connect helper is hand-rolled 14 times**
      (`science/calc/postgres_store.py:74`), including five byte-identical docstrings and four that
      say "one place, DRY".

**Complexity hotspots** — the defects the complexity was hiding, which is what the lane was asked
for rather than a decomposition proposal.

- [ ] **[M] One non-UTF-8 ORD export aborts the entire ELN sync batch**
      (`ingest/eln/ord_adapter.py:110`), contradicting the adapter's skip-and-continue contract.
- [ ] **[M] `evals.live`'s per-turn Temporal probe makes `failed_loudly` unconditionally true**
      (`evals/live.py:317`), so the harness's headline "failed silently" signal can never fire.
- [ ] **[L] `run_turn` abandons the agent's `ResponseStream` on every non-exhausting exit**
      (`api/runner.py:318`); it has no `aclose()` and no GC finalizer, so its cleanup hooks never
      run at all.
- [ ] **[L] The mid-turn resume drops `user_input_requests`** (`api/runner.py:780`), so an approval
      prompt raised during a resume never reaches the stream.
- [ ] **[L] A failed durable job is dropped from the mid-turn resume**
      (`agent/job_results.py:83`), and the function's own docstring says it is not.

**Error handling and suppression**

- [ ] **[L] 22 of 56 `# noqa` directives suppress rules ruff never runs** (`pyproject.toml:8`) —
      including 15 `BLE001` markers that read as "this broad except was linted and accepted" when
      nothing linted it. Either enable the rules or delete the comments; today they are a claim no
      gate checks.
- [ ] **[L] `beating()` abandons the work it wraps when the activity is cancelled**
      (`durable/heartbeat.py:48`) — calc's CREST runs and bo's surrogate fits keep burning CPU
      after `cancel_job`.

**Tests that cannot fail** — this repository's own most-recorded defect family
(`tasks/lessons.md`), so each of these was proven by neutering the control and watching the test
still pass.

- [ ] **[M] `SnowflakeWarehouse._connect` classifies every client error as retryable
      `ConnectionError`** (`ingest/eln/warehouse/snowflake.py:162`), and no test executes any
      function body in the module.
- [ ] **[L] `tests/test_connector_isolation.py`'s first-party half is vacuous**
      (`tests/test_connector_isolation.py:85`): `name.split(".")[0] in ("calc",)` can never match a
      `chemclaw.science.calc.*` module, so the check has always passed on an empty set.
- [ ] **[L] `test_harness_agent_still_audits_every_tool_call` asserts only that the middleware list
      is non-empty** (`tests/test_agent.py:279`) — it passes with the audit middleware removed.
- [ ] **[L] `cli/verify_audit_chain.py` has 0% coverage**, and the "refuse to re-seal a broken
      chain" control lives nowhere else.
- [ ] **[L] Eight of the eleven binding transforms in `warehouse/expr.py` are never executed**
      by the suite, including the two the shipped `eln-snowflake` binding uses.

**Documentation that asserts what the code does not do**

- [x] **[L] `NoAuth`'s docstring asserts a manifest validator that does not exist** — closed by
      D-2026-08-06-a-connector-that-authenticates-nobody, and the docstring was wrong about more
      than the validator's existence: such a validator *cannot* work, because it sees the manifest's
      loopback dev default rather than the effective URL `connector_urls` moves it to. So it would
      pass on all seven shipped manifests and catch nothing in the cluster where the exposure is.
      The rule it described is now `require_secure_channel`, judged on the effective URL at the two
      points a connector is reached.
- [ ] **[L] `connectors/calc/activities.py`'s module docstring denies the registration mechanism
      the file uses two lines later** and cites a queue count D-118 removed
      (`connectors/calc/activities.py:23`).

### Refuted, recorded so they are not re-found

- Connector server pods lacking the front door's uvicorn transport bounds — the verifier could not
  reproduce a reachable consequence.
- A durable job's failure text reaching the model unsanitized on the job path — the sanitizer that
  `connector_app` installs does cover it.

## Open — Authorization gaps left by the whole-codebase security sweep (2026-08-06)

Record: `docs/decisions/D-2026-08-06-a-gate-that-names-nothing.md`, which closed the inert core
trigger gate and added the guard that would have caught it. These are what the same lane found and
did not fix.

- [x] **[M] The unauthenticated `X-Chemclaw-Actor` header becomes durable GxP attribution** —
      closed by D-2026-08-06-a-connector-that-authenticates-nobody, by taking the root fix this row
      names rather than the narrow one. The narrow one had already landed for the *durable* path
      (D-2026-08-06-the-memo-already-carried-the-actor) and could not land for the inline one:
      `suggest_next_experiment` has no workflow and so no memo to read `requested_by` from. The
      question was never where an inline tool finds a trustworthy actor — it was why a header on a
      call between our own pods was untrustworthy at all. It is not any more: a connector refuses a
      request without the fleet credential, and a deployment that would have no credential and no
      loopback boundary refuses to start.
- [x] **[L] The built-in write gate never consults the connector-declared `state_changing` set** —
      closed by D-2026-08-06-a-write-gate-that-reads-the-wrong-declaration, **and the fix this row
      proposed is refuted by the measurement it needed**. Deriving from `state_changing` would have
      newly required a privileged role for **18 tools** — `predict_pka`, `compute_xtb_energy` and
      `suggest_next_experiment` among them — because a bundle lists those as state-changing for the
      *plan gate's* question (they burn CPU and write a cache row), which is not the RBAC gate's
      question. Any deployment with `entra_required` and no `tool_role_gates` entry would have lost
      its chemistry. The axis is *whose* state a write touches: one chemist's `predict_pka` cannot
      change what another's returns, and `report_measurement` can. So the manifest gained
      `endpoint.privileged` — the subset of `state_changing` whose writes are shared — and the gate
      derives from that plus `expensive_actions()`, which also let core stop naming
      `compute_dft_energy`, a tool that was never core's.
- [x] **[L] `map_to_hpc_identity` has no caller** — closed by
      D-2026-08-06-a-connector-that-authenticates-nobody by wiring it, in `submit_to_hpc` on both
      the Nextflow and mock paths, before the launch rather than after (a submission that fails
      still consumed the intent). The mapping also lands on `HpcJobHandle.run_as`, so the audit link
      is in Temporal history beside the `requested_by` memo it maps from rather than only in a log
      line retention prunes. The existing unit test is why it stayed unwired: it called the function
      itself, so it passed for as long as nothing else did.

## Open — Left by the agentic-engine / harness / deep-research review (2026-08-05)

Record: `docs/decisions/D-2026-08-05-one-rule-in-three-places-is-three-rules.md`. Three findings
were fixed in that pass (the two-predicate `PlanEvent`, the harness dimensions resolved in three
places, the uncached conflict index). This is what it measured and did **not** fix, because the fix
is a decision about what a chemist is shown rather than a diff.

- [x] **`conflicts._suspected` is O(k²) in the notes sharing a `(type, compound_smiles)`, and its
      output stops being readable long before it stops being computable** — closed by taking each
      note's **widest disagreements** (`conflict_max_per_note`, 3) rather than every pair. Sorting a
      group by confidence puts the strongest partners at the two ends, so the walk takes the wider
      end first and stops as soon as it falls inside the threshold. Re-measured on the same corpus:
      141,156 → 5,937 pairs, 637 → 44 ms, 3 ids per chunk instead of ~141. Two things the narrowing
      does not do, both pinned: a *declared* conflict is never evicted by a heuristic's guess
      (`Conflict.severity` pins it), and the truncation is never silent — `NoteConflicts` carries
      the full total and the report renders "(the 3 strongest of 141)". The decision the row asked
      for turned out to be smaller than it looked: `Conflict.kind` already separated author-stated
      from heuristic, so the cap applies to `suspected` alone and KM-8's declared-conflict promise
      is byte-identical. The original measurement, for the record — over a
      synthetic 2,000-note corpus spread across 7 substrates — the shape a real programme has, since
      an optimization campaign is many runs on one substrate: **141,156** conflicts, 637 ms of pure
      pair enumeration, and a `conflicts_with` list of ~141 ids on every evidence chunk that reaches
      the model's context. At 200 substrates it is 4,891 conflicts and 27 ms; at 2,000 (one note per
      compound) it is 0 and 11 ms — so the cost and the noise are both entirely in the
      many-runs-per-substrate case, which is the case this system exists for.
      The caching added in that ADR bounds *how often* this is paid (once per corpus state, not once
      per retriever call) and nothing else. The open question is the heuristic's own productivity:
      a pairwise scan that flags a note against every other note on the same substrate is telling a
      reader nothing they can act on. Options are a per-note cap, a "widest gap only" rule, or
      pairing on something narrower than `(type, compound)` — each changes what KM-8 shows a
      chemist, which is why it is a decision and not a patch.

## Open — Left by the BO deep review (2026-08-05, D-2026-08-05-a-ceiling-that-does-not-hold)

- [x] **The durable campaign does not write the campaign store** — closed, and the recorded blocker
      was a false dilemma. The seam already carries the actor: core sets `requested_by` on the
      child's **memo** for exactly the shared-service-identity case, and `connectors/qm/workflows.py`
      has read the same memo in production since F5. `BoCampaignWorkflow` reads it and hands it to a
      bundle-owned activity that reuses `record_suggestion` unchanged. Verified live against the
      real broker and Postgres, not offline: resumed with `opened_by` off the memo and every
      observation present.
- [x] **`bo_suggestions` stores no snapshot of the problem it was proposed against** — closed by
      `infra/sql/037_bo_suggestion_provenance.sql`, alongside the row above as planned.
- [x] **No unique index makes a BO write idempotent** — closed by the same migration. Keyed on the
      run (`job_id`, the workflow id) and never on the content: two genuinely identical asks are two
      history entries. Partial on `job_id <> ''` so the inline path, which has no run to name, keeps
      appending. Both store backends implement the rule, and the Postgres half — a partial unique
      index plus an `ON CONFLICT ... WHERE` inference, exactly the kind of thing that is right in
      prose and wrong in SQL — is asserted against a real database.

## Open — Left by the CHECKMATE deep review of the live/durable spine (2026-08-05)

Record with every number and its reproduction: `docs/archive/review-2026-08-05.md`. Nine findings
were fixed in the same pass and are not listed here; these are the ones that need a *decision*
rather than a diff.

- [x] **api RSS grew without bound because telemetry "off" meant no meter provider at all** —
      closed. With none set, the OpenTelemetry API does not discard instrument calls: it *proxies*
      them and keeps every proxy forever, in module-level lists that only grow.
      `configure_telemetry()` returned early when `otel_enabled` was false — the default every
      deployment runs — and MAF creates a duration histogram per exposed MCP function while this
      system rebuilds its connector tool surface every turn, so each turn leaked 35 `_ProxyMeter`s,
      35 `_ProxyHistogram`s, 70 locks and 35 lists. Measured with `make leak-probe` against the real
      front door: **+20.7 KB and +178 live objects per turn before, +2.7 KB and +3.3 after**, with
      what remains being the session LRU filling toward its cap. Two methods agreed — a gc type
      histogram named the types, `tracemalloc` named the allocation lines — and the fix is one call.
      The plausible hypothesis going in (MAF re-entering unconnected tools into the agent's
      process-lifetime exit stack) was **refuted** by a one-line probe: flat at zero over 200 turns.

- [x] **`SessionTurnClaims.refresh` discards `cur.rowcount`** — closed. It now returns whether the
      lease was still ours, the heartbeat acts on it, and `chemclaw_turn_claims_lost_total` counts
      the takeover a deployment could not previously see.

- [x] **The proposal webhook cannot be wired to any named git host without a translator** — closed
      by saying so. The route and the runbook now state that the contract is *ours* and that a
      translator is required, naming what each host actually sends.

- [x] **`tests/test_layering.py`'s policy is package-granular, so `durable → connectors` is
      blanket-allowed** — closed without the granularity. A single-module AST assertion
      (`test_the_connector_job_wrapper_imports_no_connector`) pins the one claim that was
      unguarded, and the edge's declared reason now names the module the edge actually comes from.

## Open — Found by the deeper testing pass (2026-08-04)

- [ ] **A SIGKILLed connector worker costs 600 s before its job resumes, and one setting decides
      that for every calc job** — [M]. Measured by the storm's chaos family: the workflow is
      interrupted at `species 1/5`, the activity stays `Started` against a worker identity that no
      longer exists, and Temporal reschedules it only when `xtb_job_heartbeat_timeout_seconds`
      expires — exactly the configured 600 s, after which the job completes normally. Durability
      holds; the *latency* is the finding, and it had never been measured.
      The coupling is the real issue. That one setting has to be longer than the slowest legitimate
      gap between heartbeats — a CREST search, whose manifest says its cost "is not bounded by the
      input's size" — **and** it is the only signal that detects a dead worker. So every calc job
      pays the CREST-sized detection window, including the ones that heartbeat every few seconds.
      A per-job value (long for the two `expensive: true` searches, short for everything else)
      would decouple them; that is a config-surface decision, not a diff to slip in beside a test
      pass. On OpenShift, where pod eviction is routine rather than exotic, this is ten minutes of
      dead time per eviction.

- [ ] **The remaining mutant survivors are string mutations** — [S], and the row is kept only so
      the number is not re-derived from scratch. `make mutants`, 686 mutants: the two leading files
      have been walked and their eight *behavioural* survivors killed
      (`tests/test_review_2026_08_05.py`). What is left in `api/runner_trace.py` and `kg/pr_gate.py`
      is overwhelmingly string mutation, which a substring `pytest.raises(match=...)` cannot kill,
      plus genuinely equivalent cases like `chain_hash`'s `chars=None` (64 characters either way).
      One named non-equivalent survivor remains: `PostgresAuditSink.record` with
      `statement_timeout_seconds=None` — nothing asserts the audit insert carries a timeout.

Closed by this pass: the storm's two missing families (E and H are wired, and `FAMILIES` plus the
coverage table make an overstatement structurally impossible), and **SCALE-3** — see
`docs/archive/storm-2026-08-04.md` for the per-cap tables and the mutation results. **The
measurement is closed; the finest question it asks is not.** Raising the cap from 2 to 8 buys
29–38 % goodput per step, far outside any measured noise — settled. Above 8 the steps buy 6–15 %
against floors of 9 % and 15 %, so two back-to-back sweeps disagreed and the second correctly
refused to name a knee. Whether 16 beats 8 needs more samples per cap (`--sweep-repeats`) or a
quieter machine; the shipped default of 8 sits at the top of the resolved range.

## Open — Left by the live full-stack pass (2026-08-04)

Full record with the measurements: `docs/archive/live-full-stack-2026-08-04.md`. Every layer live
at once for the first time — real broker, workers, Postgres, front door and model. Four defects
found and fixed (D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed). What it left open:

- [x] **`compare_solvents` accepts a solvent name that only fails deep inside the durable job** —
      closed. All five solvent-taking calc jobs declare
      `science.calc.solvents:require_supported_solvents`, so an unparameterized name is refused at
      launch with the closest supported spellings (2-MeTHF → thf/tetrahydrofuran) instead of ~30 s
      into an activity. The name set was *measured* against tblite rather than recalled — the two
      rejection messages are different failures ("epsilon was not found" vs "No ALPB/GBSA
      parameters"), only the intersection runs, and a test re-derives it in both directions. It
      also retired `xtb_engine.COMMON_SOLVENTS`, which had drifted to omit dmf, dioxane, benzene
      and nitromethane while claiming to name what process chemistry asks about.

- [ ] **du-03: 29 tool calls, no answer, and the capability never reached** — [M]. The turn now
      says so (`empty_answer`), which is the reporting half. The behavioural half is untouched: it
      looped `find_past_jobs` ×8, `load_skill` ×6, `find_notes` ×5 and never called
      `start_optimization_campaign`, which is what the question needed. Whether that is a
      retrieval-loop problem, a prose problem, or a 38-note corpus giving it nothing to stop on is
      not yet measured — and the corpus caveat means it cannot be settled on this data alone.

- [x] **A repeated tool call returning nothing is retried unchanged** — closed by
      `agent/repeat_guard.py`: a turn may make the identical call `max_identical_tool_calls` times
      (2) and is then refused with a message naming the tool and what to do instead, counted by
      `chemclaw_repeated_tool_calls_total`. It refuses rather than replaying the first result on
      purpose — `get_durable_job_status` legitimately changes within a turn, so a cached answer
      would pin a job at "running" for a model that was correctly re-checking.

- [ ] **The full 230-probe corpus has still not been run against a live model** — [M]. This pass
      ran the four `du-*` probes and a two-probe harness slice. The wide sweep needs a corpus worth
      sweeping: against the 38-note seed graph it would measure the corpus, not the system, and
      produce numbers that read as comparable to `live-grounded-2026-08-03.md` and are not.

- [ ] **Entra-enforced pass** — [M]. Everything ran `entra_required=false`. The documented approach
      is a local RSA keypair + self-served JWKS + minted tokens (`docs/archive/live-gates-2026-07.md`).

## Open — Left by the live lane (2026-08-04, D-2026-08-04-a-lane-that-only-runs-where-docker-runs)

The lane itself is done: `make live-infra` / `live-up` / `live-jobs` / `live-probes`, six mechanical
checks green against a real Temporal + Postgres in a container with no Docker daemon. What it did
not close:

- [ ] **The Temporal-backed tests still skip wherever `temporal.download` is blocked** — [M]. All
      13 modules fetch the *time-skipping* test server, so a live broker on 7233 cannot substitute:
      a workflow that sleeps would really sleep. But not every one of them skips time — the ones
      that only need a real server (`test_connector_job_workflow`, `test_workers`) could take
      `WorkflowEnvironment.from_client()` against `settings.temporal_address` when it answers, which
      would turn a silent skip into a real run in exactly the environments that currently prove
      least. Needs a per-module judgement about which tests depend on time skipping, which is why
      it was not guessed at inside the lane's own change.

- [x] **Stage B has never been run** — **closed 2026-08-04.** Run with a real key against the full
      live stack. It found four defects, two of them in the signal itself (a vacuous audit check and
      a false positive that flagged a working durable path), which is the argument for having run it
      rather than reasoned about it. Record: `docs/archive/live-full-stack-2026-08-04.md`.

- [ ] **`make live-jobs` exercises one connector** — [S]. `compute_reaction_energy` on `connector-calc`
      is deliberate (in-process `tblite`, no HPC, writes to the cache so D-011 is observable), but
      `connector-bo`'s campaign and `connector-qm`'s Nextflow job take different shapes — a campaign
      outlives its turn, and QM needs a cluster. The BO one is reachable now and is the obvious
      second case; the QM one belongs with the deferred cluster work.

## Open — Found while fixing the grounded live run (2026-08-03)

- [ ] **There is no documented way to populate the fingerprint index** — [S]. Chasing F5 turned up
      that the "separate documented backfill" the operator was assumed to have skipped does not
      exist. `make reindex` is note-index-only; the fingerprint tables are filled as a side effect
      of the ELN sync (`ElnSyncWorkflow`) or by `index_molecule`/`index_reaction` one record at a
      time, and `docs/guides/runbook.md` covers only re-indexing after a *definition* change (§vi).
      An operator standing up a corpus has no procedure to follow, which is how a live run reached
      1,025 indexed notes and 0 fingerprints. The connectors now say so loudly at startup, so this
      is a documentation gap rather than a silent one — but a runbook section (or a `make` target)
      is what actually closes it.

- [ ] **A BO observation naming an undeclared parameter is silently dropped** — [S], and it is a
      *fabrication* vector rather than an error-handling one, so it is worth a second look beyond
      the input validation that now rejects it. Measured while fixing F3: BoFire ignores the stray
      column and returns candidates, so `ligand: PPh3` against a problem that never declared
      `ligand` yielded a confident `predicted_value=66.5` from a decision space that had discarded
      it. The boundary check closes the path from the tool; what is not checked is whether any
      other caller (the durable campaign path builds its own observations) can reach the same
      silent drop.

## Open — Found by the grounded live run (2026-08-03)

Full write-up with the measurements: `docs/archive/live-grounded-2026-08-03.md`. 36 corpus-grounded
probes, real model, real tool calls, real front door.

- [x] **The fabrication metric measures the grader's blindfold** — [M], **P0**. `ToolResultEvent.preview`
      is capped at 200 characters (`api/runner_trace.py:23`), `gather_evidence` returns up to 40
      chunks, and `evals/live._score_citations` derives `uncited_note_ids` from those previews and
      hands the list to the judge as *"NOTE IDS CITED THAT NO TOOL RETURNED"*. The run graded 19 of
      36 answers as fabrication; **nine verdicts were checked against the tools' real return values
      and all nine were false** — `ich_impurity_limit` does return Pd 100/10/1 and Cu 3000/300/30
      µg/day, `compute_electronic_properties` does return LUMO/dipole/charges/bond orders,
      `stoichiometry_table` does return solvent volumes, the hazard rule's `explanation` does carry
      the copper/lead/silver plumbing language, and 12 of 17 flagged note ids come back from a
      single `gather_evidence` call. Fix in two parts: (1) an untruncated `note_ids: list[str]` on
      `ToolResultEvent` beside the human preview, scored against instead of the prose scan;
      (2) the judge prompt must state what the signal is — one verdict escalated "not in the
      preview" to "**mechanically verified as absent from the corpus**", which the harness never
      checked. Until both land, no fabrication number from this harness is quotable.
      **Closed. `ToolResultEvent` now carries an untruncated `note_ids`, filled from the full tool
      output by `runner_trace`; `_score_citations` is a set difference against it, which also closed
      the hyphen-suffix hole the substring scan had. The judge prompt states what the signal does
      and does not claim, since telling a grader to trust a number obliges us to say what it sees.**

- [x] **Ask-before-search: 10 of 36 turns called no tool at all** — [M], **P1**. Every one answered
      with a clarifying question in prose. Six times the answer was in the corpus and one search
      would have found it (the BTMG plate, `failure-dcm-amide-coupling`, `opt-suzuki-conditions`,
      both amination plates). The sharpest case: an answer that says *"I'll call `calculator_trust`
      … and then `calculator_outliers`"* and ends the turn having called neither — a promise, not a
      question. Two sub-items: the prose/skills need *search first, then ask about what you could
      not find*; and there are **two clarification paths with only one instrumented** —
      `ask_clarifying_question` fired on 3 turns while 10 asked in plain prose, so `asked_clarifying`
      undercounts threefold and any metric on it is wrong in that direction.
      **Closed, with the diagnosis corrected: "Look before you ask" was already in the instructions
      and was ignored on all ten turns, so restating it would have changed nothing. What is missing
      is that `ask_clarifying_question` is never *named* there — which is why 10 of 13
      clarifications took the uninstrumented prose path — and that nothing forbade naming a tool you
      do not call. Both added, and `evals/live` counts the prose path separately so the metric stops
      being wrong in a known direction.**

- [x] **A caller-fixable BO fault reaches the model as "an internal error occurred"** — [S], **P1**.
      `connectors/server.py:137` passes `ValueError` through and generalizes everything else. Right
      posture, wrong consequence: `suggest_next_experiment` died in BoFire's `_optimize_acqf_discrete`
      with `KeyError: 'base'` — meaning "the frame has no column for declared parameter `base`" —
      and the model got a string it could not repair from, then answered anyway. The fix is *not*
      to leak the exception: validate the observations and the candidate grid against the declared
      parameters at the tool boundary and raise a `ValueError` naming the parameter. **The exact
      trigger is unreproduced** — four hand-built calls in that shape (± descriptors, two and three
      factors, complete and incomplete observations) all succeeded or raised the *good*
      `ValueError: no col for input feature 'base'`, and a re-run took a different route entirely.
      Finding it needs the real arguments, which the audit log truncates at 200 chars — the same
      defect as the row above.
      **Closed by validating at the tool boundary, naming the parameter and the observation index.
      The exact live trigger is still unreproduced and the code says so rather than claiming
      otherwise. Measuring the two directions separately changed what the finding is — see the
      silent-drop row above, which is the worse half.**

- [x] **`request_development_report` leaked a raw Temporal transport error, and the model papered
      over it** — closed in this commit. `connect()` raised `RuntimeError('Failed client connect: …
      tonic::transport::Error …')`, which reached the model as `Error: Function failed.`; the model
      then **wrote the whole development report itself** — tables, summary, numbers, citations —
      and presented it as entering the PR-gate. `connect()` now raises `SubsystemUnavailableError`
      (`core/errors.py`) naming Temporal and saying nothing was queued, with the transport error as
      `__cause__`, and `surface_domain_errors` hands it to the model verbatim. Deliberately **not**
      a `ChemclawError`: that hierarchy is the non-retryable bad-data contract, and an unreachable
      broker is retryable — `tests/test_publish.py` now asserts its *absence* from `_BAD_DATA_TYPES`
      with the reason, so a completeness sweep cannot quietly add it. `connectors/jobs.py`'s own
      copy of the message is gone with it: one client, one message, and that copy had been
      mislabelling the outage as non-retryable bad data on the template activity path.

- [x] **`available_tool_names()` omitted the skill name space** — closed in this commit. `load_skill`
      was called on four live turns and `run_skill_script` on a fifth while the function reported all
      three skill tools as absent — and it is the authority for `prose-validate`, `skill-validate`
      and `tests/test_live_probes.py`, so each would have rejected a correct reference to a tool the
      agent had just called. Now unions a fourth name space read off MAF's own class constants.

- [x] **An empty fingerprint index is indistinguishable from "nothing similar"** — [S], **P2**.
      `similar_reactions` returned `{"result": []}` with 10,000 reaction notes present, because
      `make reindex` fills `note_index` and not the fingerprint tables (a documented separate
      backfill, skipped by the operator). Nothing said so: a chemist asking "have we made anything
      like this" gets "no" from a system that has simply not been indexed — on the one tool whose
      whole job is that question. `similar_molecules`/`similar_reactions` should distinguish an
      empty index from an empty result, and the health surface should report the row counts.

## Open — BO capability roadmap (2026-08-04, D-2026-08-04-what-bofire-does-when-you-actually-run-it)

Five waves out of `docs/reference/bo-capability-map.md`. **Every BoFire behaviour each row depends
on was measured before the row was written** — the previous BO roadmap said "just thread
`n_generators` through" about a parameter that turned out to be inert
(D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate). The measured numbers are in the
ADR; three of the seven measurements changed a row below, and one reversed a refusal.

- [x] **W1 — nothing computes the campaign health the answers already assert** — [S]. No BoFire
      change and no compatibility risk, which is why it is first: you cannot judge whether a later
      wave helped without a convergence read. (i) A `campaign_progress` tool — best-so-far,
      improvement over the last *k* rounds, evaluations since a real improvement, a plateau verdict,
      and `design_space` (reuse `discrete_candidate_count`) beside the distinct-candidate count,
      which makes "best point in 11 proposals against a 96-cell grid" a computed claim rather than
      the refusal `op-28` had to give. **`assay_noise` is a required argument with no default** —
      `op-13` was graded *fabricated* for calling 1–2% gains real against a ±2% reproducibility the
      user had stated in the question, and a default noise would reproduce that error with a tool's
      authority behind it. (ii) An observed-spread scale on the suggestion return, so `predicted_sd`
      can be read against what the objective's numbers actually span, and a `None` sd is stated as
      "a space-filling seed point, no surrogate had an opinion" instead of reading as endorsement.
      (iii) The explore/exploit section the `experiment-design` skill's front matter advertises and
      its body does not contain. Closes 3.5 and 3.4; gives 3.6 its defensible half.
      **Closed by D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with, with one correction
      the data forced.** The planned `op-13` replay asserted `plateaued=True` and that is wrong:
      ordered by equivalents, the 83→88% jump at 2.2 eq is a real five-point gain four runs from the
      end, so `evaluations_since_improvement` is **3** and a five-evaluation window is not satisfied.
      What is true is the grader's own narrower sentence — the last four runs span 2.0 against a
      stated ±2.0, so they are not distinguishable from each other — and both facts are now separate
      fields rather than one verdict. Rounding the second up to the first would have been `op-13`'s
      error with the sign flipped.
      **The plateau arithmetic was later superseded by
      D-2026-08-05-a-gain-is-measured-from-the-last-gain**, after a review found it reporting a
      campaign that climbed +20.9 against a ±2 assay as plateaued: the counter measured each run
      against a continuously updated running best, so a climb in sub-noise steps never reset it.
      `op-13`'s numbers above are unchanged by that fix.

- [x] **W2 — a screen cannot hold a continuous factor, and that is what makes three knobs inert** —
      [S]. Measured (M-5): on the all-categorical domain `factorial_design` accepts today,
      `n_generators`, `n_repetitions` **and** `n_center` are all no-ops — 8 runs at every value —
      and only `randomize_runorder` bites. On a mixed domain all four work: `n_generators=1` halves
      32 runs to 16, `n_center=0` returns exactly the corners at the two bounds, `n_repetitions=2`
      replicates the factorial part. So admitting continuous factors is the **precondition** for the
      other knobs, not a companion to them. The refusal being removed (`engine.py`) was right when a
      fractional design did not exist; `_fractional_design` now performs that re-encoding
      deliberately and `ScreeningDesign.summary` names what was given up. **Two measured traps for
      the diff:** `n_center` defaults to **1**, so a naive change starts silently returning midpoint
      rows; and it adds `n_center` rows *per categorical combination* (4·2^k + n_center·2^k), so the
      run count is not `corners + n_center`. Closes the rest of 2.3 and 4.4.
      **Closed by D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds.** M-8 settled
      the reduced half before it was built: two real continuous factors beside three re-encoded
      categoricals give 32/16/8 runs at `n_generators` 0/1/2, so the union fractionates as one
      factor set and the stated resolution describes the whole design. `randomize` is shuffled at
      our own boundary rather than through `randomize_runorder`, so both design paths randomize
      identically under one `bo_seed`. `block_feature_key` stays unbuilt — it needs a block factor
      (a day, a plate, an operator) and none exists in `src/`.

- [x] **W3 — multi-objective is unrepresentable, on a corpus that records a trade-off** — [M].
      Every ELN run carries `yield_percent`, `purity_percent` *and* `impurities[].area_percent`;
      `OptimizationProblem` has one `objective` field, and `op-16` was graded *fabricated* for
      promising "both objectives" anyway. Measured (M-1): `MoboStrategy` validates with no reference
      point, fits at **n=2** (so `MIN_SEED_OBSERVATIONS` is unchanged), and returns
      `<objective>_pred`/`_sd` per objective in the naming `_frame_to_candidates` already reads.
      `objectives: list[Objective]` with a `mode="before"` validator that accepts the singular
      spelling **permanently** — it is on disk in every `bo_campaigns.problem` row and in every
      in-flight `CampaignSpec` in Temporal history. `best_of` stays scalar and raises; a separate
      `pareto_front` in pure Python (not `compute_hypervolume` — `problem.py` is imported into the
      agent process as the campaign job's `params_model`, and a test exists to keep `torch` out).
      **Two things not to get wrong.** `campaign_id_for` dumps parameters with a *denylist*, so any
      new field forks every id in the database invisibly; M-2 captured the baseline ids
      (`campaign-6958b7edaa261c83`, `campaign-55e5f929fe83a9a5`, `campaign-109f34eac28892ab`) and
      confirmed an allowlist reproduces them byte-identically. And the stale refusals — the tool's
      "they are unrepresentable" and the skill's "pick the one they lead with" — must die in the
      same commit, or the model is taught to refuse a capability that exists. Inline only: the
      durable registry is `Callable[..., Awaitable[float]]` with two demo entries. Closes 3.3's
      objective half; unblocks 4.5's "objectives" plural.
      **Closed by D-2026-08-04-a-trade-off-has-no-single-best-point.** Two artefacts had to change
      with the code rather than after it: the tool description, whose "One objective, no constraints"
      sentence covered two halves that now differ (the objective half is replaced, the constraint
      half survives verbatim until W4), and `data/evals/probes/optimization.yaml`'s `op-16`, which
      graded the model on *refusing* multi-objective and would have marked the correct new behaviour
      as a failure. Its `forbids_claims` now name the overclaims actually available: a single best
      point, a front presented as a prediction, and a proof that no better trade-off exists.
      `campaign_progress` gained the same refusal `best_of` has — a plateau is per axis, so a
      trade-off must name which objective to read.

- [x] **W4 — a limit the chemist states cannot be expressed** — [M]. `Domain(constraints=…)` is
      never passed, so "keep base plus acid under 3 equivalents" has to be smuggled into a bound or
      silently ignored. Measured (M-3), and the question that mattered was not SOBO but
      `RandomStrategy`, which seeds every cold start: **0 violations of 20** random points, **0 of
      5** SOBO proposals, and an equality constraint puts **10 of 10** random points exactly on the
      simplex — so no rejection-sampling path is needed and the wave stays [M]. One neutral
      `LinearConstraint{parameters, coefficients, relation, rhs}` over `<=`/`>=`/`==`; a
      five-member discriminated union would be the single biggest comprehensibility regression
      available to an LLM-facing schema. BoFire itself refuses a constraint naming a categorical, so
      our validator is for the message, not the safety. The mixture/formulation case *is*
      `relation: "=="` and comes free — ship the mechanism, say nothing about formulations in the
      skill until a dataset can validate it. `CategoricalExcludeConstraint` joins in scoped form
      (M-4: refused on a mixed domain, **0 violations** on a pure categorical one), so "never
      combine Pd(OAc)₂ with DMSO" is expressible for a screen. `note_from_campaign_result`'s
      "Searched over:" block becomes untrue the moment constraints reach the durable path — it would
      describe a box when the campaign searched a polytope — so it gains a "Subject to:" block in
      the same diff. Closes 3.3's constraint half.
      **Closed by D-2026-08-04-a-limit-across-parameters-is-not-a-bound.** Two claims above did not
      survive the build. The exclusion is **not** expressible for a screen: M-4 had measured it
      against `SoboStrategy` and `RandomStrategy` only, and measured against
      `FractionalFactorialStrategy` (M-4c/M-4d) that strategy rejects *every* constraint class at
      construction — linear included. So `factorial_design`'s refusal is the message, not the
      safety: it raises where the caller can act instead of surfacing a pydantic error naming a
      BoFire class. The two constraint shapes are a two-member discriminated union on `kind`, which
      is what `kind` was put there for; an exclusion additionally needs an all-categorical problem,
      and the validator names the caller's own continuous parameters rather than repeating BoFire's
      "pure categorical/discrete search spaces".

- [x] **W5 — nothing reads the surrogate back** — [S]. (i) `predict_outcome`: "what would the model
      predict for 90 °C in toluene with L3", the question a chemist asks *instead of* trusting a
      recommendation. Measured (M-6): `predict()` accepts a params-only frame, works on a featurized
      domain, and does **not** clamp an out-of-bounds point — it extrapolates with the sd rising
      about sixfold, which is an honest signal to surface rather than a reason to refuse. (ii)
      Cross-validated fit quality for the model behind the current recommendation. **This one was
      refused and the measurement reversed it** (M-7): the objection was that reaching
      `cross_validate` means naming a surrogate class and risking a number describing a different
      model, and in fact `strategy.surrogate_specs.surrogates[0]` exposes the surrogate BoFire
      itself chose — 10 rows, 5 folds → R² 0.948, MAE 1.47, with no class named in our code. `shap`
      is already installed via `bofire[optimization]`, so nothing here costs a dependency. Needs the
      "a CV score over ten observations will be over-read" caveat as a `computed_field`, not a
      docstring.
      **Closed by D-2026-08-04-the-model-can-be-asked-not-only-obeyed.** Both halves ship behind one
      `predict_outcome` tool over **one** fit, which is what makes the score mean anything: a
      quality measured off a separately configured strategy would describe a model nobody's
      recommendation came from. The register's exact pair does **not** reproduce and is retracted — its
      script passes `get_metric` a string where an enum is required and raises — but the finding
      does, at R² 0.935 / MAE 1.695 corrected, 0.950 / 1.36 through the shipped code and 0.813 /
      3.45 on `op-13`'s twelve real runs. The extrapolation signal is starker than measured: sd 0.97
      in range against 18.6 at T=400. One correction to the plan: `get_metric`
      returns a `pd.Series`, and its `combine_folds=True` default pools the held-out predictions,
      which is the number to report — a mean of per-fold R² weights a two-point fold like a
      ten-point one. `op-13`'s posterior half closes with it.

- [ ] **The `method` note type is what analytical method development is actually waiting on** —
      [M], and it is a schema row rather than a BO one. 24 stories sit in §7/§8 and a
      method-development BO campaign today has neither factors nor responses to sit on: nothing can
      record "we ran this gradient on this column and it resolved these peaks", and the story
      audit's grep counts `mobile phase` 0, `C18` 0, `system suitability` 0. Same shape as the
      `reaction` note that already exists; `user-story-capability-map.md` scores it as unblocking
      eight stories alone. Only once it lands are `TargetObjective`/`CloseToTargetObjective` (a
      stated spec: "resolution ≥ 2.0, run time ≤ 12 min") and W2's centre points and replication
      worth building — which is why they are named in the map and not scheduled.

## Open — Found while closing the refactor (R5.3, 2026-08-03)
      **Closed. A `FingerprintSearch` envelope carries the distinction as a `computed_field` (a bare
      property would not survive `model_dump()`), the emptiness probe runs only when a search found
      nothing, and each bundle logs its index size at startup — the connector owns the table, so
      core never reaches into it. Extended to `substructure_matches`, which fails the same way.**

- [ ] **The two slowest pKa tests fail when the suite runs on a loaded box** — [S]. On a quiet
      machine the suite is green in ~312 s (2852 passed, 127 skipped, measured twice on
      2026-08-03). With concurrent heavy processes on the same box the same suite took 1330 s
      (4.25×) and failed exactly two tests:
      `test_pka.py::test_predicted_pkah_ranks_aromatic_bases_correctly` and
      `::test_in_sample_pkah_errors_are_far_below_the_acid_calibrations` — the file's two slowest
      (11.8 s / 11.2 s alone, 3× the next), both green 27/27 in isolation. The failure text was
      not captured (the observing run piped through `tail`), so the cause is *unconfirmed*: the
      180 s signal-based per-test timeout in `pyproject.toml` is the obvious suspect at 4× slowdown,
      but stating that as the mechanism would be exactly the prose-over-measurement claim this repo
      keeps catching. To close: reproduce under load with output captured, read the actual failure,
      then either raise/architect around the timeout or fix whatever it actually is. Until then the
      operational rule stands (`tasks/lessons.md` 2026-08-03): never run the gate while other
      processes load the box.

## Done — Found while implementing R2 of the refactor plan (2026-08-02; closed 2026-08-03)

- [x] **`chemclaw.core -> chemclaw.connectors` is the last lazy kernel edge** — closed by taking
      the row's first option: accepted permanently, and said so where the row asked.
      `core/README.md` states the kernel rule with its "exactly one declared lazy exception", and
      `tests/test_layering.py::_ALLOWED_LAZY_EDGES` declares the edge with the reason (the
      connector registry is a real capability layer, not a misfiled primitive, so no move retires
      it — the same argument against the inversion, which would trade one declared edge for a
      startup-ordering contract between two layers). The R5.3 import-graph diff against `39f9135`
      confirmed it is the *only* core→sibling edge at any scope
      (D-2026-08-03-the-refactor-closes-what-it-measured).

## Done — Found while implementing R0 of the refactor plan (2026-08-02; closed by R1.6, `ca562d7`)

Each was found by an implementation agent working a *different* task, verified, and deliberately
left unfixed rather than scope-crept into an unrelated commit. See
`docs/planning/refactor-hardening-plan.md`. Both rows were closed by R1.6 and re-verified against
the shipped tree in R5.3.

- [x] **Two more error classes bypass the non-retryable registry** — closed. `ProfileError` and
      `AuthorizationError` are registered in `durable/publish.py::_BAD_DATA_TYPES` (lines 54 and
      71 as of R5.3). `AuthorizationError` is deliberately *not* reparented to `ChemclawError` —
      that would have made `surface_domain_errors` swallow authorization refusals ahead of
      `surface_authorization_denials` — so it is listed by exact name, and
      `tests/test_publish.py` walks its hierarchy the same way it walks `ChemclawError`'s so a
      future subclass cannot go unregistered unnoticed.
- [x] **A second false retry claim in the same docstring block** — closed with the row above, the
      way the row demanded: the false "a `ValueError` which `BAD_DATA_RETRY` lists non-retryable"
      claim is deleted, and `durable/template_activities.py`'s docstring now states the true
      mechanism (Temporal matches `non_retryable_error_types` by the exact string
      `"AuthorizationError"`, which `BAD_DATA_RETRY` lists by name).

## Open — Confirmed by the 190-probe live run (2026-08-02)

Each reproduced against a running deployment; evidence in `docs/archive/live-user-stories-2026-08.md`
and `tasks/live-test/`. The fixed ones are not listed — see the ADR and the commits on that run.

- [ ] **`ask_clarifying_question` does not end the turn** — [M]. `agent/dialogue_tools.py` used to
      promise it did; `core/turn_signals.py:129-133` records the signal and returns, and the agent
      loop continues. The docstring now states what is true, but the guarantee is still unenforced.
      Enforcing it fights the deliberate "Partial data is still an answer" instruction, so the two
      rules need reconciling before either is mechanised.
- [ ] **The fingerprint index and the citable note set are disjoint, and nothing says which was
      read** — [M]. `ingest/eln/ingest.py:44-50` indexes every reaction unconditionally and PR-gates
      the note, by design. In the run that was 4,251 indexed against 987 notes, and there is no
      fingerprint *data source*, so `gather_evidence` structurally cannot see the larger set while
      `similar_reactions` sees only it. `Match` carries no yield, so a facet or aggregate question
      ("rank the three base plates") is unanswerable — and an honest "I could not find it" is
      indistinguishable from "it is not there". Wants a coverage statement on the search result.
- [x] **The *eval's* citation check is still bounded by a UI budget** — closed by
      D-2026-08-03-a-metric-must-declare-what-it-can-see, and the cost of leaving it open is worth
      recording: this row correctly predicted the mechanism *and* the fix ("wants an untruncated
      `note_ids` field on `ToolResultEvent`", "do **not** raise `_ARG_PREVIEW_CHARS`"), and while it
      sat open a live run graded 19 of 36 answers as fabrication with nine of nine checked verdicts
      false — one of them escalating "not in the preview" into "mechanically verified as absent from
      the corpus". The one thing the row got wrong is the direction: it says the harness
      *understates* citation coverage, which is true of the coverage number and backwards for the
      one anybody reads, because an id scored uncited is reported as a fabricated citation.
- [ ] **The ICH Q3C revision label is unverified** — [XS, but it is on every Q3C citation].
      `science/safety/ich_q3c.yaml` cites "ICH Q3C(R9) … ICH Step 4 (2024)". An adversarial review
      verified all 62 transcribed values and the Q3D(R2)/2022 label, and could **not** verify this
      one offline. If it is wrong, every Q3C answer carries a correct number under the wrong
      document — the one failure shape the table exists to end. Check it against the ICH site and
      correct the single `guideline:` line; no figure changes.
- [ ] **The answer shape gate has not been measured live** — [S]. The deterministic scan
      (`ungrounded_parameter_shapes`, `answer_shape_gate_enabled`, off by default) is argued from
      the run that motivated it, not from a run that includes it. Re-run the analytical and
      bucket-C probes with the gate on — roughly six to thirty probes on Haiku, small credit — and
      publish the before/after on the 46% fabrication rate. Until then the claim to make is "the
      mechanism exists and its default is off".
- [ ] **No document-level provenance share** — [S, recommend refusing rather than building].
      `kg/note.py` `created_by` is whole-note and binary, there is no document entity, and §12's
      "how much of this was AI-drafted" is unanswerable. The honest refusal is one paragraph; the
      capability is a subsystem.

## Open — Left open by the full-codebase review (2026-08-01)

An adversarially-verified review across every layer and phase; 22 distinct defects were fixed (see
the `D-2026-08-01-*` ADRs). These are what the fixes uncovered and deliberately did not close.

- [ ] **REV-2 [Medium] — a solvate collapses onto whichever fragment is larger.**
  `standard_smiles("CCN.C1CCOC1")` returns THF: `FragmentParent` keeps the largest fragment and
  both are organic, so the ethylamine is discarded. Different mechanism from the counterion rule
  and not addressed by it. Needs a rule for what a solvate's identity is — probably the solute,
  which is the opposite of "largest".
- [ ] **REV-3 [Low] — connector *server* pods receive `CHEMCLAW_TEMPORAL_TLS_*` but mount no TLS
  volume.** `chemclaw.env` is shared and `chemclaw.tlsMount` is not included there. Harmless only
  because the sole `connect()` caller on that path runs in the front door, not the MCP server — a
  trap for the first connector server that needs Temporal. Not fixed because no `helm` binary and
  no cluster exist here to render the change against.
- [ ] **REV-4 [Low] — four hazard rules are narrow rather than wrong.** From the rule-by-rule audit
  (~90 molecules): `peroxide` and `n-halamine` miss the sanitised ionic spellings (Na2O2 parses to
  `[O-][O-]`, both X1; chloramine-T's `[N-]Cl` is X2); `hydrazine`'s `H2,H1` excludes fully
  substituted free hydrazines such as UDMH while its prose says "free hydrazine motif";
  `complex-hydride-with-chlorinated-solvent` matches *gem*-dihalides only, so 1,2-dichloroethane
  does not fire. Recorded rather than widened — widening a cited hazard rule on taste is how a
  table stops being citable. The azide table's own precedent for the ionic gap was a *separate*
  rule (`non-carbon-azide`), which is the shape a fix should take.
- [ ] **REV-5 [Low] — local development needs pgvector >= 0.7.** The migrations use
  `bit_jaccard_ops`; the common distribution package is 0.6.0, so a database stood up from `apt`
  fails to migrate. Pre-existing. CI is unaffected (it provides a pgvector-enabled Postgres), so
  this is a `deploy/README.md` note, not a code change.

## Done — Reviewing the experiment-progression change (2026-07-31, D-164)

Re-reading D-162 with fresh eyes. One real defect, found because the new `experiment-proposal`
type sat next to two that were never registered, plus three cleanups in the new code itself:

- [x] **PROSE-1** `make prose-validate` gains rule 4: a note type named in agent prose must be in
      `KNOWN_NOTE_TYPES`. It immediately failed on `protocol` and `experiment-batch`, both of
      which the agent was being told to write — a real tool producing an artifact `kg-validate`
      rejects on the agent's own PR. Both fold into `experiment-proposal`; `bo-candidate` is now
      explicitly the durable campaign's to mint, not the agent's.
- [x] **PROSE-2** The campaign table is driven off `Progression.steps` with the run looked up by
      id, instead of zipping two independently-sorted lists — equal lengths meant a sort
      disagreement would have mispaired rows silently.
- [x] **PROSE-3** `gather_evidence`'s docstring states that a date window scopes the note sources
      only: fingerprint hits from a `reaction_smiles` anchor carry no date and come back
      unwindowed.
- [ ] **PROSE-4** `propose_knowledge_note`'s docstring lists the note types with an ellipsis — a
      third copy of `KNOWN_NOTE_TYPES` kept in sync by nothing. The model-facing description
      should be derived from the frozenset rather than restated. Left open because it means
      building the tool description at registration time, which is a change to how every tool's
      docstring reaches the model, not a one-line edit.

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

## Open — v1.0 readiness analysis (2026-07-31, D-2026-07-31-*)

A whole-repo sweep for what is missing before v1.0, prompted by two observations: an inline BO
suggestion is never persisted, and nothing anywhere records *why* a tool was called. Both turned
out to be instances of wider patterns. Closed in this pass: a lost knowledge note that could not be counted, the
sidecar that emptied the tree it published and three assertions the chart never made
(D-2026-07-31-the-deployment-envelope), the audit trail that could not be joined to the
conversation that caused it (D-2026-07-31-the-audit-chain-is-versioned), and the ADR numbering that
kept colliding (D-2026-07-31-adr-ids-that-cannot-collide).

**Two halves were dropped because other sessions built them first and built them better.** `main`'s
D-164 found the note-type defect independently and resolved it the other way — deleting
`protocol`/`experiment-batch` rather than adding them — which with D-162's `experiment-proposal`
already covering the proposal case is the better call. `main`'s D-167 fixed the plan-approval
escalation by demoting against the **durable** approval store and by excluding system-authored
`awaiting-job:` todos from the plan's identity; this branch's version compared in-process state and
would have let a launched job revoke its own approval. Both defer to main.

**Re-checked against `main` after D-156/D-157/D-158 landed from other branches.** Those closed
four rows this analysis had opened — the durable job record now carries a run's reason, session and
correlation id; the BO note carries its decision space; and `calc_refs` is finally written on the
QM path. The rows below are what survives that merge, narrowed to say so.

**The record says what happened, never why.** This is the largest theme and none of it is closed.

- [x] **An audit row cannot be traced back to the conversation that caused it** — closed by D-2026-07-31-the-audit-chain-is-versioned.
      `audit_events.session_id` and `session_messages.correlation_id` give the words and the tool
      calls a shared key. The chain is versioned in the same commit, because `chain_hash` covers the
      whole `AuditEvent` and widening it would otherwise have reported every historical row as
      tampered with — indistinguishable from the tampering the chain exists to detect.
- [ ] **The reasoning a `correlation_id` now reaches is still erodible** — [M], and it is what makes the
      audit-chain join necessary-but-not-sufficient. The join lands on `session_messages`, whose rows
      `session_store._compact` rewrites, `durable/retention.py` prunes by age, and `rollback_to`
      deletes on client disconnect. So a trail can point at a conversation that has since been
      compacted out of recognisability. Wants a decision about what a GxP deployment must retain,
      not more plumbing.
- [ ] **No field holds an intent for a *non-job* tool call** — [M]. D-157 gave
      `ConnectorJobInput` a required `rationale`; D-2026-07-31-the-audit-chain-is-versioned added an `AuditEvent.purpose` column and
      deliberately left it empty, because the honest way to fill it is undecided. Authoring a reason
      per call means changing every tool signature; deriving one from the harness's active todo step
      is a heuristic, and a provenance field that is sometimes an inference is worse than an empty
      one — a reader cannot tell which rows are which. D-157's `rationale` works because a job launch
      is a discrete, deliberate act with an obvious author; an inline tool call is not. Needs a
      decision, not code.
- [ ] **The reasoning that does exist is compacted, pruned and rolled back** — [M]. The only durable
      trace of intent is the raw MAF message blob in `session_messages`, and three mechanisms erode
      it: `session_store._compact` rewrites rows, `durable/retention.py` prunes by age, and
      `rollback_to` deletes a turn's rows on client disconnect.
- [ ] **The approved plan's text is still not durable** — [M]. D-157 made the authorization bind to
      the plan, but the plan itself lives in an in-process `TodoSessionStore`, so a `plan_approvals`
      row still points at a `plan_hash` whose subject exists nowhere durable — a signature on a
      document nobody kept — and an eviction forces re-approval for reasons unrelated to the plan.
      Wants a `session_plans` table read back on rehydration. Also the durable half of `SCALE-1b`.
- [ ] **Agent-authored notes cannot carry the provenance fields built for them** — [S].
      `propose_knowledge_note` accepts only `id/type/body/compound_smiles/tags/source`, so the model
      cannot attach `calc_refs`, `artifact_refs`, typed `relations`, `confidence` or a validity
      window — every field D-133/D-134 added for exactly this.
- [ ] **`calc_refs` is written on two paths of three** — [S]. D-158 wired the QM note;
      D-2026-07-31-a-campaign-is-an-entity now carries the featurization's calculation keys out to
      the BO suggestion and onto any `experiment-proposal` note drafted from it. What remains is
      `connectors/bo/knowledge.py` — the *durable* campaign's note, which never featurizes, so it
      has no calculation to cite until the durable path is reconciled with the inline one. ELN
      reaction notes rest on no calculation at all and correctly cite none.
- [ ] **`Note.confidence` is never set by any machine path — and the obvious fix would make
      things worse** — [M], re-diagnosed while implementing it. One consequence stands
      (`kg/conflicts.py` needs a confidence on both sides); the truncation half is now stale twice
      over — the cross-source score ordering is gone, and `EvidenceChunk.score` orders only a
      source's own list (D-2026-08-01-a-cap-that-starves-a-source). What is wrong is the implied
      remedy. The two consumers want *different signals*: retrieval wants a trust score for
      ordering, `_suspected` wants a disagreement signal — and it fires purely on a confidence gap
      ≥ `conflict_confidence_gap` between same-`(type, compound_smiles)` notes. So populating
      confidence from **record completeness**, the obvious machine source, would flag "one run is
      better documented than another" as a suspected conflict: manufactured noise, in a module
      whose own docstring says a wrong answer here is worse than none. And the one *principled*
      source — a calculator's calibration — reports `n=0`, because nothing but `predict_pka`/
      `predict_solubility` writes to the ledger (see the row above). So there is currently no
      honest machine source at all. The unblocking work is the **value comparator** (two yields for
      one transformation), not more producers.
      `propose_knowledge_note` can now state a confidence, which is the right home for a judgement
      call: a human reviews it at the gate.

**The write-back paths are open loops.**

- [x] **The PR-gate opens no PR and notifies nobody** — the proposal-surface half is closed by
      D-2026-07-31-a-proposal-is-a-record-not-a-branch: `note_proposals` records every submission
      with its provenance and the rendered note, `GET /proposals`, `GET /proposals/{id}` and
      `POST /proposals/{id}/decision` make the queue operable, and the merge webhook — now
      HMAC-signed, because its body carries an authorization-shaped claim — closes rows so the
      queue drains. `rejected` is a state the system has for the first time.
- [ ] **The gate is findable, not pushed, and still opens no PR object** — [M], what the row above
      deliberately left. (a) **No notification**: a new proposal reaches nobody until someone opens
      the queue; routing it through the existing `notify` seam is a decision about who gets told
      what. (b) **No platform adapter**: opening the actual PR needs a real token and base URL to
      be verifiable, so it is unwritten. The shape is settled — a `NoteSubmitter` decorator that
      pushes, opens the PR, and returns its URL as the `reference` the record already stores.
- [x] **A failed proposal is counted but not recorded** — closed by the same ADR. A submission that
      never reached git now leaves a `failed` row carrying the rendered note, so the knowledge is
      replayable rather than only countable.
- [x] **The inline BO path persists nothing at all** — closed by
      D-2026-07-31-a-campaign-is-an-entity-not-a-turn. The problem, the observations it rested on,
      the candidates, the calculations behind the decision space, and the caller are all recorded
      against a campaign; the tool returns the `campaign_id` so a later turn adds to the same one.
      The skill tells the agent to quote it back and to cite `calc_refs` on any
      `experiment-proposal` note it drafts.
- [x] **There is no first-class campaign entity** — closed by the same ADR, and by *not* making it
      something a chemist starts: `campaign_id` is a hash of the decision space and objective, so
      three refinements of one optimization accumulate against one campaign with nobody having to
      open one first. A chemist does not know at the first question that they are beginning a
      campaign, which is why "start one" was the wrong shape.
- [ ] **The retrospective `optimization-campaign` note still has no link to a BO campaign** — [M].
      DRFP clustering mints one from ingested reactions and `bo_campaigns` now exists beside it,
      with nothing joining them. The join wants the same matching rule as the row below, so the two
      belong together.
- [ ] **The BO loop is open at one end now, not two** — [L]. The proposing half is recorded: a
      suggestion, its evidence and its campaign survive the turn. What is still missing is the
      return path — nothing decides that an ingested `reaction` note *is* the execution of a given
      candidate. That needs a matching rule over conditions with tolerances, on parameters an ELN
      records inconsistently, and getting it wrong attributes a result to an experiment nobody ran,
      which is worse than the open loop. It wants its own decision rather than a heuristic. Until
      then `bo_regret` can only be scored against a benchmark surrogate, and
      `connectors/bo/activities.py` still stamps every observation `provenance="predicted"`.
- [x] **The BO note is a graph island** — closed by D-157: `note_with_run_provenance` stamps the run
      and its reason onto any connector's note, and the BO note now carries its decision space.
- [ ] **Two of ~12 calculators log predictions, and nothing reconciles ELN data** — [M]. Only
      `predict_solubility`/`predict_pka` write to the `predictions` ledger, and its only reconciler
      is `report_measurement`, a chat tool a human must type. The ELN sync ingests measured yields
      and never touches it, so `calculator_trust` will report "not yet calibrated" indefinitely.
      `calibration.record_prediction` also swallows every exception, so a wrong DSN produces a
      permanently empty ledger that reports `n=0` forever.

**Controls that are advisory where the documents say binding.**

- [x] **The plan gate gates loop continuation, not side effects** — closed by D-167:
      `agent/plan_gate.py::enforce_plan_approval` gates the *act*, and the approval is checked
      against the durable store rather than in-process state. D-168 extends the same to a template
      step, which now runs as its requester.
- [x] **Dry-run does not cover the write tools it advertises** — closed by
      D-2026-07-31-one-gate-over-one-side-effecting-set, as this row proposed: one middleware over
      `authz.side_effecting_tools()` (the set D-167 had already assembled), and the three ad-hoc
      checks deleted. The set moved out of `plan_gate` on the way, because dry-run applies whether
      or not the harness is on.
- [x] **Every shipped connector is unauthenticated** — closed by
      D-2026-08-06-a-connector-that-authenticates-nobody. `connector_token_env` names the fleet's
      shared credential; connector servers require it and clients send it to any connector whose
      manifest declares `mode: none` — the set that declares itself inside our boundary — while a
      connector with its own `bearer` keeps it, because sending the fleet key to a third party is
      the thing being protected against. `mode: none` off loopback with nothing to present is now a
      startup refusal rather than a silent open port.
      **`allowed_tools` stays a client-side filter, and that is the decision rather than the
      remainder.** Enforcing the manifest's `tools` list server-side would break the ingestion path,
      which legitimately calls index tools outside the *agent's* subset (`connector_app`'s docstring
      already said so). One surface with two legitimate clients wants authenticating, not
      partitioning — and once the port refuses unauthenticated callers, the exposure the row
      described is closed by the channel rather than by the list. The Entra auth modes are
      unchanged: they still need the tenant that blocks every other live Entra edge.
- [ ] **Egress is still port-scoped by default** — [S], **narrowed, not closed**. Unrestricted
      egress is no longer *inherited*: an empty `egressDestinations` now requires
      `allowEgressAnywhere` or the chart refuses to render, naming both ways out
      (D-2026-08-06-a-secret-that-masks-itself-when-you-forget) — the shape
      `service_allow_insecure` and the connector opt-out already use. What remains is what the row
      always said: narrowing needs the operator's real CIDRs, and until a deployment sets them
      `tests/test_no_egress.py` is a source scan rather than a control. Deriving them from the
      chart's own Service addresses was considered and rejected: the Postgres host lives in a
      *secret*, so a derived policy would silently black-hole database traffic.
- [x] **Workload identity federation is dead code the docs lean on** — closed by
      D-2026-08-06-a-secret-that-masks-itself-when-you-forget, by taking the documents half: nothing
      offline can legitimately call it (both consumers — the connector `entra_workload`/`entra_obo`
      modes and per-user warehouse reads — wait on the same tenant), so "wire it" was never
      available. `deploy/README.md`'s "everything that *can* federate does", offered as the reason so
      few plain secrets are needed, had the argument backwards: the plain secrets that exist are
      exactly the ones federation **cannot** supply, and it removes none of them once wired. The
      missing `azure.workload.identity/use` label is added to every connector pod, and a test now
      asserts it on every pod spec that names a ServiceAccount — nothing asserted it anywhere, which
      is how four templates carried it and one did not.
- [~] **Secrets are plain `str`, never rotated** — the six credential fields are `SecretStr` and
      `hpcArtifactStoreToken` has its chart key
      (D-2026-08-06-a-secret-that-masks-itself-when-you-forget). **The three DSNs deliberately stay
      `str`**: they are read directly in 26 modules, which is the same duplication the store-seam row
      records as "the connect helper is hand-rolled 14 times", so converting now means writing 26
      `.get_secret_value()` calls to delete 25 when the helper lands. They keep the redactor's
      coverage meanwhile. **What the measurement changed:** the row's stated hazard (a repr in a log)
      was already covered by the redactor — what is *not* covered is every non-log path, and `repr`,
      `str`, `model_dump` and a JSON dump each leaked. And the conversion's own failure is silent:
      an f-string, an `Any`-typed sink and an `lru_cache`-wrapped callee all render a `SecretStr` as
      its mask while type-checking cleanly, so `mypy` found three of four sites and an AST guard now
      covers the shape.
- [x] **One database credential can rewrite the audit chain** — closed by
      D-2026-08-05-append-only-by-grant-not-by-contract. `postgres_migration_dsn` splits the schema
      owner from the runtime role; `infra/sql/grants/` grants the runtime role exactly the verbs
      `src/` executes, derived from the SQL literals and checked in both directions by
      `tests/test_database_privileges.py`. Verified against a live Postgres: INSERT on
      `audit_events` succeeds, UPDATE/DELETE/TRUNCATE are refused, as are DDL, the ledger, and
      DELETE on the tables retention already refuses to prune. The owner credential can still
      rewrite the trail — this narrows who holds that power and for how long, so the chain and the
      anchors remain the evidence, and the ADR says so rather than claiming tamper-proofing.

**Trust: the system predicts without saying when not to be trusted.**

- [x] **F8-T1 — no applicability domain and no uniform uncertainty contract** — closed by
      D-2026-08-01-unknown-is-not-fine, with the domain half deliberately scoped. `Estimate`
      (`science/calc/uncertainty.py`) carries `value + unit + uncertainty + method + in_domain +
      domain_reasons` beside each calculator's own result, `in_domain` is three-valued so "unknown"
      can never read as "fine" (`trustworthy` requires an affirmative `True`), and `method`
      distinguishes a constant from a paper's test set from a split-conformal interval over this
      deployment's own reconciled residuals — which the calibration ledger has been recording since
      REV-12 and nothing consumed.
      **The domain that shipped is structural, not statistical, and the difference is the point.**
      That a salt, an ion or an organometallic is out of domain follows from what the ESOL equation
      *is* — one molecule, neutral, organic contributions to sum — and is citable without its
      training data. A descriptor-range or leverage cutoff would need that training set, which this
      repository does not ship; inventing bounds and calling them "the training ranges" would put a
      fabricated threshold into a GxP record, which is worse than no check because a check that
      exists gets trusted.
- [ ] **F8-T1b — the statistical applicability domain still needs training data** — [S], blocked on
      a labelled solubility corpus (ESOL's own set, or any other) to derive descriptor bounds or a
      leverage cutoff from. Structural checks catch a salt and an organometallic; they do not catch
      a perfectly ordinary neutral organic that is simply far from anything the model was fitted on.
      This is a data-acquisition decision, not a coding one.
- [x] **Uncertainty stops at the calculator and never reaches a note** — closed by
      D-2026-08-01-trust-travels-on-the-value-line, with two of its three claims corrected on the
      way. `Estimate.render()` puts value, unit, uncertainty, its provenance and any domain failure
      on the value line — inline, because `_excerpt` is a blind 240-character prefix and a trust
      stanza below the value is cut from exactly the notes carrying the most prose.
      **"The calculator layer carries it well" was false where it mattered.** QM has no error bar
      and cannot acquire one (an absolute total energy has none — `science/calc/reaction.py` already
      says so), so its honest gain is the unit plus convergence restated as the domain flag. BO's
      uncertainty *was* computed on every model-guided ask and dropped by
      `engine._frame_to_candidates` one function before anything could record it; recovered onto
      `Candidate`, carried through `Observation`, written into the note.
      **A structured front-matter field was deliberately not added**: `_excerpt` reads the body, so
      the prose line is what retrieval quotes, and a field would need threading through `NoteRef`
      and `EvidenceChunk` for zero readers. Additive later if a machine consumer appears.
- [ ] **`Estimate` is a three-writer contract, and four calculators are still outside it** — [S].
      `pka`, `logd`, `reaction` and `xtb_thermo` each carry an uncertainty under their own field
      name (`uncertainty`, `uncertainty_kcal`) with no `method` and no domain answer. None of them
      writes a note today, which is why the row above did not force the conversion — but a skill
      consulting "how far do I trust this" still gets four shapes and one.
- [ ] **`conformal_uncertainty` has no caller** — [S]. It needs a database read of the calibration
      ledger's reconciled residuals, so it belongs on the cached path rather than the inline one;
      until that is wired, `calibration_conformal_coverage` and `calibration_conformal_min_samples`
      are configured and unread, and every `method` in the system is `reported` or `none`.
- [x] **F9-T3 — zero evaluation of agent behaviour** — closed by
      D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment. `evals/autonomy.py`
      registers `plan_quality`, `runaway_rate` and `plan_execute_utility`; four cases join the
      versioned set and three numbers join `baseline.json`, so `make eval-strict` and the drift
      check now cover them.
      **"Zero evaluation" was overstated**: `tests/test_harness_execution.py` already drove real MAF
      machinery and pinned the loop cap. What was missing was its absence from the *eval layer*.
      Two of the three measures already existed in pieces — `evals/ab.py::compare_tool_utility` is
      the A/B and was simply never registered, and `precision_recall_f1` already defines "did it
      name the right things".
      **`runaway_rate` originally inferred the cap from its residue** (an answer sent with todos
      still open), because `AgentLoopMiddleware` stops and returns normally without emitting
      anything. That proxy counted a turn that correctly deferred to a durable job as a runaway —
      `mark_awaiting_job` leaves the same residue — so it was replaced by the explicit signal
      described in the row below.
      **AG-13 is not closed by this and the ADR says so explicitly**: a scripted transcript pins the
      model's replies, so these gate the harness's plumbing, never the model's judgment.
- [x] **The loop cap is silent, so nothing can alert on a runaway in production** — closed.
      `chemclaw.agent.loop_cap` wraps the loop predicate and records its last stop decision (the
      loop hit the cap exactly when it still wanted another iteration — MAF exposes no hook on the
      cap itself), `run_turn` emits `ErrorEvent(code="loop_cap_reached")` before the partial answer
      and increments `chemclaw_turn_loop_caps_total`, and `runaway_rate` scores that instead of the
      residue it used to guess from. An `ErrorCode` member rather than a new `Event` member: a
      capped turn is cut off, which is what `turn_timeout` and `budget_exhausted` already say.
      *Left open:* `Chemclaw3_ui` renders the new code through its generic error path and does not
      yet label it.
- [ ] **The plan-vs-single-shot A/B has no real task set** — [M], blocked on AG-13.
      `plan_execute_utility` scores the pairs a case hands it, and the shipped case is illustrative.
      Genuine baseline-vs-augmented numbers mean running the same tasks twice against a live model.
- [ ] **The retrieval eval still scores only `GraphRetriever`** — [M], but it no longer *pretends*
      otherwise: under `hybrid`, or with `vector`/`lexical` active, the metric raises rather than
      report a graph-only recall under a name that promises the shipped path. Scoring the fused and
      derived paths for real needs the note index built over the eval fixture corpus, which needs
      Postgres — the same blocker as `DEFERRED.md`'s live-retriever-drift row, and the reason this
      is [M] rather than the [S] it was first filed as.
- [x] **The CI step named "the scientific quality gates" cannot fail on a science regression** —
      closed. The blocker was not the CLI's exit code: two shipped cases exist to *demonstrate* a
      gate firing, so a command that failed on any failure would have been red from the day they
      were written. `EvalCase.expect_pass` separates a demonstration from a regression,
      `EvalReport.regressions()` is the difference, and `make eval-strict` — what CI now runs —
      exits non-zero on one.

**Data correctness, in rough order of how expensive it gets to fix later.**

- [x] **No molecule standardization** — closed by D-2026-07-31-two-spellings-of-one-molecule,
      before first real ingest as the row asked. The pipeline splits the one function in two,
      because there were always two questions: `canonical_smiles` answers "same structure" and
      keys the calculation cache (an anion must not silently become its conjugate acid — the test
      suite caught that immediately), while `standard_smiles` answers "same compound" and keys
      `compound_id`, both fingerprint indices, chain matching and progression grouping.
      `STANDARDIZATION_VERSION` in the fingerprint definitions retires stale rows rather than
      ranking two notions of sameness against each other.
- [ ] **Stereochemistry is left exactly as RDKit reports it** — [M], and deliberately out of the
      row above. Collapsing a racemate onto a single enantiomer is a chemistry decision with real
      consequences for a chiral route, and it is not one to make as a side effect of stripping
      counterions. Wants its own argument, and probably a per-deployment answer.
- [x] **An amended ELN entry is silently discarded** — closed by
      D-2026-07-31-an-eln-entry-is-versioned-not-immutable, at both layers. The adapters filter on
      `entry_window(created, modified)`, so a correction re-enters the fetch window at all — it
      could not before, which is why the sync never even saw one — and the sync compares the note's
      *body* rather than its id. An amendment is a re-proposal of the same note, so the PR-gate
      shows a reviewer the diff; no second versioning scheme, because git already expresses this.
- [ ] **A retracted ELN entry stays current evidence** — [M], the half the row above deliberately
      did not close. A withdrawn entry that simply disappears from the export is invisible to a
      sync that only reads what is present, and treating a missing file as a retraction would make
      an export glitch indistinguishable from a withdrawal. Noticing absence needs a full
      reconciliation pass against the source, which is a different mechanism from the incremental
      cursor and wants its own decision.
- [x] **No source-system provenance on ingested records** — closed for what a file-drop adapter
      can honestly claim: `eln-json:<entry_id>:<operator>` names the source *format* and the record,
      so two systems' colliding entry ids are at least distinguishable in the note. A file adapter
      is handed a directory, not a tenant, so it cannot name an instance; a connector talking to a
      real ELN knows its own and should say so there.
- [ ] **Mass balance is element-set subsumption only** — [M]. `ingest/eln/validate.py` checks that
      no product element is absent from the inputs, so `benzene + methanol >> paracetamol` passes.
      No charge balance, no yield-vs-limiting-reagent check despite `amount_mmol` being parsed. The
      stronger check already exists and is not reused (`science/calc/reaction.py:178`).
- [ ] **`note_index` has no embedding-model identity, and there is no chunking** — [M], **and it
      is the one item of this block that was not attempted**, so it is stated rather than
      half-done. Changing `CHEMCLAW_EMBEDDING_MODEL` serves mixed-generation vectors until someone
      remembers `make reindex`, and nothing detects it — while the in-process embed cache *is*
      keyed on the model and its docstring names this hazard. Separately, a note is one vector over
      its whole body and the returned excerpt is `body[:240]`, so a reaction note's matched
      procedure step is never what comes back. The two halves are one migration and one reindex,
      and chunking in particular changes what a retrieval hit *is* — every eval baseline moves with
      it — so it earns its own change rather than riding along with molecule identity.
- [x] **Hazard screening misses the notes that propose conditions** — the note-type half is
      closed: the gate covers `experiment-proposal` and `bo-candidate` by type, not only by a
      `## Procedure` heading a parameter table does not have.
- [ ] **Pair rules have no notion of sequence** — [M], the other half of that row. They fire across
      components of one mixture SMILES, so a quench reagent added at step 8 is screened against one
      consumed at step 1. Needs a `same_step` scope on the rule table, which is a change to
      `rules.yaml`'s schema rather than to the matcher.
- [x] **Reaction fingerprints are dominated by solvent choice** — closed by
      D-2026-08-01-the-agent-slot-that-changed-no-bits, which is the *second* attempt: this row
      was ticked once for moving solvent and catalyst into the agent slot, and DRFP folds that slot
      back onto the reactants (`sides[0] += "." + sides[1]`), so the bits were byte-identical and a
      THF/2-MeTHF pair still scored 0.8194. The fingerprint is now built from
      `OrdReaction.transformation_smiles` — `reactants>>products` with the agent-slot species left
      out and every species standardized — and that pair scores 1.0. `reaction_smiles` stays as the
      record form a note renders; reagents stay on the left. `drfp:b2048:agents-excluded:std4`
      retires rows built under either earlier token.
**Operations.**

- [x] **No backup, restore or DR anywhere** — the GxP trap is closed by
      D-2026-08-01-a-restore-is-a-truncation-nobody-can-see; the tooling is deliberately not, and
      the row is split rather than half-ticked. The trap was the whole reason this was [L]: a
      restore is a *trailing* deletion, the one alteration the chain cannot see, so writing the
      recovery procedure without an anchor would have documented how to silently shorten the
      compliance trail. A signed high-water anchor now closes it — published to the log as well as
      the database, because a PITR rolls the database copy back into agreement with the truncated
      trail it exists to catch. `runbook.md` §(xiii) is the restore procedure and states what the
      system requires of each of the four stores.
- [ ] **The audit trail's append rate has a fleet-wide ceiling** — [L], and recorded so it is not
      rediscovered as a defect. Every `PostgresAuditSink.record` takes
      `pg_advisory_xact_lock(0x43484D4157_00_01)` before reading the chain tip, so every audited
      tool call in the whole deployment serializes on one lock for ~4 round trips — a ceiling of a
      few hundred appends/second fleet-wide, on the turn's own hot path (the write is awaited, and
      shielded so a cancelled turn keeps its row). Correct by design: two appends that read one tip
      fork the chain, and a forked chain cannot be repaired. Far above current demand — 48
      concurrent turns at a handful of tool calls each is well under one percent utilisation — so
      this is a number to know before scaling the fleet an order of magnitude, not work to do now.
      Measured during the 2026-08-05 database review.
- [ ] **Eight tables retention neither prunes nor refuses** — [M]. `durable/retention.py` names
      two prunable tables and *refuses* three with stated reasons (`audit_events`, `job_records`,
      `calculation_results`), which is the right shape. The rest are simply unlisted:
      `session_owners`, `session_turns`, `turn_costs`, `predictions`, `measurements`,
      `note_proposals`, `plan_approvals`, `bo_suggestions`. Each grows for the life of the
      deployment with no policy either way, and "unlisted" reads as neither decided nor deferred.
      Wants: a disposal decision per table — pruned, or refused with its reason — not a sweep that
      picks them up by default. `infra/sql/README.md` is the current inventory. (2026-08-05
      database review.)
- [ ] **A pruned session keeps its listable identity** — [L]. Retention prunes `session_messages`
      and leaves the `session_owners` row, so `SessionOwnerStore.list_for_owner` still returns the
      session and opening it shows an empty conversation. Not a correctness bug — the id is
      genuinely still owned — but the listing means "what was I working on", and an entry with
      nothing behind it does not answer that. Wants: either the owner row goes with the last
      message, or the listing filters on remaining history. (2026-08-05 database review.)
- [ ] **No backup *tooling*, and three stores whose recovery is someone else's** — [M]. The anchor
      made a restore safe to perform; nothing here performs one. Deliberately: this chart deploys
      neither Postgres nor Temporal (the row below), so a `pg_dump` CronJob would claim ownership of
      stores it does not own and be wrong for the expected case of a managed instance with its own
      snapshot policy. Wants: whatever the Postgres/Temporal ownership row settles, plus an
      RPO/RTO an operator can hold their provider to. Only the audit trail needs a point-in-time
      story — the calculation cache is regenerable by definition (D-011), the note index is rebuilt
      by `make reindex`, and the knowledge repo is git, so every clone is already a backup.
- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart,
      no operator manifest, no `register_namespace` call, no retention/archival config, no HA or
      sizing guidance. `helm install` does not produce a working system.
- [x] **Worker and connector metrics go nowhere** — closed by
      D-2026-08-01-every-process-carries-its-own-witness, together with the probes row below: they
      were one missing thing. `core/worker_http.py` serves `/healthz`, `/readyz` and `/metrics`
      beside every Temporal worker, `connectors/server.py` serves `/metrics`, the ServiceMonitor
      drops its `component: service` selector (so every connector Service is collected too), and a
      PodMonitor collects the worker pods, which have no Service by design. The row's diagnosis was
      right and understated: the false sentence was in three places, one of them a *test assertion*
      pinning the narrow selector, which is how it stayed true-looking.
- [x] **No PDB, no topology spread, no graceful shutdown** — closed by
      D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps, except the singleton, which is split out
      below. Both grace periods are now *derived* from the budget they must outlast (the turn
      timeout; the worker drain budget) rather than written as numbers, plus a `preStop` sleep,
      `topologySpreadConstraints` and a `maxUnavailable: 1` PDB on the front door. The row named the
      chart gaps and missed the deeper one: `asyncio.run` around `worker.run()` installed **no
      SIGTERM handler at all**, so a worker did not merely have too little grace — it had no
      shutdown path, and every drain killed it mid-activity. `durable/serve.py` is that path.
      **No PDB on the workers, deliberately**: over a `replicas: 1` singleton, `minAvailable: 1`
      blocks every node drain in the cluster forever and `maxUnavailable: 1` permits what no PDB
      permits. The ADR argues it; the fix is the row below, not a policy object.
- [ ] **The background worker is a hard singleton** — [M]. `workers.background.replicas: 1` owns ELN
      sync, memory synthesis, retention, eval drift and audit-chain verification, and cannot be
      scaled because the PR-gate checkout lock is host-local (D-069). Split from the row above,
      which closed everything *except* this: it is the actual availability gap, it needs the
      distributed lock (its own row), and it is the one thing a PDB would make worse rather than
      better.
- [x] **Workers and connectors have no probes** — closed by
      D-2026-08-01-every-process-carries-its-own-witness (see the row above; one HTTP surface
      answers both). `/readyz` is the worker's own `is_running` and `/healthz` is served on its
      event loop, so a wedged loop is a restart rather than a permanently `Running` pod. Readiness
      and liveness are deliberately different signals here: a worker that stopped polling serves no
      traffic, so restarting on that alone would turn a Temporal reconnect into a crash loop.
      Connector *servers* keep one route for both, and honestly so — uvicorn accepts only after the
      lifespan that starts the MCP session manager has completed, so `/healthz` answering **is** the
      readiness evidence and a second route could only restate it.
- [x] **Migrations take no advisory lock and have no lock timeout** — closed by
      D-2026-08-01-a-migration-waits-in-front-of-live-traffic (the forward-only half is still
      tracked below). A transaction-scoped `pg_advisory_xact_lock` serializes migrators, and
      `lock_timeout` — *not* `statement_timeout`, which would bound an index build rather than the
      wait — caps how long DDL may queue for a table lock. Two budgets, deliberately far apart:
      waiting for a peer migrator is a legitimate event (300 s), waiting in front of live traffic is
      not (5 s). `activeDeadlineSeconds` on the hook Job, with the `pending-upgrade` recovery now in
      `runbook.md` §(xi). One correction: the row (following the module's own docstring) implied
      services migrate at startup; nothing has ever done that, `migrate()` has one caller and it is
      its own `__main__`.
- [x] **Image not pinned, supply chain ungated** — closed by
      D-2026-08-01-a-tag-is-a-pointer-not-a-build, except signing (below) and the licence decision
      itself (below), neither of which is this repo's to make. `image.digest` through one helper
      every pod uses, `image.pullSecrets` on every pod spec, `ARG BASE_IMAGE` so a release pins the
      base, and three **blocking** CI gates: `pip-audit` over the exported lockfile, a retained SPDX
      SBOM with the built digest, and a `trivy` image scan on fixable HIGH/CRITICAL. The base image
      is deliberately *not* digest-pinned in the file: a pinned digest goes stale in weeks and every
      developer build then pulls a base months behind on CVE fixes, so the dev default floats, a
      release pins, and the SBOM records what a build actually contained.
- [ ] **The image vulnerability scan is written but not merged as a gate** — [M]. `trivy image
      --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL` was built in
      D-2026-08-01-a-tag-is-a-pointer-not-a-build, run eight times against real builds, and pulled
      back out. It **earned its keep**: three classes of real problem, all now fixed in
      `deploy/Containerfile` and staying — base OS errata (`dnf -y update`), `setuptools` 65.5.1 in
      the base interpreters, and **uv's wheel cache shipped inside the runtime image**
      (`uv cache clean`). None was reachable by `make deps-audit`, the offline suite, `mypy` or
      review; all had been in every image this repo has ever built.
      **Why it is not merged:** after all three fixes it still reports `setuptools` 70.3.0 and
      `msgpack` 1.1.2, while an exhaustive `find / -xdev` in the same build — printed into the build
      log, and still there — lists every versioned artifact of both and contains neither. A gate
      whose last word contradicts the artifact it scanned makes every future red build ambiguous,
      which is the non-blocking-scanner disease from the other direction. The two ways to ship it
      anyway are both worse: softening the severity is the failure the row existed to end, and an
      `--ignore-vuln` whose reason is "I could not find it" is a documented decision resting on an
      unverified claim.
      **What it needs:** an environment with a container runtime, where the built image can be
      inspected interactively. Every hypothesis here cost a full CI round trip. Start by finding
      what trivy is actually reading — `trivy image --list-all-pkgs` names the file per package.
- [ ] **No image signing or admission policy** — [M]. Pinning by digest is the property a signature
      would enforce; adding one nothing verifies would be a fourth control reporting to nobody.
      Needs a key, a policy admission controller, and a registry to push to — all three belong to
      the cluster-ownership row below.
- [ ] **Shipping crest (GPL-3.0) is an unmade decision, and now a takeable one** — [S], and not an
      engineering task: whether to redistribute a GPL-3.0 binary inside a product image is the
      product owner's call. What was wrong was that taking it required editing a `RUN` block, so it
      looked like writing a patch and was therefore never taken. `--build-arg INCLUDE_CREST=false`
      builds without it; `calc.crest_cli` already reports unavailable rather than failing, so the
      image loses conformer sampling and nothing else. **Owner: whoever owns the product's
      licensing.** xtb (LGPL-3.0) is not in question — it is invoked as a separate process over
      files and never linked, which is the same analysis crest gets and the reason this is a
      distribution question rather than a licence-compatibility one.
- [x] **No rate limiting; attachments buffer before they are checked** — closed by
      D-2026-08-01-a-cheap-request-is-still-a-request. Three layers, each at the only level that can
      enforce it: uvicorn flags for connections/keep-alive/header size (the app never sees these),
      an ASGI `_BodySizeLimit` above body parsing, and a per-principal token bucket spent inside
      `require_principal` so it covers every authenticated route and none of the probes.
      Two corrections to the row. The buffering is to a **spooled temp file** (RAM to 1 MB, then the
      pod's disk), not to memory — worse in a different way, and `await file.read()` was the second
      problem rather than the first: the body is already fully ingested before any handler runs, so
      no fix inside the handler could have worked. And `parse_attachment`'s check is **not** the one
      in the wrong place to be deleted — it is a different check (data-shaped, 422, with a second
      caller in the backfill CLI) that stays beside the transport-shaped 413.
- [x] **Tracing is shallow and the docs overstate it** — closed by
      D-2026-08-01-a-turn-you-can-follow-across-a-process, with what is still absent named rather
      than implied. Two first-party spans (`chemclaw.turn`, `chemclaw.tool`) and W3C `traceparent`
      on connector calls, adopted server-side — so a calculation's spans are children of the turn
      that asked for it instead of an orphan trace. The row's parenthesis was the whole finding: the
      custom correlation header exists *because* the standard one was not being sent. Both stay and
      they answer different questions — the correlation id is what `audit_events` is keyed on and
      works with no collector; `traceparent` is what makes a trace a tree. `deploy/README.md`'s
      claims about job spans and dashboards are deleted rather than softened.
- [ ] **No span around a durable job, and no auto-instrumentation** — [M]. The two boundaries above
      are in one process; a job spans two and a Temporal boundary, so the workflow has to carry the
      trace context in its payload — a real design question (payloads are replayed, and a stale
      `traceparent` would attach a replay to the original trace) rather than another `start_span`.
      Separately: no FastAPI/httpx/Temporal auto-instrumentation packages, which would give HTTP and
      database spans under the two first-party ones for the cost of three dependencies.
- [x] **Logs are unstructured and unredacted** — closed by
      D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not, with the redaction half
      re-scoped. Every record now carries `correlation_id`/`actor`/`session_id` from the
      ContextVars that already existed, and `CHEMCLAW_LOG_JSON` (on in the chart) emits one JSON
      object per line.
      **The re-scope:** the row reads as "redact the PII `SECURITY.md` says the audit trail holds",
      and doing that would break the requirement the trail exists to meet — `SECURITY.md` says in
      the same breath that recording tool-call arguments is *intentional*, because GxP needs an
      attributable "who did what to which inputs" record. What has no such justification is a
      **credential** in an ordinary log line, and `core/db.py::_redact` (a DSN password stripped in
      exactly one place) was the tell that the concern was real and unsystematised. So the filter
      matches the secret *values* this process holds rather than guessing at token-shaped strings,
      and the deployment's retention/PII policy over the audit trail remains a policy question, not
      a code one.
- [x] **Autoscaling defeats the admission guard's purpose** — closed by
      D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down. The guard stays
      per-process (SCALE-1's trade is unchanged); what was missing was the *arithmetic*.
      `service_fleet_replicas` is derived by the chart from `autoscaling.maxReplicas`, so it cannot
      drift from the number the HPA obeys, and `service_fleet_max_concurrent_turns` is the ceiling
      the LLM endpoint's throughput budget permits — a configuration whose
      `replicas × workers × cap` exceeds it refuses to start, naming the product and every factor.
      The chart declares 48, exactly `6 × 1 × 8`, so it ships as a statement of today's shape rather
      than slack, and a test rejects a chart whose autoscaling outruns its own declaration.
      The metric half is `chemclaw_fleet_turn_ceiling` against `sum(chemclaw_turn_capacity)`, which
      catches what startup validation structurally cannot: a hand-scaled Deployment, an HPA edited
      in-cluster, or a rollout with both generations up — each of which passes the fleet ceiling
      while every pod's own config stays valid.
      **Not closed by this:** the 49th turn is still admitted. This bounds the configuration, not
      the request; fleet-wide *admission* remains rejected under SCALE-1.
- [x] **No cost attribution** — closed by D-2026-08-01-spend-is-a-ledger-not-a-label, with one of
      the row's two claims corrected. `turn_costs` books one row per turn — actor, session, profile,
      the four token counts, duration, and whether it answered — keyed on `correlation_id`, so
      "what did team X cost" is a `GROUP BY` that joins to the audit trail. A turn torn down by a
      disconnect is billed and marked `completed=false`, because that is the runaway the ledger
      exists to find. An `actor` **label** was the obvious fix and is unavailable by design: the
      registry refuses a counter past 64 label series (D-152) because the value is
      attacker-influenced, so per-actor attribution needs unbounded cardinality and quarters of
      history — a database's job, not Prometheus's.
      **The compute claim was half false.** `chemclaw_jobs_started_total` has existed since D-118
      (`connectors/jobs.py`). What was missing is a *magnitude*: a two-second xTB call and a
      six-hour DFT run incremented it identically. `job_records.runtime_seconds` (measured across
      the child with `workflow.now()`) and `chemclaw_job_runtime_seconds_total{connector}` fix that.
      **Still open, and named rather than implied:** node-hours. Parallelism belongs to the
      launcher and none reports it back yet — see the row below. Pricing is deliberately absent: the
      ledger records quantities, and a rate card is a deployment's own fact.
- [ ] **Node-hours are still unmeasured** — [S], the half of cost attribution that needs a live
      cluster. `runtime_seconds` is wall clock across the child workflow; what an HPC run actually
      costs is that times its allocation, which only the launcher knows. Seqera/Tower reports task
      resource usage on a completed run; plumbing it back through `NextflowLauncher` into
      `job_records` is the fix, and it cannot be verified without a real Tower endpoint.

**Product floor.**

- [x] **No durable-job surface for a user** — closed by D-2026-08-01-a-running-job-has-no-owner,
      with the row's own premise corrected. `GET /jobs`, `GET /jobs/{id}` and `DELETE /jobs/{id}`
      exist; the result already outlived Temporal history (D-157's `job_records`) and simply had no
      route. What could **not** be built is the row's *owner-scoped* cancel: `job_workflow_id`
      deliberately excludes the requester so two chemists asking for one campaign rejoin one run
      (D-011), so a running job has several requesters and cancelling it cancels it for all of
      them. It is an operator action, and the ADR says so rather than shipping a scope check that
      would read as ownership and not be it.
- [ ] **A chemist cannot stop their own runaway run** — [M], the cost the row above accepted. The
      fix is not a scope check on the cancel route; it is a *per-requester* job id, which trades a
      recompute of every shared expensive job for it — a change to D-011's idempotency contract
      with a measurable cost, so it wants its own decision.
- [ ] **No session delete, export, or pagination** — [M]. Only `POST`/`GET /sessions`; no per-user
      erasure across the seven tables that hold a conversation (a data-subject request is currently
      unimplementable, and `audit_events` is deliberately unprunable); `SessionSummary` is
      `session_id + created_at` with a `LIMIT` and no cursor, so past 100 sessions the older ones
      are unreachable.
- [x] **`GET /sessions/{id}/messages` loses the whole trace on reload** — the half that was
      recoverable is closed. Tool calls and their results were **never missing from storage**: a MAF
      message already holds `function_call`/`function_result` contents and the route was flattening
      them away, so `TranscriptMessage.tool_calls` reads what was always there. A `tool` message is
      folded into the call it answers rather than rendered twice, an unanswered call reports
      `result=None` (a real state: "it ran and we do not know how it ended"), and arguments are
      bounded like the audit trail's.
- [ ] **A plan snapshot, an attachment reference and an answer's confidence are not persisted at
      all** — [M], the other half, and it is a different problem from the row above. Those are
      turn-time events computed and streamed; nothing writes them to `session_messages`. Recovering
      them is a change to what a turn *stores*, not to how it is read, so it wants its own decision
      — including whether a plan snapshot per turn is worth the rows. Pagination belongs with it:
      the read goes through the history provider, which has no cursor, and giving it one is the
      same change.
- [x] **Every turn failure collapses to one opaque string** — closed. `ErrorEvent` carries a
      `code` from a short closed taxonomy, a `retryable` flag, and the `correlation_id` the audit
      trail is keyed on (the old message named the *session*, which the user already has). An
      unrecognised failure stays `internal` rather than guessing: a wrong `retryable=True` sends
      someone to burn another turn on a failure that cannot succeed.
- [ ] **No project as a first-class concept** — [M]. `project` is free text on a reaction and a note
      *tag*; no registry, no project-scoped retrieval, no project-scoped access. Broad internal read
      is a conscious call (`DEFERRED`, KM-9), but "which programme is this work part of" is how
      pharma R&D is organized and the graph cannot answer it reliably.

**Bookkeeping.**

- [x] **F8 and F9 have no backlog entries at all** — reconciled. F8-T2 was absorbed into F10-A and
      is done. Of the three never picked up, **F8-T1** (uncertainty + applicability domain) and
      **F9-T3** (autonomy metrics) got rows under *Trust* above when this analysis was written;
      **F9-T1** was still missing and is added below. All three are now in the file `CLAUDE.md`
      designates as the memory read at session start — a ticket tracked only in
      `implementation-tickets.md` is invisible to the session that would pick it up, which is how
      these three stayed unstarted through seven phases.
- [ ] **F9-T1 — `architektur.md` §6 still describes a stack this repo does not build** — [S], and
      `CLAUDE.md` already warns readers off it in prose ("historical, not current"). The ticket asks
      for §6 to name OpenShift, Nextflow-on-HPC and the internal LLM adapter instead of Azure AI
      Foundry / Container Apps / raw SLURM, keeping §7/§8. The warning is a workaround for the
      rewrite, not a substitute: the document is still the first thing a newcomer reads.
- [x] **The systemic guard: `prose-validate` widened to operator prose** — closed by
      D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check. Three rules over the operator
      documents: a backticked path must exist, an ADR id must resolve to a shipped decision (or to
      a sub-decision label some ADR defines), and a `CHEMCLAW_*` key must be a `Settings` field.
      Fixed what it caught: **33 module paths** dead since the D-148 move, `.github/workflows/`
      `deploy.yml` which never existed, and three unresolvable ADR citations.
      **The validator was itself an instance of the defect** — it labelled its own prose source
      `agents/chemclaw_agent.py`, a path gone since D-148.
- [ ] **`docs/planning/` fails the widened prose rules 175 times** — [M], and deliberately left out
      of the gate rather than swept. It is a different defect: a ticket that says "create
      `agents/qm_tools.py`" names a file D-118 later deleted, so there is no path to correct it *to*
      — the sentence needs rewording, one judgement at a time, and rewriting each to the nearest
      surviving module would falsify the build record the tickets exist to be. `BACKLOG.md` and
      `implementation-tickets.md` carry all 175. Add the glob to `_OPERATOR_DOC_GLOBS` when they are
      clean; a test currently pins the exclusion so it cannot lapse silently.
- [x] **The counts in prose, and the retired concepts** — closed by
      D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose. **Eight** counts corrected, not the
      nine reported: `values.yaml`'s "so a seventh cannot arrive unnoticed" is **right** — six
      secrets exist — and fixing it would have introduced the error. The rest went: `README.md`
      "three plain secrets"/"two Temporal workers"; `CLAUDE.md` "three-secret model"/"167 numbered
      ADRs" (165); `deploy/README.md` "Five exist" (six), in the paragraph that then says the count
      is no longer restated in prose; `architektur.md` "Nur drei Klartext-Secrets"; `runbook.md`
      "Six bundles" (seven) and "`bo` — the one that also owns durable work" (`calc`, `bo`, `qm`);
      `ARCHITECTURE.md` "the eight validators"; `test_helm_chart.py`'s "no sixth crept in".
      The remedy is the one this repo had already found twice and not applied everywhere: **prose
      names the set and where it is pinned, never how many.** Where a count aids a reader it became
      an assertion first — `tests/test_repo_map.py` now derives the bundle set and the
      durable-work set from `connector.yaml` on disk.
      Six retired concepts corrected too: two core task queues (one, plus a derived
      `connector-<name>` per bundle); an "MCP-Server" deployment role `deploy/entrypoint.sh` has
      never had; "MCP servers hold capability" where connectors do; "HPC/DFT is deferred" 58 lines
      after the same file says F5 shipped the launcher; `infra/sql/006` calling `actor` an unwired
      Phase-6 seam it stopped being at F4. Plus `runbook.md`'s wrong metric name
      (`chemclaw_tool_duration_seconds`) and wrong CLI invocation (`make explain SESSION=<id>`).
      **`architektur.md` §8 was wrong in the opposite direction** from the row that reported it: it
      says role-aware skill filtering must still be built, and `RoleScopedSkillsSource` has done it
      since D-052.

## Open — Every capability exercised live with the flags on (2026-07-31, D-155)

Full record: `docs/archive/live-matrix-2026-07.md`. The whole stack up natively with **every**
off-by-default flag enabled, driven with real Anthropic traffic and one signed identity per probe,
plus three parallel code reviews. Eight defects fixed under D-155; what follows is confirmed and
deliberately not fixed there, each because it needs a decision rather than a patch.

- [x] ~~**DARK-1 [High] — the harness plan-approval gate authorizes a session, not a plan.**~~ —
  **fixed (D-167).** Both blocking decisions taken: an approval binds to the plan's *work items*
  (reversing D-137, whose rendered-lines hash moved on the first ticked box and so could never be
  checked against the plan being executed), and the store follows the session store — which
  dissolves the fail-open/fail-closed question rather than answering it, since the approval and the
  mode it authorizes must share a lifetime. Enforcement is a function middleware at the tool
  boundary, inside audit and inside the denial converter; reads stay open so a plan can still be
  built. Running it live then found the fix incomplete: the model answered a *different* question
  without touching its todo list, so the plan identity never changed and the approval never lapsed.
  An approval is therefore also spent by the turn it authorizes. One residual limit, stated in
  D-167: the system cannot tell "proceed" from "a new question" in the single turn that follows an
  approval — bounded, audited, and immediately preceded by a human decision, but not zero.
- [x] ~~**DARK-2 [High] — a template step is a route around `authorize_trigger` and the audit
  trail.**~~ — **fixed (D-168).** A template step runs with the requester's entitlements, which is
  what the module's own docstring already claimed. The connector branch goes through the same
  audited, authorized path as the in-process one; the job step's pre-flight became one shared
  function (`prepare_job_launch`) called by both the chat launcher and the new `authorize_job_step`,
  which returns the *validated* payload so the workflow cannot start a child with raw arguments.
  Running the shipped `hazard-briefing` template live then found four further defects in the same
  path, all of which meant no template had ever executed a step in a deployment: `run_tool_step` and
  `run_agent_step` were registered on no worker, a tool step's `list[Content]` result could not
  cross the activity boundary, and an agent step could not run under `harness_enabled` at all.
- [x] ~~**DARK-3 — mid-turn resume claims other jobs' completions and drops them.**~~ — **fixed on main by D-153** while this pass was running. `await_job_results` no longer consumes the push-back mailbox at all: each job is awaited on its own Temporal handle, so there is no destructive claim to steal another job's completion with. Recorded rather than deleted because the review found it independently against the pre-D-153 tree.
- [ ] **DARK-4 [Med] — the durable job idempotency key omits every versioned input.**
  `job_workflow_id` hashes `[connector, job, payload]` only. Change `xtb_method` or a calibration
  constant and the calculation store correctly misses and recomputes, while `start_workflow` raises
  `WorkflowAlreadyStartedError`, rejoins the *completed* prior run, and returns numbers produced by
  the old method. `science/calc/store.py` takes the opposite and correct position for the same
  computations (`calc_version` is in the key). Fix needs to decide what "the version of a job" is.
- [x] ~~**DARK-5 [Med] — retention is one transaction over an unindexed column.**~~ The docstring claims
  each table is pruned "in its own statement so one failure cannot roll back the others"; there is a
  single commit after the loop. And `session_events` has no index on `created_at` — the gap
  migration 022 closed for `session_messages` — so under the 30 s statement timeout the sweep starts
  failing permanently once the table is big enough to need it. Wants a migration.
  **Fixed.** Migration 028 adds a partial index on `(created_at) WHERE consumed_at IS NOT NULL` —
  009's index is that predicate's exact complement, which is why there was never one to use — and
  each table now commits in its own statement, making the docstring's claim true rather than
  merely written.
- [x] ~~**DARK-6 [Med] — `verify_chain` loads the whole audit table into memory.**~~ No LIMIT, no
  watermark, `fetchall()`. This is the one table retention refuses to prune, so the scheduled check
  eventually times out or OOMs the shared background worker.
  **Fixed.** Paged by id, with the fold carrying the chain link across pages. Two tests pin that
  paging cannot change the verdict — in particular an interior deletion falling exactly on a page
  boundary, which a fold that reset per page would have read as a fresh genesis and reported clean.
- [x] ~~**DARK-7 [Low] — the digest re-reports every note at least twice.**~~ `_is_new` compares a
  `date` against `last_seen_at.date()` with `>=`, so a note whose `valid_from` is the day of the
  last report re-qualifies; at an hourly cadence the same note is sent 24 times, against
  `subscriptions.py`'s promise that "asking twice does not double-notify".
  **Fixed**, and both readings of the comparison are wrong: `>` would silently drop a note that
  appeared later the same day, which is the failure the feature exists to prevent. Migration 029
  lets the subscription remember which ids it sent *at the watermark's date*, which separates the
  two cases instead of choosing between them, and resets when the date rolls over.
- [x] ~~**DARK-8 [Low] — `embedding_dim` is cross-validated only when the `vector` source is on**~~, but
  `reindex_notes` writes the embedding column unconditionally, so a `lexical`-only deployment with a
  768-wide model passes config validation and fails every reindex on a pgvector dimension error.
  **Fixed.** The question the check asks is now "does anything here write `note_index`", which
  covers a `lexical`-only deployment and `note_reindex_enabled` — the scheduled rebuild, which
  needs no retrieve source at all. Still scoped rather than unconditional: the standalone embedder
  must stay free to pick any width.
- [x] ~~**DARK-9 [Low] — a reported measurement with no matching prediction is silently discarded**~~
  while the tool reports success. `_RECORD_OBSERVATION` is a bare `UPDATE` with no insert path;
  `record_observation`'s own docstring says the caller logs the zero-row case and the caller does
  not. This is the common case for new chemistry.
  **Fixed, and it was not [Low].** `predictions.predicted_value` is `NOT NULL`, so there was no row
  for an unpredicted molecule to attach to and the measurement was destroyed — while
  `report_measurement` answered "Recorded". Migration 030 keeps the measurement on its own, and a
  later prediction of the same thing reconciles against it on write, so measure-then-predict works
  as well as the reverse.
- [x] **DARK-10 [Low] — the PR-gate's checkout window exposes unreviewed notes to readers** —
      **already closed and the row was stale.** `kg/git_submitter.py` submits in a private
      `git worktree` under `.git/` and never switches the tree readers resolve, which landed
      2026-08-05 in D-2026-08-05-three-searches-that-disagreed-about-one-note. Confirmed against
      the tree rather than re-implemented (D-2026-08-06-a-wikilink-is-an-edge-not-a-word).
  `knowledge_path` is the same tree the submitter runs `checkout -B note/<id>` against, so a
  concurrent turn can retrieve an agent-proposed, unreviewed note as authoritative evidence. The
  remaining window is transient and spans a commit, a fetch and a push.
  **This row previously claimed `_return_to_base` had fixed the permanent version. It had not** —
  `_return_to_base` was reachable only on the two success returns, so *every* failed push left the
  checkout on `note/<id>` permanently, and retries landed on the same branch without repairing it.
  A bare `checkout -B` also keeps untracked files, so a failure between `write_text` and `git add`
  leaked the note even once the restore was unconditional. Both are fixed and pinned by separate
  mutations (D-2026-08-01-a-gate-that-leaks-on-the-failure-path); the mid-window
  `invalidate_cache()` named above is gone, because it widened this very window and its stated
  justification died with the `try/finally`. Closing the transient window needs the submitter to
  work somewhere readers do not resolve — a second checkout, or a bare repo and a temporary index.
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
      **Done (D-137).** `PlanApprovalModeProvider` retracts the `mode_set` tool MAF injects, and the
      only path into execute mode is now the owner-scoped `POST /sessions/{id}/plan/decision`, bound
      to a hash of the plan the human was shown and recorded in `plan_approvals`.
- [x] **REV-2 [High] Nothing scrapes `/metrics`.** No ServiceMonitor, PodMonitor or scrape
      annotation anywhere under `deploy/`. Every metric in the system is uncollected in production.
      **Done (D-143).** A ServiceMonitor on the front-door Service, selecting it by the `http` port
      *name* so a port change cannot orphan the scrape. Front door only: workers and connectors
      record through `chemclaw.metrics_bridge`, whose contract is that a metric recorded outside the
      front door is a no-op, so a scrape pointed at them would collect nothing and report up.
      `additionalLabels` is left empty for the operator's `serviceMonitorSelector`, which is
      release-specific. A test asserts the scraped *path* is a route the app actually serves — the
      D-142 shape, since a ServiceMonitor naming `/metric` renders, validates, deploys and collects
      nothing forever.
- [x] **REV-3 [High] The two `expensive: true` CREST jobs heartbeat once** against a 600 s
      heartbeat timeout. `run_cached_ensemble`/`run_cached_interaction` have no `progress`
      parameter at all, so this is plumbing, not a kwarg. Each retry restarts CREST from zero
      (the store is written only on completion): ~50 min of saturated CPU to fail a job that would
      have succeeded. Third instance: `calc/reaction.py` at `level="thorough"`.
      **Done.** `_beating` in `connectors/calc/activities.py` awaits the CREST work on a timer
      derived from the heartbeat timeout. A timer, not a progress callback: a single subprocess has
      no unit boundary to report at, and "still running" is the honest signal.
- [x] **REV-4 [High] After-run compaction is a silent no-op under `session_store=postgres`** (the
      production default). MAF reads `session.state[source_id]["messages"]`, whose only writer is
      `InMemoryHistoryProvider`. So `session_messages` is read with no LIMIT every turn and a
      long-lived session re-reads its whole history before every model call. The docstring promises
      the opposite. **Confirmed by reading MAF, and the obvious fix is unsafe.**
      **Documented and pinned, not fixed (D-143).** Confirmed exactly as described. Two corrections
      to the framing: the `before_run` half *does* work under Postgres, so the model's input is
      still bounded and this is not a context-window bug — what is unbounded is the per-turn read
      and the forever-growing stored history. And **a `LIMIT` on the load would corrupt data**:
      `get_messages` repairs unmatched tool-call pairings by *writing back*, and over a windowed
      read a `tool_result` whose `tool_use` fell outside the window is indistinguishable from a real
      orphan, so the repair would strip and commit a pairing that was intact on disk. Both
      docstrings that promised the opposite are corrected, and
      `tests/test_durable_compaction_gap.py` pins the no-op *and* the write-back hazard.
      **Still open:** the real fix, which is either (a) make the read-repair in-memory-only when the
      load is partial, then bound the read, or (b) durable compaction that prunes whole tool-call
      groups from `session_messages`. Either is a design change to a durable path with a data-loss
      failure mode and wants its own ADR.
      **Done (D-151).** `save_messages` now runs the *same* strategy against the table after storing
      a turn — inline rather than on a schedule, because that is where MAF intends after-run
      compaction and the turn claim already guarantees one writer per session. Measured over 60
      turns: uncompacted the table grows by exactly 4 rows/turn to 240; compacted it sits in a band
      (14 → 23 → 22 → 18) bounded by the window, not the turn count. Off by default, matching
      `retention_enabled`. `get_messages` is untouched — no `LIMIT` — because compaction never reads
      a partial history and so sidesteps the corruption class rather than accepting it.

- [x] **REV-5 [High] `retrieval_recall`/`retrieval_precision` are absent from `evals/baseline.json`**,
      so the only metrics that run a live retriever have zero drift coverage — verified by
      collapsing both to 0.0 and getting no alert. Also give `save_baseline` a Makefile target; it
      has no caller today, which is how the two metrics drifted out.
      **Done.** Both metrics are in `baseline.json`, regenerated by `scripts/refresh_baseline.py`
      (`make eval-baseline`) rather than hand-edited — `save_baseline` had no caller, which is how
      they drifted out. A test asserts both are present and that a collapsed score now alerts.
- [x] **REV-6 [Med] `open_reachable`'s unreachable-connector list is discarded by all four
      callers**, though its docstring says it is "for the caller to surface". A turn answers with a
      silently degraded capability set; in `template_activities` the output enters the PR-gate with
      no marker.
      **Done (D-139).** The announcement moved *into* `open_reachable` — a WARNING naming the
      connectors plus `chemclaw_connectors_unreachable_total`, counted per connector — because a
      return value that must be read had been forgotten four times out of four. The front door
      additionally yields a `CapabilityDegradedEvent` before the first token; the CLI prints to
      stderr, which its docstring had promised and never done. Still degrades rather than raising:
      one dark connector must not become a dead front door.
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
      **Partly done (D-153).** The *second* defect in this area is fixed: `await_job_results` tailed
      the mailbox, whose claim is destructive, so a mid-turn resume waiting on job A consumed job
      B's push-back and discarded it — the front door's stream never saw it. It now asks Temporal
      about its own job ids and never touches the mailbox, so there is no shared queue to race over.
      Also strictly more informative: the model resumes with the `ConnectorJobResult` envelope
      rather than the one-line summary the event payload carried.
      **Still open — and the cheap fix is refuted.** "Select, yield, then confirm" does not work:
      `stream_new_events` polls on a timer with no `try/finally`, so an event yielded but
      unconfirmed is re-selected every poll (`test_tailer_releases_its_connection_between_polls`
      would see ~37 deliveries instead of 1). Preventing re-selection *is* a visibility timeout. The
      fix stays as recorded above, and additionally needs a **per-stream** holder id (`_WORKER_ID`
      is per-process, so two streams in one pod would steal each other's leases) and a confirm
      shielded against cancellation (D-130's trap — the confirm is reached from a cancelled
      generator). It is an operator-facing contract change too.

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
      **Done by main (D-130)** — turn teardown is shielded so its cleanup runs in a cancelled task.
      That is the root cause this review identified: the release was an await in a closing generator
      and the `RuntimeError` was swallowed.
- [ ] **REV-9 [Med] Prompt caching: a large fixed prefix is re-paid every model call** — but
      **measure before building** (D-152), and this entry as first written overstated how reachable
      the saving is. Two corrections from verifying it:
      **(a) the ~14.6 k prefix was measured on the wrong provider.** That figure came from the
      Anthropic dev path. Production is `openai_compatible`, where `agent_framework_openai` contains
      **zero** occurrences of `cache_control` — the mechanism is not reachable from production at
      all, so this is upstream work in MAF, not a knob here.
      **(b) "the ~3.5 k system half is cacheable" is false through `Agent`.** `SkillsProvider`
      merges
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
      **Done (D-144), the pricing half.** Four counters for the four priced dimensions, with
      `chemclaw_tokens_total` kept as the total. The budget guard still meters the total, so the 429
      behaviour is unchanged — this splits what is published, not what is enforced. Cache counts are
      *not* folded into `input` (a provider reporting them has already excluded them, so folding
      would re-price cheap tokens as expensive), and a counter stays untouched rather than
      publishing a fabricated `0` when the provider reports nothing — the REV-19 rule.
      **Done (D-152), the attribution half — and half of it turned out to be already solved.**
      Per-*model* attribution needs nothing built: MAF emits `gen_ai.client.token.usage` labelled by
      request model, response model, provider and token type, and the shipped chart turns OTel on.
      Duplicating that axis in this registry would mean two systems to reconcile, so it is
      deliberately not done — with two gaps recorded: MAF records only the `input`/`output` token
      types, so D-144's cache-read/cache-write dimensions are *not* in that histogram, and OTel has
      no notion of a Chemclaw `profile`. Per-*profile* attribution is the real gap and is what
      shipped: the registry gained declared labels (an undeclared label name raises exactly as an
      undeclared metric does, because a label typo's failure mode is a second silent time series
      rather than a crash), a per-counter series cap against the unbounded-map leak this codebase
      has already fixed three times, and the five spend counters carry `profile`. `/metrics` is
      unauthenticated, so `test_metrics_carry_no_identifiers_or_turn_content` became an allowlist of
      *declared* label names rather than "`le` is the only label": a profile is configuration, low-
      cardinality, and not user-derived.
- [x] **REV-11 [Med] `correlation_id` stops at the process boundary.** Not in the connector
      identity headers, not in `ConnectorJobInput`, not into HPC. ~4 lines to make the audit trail
      joinable across all four runtimes. Note that fixing OTel does not fix this.
      **Done (D-141).** An `X-Chemclaw-Correlation-Id` header beside the actor/roles/session, and a
      `correlation_id` on `ConnectorJobInput` that becomes a workflow memo beside `requested_by`.
      Both follow the shape already established for the actor: advisory-never-authorization for the
      header, in the input rather than ambient for the job (a workflow has no request context), and
      a memo rather than `payload` so it is not something the model can write. HPC is unchanged —
      the bridge runs under a shared service identity and wants its own pass.
- [x] **REV-12 [Med] Prediction calibration pools every calculator version.** `calc_version` is
      never passed when recording, so the unique index degenerates and a v2 prediction destroys
      v1's row; the read path has no version predicate either. `calculator_trust` reports the
      pooled figure. Dormant while `calibration_enabled` is off.
      **Done (D-139).** Both halves — the tools pass the running version, and `calibration_for` now
      *requires* one and filters on it. The observation write stays version-blind on purpose: a
      measurement is a fact about the molecule, which is what makes a version-over-version
      comparison possible. Verified against live Postgres by simulating the pooled read, where a
      high version and a low one cancel to a bias of exactly 0.0.
- [x] **REV-13 [Med] `find_job` does filesystem I/O inside workflow code**, and the comment above
      it says it is I/O-free. `ConnectorError` is a `ValueError`, not a `FailureError`, and no
      `failure_exception_types` is declared — so it fails the *workflow task* and Temporal retries
      indefinitely. The run hangs rather than failing. No test constructs a `JobStep`.
      **Done (D-140).** The lookup moved to a local activity, `resolve_job_step`, following
      `orchestrator.resolve_fan_out_limit`'s precedent — the resolution is now recorded in history
      rather than re-read from the replaying worker's disk. That also turns the `ConnectorError`
      into an `ActivityError`, which `BAD_DATA_RETRY` fails on the first attempt. `TemplateWorkflow`
      gains `failure_exception_types=[Exception]` for the sequencer's own raw raises.
      `tests/test_template_job_step.py` is the first test to construct a `JobStep`.
- [x] **REV-14 [Med] Rehydrated and LRU-evicted sessions revert to the default profile**,
      permanently. The profile is never persisted. Eviction matters more than restart: no TTL, so
      session 1001 evicts session 1. All three rehydration tests discard the profile argument.
      **Done (D-141).** Persisted as a nullable column on `session_owners` (`infra/sql/021`) and
      rehydrated onto. The old comment called the loss graceful — "the conversation resumes with the
      full tool surface rather than a narrowed one" — which has the direction backwards: a profile
      is attenuation only, so restoring the full surface is the control being switched off, and the
      LRU has no TTL so it happens on a live pod without any restart. `None` surviving as `None` is
      pinned separately; storing `""` would ask for a profile named empty-string.
- [x] **REV-15 [Med] Chart parity test proves nothing about behaviour.** It constructs
      `Settings(**helm_values)`; `otel_enabled=True` constructs perfectly and then kills the pod.
      Two holes: keys from `templates/config.yaml` (`note_repo_dir`, `connector_urls`) are outside
      it, and there is no inverse test that a production value is *executed*. This is the test
      class that would have caught two of the three Criticals.
      **Done (D-142).** The derived keys are discovered from `templates/config.yaml` and rendered
      offline, and `connector_urls` is now *asserted*, not merely constructed — a render of `{}`
      builds a valid `Settings` while pointing the front door at nothing. Writing it surfaced the
      sharper point: pydantic-settings JSON-decodes a complex field from an env var and **not** from
      an init kwarg, so the old model of "the pod environment" was the wrong mechanism for these
      keys, not just incomplete. The inverse direction now has tests too (below).
- [x] **REV-16 [Med] Dark-by-default flags that arguably should not be.** `budget_enabled` off
      (the load test that validated the system ran with budgets *on*); `audit_verify_enabled` off,
      so the tamper-evident chain is never verified; `connectors_required` off.
      **Done (D-142), two of three.** `budget_enabled` and `audit_verify_enabled` are on in the
      chart, each pinned by an *executed* test rather than by asserting the flag.
      `connectors_required` deliberately stays false: unlike the other two its docstring is a real
      considered trade, and the review's argument for flipping it — that the degradation was silent
      — stopped being true when D-139 landed `CapabilityDegradedEvent`, the WARNING and the counter.
      Fail-fast would now trade availability away for visibility that already exists.
- [x] **REV-17 [Med] `deployment_revision` can never be set in production** — no chart key,
      Containerfile ARG or build step sets it, though its docstring says the image build injects
      the digest. AG-14 is unmet while reading as done.
      **Done (D-140).** A `CHEMCLAW_REVISION` build ARG exported as `CHEMCLAW_DEPLOYMENT_REVISION`,
      with the image workflow passing the commit SHA — a build arg rather than a chart value because
      the image is the thing that has a revision, and one that disagrees with the running bytes is
      worse than an honest "unknown". The wiring is pinned offline in `test_deploy_chart.py`; the
      image job runs the built image and compares, because only that proves the value arrived.
- [x] **REV-18 [Low] Missing validators** for combinations the config comments already forbid in
      prose: `session_store="memory"` with `uvicorn_workers > 1`, `mid_turn_resume_timeout >=
      turn_timeout`, `budget_enabled` with all caps zero, `embedding_dim` vs the `vector(N)` column.
      **Done.** Four validators on the composed `Settings`. The `embedding_dim` check is scoped to
      `"vector" in data_sources`: unconditional, it rejected three hash-embedder unit tests that
      never touch pgvector — the tests were right and the validator was wrong.
- [x] **REV-19 [Low] `chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` are never
      incremented** — a permanent `0`. The gauge path refuses to fabricate zeros; counters get no
      such protection. Increment them or delete them.
      **Done (D-139).** Incremented at the durable-job launch and the PR-gate proposal. The note
      counter moves *after* the submitter returns: counting the attempt would report a healthy gate
      during exactly the outage the metric exists to reveal. `agents/audit.py`'s private
      `_record_metric` was promoted to `chemclaw/metrics_bridge.py` at its fourth caller rather than
      imported by its underscore name.
- [x] **REV-20 [Low] Anthropic client ignores `llm_timeout_seconds`/`llm_max_retries`/CA bundle.**
      Actual timeout is the SDK's 600 s, not the configured 60 s. Default for CLI and dev.
      **Done.** `AsyncAnthropic` now carries `llm_timeout_seconds`, `llm_max_retries` and the CA
      bundle. Verified against the live API.
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

      **Done.** `harness-konzept.md` says 25 and "both modes"; `template_job.py` no longer calls its
      lookup I/O-free; `chemclaw_agent.py` names what enforces the gate now that D-137 makes the
      claim true.
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
- [x] **STO-10 [Med] `knowledge/` was empty.** **Done (D-135):** 38 seed notes covering all eleven
      types and all fifteen relations, with real instances of the awkward cases (a superseded
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
      *resource*, which is a worse trade than tuning the per-process number (SCALE-3). That product
      is now declared, checked at startup and exported as a gauge
      (D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down) — the trade above is
      unchanged, but the number it produces is no longer written down nowhere.
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
- [x] **Migration rollback is unaddressed** — closed by D-2026-08-04-the-schema-only-goes-forward,
      which took the second of the two options this row offered. Forward-only *is* the policy, and
      the reasons a tested down-path is worse here are stated: a scripted `DROP COLUMN` against the
      hash-chained `audit_events` is the operation that control exists to prevent, an additive
      schema makes "deploy the previous image" a complete revert already, and an inverse nobody
      ever runs is a second schema definition that drifts. `tests/test_migrations_are_additive.py`
      enforces it per file. (Found still open by the 2026-08-05 database review, a day after the
      ADR that answers it.)

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
- [x] **F7 (the first live connector)**: custom Snowflake ELN source. Done
      (D-2026-08-04-the-schema-is-a-file), except the tenant itself. It landed one step further than
      this row asked for: the Snowflake specifics do not live "inside that one adapter", they live
      in the manifest's `binding:` block, because a schema nobody can see yet cannot be written into
      Python. `chemclaw.ingest.eln.warehouse` is a generic engine naming no table and no column;
      both halves ship (ingest through the PR-gate, plus similarity search run inside the warehouse
      over its own embedding column), proven against a fake driver with no tenant. Remaining work is
      infrastructure only — see `docs/planning/DEFERRED.md`.
- [ ] **The other F7 adapters**: LIMS/MES/analytical/literature. Each is now a question of whether
      it is reaction-shaped: one that is becomes a binding over the same engine, one that is not is
      the trigger for the "universal ingest abstraction" row in `docs/planning/DEFERRED.md`.

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
      **Blocked on user input** (which GNN + weights/license); the calculator contract makes the
      swap cheap.
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
