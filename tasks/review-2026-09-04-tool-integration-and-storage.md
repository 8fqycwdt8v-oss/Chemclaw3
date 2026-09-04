# Deep review: tool integration, tool usage, result storage, later retrieval — 2026-09-04

Branch `claude/tool-integration-storage-review-3zupm4`. Scope, as asked: how a tool reaches the
model, how it is called, where its result is stored, and how that result comes back later.

Every finding below carries the command or the measurement that produced it. Where a reviewer's
claim is repeated here it was re-verified independently against the tree; the four marked
**(re-verified by the coordinator)** were the load-bearing ones.

## Method

Seven fresh-context reviews over a running stack (`dockerd` + `make up` + `make db-migrate`, so the
Postgres-backed half of the suite was evidence rather than a skip), on disjoint territory: the
connector seam, the middleware chain, the calculation cache, the publish/sink path, retrieval and
memory, the `Chemclaw3-mcp` fleet, and one asking what the suite can actually fail. Plus the
coordinator's own pass on the seams no single reviewer owns — the hops *between* the four.

Reviews ran read-only. No repository file was modified; every probe ran from a scratch directory or
a temp copy.

## What this run proves

Measured on this checkout, with the stack up. `tests/pg.py::migrated_db_or_skip` runs `migrate()`
itself into a per-pid schema, so the suite never depended on the operator's `make db-migrate`
timing.

| | Result |
| --- | --- |
| `uv run pytest -q -rs` | **6,547 passed, 36 skipped, 0 failed**, 848.70 s, exit 0 |
| Independent cross-check (17 chunks of 20 files) | 6,547 / 36 / 0 — identical |
| `make lint` | `All checks passed!`; 801 files already formatted |
| `make type` | `Success: no issues found in 801 source files` |
| Coverage over the nine modules in scope | **93%** (repo floor 84%; whole run 92.90%) |

**Postgres was worth 404 tests, and all of them executed.** A control run of the 66
Postgres-backed files against a dead DSN gave 611 passed / 437 skipped. The residual 36 skips are
33 helm renders plus 3 that need untruncated git history — neither class is Postgres.

This does not contradict `tasks/todo.md`'s 6,551 / 4 / 0; it is a different tree (6,583 collected
here against 6,555 there, sibling work merged today). With `helm` installed this tree would show
6,580 / 3.

Two caveats about the sweep itself, recorded because they bound what the numbers mean: sibling
agents were mutating this checkout during it (an early `make lint` failed on two probe files that
no longer exist; all authoritative numbers were taken after they vanished), and a first full-suite
attempt was killed by the cgroup OOM killer at 12.4 GB anon-rss with three suites running at once.
This box cannot run three suites concurrently.

## The five cross-cutting patterns

These are the finding, more than any individual row. Each is stated with the instances that
produce it, because a pattern asserted from one example is an anecdote.

### 1. A control applied at every site but one

The dominant shape, six independent instances, and in every case the missed site is the expensive
one:

| The control | Applied at | Missing at |
| --- | --- | --- |
| `mcp_server_kit.limits` SMILES length guard | `chem`, `safety`, `rxnlabel`, `rxnpredict` | **`calc`** — the pod holding 14,400 s CREST searches |
| The vector tie-break in the *outer* sort | `retrieval/vector_index.py`, `ingest/documents/index.py` | **`science/fingerprints/store.py`** — the store sized for Pistachio |
| `embedding_key` bound on the dense *read* | `PostgresNoteIndex` | **`ExternalVectorNoteIndex.search_dense`** |
| `_echo()` bounding a parse error's echo | `chem` (120 chars) | **`calc`** (`{smiles!r}`, unbounded) |
| `readiness=` on `/healthz` | five of seven servers | **`pyexec`, `rxnlabel`** |
| Client bound ≥ server bound (timeouts) | process hop, pod hop | **the agent hop** |

The tie-break and `embedding_key` cases are the sharpest: `retrieval/vector_index.py:354-366`
explains *both* fixes at length, and the two modules that needed the same fix did not get it.

### 2. The failure mode is silence, and readiness stays green

Nine findings share it. A tool a server stopped advertising is dropped with no log and no metric,
while the plan gate and the skills backend go on counting it. A result whose `payload_kind` is
unregistered is dropped with `logger.debug` and no counter — measured `written=0`,
`counters_moved={}`, nothing at INFO. A missing bearer token renders as `ExceptionGroup` and the
health probe hits unauthenticated `/healthz`, so `/readyz` says healthy. `rxnpredict` books
`outcome="ok"` on a zero-success ensemble. An evicted artifact leaves a cache row three of four
readers still serve.

### 3. A bound applied before something that can undo it

- `defang` runs *after* the 60,000-char cut and its `<` → `&lt;` pass measured **3.98×** expansion,
  delivering 239,091 chars to the model.
- The Fukui payload is sliced to 15 sites *before* it is cached, by a `top_n`/`mode` pair
  deliberately excluded from the cache key.

### 4. Ceilings that do not know about each other

`agent_max_parallel_tool_calls` (8) × `agent_max_tool_result_chars` (60,000) = **120,040 estimated
tokens** in a batch compaction is *designed* never to clear, on top of a **42,730**-token prefix
charged unconditionally, against a **100,000**-token budget. Nothing asserts the product.
Separately, `calc`'s inline tools wait 3,600 s on a backend behind a 600 s agent bound.

### 5. A claim held only by a test

Three instances, and `CLAUDE.md` already names the shape (`map_to_hpc_identity`: a guard with no
caller, kept alive by a test that calls it directly, is a *claim* that a control exists).

