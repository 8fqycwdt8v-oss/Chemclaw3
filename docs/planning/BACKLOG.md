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

- [ ] **A worker's memory grows with a BO campaign's rounds whether or not the run stays cached** [M] — parked on the `make up` broker (macOS footprint, 2 runs): calc 146 KiB, a fresh campaign 253 KiB, under the
  530 KiB per workflow `tests/test_workers.py` asserts; but 30 rounds 1.6 MiB cached vs 1.3–1.5 MiB with the cache at 1, 100 rounds 6.6 MiB; find the holder before the bound covers `bo`; `connectors/bo/workflows.py`.

### Answers that are wrong without saying so

- [ ] **`similar_reactions` and the design precedent check still serve a fingerprint hit with no record** [S] — the retriever now passes only servable records, but the tool keeps "unindexed is not withdrawn",
  so a hit `expand_note` refuses is still cited; owner call whether one rule holds; `connectors/rxnfp/server/tools.py::similar_reactions`, `agent/protocol_design_tools.py::uncited_precedent`.
- [ ] **A salt dissolved in one organic solvent standardizes to the solvent** [S] — `[Li+].[AlH4-].C1CCOC1` and `[Na+].[H-].C1CCOC1` share THF's
  `compound_id`, `[Na+].[BH4-].CO` methanol's: ions that balance each other are stripped as the solvent's counterions; needs an owner decision on the strip rule, `core/chem.py::standardize`.
- [ ] **Tool-utility results used a control arm that also swaps the prompt** [M] — re-run `make live-ab` / `make live-benchmark`
  with `data/evals/profiles/tools-removed.yaml` as baseline (needs gateway credit); `cli/live_probes.py::_AB_BASELINE_PROFILE`.
- [ ] **`hybrid` ranks better than `graph` with a real embedder, but 21 probes cannot flip the default** [M] — `make retrieval-arms` on `nomic-embed-text`
  (`D-2026-10-24-an-orthogonal-dense-leg-ranks-better-and-does-not-yet-change-the-default`) shows equal recall and a better mean rank; grow the labelled gold set to 100 pairs over 40 probes, or fix a deployment embedder, then re-run and flip under that ADR's option 3.

### Operating it

- [ ] **Backend RPC is built but off: `calc_transport` defaults to `mcp`** [M] — flip after the in-cluster `make bench-backend-rpc` (cache-hit p50 ≤ 10 ms), a cluster run on `rpc` that proves no hop with a shorter idle or read timeout (mesh sidecar, load balancer, ingress at 60 s) cuts a long call, and a decision on `/rpc` having no `cancel_on_timeout` counterpart; then delete the MCP path for backends:
  `core/mcp_session.py` (`open_session`, `cancel_on_timeout`, `short_connect_client`, `invoke`), `connectors/calc/remote.py::_open_transport`, `ingest/labels/labeller.py::_open_transport`, `core/backend_rpc.py::BackendSession`, the `calc_transport` setting and `tests/test_backend_rpc_fleet.py`'s MCP arms.
- [ ] **A NaN or infinity is refused in a request but not in an answer over `/rpc`** [S] — `/rpc` returns such a float as null while MCP's text carries it; fleet contract decision (reject in the tool or declare null), then pin in `tests/test_backend_rpc_fleet.py`;
  `core/backend_rpc.py::RpcSession.invoke`, `tests/test_backend_rpc.py::test_the_client_passes_an_answer_through_as_the_server_serialized_it`.
- [ ] **A note written in one activity is unseen by a later one on another worker pod until the sync** [M] — each pod commits into its own
  clone; no shipped workflow reads its own write, so drive one first; `kg/git_writer.py`, `durable/template_activities.py::run_agent_step`.
- [ ] **The drill's `kg-validate` still reads a notes directory, not the Postgres store** [S] — the `kg_*` tables are restored and counted, but their notes are not what is validated until W4.10;
  `src/chemclaw/cli/restore_drill.py::kg_checks`, `src/chemclaw/core/restore_check.py::load_knowledge_rows`.
