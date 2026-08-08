# Review & hardening campaign — 2026-08-08

15 audit agents over `src/chemclaw` (64k lines) plus 3 recon agents, each required to **measure rather
than argue**. A live Postgres 16 + pgvector 0.8.0 was built from source first, which unskipped 107
tests and made the DB-backed claims testable — most of the findings below could not have been proved
without it.

**~50 findings. 21 high-severity, the large majority reproduced by execution.** Nine of my prior leads
were *refuted* by measurement; those are recorded at the bottom so they are not re-opened.

Evidence for every item: `/tmp/claude-0/-home-user-Chemclaw3/19bd112e-beec-51d2-adff-7a9bfb21d523/scratchpad/findings_*.md`

### Gate state before any change (measured, not assumed)

`tests/test_pka.py` has **two pre-existing failures** on unchanged `origin/main` with a clean tree:
`test_predicted_pkah_ranks_aromatic_bases_correctly` and
`test_in_sample_pkah_errors_are_far_below_the_acid_calibrations`. They reproduce with the database
**unreachable** as well as live (2 failed, 362.94s), so they are not caused by standing up Postgres and
not caused by this campaign — they are an environment difference in the tblite numerics. Any coverage
or suite claim in this session must be stated as a delta against that baseline, never as "green".
The two are plausibly the same root cause the pKa-key finding names: an optimizer setting that moves
the number is absent from the cache key, so "which optimizer ran" is not pinned anywhere.

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

## Lane T6 — Index and ingest integrity

- [ ] **`note_index` keeps two models' vectors after a swap** — `retrieval/vector_index.py:329`.
      Proved on live pgvector: after the swap the exact-text match scores **0.0000** and is dropped by
      the `> 0` floor; production `search_dense` returned 1 of 4 notes. **The documented workaround is
      wrong** — BACKLOG names `make reindex`, the incremental target, which measured 0. Fix: mirror
      migration 038's `embedding_key` column.
- [ ] **`embedding_config_key` omits the endpoint** — `core/embeddings.py:58`. Fixes `document_chunks`
      too, one variable further out. (Both halves are existing BACKLOG rows — close them.)
- [ ] **Pushed note recorded FAILED, or not at all** — `kg/git_submitter.py:309`. A raising `finally`
      in cleanup replaces a successful return *after* the branch is on the remote: the reviewer queue
      shows 0 and `close_merged_notes` moves 0. With `CancelledError` (BaseException) there is **no
      durable row of any kind**.
- [ ] **`, note_id` tie-break disables the HNSW index entirely** — `retrieval/vector_index.py:207`.
      Isolated by EXPLAIN at N=20,000: bare ORDER BY → HNSW, 16.7 ms; with the tie-break → Seq Scan,
      250.2 ms. Removing only the tie-break: **11.7 ms, 25x**. Fix: tie-break in an outer query over
      the k rows.
- [ ] **A note whose filename stem ≠ its id is never indexed at all** — `vector_index.py:332`.
      `None != None` is False, so it is "unchanged" forever, and `full=True` does not help. Corpus is
      currently clean → latent. Add the check to `kg-validate`.
- [ ] **Warehouse sync wedges permanently** — `ingest/eln/warehouse/adapter.py:160`. LIMIT cuts on the
      watermark, the cursor advances on `created_at`. Once >`fetch_limit` rows are amended, new
      reactions are never ingested again and the wedge guard is never reached. The repo's own test fake
      ignores WHERE/ORDER/LIMIT, which is why no test sees it.
- [ ] **Chunking params are outside document identity** — `ingest/documents/sync.py:313`. Changing
      `chunk_chars` re-chunks nothing; when a re-chunk does happen it strands the old chunking's higher
      ordinals, which are then re-embedded and become indistinguishable.
- [ ] **Warehouse retriever embeds on the event loop** and lets provider errors escape into a
      `gather` without `return_exceptions` — `warehouse/retriever.py:109`.
- [ ] Migration 038's btree cannot serve its own `IS DISTINCT FROM` scan — measured Seq Scan at 1M rows.

## Lane T7 — API robustness — DONE (D-2026-08-08-a-slot-lives-as-long-as-its-response)

- [x] **Push-back stream slot leaks permanently on disconnect** — `api/routes/streams.py:95`. A
      never-started async generator runs no `finally`; 5 stalled connects → an honest 6th gets 429
      "close one and retry" with nothing open to close. Fixed by **response-scope release, not the
      expiring lease** `api/state.py:171` uses: a turn has a widest wall clock, but a push-back
      stream polls until the client leaves, so any deadline that clears a leak also evicts a healthy
      stream's accounting. See the ADR's *Alternatives rejected*.
- [x] **Token budget overshoots by the concurrent-request count** — `api/routes/turns.py:188`.
      Documented bound 8, measured 40. Re-checked after the admission permit; 10 concurrent POSTs
      against a 1-turn cap now yield 1 answer, not 10.
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

## Lane T8 — Science correctness

- [ ] **A truncated substructure scan renders as a genuine negative** — `molfp/search.py:165`. The
      verdict string asserts "this is a genuine negative result" while the cap silently hid matches.
      The module's own docstring says a caveat outside the payload has zero effect on the model.
- [ ] **pKa cache key does not name the program that ran** — `science/calc/pka.py:430`. Proved the
      un-keyed setting moves the number (5.400052 / 5.402952 / 5.335181) under a byte-identical key.