- The effect ledger's entire operator read path — `unsettled()`, `effects_for_session()`,
  `Unsettled` — has **zero** callers outside `tests/test_effects.py` (H14).
- Two guards revert with the whole suite green, one of them at 100% branch coverage while mutated
  (M24).
- `tests/test_tool_framing.py:329` asserts the index order of the literal list the function under
  test returns — a constant compared with itself (L, below).

---

## HIGH

### H1 — A 20 kB SMILES segfaults the `calc` pod (`Chemclaw3-mcp`)
`servers/calc/src/chemclaw_mcp_calc/engine/chem.py:95,58`. It is the only SMILES-taking server that
imports nothing from `mcp_server_kit.limits`, whose own docstring (`limits.py:1-10`) names this
exact defect and lists the four servers it protects.

```
require_canonical_smiles("C"*20000)   -> exit 139 (SIGSEGV)     # re-verified by the coordinator
compute_xtb_energy / predict_pka / predict_solubility
  / embed_structure / calculation_key, same input -> 139
"C"*8000                              -> exit 137 (SIGKILL)
chem, safety, same input -> InvalidSmilesError, "above the 4000-character limit"
```

One authenticated `tools/call` of ~20 kB — far under the 1 MB `BodySizeLimit` — takes the pod down
and every in-flight CREST search with it. Two of the affected tools (`calculation_key`,
`embed_structure`) are `read_only`, so they are reachable **under an unapproved plan** and sit
outside `engine/admission.py`. The fix is one import and two guard lines, copying
`servers/safety/.../engine/chem.py:88,99`.

### H2 — The Fukui cache row is truncated by an argument excluded from its key
`Chemclaw3-mcp` `servers/calc/.../tools.py:435-436` (and `:849-850`) applies
`sites[:top_n or xtb_fukui_top_n]` **before** the payload is cached, while
`engine/identity.py:157-164` correctly excludes `mode` and `top_n` from the key on the ground that
neither changes the *computation*. They change the *payload*. Chemclaw3's own tool docstring
(`connectors/calc/server/tools.py:1076-1078`) states the opposite: *"the row holds every atom, so
asking for more sites re-slices a cached result."*

Measured on a real GFN2 run of aspirin (21 atoms): engine returned 21 sites, the stored payload
holds 15, ranked by whichever `mode` the first caller triggered; four of the atoms a nucleophilic
top-15 should contain are absent from the row. `total_atoms` still reports 21, so nothing signals
it. The top hit survived for aspirin, caffeine and 4-nitro-benzanilide, so the symptom is a
silently thinned ranking rather than a wrong answer — and nothing bounds that as atom count grows.

### H3 — Structural similarity search can never use its HNSW index
`science/fingerprints/store.py:494` puts the tie-break in the **inner** `ORDER BY`, the exact form
`retrieval/vector_index.py:354-362` documents as making the ordering underivable from the index.
**(re-verified by the coordinator)**

| corpus | shipped | tie-break removed | tie-break in the outer sort |
| --- | --- | --- | --- |
| 20,000 | Seq Scan, **14.50 ms** | Index Scan, 1.76 ms | 1.48 ms |
| 100,000 | Parallel Seq Scan, **103.93 ms** | Index Scan, 6.01 ms | 7.36 ms |

Cost is O(N). The same statement serves four tables including `corpus_reactions` — the table sized
for Pistachio's ~10⁷ patent reactions. A site that loads it seq-scans ten million rows per
conversational tool call.

### H4 — One unusable endpoint declaration removes every connector tool from every turn
`connectors/registry.py:515-526` builds all specs in one comprehension; `_mcp_connection` raises
`ConnectorError` for `transport: stdio` when `connector_stdio_enabled` is false. `connector_specs()`
reaches `api/runner.py:824` unwrapped — **(re-verified: no `except ConnectorError` anywhere in
`api/` or `agent/`)**. Startup calls the offending bundle `unprobed`, which is explicitly not
counted unhealthy and does not trip `connectors_required`, while every turn dies before any tool
binds. This inverts `transport.py`'s own stated trade — *"losing a capability is a much smaller
failure than losing the turn"* — one function earlier in the same package.

### H5 — A *disabled* bundle's malformed manifest breaks the whole deployment
`connectors/registry.py:146-155`: `discovered()` parses and validates every bundle on disk before
`enabled()` (`:188-205`) applies `connectors_enabled`. **(re-verified: `discovered()` is a
comprehension over every bundle dir and `enabled()` calls it first.)** Every caller of `enabled()`
raises — the agent build, `bearer_token_env_names()` (the log-redaction filter),
`kg.note.known_note_types`, the health sweep. Directly contradicts `registry.py:7-9`:
*"Discovery is not enablement."*

### H6 — A manifest-declared tool the server no longer serves is dropped silently
`connectors/transport.py:325-334` intersects with the advertised set and logs nothing. The connector
stays `connected`, `/readyz` stays `healthy`, and `advertised_tool_names()`,
`state_changing_tool_names()`, the plan gate, the skills backend and `skill-validate` all still
count the missing name — so a skill gated on it is offered for a tool that cannot be called.
`make connector-validate` exits 0. Verified with a probe serving `echo` while declaring
`["echo","does_not_exist"]`: bound `['echo']`, zero log output.

**Nothing anywhere validates argument schemas** — not on either side. `load_mcp_tools` takes the
server's `inputSchema` verbatim; `assert_manifest_matches` checks names and classification only.
`MODULES.md` states "same argument names" as what makes `chem`/`safety` drop-in *replacements* for
Chemclaw3's in-tree bundles, and that claim is unchecked in both repositories.

