# Task: implement every open backlog item that hardens the platform (2026-08-06)

Branch: `claude/platform-robustness-security-qliwby`.

**The brief.** Implement all open backlog elements that do *not* add a completely new feature but
make the platform more robust, secure or flexible.

**The filter, stated before the list so the exclusions are checkable.** `BACKLOG.md` carries **153**
open rows. A row is *in scope* here when closing it changes how the existing system behaves under
attack, failure or configuration — and *out of scope* when closing it means the system can answer a
question it cannot answer today. Three examples of the line: the `method` note type is a new
capability (out); deriving the write gate from the manifest's `state_changing` set is the same
capability with a control that follows its declaration (in); a `campaign_progress`-style tool would
be new (out) while making a durable job's idempotency key cover its own version is the job it
already has, done correctly (in).

That leaves **61 in-scope rows**, grouped below into **16 work packages**, each one PR with its own
ADR. Ordering is by blast radius, not by size: a forgeable actor header outranks a hand-rolled
connect helper even though the second is ten times the diff.

**What every package owes, from `CLAUDE.md`.** A repro before the fix (prose is evidence about its
author, never about the code), a test that fails on the unfixed tree, `make lint type test` green,
an ADR named `D-YYYY-MM-DD-<slug>.md` with its row in `docs/decisions/README.md`, and the closed
`BACKLOG.md` rows ticked in the same commit.

---

## Wave 1 — Security: the controls that can be forged or bypassed

### WP-1 · Attribution cannot rest on an unauthenticated header
*Backlog: "The unauthenticated `X-Chemclaw-Actor` header becomes durable GxP attribution" [M];
"`map_to_hpc_identity` has no caller" [L].*

- [x] `connectors/server.py:84` — a bundle activity stamps `CallerLogMiddleware`'s advisory headers
      into `bo_campaigns`/`bo_suggestions`, so anything that reaches the pod forges who ran an
      experiment. Attribute from the workflow payload's `requested_by` instead — the memo is set
      from the validated principal, and `connectors/qm/workflows.py` has read that memo since F5, so
      the seam exists and the fix is to use it rather than to build one.
- [x] Sweep every other write that reads an identity header, so this closes as a rule and not as one
      call site. **Superseded by a better rule**: the durable half was already fixed off the workflow
      memo, and the inline half has no memo — so the fix is the channel, not the call site. A
      connector now refuses a request without the fleet credential, and a deployment with neither a
      credential nor a loopback boundary refuses to start.
- [x] `agent/identity/hpc_bridge.py:18` — wired, in `submit_to_hpc` on both paths, with the mapping
      also landing on `HpcJobHandle.run_as` so it is in Temporal history rather than only a log line
      retention prunes. Its existing unit test is why it stayed unwired: it called the function
      itself, so it passed for as long as nothing else did.

**Acceptance**: met, and mutation-proven — removing the credential gate fails three tests, removing
the startup refusal fails two, and the ten tests pinning unchanged behaviour pass under both
mutations. Record: `D-2026-08-06-a-connector-that-authenticates-nobody`.

### WP-2 · The write gate follows the declaration
*Backlog: "The built-in write gate never consults the connector-declared `state_changing` set" [L].*

- [x] `agent/authz.py:74` — derived, **from a new narrower declaration rather than from
      `state_changing`**: deriving from that one was measured to newly gate 18 tools including
      `predict_pka` and `compute_xtb_energy`, closing the science of any `entra_required` deployment
      without a `tool_role_gates` entry. `endpoint.privileged` names the subset whose writes are
      shared across users.
- [x] `report_measurement` is the live miss — pinned as the regression case, alongside its inverse
      (the ordinary calculators stay open), so the plausible refactor back to `state_changing` fails.
- [x] `CORE_WRITE_TOOLS` is core's own only. `compute_dft_energy` left it — `qm`'s `expensive: true`
      already gates it against the identical predicate, so core stopped naming another bundle's tool.
      `index_*` stay, because they are absent from their manifests' agent-facing `tools` and so
      cannot be declared there.

**Acceptance**: met, with the target corrected — adding a **`privileged`** tool to any manifest gates
it with no Python edit, and the inverse is pinned too (a `state_changing` tool does *not* get gated,
because that would have closed 18 tools). Record:
`D-2026-08-06-a-write-gate-that-reads-the-wrong-declaration`.

