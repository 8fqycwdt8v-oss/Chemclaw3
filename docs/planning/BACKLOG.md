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

### Answers that are wrong without saying so

- [ ] **A metal hydride standardizes to its metal without the hydride** [S] — `[PdH]Cl` → `[Cl-].[Pd+]`, so hydride and salt share a
  `compound_id`; trace the dropping step in `core/chem.py::_cleaned` / `core/chem.py::standardize`.
- [ ] **`plan_gate.py`'s mutmut selection pairs one of its five covering test files** [S] — add the four siblings at the next floor
  re-measure (~80 min); `[tool.mutmut]` in `pyproject.toml`, `tests/test_mutation_workflow.py`.
- [ ] **Tool-utility results used a control arm that also swaps the prompt** [M] — re-run `make live-ab` / `make live-benchmark`
  with `data/evals/profiles/tools-removed.yaml` as baseline (needs gateway credit); `cli/live_probes.py::_AB_BASELINE_PROFILE`.
- [ ] **`hybrid` retrieval is worse than `graph`; fusion knobs are measured no-ops** [M] — measure an `openai_compatible` embedder
  with `make retrieval-arms` before quoting any number; `D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number`.

### Operating it

- [ ] **A helper's report carries no "derived from untrusted reading" marking** [M] — first measure whether injected instructions
  propagate (`make live-delegation`); `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`.
- [ ] **2026-09-27 live-run fixes unverified against a real model** [S] — re-run pc-03, a capping delegation probe and a revised
  answer; `connectors/calc/compose.py::require_solvent_for_ions`, `api/runner.py::_REVISION_NOTE`, `agent/loop_cap.py`.
- [ ] **Connectors dev server stalled 52 s under probe load** [S] — reproduce on an idle host before fixing; `cli/connectors_dev.py`.
- [ ] **Live-lane papercuts** [S] — `cli/live_storm.py` rejects `--families D,H`; `infra/live/processes.sh restart api` truncates the
  log; the lane leaves `CHEMCLAW_FRAMING_ENVELOPE_SECRET` unset so two processes frame differently.
- [ ] **Envelope-nonce prefix test hit its 180 s timeout under load** [S] — time it on an idle host before loosening;
  `tests/test_context_floor.py::test_two_processes_send_the_same_prefix_but_for_the_envelope_nonce`.
- [ ] **The same three questions cost 2.1x more on one boot than another** [M] — record bound tool names and the schema gauge per
  boot (`make live-turn-cost`); a tool-binding race is the leading, unproven candidate.

### Where the field moved past us

- [ ] **Tool schemas are 72% description** [M] — whether shorter `Args:`/`Returns:` still route correctly is a `make live-ab`
  question; `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`.
- [ ] **Enumeration and scan tools rest on one probe each** [S] — add second phrasings for `enumerate_*`, `scan_coordinate`,
  `profile_rotation`, `optimize_geometry` in `data/evals/probes/`, ranked by `audit_events` once a deployment exists.