### H7 — The unreducible floor exceeds the whole context budget
Measured with the estimator the policy itself uses:

```
count_tokens_approximately(8 x 60,000-char ToolMessages) = 120,040 tokens
static prefix (measured 2026-09-04, tests/test_context_floor.py)  =  42,730
                                                          floor  = 162,770
agent_context_token_budget                                        = 100,000
```

Both halves are untouchable by design: `compaction.py:209` sets `keep=max(self.keep,
newest_batch_size(messages))` so the newest batch survives structurally, and D-2026-09-04 made the
prefix a charge rather than a subtraction. `llm_context_window_tokens` is still 0, so nothing else
bounds it. Reviewer B's H8 compounds this by up to 3.98×.

### H8 — `defang` runs after the size bound and can undo it
`framing.py:118-121`'s second pass replaces **every** `<` with `&lt;` (1→4 chars) once an invisible
character reveals a disguised tag; `frame_connector_results` (outer) runs it after
`bound_tool_results` (inner) has cut to 60,000. **(re-verified at `framing.py:118` and the nesting
at `langgraph_agent.py:908` vs `:917`.)** Measured: crafted payload → **239,091** chars (3.98×);
realistic half-`<` payload → 149,587 (2.49×). The ordering is deliberate and well argued — the cut
must not sever the closing tag — but the expansion it permits is unconsidered, and it qualifies the
claim in `CLAUDE.md` that an oversized connector result stays bounded "for two independent reasons".
Well-formed, yes; bounded, no.

### H9 — Provenance for a second chemist is lost by two independent routes
The two must be fixed together; either fix alone will measure as a success while the other stays
open. **(both mechanisms verified by the coordinator.)**

- **Primitives.** `science/calc/store.py:452` returns on a cache hit *before*
  `publish_stored_result` at `:470`, so a second identical run never enqueues at all.
- **Composites.** `publish/hooks.py:174` wraps the *tool* rather than `cached_compute`, so it does
  enqueue — and `outbox.py:45-49`'s `ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING` then
  drops the row *including its `publications` document*. The sink's own primary key is
  `(calc_ref, tenant_id, session_id, job_id)` (`schema/result-store/001_core.sql:266`), built to
  hold several. Measured: alice 1 row, bob 0, one stored document naming alice.

### H10 — Publishing is at-most-once, and a lost tool composite has no backfill source
`store.py:463-470` commits the cache row, *then* enqueues in a separate transaction, and the enqueue
swallows every failure (`outbox.py:217-225`). Every later call is a cache hit that returns before
reaching the enqueue. `backfill_cached` recovers `calculation_results` rows and `backfill_jobs`
recovers `job_records` rows — but the tool hook publishes `ThermochemistryResult`/`LogdResult`,
which by construction are in **neither** table, and `connectors/results/workflows.py:_walk` calls
only those two walks. A dropped composite is permanently unrecoverable.

### H11 — An unprojectable result is dropped in total silence
`outbox.py:267-269` returns 0 with a `logger.debug` line when `projector_for(...)` is None, where a
projector that *raises* increments `chemclaw_result_projection_failures_total` (`:284-307`).
Measured: an unregistered `payload_kind` gave `written=0`, `counters_moved={}`,
`records_at_INFO=[]`. A new connector job publishes nothing and every dashboard, alert and gauge
reads normal.

### H12 — The documented retry contract does not exist
`driver.py:34-36` states that the `SinkRejectedError` distinction *"decides whether the outbox tries
again"*. `publish_results.py:105-155` catches both families and re-raises neither, and
`mark_failed` is called identically in both branches, so `durable/publish.py:133`'s non-retryable
listing is unreachable from the drain. Measured: a sink whose target has no tables spent all 8
attempts on a fault that fails identically forever — 2 h of retries at the shipped 15-minute
schedule before dead-lettering.

### H13 — An external vector store cites cross-model garbage after a model swap
`retrieval/external_note_index.py:128-143` overrides `search_dense` with a bare store call, dropping
the `embedding_key` predicate the base class binds precisely to stop *"cross-space garbage cited as
evidence"* — a paragraph that sits eleven lines below the tie-break paragraph of H3, in the same
file. The subclass namespaces the catalogue key and the rebuild, not the read. Measured: after
switching `embedding_model`, the external store returned `[('note-old-model', 0.9939)]` where
pgvector returned `[]`. Unbounded until a reindex, and the citations resolve, so it looks correct.

### H14 — The effect ledger's operator read path has no production caller
`durable/effect_ledger.py:164` (`unsettled()`), `:181` (`effects_for_session()`) and `:193`
(`class Unsettled`) are referenced nowhere in `src/`, the CLI, the API, `deploy/` or `infra/` —
only from `tests/test_effects.py:120,129`. **(re-verified by the coordinator: the only two `src/`
hits for the word are unrelated prose.)** The *write* path is live
(`durable/connector_job.py:58`), and three docstrings claim the read is too, in the present tense:
`:9` *"`unsettled` is the query an incident starts from"*, `:194` *"the sentence an operator needs
beside it"*.

Aggravating rather than mitigating: `operations/evidence_pack.py:282` re-implements the same read
inline with the **opposite** ordering, and `evidence_pack.py:150` documents that its oldest-first
ordering drops *"the ones an incident is actually about"* — the dead function is the one with
`ORDER BY attempted_at DESC`. So the ledger has a live reader with the wrong order and a
right-ordered reader nothing calls. In neither `BACKLOG.md` nor `DEFERRED.md`.

---

## MED