### WP-3 · Connector authentication
*Backlog: "Every shipped connector is unauthenticated" [M]; "REV-3 connector server pods receive
`CHEMCLAW_TEMPORAL_TLS_*` but mount no TLS volume" [L].*

- [x] Shipped as one fleet credential named by `connector_token_env` rather than as seven manifest
      edits: the credential is a deployment fact, and `connector_urls` already established
      "manifest ships the dev default, deployment overrides". `none` stays reachable for the dev
      fleet, and is refused off loopback.
- [x] **Deliberately not enforced server-side, and the ADR argues it**: `allowed_tools` is the
      *agent's* subset and the ingestion path legitimately calls index tools outside it. One surface
      with two legitimate clients wants authenticating, not partitioning — which the credential does.
- [x] `entra_workload`/`entra_obo` stay unbuilt, recorded in the ADR as tenant-blocked.
- [ ] Fold in REV-3: include `chemclaw.tlsMount` on the connector pod spec, or state in the chart why
      the env vars are shared without it. No `helm` binary here, so this lands as a chart change
      pinned by `tests/test_helm_chart.py`, not by a render.

**Acceptance**: an unauthenticated call to a connector tool is refused (met). The second half is
**withdrawn with its reason**: a server-side `allowed_tools` check would break the ingestion path,
which legitimately calls index tools outside the agent's subset.

### WP-4 · Secrets are typed and complete
*Backlog: "Secrets are plain `str`, never rotated" [M]; "Workload identity federation is dead code
the docs lean on" [M]; "Egress is still port-scoped by default" [S].*

- [x] Six credential fields converted. **Measured first**: the redactor already covers the row's
      stated hazard (a repr in a log), and what it cannot cover is every non-log path — `repr`,
      `str`, `model_dump` and JSON each leaked. The conversion's own failure is silent (an f-string,
      an `Any` sink and an `lru_cache` callee each render the mask while type-checking), so an AST
      guard plus value-level assertions cover the shape `mypy` cannot.
- [x] **The three DSNs deliberately deferred to WP-10**: read directly in 26 modules, which is that
      package's own duplication — converting first writes 26 call sites to delete 25.
- [x] `hpcArtifactStoreToken` added as the eighth secret key, argued in the test beside the others.
- [x] Documents corrected — "wire it" was never available, since both consumers are tenant-blocked.
      `deploy/README.md`'s claim had the argument backwards: the plain secrets that exist are exactly
      the ones federation cannot supply. Label added to every connector pod, with a test asserting it
      on every pod spec that names a ServiceAccount.
- [~] **Narrowed, not closed, and the row stays open saying so.** Unrestricted egress is no longer
      inherited — an empty list now requires `allowEgressAnywhere` or the chart refuses to render.
      Narrowing itself needs the operator's CIDRs; deriving them from the chart's own addresses was
      rejected because the Postgres host is in a *secret*, so a derived policy would silently
      black-hole database traffic.

**Acceptance**: a settings repr contains no secret value (met, and asserted in four render shapes).
The second half is **met differently than written**: a chart with no `egressDestinations` still
permits arbitrary destinations, and now refuses to render unless the operator says so — because
narrowing needs addresses this chart must not invent. Record:
`D-2026-08-06-a-secret-that-masks-itself-when-you-forget`.

### WP-5 · Untrusted content that reaches the model unframed
*Backlog: the four framing rows [M/M/L/L].*

The deferral was deliberate and its reason is the design work: `frame_untrusted` wraps a prose
*string*, while each of these returns a structured model. This package makes that decision once.

- [x] **The decision was already made and unnoticed**: all five existing call sites frame a named
      free-text field of a structured result, so three of the four rows are that pattern applied
      once more. No allowlist helper — that would be an abstraction over a pattern of five explicit
      lines. Two treatments instead, chosen by *shape*: an envelope for a sentence, a charset
      reduction (`safe_identifier`) for an identifier.
- [x] `find_past_jobs` frames `rationale`; `summary` deliberately stays bare (our own output) and
      that half is pinned too.
- [x] **One tool, not a surface** — measured against what each bundle returns. `fetch_artifact` is
      framed in the connector; the rest are numbers, keys and names the bundle computed. A manifest
      declaration is the right answer at a *second* such tool, and that trigger is recorded.
