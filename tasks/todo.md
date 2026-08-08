# Review & hardening campaign — 2026-08-08

15 audit agents over `src/chemclaw` (64k lines) plus 3 recon agents, each required to **measure rather
than argue**. A live Postgres 16 + pgvector 0.8.0 was built from source first, which unskipped 107
tests and made the DB-backed claims testable — most of the findings below could not have been proved
without it.

**~50 findings. 21 high-severity, the large majority reproduced by execution.** Nine of my prior leads
were *refuted* by measurement; those are recorded at the bottom so they are not re-opened.

Evidence for every item: `/tmp/claude-0/-home-user-Chemclaw3/19bd112e-beec-51d2-adff-7a9bfb21d523/scratchpad/findings_*.md`

### Gate state before any change (measured, not assumed)

**This section was wrong for most of the campaign. Corrected 2026-08-08, with the measurement.**

It said `tests/test_pka.py` had **two pre-existing failures** on unchanged `origin/main`
(`test_predicted_pkah_ranks_aromatic_bases_correctly` and
`test_in_sample_pkah_errors_are_far_below_the_acid_calibrations`), attributed them to "an environment
difference in the tblite numerics", and suggested they shared a root cause with the pKa cache-key
finding. All three claims are false, and the error was mine: I read a red suite and never read the
failure text.

Both are `Failed: Timeout (>180.0s) from pytest-timeout`. **The assertions never run.** Nothing about
a pKa value is wrong, and the cache key cannot be the cause — neither test touches a store (T8
measured this independently). Decisive measurement, merged tree, same box, timeout lifted:

```
pytest tests/test_pka.py::…ranks_aromatic_bases_correctly \
       tests/test_pka.py::…errors_are_far_below_the_acid_calibrations --timeout=0
-> 2 passed in 1071.49s (0:17:51)
```

Seventeen minutes for two tests against a 180 s marker, on a box running four other agents. They are
timeout expiries under CPU contention, and they pass whenever given the time.

**The cost of getting this wrong:** I briefed six lane agents to ignore these as known-bad, so the
campaign ran for hours against a baseline that was not real, and one lane spent effort refuting a
claim I had made carelessly. The same failure class covers every other "environmental" red this
campaign wrote off — `test_bo_constraints.py`, `test_bo_predict.py` ×2, `test_reizman.py` — all
hard `@pytest.mark.timeout` markers, which **override `--timeout` on the command line** and so cannot
be relaxed for a contended run. It is item 10 of lane T11.

The standing rule survives the correction, for a better reason: any coverage or suite claim in this
session is a delta against a stated baseline, never a bare "green" — and a red test's *message* gets
read before it is characterised.

---

## Lane T1 — Secrets reach the log stream — DONE (b8ddd16, D-2026-08-08-redaction-must-outlive-the-formatter)