**M1 — A synchronous calc tool budgeted 3,600 s behind a 600 s turn ceiling.**
`compute_atomic_descriptors` and `compute_surface_potential` are inline `tools:`, not `jobs:`, and
pass `calc_atomic_timeout_seconds` (3,600) to `cached_remote`
(`connectors/calc/server/tools.py:961,995`) behind `request_timeout: 600` = 
`service_turn_timeout_seconds`. The rule that a client bound must not be shorter than the server's
is argued at `core/config/calculators.py:335-341` and asserted at the process hop
(`tests/test_calc_tools.py:97`) and the pod hop (`tests/test_deploy_chart.py:3349`) — and not at the
agent hop, which is the one that runs. A 600–3,600 s call can never complete inline while holding a
`servers/calc` admission slot to its own ceiling. `sample_conformers` (14,400 s) is correctly a
durable job; these two are the exception.

**M2 — Refusals composed by the two outer converters bypass the size ceiling.**
`bound_tool_results` sits at chain index 3; `surface_authorization_denials` (0) and
`surface_domain_errors` (1) *manufacture* the `ToolMessage` above it, so
`tool_result_size.py:36-41`'s claim to be *"applied at the one place every tool result passes"* is
false for them. Both interpolate model-authored text. Measured on the real chain: a 200,000-char
malformed-argument document produced a **200,254**-character result; a 150,000-char invented tool
name produced a **150,141**-character refusal, against a 60,000 ceiling. The repeat guard keys on
name+args, so each invented name is a fresh call.

**M3 — A successful computation is discarded when `store.put` fails.**
`store.py:462-479`: a `put` exception is caught by `except BaseException`, set on the future and
re-raised, so a potentially 19-minute CREST result reaches neither leader nor waiter. Nine lines
later `publish_stored_result` argues the opposite for itself — *"a results store that cannot be
queued to is strictly less important than returning the science"* — and the same argument applies to
a transient Postgres fault on the write.

**M4 — Artifact eviction and the cache row are pruned by different keys.**
`artifact_eviction.py:62-100` deletes `artifact_blobs`, `calculation_artifacts` cascades, and
`calculation_results` keeps a row whose payload names the deleted hash. `ArrayOffloadingStore.get`
treats that as a miss; `PostgresStore.get`, `find`, `publish/backfill.py:39-45` and
`postgres_store.known()` all serve it. So `find_calculations` renders a content hash
`fetch_artifact` will not resolve.

**M5 — Rewriting an offloaded array leaks an unreachable blob, and both eviction triggers ship off.**
`postgres_artifacts.py:45-54` upserts to a new hash with nothing deleting the old; measured
`blobs=2 links=1 unlinked=1`. `artifact_store_max_bytes = 0` and `artifact_evict_idle_days = 0`
while `artifact_store_enabled = True`, so neither bound exists in the shipped configuration —
though `durable/retention.py:346` records the bound as a fact. The "a knob that renders nothing is
not a knob" shape.

**M6 — `CALCULATION_EPOCH` is invisible on the row and in the published record.**
No epoch column on `calculation_results`; `publish/record.py:550-554` carries `calc_version`,
`input_hash`, `params_hash` and no epoch. The cache is correctly protected (exact-key), but
`backfill_cached` projects epoch-1 rows — per the epoch log, wrong linear-rotor S and G, incomplete
reactivity panels — into the external scientific record beside epoch-2 rows for the same subject,
separable only by `computed_at`. `find_calculations` serves them the same way.

**M7 — `find_calculations`' own documented example matches nothing and defeats its guard.**
`connectors/calc/server/tools.py:320` offers `calc_type` *"e.g. `xtb`, `pka`, `dft`"*. Matching is
exact equality and the real types are `xtb.sp`, `xtb.fukui`, `xtb.hess`. Worse, the
molecule-filter refusal tests `startswith(("xtb.", "geometry."))`, and `"xtb"` does not start with
`"xtb."` — so the combination the validator exists to refuse is accepted and answers `[]`, which the
same docstring instructs the model to report as *"the store has nothing"*.

```
calc_type='xtb.sp' -> 1     calc_type='xtb' -> 0     'xtb' + smiles -> accepted, 0 rows
```

**M8 — The always-loaded prompt never names `find_calculations`.**
`_INSTRUCTIONS` (13,274 chars): `find_calculations` **0**, `list_artifacts` 0, `fetch_artifact` 0,
against `find_past_jobs` 2 (*"check it before starting an expensive job"*), `find_notes` 3,
`gather_evidence` 3. The molecule-level "what do we already know" retrieval — the tool built for
exactly the question a chemist asks before committing compute — is reachable only through the
on-demand `computational-evidence` skill.

**M9 — The durable report path has neither the merge nor either budget cap.**
`retrieval/harness.py:204` is a flat concat of the ranked lists: no `_interleave_dedup`/RRF, no
`gather_evidence_max_chunks`, no `gather_evidence_max_chars`. Measured on the committed 38-note
corpus: 24 chunks, 12 distinct notes, 7 appearing more than once, worst three byte-identical copies
with three identical citations — in the PR-gated artifact a chemist signs, whose own docstring warns
that *"two agreeing-looking bullets are most likely to be read as two independent confirmations"*.
Scales as legs × `retrieval_top_k`.

**M10 — `chem.render_structure` returns an unbounded SVG.**
`depiction.py:46` bounds *atoms* (250) and nothing bounds output. Measured over the wire:
erythromycin 71,107 chars, `C*250` 254,109. The caller's ceiling is 60,000 *divided by*
`batch_width`, so erythromycin breaches in any 2-wide batch; the head-and-tail cut leaves a
truncated XML fragment — no picture for the chemist, ~9k tokens paid for the fragment. It is also
the one `chem` surface that does not follow that server's stated rule of refusing past the bound.

