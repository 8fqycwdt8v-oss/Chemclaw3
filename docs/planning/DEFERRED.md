# DEFERRED

Consciously postponed items — each with the reason it is *not now* and the trigger that would
revisit it. Default is "off-the-shelf, defer until measured".

**This is a register of what is still pending, not a log of what was decided.** A closed item is
**deleted** from it in the commit that closes it; its ADR is the record, and `git log` is the
history. Recording closure by appending a status note below a stale row is what turned this file
into nine chronological sections describing each other, three of which had gone false (D-154).

Rows are grouped by **what would have to change** — the only axis that makes a trigger checkable.
Each names an anchor in the tree, so any row can be verified with one `grep`.

## Gated on infrastructure this environment does not have

Real cluster, real Entra tenant, real HPC, real registry. Nothing here is closable offline.

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **HPC/DFT real integration** (SLURM, `submit_to_hpc`) | User deferred it (D-010); the mock spine proves the durable pattern and the early compute (xTB/GFN2 + ML predictors) covers near-term needs locally. The real Nextflow launcher (F5) sits behind the QM activities awaiting a cluster | Heavy QM/DFT accuracy is genuinely required **and** HPC access is provisioned |
| **No converged electronic structure kept** (STO-5) | Deferred with DFT itself. D-124/D-132 already define the media types (`density.restart`, `orbitals.molden`) and the link role in `science/calc/artifacts.py`; nothing writes them, because the cheap GFN2 path has nothing worth restarting from. Published measurement for when it matters: reusing a converged density cuts mean SCF iterations from ~33 to ~2 | DFT lands (the row above) |
| **Postgres RLS mirror of the graph** (KM-9) | Broad internal read access is fine for cross-project learning; a mirror adds a sync pipeline and a second source of truth against D-004 | Real, combinatorial project-level confidentiality requirements |
| **`knowledge/` as its own Git repo** | A subfolder is enough for v1 | A governance/confidentiality boundary requires the repo split |
| **The Snowflake ELN connection** | D-2026-08-04-the-schema-is-a-file built the whole engine and proved it against a fake driver: the source ships as a manifest whose `binding:` block *is* the site's schema, so attaching one is configuration. What is left needs the tenant itself — the client package in the sync worker's image, the credentials the binding names, and the site's real table and column names | A real Snowflake tenant is reachable |
| **Per-user reads from the warehouse ELN** | The warehouse connects as a service identity, so its row-level access control sees one principal and the deployment's answer to "who may see which reactions" is the view named in the binding. `agent/identity/obo.py` exists for exactly this and is dormant; exercising it needs a real tenant on both the Entra and the warehouse side | The Snowflake tenant above, plus an Entra tenant (`entra_obo_enabled`) |
| **Push-to-registry + `helm upgrade` rollout in CI** | D-117 deleted the stub job whose body was an `echo`, keeping the two root gates that do real work (image build + non-root smoke; chart render against the Kubernetes schemas). Writing the real rollout now would mean guessing a registry, a namespace and a credential shape — assertions about someone else's cluster | A real cluster, its registry and the credentials to reach both. Then a `deploy` workflow gated on the default branch with an `environment:` guard, `helm upgrade --install` (which runs the pre-deploy migrate Job first), and a dry-run to a dev namespace ahead of it |
| **Live-retriever drift over the deployment's own graph** | The KM-13 gold-set (D-056) scores `GraphRetriever` over the committed fixture corpus and the F10-F2 drift job re-runs it — a deployment-consistency tripwire (`durable/eval_drift.py`). Drift over a *live, changing* graph needs labelled cases that are deployment-local; the shipped graph has none | A deployment with a populated graph and local labelled cases exists — then score the live retriever on the drift cadence alongside the fixture tripwire |

## Gated on an upstream fix