- [ ] **A helper's report carries no "derived from untrusted reading" marking** [M] — first measure whether injected instructions
  propagate (`make live-delegation`); `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`.
- [ ] **The same three questions cost 2.1x more on one boot than another** [M] — record bound tool names and the schema gauge per
  boot (`make live-turn-cost`); a tool-binding race is the leading, unproven candidate.

### Where the field moved past us

- [ ] **Tool schemas are 72% description** [M] — whether shorter `Args:`/`Returns:` still route correctly is a `make live-ab`
  question; `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`.
- [ ] **The default prompt names `find_past_jobs` but never `find_calculations`** [S] — the molecule-level "already computed" read is named only in the `evidence`/`computation` profiles;
  add it to the research-loop block behind a `make live-ab` arm; `agent/chemclaw_agent.py::_INSTRUCTION_BLOCKS`.
- [ ] **`fetch_artifact` refuses every artifact this release stores and is still bound** [S] — kept only for older notes' `artifact_refs`; unbind it from the `computation`/`evidence`
  profiles, or bind it only when a text artifact exists, behind a live eval; `connectors/calc/server/tools.py::fetch_artifact`.
- [ ] **A chat-room connector over a shared session** [M] — owner call between a Teams bridge on each sender's
  own token and declining (proposal PR #640); the queue/stream UI shipped in `Chemclaw3_ui`.
- [ ] **Profile routing is not compared on answers** [M] — the corpus exists (`data/evals/routing/profile-routing.yaml`, checked offline by `tests/test_routing_corpus.py`);
  run each case under its `worded_like`, its `needs` and the default agent against a live model and grade on `direction`; a router only after (`D-2026-08-15` deleted automatic routing).
- [ ] **Five upstream reports are open; pin each fix as it lands** [S] — deepagents#7236, #7237, langchain#41449, #41450 (filed) and langgraph#9267 (another reporter); status per draft in
  `docs/planning/upstream-issues/`; `core/graph_durability.py::ignoring_inherited_durability`, `agent/compaction.py::OffLoopContextEditing`.

### Knowledge and records

- [ ] **A citation-only record is pointed to by a facet answer, never counted in it** [M] — owner call: is a facet on a drawn species the
  structure search `D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable` excludes? If not, a label row with no derivation (`ingest/labels/record.py::record_phase`) and its verdict and skill text behind a live eval.

- [ ] **A tool composite whose enqueue failed can never be republished** [M] — `ThermochemistryResult`/`LogdResult` have no cache row and no job record, so no backfill walk finds them;
  give them a durable record and a walk, or declare the loss; `publish/hooks.py::publish_tool_result`, `connectors/results/workflows.py::_walk`.
- [ ] **A sink cannot tell a superseded-epoch calculation from a current one** [M] — the backfill skips superseded rows, but one published live before an epoch bump stays beside
  its successor, both `status = 'valid'`; carry the epoch on `ResultRecord` (a contract and DDL change) or mark the old row superseded; `publish/record.py::ResultRecord`.

### Everything else

- [ ] **deepagents 0.7.32 waits on the live model-text evaluation** [S] — the bump rewrites upstream's `task`, `read_file` and `edit_file` text, so it ships behind
  `docs/guides/model-text-evaluation.md`; 0.7.31+ also takes `FilesystemMiddleware(permissions=)` publicly, so the `_permissions` shim in `agent/langgraph_agent.py::_middleware` goes with it.

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
- **Probe phrasings ranked by real use** — every `enumerate_*`, `scan_coordinate`, `profile_rotation` and `optimize_geometry` has a second phrasing in `data/evals/probes/`, chosen without usage data, so which tools deserve a third is unknown · *Revisit:* a deployment's `audit_events` say which tools chemists call most.
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