**M11 — `rxnpredict` books `outcome="ok"` on a zero-success ensemble.**
`tools.py:228-249` continues past each `BaseException`; the guard at `:227` fires only when zero
predictors are *installed*. Measured with a predictor raising the `EgressForbidden` shape:
`{"consensus": [], "n_models_succeeded": 0}` with `isError=false`. A vanished checkpoint mount reads
as a healthy server.

**M12 — A missing bearer token degrades to "connector unreachable" without naming the variable.**
`identity.py:150-159` raises inside `session.initialize()`, which `transport.py:318` →
`absorb_connect_failure` renders as the *wrapping* `ExceptionGroup`'s type name. The health probe
hits unauthenticated `/healthz`, so `/readyz` and `chemclaw_connectors_unhealthy` stay green.
Contradicts `manifest.py:86-87` and `tests/test_helm_chart.py:383` (*"rather than degrading"*). The
`calc` backend hop is wrong in the other direction: `core/mcp_session.py:384-385` uses
`bearer_from_env`, which returns `None` on unset and sends no `Authorization` header at all.

**M13 — `/healthz/` and `/metrics/` are auth-exempted and then 404.**
`mcp_server_kit/auth.py:61-71` normalises the trailing slash specifically so a kubelet probe
configured as `path: /healthz/` makes the pod ready. Measured on all seven servers under uvicorn:
**404**, every one — FastAPI routes are exact and the `Mount("/")` swallows the redirect. The test
asserts only `status_code != 401`, which passes on a 404, so the fix's stated purpose is unmet and
its test cannot see that.

**M14 — Two servers have no readiness callable while every `deployment.yaml` says they do.**
The comment *"`/healthz` is real readiness here (503 until the corpus/model/backend loads), not a
constant 200"* is byte-identical in all seven; `pyexec` and `rxnlabel` pass no `readiness=`.
`rxnlabel`'s models are optional by design, so a pod whose checkpoint failed passes the probe and
takes traffic. "A README is not a gate", applied to a Kubernetes comment.

**M15 — The outbox grows without bound and drains at 400 records/hour/sink.**
`retention_result_publications_days` defaults to 0 (pruning disabled) and `failed` rows are kept
forever by design; `_DEAD_LETTERED` is an unindexed sequential scan run once per drain pass.
Throughput is one batch per run — 100 × 4/hour — with no back-pressure: `enqueue` never blocks,
refuses or samples, and `ChemclawResultOutboxStuck` cannot distinguish "drain stopped" from "drain
is structurally slower than production".

**M16 — Site schema drift is detected only at first write.**
`cli/validate_sinks.py:19-20` deliberately does not connect; `_known_columns` probes
`information_schema` at delivery. A missing table dead-letters the corpus. Worse,
`property_value.property REFERENCES property_definition` is seeded by a *separate manual command*
and `dialect.py:148` checks only the **local** registry — so a release adding a property publishes
rows the site's FK rejects, undetected anywhere. Measured on the missing-table case: **48
`information_schema` round trips for 5 rows**, because the column cache is never populated on the
failure path.

**M17 — An HTTP sink carries no idempotency key and accepts `verify_tls=False` from a manifest.**
`http.py:132-143` sends `{tenant_id, writer_version, contract_version, records}` with no batch id
and only a `content-type` header, so the receiver must dedupe on `calc_ref` alone. `verify_tls` is a
plain constructor kwarg reachable from `config:` in `sink.yaml`; the docstring says *"never set this
false"* and nothing checks it — `sink-validate` binds the signature, not the values. Plaintext
transport is refused only under `entra_required`, which is off by default (`http.py:47`,
`drivers/postgres.py:271`).

**M18 — `request_timeout` has no upper bound and no relation to the turn deadline.**
`manifest.py:122` is `gt=0` only; `calc` ships at exactly `service_turn_timeout_seconds`, so which
bound fires first is a race and the model may never receive the recoverable
`transport_error_result` the design intends. A third-party bundle may declare
`request_timeout: 100000` and the manifest accepts it (verified).

**M19 — The collision guard misses template launchers on the first agent build in a process.**
`registry.py:625-666` reads `registered_tools()`, but `_register_generated_tools` evaluates
`job_tools()` — which runs the check — before `template_tools()` is registered, while the docstring
at `:653` claims template launchers are covered. Build #1 succeeds and the connector *shadows* the
launcher (connector tools are appended after in-process ones and `ToolNode` keys by name); build #2
onward raises and kills the turn. `make connector-validate` exits 0.

**M20 — `calc`'s parse error echoes the caller's whole string.**
Measured on `"Q"*3000`: `calc` 3,018 chars, `chem` 152. It lands in a `ValueError`, the family
`_sanitize_tool_errors` passes to the model verbatim.

**M21 — `calculation_key`'s docstring — which is the prompt — is wrong about its own surface.**
Says *"one of the nine on this server"* over a `COMPUTE_TOOLS` of 17, and names
`compute_thermochemistry` as one of *"the two that have no key"* — a tool not on that server, for
which `calculation_identity` raises. A model following the docstring calls a tool that refuses. The
same drift runs through the declarations: `MODULES.md:216` says seventeen over a breakdown of 18,
the manifest declares 20, and inline comments say "eight" over 10 and "six" over 7.

**M22 — A fingerprint row whose record write did not land is retrievable at Tanimoto 1.00.**
`ingest/eln/ingest.py:41-108` writes across four transactions with the record last (deliberate, for
replay-skip), and `FingerprintReactionRetriever` applies `records.eligible` only when a filter is
given. Inside that window `gather_evidence` hands the model a perfect-similarity precedent that
`expand_note` refuses to open.

