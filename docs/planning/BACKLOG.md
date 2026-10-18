# BACKLOG

What is still open, highest consequence first, then what is consciously deferred. Rules:

- A row is at most two lines and names an anchor (`path::symbol`, a make target or an ADR id).
- Delete a row in the commit that closes it — no strike-through, no status notes.
- A row is a claim about the code: check it against `HEAD` before working it, fix or delete it if wrong.
- Claiming a row: open a GitHub issue linking it and mark the row `(issue #NNN)`.
- Count with `grep -c '^- \[ \]' docs/planning/BACKLOG.md`, never in prose.
- A deferred item is one line: **what** — why not now · *Revisit:* the trigger.

Provenance for older rows: `docs/archive/findings-2026-08.md` and git history.

## Open

### Untrusted input and privileged surfaces

- [ ] **Worker workflow-cache bound rests on placeholders** [S] — sample cached state of `BoCampaignWorkflow` and calc workflows on a real broker;
  `core/config/temporal.py::worker_max_cached_workflows`, `tests/test_workers.py::test_the_workflow_cache_fits_the_memory_the_chart_asks_for`.
- [ ] **IPv4-mapped arm of the compiled egress guard is unmeasured** [S] — needs a host with IPv6;
  `tests/test_netguard_preload.py::test_an_ipv4_mapped_address_is_not_a_way_around_the_check`.
- [ ] **An HTTP result sink accepts `verify_tls: false` from its manifest** [S] — only a docstring forbids it and `make sink-validate` binds the signature, not the values;
  refuse it at construction (at least under `entra_required`); `publish/drivers/http.py::HttpResultSink`.
- [ ] **A third-party connector's failure text is credited to a sanitizer we do not run** [S] — bounded and defanged, but `answered_failure` says `connectors/server.py` sanitized it,
  which holds only for bundles this repo serves; frame or mark it, or correct the claim; `agent/tool_authz.py::answered_failure`.
- [ ] **Share-document chunks reach the model with their wikilinks intact** [S] — note excerpts and report cells strip links, share chunks do not, so a share author's `[[note-id]]`
  arrives verbatim inside the envelope; strip as `retrieval/retrievers.py::_excerpt` does; `ingest/documents/retriever.py::ShareDocumentRetriever._chunks`.

### Answers that are wrong without saying so

- [ ] **A salt dissolved in one organic solvent standardizes to the solvent** [S] — `[Li+].[AlH4-].C1CCOC1` and `[Na+].[H-].C1CCOC1` share THF's
  `compound_id`, `[Na+].[BH4-].CO` methanol's: ions that balance each other are stripped as the solvent's counterions; needs an owner decision on the strip rule, `core/chem.py::standardize`.
- [ ] **An external vector store cites cross-model neighbours after an embedding-model swap** [S] — its dense read binds no `embedding_key`, so stale vectors resolve and look correct
  until a reindex; keep only ids catalogued under the live key, notes and documents alike; `retrieval/external_note_index.py::ExternalVectorNoteIndex.search_dense`, `ingest/documents/external_index.py::ExternalVectorDocumentIndex._resolve`.
- [ ] **A reaction fingerprint whose record write has not landed is served at full similarity** [S] — the unfiltered path drops withheld records, not absent ones, so `gather_evidence`
  returns a hit `expand_note` refuses; treat an absent record as withheld; `retrieval/retrievers.py::FingerprintReactionRetriever.retrieve`, `ingest/eln/records.py::structurally_withheld`.
- [ ] **A report section is bounded by neither evidence budget** [S] — `gather_section` dedups but concatenates legs in order with no chunk or character cap; reuse the conversational
  merge and both `gather_evidence_max_*` caps, and render the cut; `retrieval/harness.py::gather_section`, `agent/research_tools.py::_interleave_dedup`.
- [ ] **A declared tool the server stopped advertising is dropped without a word** [S] — the plan gate, the skills backend and `skill-validate` still count it, so a skill is offered for
  a tool that cannot be called; log and count the gap per open; `connectors/transport.py::_allowed`, `connectors/session_pool.py::tools_for`.
- [ ] **`find_calculations` with `calc_type="xtb"` and a SMILES answers empty instead of refusing** [S] — no type is a bare `xtb`, so the structure-keyed refusal never fires; refuse a bare
  family name; the docstring's `xtb`/`dft`/`descriptors` examples change only behind a live eval; `science/calc/store.py::CalculationQuery._molecule_filter_addresses_the_type`.