- [x] `recall_observations` frames `Observation.statement`.
- [x] `gather_evidence` reduces `chunk.source` rather than framing it — a label loses nothing by
      being restricted to an identifier's charset, and the reduction removes the capability.

**Acceptance**: met, each **through its own tool** — the first version of two of these tests called
`frame_untrusted` directly and passed against the unframed tool, which is the defect family this
whole package is about, caught by running the mutation rather than by reading it.

**Beyond the four rows**, and the larger find: the envelope tag was per-process in every shipped
deployment, because the chart never set `framing_envelope_secret` while running postgres sessions
behind six replicas — so content framed by one pod was replayed by another as ordinary text.
`framing.py` also claimed a `Settings` warning that did not exist. Both fixed. Record:
`D-2026-08-06-a-mitigation-shipped-and-left-switched-off`.

### WP-6 · The two injection paths into stored knowledge
*Backlog: "ELN free text becomes real knowledge-graph edges" [M]; "A report note wikilinks non-note
evidence ids" [M]; "DARK-10 the PR-gate's checkout window exposes unreviewed notes" [Low].*

- [x] Escaped once at the composition point, which is sound because the mapper emits **no**
      wikilinks of its own — an invariant its docstring always claimed and a test now asserts, so a
      future link tells the escaping to move per-field. Escaped, not stripped: the reviewer is the
      control and must see the attempt.
- [x] Plain text for a non-note source, by the **reader's** rule: link a target exactly when
      `cited_ids` returns it unchanged. Two shipped retrievers (`warehouse`, `vendored_dataset`)
      return colon-bearing ids, so this fired on real reports — and the reader's rule is measurably
      more precise than "no colons", which would also refuse `[[:x]]` and `[[rel:]]`, dangling
      citations rather than forged relations.
- [x] **Already closed; the row was stale.** `kg/git_submitter.py` has submitted in a private
      `git worktree` since 2026-08-05
      (D-2026-08-05-three-searches-that-disagreed-about-one-note). Confirmed against the tree
      rather than re-implemented.

**Acceptance**: met on all three, each mutation-checked **at the call site** — the first version of
the report test called `_citation` directly and passed with the call site reverted, the third
occurrence of that shape this session. The forgery test is also parametrized over the five ELN
fields that *measurably* reach the body; the first version covered two that do not, and passed while
proving nothing. Record: `D-2026-08-06-a-wikilink-is-an-edge-not-a-word`.

### WP-7 · The warehouse's own trust boundary
*Backlog: "`vector.server_embed_function` reaches the SQL text unchecked" [L]; "A warehouse row key
is interpolated into a filesystem path" [L]; "`SnowflakeWarehouse._connect` classifies every client
error as retryable" [M] and executes no function body under test.*

- [x] `server_embed_function` now takes `_check_identifier`, the same rule every other identifier in
      a binding takes — a function name *is* a dotted identifier, so it needed no rule of its own.
      The `where:` clause stays raw: it announces itself as operator-written SQL, which is exactly
      what `server_embed_function` did not.