**M23 — The starved-leg alert fires on a healthy deployment.**
`fanout.py:177` counts survivors by `chunk.retriever` while both merges attribute a deduped note to
the *first* list that found it. Measured over 20 queries in hybrid mode: graph 1.00, vector 0.47,
lexical 0.10, with lexical keeping **zero on 8 of 18 queries**. No leg is structurally starved; the
metric cannot tell that from one that is.

**M24 — Two guards revert with the whole suite green.**
`core/fulltext.py:35`'s tokeniser can revert to the exact bug its own comment names: mutated in a
scratch copy, **706 tests pass** and `--cov-branch` reports **100% line and 100% branch** over the
module while mutated, because no test imports it and all 21 `search_lexical` fixtures are lowercase
ASCII. Measured effect: `tokens('HPLC purity')` → `['purity']`, `terms('HPLC')` → `(set(), set())`,
`'Übergangsmetall'` → `'bergangsmetall'`. Both callers are in-memory references, so this is a
*reference-fidelity* regression — and that module exists solely to stop the reference answering a
different question than the backend it stands in for. Second instance: deleting
`if not keep: return []` from `PostgresNoteIndex._retire_absent_ids` also survives all 706, while
the identical guard on the in-memory class *is* pinned.

**M25 — The model never selects a tool anywhere in `make test`.**
34 test files drive a scripted or fake chat model, with the `tool_call` authored by the test
(`tests/test_langgraph_connectors.py:184` scripts `{"name": "echo", ...}`). The corpus that would
test *selection* is only parsed: `tests/test_probe_coverage.py:87` asserts every agent-callable
tool is **named** in `data/evals/probes/`, never invoked — execution is `make eval`, which needs an
LLM and sits outside `make test`. So tool integration is proven from the `tool_call` onward and
never from the prompt, which is precisely where M8 (the unadvertised `find_calculations`) lives and
why nothing caught it.

**M26 — Dense retrieval is proven only against a token feature-hash.**
`embedding_provider` defaults to `"hash"` (pinned at `tests/test_config.py:96`), and
`core/embeddings.py:13-16` states it gives *"token-overlap cosine similarity — NOT neural-semantic
retrieval"*. `tests/test_vector_index.py:76` is nonetheless named
`test_reindex_then_dense_search_finds_the_semantic_note` and comments "found without any
id/substring overlap" — query and note share the tokens *epimerization*/*amide*/*coupling*, so
token overlap is exactly what passes it. Ranking mechanics and the embedding-key staleness rule
*are* genuinely proven; semantic behaviour is not.

**M27 — Two vector adapters have never met their servers.**
`retrieval/vectors/qdrant.py` (85% covered) and `databricks.py` (74%, the lowest module in scope)
run only against injected fakes, and `tests/test_vector_store.py:7` says so: *"the fake agrees with
the adapter about the calls, which is a different claim from the server agreeing with them."* H13
lives in this family.

**M28 — Three silent skip classes, and the deployment assurance under them.**
`tests/conftest.py:438-440` calls exactly three reporters (Postgres, Temporal, public-schema
shadowing); the authoritative run printed **no epilogue at all** for 36 real skips. 33 are helm
renders (already `BACKLOG.md:161`, and my measurement confirms the count exactly); **3 are
untracked** — `tests/test_migrations_are_additive.py:500,580,618` skip on truncated git history, so
the guard that an already-shipped migration was not edited does not run here. And the ~100 chart
tests that *don't* skip assert on template **source text**, not rendered YAML, as
`tests/test_helm_chart.py:20-30` states outright. This sandbox's entire deployment assurance is
grep-over-templates plus 33 skipped renders.

---

## LOW and NIT

- **Verbatim third-party error text reaches the model.** `tool_authz.py:158-194` returns the
  server's words unchanged, justified by `connectors/server.py:568`'s sanitizer — which only runs in
  servers this repository hosts, while `D-2026-08-09-a-connector-we-do-not-run` opens the seam to
  third parties. Size is capped; content is not.
- **A helper's `files` cross into the caller's checkpointed state unbounded** — model-controlled and
  durable, though a later `read_file` *is* bounded, so this is checkpoint growth rather than prompt
  flooding.
- **A concurrent `task` fan-out shares the budget's accounting but not its enforcement** off the
  request path (CLI, template activities), where `metered_turn_tokens()` is 0 and the ceiling
  degrades to N × budget.
- **The plan gate's one fail-open** is `if not session_id: return await handler(request)`
  (`plan_gate.py:474`), reachable on the CLI and on any template step whose identity carries no
  session id. `task` is in neither half of the side-effect partition, and the partition test —
  held over `registered_tool_names()` — cannot see it: 7 of 61 bound tools are outside it.
- **The static no-egress scan misses `__import__` and `importlib`** while its docstring claims
  `__import__` is covered; measured clean on `__import__("httpx")` and
  `importlib.import_module("socket")`. The runtime guard still catches the connect.
- **`chemclaw_mcp_egress_guard_armed` is truthful about *armed* and silent about *widened*.** No
  test asserts the deployment manifests omit `MCP_EGRESS_ALLOW`; there is no `MCP_EGRESS*` in
  `servers/*/deploy/*.yaml` at all, so it comes from image `ENV`, which a pod `env:` overrides.
- **`props.vapour_pressure` refuses below the melting point and has no upper bound**: toluene at
  5000 °C returns 15,606 bar, well above its 594 K critical temperature. The server's own
  refuse-rather-than-approximate rule applied on one side only.
- **`fetch_artifact` refuses every artifact in this release, by its own docstring** — permanently
  refusing surface still bound and still paying prefix tokens.