- [ ] **`plan_gate.py`'s mutmut selection pairs one of its five covering test files** [S] — add the four siblings at the next floor
  re-measure (~80 min); `[tool.mutmut]` in `pyproject.toml`, `tests/test_mutation_workflow.py`.
- [ ] **Tool-utility results used a control arm that also swaps the prompt** [M] — re-run `make live-ab` / `make live-benchmark`
  with `data/evals/profiles/tools-removed.yaml` as baseline (needs gateway credit); `cli/live_probes.py::_AB_BASELINE_PROFILE`.
- [ ] **`hybrid` retrieval is worse than `graph`; fusion knobs are measured no-ops** [M] — measure an `openai_compatible` embedder
  with `make retrieval-arms` before quoting any number; `D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number`.

### Operating it

- [ ] **One bundle a deployment cannot use breaks every turn** [M] — a `stdio` bundle with stdio off raises out of `mcp_connections` unhandled while health calls it `unprobed`, and a
  malformed manifest nobody enabled raises out of `enabled()`; skip and report per bundle; `connectors/registry.py::mcp_connections`, `connectors/registry.py::_discovered_in`.
- [ ] **Two calc tools are told to wait past the hop that cancels them** [M] — `compute_atomic_descriptors`/`compute_surface_potential` pass `calc_atomic_timeout_seconds` to a backend
  behind a queued call bounded by `request_timeout`, so a long run is cut and stores nothing; clamp to the hop and assert it there; `connectors/calc/server/tools.py::compute_atomic_descriptors`.
- [ ] **Backend RPC is built but off: `calc_transport` defaults to `mcp`** [M] — flip after the in-cluster `make bench-backend-rpc` (cache-hit p50 ≤ 10 ms), a cluster run on `rpc` that proves no hop with a shorter idle or read timeout (mesh sidecar, load balancer, ingress at 60 s) cuts a long call, and a decision on `/rpc` having no `cancel_on_timeout` counterpart; then delete the MCP path for backends:
  `core/mcp_session.py` (`open_session`, `cancel_on_timeout`, `short_connect_client`, `invoke`), `connectors/calc/remote.py::_open_transport`, `ingest/labels/labeller.py::_open_transport`, `core/backend_rpc.py::BackendSession`, the `calc_transport` setting and `tests/test_backend_rpc_fleet.py`'s MCP arms.
- [ ] **A NaN or infinity is refused in a request but not in an answer over `/rpc`** [S] — `/rpc` returns such a float as null while MCP's text carries it; fleet contract decision (reject in the tool or declare null), then pin in `tests/test_backend_rpc_fleet.py`;
  `core/backend_rpc.py::RpcSession.invoke`, `tests/test_backend_rpc.py::test_the_client_passes_an_answer_through_as_the_server_serialized_it`.
- [ ] **A note written in one activity is unseen by a later one on another worker pod until the sync** [M] — each pod commits into its own
  clone; no shipped workflow reads its own write, so drive one first; `kg/git_writer.py`, `durable/template_activities.py::run_agent_step`.
- [ ] **The turn relay polls Postgres 8 times a second per held turn and per remote watcher** [L] — measured (two processes, defaults): idle holder 0.4 → 8.3 xact/s, +8.0 per remote watcher; remote attach 0.35 s vs 0.05 s local, remote Stop 0.32 s vs 0.07 s;
  a payload-free `LISTEN` wake-up before the same rows needs one session-mode connection per replica (`science/calc/flight.py::_Listener`, the `tests/test_db.py` allow-list, `D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`); then `api/turn_relay.py::TurnRelay.run`, `agent/session_queue.py` can poll rarely.
- [ ] **The drill's `kg-validate` still reads a notes directory, not the Postgres store** [S] — the `kg_*` tables are restored and counted, but their notes are not what is validated until W4.10;
  `src/chemclaw/cli/restore_drill.py::kg_checks`, `src/chemclaw/core/restore_check.py::load_knowledge_rows`.
- [ ] **A helper's report carries no "derived from untrusted reading" marking** [M] — first measure whether injected instructions
  propagate (`make live-delegation`); `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`.
- [ ] **2026-09-27 live-run fixes unverified against a real model** [S] — re-run pc-03, a capping delegation probe and a revised
  answer; `connectors/calc/compose.py::require_solvent_for_ions`, `api/runner.py::_REVISION_NOTE`, `spend/guard.py`.
- [ ] **Connectors dev server stalled 52 s under probe load** [S] — reproduce on an idle host before fixing; `cli/connectors_dev.py`.
- [ ] **Live-lane papercuts** [S] — `cli/live_storm.py` rejects `--families D,H`; `infra/live/processes.sh restart api` truncates the
  log; the lane leaves `CHEMCLAW_FRAMING_ENVELOPE_SECRET` unset so two processes frame differently.