Each has a local workaround that costs something, and each ends with deleting code rather than
writing it. Re-check these whenever `agent-framework-*` is bumped in `pyproject.toml`.

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **Anthropic streaming tool-call state** | `agent_framework_anthropic/_chat_client.py` keeps the tool call it is parsing on the *client instance* (`self._last_call_id_name`); an `input_json_delta` carries `name=""` and recovers its identity from that attribute. Two turns streaming through one client interleave and one turn's arguments are filed under the other's call id — a `tool_use` block with an empty name, Anthropic 400, 20 % of turns in a live 50-user run. Worked around by leasing one client per concurrent turn (D-123, `agent/agent_pool.py`), which costs a small pool of clients per pod | Upstream scopes that state to the response. Then delete `agent/agent_pool.py` and `tests/test_agent_pool.py`, and restore the single cached agent per profile in `api/app.py` |
| **Harness streaming 400** | `create_harness_agent` enables per-service-call history persistence *and* installs `MessageInjectionMiddleware`; while streaming the latter rebuilds the response via `ChatResponse.from_updates()`, dropping the sentinel `conversation_id` the former uses to tell the function-invocation loop to stop resending the transcript. The loop re-sent everything while history was independently re-injected, putting a `user` block between a `tool_use` and its `tool_result` — Anthropic 400 on 100 % of tool calls. Worked around by disabling per-service-call persistence (`agent/chemclaw_agent.py`) | `agent-framework-core` preserves `conversation_id` across that finalizer. Then drop the local override — `tests/test_harness_execution.py` fails if it is removed early |
| **Per-model-call history durability under the harness** | Follows from the row above: with the override in place a harness turn persists history per *run*, so a crash mid-turn loses that turn rather than resuming from the last model call. Acceptable because it is exactly what the non-harness path has always done | The upstream fix lands, restoring the option |
| **Prompt-cache control on the production provider** (REV-9) | `agent_framework_openai` contains zero occurrences of `cache_control`, so the mechanism is unreachable from the `openai_compatible` path the chart ships; MAF also exposes no hook for `tools`, which is the larger half of the prefix. Both fixes are upstream, not knobs here. Meanwhile the prefix is not byte-stable (`tools/list` is re-fetched per turn), so one flapping connector would invalidate it anyway | Upstream exposes cache control on the OpenAI-compatible client. Before building anything, read `chemclaw_cache_read_tokens_total` against `chemclaw_input_tokens_total` — the provider may already be caching unasked (`docs/guides/runbook.md` §(viii)) |

## Gated on a scale not yet reached

Each is a real optimization whose cost is currently zero. The measured current value is stated so
the trigger is checkable rather than a feeling.

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **Sub-quadratic playbook clustering** (KM-14, half) | `memory/playbook.py` pairwise Tanimoto is O(n²) — simple and exact. The corpus is **37 notes** | ~10⁴ reactions (~10⁸ comparisons per run); switch to per-reaction Postgres HNSW k-NN |
| **Substructure pattern-fingerprint prefilter** | `find_substructure_matches` bounds its scan to `substructure_scan_max_records` (5000) and warns on truncation, and matching runs in a worker thread under `substructure_match_timeout_seconds` (D-080), so both footguns are closed. Screening with a pattern fingerprint first would raise the ceiling, but ECFP bits cannot screen substructures soundly — it needs a dedicated pattern-fingerprint column and index. Honest residual: the wall-clock bound frees the caller and the event loop, not the CPU (RDKit exposes no interruption hook); killing the work outright needs a subprocess | The truncation warning fires in real use, past ~10⁴ molecules |
| **`within=` id-array scaling** | Retrieval eligibility ships the full eligible-id list as one SQL array parameter; fine at 10³–10⁴ notes | The corpus approaches ~10⁵ notes — then index type/tag/currency columns instead |
| **Per-key in-flight dedup in the calculation store** | Two *concurrent* misses on one key both compute (benign last-writer-wins upsert); serializing needs cross-process locking | Duplicate expensive runs (real HPC/DFT) become a measured cost |
| **Durable / rolling-window budget quota** | `api/budget.py` bounds a *running process's* runaway (the "$400 in twenty minutes" failure), which is what the per-turn loop cap left open. A quota surviving a restart or shared across pods needs a durable store and a window policy (per-day/per-month reset) — real value only under multi-tenant billing pressure | A deployment needs per-user spend fairness *across* restarts and pods — then back the counters with a Postgres table and a windowed reset, reusing the same `check`/`record` seam |