- [ ] **A chat-room connector over a shared session** [M] — prerequisites shipped
  (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`); left: the connector and the queue/stream UI in `Chemclaw3_ui`.
- [ ] **A routing corpus where the right profile is not inferable from the wording** [M] — build the corpus, compare on answers;
  a router only after (`D-2026-08-15` deleted automatic routing).

### Knowledge and records

- [ ] **A citation-only record can be cited and cannot be found** [M] — carry structured species on its label row;
  `ingest/eln/ingest.py::ingest_reaction`, `science/labels/records.py`, `D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable`.
- [ ] **An entry amended to citation-only keeps its old label row** [S] — `connectors/rxnfp/server/tools.py::reagent_frequency` still
  counts it; `agent/protocol_design_tools.py::uncited_precedent` checks neither withheld nor retracted.

### Everything else

- [ ] **PR #321's review findings never landed** [M] — extract what is still open from its review document into rows, then close #321.
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

- **Databricks workspace** — vector store and warehouse driver proven only against fakes; three vendor facts unpinned, score formula first · *Revisit:* a real workspace with a Direct Vector Access index and SQL warehouse.
- **Per-user reads from the warehouse ELN** — warehouse connects as one service identity · *Revisit:* Databricks plus an Entra tenant and a new OBO decision (D-046).
- **`X-Chemclaw-Actor` as durable attribution** — the header is unauthenticated · *Revisit:* an OBO exchange or a signed actor memo on core's MCP calls.
- **Push-to-registry + `helm upgrade` rollout, run** — written, never run · *Revisit:* a registry, namespace and the Jenkins credential ids in `deploy/jenkins/README.md`.
- **A chart for `Chemclaw3_ui` and the `Chemclaw3-mcp` servers** — neither repo is deployable as a chart yet · *Revisit:* the rollout row above closes.
- **Live-retriever drift over the deployment's own graph** — the drift job scores the fixture corpus only · *Revisit:* a deployment with a populated graph and labelled cases.
- **A live target for the results store** — the publish path is built, no real sink exists · *Revisit:* a deployment sets `CHEMCLAW_RESULT_SINKS` to a real database.
- **Backup and restore tooling for Postgres and Temporal** — no owner of those stores is named · *Revisit:* an owner exists to run and verify a restore.
- **A worker whose broker is down never opens its probe port** — `durable/background_worker.py::main` connects first · *Revisit:* a second dependency joins `connect()` or an operator misdiagnoses an outage.
- **Readiness cannot see a connector that is up and broken** — `connectors/health.py::_probe` only reads `/healthz` · *Revisit:* `connectors_required` becomes a runtime gate, or a long-broken connector is reported.
- **A template step's roles cross the durable boundary unsigned** — `durable/template_activities.py::_acting_as` trusts the payload · *Revisit:* a broker other teams can write to, or one without mTLS.

### Gated on an upstream fix

- **A cancellation lost while waiting for a pooled connection** — `psycopg_pool` still uses `asyncio.wait_for` · *Revisit:* a pool release without it, or Python ≥ 3.12.
- **`mcp` 2.x** — a deliberate two-repo migration, not a bump · *Revisit:* a decision to pay for it; re-run `tests/test_connector_transport.py` and `tests/test_connector_identity.py`.
- **Front door on `stream_events(version="v3")`** — built, measured, reverted: usage per block is lost · *Revisit:* upstream emits usage per content block or the raw stream.
- **Prompt-cache control on the production provider** (REV-9) — `langchain_openai` exposes no `cache_control` · *Revisit:* upstream exposes it; first read `chemclaw_cache_read_tokens_total`.
- **Delta representation for the checkpointer's `messages` channel** — writes are quadratic in thread length · *Revisit:* `DeltaChannel` leaves beta, or WAL/replication lag is attributed to it.
- **`/readyz` cannot bound a Postgres that accepts and stops answering** — psycopg's cancel re-wait is unbounded · *Revisit:* psycopg bounds `AsyncConnection.wait`'s re-wait.

### Gated on a live model budget

- **The judge names no claims on 88% of turns** — schema half fixed, measurement half open · *Revisit:* a live run re-counts non-empty `claims`.
- **Whether a `stated` quote's figure is about this slot** — `agent/protocol_design_tools.py::_quote_supports` cannot attribute · *Revisit:* a deployment's real turns to count over.
- **An advisor tool** — design fully determined, no second model tier to consult · *Revisit:* an endpoint serving a stronger tier via `build_chat_model("advisor")`.
- **Deferring connector tool schemas behind a discovery verb** — designed, unbuilt (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) · *Revisit:* a live A/B on `expected_tools_met`.
- **Narrowing the `default` profile's eleven redundant names** — worth −21% tokens, unmeasured on answer quality · *Revisit:* a live lane.

### Gated on a scale not yet reached

- **Delete the `propose_report` activity alias** — old executions may still run · *Revisit:* no `DevelopmentReportWorkflow` started before the rename is running.
- **Drop `note_proposed` from `evals/live.py`** — older UI builds still send it · *Revisit:* every probed deployment sends `note_recorded`.
- **Watching for a new ELN run, not just a new note** — no one has asked · *Revisit:* a chemist asks for run-level alerting on a real corpus.
- **Sub-quadratic reaction clustering** (KM-14) — `memory/similarity.py::cluster_by_similarity` is O(n²) and exact · *Revisit:* ~10⁴ reactions; switch to Postgres HNSW k-NN.
- **`pattern_bits` GIN prefilter on `molecule_fingerprints`** — the scan is bounded and warns · *Revisit:* the truncation warning fires past ~10⁴ molecules.
- **`within=` id-array scaling** — eligibility ships one SQL array · *Revisit:* the corpus approaches ~10⁵ notes.
- **A durable digest of a named protocol set** — `condense_protocols` refuses past its bounds · *Revisit:* `chemclaw_protocol_digests_total` shows the refusal in real use.
- **CREST's other run types** (`--qcg`, `--msreact`, `--entropy`, `--mecp`) — one flag away, no question asks for them · *Revisit:* a chemist asks; `--qcg` first.
- **Better-sampled or free-energy-refined ensemble pKa** — neither refinement moves the class error · *Revisit:* explicit solvent lands, or the residual decides something real.
- **Cross-process in-flight dedup in the calculation store** — in-process half done (`science/calc/store.py::_IN_FLIGHT`) · *Revisit:* duplicate CREST runs across workers become a measured cost.
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