- **Stale prompt- and caller-facing text**: `dft` named as a live molecule-keyed calculator in
  `connectors/calc/server/tools.py:308,320` and `science/calc/store.py:285` after the tier was
  deleted; `descriptors` offered in the same list matches nothing (the real type is
  `developability`); `registry.py:349` says all shipped manifests declare `auth: mode: none` when
  all seven declare `bearer`; `registry.py:447` and `identity.py:203` still describe harvesting a
  role set deleted by `D-2026-08-26`.
- **Smaller storage races**: the backfill `LIMIT/OFFSET` walk can skip a row when a concurrent
  re-put stamps a new `created_at`; `_IN_FLIGHT` is keyed by the flat key alone, ignoring which
  store; `remote_key` catches `(KeyError, TypeError)` but the field constraints raise
  `pydantic.ValidationError`, so a malformed server answer reaches the chemist as a raw traceback;
  the UPSERT race keeps one writer's payload with another's `compute_seconds`, which is what
  artifact eviction ranks by; `_CLAIM` orders by a column that ties.
- **Nothing bounds `calculation_results.result`** — the only fences are `calc_hessian_max_atoms` and
  `crest_max_members`, each covering one family, so the cap is incidental rather than stated. A 4 MB
  payload wrote in 494 ms and read back in 139 ms; TOAST absorbed it.
- **Share document content is framed but not link-stripped** on the conversational path, so a
  `[[playbook-x]]` reaches the model verbatim inside the envelope. Delimiter forgery itself is
  closed: three probes (live tag, foreign nonce, zero-width obfuscation) all arrived escaped.
- **`harness.gather_section:212` sets `retrieval_failed` on any skip** and
  `ShareDocumentRetriever` skips whenever `note_type` is set, so every filtered report section on a
  share-enabled deployment renders "Some retrieval sources failed… re-run required".
- **One tautological test and 36 that assert nothing.** `tests/test_tool_framing.py:329` asserts
  the index order of the literal list `tool_call_middleware()` returns — it cannot fail unless
  someone edits that literal, and proves nothing about how LangChain nests them; its value is
  carried entirely by `tests/test_middleware_order.py`, which spies inside
  `deepagents.graph.create_agent` and drives a real compiled graph. Separately, 36 tests contain no
  assertion at all; most are legitimate "must not raise", but the *positive* authorization ones are
  the weakest form available — `tests/test_authz.py:47`, `tests/test_tool_authz.py:126,142`,
  `tests/test_plan_gate.py:387,427` each pass against a gate replaced by `pass`. Their paired
  denial tests do use `pytest.raises`, so both directions are covered in aggregate.
  `tests/test_plan_gate.py:427`'s docstring additionally claims a property ("the gate still fails
  closed on the next call") the test does not check.
- **`kg/crosslink.py:53` `notes_for_calculation` has no production caller** — already on record in
  `D-2026-08-05-three-searches-that-disagreed-about-one-note.md:179`.
- **The three observer middlewares are blind to a `Command`** — latent, no producer today; worth an
  absence test in the style this repository already uses.

---

## What is solid

Named because a review that lists only defects misrepresents the tree, and because each of these
was *checked* rather than assumed.

**Integration.** `request_timeout` genuinely fires client-side (a 5 s tool behind `request_timeout:
1` raised at 1.02 s), with the httpx bound deliberately 5 s looser so the raising bound wins and
`cancel_on_timeout` bounding the call on the server too. The tool-name partition is total and
fail-closed; `extra="forbid"` holds at both manifest levels; cross-bundle tool/job collisions are
caught loudly. Per-connector degradation is real — confined cancel scope, concurrent gather, a
WARNING plus a counter plus a `CapabilityDegradedEvent`, and a reachability breaker with two
recovery paths. `follow_redirects=False`, `trust_env=False`, and identity headers *stripped* (not
merely skipped) off-origin from a derived producer set; `is_loopback_url` failed closed on every
hostile form tried. `stdio` refused by default.

**Governance.** Authorization covers middleware-supplied tools — driven on a compiled graph with
`entra_required` and `tool_authz_default=deny`, `task`, `write_todos`, `write_file` and `read_file`
were **all** refused. The `tool_result_shape.py` seam is real: one function, both rewriters, a
`Command`'s other update keys preserved (verified on a real `Command` — 80,037 → 60,000 with
`files` and `model_calls` intact). Exactly one `FilesystemMiddleware` (withholding `execute` and
`delete`) and one skills middleware, gates provably inside both registrars. `NarrowedSkillsBackend`
gates every reach path including `download_files`. Metric labels are clamped, so no unbounded model
string reaches `/metrics`. `TurnTotal.update` folds `base + Σ max(v-base, 0)`, so a fan-out's
branches sum rather than overwrite.

**Storage.** Epoch composition is real rather than asserted: nine distinct keys over a 3×3
client/server epoch grid, so a unilateral bump on either side re-addresses every row. The flat-key
bijection holds (`@` and `:` and whitespace refused in exactly the right fields). `stable_hash` is
order-independent, and `int ≠ float`, `{"solvent": None} ≠ {}`, `"1" ≠ 1`. Payload opacity is
intact — no `result ->` predicate anywhere. Single-flight is correct on all four paths, including
leader cancellation (waiters get `_Abandoned` naming the slot, `_IN_FLIGHT` empty) and a waiter's
own cancellation not killing the leader. Solvent naming cannot fork a key; SMILES are canonicalised;
`structure_id` rounds on construction. The offload write order is right — blobs first, row only if
every array landed, with `ON DELETE CASCADE`.