- [ ] **Envelope-nonce prefix test hit its 180 s timeout under load** [S] — time it on an idle host before loosening;
  `tests/test_context_floor.py::test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce`.
- [ ] **The same three questions cost 2.1x more on one boot than another** [M] — record bound tool names and the schema gauge per
  boot (`make live-turn-cost`); a tool-binding race is the leading, unproven candidate.
- [ ] **A sink's refusal spends the whole attempt budget before dead-lettering** [S] — `_drain_one` books a `SinkRejectedError` like any refusal and `mark_failed` retires it only at the
  last attempt, against `publish/driver.py::SinkRejectedError`'s own contract; retire on refusal; `durable/publish_results.py::_drain_one`, `publish/outbox.py::mark_failed`.
- [ ] **`request_timeout` has no ceiling, and `calc` declares the turn deadline itself** [S] — which bound fires first is a race, so the recoverable transport error may never arrive;
  bound it below `service_turn_timeout_seconds`; `connectors/manifest.py::HttpEndpoint`, `connectors/registry.py::request_timeout_seconds`.
- [ ] **A rewritten artifact strands its old blob, and an evicted one leaves its row served** [M] — `_UPSERT_LINK` moves the link with no sweep (deleting in place raced an insert), and
  eviction never touches `calculation_results`, so `find`/`known`/backfill name a dead hash; `science/calc/postgres_artifacts.py::_UPSERT_LINK`, `durable/artifact_eviction.py::_evict_cold_artifacts`.
- [ ] **A site registry missing a property this release adds is found only by a refused fact row** [S] — the pre-write check asks only that `property_definition` is non-empty; compare it
  with the local registry first, and keep a failed column probe so a per-record replay does not re-probe; `publish/drivers/sql.py::SqlResultSink._known_columns`.
- [ ] **A payload with no projector is dropped on the live hooks with a debug line and no counter** [S] — in-repo shapes are pinned by `tests/test_publish_reaches_the_hooks.py`, a new
  bundle's are not; count it (the backfill's skip of retired calculators may stay quiet); `publish/outbox.py::enqueue_payload`.

### Where the field moved past us

- [ ] **Tool schemas are 72% description** [M] — whether shorter `Args:`/`Returns:` still route correctly is a `make live-ab`
  question; `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`.
- [ ] **Enumeration and scan tools rest on one probe each** [S] — add second phrasings for `enumerate_*`, `scan_coordinate`,
  `profile_rotation`, `optimize_geometry` in `data/evals/probes/`, ranked by `audit_events` once a deployment exists.
- [ ] **The default prompt names `find_past_jobs` but never `find_calculations`** [S] — the molecule-level "already computed" read is named only in the `evidence`/`computation` profiles;
  add it to the research-loop block behind a `make live-ab` arm; `agent/chemclaw_agent.py::_INSTRUCTION_BLOCKS`.
- [ ] **`fetch_artifact` refuses every artifact this release stores and is still bound** [S] — kept only for older notes' `artifact_refs`; unbind it from the `computation`/`evidence`
  profiles, or bind it only when a text artifact exists, behind a live eval; `connectors/calc/server/tools.py::fetch_artifact`.