## Gated on a capability, source or licence not in scope

Each waits on a concrete need or an external artifact, not on us.

**Three capabilities share one blocker: model weights fetched at runtime.** D-089 rejects runtime
external data, and D-135 shipped the amendment — a dataset *may* be vendored into the image at build
time, checksummed and licence-labelled (`data/vendored/`, `ingest/sources/vendored/`). So the trigger
for all three has moved from "no mechanism exists" to "pick a licence-clean artifact and add the
build step", which is the same residual as BACKLOG's open *vendor a real third-party corpus*.

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **ML interatomic potentials** (ANI-2x/TorchANI, MACE-OFF/MACE-MP) | Researched under D-092: current `torchani` releases fetch pretrained weights from the Hugging Face Hub at first use rather than bundling them. `science/calc/conformers.py`'s CREST/GFN2 ensemble covers the near-term "cheaper than DFT, more realistic than one rigid conformer" need | The weights are vendored at build time as a reviewed infrastructure decision — never a quiet runtime fetch |
| **Retrosynthesis + reaction prediction** | Re-examined under D-092: the original prerequisite (spine + graph + fingerprint layers) is met, so what remains is only the data problem — AiZynthFinder fetches its USPTO models and stock file from a public host via `download_public_data` | Same vendoring escalation, **and** route planning is a real user need |
| **Tabular foundation model** (TabPFN/TabICL) | "Which experiment next" is answered by BoFire inline (`suggest_next_experiment`, D-024); a tabular FM is a different, non-critical capability (few-shot numeric prediction from a table) needing a model download and a licence check | Few-shot numeric-trend prediction over historic tables is a real need; check the model version's licence first |
| **Spectra and images** (multimodal analytical data) | Text/CSV/TSV and PDF/PPTX/DOCX/XLSX ingress all landed (AGT-3, D-089), each read through the format's own document model. What is left needs OCR/vision — which is why a *scanned* PDF is refused by name rather than guessed at. The gated item in `docs/archive/plans/parity-plan.md` | OCR/vision is adopted |
| **Universal ingest abstraction** | Two shapes of the same question. `IngestHalf = ElnAdapter`, so a future non-ELN source must expose `fetch_new_entries`/`map_to_ord`, and both current adapters are datetime-cursored — acceptable while every ingest source is reaction-shaped (it maps to the canonical ORD reaction). D-120 also lowered the price of *not* generalizing: a new source is a manifest folder, so the duplication a shared adapter would remove is small | A third real ELN source, or the first non-reaction-shaped one — then generalize the ingest half's mapped type and the cursor contract together |
| **Durable multi-step "deep research" as a Temporal workflow** | Research is interactive Q&A (MAF's job); `gather_evidence` plus the `deep-research` skill cover it conversationally without a durable job | A single research question needs many expensive fan-out steps that must survive a restart |
| **F10-B3 — LLM faithfulness check of drafted report sections** | The conversational verifier scores a chat turn's cited prose. The *durable* report path assembles evidence per section and renders a template — there is no free-form synthesized prose in the workflow to judge, only citations, which `verify_claims` already gates | The report workflow gains an in-workflow prose-synthesis step — then route that prose through `verify_answer` exactly as the chat runner does |
| **Audit chain: provable disposal** (STO-13) | `durable/retention.py` refuses to prune `audit_events` at all, because deleting from a hash chain is indistinguishable from the tampering it detects — so the one table with no upper bound on its size also has no disposal path. The *completeness* half of this row is closed (D-2026-08-01-a-restore-is-a-truncation-nobody-can-see: a signed high-water anchor the verifier accepts), and it does not answer disposal: an anchor makes a shortened trail **visible**, which is the opposite of making one **permissible**. Disposal needs archive-then-reseal — export the pruned run, prove the export, and re-seal the remainder — and every step of that is a claim a QA function has to accept, not a mechanism a repo can choose | A regulated deployment requires provable disposal (a retention obligation that *mandates* deletion, not merely permits it) — then design archive-then-reseal against that obligation, with QA sign-off |
| **Hazard screening beyond structural alerts** (D-080) | The shipped screen is deliberately a *structural alert* layer: deterministic, offline, citable, advisory. GHS/SDS data, ADMET prediction, thermal-stability data and route-level verdicts each need a licensed source, a model with its own validation burden, or a claim the system must not make. Bolting any on would blur the one invariant that makes it usable — the system flags, it never certifies | A named, licensed hazard data source is procured, or a regulated deliverable requires a documented hazard assessment — then design it as its own layer with its own review, not as more rules in `science/safety/rules.yaml` |
| **PMI/E-factor as a BO objective** (IDEA-3, second half) | The *tool* half shipped (`green_metrics`). An objective needs a real formulation/solvent dataset this repo does not have; inventing a problem space to host it would be a one-caller abstraction over fabricated chemistry | A real formulation case with mass data |
| **Blocking a low-confidence answer on the durable hold** | The verifier stamps `review_required` when confidence falls below the threshold (`api/runner.py`), and the durable hold it would route into already exists (`durable/interaction_approval.py`, D-032) — but nothing connects them, so a weakly-grounded answer is *marked*, not withheld. Deliberate for now: blocking a chat answer on a human click is a UX and a GxP decision, and the surface that renders the button is the frontend repo's, not this one's. (`agent/verifier.py` claimed this row existed before D-154; it did not, and it also called the hold itself deferred, which it has not been since D-032) | A surface renders the hold, **and** a deployment decides an unverifiable answer must be withheld rather than flagged — then route `review_required` into `InteractionApprovalWorkflow` and ADR the UX contract |
| **Lab automation / SiLA2 closed loop** | Requires real instrument integration; out of v1 scope | Physical/robotic execution enters scope |
| **Process flowsheet synthesis/simulation** | A separate capability area (e.g. Aspen HYSYS) | Process design, not just reaction design, is in scope |
| **Domain foundation models** | Heavy; a general LLM plus tools suffices for v1 | Task accuracy plateaus and a domain model is justified |
| **Per-bundle `log.md` changelog** (OKF, D-074) | Dropped as designed and redeferred as a redesign: every note lands on its own PR-gate branch, so N concurrent proposals appending to one file manufacture merge conflicts to duplicate what git history already holds. The sound form is a *generated* view (`git log` → rendered changelog) | A reviewer or auditor asks for a changelog view that does not require `git log` |
| **JS test infrastructure** | `api/static/app.js` is covered by `node --check` only; no JS test runner exists in the repo, and the client is a demo shell | The web client grows beyond a demo shell |

## Deliberately declined

Not pending. Listed so each is not re-proposed as an oversight — the trigger column says what, if
anything, would reopen the question.

| Item | Why declined | Would reopen it |
|---|---|---|
| **External literature/patent retrievers** (TOOL-6) | **This system takes no external sources** (D-089). It was built against PubChem, reviewed and removed. The earlier wording here — "blocked on choosing a source" — is what invited the build, so `tests/test_no_egress.py` now enforces the decision, because prose in this file demonstrably did not | Nothing. A future need for external data is a new architectural decision, not a resumption of this one |
| **Second queue system** (pg-boss) | Temporal already runs, and its `background-jobs` queue covers small jobs (D-006) | Nothing |
| **LLM summarization of compacted history** | The deterministic collapse-plus-window (D-025) reclaims context with no LLM call, and MAF flags an untrusted summarizer as an indirect-prompt-injection risk that then *persists* in history | Token-frugal collapse proves insufficient (essential older context lost) **and** a trusted summarization client exists — then add it as the first strategy in the composed budget |
| **MAF Durable Extension for jobs** | Temporal owns durability (D-011); the extension is Azure-Functions-native and job-inappropriate | Only very long *conversation* pauses — days awaiting human input — which is a different problem from job durability |