**Delivery.** Poison isolation works in both halves (10 records, one refused → 9 delivered, 1
failed, neighbours untouched). Attempt accounting is race-free: the claim spends the attempt inside
the same `UPDATE … FOR UPDATE SKIP LOCKED` that selects it. Exactly-once holds at the durable-job
seam — `job_records` upserts on `job_id`, `effects` is idempotent on `effect_id` with guards in both
directions, and a replay cannot double-write. No SQL injection surface: identifiers are literals
intersected with what `information_schema` returned, every value bound. Connections are built per
pass and closed in a `finally`.

**Retrieval.** No analogue of `D-2026-08-01` in the shipped merge: round-robin plus `(note,
content)` dedup plus a two-currency cap, measured 84/115/110 pre-merge → 52/63/34 kept over 20
queries with no leg at zero. Failure, skip and empty are three distinct facts end to end. Supersede
is correct — `is_current` is applied before both index legs and the survivors passed as `within=`.
The pgvector model-swap path is genuinely self-healing. Provenance is complete on the note-backed
legs, and `_citation` refuses to wikilink a non-slug id. No dead tier: every table has a live reader
and a live writer except `audit_anchors`, already documented as retired in three places. Twelve
mutants the suite *killed* are listed in the working notes, as calibration for the two it did not.

**The fleet.** Bearer enforcement on `/mcp` verified against seven **running** servers across six
bypass shapes, and fail-closed with the variable unset. `/metrics` leaks nothing about a caller —
driven with a secret actor and a secret tool argument, then scraped — and hostile tool names fold to
`tool="<unknown>"`. The identity header contract matches Chemclaw3's spellings exactly, pinned both
directions with transcribed literals. The egress guard covers exactly what it claims and no more
(bytes hosts included; `ctypes`, subprocess and `_socket` pass through *by construction*, as
documented). `load_dataset` refused all 14 corruption directions tried. `calc`'s admission ceiling
counts **cores, not calls** — one CREST call takes the whole budget of four. Process isolation kills
the group and leaves no partial result: a timed-out search returns an error, never a truncated
ensemble. The manifest ↔ served-surface check runs against a real uvicorn for all seven servers with
no `skipif` or `xfail`. The `pyexec` sandbox held on every probe, including the `uuid.os.chdir` jail
escape and a 4 GiB allocation.

**The suite's own honesty.** Mock usage is very low — nine files touch `unittest.mock`, there are
**zero** `assert_called` assertions in the tree, and no test in the nine modules in scope patches
the thing under test. `tests/middleware.py` drives the real `middleware.awrap_tool_call` rather
than re-implementing composition, and says why. Tool invocation is exercised against **real
uvicorn, real streamable-HTTP MCP and the real client** across six files — identity headers
actually landing, redirect-harvest refusal, body caps, request timeouts that cancel, session-per-turn
isolation. Storage and retrieval are proven against **real pgvector**: round-trip, upsert, `find`
parity between backends, bulk `known`, the outbox's claim/attempt/retire semantics and two-worker
claim splitting, 8-concurrent-misses → 1 compute, and D-011 across the wire.

---

## Suggested order of work

Grouped by what a fix buys, not by severity alone.

1. **H1** — one import and two lines in `Chemclaw3-mcp`, and it closes an authenticated remote
   crash of the pod that holds every long calculation. Nothing else here is this cheap or this
   severe.
2. **H2, H3, H13** — the three that make a *stored* answer wrong or unfindable. H3 is a one-line
   move of the tie-break into the outer sort, using the form two other modules already have.
3. **H4, H5, H6** — the connector seam's fail-total and fail-silent pair. H4 and H5 are the same
   shape (validation eagerly applied to bundles a deployment never enables) and are naturally one
   change.
4. **H9 + H10 + H11 + H12** — the publish path, which should be taken as one piece: the provenance
   gap has two causes, and a fix to either alone will measure as success.
5. **H7, H8, M2** — reconcile the ceilings. Assert the product of the parallelism and per-result
   bounds against the budget, and either bound after `defang` or make the expansion pass idempotent
   in size.
6. **M1, M18** — extend the timeout invariant to the agent hop and give `request_timeout` an upper
   bound derived from the turn deadline. The invariant is already written and already tested twice;
   this is a third assertion, not a new argument.
7. **M8** — the cheapest usability fix in the list: name `find_calculations` in `_INSTRUCTIONS`.
8. **H14, M24** — the claims held only by a test: give the effect ledger's read path a caller (or
   delete it and keep the ordering fix in `evidence_pack.py`), and close the two surviving mutants.
   A guard whose only witness is 100% branch coverage is not witnessed. Then ask whether the class
   is wider.
9. **M25** — the gap under all of this: nothing in `make test` ever lets a model choose a tool, so
   every finding above about what the model is *told* (M8, M21, and the stale `dft` text) sits in a
   region the suite structurally cannot reach.

## What this review is not evidence about

No OpenShift cluster and no real Temporal broker, so every claim about durable execution here is
about the code and the dev server, not a deployed system. The `xtb` and `crest` binaries are absent
in this sandbox, so all seven of the fleet's skipped tests — and everything about those two engines
— is stub-driven; H2's aspirin measurement is the exception, and it ran a real GFN2 calculation.
The browser → tenant identity hop remains unproven for the reason `CLAUDE.md` already gives.

Deployment assurance in this sandbox is grep-over-templates plus 33 skipped renders (M28), and no
test in `make test` ever lets a model choose a tool (M25) — so the half of "tool usage" that is
about *selection* is argued here from the prompt and the docstrings, not measured. `make eval` is
where that would be measured, and it needs an LLM.