- [ ] **Peroxide-salt fix applied to one rule, not its twin** — `safety/rules.yaml:154`. Na2O2 + NaBH4
      raises no `oxidizer-with-reductant`; H2O2 + NaBH4 does.
- [ ] **Calibration outage is byte-identical to a clean ledger** — zero bias/MAE/RMSE read as "never
      measured". The write half of this exact argument was already fixed; the read half was not.
- [ ] **Campaign id forks on parameter and category ordering** — while constraint terms *are*
      canonicalized against precisely this failure.
- [ ] **Observation support accumulates evidence the corpus retracted** — the generated PR body
      contradicts itself in consecutive paragraphs and cites a documented success as evidence of failure.
- [ ] **`eval-strict` cannot see a science gate that stops firing** — raising two thresholds to 1000
      dropped a by-design failure and CI stayed green with `regressions=0`.

## Lane T9 — Make recurrence impossible (enforcement)

- [ ] **Third-party layering test** — `tests/test_layering.py:167` filters on `chemclaw.*`, so the
      policy every architecture document actually states is unenforced. A working 208-line prototype
      exists and flags the exact edges; land it with the allow-list.
- [ ] **`durable/launch.py`** — one `start_job()`; five copies of the launch idiom exist and one has
      already diverged (the `start_approval` bug above).
- [ ] **Pin `agent-framework-core<1.12`** and funnel the five private-module imports through one shim.
- [ ] **Default the statement timeout in `db.connection()`** — already prototyped: **23 files,
      +76/−261, mypy --strict clean, 874 tests green**. Closes the real risk that a new store silently
      gets no timeout.
- [ ] **Apply the jitter fix to its two stale copies** — `live_jobs.py`'s `% 25` yields 25 distinct
      temperatures that ever exist, so the lane goes permanently green while computing nothing.
- [ ] **A `degraded()` helper + the 22 warn-and-degrade sites** — 17 modules, none referencing METRICS.
- [ ] **Adopt `heartbeat.beating` in its three holdouts** — they use `timeout/3` with no floor vs
      `max(1.0, timeout/4)`.
- [ ] **`template-validate` should check step arguments**, not just names.
- [ ] **A test that every metric string literal is declared** — converts the one INVISIBLE swallow into
      a build-time failure.
- [ ] **`deps-audit` into `make ci` and ci.yml** — today no branch push audits dependencies.

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

## Lane T11 — Tests that do not constrain behaviour (proved by stub-survival)

Each item below was proved by mutating the implementation in a scratch clone and observing the tests
still pass — not by reading them.

- [ ] **Artifact eviction is tested only by grepping its SQL string** — `tests/test_artifact_eviction.py:46`.
      All 7 tests assert substrings of `_EVICT_IDLE`/`_EVICT_TO_FIT`; the one test that calls the job
      runs it with both triggers off, so it returns before opening a connection. **Four mutations each
      leave all 7 green**, including one that deletes every blob in `artifact_blobs` on the first sweep,
      and one that evicts the most expensive artifacts first — the exact failure the docstring says it
      prevents. Fix: DB-backed tests in the shape of `test_retention.py`, which does this correctly.
- [ ] **`compute_seconds` COALESCE in the upsert is unconstrained** — `tests/test_postgres_store.py:53`.
      Replacing it with `EXCLUDED.compute_seconds` survives 45 tests. Every re-put then NULLs the
      recorded cost, and eviction orders by it — so the most expensive calculations become the first
      evicted, turning D-011 cache hits into HPC re-runs.
- [ ] **`default_store()` may return an in-memory cache undetected** — stubbing it to `InMemoryStore()`
      survives 56 tests. This is the identical defect class the repo already found once and wrote
      `test_audit.py:210` for; its own artifact twin has the assertion. One line fixes it.
- [ ] **The disabled-budget no-op holds only if both guards do** — `tests/test_budget.py:30` asserts
      only that `check` did not raise. Deleting the guard in `record()` survives 77 tests and re-opens
      the memory leak `BoundedLru` exists to prevent.
- [ ] **The no-authenticated-user rejection is fully redundant** — `tests/test_authz.py:65`. The whole
      `if actor is None: raise` block in `authorize_trigger` can be deleted with the authz suite green,
      because the role check happens to also raise. `pytest.raises` carries no `match=`, so the two
      refusals are indistinguishable. Add `match=` to both.
- [ ] **The approval/user-input SSE path is never exercised** — every fake update in the repo hard-codes
      `user_input_requests=[]`, so stubbing `approval_prompt` to `None` survives; in production that is
      a mid-stream ValidationError at the exact moment a human approval was requested.
- [ ] **Chart tests grep Go-template source, never rendered YAML** — `_template_text()` reads raw
      `.yaml`/`.tpl`, so `kind: ServiceMonitor` inside a comment or a false `{{ if }}` satisfies them.
- [ ] `InMemoryStore.find`'s `created_at` sort can be deleted; only the insertion-order fallback is
      covered, so the two backends can disagree exactly where ordering was stated rather than implied.
- [ ] **Widen the 9-test property beachhead** to four crisp invariants: `render_note`→`parse_note`
      round-trip (pinned today by one example), `_build_submission` dedup permutation-invariance,
      budget monotonicity (the axis where `+=`→`=` survived a previous run), and in-memory vs Postgres
      `find` agreement.

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