- [x] The slug rule moved to `note_relative_path`, the choke point the PR-gate and every reader
      share, and is exposed as `is_note_slug` for the retriever, which wants an answer rather than
      an exception (one hostile row must not fail a chemist's whole query).
- [x] `InterfaceError`/`ProgrammingError` → non-retryable `WarehouseQueryError`; the operational
      family keeps `ConnectionError`. `tests/test_warehouse_boundary.py` is the module's first
      executed coverage, driven through the `_client()` seam with a fake exposing the DB-API
      hierarchy.

**Acceptance**: met. **The traversal measurement was wrong twice and the correction is the finding.**
I measured with `Path.resolve()` — lexical — and wrote the escape into a comment, an ADR draft and
three docstrings. The code calls `is_file()`, and the OS will not walk `..` through a component that
does not exist. Re-measured on the primitive the code actually calls: the escape is real but
*conditional* on an intermediate directory existing, which is why the row is [L]. Two tests were
unfalsifiable on the way and mutation caught both. Record:
`D-2026-08-06-the-warehouse-boundary-and-a-probe-that-measured-the-wrong-call`.

### WP-8 · The audit trail records what was attempted
*Backlog: "AUDIT-2 a tool call rejected for bad arguments is neither audited nor
authorization-checked"; the `PostgresAuditSink.record` `statement_timeout=None` mutation survivor.*

- [x] A wrapper on the dispatcher records a `rejected` event for any call that reached no
      middleware, carrying the **raw** arguments (the validated form does not exist — that is what
      "rejected" means). Two corrections to the row: there is a **second** early return it does not
      name — a tool name in no map, which returns earlier still — and the discriminator is a
      `ContextVar` the middleware sets, not a match on MAF's refusal message, so a third upstream
      path is covered the day it appears rather than the day someone notices.
- [x] Killed as a **sweep**, not as one test: an AST guard asserts every `db.connection`/`pool`
      call in `src/` bounds its statements. 29 call sites had the same shape and the same absence
      of a test; four must run unbounded and are an allowlist with a reason per entry, plus a third
      test that fails when an entry stops being needed.

**Acceptance**: met. Mutation-proven in both directions — removing the recording fails six tests,
recording *unconditionally* fails three (a tool that raises did run, so the middleware owns that
row), and stripping the timeout kwarg fails the sweep. **The first version bound sink, actor and
correlation id at install time** like the middleware does; the patch is process-global and permanent
while an agent is not, so the first `build_agent` would have owned every later agent's rejections.
Only visible because the tests ran beside another agent's build. Record:
`D-2026-08-06-a-refusal-is-an-attempt-worth-recording`.

---

## Wave 2 — Robustness: failures the system currently mis-handles

### WP-9 · The turn's own failure paths
*Backlog: `run_turn` abandons the agent's `ResponseStream` [L]; the mid-turn resume drops
`user_input_requests` [L]; a failed durable job is dropped from the resume [L]; `beating()` abandons
the work it wraps when the activity is cancelled [L]; `evals.live`'s `failed_loudly` is
unconditionally true [M]; heal sessions bricked by a stranded `tool_result`; VIBE-1(a).*

- [ ] `api/runner.py:318` — the stream has no `aclose()` and no GC finalizer on any non-exhausting
      exit, so its cleanup hooks never run at all.
- [ ] `api/runner.py:780` — an approval prompt raised during a resume never reaches the stream.
- [ ] `agent/job_results.py:83` — a failed job is dropped from the mid-turn resume while the
      function's docstring says it is not. Fix the code, not the docstring.
- [ ] `durable/heartbeat.py:48` — calc's CREST runs and bo's surrogate fits keep burning CPU after
      `cancel_job`. Cancel the wrapped work with the beat.
- [ ] `evals/live.py:317` — the per-turn Temporal probe makes the harness's headline "failed
      silently" signal unable to fire. A metric that cannot report its own subject is worse than
      absent, which is this repo's most-recorded defect family.
- [ ] Heal sessions already bricked by a stranded `tool_result` (D-145 left this deliberately, to
      avoid masking a `droppable_rows` regression). It is a new destructive read-path behaviour, so
      it gets the ADR argument the row asks for: repair, count the repairs, and alert on a non-zero
      rate so the regression it would mask stays visible.
- [ ] VIBE-1(a): run the atom/charge balance check as a `JobSpec.precondition` before launch, so the
      actionable message reaches the model through `surface_domain_errors` and five pointless
      Temporal retries do not happen. VIBE-1(b) — relaying arbitrary workflow failure text — stays
      open as the policy decision it is.

**Acceptance**: each of the seven has a test that fails on the unfixed tree; `failed_loudly` fires on
a scripted silent failure.

### WP-10 · The store seam's divergences
*Backlog: the four store rows [M/L/L/L].*

The Q-A lane's measured verdict stands — the ten triads are *not* one abstraction waiting to be
extracted. This package takes only what it found: the plumbing, and the divergences it hid.

- [ ] `agent/turn_cost_store.py:60` — the `session_store_dsn` split means two stores use a database
      `make db-migrate` never touches. Either migrate it or refuse the split at startup.
- [ ] `science/calc/store.py:250` — `InMemoryStore.find` raises `TypeError` on a timezone-aware
      `created_at` where Postgres does not. Two backends of one Protocol disagreeing is the bug the
      Protocol exists to prevent.
- [ ] `science/calc/postgres_store.py:116` — only one of three jsonb writers rejects non-finite
      floats. A `NaN` in the other two is a row nobody can read back.
- [ ] `science/calc/postgres_store.py:74` — the connect helper is hand-rolled 14 times, five
      docstrings byte-identical and four claiming "one place, DRY". One helper; the DRY claim becomes
      true rather than merely written.

**Acceptance**: one round-trip test runs against both backends and passes identically, including the
tz-aware and non-finite cases.

### WP-11 · Durability: what a job is, and what survives it
*Backlog: DARK-4 [M]; a failed run leaves no record [M]; `request_development_report` writes no
record [S]; the SIGKILL 600 s coupling [M]; Temporal namespace retention [S]; eight tables retention
neither prunes nor refuses [M]; a pruned session keeps its listable identity [L]; REV-7 [M].*

- [ ] **DARK-4** — `job_workflow_id` hashes `[connector, job, payload]` only, so changing
      `xtb_method` correctly misses the calculation cache and then rejoins the *completed* prior run,
      returning numbers from the old method. `science/calc/store.py` takes the opposite and correct
      position (`calc_version` is in the key). Decide what "the version of a job" is — proposal: the
      bundle's declared job version plus the versions of the calculators it dispatches, derived, not
      hand-maintained.
- [ ] **A failed run leaves no record.** Three decisions the row names, taken in the ADR: the status a
      row carries (failed-after-rounds ≠ never-started), where the write happens (a workflow-level
      handler, since the failure is an exception rather than a return), and whether a later success
      supersedes the failed row or joins it — proposal: joins, because "what have we tried that did
      not work" is the question the table exists for.
- [ ] `request_development_report` does not run through `ConnectorJobWorkflow` (D-115 kept it in
      core), so lift the record write into a helper both call.
- [ ] **The 600 s heartbeat coupling.** One setting must be longer than a CREST search's slowest
      legitimate gap *and* is the only dead-worker signal, so every calc job pays the CREST-sized
      detection window. Per-job values: long for the two `expensive: true` searches, short for the
      rest. On OpenShift this is ten minutes of dead time per eviction.
- [ ] **Namespace retention** is unset, so a deployment inherits the server's default and the runbook
      cannot say how long history is kept. One Helm value plus a stated policy.
- [ ] **Retention's eight unlisted tables** — `session_owners`, `session_turns`, `turn_costs`,
      `predictions`, `measurements`, `note_proposals`, `plan_approvals`, `bo_suggestions`. A
      disposal decision each — pruned, or refused with its reason. Not a sweep that picks them up by
      default; "unlisted" must stop reading as neither decided nor deferred.
- [ ] **A pruned session keeps its listable identity** — the owner row outlives the last message, so
      "what was I working on" returns an empty conversation. Filter the listing on remaining history.
- [ ] **REV-7** — a push-back event lost between claim and delivery is lost permanently, and both
      cheap fixes are refuted in the row. Build the visibility-timeout redelivery: claim with a lease
      and a delivery deadline, confirm on delivery, re-offer on expiry — keeping COR-4's single-claim
      property. Needs a **per-stream** holder id (`_WORKER_ID` is per-process, so two streams in one
      pod steal each other's leases) and a confirm shielded against cancellation (D-130's trap). Own
      ADR; it is an operator-facing contract change.

**Acceptance**: a version bump recomputes rather than rejoining; a killed worker's job resumes inside
the per-job window; a lost delivery is re-offered; every table is either pruned or refused by name.

### WP-12 · Data correctness at the ingest and identity boundaries
*Backlog: one non-UTF-8 ORD export aborts the batch [M]; two ELN sources with one entry id collapse
[M]; a BO observation naming an undeclared parameter is silently dropped [S]; two compound-id
conventions [M]; mass balance is element-set subsumption only [M]; REV-2 solvate [M]; REV-4 hazard
rules [L]; pair rules have no notion of sequence [M]; `note_index` has no embedding-model identity
[M, first half only].*

- [ ] `ingest/eln/ord_adapter.py:110` — one bad file aborts the whole sync, contradicting the
      adapter's skip-and-continue contract.
- [ ] `ingest/eln/ingest.py:51` — two enabled sources sharing an entry id collapse onto one note and
      one fingerprint row, contradicting the manifest's per-source guarantee. Namespace the id by
      source, which the `eln-json:<entry_id>:<operator>` provenance string already half does.
- [ ] The durable BO path builds its own observations and can still reach the silent drop the tool
      boundary now rejects — a *fabrication* vector (a confident `predicted_value` from a decision
      space that discarded the parameter), not an error-handling one. Validate once, where both
      paths pass.
- [ ] **Two compound-id conventions in one graph** — the seed corpus names notes by slug, the machine
      path mints `compound-<hash>`. Not a dangling citation today; a duplicate-identity hazard on the
      ingest path. Rename the nine seed notes and their eight citers, and add the `kg-validate` rule
      (a compound note's id equals `compound_id(compound_smiles)`) that keeps it from recurring.
- [ ] **Mass balance** is element-set subsumption, so `benzene + methanol >> paracetamol` passes. The
      stronger check exists at `science/calc/reaction.py:178` and is not reused; add charge balance
      and the yield-vs-limiting-reagent check `amount_mmol` already supports.
- [ ] **REV-2** — `standard_smiles("CCN.C1CCOC1")` returns THF, because `FragmentParent` keeps the
      largest fragment. A solvate's identity is the solute, which is the opposite of "largest".
      Distinct mechanism from the counterion rule.
- [ ] **REV-4** — four hazard rules are narrow rather than wrong (sanitised ionic peroxide and
      n-halamine spellings, fully substituted hydrazines, 1,2-dichloroethane). Add *separate* rules,
      following the `non-carbon-azide` precedent — widening a cited rule on taste is how a table
      stops being citable.
- [ ] **Pair rules have no sequence**, so a quench reagent added at step 8 is screened against one
      consumed at step 1. A `same_step` scope in `rules.yaml`'s schema, not a matcher change.
- [ ] **`note_index` embedding identity** — changing `CHEMCLAW_EMBEDDING_MODEL` serves
      mixed-generation vectors until someone remembers `make reindex`, and nothing detects it, while
      the in-process embed cache *is* keyed on the model. Stamp the model on the index and refuse a
      mixed read. **Chunking is explicitly not in this package**: it changes what a retrieval hit
      *is* and moves every eval baseline, so it earns its own change.

**Acceptance**: each row has a failing-first test; `kg-validate` rejects a mis-named compound note.

---

## Wave 3 — Tests that cannot fail, and the gates that cannot see

### WP-13 · Kill the vacuous controls
*Backlog: the five "tests that cannot fail" rows; 22 of 56 `# noqa` suppress rules ruff never runs;
LIVE-6 test-to-table locality [S]; DA-7 test-to-module locality [S]; the two slowest pKa tests fail
under load [S]; Temporal tests skip where `temporal.download` is blocked [M].*

Each of the first five was proven by neutering the control and watching the test still pass, so each
fix is verified the same way — mutate, watch it fail.

- [ ] `tests/test_connector_isolation.py:85` — `name.split(".")[0] in ("calc",)` can never match a
      `chemclaw.science.calc.*` module, so the first-party half has always passed on an empty set.
- [ ] `tests/test_agent.py:279` — passes with the audit middleware removed.
- [ ] `cli/verify_audit_chain.py` has 0 % coverage and the "refuse to re-seal a broken chain" control
      lives nowhere else.
- [ ] Eight of eleven binding transforms in `warehouse/expr.py` are never executed, including the two
      the shipped `eln-snowflake` binding uses.
- [ ] `pyproject.toml:8` — 22 `# noqa` directives suppress rules ruff never runs, including 15
      `BLE001` markers reading as "this broad except was linted and accepted". Enable the rules or
      delete the comments; today they are a claim no gate checks. Recommendation: enable `BLE001`,
      fix or explicitly ignore each hit, delete the rest.
- [ ] **LIVE-6** — tests share one schema within a run, so ordering can couple them. Per-test schema
      or transactional rollback.
- [ ] **DA-7** — 3 of 5 mutations survived their own test file and died only under the full suite. A
      developer feedback-loop gap, not a correctness one; fix by locality, not by more tests.
- [ ] **The two slowest pKa tests under load** — reproduce with output captured (the observing run
      piped through `tail`, which is why the cause is unconfirmed), read the actual failure, then fix
      what it actually is rather than assuming the 180 s signal-based timeout. The operational rule
      (never run the gate while the box is loaded) stands until then.
- [ ] **Temporal tests** — all 13 modules fetch the *time-skipping* server, so a live broker cannot
      substitute. But `test_connector_job_workflow` and `test_workers` need only a real server and
      could take `WorkflowEnvironment.from_client()` against `settings.temporal_address` when it
      answers — turning a silent skip into a real run in exactly the environments that prove least
      today. Per-module judgement, which is why it was not guessed at inside the live lane.

**Acceptance**: every fixed test fails under the mutation that proved it vacuous; `ruff` runs the
rules the code claims it runs.

---

## Wave 4 — Flexibility: seams that price a change against core

### WP-14 · Declarations reach the model and the graph
*Backlog: PROSE-4 [S]; "Agent-authored notes cannot carry the provenance fields built for them" [S];
`Estimate` is a three-writer contract with four calculators outside it [S]; `conformal_uncertainty`
has no caller [S].*

- [ ] **PROSE-4** — `propose_knowledge_note`'s docstring lists note types with an ellipsis: a third
      copy of `KNOWN_NOTE_TYPES` kept in sync by nothing. Derive the model-facing description from
      the frozenset at registration. Left open because it changes how every tool's docstring reaches
      the model — which is exactly what makes it a seam rather than an edit.
- [ ] The same tool accepts only `id/type/body/compound_smiles/tags/source`, so the model cannot
      attach `calc_refs`, `artifact_refs`, typed `relations` or a validity window — every field
      D-133/D-134 built for it. A human reviews them at the gate, which is what makes this safe.
- [ ] `pka`, `logd`, `reaction` and `xtb_thermo` each carry an uncertainty under their own field name
      with no `method` and no domain answer, so a skill asking "how far do I trust this" gets four
      shapes and one. Convert them onto `Estimate`.
- [ ] `conformal_uncertainty` has no caller, so `calibration_conformal_coverage` and
      `calibration_conformal_min_samples` are configured and unread and every `method` in the system
      is `reported` or `none`. Wire it on the cached path, which is where the ledger read belongs.

**Acceptance**: adding a note type changes the model-facing description with no prose edit; all four
calculators answer the same trust question in one shape.

### WP-15 · The connector seam's remainders
*Backlog: the `report` job [S]; `mcp_servers/molfp|rxnfp` bodies [S]; concurrent-turn MCP lifecycle
guard [S]; SCALE-5 concurrent connects [S]; SCALE-1b attachments [M]; SCALE-4 rollback watermark
[S]; "There is no documented way to populate the fingerprint index" [S]; REV-5 pgvector ≥ 0.7 [L].*

- [ ] **The `report` job** — the last bespoke durable adapter that can move; mechanical after
      D-111/D-113 once its workflow returns the `ConnectorJobResult` envelope directly instead of
      being wrapped a third time. Its trigger is recorded as *now*.
- [ ] **`mcp_servers/molfp|rxnfp` bodies into their bundles** — cosmetic, and the recorded trigger is
      "the next substantive edit to either capability", which WP-12's fingerprint work is. Take it
      there or leave it; do not open a PR for it alone.
- [ ] **The MCP lifecycle guard** — a test asserting no MCP tool is attached to the process-lived
      agent makes D-109's fix structural instead of remembered.
- [ ] **SCALE-5** — connect the six connectors concurrently rather than sequentially. Measured at
      139–198 ms per turn (~0.6 % of p50), so this is the cheap isolation-preserving half only;
      pooling connections across turns stays rejected.
- [ ] **SCALE-1b** — attachments and harness todos live in one process's memory, so a chemist who
      uploads a CSV must reach the same pod and `service_uvicorn_workers` is pinned at 1 for any
      deployment using uploads or the harness. Making attachments durable is the fix.
- [ ] **SCALE-4** — have `save_messages` remember the ids it inserted, removing the pre-turn read;
      needs per-turn state that does not collide on a pod-shared history provider.
- [ ] **The fingerprint backfill** — `make reindex` is note-index-only and the tables fill as a side
      effect of the ELN sync, which is how a live run reached 1,025 notes and 0 fingerprints. A
      `make` target plus the runbook section.
- [ ] **REV-5** — the migrations use `bit_jaccard_ops`, so a database from `apt` (pgvector 0.6.0)
      fails to migrate. A `deploy/README.md` note, not a code change.

**Acceptance**: the report job runs through the connector seam unchanged; an operator can populate
the fingerprint index from the runbook alone.

---

## Wave 5 — Documents that are gates

### WP-16 · Prose that asserts what the code does not do
*Backlog: `NoAuth`'s docstring [L]; `connectors/calc/activities.py`'s module docstring [L]; F9-T1
`architektur.md` §6 [S]; `docs/planning/` fails the widened prose rules 175 times [M]; the ICH Q3C
revision label [XS].*

- [ ] `connectors/manifest.py:60` — `NoAuth`'s docstring asserts a manifest validator that does not
      exist. Either write the validator (WP-3 gives it a reason to exist) or correct the sentence.
- [ ] `connectors/calc/activities.py:23` — the module docstring denies the registration mechanism the
      file uses two lines later and cites a queue count D-118 removed.
- [ ] **F9-T1** — `architektur.md` §6 still describes Azure AI Foundry / Container Apps / raw SLURM.
      `CLAUDE.md`'s "historical, not current" warning is a workaround for the rewrite, not a
      substitute: §6 is the first thing a newcomer reads. Rewrite §6 to name OpenShift,
      Nextflow-on-HPC and the internal LLM adapter; keep §7/§8.
- [ ] **The 175 planning-doc failures** — deliberately outside the gate, and they stay outside it
      until the sentences are reworded one judgement at a time (a ticket saying "create
      `agents/qm_tools.py`" names a file D-118 deleted; there is no path to correct it *to*). Do the
      rewording for `BACKLOG.md` and `implementation-tickets.md`, then add the glob to
      `_OPERATOR_DOC_GLOBS` and drop the pinned exclusion. If the rewording proves larger than one
      package, split it and say so rather than half-adding the glob.
- [ ] **ICH Q3C(R9)** — the label is unverified offline and sits on every Q3C citation; an
      adversarial review verified all 62 values and the Q3D label but not this one. Check it against
      the ICH site (this session has outbound HTTPS) and correct the single `guideline:` line. No
      figure changes.

**Acceptance**: `make prose-validate` covers `docs/planning/`; every corrected docstring is pinned by
the test that would catch its next drift.

---

## Deliberately out of scope, with the reason

**New capability, not hardening** — the `method` note type; X10 transition states; U1 xTB descriptors
as BO featurization; the GNN solubility model; a project entity; session delete/export/pagination;
external ontology anchoring; vendoring a third-party corpus; PR-gate notification and the git-host
adapter; a second step template; the `eval_drift` operator surface; the BO return-path matching rule;
document-level provenance share; per-step species linking; a per-requester job id; `Note.confidence`
producers; the retrospective campaign↔`bo_campaigns` join; VIBE-2's project vocabulary route;
`ask_clarifying_question` ending the turn; `AnswerEvent` narration; du-03's behavioural half;
`note_index` chunking; the retracted-ELN-entry reconciliation pass.

**Blocked on infrastructure this environment does not have** — the Entra-enforced pass, connector
`entra_workload`/`entra_obo`, the trivy image gate (needs a container runtime to inspect the built
image interactively), image signing and admission policy, Postgres/Temporal ownership in the chart,
SCALE-2's Prometheus adapter, SCALE-3's re-derived concurrency cap, node-hours from Tower, F8-T1b's
statistical applicability domain, the 230-probe live corpus run, live-retriever drift.

**Owner is not engineering** — shipping crest (GPL-3.0) is the product owner's licensing call, and
`--build-arg INCLUDE_CREST=false` already makes taking it a decision rather than a patch.

---

## Sequencing and cost

Waves are ordered by blast radius and by what unblocks what: WP-3 gives WP-16's `NoAuth` validator a
reason to exist; WP-12's fingerprint work carries WP-15's `mcp_servers` move; WP-10 lands before
WP-11 because the job record writes through the store seam it fixes.

Sixteen packages, sixteen PRs, sixteen ADRs. Five need their own recorded decision before code —
WP-5 (which fields are framed), WP-11's DARK-4 (what "the version of a job" is), WP-11's failed-run
record (status, write site, supersede-or-join), WP-11's REV-7 (an operator-facing contract change),
and WP-9's session healing (a new destructive read-path behaviour). Those five are where this plan is
most likely to be wrong, so each states its proposal above rather than deferring the decision to
implementation time.

## Review

*(Filled in as packages land — one line per package: what shipped, what the measurement said, and
what the row's own diagnosis got wrong.)*