- [x] **`JsonFormatter` re-renders the traceback, undoing redaction** — `core/logging.py:468`.
      log_json=false → `key=*** dsn=***`; log_json=true → API key + DSN password verbatim. The chart
      sets JSON on, so **the leak exists only in production and is absent in the tests**. Falsifies
      D-2026-08-06-a-redactor-that-only-reads-the-message §1 ("including under a deployment's own
      formatter"). Fix: `record.exc_text or self.formatException(...)`; add `stack_info`; parametrize
      the existing redaction test over both formatters.
- [x] **Front door's uvicorn logs bypass the filter entirely** — `core/logging.py:61`.
      `uvicorn.error` has `propagate: false` and its own handler; it logs every unhandled ASGI
      exception with `exc_info`. `worker_http.py` and `connectors/server_entry.py` both pass
      `log_config=None` for this reason; `deploy/entrypoint.sh:40` — the one process holding user
      traffic, the LLM key and every DSN — does not.
- [x] **One bad connector manifest silently unredacts every bearer token** — `core/logging.py:371`.
      Degrades to `_connector_token_envs=()` for the process lifetime after a single boot WARNING.
      The leak and its trigger are correlated. Fix: refuse to boot, or ERROR + counter.
- [x] **`framing_envelope_secret` has no Secret slot and no redaction** — absent from the chart
      entirely, so its only home is `.Values.config` → a **plaintext ConfigMap** (`view` role reads it).
      It derives ENVELOPE_TAG, so leaking it defeats the prompt-injection mitigation it exists to make
      durable.
- [x] **`redact_secrets` only matches this process's own configured values** — ghp_/github_pat_/JWT/
      libpq `password=`/Azure/sk-proj-/sk-ant-/PEM/`?access_token=` all pass through. Add a small set
      of high-confidence structural patterns that cannot collide with a molecule id or note slug.

## Lane T2 — Connector surface is wider than declared — DONE (D-2026-08-08-a-served-tool-is-a-reachable-tool)

- [x] **Undeclared write tools served unauthenticated on `/mcp`** — `connectors/molfp/server/tools.py:64`
      and rxnfp. Proved by completing an anonymous MCP handshake and writing a row to
      `molecule_fingerprints`, the table that backs the report path — a route to citing attacker-chosen
      SMILES as lab precedent, around the PR-gate. The manifests' justification ("for the ingestion
      path") is false: the only writers call `FingerprintStore.add()` in-process.
      Fix: delete both wrappers; make `connector-validate` diff the **live** FastMCP tool set against
      the manifest so served and declared cannot drift again.
- [x] **`auth: mode: bearer` is send-only** — nothing server-side reads `Authorization`, and
      `connector-validate` raises no objection. Either implement the check or reject the mode.

## Lane T3 — Identity and authorization — DONE except the correlation-header item (D-2026-08-08-identity-must-travel-with-the-work)

- [x] **CLI `--admin` borrows skill roles into the authz gates** — `cli/chat.py:70`. Shipped config
      fails closed (36 allowed / 6 denied, 0/5 expensive). One skill-gate entry whose role name
      overlaps `entra_privileged_roles` → **42/42 allowed, all 5 expensive actions allowed**, on an
      unauthenticated terminal reachable by `oc exec`. Fix: dedicated `cli_admin_roles` (default empty).
- [x] **Durable-job note proposals recorded with no actor** — `durable/connector_job.py:334`.
      `set_current_identity` appears in exactly one file under `durable/`, so the chemist who requested
      the job cannot see the PR opened on their behalf (`list_proposals` scopes by oid; detail 404s).
- [x] **The report workflow carries no requester identity at all** — `ReportRequest` has no
      `requested_by`, unlike every other job input. A gated share contributes nothing and the draft
      reads as a complete sweep. **Corrects BACKLOG.md:17**, whose stated fix ("the workflow already
      carries it") is not applicable as written.
- [ ] **Template-step connector calls drop the audit join key** — `connectors/identity.py:118`.
      Correlation/session contextvars are set only in `api/runner.py`, so connector rows written from a
      template step have empty `session_id`/`correlation_id`.
- [x] `/approve` records `cli_admin_actor`, not the run's actor — `cli/chat.py:228`. GxP sign-off names
      someone who took no action.

## Lane T4 — Answers that look right when they are not — DONE except the budget-refusal half (D-2026-08-08-a-degraded-check-must-not-clear-the-gate)

- [x] **Judge outage raises confidence to 1.0** — `agent/verifier.py:249`. A cited-but-contradicted
      answer scores **1.0/supported** when the judge is down vs **0.0/unsupported** when it works, and
      `review_required` flips False. The docstring argues no flag is needed — that argument covers only
      the *uncited* branch. Fix: `verified_by` on the result, cap the deterministic gate below the
      review threshold when standing in, add `chemclaw_verifier_degraded_total`.
- [~] **Zero token metering silently disarms the budget guard** — `api/runner_usage.py:62`. Duck-typed
      on MAF keys; a rename meters 0 forever. Proved: 50 turns × 15,000 real tokens booked as 0 against
      a 1,000-token cap, and `check()` still allowed the next turn.
      **Instrumented, not fixed** — and the distinction matters, because a review caught me marking
      it done. A usage content that is present and unreadable now logs at ERROR, increments
      `chemclaw_usage_unreadable_total` and pages via `ChemclawUsageUnreadable`. The guard itself is
      unchanged: those turns still meter zero and are still admitted. Making the budget *refuse* on
      unreadable usage is the real fix and is a deliberate deferral — it would turn an upstream key
      rename into a full outage, which needs a decision about which failure a deployment prefers.
      *Trigger:* the first deployment that runs with a real cost ceiling it cannot afford to exceed.
- [x] **Temporal outage detected every turn, reported to no operator** — `api/runner.py:683`. The
      rationale comment ("open_reachable already counts it") is checkable and false — that counter is
      over connector tools. At the shipped INFO level: no log line, no metric change.
- [x] **Retrieved content can close the judge's `<evidence>` tag** — `agent/verifier.py:182`. Reuse
      `framing.frame_untrusted` so the delimiter is nonce'd, instead of a hand-rolled tag.
- [x] Verification crash yields an unscored answer that reads as unflagged — `api/runner_answer.py:60`.

## Lane T5 — Durable execution correctness — DONE except the digest mailbox (D-2026-08-08-an-outage-is-not-a-missing-job)

- [x] **Every Temporal `RPCError` reported as "no such job"** — `agent/durable_tools.py:322`, `:190`.
      Five status codes all produce HTTP 404 "no such job". An operator cancelling a runaway DFT job
      during a broker roll is told it does not exist. Fix: re-raise unless NOT_FOUND.
- [x] **`start_approval` omits `id_reuse_policy`** — `agent/interaction_tools.py:51`, the one of five
      copies without it. temporalio defaults to ALLOW_DUPLICATE, so a **decided** hold reopens as
      pending and a second click can flip a recorded GxP sign-off.
- [x] **A MAF string discriminator decides which DB rows get deleted** — `durable/retention.py:53`.
      Simulating a plausible `function_call` rename flips `droppable_rows` from `set()` to `{1}` —
      deleting a call row and stranding its result, which `message_pairing.py` calls "a bricked session
      with no self-heal path". Silent; breaks sessions days later.
- [ ] **Digest events land in a mailbox no consumer can claim** — `durable/digest.py:146`. The only
      consumer claims different kinds on a real session id, yet the watermark advances, **permanently**
      disqualifying the matched notes.
- [x] **Mid-turn resume silently drops failed jobs** — `agent/job_results.py:83`. `gather(...)`'s
      result is discarded, so the model finishes the turn narrating a success that did not happen. Two
      docstring sentences promise the opposite.
- [x] **Live settings read decides how many children a synthesis starts** — `durable/memory_jobs.py:138`.
      A mid-flight redeploy that lowers the cap wedges the run in a workflow-task retry loop.
      Same shape one level milder in `document_sync.py:210,230`.
- [x] **One junk anchor row disables tail-truncation detection** — `agent/audit_anchor.py:266`. No
      forgery needed; a truncated audit trail then verifies clean.

## Lane T6 — Index and ingest integrity — DONE (D-2026-08-08-a-derived-index-must-record-what-derived-it)

- [x] **`note_index` keeps two models' vectors after a swap** — migration **039** adds
      `note_index.embedding_key`; `fingerprints()` reports only rows made by the current
      configuration, so a superseded row has no fingerprint to match and the *incremental*
      `make reindex` re-embeds it. Pinned on live pgvector: model swap → 2 of 2 re-embedded, one
      distinct key in the table, next run 0.
- [x] **`embedding_config_key` omits the endpoint** — the key now names the endpoint for
      `openai_compatible` (`rstrip("/")`), empty slot for `hash`. Both BACKLOG rows closed.
- [x] **Pushed note recorded FAILED, or not at all** — `_release_worktree` swallows everything the
      cleanup raises, `CancelledError` included; three tests (error, cancelled, and the leftover the
      next sweep reclaims) fail against the old `finally` and pass now.
- [x] **`, note_id` tie-break disables the HNSW index entirely** — moved to an outer sort over the
      k rows. Re-measured, EXPLAIN ANALYZE at N=20,000, median of 5: shipped **243.05 ms** (Seq
      Scan) → **3.58 ms** (Index Scan + 10-row sort); ids identical to the no-tie-break form.
      Consequence measured and filed: the search is approximate again — recall@10 vs an exact scan
      is 1.0000 clustered, 0.116 uniform-random (new BACKLOG row for `hnsw.ef_search`).
- [x] **A note whose filename stem ≠ its id is never indexed at all** — absent now means unknown
      means embed it (with a WARNING naming the file), and `kg.validate` refuses the mismatch.
      Old logic reproduced first: indexed 1 of 2, and `full=True` also 1.
- [x] **Warehouse sync wedges permanently** — the cursor advances on `entry_window(...)`, and the
      future-timestamp guard checks the same value. `tests/warehouse_fake.WatermarkWarehouse`
      honours WHERE/ORDER BY/LIMIT; the new test fails against the old cursor.
- [x] **Chunking params are outside document identity** — migration **040** adds `chunking_key` to
      both `document_files` and `document_chunks` (two gates, both must see it), and `upsert`
      deletes every ordinal at or above the new count. Three tests, including the first test the
      Postgres backend has ever had.
- [x] **Warehouse retriever embeds on the event loop** — offloaded, plus an `except Exception`
      backstop. The same backstop went into `documents/retriever.py`, whose "never raises"
      docstring was untrue for the identical provider-error case.
- [ ] Migration 038's btree cannot serve its own `IS DISTINCT FROM` scan — **not fixed**, left as
      the existing BACKLOG row. The lesson was taken rather than the index: 039 adds no index for
      its own key column and says why. It costs write amplification and buys nothing; it corrupts
      nothing, which is why it ranked last.

## Lane T7 — API robustness — DONE (D-2026-08-08-a-slot-lives-as-long-as-its-response)

- [x] **Push-back stream slot leaks permanently on disconnect** — `api/routes/streams.py:95`. A
      never-started async generator runs no `finally`; 5 stalled connects → an honest 6th gets 429
      "close one and retry" with nothing open to close. Fixed by **response-scope release, not the
      expiring lease** `api/state.py:171` uses: a turn has a widest wall clock, but a push-back
      stream polls until the client leaves, so any deadline that clears a leak also evicts a healthy
      stream's accounting. See the ADR's *Alternatives rejected*.
- [x] **Token budget overshoots by the concurrent-request count** — `api/routes/turns.py:188`.
      Documented bound 8, measured 40. Re-checked after the admission permit, so the overshoot is
      now bounded by the permit count as documented: at the shipped `service_max_concurrent_turns`
      of 8, 40 concurrent POSTs against a 1-turn cap answered 40 before and 8 after. (An earlier
      write-up of this said "1 answer, not 10" — that is the *test's* one-permit setting, not the
      shipped default.)
- [x] **pypdf 6.14.2 → 6.15.0** (CVE-2026-71852, CVE-2026-71870). Re-measured in-process on a
      crafted 201 KB PDF: 33.83 s / 1948 MB RSS → 0.00 s / 36 MB / `LimitReachedError`.
      `make deps-audit` went from 2 findings to "No known vulnerabilities found".
- [x] **Parse attachments off the event loop** with a timeout — `parse_attachment_off_loop`, bounded
      by two new config fields, shedding with 503 rather than queueing (the default executor is where
      `api/auth` validates every token). *Refuted for `ingest/documents/crawl.py`: it opens no files;
      the share's parse at `sync.py:200` has always run under `asyncio.to_thread`. Its missing
      timeout is a throughput concern and is now a backlog row.*
- [x] Malformed webhook body is a 500, not a 422. Now 422 with `loc`/`msg` only — never `errors()`
      whole, which echoes the body back. The signature gate is untouched and still runs first.

## Lane T8 — Science correctness — DONE (D-2026-08-08-a-partial-answer-must-say-so)

- [x] **A truncated substructure scan reported a genuine negative** — 21-record store, cap 20, the
      sole azide at id 900 → `hits: []` and a verdict calling it "a genuine negative result".
      `FingerprintSearch` now carries `scan_truncated`/`hits_truncated` and the verdict reads
      `SEARCH INCOMPLETE` / `PARTIAL RESULT`; `_scan_for_matches` reports that it stopped early
      rather than the caller inferring it from `len == cap`.
- [x] **The pKa cache key did not name the method** — tblite and xtb produced a byte-identical key,
      and pyridine came back 5.400052 / 5.402952 / 5.335181 across gradient tolerances under one
      key. `relaxation_spec()` is now the single spec construction shared by `_relaxed_energy` and
      the key. **A second instance surfaced while fixing it**: `xtb_opt_trust_radius` was read
      inside the optimizer loop, so it was in no key at all — measured, 0.35 vs 0.05 relax ethanol
      to two different hashes. Now an `OptSpec` field.
- [x] **The peroxide pair rule missed inorganic peroxides** — `Na2O2 + NaBH4` raised `peroxide`
      only; it now also raises `oxidizer-with-reductant`. A carboxylate and a nitro group are
      pinned as still silent.
- [x] **A dead DSN and a disabled ledger produced identical calibration payloads** — and the
      disabled state is the *default*, so that was the shipped deployment. `reconciled_for` raises
      (its only callers are the two trust tools, so the swallow protected nothing) and
      `Calibration` gained a verdict `computed_field` with `None` figures.
- [x] **One campaign was three ids** — `[T,S]`, `[S,T]` and reversed categories. Both lists are
      sorted in the identity payload *only*: measured, parameter order gives byte-identical
      candidates, but category order moves the acquisition optimizer (2.1018 vs 2.0691), so the
      surrogate keeps the caller's order.
- [x] **A retracted observation kept its support** — proved on live Postgres: support stayed at 3
      with a self-contradictory PR body. The upsert now replaces; support falls 3→2 and the row
      leaves `promotable()`.
- [x] **`eval-strict` exited 0 on an inert suite** — thresholds at 1000 silently dropped
      `pharma-solvent-heavy`. `inert_demonstrations()` + `--strict` now exit 1 with a report line.

**Refuted — the two `tests/test_pka.py` failures this campaign treated as pre-existing on `main`
do not reproduce.** 27 passed on unaltered `main` sources (320 s) and 29 with the change (291 s).
The pKa key cannot have been the cause either: both tests call `predict_pka` directly and never
touch a store. The campaign's alternate hypothesis — a tblite-numerics environment difference — is
the one left standing.

## Lane T9 — Make recurrence impossible (enforcement) — MOSTLY DONE (D-2026-08-08-a-rule-with-no-test-is-a-claim)

- [x] **Third-party layering test** — `tests/test_third_party_layering.py`, keyed by *file* so a new
      module joining an existing leak fails. Mutation-proved four ways, and it caught a real fifth
      leak at merge time: `agent/job_results.py`, added by a parallel lane after the test was
      drafted. `test_layering.py`'s `TYPE_CHECKING` skip was also dead code guarding zero imports
      and is now a third checked scope.
- [ ] **`durable/launch.py`** — not built. A single shared reuse policy cannot serve all five
      callers (D-2026-08-08-an-outage-is-not-a-missing-job showed "closed with a decision" and
      "closed without one" need different ones). The five sites are recorded in `_KNOWN_LEAKS` so
      the debt cannot grow silently; BACKLOG carries the trigger.
- [ ] **Pin `agent-framework-core<1.12`** — deferred: `pyproject.toml` was another lane's file this
      wave. **Three of the five private imports turned out to be unnecessary**: measured against the
      installed 1.11.0, `todos_remaining`, the agent-mode trio and all five `_compaction` names are
      exported at the top level and are the identical objects, against a comment in
      `chemclaw_agent.py` asserting the opposite. Two genuinely-private imports remain.
- [ ] **Default the statement timeout in `db.connection()`** — held for the parent session
      (23 files; conflicts with three lanes' packages this wave).
- [x] **Apply the jitter fix to its two stale copies** — `tests/test_run_jitter.py` *evaluates* each
      expression over a 24 h window rather than reading its modulus: 25 and 971 distinct values
      before, 86,400 after. Pins the three files, so a fourth copy fails.
- [x] **A `degraded()` helper + the warn-and-degrade sites** — the finding was re-measured and was
      **worse than filed**: re-derived twice under a stated definition (one `ast.ExceptHandler`
      whose subtree logs a warning and does not re-raise) as **41 handlers across 34 modules, of
      which 4 counted anything** — filed as 22/17, and the lane's own first answer of 42/35/3 named
      the wrong three modules (`kg/proposal.py` logs and `return`s before its `record_metric`, while
      `api/routes/turns.py` and `api/state.py` do count and were not named). So "32 of 35 invisible"
      is really 30 of 34. Ten sites adopted, one labelled counter rather than ten counters; the
      remaining **29** are in BACKLOG with triggers (4 need `is_replaying()`, 1 is a CLI, 24 are
      other lanes').
- [x] **Adopt `heartbeat.beating` in its three holdouts** — and adoption exposed a defect *in the
      helper*: `asyncio.wait` does not cancel what it waits on, so a cancelled activity left its
      work running detached. Fixed. Two versions of that test passed against the unfixed helper
      before the third one didn't; both failures are recorded in its docstring.
- [x] **`template-validate` checks step arguments**, not just names.
- [x] **A test that every metric string literal is declared** — both directions, so a typo in a
      *loop-variable* metric name in `runner.py` is caught too.
- [x] **`deps-audit` into `make ci` and ci.yml** — red when written (2 CVEs), green now that T7's
      pypdf bump is merged. Verified in the merged tree: "No known vulnerabilities found".

## Lane T10 — Claims that are false

- [ ] **"Every result is persisted once — never recomputed"** (CLAUDE.md:94, ARCHITECTURE.md:17-18,
      unqualified). 8 concurrent `cached_compute` calls on one fresh key → **8 computes**. The gap is
      in DEFERRED; the two files CLAUDE.md tells you to internalize do not qualify it.
- [ ] **"the only place a finished job's result is collected"** — there are three; two produce
      different exception types for the same bad input, one of which reaches a chemist verbatim.
- [ ] **`infra/sql/README.md` Migration column wrong for 5 of 27 tables** — and
      `test_schema_inventory.py` compares table *names* only, so the column its own "an inventory
      nobody verifies" paragraph vouches for is the one part nothing verifies. Extend the test.
- [ ] **"--admin bypasses auth"** — it bypasses authentication, not authorization.
- [ ] **"No durable state lives here"** — the module defines durable identity; `_report_id` is
      order- and case-sensitive, so the advertised idempotency holds only for a byte-identical request
      from an LLM that reorders freely. Canonicalize it.
- [ ] **`known_documents` answers "any chunk", not "all chunks"** — both backends agree with each other
      and disagree with the docstring.
- [ ] Two `connectors/server.py` / `caller.py` docstrings claim a per-request guarantee that
      measurement disproves (identity is frozen at MCP handshake).

## Lane T11 — tests that do not constrain behaviour

Closed by `docs/decisions/D-2026-08-08-a-test-that-survives-the-mutation-it-names.md`. Every item
marked done was re-proved by re-applying the exact mutation that had survived, watching the suite
go red, then reverting it and watching it go green. The mutations are quoted verbatim in the ADR.

- [x] **Artifact eviction (`tests/test_artifact_eviction.py`) — 7 substring tests, 4 surviving
      mutations.** Kept all seven (indistinguishability was the defect, not redundancy) and added
      five live-Postgres tests that assert on *which blobs survive*. All four mutations now fail:
      `cumulative >= 0 AND %s IS NOT NULL` (deletes every blob), `) DESC,` → `) ASC,` (evicts the
      most expensive first), `last_access_at < now()` (idle window widened to everything), and
      `MAX(a.compute_seconds)` without the idle divisor. The fourth needed a second attempt — the
      first version of that test had the two ranking axes agreeing, so the tiebreaker rescued the
      mutant and it survived; it discriminates only when cost and idle time disagree.
      A fifth test pins that an evicted blob takes its `calculation_artifacts` link row (migration
      019's cascade) and leaves `calculation_results` alone.

- [x] **`compute_seconds` COALESCE — both of them.** `postgres_artifacts._UPSERT_LINK` (the one that
      turns a D-011 cache hit into a re-run: a nulled cost ranks the blob at the bottom of
      `_EVICT_TO_FIT`) and `postgres_store._UPSERT`. `compute_seconds = EXCLUDED.compute_seconds,`
      survived 42 and 47 tests respectively; both now fail.

- [x] **`default_store()` may be in-memory undetected.** One assertion mirroring
      `test_default_artifact_store_is_postgres_backed`. `return InMemoryStore()` now fails.

- [x] **Budget `record()` guard — 77 tests survived its deletion.** `test_disabled_is_a_no_op` now
      re-reads the disabled period through an enabled tracker, which is what a deployment does (the
      chart ships `budget_enabled: true`). Deleting `if not settings.budget_enabled: return` fails.

- [x] **`authorize_trigger` no-actor rejection was indistinguishable.** `match=` added to both
      `pytest.raises`; neither test removed. Deleting the `if actor is None:` block now fails
      `test_no_user_is_forbidden` with a regex mismatch naming the wrong refusal.

- [x] **The approval / user-input SSE path — the `ValidationError` is REFUTED, a different defect
      found.** `_Update.user_input_requests` in `tests/test_runner.py` became a property over
      `contents`, as MAF derives it; driven with a real `function_approval_request` the stream does
      not raise. It did render every approval as the bare `"Approval requested."`, because MAF puts
      the subject on a nested `function_call` and none of the attributes `approval_prompt` scanned
      are set. Fixed to `Approve calling <name>?`. The comment justifying the empty `approval_id`
      ("a plan prompt … answered by the next turn") was wrong and is corrected — plan approval is
      `agent/plan_gate.py` and never reaches this stream. Not done: unifying the other nineteen
      per-file fake updates (BACKLOG).

- [~] **Chart tests grep Go-template source, never rendered YAML.** Not fixable here: `helm` is
      absent and faking a renderer was explicitly ruled out. `tests/test_helm_chart.py`'s module
      docstring now states the limit in the first paragraph a reader reaches — including that CI's
      `make helm-validate` only schema-checks the render and never asserts on it. BACKLOG row with
      trigger "a `helm` binary in the job that runs pytest".

- [x] **`InMemoryStore.find` `created_at` sort — partly REFUTED, and it exposed a live crash.**
      Both sort *directions* are already killed by existing tests (`reverse=False` fails
      `test_an_empty_query_returns_everything_newest_first`; Postgres `DESC` → `ASC` fails
      `test_find_matches_the_in_memory_backend` via its `limit=1` query). Only *where an undated row
      lands* was unpinned. Writing that test found the sort key `s.created_at or datetime.max` is
      **naive** while every real `created_at` is aware, so one store holding one dated and one
      undated result raised `TypeError: can't compare offset-naive and offset-aware datetimes` —
      no order at all. Sentinel removed; undated rows are partitioned out and lead.

- [x] **Property beachhead widened, three of the four named invariants.** `render_note`→`parse_note`
      round-trip (which showed `kg/render.py`'s docstring states an equation that is not one:
      frontmatter strips the body, `read_text` normalises `\r` — docstring corrected, every
      frontmatter field round-trips exactly), `_build_submission` dedup, budget monotonicity. The
      fourth (in-memory vs Postgres `find` agreement) needs a database, which
      `tests/test_properties_core.py` refuses by design — BACKLOG.

- [x] **Hard `@pytest.mark.timeout` markers.** A marker overrides `--timeout` and `PYTEST_TIMEOUT`,
      so the tightest caps were the ones no command line could relax. `CHEMCLAW_TEST_TIMEOUT_SCALE`
      now multiplies **every** cap including the markers; `CHEMCLAW_TEST_TIMEOUT_SCALE=4 make test`
      is the supported answer on a loaded box. Raising the constants was rejected on the
      measurement (single-test runtime under contention was ~6x the cap, and that multiplier is a
      property of the machine); deleting them was rejected because a runaway xTB optimisation
      hanging CI is a real failure they catch. A `pytest_terminal_summary` hook now prints
      `timeouts — these assertions never ran` naming each node and the knob, which is the part that
      addresses the damage: two readers of this repository read `Failed: Timeout (>180.0s)` as a
      numerical failure. `tests/test_suite_timeouts.py` drives a real pytest session importing the
      real hook; both `_apply_timeout_scale(config, items)` and the `**kwargs` carry-forward (which
      keeps `method="thread"` on Temporal modules) fail when mutated. Documented in
      `tests/README.md`. CI does not set a scale — the gate runs in ~5 min on a dedicated runner.

## To BACKLOG with triggers (not fixed this session)

- Network policy: DNS egress `to: []` survives a correct config; default install permits all egress.
  Both are chart decisions with operator impact — they need a deployment owner's call, not a silent edit.
- Connector `/mcp` guarded only by self-asserted pod labels.
- xtb/crest tarballs installed with no sha256 (and the syft install script in CI).
- `CHEMCLAW_DATA_SOURCES_DIR` pointed at a member-writable path would hand over `module:callable`
  strings = arbitrary import. The trust unit is the whole manifest, not the `where:` clause.
- Per-key in-flight dedup in the calculation store (already a DEFERRED row; now measured).

## Refuted by measurement — do not re-open

- Duplicate `037` migration prefix is harmless: filename-keyed, deterministic, idempotent (tested with
  a third `037_*`).
- All eight hand-rolled `_connection()` helpers use the pool, and every call site carries a timeout.
- The nine Postgres stores share **structure, not code**: 23 shared 3-line blocks in ~1,500 normalized
  lines, nearly all the connection helper. A shared base would be an ORM re-implementation between
  three distinct documented correctness arguments.
- The four httpx factories share ~0 logic; only two touch a CA bundle and there is one such setting.
- `worker.py` repetition **is** the seam working — the imports are the mechanism.
- Dead-code sweep clean: 880 public functions and 336 config fields checked against 1,471 files; the
  2+1 candidates are all dynamic-dispatch false positives.
- `read_only` tool classification is honest today (all 18 tools instrumented for writes).
- Owner-scoping matrix clean; `_is_reviewer` fails closed; the host-bind check resists every encoding.
- Path containment in the git submitter resists traversal, symlinks, `--options` and unicode lookalikes.
- Temporal **activity authorization** is sound post-D-168 — every user-reachable path that launches
  expensive work reaches `prepare_job_launch`. The real gap there is identity, not authorization.
- Filtered-HNSW recall loss does not exist (recall@10 = 1.0000) — because the index was never used.
- The SQL trust boundary in `warehouse/sql.py` holds today; manifests are image-baked and unwritable.
- Solvent domination really is fixed (Tanimoto 1.0000), despite being the repo's cautionary tale.
- `nextflow.py`'s httpx client is properly closed on every path.