- [ ] **A chat-room connector over a shared session** [M] — prerequisites shipped
  (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`); left: the connector and the queue/stream UI in `Chemclaw3_ui`.
- [ ] **A routing corpus where the right profile is not inferable from the wording** [M] — build the corpus, compare on answers;
  a router only after (`D-2026-08-15` deleted automatic routing).
- [ ] **Seven upstream issues are drafted and unfiled** [S] — file them from `docs/planning/upstream-issues/` (a human, with access to
  `langchain-ai`), then pin each fix: `core/graph_durability.py::ignoring_inherited_durability`, `agent/compaction.py::OffLoopContextEditing`.

### Knowledge and records

- [ ] **A citation-only record is pointed to by a facet answer, never counted in it** [M] — owner call: is a facet on a drawn species the
  structure search `D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable` excludes? If not, a label row with no derivation (`ingest/labels/record.py::record_phase`) and its verdict and skill text behind a live eval.

- [ ] **A note view is read in several steps, not one snapshot** [S] — `expand_note` awaits `edges`, `neighborhood`, `get_many` and
  `current_successors` separately, so a write between them can show a neighbour its links do not yet name; the Postgres store answers it with one transaction (`kg/store.py::NoteStore`).

- [ ] **`record_note` still writes files; the transactional write has no caller** [M] — put `put(NoteUnit)` on `NoteStore`, give `record_note` a
  Postgres arm that builds the unit and books `PutOutcome.notes`, and let the importer set `created_at`; `kg/postgres_store.py::PostgresNoteStore.put`, `kg/record.py::record_note`.
- [ ] **`NoteStore.search` returns every match, so a common term holds the corpus** [S] — the caller caps afterwards (`graph_max_results`); pass the cap
  into the statement (20,000 notes, one common term: ~0.9 s and ~60 MB per call, `cli/kg_store_bench.py`); `kg/postgres_store.py::search`.

- [ ] **`content_sha` is the digest of the canonical note, which the 128 header and the W4 ADR call the file text** [S] — they differ when reading the
  file back changes the note (edge whitespace, CRLF, U+2028); `kg-validate --compare` must compare `rows.canonical_note` values, never fingerprints; `kg/rows.py::content_sha`.
- [ ] **`PostgresNoteStore.put` trusts `created_by`; the API layer must authenticate first** [S] — W4.8's `PUT /knowledge/notes/{id}` has to decide who may write as
  `human` before it calls `put`; `kg/postgres_store.py::PostgresNoteStore.put`.
- [ ] **`neighborhood` walks to `hops` however small the graph** [S] — a recursive CTE cannot see its visited set, so cost is hops x edges reached (callers clamp to
  `graph_max_hops`); a level-by-level walk would stop early; `kg/postgres_store.py::_NEIGHBORHOOD`.
- [ ] **A retirement through the Postgres store applies `valid_to` and `superseded-by` only** [S] — the prose the retire helpers append to the body is dropped; stop
  writing it once the file writer is gone; `kg/postgres_store.py::_closed`, `memory/supersede.py::retire_note`.

- [ ] **A published result names no requester, and a second chemist's identical run is dropped** [M] — the cache hook returns on a hit before publishing, neither hook passes a
  `Publication`, and `_ENQUEUE`'s `DO NOTHING` drops a later row carrying one; fix all three together, test the set of actors; `science/calc/store.py::cached_compute`, `publish/outbox.py::_ENQUEUE`.
- [ ] **A tool composite whose enqueue failed can never be republished** [M] — `ThermochemistryResult`/`LogdResult` have no cache row and no job record, so no backfill walk finds them;
  give them a durable record and a walk, or declare the loss; `publish/hooks.py::publish_tool_result`, `connectors/results/workflows.py::_walk`.
- [ ] **A published record carries no calculation epoch, so the backfill republishes superseded rows** [M] — `find` drops them by the row's epoch, but `ResultRecord` has none and
  `_CACHED` selects none, so a sink holds both epochs' values for one subject; `publish/record.py::ResultRecord`, `publish/backfill.py::_CACHED`.

### Everything else

- [ ] **Follow-ups the architecture programme's reviews recorded** [M] — note-store search cap, kept-session metric, `gc.freeze()` after startup, test-logger hygiene; each is written up in `docs/planning/programme-deferred-2026-10.md` §D–E; anchor `durable/retry.py::retry_for`.
- [ ] **A failure returned inside a `Command` is invisible to audit and the failure observers** [S] — no tool returns one today; add the absence test (or unwrap `update["messages"]`)
  before one does; `agent/audit.py::returned_failure`.
- [ ] **The read/write partition test sees only registered tools, not the middleware verbs a graph binds** [S] — `task`, `write_todos`, the scratchpad verbs and handoffs are classified by
  special cases nothing enumerates; `agent/authz.py::side_effecting_call`, `tests/test_authz.py::test_every_advertised_tool_is_classified_write_or_read`.
- [ ] **A malformed key from the calc server reaches the chemist as a pydantic traceback** [S] — `remote_key` wraps only `KeyError`/`TypeError`; same pass: `_IN_FLIGHT` ignores which store a
  waiter joins, and the outbox `_CLAIM` has no tie-break; `connectors/calc/remote.py::remote_key`, `science/calc/store.py::_IN_FLIGHT`.
- [ ] **A capped evidence pack drops a session's newest effects** [S] — `assemble` reads `effects` oldest first and only flags `truncated`; read newest first (latent while no shipped job
  declares an effect); `operations/evidence_pack.py::assemble`.
- [ ] **Agent tests that prove less than they say** [S] — the framer-order test reads back a literal list, the positive authz tests pass with the gate deleted, and a plan-gate docstring
  claims an unchecked fail-closed; `tests/test_tool_framing.py::test_the_framer_sits_inside_the_converters_and_outside_the_trail`, `tests/test_plan_gate.py::test_spending_never_raises_when_the_store_is_unreachable`.
- [ ] **Retrieval tests and guards that prove less than they say** [S] — `PostgresNoteIndex`'s empty-`keep` guard survives deletion (only the in-memory twin is pinned), and the "semantic
  note" test passes on token overlap; `retrieval/vector_index.py::PostgresNoteIndex._retire_absent_ids`, `tests/test_vector_index.py::test_reindex_then_dense_search_finds_the_semantic_note`.
- [ ] **Dependabot group #431 needs hand-made bumps** [S] — mutmut 3.8 drops `Config.ensure_loaded`; deepagents `task` schema vs the
  900-token bound in `tests/test_context_floor.py`; rdkit 2026.3.6 lands paired with Chemclaw3-mcp (`tests/test_calc_rotation.py`).

## Upstream watch

On a dependency bump, ask per line: does upstream now do this, and better? A changed answer needs an ADR.
Standings derived at `temporalio` 1.31.0, `langchain` 1.3.15, `langgraph` 1.2.11, `langchain-core` 1.5.5, `deepagents` 0.7.6, `mcp` 1.x.

- `temporalio.contrib.langgraph.LangGraphPlugin` — declined, we use no `interrupt()` (`D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use`).
- `ContextEditingMiddleware` / `ClearToolUsesEdit` — adopted. `deepagents.SkillsMiddleware` — adopted, narrowed at the backend.
- `SummarizationMiddleware` — declined: a summary is new prose over untrusted content and loses the envelope.
- `ModelCallLimitMiddleware` — reverted (`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`).
- `ToolErrorMiddleware`, `ToolRetryMiddleware` — declined: MCP tools never raise.
- `HumanInTheLoopMiddleware` — declined for plan approval (`D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question`); per-call approval of irreversible actions is still open.
- deepagents `execute` verb — declined; code runs in the fleet's `pyexec` (`D-2026-08-25-a-sandbox-is-a-server-not-a-verb`). LangSmith — declined.
- MCP progressive discovery — watch: would narrow the server side of the schema cost. MCP Tasks — declined for durability, watch the wire.
- MCP server-initiated events — declined: fleet servers are stateless. MRTR — declined: different question from `ask_clarifying_question`.
- MCP agent identity (WIF, ID-JAG, RFC 8693, DPoP) — open: the trigger for re-adding OBO is recorded here.
- MCP `ttlMs`/`cacheScope` on list results — watch. MCP stateless protocol — the 2.x migration; it obsoletes two `connectors/server.py` traps.
- Libraries adopted (`D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency`): `httpx-sse`, `pathspec`,
  `charset-normalizer`, `pint`, `tiktoken`, `networkx` UnionFind, `rdkit.rdSubstructLibrary`. Declined: `tabulate`, `detect-secrets`,
  `rank_bm25`, `dimorphite-dl`, `yoyo`/`alembic`, `slowapi`, `secure`, `asgi-correlation-id`, `pytest-postgresql`, `respx`, `ase`/`cclib`,
  `RestrictedPython`, `EnsembleRetriever`, ruff `TID253` here (`D-2026-09-16-a-flat-ban-cannot-express-a-matrix`).

## Deferred

### Gated on infrastructure this environment does not have

- **The programme's cluster-, tenant- and person-gated items** — the kind `oidc-mock` lane, the Entra registration, UI egress, a real CloudNativePG rehearsal, the `calc_transport=rpc` flip, fleet session affinity and W4.9; steps in `docs/planning/programme-deferred-2026-10.md` §B · *Revisit:* a kind or OpenShift cluster with the operator installed and an Entra tenant (or the mock).
- **Databricks workspace** — vector store and warehouse driver proven only against fakes; three vendor facts unpinned, score formula first · *Revisit:* a real workspace with a Direct Vector Access index and SQL warehouse.
- **A real Qdrant server** — `retrieval/vectors/qdrant.py` runs only against an injected fake, so a filter it sends is not one it is known to apply · *Revisit:* a deployment sets `vector_store_provider` to `qdrant`, or the gate gains a Qdrant container.
- **Per-user reads from the warehouse ELN** — warehouse connects as one service identity · *Revisit:* Databricks plus an Entra tenant and a new OBO decision (D-046).
- **`X-Chemclaw-Actor` as durable attribution** — the header is unauthenticated · *Revisit:* an OBO exchange or a signed actor memo on core's MCP calls.
- **Push-to-registry + `helm upgrade` rollout, run** — written, never run · *Revisit:* a registry, namespace and the Jenkins credential ids in `deploy/jenkins/README.md`.
- **The core chart consuming the fleet's, and a chart for `Chemclaw3_ui`** — `Chemclaw3-mcp` ships `deploy/helm/chemclaw-fleet` and the kind lane renders it (`deploy/kind/render-fleet.sh`), but this chart does not depend on it, so each `connectors.<name>.url`, port and token key is written here and held against its render by `tests/test_helm_chart.py` (W6.8); the UI still ships plain manifests (W6.9) · *Revisit:* the rollout row above closes.
- **Live-retriever drift over the deployment's own graph** — the drift job scores the fixture corpus only · *Revisit:* a deployment with a populated graph and labelled cases.
- **A live target for the results store** — the publish path is built, no real sink exists, so outbox pruning (`retention_result_publications_days` ships off) and drain throughput are unsized · *Revisit:* a deployment sets `CHEMCLAW_RESULT_SINKS` to a real database.
- **Backup and restore on a real CloudNativePG cluster** — the template is schema-validated and the drill runs in containers; neither has met the operator, the Barman plugin or a real bucket (the `import` and `recovery` bootstraps, RTO at real size, the exported metric names the `chemclaw.backup` alerts read, `firstRecoverabilityPoint` under the plugin) · *Revisit:* a cluster with the operator and plugin: install `chemclaw-postgres`, rehearse a restore into a scratch namespace and time it (`docs/guides/runbook.md` § Backup, restore and PITR).
- **A backup archive in one region, and a platform Temporal outside the cluster** — the drill restores Temporal's SQL databases in the same cluster only; an Elasticsearch visibility store or a platform-owned Postgres is the platform's to back up · *Revisit:* the platform names its Temporal persistence, or a second region is required.
- **PgBouncer as CloudNativePG `Pooler`s** — two (transaction and session mode) would serve `deploy/README.md` § PgBouncer; not templated, the `-rw` Service is the direct endpoint · *Revisit:* a release runs above three replicas.
- **A worker whose broker is down never opens its probe port** — `durable/background_worker.py::main` connects first · *Revisit:* a second dependency joins `connect()` or an operator misdiagnoses an outage.
- **Readiness cannot see a connector that is up and broken** — `connectors/health.py::_probe` only reads `/healthz` · *Revisit:* `connectors_required` becomes a runtime gate, or a long-broken connector is reported.
- **A template step's roles cross the durable boundary unsigned** — `durable/template_activities.py::_acting_as` trusts the payload · *Revisit:* a broker other teams can write to, or one without mTLS.
- **`chemclaw-contracts` is a git dependency pinned by commit** — `uv.lock` holds a commit, not a content hash, `make deps-audit` skips it ("URL requirements cannot be pinned"), the build backend is unpinned and Dependabot cannot bump it; accepted because the package is pydantic and pyyaml plus data the fleet owns and the tag `contracts-v1.0.0` could not be pushed (`pyproject.toml`, `D-2026-10-08-a-connector-name-has-one-owner`) · *Revisit:* the tag can be pushed or the fleet publishes a release wheel; pin that by URL and hash.

### Gated on an upstream fix

- **A cancellation lost while waiting for a pooled connection** — `psycopg_pool` still uses `asyncio.wait_for` · *Revisit:* a pool release without it, or Python ≥ 3.12.
- **`mcp` 2.x** — a deliberate two-repo migration, not a bump · *Revisit:* a decision to pay for it; re-run `tests/test_connector_transport.py` and `tests/test_connector_identity.py`.
- **Front door on `stream_events(version="v3")`** — built, measured, reverted: usage per block is lost · *Revisit:* upstream emits usage per content block or the raw stream.
- **Prompt-cache control on the production provider** (REV-9) — `langchain_openai` exposes no `cache_control` · *Revisit:* upstream exposes it; first read `chemclaw_cache_read_tokens_total`.
- **Delta representation for the checkpointer's `messages` channel** — writes are quadratic in thread length · *Revisit:* `DeltaChannel` leaves beta, or WAL/replication lag is attributed to it.
- **`/readyz` cannot bound a Postgres that accepts and stops answering** — psycopg's cancel re-wait is unbounded · *Revisit:* psycopg bounds `AsyncConnection.wait`'s re-wait.

### Gated on a live model budget

- **Tool selection is never exercised by `make test`** — every test scripts its own `tool_call` and `tests/test_probe_coverage.py` checks probe names only, so a prompt change that stops the model reaching for a tool passes the gate · *Revisit:* a gateway credential in the environment that runs `make live-ab`; gate on its tool-selection accuracy.
- **Model-facing text rewrite (W2.15–W2.17), the deepagents upgrade merge (W5.2), the delegation re-check (W5.14), a semantic default embedder (W7.1) and the retrieval-eval floor (W7.3)** — each ships only behind the live arm; the first step of each is in `docs/planning/programme-deferred-2026-10.md` §A · *Revisit:* a gateway credential in the environment that runs `make live-ab`.
- **The judge names no claims on 88% of turns** — schema half fixed, measurement half open · *Revisit:* a live run re-counts non-empty `claims`.
- **Whether a `stated` quote's figure is about this slot** — `agent/protocol_design_tools.py::_quote_supports` cannot attribute · *Revisit:* a deployment's real turns to count over.
- **An advisor tool** — design fully determined, no second model tier to consult · *Revisit:* an endpoint serving a stronger tier via `build_chat_model("advisor")`.
- **Deferring connector tool schemas behind a discovery verb** — designed, unbuilt (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) · *Revisit:* a live A/B on `expected_tools_met`.
- **Narrowing the `default` profile's eleven redundant names** — worth −21% tokens, unmeasured on answer quality · *Revisit:* a live lane.

### Gated on a scale not yet reached

- **The programme's sequenced remainder** — W3.12 (parallel gate), W4.7 to W4.11, W6.4, W7.8, W7.9, W7.12, W5.4–W5.13 and W8; order and file lists in `docs/planning/programme-deferred-2026-10.md` §C · *Revisit:* ten clean parallel runs on settled `main` (W3.12), `kg-validate --compare` clean for seven days (W4.9), the five flip criteria in §B5 (W6.4).
- **Delete the `propose_report` activity alias** — old executions may still run · *Revisit:* no `DevelopmentReportWorkflow` started before the rename is running.
- **Drop `note_proposed` from `evals/live.py`** — older UI builds still send it · *Revisit:* every probed deployment sends `note_recorded`.
- **Watching for a new ELN run, not just a new note** — no one has asked · *Revisit:* a chemist asks for run-level alerting on a real corpus.
- **Sub-quadratic reaction clustering** (KM-14) — `memory/similarity.py::cluster_by_similarity` is O(n²) and exact · *Revisit:* ~10⁴ reactions; switch to Postgres HNSW k-NN.
- **`pattern_bits` GIN prefilter on `molecule_fingerprints`** — the scan is bounded and warns · *Revisit:* the truncation warning fires past ~10⁴ molecules.
- **`within=` id-array scaling** — eligibility ships one SQL array · *Revisit:* the corpus approaches ~10⁵ notes.
- **A durable digest of a named protocol set** — `condense_protocols` refuses past its bounds · *Revisit:* `chemclaw_protocol_digests_total` shows the refusal in real use.
- **CREST's other run types** (`--qcg`, `--msreact`, `--entropy`, `--mecp`) — one flag away, no question asks for them · *Revisit:* a chemist asks; `--qcg` first.
- **Better-sampled or free-energy-refined ensemble pKa** — neither refinement moves the class error · *Revisit:* explicit solvent lands, or the residual decides something real.
- **Live reattachment to a detached turn's stream** — a detached turn completes unseen · *Revisit:* a deployment shows watching it matters.
- **Pruning unconsumed `session_events` rows** — an undelivered completion must survive · *Revisit:* the unconsumed count is a measured cost.
- **A bounded per-round campaign record** — rounds store cumulative observations · *Revisit:* the first deployment running durable campaigns.
- **Streaming the memory corpus** — reading whole is memory-bound, not time-bound · *Revisit:* a corpus exceeds `memory_corpus_max_reactions`.
- **A staleness signal for a stalled append-only feed** — `ingest/labels/cursor.py::load_corpus_cursor` ignores `updated_at` · *Revisit:* the first `append_only:` source.
- **Advisory audit over the `github-actions` closure** — accepted risk in `.github/dependabot.yml` · *Revisit:* the first advisory on an action in `.github/workflows/`.
- **Mining a chemist's edits to a generated protocol** — no human revisions exist yet · *Revisit:* a used deployment.
- **A distiller from recurring trajectories to procedures** — no sessions to distil · *Revisit:* a deployment with sessions.
- **Review scaling over distilled skills** — downstream of the distiller · *Revisit:* the distiller produces proposals.
- **Three row-projecting tools defang a whole page on the event loop** — cheap at today's page sizes · *Revisit:* a latency profile names one, or a page bound is raised.
- **A write-time ceiling on `calculation_results.result`** — none is stated: TOAST absorbs large payloads, `calc_hessian_max_atoms` and `crest_max_members` fence the families that grow, and `calc_find_max_result_chars` bounds what reaches the model · *Revisit:* a payload family with no fence.
- **Exact accounting of the helper `files` budget** (issues #463, #489) — shares are over-charged, nothing measured it · *Revisit:* `checkpoint_writes` holds a `files` row and truncations move.

### Gated on a capability, source or licence not in scope

- **ML interatomic potentials** (ANI-2x, AIMNet2) — no demand (`D-2026-09-19-the-condition-was-met-and-the-answer-is-still-no`) · *Revisit:* someone asks and the residual stops being solvation's.
- **Retrosynthesis and reaction prediction** — not a stated need · *Revisit:* route planning is needed; it lands as a `url:` connector bundle in its own image.
- **The ELN run a calculation was computed for** — a missing fact, not a column · *Revisit:* a result sink has a live target.
- **A site-supplied compound identifier on a published result** — no joining system named · *Revisit:* a result sink has a live target.
- **Tabular foundation model** (TabPFN/TabICL) — BoFire answers "which next" · *Revisit:* few-shot trend prediction over tables is needed; check licence.
- **Generic-document OCR** — structured formats are covered · *Revisit:* `skipped_scan` is material and a machine-read citation marker is decided.
- **Hand-drawn structures and spectra images** — unsolved accuracy · *Revisit:* review-grade OCR-for-structures, or a correction surface.
- **Legacy binary Office** (`.doc`, `.xls`, `.ppt`, `.msg`) — no offline reader · *Revisit:* `make share-estimate` shows a material count.
- **Mass balance beyond element subsumption** — exports lack coefficients and masses · *Revisit:* an export carries either.
- **Universal ingest abstraction** — every source fits `ElnAdapter` · *Revisit:* a cursored source that does not.
- **Durable multi-step deep research** — research is interactive · *Revisit:* one question needs restart-surviving fan-out.
- **LLM faithfulness check of drafted report sections** (F10-B3) — reports have no prose-synthesis step · *Revisit:* one is added; route it through `verify_answer`.
- **PMI/E-factor as a BO objective** — no mass data · *Revisit:* a real formulation case with masses.
- **Nonlinear and product constraints on a BO domain** — would need a worse optimizer · *Revisit:* a stated nonlinear coupling.
- **`NChooseKConstraint`, interpoint equality, DoE blocking** — no story asks · *Revisit:* a story, or the plate/batch entity.
- **A second molecular representation for BO** — xTB descriptors suffice electronically · *Revisit:* a steric axis is needed.
- **LLM-embedding deep kernels for BO** (GOLLuM) — no heterogeneous campaign · *Revisit:* one exists and our own measurement shows xTB losing.
- **Feature importance over a BO surrogate** — available, no caller · *Revisit:* a campaign big enough to attribute, or a second caller.
- **Blocking a low-confidence answer** — the verifier flags instead · *Revisit:* a UI decision point and a deployment wanting withholding.
- **Lab automation / SiLA2 closed loop** — needs instruments · *Revisit:* robotic execution enters scope.
- **Process flowsheet synthesis/simulation** — separate capability · *Revisit:* process design is in scope.
- **Domain foundation models** — heavy · *Revisit:* task accuracy plateaus.
- **Per-bundle `log.md` changelog** (D-074) — redesign pending · *Revisit:* someone asks for a changelog without `git log`.
- **JS test infrastructure** — `api/static/app.js` is a demo shell · *Revisit:* the web client grows.
- **Profiles supplying prompt blocks** — no caller · *Revisit:* the first site profile that narrows tools.
- **In-product request to publish a chemist's skill** — nobody has asked · *Revisit:* a chemist reports wanting a promotion and not getting one.

### Declined

- **MACE-OFF / MACE-MP** — weights are academic-licence only · *Revisit:* the weights are relicensed for commercial use.
- **Literature/patent retrieval by request-time call** (TOOL-6) — no-egress (D-089) · *Revisit:* a licence-clean bulk export vendored at build time.
- **LLM extraction of what an ELN entry learned** — review cost unbudgeted · *Revisit:* a budget is allocated; scope the review first.
- **Second queue system** (pg-boss) — Temporal covers it (D-006) · *Revisit:* none.
- **LLM summarization of compacted history** — injection risk that persists · *Revisit:* collapse loses essential context and a trusted summarizer exists.
- **Split-conformal uncertainty on a predictor** — too few observations · *Revisit:* ≥59 observations per calc version, a non-manual producer, and a 1σ-vs-90% resolution.
- **JSON payloads for structured tool results** — models read, not parse · *Revisit:* a tool needs a parseable payload.
